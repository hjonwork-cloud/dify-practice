"""배민·오늘얼마 경쟁 시뮬레이터(BT) 전용 SQLite 저장소.

portal_db 와 같은 DB 파일(portal_activity.db)을 사용하되 테이블은 bt_ 접두사로 분리한다.

테이블
  bt_param            : 시뮬레이터 파라미터 (수수료·판정 밴드·반영률·전환율·알림 메일)
  bt_sku_exclusion    : 3배치 운영 SKU 수기 제외 목록
  bt_sku_state        : 4121 수발주 최신 상태 (시간당 스냅샷 비교 기준)
  bt_sku_event        : 상태 변화 이벤트 (열림/막힘/신규/삭제/자재그룹/수기제외/해제)
  bt_snapshot_run     : 스냅샷 실행 로그 (run_key=YYYYMMDDHH, 중복 실행 방지)
  bt_sku_daily        : 일 확정 운영 SKU 버전 (snap_date, product_code)
  bt_sim_scenario     : 시뮬레이션 시나리오 저장
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading

import portal_db

logger = logging.getLogger(__name__)

_init_lock = threading.Lock()
_initialized = False

# 파라미터 기본값 (사용자 확정 2026-10-20)
#  - 배민상회 당사 수수료 5.5%, 배민상회 타사 6.6%, 오늘얼마 4.9%  ← 모두 VAT 포함 고지율
#  - 비교가 = 평균 판매단가(당사 공급가, VAT 제외)
#  - 승·무·패 밴드 ±2%, 수요 반영률 승 100 / 무 50 / 패 10
#  - 손익 구조(총액 인식, VAT 제외)
#      매출액 G = 등록가 ÷ VAT
#      매출총이익 = G − 매입원가
#      변동비  = 운반비(콜당 call_cost ÷ call_revenue) + 하역비 + 기타1(브랜드수수료 + 보험료 + 포장비)
#      공헌이익  = 매출총이익 − 변동비
#      고정비  = 물류간접비 + 기타2(other2_pct − vat_credit_pct)
#      영업이익  = 공헌이익 − 고정비
#  - 브랜드수수료는 고지율이 VAT 포함이므로 손익 계산 시 ÷VAT 해서 공급가 기준으로 환산
DEFAULT_PARAMS: dict[str, float | str] = {
    "fee_our_baemin":      5.5,   # 당사가 배민상회에서 판매 시 수수료(%) · VAT 포함
    "fee_our_todaysales":  4.9,   # 당사가 오늘얼마에서 판매 시 수수료(%) · VAT 포함
    "fee_comp_baemin":     6.6,   # 배민상회 타사 셀러 수수료(%)
    "fee_comp_todaysales": 4.9,   # 오늘얼마 타사 셀러 수수료(%)
    "tie_band_pct":        2.0,   # 무승부 밴드 ±%
    "reflect_win":         100.0, # 수요 반영률(%) — 승
    "reflect_tie":         50.0,  # 수요 반영률(%) — 무
    "reflect_lose":        10.0,  # 수요 반영률(%) — 패
    "conversion_pct":      100.0, # 플랫폼 전환율(%) — 전사 수요 중 플랫폼 주문으로 전환 가정
    "demand_days":         28,    # 예상수량 산정 기간(일) — 최근 4주
    "gp_alert":            10.0,  # GP 경보(%)
    "gp_warn":             20.0,  # GP 주의(%)
    "cm_floor_pct":        8.0,   # 공헌이익율 하한(%) — 추천가·시나리오 하한
    # ── 변동비 ──
    "call_cost":           11000.0,  # 운반비 콜당 단가(원)
    "call_revenue":        150000.0, # 콜당 예상 매출액(원) — 운반비율 = call_cost ÷ call_revenue
    "unload_pct":          4.05,  # 하역비율(%)
    "insurance_pct":       0.0,   # 보험료율(%) — 기타1
    "packing_pct":         0.06,  # 포장비율(%) — 기타1
    # ── 고정비 ──
    "logi_indirect_pct":   1.3,   # 물류간접비율(%)
    "other2_pct":          2.33,  # 기타2 원천 비율(%)
    "vat_credit_pct":      0.19,  # 수수료 공통매입세액(%) — 기타2에서 차감
    "alert_email":         "",    # 수발주 🔴 이벤트 알림 수신 메일 (콤마 구분)
}

EXCLUDE_REASONS = {
    "not_target": "경쟁 대상 아님",
    "bulk":       "대용량·업소 전용",
    "eol":        "단종 예정",
    "quality":    "품질 이슈",
    "etc":        "기타",
}

EVENT_LABELS = {
    "block":     "막힘",
    "open":      "열림",
    "new":       "신규 코드",
    "removed":   "목록 이탈(삭제)",
    "group_in":  "자재그룹 5140 편입",
    "group_out": "자재그룹 5140 이탈",
    "exclude":   "수기 제외",
    "release":   "수기 제외 해제",
}


def _connect():
    return portal_db._connect()


# ── 인덱스 손상 자가복구 ─────────────────────────────────────────────────────────
#  Azure /home(네트워크 공유) 위 SQLite 는 재시작·동시쓰기 중 인덱스가 깨지는 일이 있다
#  (2026-10-21 실측: idx_bt_event_ts / idx_bt_event_code → "database disk image is malformed").
#  테이블 데이터는 온전하므로 BT 인덱스만 REINDEX 하면 복구된다.

_BT_INDEXES = ("idx_bt_event_ts", "idx_bt_event_code")
_heal_lock = threading.Lock()


def _reindex_bt(reason: str) -> bool:
    with _heal_lock:
        try:
            with _connect() as conn:
                for ix in _BT_INDEXES:
                    conn.execute(f"REINDEX {ix}")
            logger.warning(f"[bt_db] 인덱스 재생성 완료 ({reason})")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[bt_db] 인덱스 재생성 실패 ({reason}): {e}")
            return False


def _check_bt_indexes() -> None:
    """기동 시 1회: bt_sku_event 인덱스 무결성 점검 → 이상 시 REINDEX."""
    try:
        with _connect() as conn:
            bad = [r[0] for r in conn.execute("PRAGMA quick_check(20)").fetchall() if r[0] != "ok"]
        if any(ix in msg for msg in bad for ix in _BT_INDEXES):
            _reindex_bt("startup quick_check")
    except sqlite3.DatabaseError as e:
        _reindex_bt(f"startup: {e}")


def _read(sql: str, args=()) -> list[dict]:
    """읽기 전용 조회 — 손상(malformed) 시 인덱스 재생성 후 1회 재시도."""
    for attempt in (1, 2):
        try:
            with _connect() as conn:
                return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.DatabaseError as e:
            if attempt == 2 or "malformed" not in str(e):
                raise
            _reindex_bt(str(e))
    return []


def _now() -> str:
    return portal_db._now()


def init() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bt_param (
                    key        TEXT PRIMARY KEY,
                    value      TEXT,
                    updated_by TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bt_sku_exclusion (
                    product_code TEXT PRIMARY KEY,
                    product_name TEXT,
                    reason_code  TEXT NOT NULL DEFAULT 'etc',
                    memo         TEXT,
                    release_date TEXT,
                    is_active    INTEGER NOT NULL DEFAULT 1,
                    created_by   TEXT,
                    created_at   TEXT,
                    released_by  TEXT,
                    released_at  TEXT
                );
                CREATE TABLE IF NOT EXISTS bt_sku_state (
                    product_code   TEXT PRIMARY KEY,
                    product_name   TEXT,
                    material_group TEXT,
                    use_hold       TEXT,
                    present        INTEGER NOT NULL DEFAULT 1,
                    first_seen     TEXT,
                    last_seen      TEXT,
                    updated_at     TEXT
                );
                CREATE TABLE IF NOT EXISTS bt_sku_event (
                    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_ts     TEXT NOT NULL,
                    run_key      TEXT,
                    product_code TEXT NOT NULL,
                    product_name TEXT,
                    event_type   TEXT NOT NULL,
                    severity     TEXT NOT NULL DEFAULT 'yellow',
                    before_val   TEXT,
                    after_val    TEXT,
                    mapped_cnt   INTEGER DEFAULT 0,
                    note         TEXT,
                    ack_by       TEXT,
                    ack_at       TEXT,
                    ack_note     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_bt_event_ts   ON bt_sku_event(event_ts DESC);
                CREATE INDEX IF NOT EXISTS idx_bt_event_code ON bt_sku_event(product_code, event_ts DESC);
                CREATE TABLE IF NOT EXISTS bt_snapshot_run (
                    run_key     TEXT PRIMARY KEY,
                    trigger     TEXT,
                    started_at  TEXT,
                    finished_at TEXT,
                    status      TEXT,
                    sku_total   INTEGER,
                    sku_active  INTEGER,
                    event_count INTEGER,
                    message     TEXT
                );
                CREATE TABLE IF NOT EXISTS bt_sku_daily (
                    snap_date    TEXT NOT NULL,
                    product_code TEXT NOT NULL,
                    product_name TEXT,
                    PRIMARY KEY (snap_date, product_code)
                );
                CREATE TABLE IF NOT EXISTS bt_sim_scenario (
                    scenario_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    name         TEXT NOT NULL,
                    platform     TEXT,
                    sellers_json TEXT,
                    params_json  TEXT,
                    summary_json TEXT,
                    created_by   TEXT,
                    created_at   TEXT
                );
                CREATE TABLE IF NOT EXISTS bt_listing_price (
                    product_code TEXT NOT NULL,
                    platform     TEXT NOT NULL,
                    list_price   REAL NOT NULL,
                    basis        TEXT,
                    memo         TEXT,
                    updated_by   TEXT,
                    updated_at   TEXT,
                    PRIMARY KEY (product_code, platform)
                );
            """)
        _check_bt_indexes()
        _initialized = True


# ── 파라미터 ────────────────────────────────────────────────────────────────

def get_params() -> dict:
    init()
    out = dict(DEFAULT_PARAMS)
    with _connect() as conn:
        for r in conn.execute("SELECT key, value FROM bt_param").fetchall():
            k = r["key"]
            if k not in DEFAULT_PARAMS:
                continue
            if isinstance(DEFAULT_PARAMS[k], str):
                out[k] = r["value"] or ""
            else:
                try:
                    out[k] = float(r["value"])
                except (TypeError, ValueError):
                    pass
    return out


def set_params(values: dict, updated_by: str) -> dict:
    init()
    now = _now()
    with _connect() as conn:
        for k, v in values.items():
            if k not in DEFAULT_PARAMS:
                continue
            if not isinstance(DEFAULT_PARAMS[k], str):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
            conn.execute(
                """INSERT INTO bt_param(key, value, updated_by, updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                       updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (k, str(v), updated_by, now),
            )
    return get_params()


# ── 상품별·플랫폼별 등록가 결정 ───────────────────────────────────────────
#  list_price = 플랫폼 등록가(VAT 포함, 1단위). basis = 결정 근거(lowest/floor/manual)

LISTING_BASIS = {"lowest": "최저가 맞춤", "floor": "하한가", "calc": "현재가 역산", "manual": "수기"}


def list_listing_prices() -> list[dict]:
    init()
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM bt_listing_price").fetchall()]


def set_listing_price(product_code: str, platform: str, list_price: float | None,
                      basis: str, memo: str, updated_by: str) -> None:
    """list_price 가 None/0 이면 결정 해제(삭제)."""
    init()
    with _connect() as conn:
        if not list_price or list_price <= 0:
            conn.execute("DELETE FROM bt_listing_price WHERE product_code=? AND platform=?",
                         (product_code, platform))
            return
        conn.execute(
            """INSERT INTO bt_listing_price(product_code, platform, list_price, basis, memo, updated_by, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(product_code, platform) DO UPDATE SET list_price=excluded.list_price,
                   basis=excluded.basis, memo=excluded.memo,
                   updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            (product_code, platform, float(list_price), basis if basis in LISTING_BASIS else "manual",
             memo or "", updated_by, _now()),
        )


# ── 수기 제외 ───────────────────────────────────────────────────────────────

def list_exclusions(active_only: bool = True) -> list[dict]:
    init()
    sql = "SELECT * FROM bt_sku_exclusion"
    if active_only:
        sql += " WHERE is_active=1"
    sql += " ORDER BY is_active DESC, created_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def active_exclusion_codes() -> set[str]:
    init()
    with _connect() as conn:
        rows = conn.execute("SELECT product_code FROM bt_sku_exclusion WHERE is_active=1").fetchall()
    return {r["product_code"] for r in rows}


def add_exclusion(product_code: str, product_name: str, reason_code: str,
                  memo: str, release_date: str, created_by: str) -> None:
    init()
    if reason_code not in EXCLUDE_REASONS:
        reason_code = "etc"
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO bt_sku_exclusion(product_code, product_name, reason_code, memo,
                   release_date, is_active, created_by, created_at, released_by, released_at)
               VALUES (?,?,?,?,?,1,?,?,NULL,NULL)
               ON CONFLICT(product_code) DO UPDATE SET product_name=excluded.product_name,
                   reason_code=excluded.reason_code, memo=excluded.memo,
                   release_date=excluded.release_date, is_active=1,
                   created_by=excluded.created_by, created_at=excluded.created_at,
                   released_by=NULL, released_at=NULL""",
            (product_code, product_name, reason_code, memo, release_date or None, created_by, now),
        )
    add_event(product_code, product_name, "exclude", "yellow",
              before_val="", after_val=EXCLUDE_REASONS[reason_code],
              note=f"{memo or ''} (by {created_by})".strip())


def release_exclusion(product_code: str, released_by: str) -> bool:
    init()
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT product_name FROM bt_sku_exclusion WHERE product_code=? AND is_active=1",
                           (product_code,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE bt_sku_exclusion SET is_active=0, released_by=?, released_at=? WHERE product_code=?",
            (released_by, now, product_code),
        )
    add_event(product_code, row["product_name"], "release", "yellow",
              before_val="제외", after_val="운영", note=f"by {released_by}")
    return True


def due_release_codes(today: str) -> list[dict]:
    """해제 예정일이 도래한 제외 건 (자동 해제하지 않고 후보로만 노출)."""
    init()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM bt_sku_exclusion
               WHERE is_active=1 AND release_date IS NOT NULL AND release_date<>'' AND release_date<=?""",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── 상태 스냅샷 / 이벤트 ────────────────────────────────────────────────────

def load_state() -> dict[str, dict]:
    init()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM bt_sku_state").fetchall()
    return {r["product_code"]: dict(r) for r in rows}


def save_state(rows: list[dict], missing_codes: list[str]) -> None:
    """rows: 이번 스냅샷에 존재하는 SKU, missing_codes: 이번에 사라진 SKU."""
    init()
    now = _now()
    with _connect() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO bt_sku_state(product_code, product_name, material_group, use_hold,
                       present, first_seen, last_seen, updated_at)
                   VALUES (?,?,?,?,1,?,?,?)
                   ON CONFLICT(product_code) DO UPDATE SET product_name=excluded.product_name,
                       material_group=excluded.material_group, use_hold=excluded.use_hold,
                       present=1, last_seen=excluded.last_seen, updated_at=excluded.updated_at""",
                (r["product_code"], r.get("product_name"), r.get("material_group") or "",
                 r.get("use_hold") or "", now, now, now),
            )
        for c in missing_codes:
            conn.execute("UPDATE bt_sku_state SET present=0, updated_at=? WHERE product_code=?", (now, c))


def add_event(product_code: str, product_name: str | None, event_type: str, severity: str,
              before_val: str = "", after_val: str = "", run_key: str | None = None,
              mapped_cnt: int = 0, note: str = "") -> None:
    init()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO bt_sku_event(event_ts, run_key, product_code, product_name, event_type,
                   severity, before_val, after_val, mapped_cnt, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_now(), run_key, product_code, product_name, event_type, severity,
             before_val, after_val, mapped_cnt, note),
        )


def _event_where(days: int, event_type: str, unacked_only: bool, exclude_5140: bool) -> tuple[list[str], list]:
    where = ["e.event_ts >= datetime('now','localtime', ?)"]
    args: list = [f"-{int(days)} days"]
    if event_type:
        where.append("e.event_type=?")
        args.append(event_type)
    if unacked_only:
        where.append("e.ack_at IS NULL")
    if exclude_5140:
        # FC전용상품(자재그룹 5140) 제외 — 현재(최종) 상태의 자재그룹 기준
        where.append("COALESCE(s.material_group,'') <> '5140'")
    return where, args


_EVENT_FROM = "bt_sku_event e LEFT JOIN bt_sku_state s ON s.product_code = e.product_code"


def list_events(days: int = 7, event_type: str = "", unacked_only: bool = False,
                product_code: str = "", limit: int = 1000, exclude_5140: bool = False) -> list[dict]:
    """이벤트 목록 + 해당 SKU의 현재 상태(cur_material_group / cur_use_hold / cur_present)."""
    init()
    where, args = _event_where(days, event_type, unacked_only, exclude_5140)
    if product_code:
        where, args = ["e.product_code=?"], [product_code]
    sql = (f"SELECT e.*, s.material_group AS cur_material_group, s.use_hold AS cur_use_hold, "
           f"s.present AS cur_present FROM {_EVENT_FROM} WHERE {' AND '.join(where)} "
           f"ORDER BY e.event_ts DESC, e.event_id DESC LIMIT ?")
    args.append(int(limit))
    return _read(sql, args)


def event_summary(days: int = 1, exclude_5140: bool = False) -> dict:
    """기간 내 이벤트 건수 집계 (LIMIT 없이 전체)."""
    init()
    where, args = _event_where(days, "", False, exclude_5140)
    rows = _read(f"SELECT e.event_type, e.severity, (e.ack_at IS NULL) AS unacked, COUNT(*) AS n "
                 f"FROM {_EVENT_FROM} WHERE {' AND '.join(where)} GROUP BY 1,2,3", args)
    out = {"total": 0, "red": 0, "unacked": 0, "unacked_red": 0, "by_type": {}}
    for r in rows:
        n = r["n"]
        out["total"] += n
        out["by_type"][r["event_type"]] = out["by_type"].get(r["event_type"], 0) + n
        if r["severity"] == "red":
            out["red"] += n
        if r["unacked"]:
            out["unacked"] += n
            if r["severity"] == "red":
                out["unacked_red"] += n
    return out


def ack_events(event_ids: list[int], ack_by: str, ack_note: str = "") -> int:
    init()
    if not event_ids:
        return 0
    now = _now()
    with _connect() as conn:
        cur = conn.executemany(
            "UPDATE bt_sku_event SET ack_by=?, ack_at=?, ack_note=? WHERE event_id=? AND ack_at IS NULL",
            [(ack_by, now, ack_note, int(i)) for i in event_ids],
        )
        return cur.rowcount or 0


def try_start_run(run_key: str, trigger: str) -> bool:
    """run_key 선점. 이미 같은 run_key가 있으면 False (manual 트리거는 접미사로 구분)."""
    init()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bt_snapshot_run(run_key, trigger, started_at, status) VALUES (?,?,?, 'running')",
            (run_key, trigger, _now()),
        )
        return (cur.rowcount or 0) > 0


def finish_run(run_key: str, status: str, sku_total: int, sku_active: int,
               event_count: int, message: str = "") -> None:
    init()
    with _connect() as conn:
        conn.execute(
            """UPDATE bt_snapshot_run SET finished_at=?, status=?, sku_total=?, sku_active=?,
                   event_count=?, message=? WHERE run_key=?""",
            (_now(), status, sku_total, sku_active, event_count, message[:2000], run_key),
        )


def list_runs(limit: int = 48) -> list[dict]:
    init()
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM bt_snapshot_run ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()]


def has_daily(snap_date: str) -> bool:
    init()
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM bt_sku_daily WHERE snap_date=? LIMIT 1", (snap_date,)).fetchone() is not None


def save_daily(snap_date: str, rows: list[dict]) -> None:
    init()
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO bt_sku_daily(snap_date, product_code, product_name) VALUES (?,?,?)",
            [(snap_date, r["product_code"], r.get("product_name")) for r in rows],
        )


def daily_counts(limit: int = 30) -> list[dict]:
    init()
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT snap_date, COUNT(*) AS cnt FROM bt_sku_daily
               GROUP BY snap_date ORDER BY snap_date DESC LIMIT ?""", (int(limit),)).fetchall()]


# ── 시나리오 ────────────────────────────────────────────────────────────────

def save_scenario(name: str, platform: str, sellers: list, params: dict,
                  summary: dict, created_by: str) -> int:
    init()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO bt_sim_scenario(name, platform, sellers_json, params_json, summary_json,
                   created_by, created_at) VALUES (?,?,?,?,?,?,?)""",
            (name, platform, json.dumps(sellers, ensure_ascii=False),
             json.dumps(params, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
             created_by, _now()),
        )
        return cur.lastrowid


def list_scenarios(limit: int = 50) -> list[dict]:
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bt_sim_scenario ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("sellers_json", "params_json", "summary_json"):
            try:
                d[k[:-5]] = json.loads(d.pop(k) or "null")
            except Exception:
                d[k[:-5]] = None
        out.append(d)
    return out


def delete_scenario(scenario_id: int) -> None:
    init()
    with _connect() as conn:
        conn.execute("DELETE FROM bt_sim_scenario WHERE scenario_id=?", (int(scenario_id),))
