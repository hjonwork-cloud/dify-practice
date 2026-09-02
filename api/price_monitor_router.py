"""외부 플랫폼 가격 모니터링 라우터 (배민상회/식봄 vs 우리 상품)."""
from __future__ import annotations

import os
import time
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import access_control
import portal_db

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portal/price-monitor", tags=["price-monitor"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml")),
)

_SESSION_COOKIE = "dongwon_portal_session"
_SESSION_SECRET = os.getenv("PORTAL_SESSION_SECRET", "dongwon-portal-dev-secret-change-me")

# 테스트 기간 중 관리자 전용
_ALLOWED_EMP_CODES = {access_control.ADMIN_EMP_CODE}

PLANTS = ["ALL", "4120", "4123", "4121"]
PLANT_LABELS = {"ALL": "전체센터", "4120": "4120(시화)", "4123": "4123(화성)", "4121": "4121(화성3배치)"}
PLANTS_REAL = ["4120", "4123", "4121"]  # ALL 제외 실제 플랜트
GP_ALERT_PCT = 10.0   # GP < 10% → 경보
GP_WARN_PCT  = 20.0   # GP < 20% → 주의

# ── POC 결과 인메모리 저장소 (파일 대신 메모리 사용) ────────────────────────
_POC_LATEST: dict | None = None  # poc-benchmark 완료 후 결과 저장

# ── 인증 헬퍼 ──────────────────────────────────────────────────────────────

def _get_session(request: Request) -> dict | None:
    """portal_router와 동일한 세션 파싱. 성공 시 user dict 반환."""
    try:
        import portal_router as _pr
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        emp_code = _pr._read_session(cookie)
        if not emp_code:
            return None
        user = _pr._portal_user(emp_code)
        return user
    except Exception as e:
        logger.warning(f"[pm] _get_session 오류: {e}")
        return None


def _require_pm_access(request: Request) -> dict:
    """가격 모니터링 접근 권한 확인. 현재는 관리자 전용."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    emp_code = session.get("emp_code", "")
    if emp_code not in _ALLOWED_EMP_CODES:
        raise HTTPException(status_code=403, detail="가격 모니터링 기능은 현재 관리자 테스트 중입니다.")
    return session


def _require_login(request: Request) -> dict:
    """전체 계정 공용 접근 헬퍼: 로그인 여부만 확인 (역할/베타 제한 없음).
    상품 조회/GP 요약, AI 코드매핑 데모 등 전 직원 대상 기능에 사용."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return session


def _require_admin(request: Request) -> dict:
    """관리자 전용 접근 헬퍼 (emp_code 화이트리스트가 아닌 is_admin 플래그 기준)."""
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    if not session.get("is_admin"):
        raise HTTPException(status_code=403, detail="관리자만 접근 가능합니다.")
    return session


def _render(request: Request, template_name: str, **ctx) -> HTMLResponse:
    session = _get_session(request)
    tpl = _jinja_env.get_template(template_name)
    ctx.setdefault("request", request)
    ctx.setdefault("user", session or {})
    ctx.setdefault("asset_v", "1")
    html = tpl.render(**ctx)
    return HTMLResponse(html)


# ── Databricks 쿼리 헬퍼 ────────────────────────────────────────────────────

def _q(sql: str) -> list[dict]:
    """Databricks SQL 쿼리 실행. main.run_query 래퍼."""
    import main as _main
    return _main.run_query(sql)


def _serialize_rows(rows: list[dict]) -> list[dict]:
    """date/datetime/Decimal 등 JSON 비직렬화 타입을 안전 변환."""
    import datetime
    from decimal import Decimal
    result = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, (datetime.date, datetime.datetime)):
                new_row[k] = v.isoformat()
            elif isinstance(v, Decimal):
                new_row[k] = float(v)
            else:
                new_row[k] = v
        result.append(new_row)
    return result


# ── 디스크 영구 캐시 ────────────────────────────────────────────────────────
# Azure App Service /home 경로는 재시작 후에도 유지됨 (Persistent Storage)
# 메모리 캐시 miss → 디스크 캐시 확인 → Databricks 조회 순서
# 앱 재시작 시 Databricks 콜드스타트 없이 즉시 응답 가능
import pickle as _pickle

_DISK_CACHE_DIR: Path = Path(
    os.getenv("PM_CACHE_DIR",
              "/home/pm_cache" if Path("/home").exists() else "/tmp/pm_cache")
)
try:
    _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    try:
        _DISK_CACHE_DIR = Path("/tmp/pm_cache")
        _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        _DISK_CACHE_DIR = None  # type: ignore  # 디스크 캐시 비활성화 (read-only 환경)

_disk_lock = threading.Lock()


def _disk_get(key: str):
    """디스크에서 캐시 읽기. (ts, data) 반환 또는 None."""
    if _DISK_CACHE_DIR is None:
        return None
    p = _DISK_CACHE_DIR / f"{key}.pkl"
    try:
        if p.exists():
            with open(p, "rb") as f:
                return _pickle.load(f)
    except Exception as e:
        logger.warning(f"[disk-cache] read 실패 {key}: {e}")
    return None


def _disk_set(key: str, ts: float, data: object) -> None:
    """디스크에 캐시 저장 (스레드 세이프)."""
    if _DISK_CACHE_DIR is None:
        return
    p = _DISK_CACHE_DIR / f"{key}.pkl"
    try:
        with _disk_lock:
            with open(p, "wb") as f:
                _pickle.dump((ts, data), f, protocol=_pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        logger.warning(f"[disk-cache] write 실패 {key}: {e}")


# ── 기준가/구매가 캐시 (메모리 5분 / 디스크 2시간) ──────────────────────────
_price_cache = {}
_CACHE_TTL        = 300     # 메모리 TTL: 5분
_CACHE_TTL_DISK   = 7200    # 디스크 TTL: 2시간


def _get_base_prices(plant: str) -> list[dict]:
    """최근 2주 기준가(매출액/매출수량) + 구매단가(매출원가/매출수량) (플랜트별)"""
    cache_key = f"base_prices_{plant}"
    # 1) 메모리 캐시
    if cache_key in _price_cache:
        ts, data = _price_cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data
    # 2) 디스크 캐시
    disk = _disk_get(cache_key)
    if disk and time.time() - disk[0] < _CACHE_TTL_DISK:
        _price_cache[cache_key] = disk  # 메모리에도 올림
        return disk[1]
    try:
        import main as _main
        plant_cond_bp = (f"`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
                         if plant == 'ALL' else f"`플랜트` = '{plant}'")
        rows = _q(f"""
            SELECT
                `자재`                                                        AS product_code,
                ROUND(
                    SUM(CAST(`매출액` AS DOUBLE)) /
                    NULLIF(SUM(CAST(`매출수량` AS DOUBLE)), 0) * 100
                , 2)                                                          AS avg_sale_price,
                ROUND(
                    SUM(CAST(`매출원가` AS DOUBLE)) /
                    NULLIF(SUM(CAST(`매출수량` AS DOUBLE)), 0) * 100
                , 2)                                                          AS avg_buy_price
            FROM {_main.T_MAIN}
            WHERE {plant_cond_bp}
              AND CAST(`년월` AS INT) >= YEAR(DATE_SUB(CURRENT_DATE(), 14)) * 100 + MONTH(DATE_SUB(CURRENT_DATE(), 14))
              AND `자재` IS NOT NULL
              AND `매출수량` > 0
              AND `매출원가` IS NOT NULL
            GROUP BY `자재`
        """)
        _price_cache[cache_key] = (time.time(), rows)
        _disk_set(cache_key, time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] base_prices 조회 실패 ({plant}): {e}")
        return []


# ── 전월 매출 캐시 (메모리 1시간 / 디스크 24시간) ────────────────────────────
_prev_sales_cache = {}


def _get_prev_month_sales(plant: str) -> dict[tuple, dict]:
    """전월(1개월 전) 상품·플랜트별 매출액·수량 합계 반환 ((product_code, plant) → dict)"""
    cache_key = f"prev_sales_{plant}"
    # 1) 메모리
    if cache_key in _prev_sales_cache:
        ts, data = _prev_sales_cache[cache_key]
        if time.time() - ts < 3600:
            return data
    # 2) 디스크 (24시간)
    disk = _disk_get(cache_key)
    if disk and time.time() - disk[0] < 86400:
        _prev_sales_cache[cache_key] = disk
        return disk[1]
    try:
        import main as _main
        plant_cond_ps = (f"`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
                         if plant == 'ALL' else f"`플랜트` = '{plant}'")
        rows = _q(f"""
            SELECT
                `자재`                                   AS product_code,
                `플랜트`                                  AS plant,
                SUM(CAST(`매출액`   AS DOUBLE)) * 100    AS prev_sales_amt,
                SUM(CAST(`매출수량` AS DOUBLE))          AS prev_sales_qty
            FROM {_main.T_MAIN}
            WHERE {plant_cond_ps}
              AND CAST(`년월` AS INT) = CAST(DATE_FORMAT(ADD_MONTHS(CURRENT_DATE(), -1), 'yyyyMM') AS INT)
              AND `자재` IS NOT NULL
              AND `매출수량` > 0
            GROUP BY `자재`, `플랜트`
        """)
        result = {(r["product_code"], r["plant"]): r for r in (rows or [])}
        _prev_sales_cache[cache_key] = (time.time(), result)
        _disk_set(cache_key, time.time(), result)
        return result
    except Exception as e:
        logger.warning(f"[price_monitor] prev_month_sales 조회 실패 ({plant}): {e}")
        return {}


def _get_prev_month_sales_totals(plant: str) -> dict[str, dict]:
    """전월 매출을 product_code 단위로 플랜트 합산 반환 (product_code → dict).
    매핑 모달, 대시보드 등 플랜트 구분 불필요한 조회에 사용."""
    per_plant = _get_prev_month_sales(plant)
    totals = {}
    for (code, _plant), v in per_plant.items():
        if code not in totals:
            totals[code] = {"product_code": code, "prev_sales_amt": 0.0, "prev_sales_qty": 0.0}
        totals[code]["prev_sales_amt"] = (totals[code]["prev_sales_amt"] or 0) + (v.get("prev_sales_amt") or 0)
        totals[code]["prev_sales_qty"] = (totals[code]["prev_sales_qty"] or 0) + (v.get("prev_sales_qty") or 0)
    return totals


# ── 운영상품 목록 캐시 (메모리 1시간 / 디스크 2시간) ───────────────────────
_product_cache = {}
_PRODUCT_CACHE_TTL      = 3600   # 메모리 TTL: 1시간
_PRODUCT_CACHE_TTL_DISK = 7200   # 디스크 TTL: 2시간

# ── 역인덱스 캐시: plant → (ts, tok_inv, prod_map) ──────────────────────────
# _get_our_products와 동일 TTL로 관리 — 상품 목록 갱신 시에만 재구축
_inv_cache: dict = {}

# ── AI 매핑 백그라운드 잡 스토어 ─────────────────────────────────────────────
# job_id → {status, progress, total_unmapped, items, error}
_job_store: dict = {}

# ── 플랫폼 SKU 캐시 (10분 TTL) ───────────────────────────────────────────────
# plat_rows 쿼리는 매번 Databricks를 치므로 웨어하우스 콜드스타트 시 hang 원인
# seller 단위로 캐시 → warmup에서 미리 채워두면 분석 즉시 응답
_plat_rows_cache: dict = {}
_PLAT_ROWS_TTL = 600  # 10분


def _get_plat_rows(platform: str, seller_name: str) -> list[dict]:
    """플랫폼 SKU 목록 조회 (10분 캐시).
    특정 셀러 요청 시 __ALL__ 캐시에서 필터링 우선 → Databricks 추가 조회 없음.
    warmup은 __ALL__만 채우면 모든 셀러 요청을 커버.

    ※ 캐시 키에 버전 접두사(v2)를 둔다. 과거 __ALL__ 쿼리의 LIMIT 5000 절삭으로
      인해 일부 셀러(예: 배민상회 현대그린푸드)에 대해 빈 리스트가 디스크 캐시
      (Azure /home, 배포/재시작 후에도 유지됨)에 영구 저장된 적이 있었다.
      LIMIT을 제거한 뒤에도 이 오염된 디스크 캐시 파일이 TTL 이내라면 그대로
      재사용되어 버그가 재현되므로, 키 네임스페이스를 바꿔 과거 캐시를 전부
      무효화한다.
    """
    cache_key = f"platv2_{platform}_{seller_name}"
    all_sellers = (seller_name == "__ALL__")

    # 1) 메모리 캐시
    entry = _plat_rows_cache.get(cache_key)
    if entry and time.time() - entry[0] < _PLAT_ROWS_TTL:
        return entry[1]

    # 2) 디스크 캐시
    disk = _disk_get(cache_key)
    if disk and time.time() - disk[0] < 3600:
        _plat_rows_cache[cache_key] = disk
        return disk[1]

    # 3) 특정 셀러 요청 시 __ALL__ 캐시에서 필터링 (Databricks 조회 없이)
    #    ※ __ALL__ 캐시도 TTL을 반드시 확인한다. 이전에는 메모리 캐시를 무기한
    #      재사용해 과거의 LIMIT 절삭 결과가 영구히 재사용되는 문제가 있었다.
    if not all_sellers:
        all_key = f"platv2_{platform}___ALL__"
        all_entry = _plat_rows_cache.get(all_key)
        if all_entry and time.time() - all_entry[0] >= _PLAT_ROWS_TTL:
            all_entry = None
        if not all_entry:
            all_disk = _disk_get(all_key)
            if all_disk and time.time() - all_disk[0] < 3600:
                all_entry = all_disk
        if all_entry:
            rows = [r for r in all_entry[1]
                    if r.get("platform_seller_name") == seller_name]
            # ※ 빈 결과는 캐시에 절대 저장하지 않는다. __ALL__ 캐시가 아직
            #   완전히 채워지지 않았거나 일시적 문제로 특정 셀러가 0건으로
            #   필터링되는 경우, 이를 그대로 캐싱하면 TTL 동안 계속 빈 결과가
            #   재사용되는 "미매핑 상품 없음" 오탐이 재발할 수 있다.
            if rows:
                ts = time.time()
                _plat_rows_cache[cache_key] = (ts, rows)
                _disk_set(cache_key, ts, rows)
                logger.info(f"[pm-ai] plat_rows {platform}/{seller_name} → __ALL__ 필터링 {len(rows)}개")
                return rows
            logger.warning(f"[pm-ai] plat_rows {platform}/{seller_name} → __ALL__ 필터링 결과 0건, "
                            f"직접 조회로 폴백 (캐시 저장 안 함)")

    # 4) Databricks 직접 조회 (캐시 없을 때만)
    safe_seller = seller_name.replace("'", "''")
    try:
        if all_sellers:
            # ※ LIMIT을 두지 않는다. 과거 LIMIT 5000이 있었는데, 셀러별 필터링에도
            #   이 결과가 재사용되다 보니 상품명 정렬 순서상 뒤쪽에 위치한 셀러
            #   (예: 배민상회 '현대그린푸드' 2,095건은 5,638~7,732번째라 통째로 잘림)
            #   전체가 누락되어 "미매핑 상품 없음"으로 오표시되는 버그가 있었다.
            #   (baemin 전체=12,024건, foodspring 전체=86,493건으로 5000 훨씬 초과)
            rows = _q(f"""
                SELECT p.product_key, p.product_name, p.spec,
                       p.price_sale, p.price_original, p.delivery_type,
                       p.is_free_delivery, p.platform_seller_name
                FROM {T_SILVER} p
                INNER JOIN (
                    SELECT platform_seller_name, MAX(crawl_date) AS max_date
                    FROM {T_SILVER}
                    WHERE platform = '{platform}'
                    GROUP BY platform_seller_name
                ) md ON p.platform_seller_name = md.platform_seller_name
                      AND p.crawl_date = md.max_date
                WHERE p.platform = '{platform}'
                ORDER BY p.product_name
            """) or []
        else:
            rows = _q(f"""
                SELECT p.product_key, p.product_name, p.spec,
                       p.price_sale, p.price_original, p.delivery_type,
                       p.is_free_delivery, p.platform_seller_name
                FROM {T_SILVER} p
                INNER JOIN (
                    SELECT MAX(crawl_date) AS max_date
                    FROM {T_SILVER}
                    WHERE platform = '{platform}' AND platform_seller_name = '{safe_seller}'
                ) md ON p.crawl_date = md.max_date
                WHERE p.platform = '{platform}' AND p.platform_seller_name = '{safe_seller}'
                ORDER BY p.product_name
            """) or []
        # ※ 빈 결과는 캐시하지 않는다 (일시적 조회 실패/데이터 지연으로 인한
        #   0건이 TTL 동안 고착되어 "미매핑 상품 없음" 오탐을 유발하지 않도록).
        if rows:
            _plat_rows_cache[cache_key] = (time.time(), rows)
            _disk_set(cache_key, time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[pm-ai] plat_rows 조회 실패 ({platform}/{seller_name}): {e}")
        raise

T_ZSDR  = "h_hmfo_fsi.gd_fsi_ent.sap_zsdr0017_order_linkage_status_d"
T_ZMM60 = "h_hmfo_fsi.gd_fsi_ent.sap_zmm60_material_master_d"
T_SILVER = "h_hmfo_fsi_dm.gd_rst_ing.dim_platform_products"


def _preload_products_background():
    """앱 시작 시 백그라운드에서 모든 DB 캐시를 미리 워밍업 + 25분 주기 keep-alive.
    Databricks 웨어하우스 auto-stop 방지 — 웨어하우스가 잠들지 않도록 주기적 ping.
    """
    def _load():
        import time as _t, concurrent.futures as _cf_pre
        _t.sleep(5)  # 앱 startup 완료 후 실행

        # ── 최초 워밍업 ──────────────────────────────────────────────────
        logger.info("[pm-preload] Databricks 웨어하우스 워밍업 시작")
        for plant in PLANTS:
            try:
                with _cf_pre.ThreadPoolExecutor(max_workers=5) as _pex:
                    _pex.submit(portal_db.pm_list_all_mappings, plant)
                    _pex.submit(_get_our_products, plant)
                    _pex.submit(_get_our_products_with_batch, plant)
                    _pex.submit(_get_base_prices, plant)
                    _pex.submit(_get_prev_month_sales_totals, plant)
                logger.info(f"[pm-preload] 완료: plant={plant}")
            except Exception as e:
                logger.warning(f"[pm-preload] 실패 ({plant}): {e}")

        # ── 25분 주기 keep-alive (Databricks auto-stop 방지) ─────────────
        # Databricks 웨어하우스 기본 auto-stop: 30분 미사용 시 종료
        # → 25분마다 SELECT 1 ping으로 웨어하우스를 깨어있게 유지
        _KEEPALIVE_INTERVAL = 25 * 60  # 25분
        logger.info("[pm-keepalive] 25분 주기 keep-alive 루프 시작")
        while True:
            _t.sleep(_KEEPALIVE_INTERVAL)
            try:
                _q("SELECT 1 AS ping")
                logger.info("[pm-keepalive] Databricks ping OK")
                # 캐시 TTL 만료 임박 시 백그라운드 갱신 (1시간 TTL → 55분마다 갱신)
                _first_plant = PLANTS[0] if PLANTS else None
                if _first_plant:
                    entry = _product_cache.get(f"products_{_first_plant}")
                    if not entry or (time.time() - entry[0]) > 3300:  # 55분
                        for plant in PLANTS:
                            try:
                                _get_our_products(plant)
                                _get_base_prices(plant)
                                _get_prev_month_sales_totals(plant)
                            except Exception:
                                pass
                        logger.info("[pm-keepalive] 캐시 갱신 완료")
            except Exception as e:
                logger.warning(f"[pm-keepalive] ping 실패: {e}")

    t = threading.Thread(target=_load, daemon=True, name="pm-preload")
    t.start()


_preload_products_background()


def _build_like_clause(keyword: str, col: str) -> str:
    """와일드카드(*) 검색: *토큰* → LIKE '%토큰%' AND LIKE '%토큰2%'"""
    if '*' in keyword:
        tokens = [t for t in keyword.split('*') if t.strip()]
        if not tokens:
            return "1=1"
        conditions = [f"{col} LIKE '%{t.replace(chr(39), chr(39)*2)}%'" for t in tokens]
        return ' AND '.join(conditions)
    else:
        safe = keyword.replace("'", "''")
        return f"{col} LIKE '%{safe}%'"


def _get_our_products(plant: str) -> list[dict]:
    """매핑 등록용: 상품코드당 1건 (배치 무시, GROUP BY 상품코드)"""
    cache_key = f"products_{plant}"
    # 1) 메모리
    if cache_key in _product_cache:
        ts, data = _product_cache[cache_key]
        if time.time() - ts < _PRODUCT_CACHE_TTL:
            return data
    # 2) 디스크 (2시간)
    disk = _disk_get(cache_key)
    if disk and time.time() - disk[0] < _PRODUCT_CACHE_TTL_DISK:
        _product_cache[cache_key] = disk
        return disk[1]
    try:
        if plant == 'ALL':
            plant_cond_op = f"z.`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
            batch_filter = "AND z.`배치` IN ('01','03')"
            group_by = "z.`상품코드`"  # ALL: 플랜트 무관, 상품코드 기준
        else:
            plant_cond_op = f"z.`플랜트` = '{plant}'"
            batch_filter = "AND z.`배치` = '01'" if plant == '4120' else "AND z.`배치` IN ('01','03')"
            group_by = "z.`상품코드`, z.`플랜트`"
        rows = _q(f"""
            SELECT
                z.`상품코드`                                    AS product_code,
                COALESCE(MAX(m.`상품명`), z.`상품코드`)        AS product_name,
                MAX(m.`자재유형명`)                            AS brand,
                MAX(m.`단위`)                                  AS unit,
                MAX(m.`자재그룹명`)                            AS product_group,
                MAX(m.`자재그룹`)                              AS material_group,
                MAX(m.`대분류`)                                AS category,
                MAX(m.`중분류`)                                AS mid_category,
                MAX(m.`소분류`)                                AS sub_category,
                MAX(m.`총중량`)                                AS total_weight,
                MAX(m.`순중량`)                                AS net_weight,
                MAX(m.`온도조건`)                               AS temp_cond,
                MAX(COALESCE(m.`세금분류명`, '과세'))            AS tax_class,
                MIN(z.`플랜트`)                                AS plant,
                MAX(COALESCE(z.`사용보류`, ''))                AS use_hold
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE {plant_cond_op}
              {batch_filter}
              AND COALESCE(m.`자재그룹`, '') != '5140'
            GROUP BY {group_by}
            ORDER BY COALESCE(MAX(m.`상품명`), z.`상품코드`)
            LIMIT 100000
        """)
        _product_cache[cache_key] = (time.time(), rows)
        _disk_set(cache_key, time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] our_products 조회 실패 ({plant}): {e}")
        return []


def _get_our_products_with_batch(plant: str) -> list[dict]:
    """운영상품 목록용: 배치별 행 반환 (4120→01, 4123→01+03)"""
    cache_key = f"products_batch_{plant}"
    if cache_key in _product_cache:
        ts, data = _product_cache[cache_key]
        if time.time() - ts < _PRODUCT_CACHE_TTL:
            return data
    try:
        if plant == 'ALL':
            plant_cond_wb = f"z.`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
            batch_filter = "AND z.`배치` IN ('01','03')"
            group_by_wb = "z.`상품코드`, z.`배치`, z.`플랜트`"  # 플랜트 포함: 4120/01 vs 4123/01 분리
        else:
            plant_cond_wb = f"z.`플랜트` = '{plant}'"
            batch_filter = "AND z.`배치` = '01'" if plant == '4120' else "AND z.`배치` IN ('01','03')"
            group_by_wb = "z.`상품코드`, z.`배치`, z.`플랜트`"
        rows = _q(f"""
            SELECT
                z.`상품코드`                                    AS product_code,
                z.`배치`                                        AS batch,
                COALESCE(MAX(m.`상품명`), z.`상품코드`)        AS product_name,
                MAX(m.`자재유형명`)                            AS brand,
                MAX(m.`단위`)                                  AS unit,
                MAX(m.`자재그룹명`)                            AS product_group,
                MAX(m.`자재그룹`)                              AS material_group,
                MAX(m.`대분류`)                                AS category,
                MIN(z.`플랜트`)                                AS plant,
                MAX(COALESCE(z.`사용보류`, ''))                AS use_hold
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE {plant_cond_wb}
              {batch_filter}
              AND COALESCE(m.`자재그룹`, '') != '5140'
            GROUP BY {group_by_wb}
            ORDER BY COALESCE(MAX(m.`상품명`), z.`상품코드`), z.`배치`
            LIMIT 100000
        """)
        _product_cache[cache_key] = (time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] our_products_with_batch 조회 실패 ({plant}): {e}")
        return []


def _get_platform_latest(product_keys: list[str] | None = None,
                          keyword: str = "") -> list[dict]:
    """silver.dim_platform_products 최신 가격 조회
    — 플랫폼별 MAX(crawl_date)를 경량 쿼리로 먼저 조회 후 각각 적용
      → 식봄/배민 수집일이 달라도 둘 다 노출, 풀스캔 없이 인덱스 활용
    """
    try:
        # Step 1: 플랫폼별 최신 수집일 경량 조회
        max_rows = _q(f"SELECT platform, MAX(crawl_date) AS max_date FROM {T_SILVER} GROUP BY platform") or []
        if not max_rows:
            return []
        date_clauses = " OR ".join(
            f"(p.platform='{r['platform']}' AND p.crawl_date='{r['max_date']}')"
            for r in max_rows if r.get("platform") and r.get("max_date")
        )
        if not date_clauses:
            return []

        # Step 2: 추가 조건 빌드
        if product_keys is not None:
            if not product_keys:
                return []
            keys_str = ", ".join(f"'{k}'" for k in product_keys)
            where_extra = f"AND p.product_key IN ({keys_str})"
        elif keyword:
            safe_kw = keyword.replace("'", "''")
            where_extra = f"AND p.product_name LIKE '%{safe_kw}%'"
        else:
            where_extra = ""

        rows = _q(f"""
            SELECT p.*
            FROM {T_SILVER} p
            WHERE ({date_clauses})
              {where_extra}
            ORDER BY p.platform, p.platform_seller_name, p.price_sale
            LIMIT 10000
        """)
        return _serialize_rows(rows)
    except Exception as e:
        logger.warning(f"[price_monitor] platform_latest 조회 실패: {e}")
        return []


# 외부 플랫폼 수수료율 (VAT 포함)
_FEE_DIRECT   = 0.066   # 직배송: PG 3% + 플랫폼 3% + VAT = 6.6%
_FEE_SINGSING = 0.171   # 싱싱배송: 직배송 6.6% + 추가 10.5% = 17.1%
_FEE_CJ       = 0.048   # CJ프레시웨이 예외: 식봄 최대주주로 우대 수수료 4.8%

# CJ프레시웨이 예외 셀러 (식봄 플랫폼 한정)
_CJ_SELLER_NAMES = {"CJ프레시웨이", "cj프레시웨이", "CJ 프레시웨이"}


def _get_fee(delivery_type: str = "직배송", platform: str = "", seller_name: str = "") -> float:
    """수수료율 반환. CJ프레시웨이(식봄)는 4.8% 우대 적용."""
    if platform == "foodspring" and seller_name in _CJ_SELLER_NAMES:
        return _FEE_CJ
    if delivery_type == "싱싱배송":
        return _FEE_SINGSING
    return _FEE_DIRECT


def _calc_gp(platform_price: float | None, buy_price: float | None,
             delivery_type: str = "직배송",
             platform: str = "", seller_name: str = "",
             tax_status: str = "과세") -> float | None:
    """
    수수료 차감 후 GP율 계산.
    - 직배송: 외부판매가 × (1 - 0.066) / VAT제수 = 수취액 A
    - 싱싱배송: 외부판매가 × (1 - 0.171) / VAT제수 = 수취액 A
    - CJ프레시웨이(식봄): 외부판매가 × (1 - 0.048) / VAT제수 = 수취액 A
    - VAT제수: 과세 상품은 1.1(부가세 10% 제외), 면세 상품은 1.0(제외 없음)
    - GP% = (A - 구매단가) / A × 100
    """
    if not platform_price or not buy_price:
        return None
    try:
        fee = _get_fee(delivery_type, platform, seller_name)
        vat_mult = 1.1 if tax_status == "과세" else 1.0
        a = platform_price * (1.0 - fee) / vat_mult   # 수수료 차감 후 (과세 상품만) 부가세 10% 제외
        return round((a - buy_price) / a * 100, 1)
    except Exception:
        return None


def _gp_status(gp: float | None) -> str:
    if gp is None:
        return "unknown"
    if gp < GP_ALERT_PCT:
        return "alert"
    if gp < GP_WARN_PCT:
        return "warn"
    return "ok"


# ── 화면 1: 가격 비교 대시보드 ─────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def pm_dashboard(
    request: Request,
    plant: str = "ALL",
    platform: str = "",
    alert_only: str = "",
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"

    # 기준가/구매가
    price_rows = _get_base_prices(plant)
    price_map = {
        r["product_code"]: r for r in price_rows
    }

    # 우리 상품명 맵 (T_ZMM60 기준) — miss 시 T_ZMM60 직접 fallback
    _our_products_cache = _get_our_products(plant)
    our_name_map = {
        p["product_code"]: p["product_name"]
        for p in _our_products_cache
        if p.get("product_name")
    }
    # 세금분류 맵 (면세 상품은 부가세 10% 차감 없이 GP 계산)
    our_tax_map = {
        p["product_code"]: (p.get("tax_class") or "과세")
        for p in _our_products_cache
    }

    def _resolve_name(p_code: str) -> str:
        """our_name_map 미스 시 T_ZMM60 직접 조회 (해당 플랜트가 아닌 상품 대응)"""
        name = our_name_map.get(p_code)
        if name and name != p_code:
            return name
        try:
            rows_fb = _q(f"SELECT MAX(`상품명`) AS nm FROM {T_ZMM60} WHERE `상품코드` = '{p_code}'")
            nm = rows_fb[0]["nm"] if rows_fb else None
            if nm:
                our_name_map[p_code] = nm  # 캐시에 저장
                return nm
        except Exception:
            pass
        return p_code

    # 전월 매출 데이터 (product_code 단위 합산)
    prev_sales_map = _get_prev_month_sales_totals(plant)

    # 전체 매핑 목록
    all_mappings = portal_db.pm_list_all_mappings(plant)
    if not all_mappings:
        return _render(request, "pm_dashboard.html",
                       rows=[], plant=plant, plants=PLANTS,
                       platform=platform, alert_only=alert_only,
                       last_crawl_date="", total=0)

    # 플랫폼 최신 가격
    product_keys = [m["product_key"] for m in all_mappings]
    platform_rows = _get_platform_latest(product_keys=product_keys)
    platform_map = {r["product_key"]: r for r in platform_rows}

    # 매핑 + 가격 + GP 계산
    rows = []
    for m in all_mappings:
        pk = m["product_key"]
        pf_data = platform_map.get(pk, {})
        if not pf_data:
            continue
        if platform and pf_data.get("platform") != platform:
            continue
        p_code = m["our_product_code"]
        price_info = price_map.get(p_code, {})
        buy_price = price_info.get("avg_buy_price")
        sale_price = pf_data.get("price_sale")
        delivery_type = pf_data.get("delivery_type", "직배송")
        _platform    = pf_data.get("platform", "")
        _seller_name = pf_data.get("platform_seller_name", "")
        _tax_status  = our_tax_map.get(p_code, "과세")
        gp = _calc_gp(sale_price, buy_price, delivery_type, _platform, _seller_name, _tax_status)
        status = _gp_status(gp)
        if alert_only and status not in ("alert", "warn"):
            continue
        rows.append({
            "our_product_code":   p_code,
            "product_name":       _resolve_name(p_code),
            "platform":           _platform,
            "seller_name":        _seller_name,
            "ext_product_name":   pf_data.get("product_name", ""),
            "ext_spec":           pf_data.get("spec", ""),
            "avg_sale_price":     price_info.get("avg_sale_price"),
            "avg_buy_price":      buy_price,
            "ext_price":          sale_price,
            "gp_pct":             gp,
            "gp_status":          status,
            "delivery_type":      pf_data.get("delivery_type", ""),
            "is_free_delivery":   pf_data.get("is_free_delivery"),
            "crawl_date":         str(pf_data.get("crawl_date", "")),
            "product_key":        pk,
            "prev_sales_amt":     prev_sales_map.get(p_code, {}).get("prev_sales_amt"),
            "prev_sales_qty":     prev_sales_map.get(p_code, {}).get("prev_sales_qty"),
        })

    # GP 오름차순 (경보 최상단)
    rows.sort(key=lambda r: (r["gp_pct"] if r["gp_pct"] is not None else 999))

    last_crawl = rows[0]["crawl_date"] if rows else ""
    return _render(request, "pm_dashboard.html",
                   rows=rows, plant=plant, plants=PLANTS,
                   platform=platform, alert_only=alert_only,
                   last_crawl_date=last_crawl,
                   gp_alert_pct=GP_ALERT_PCT, gp_warn_pct=GP_WARN_PCT,
                   total=len(rows))


# ── 화면 2: 운영상품 목록 & 매핑 현황 ─────────────────────────────────────

@router.get("/products", response_class=HTMLResponse)
async def pm_products(
    request: Request,
    plant: str = "ALL",
    keyword: str = "",
    map_filter: str = "",       # "mapped" | "unmapped" | ""
    category: str = "",         # 대분류 필터
    status_filter: str = "",    # "active" | "stopped" | ""
    sort: str = "sales_desc",   # "sales_desc" | "qty_desc" | ""
    page: int = 1,
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"
    PAGE_SIZE = 20

    products    = _get_our_products_with_batch(plant)
    prev_sales  = _get_prev_month_sales(plant)   # dict[product_code → {prev_sales_amt, prev_sales_qty}]
    all_mappings = portal_db.pm_list_all_mappings(plant)

    # 상품코드별 매핑 수 집계
    mapping_count = {}
    for m in all_mappings:
        c = m["our_product_code"]
        if c not in mapping_count:
            mapping_count[c] = {"baemin": 0, "foodspring": 0, "total": 0}
        mapping_count[c][m["platform"]] = mapping_count[c].get(m["platform"], 0) + 1
        mapping_count[c]["total"] += 1

    # 대분류 목록
    categories = sorted(set(p.get("category") or "" for p in products if p.get("category")))

    all_rows = []
    for p in products:
        code = p["product_code"]
        is_stopped = p.get("use_hold") == "X"

        # 키워드 필터
        if keyword:
            name = (p.get("product_name") or "").lower()
            if '*' in keyword:
                tokens = [t.lower() for t in keyword.split('*') if t.strip()]
                if not all(t in name or t in code.lower() for t in tokens):
                    continue
            else:
                kw = keyword.lower()
                if kw not in name and kw not in code.lower():
                    continue
        # 대분류 필터
        if category and (p.get("category") or "") != category:
            continue
        # 운영여부 필터
        if status_filter == "active" and is_stopped:
            continue
        if status_filter == "stopped" and not is_stopped:
            continue
        # 매핑 필터
        mc = mapping_count.get(code, {"baemin": 0, "foodspring": 0, "total": 0})
        if map_filter == "mapped" and mc["total"] == 0:
            continue
        if map_filter == "unmapped" and mc["total"] > 0:
            continue

        # (product_code, plant) 키로 플랜트별 정확한 매출 조회
        _plant_key = p.get("plant", "")
        ps = prev_sales.get((code, _plant_key), {})
        all_rows.append({
            **p,
            "mapping_baemin":     mc["baemin"],
            "mapping_foodspring": mc["foodspring"],
            "mapping_total":      mc["total"],
            "is_stopped":         is_stopped,
            "prev_sales_amt":     ps.get("prev_sales_amt"),
            "prev_sales_qty":     ps.get("prev_sales_qty"),
        })

    # 정렬
    if sort == "qty_desc":
        all_rows.sort(key=lambda r: (r["prev_sales_qty"] or 0), reverse=True)
    else:  # sales_desc (기본)
        all_rows.sort(key=lambda r: (r["prev_sales_amt"] or 0), reverse=True)

    total = len(all_rows)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    rows = all_rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

    return _render(request, "pm_products.html",
                   rows=rows, plant=plant, plants=PLANTS,
                   keyword=keyword, map_filter=map_filter,
                   category=category, categories=categories,
                   status_filter=status_filter, sort=sort,
                   total=total, page=page, total_pages=total_pages,
                   page_size=PAGE_SIZE)


# ── 화면 3: 매핑 등록 ───────────────────────────────────────────────────────

@router.get("/mapping", response_class=HTMLResponse)
async def pm_mapping_page(request: Request, plant: str = "ALL"):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"
    return _render(request, "pm_mapping.html", plant=plant, plants=PLANTS)


# ── API: 진단 공개용 (인증 없음, 임시) ────────────────────────────────────

@router.get("/api/debug-pub")
async def api_debug_pub(request: Request):
    """인증 없이 테이블 컬럼 확인용 임시 엔드포인트"""
    result = {}
    try:
        rows = _q(f"SELECT * FROM {T_ZSDR} WHERE `플랜트`='4120' LIMIT 1")
        result["zsdr_columns"] = list(rows[0].keys()) if rows else []
    except Exception as e:
        result["zsdr_error"] = str(e)
    try:
        rows = _q(f"SELECT * FROM {T_ZMM60} LIMIT 1")
        result["zmm60_columns"] = list(rows[0].keys()) if rows else []
    except Exception as e:
        result["zmm60_error"] = str(e)
    # 175301 직접 조회 (5140 필터 우회)
    try:
        rows = _q(f"""
            SELECT z.`상품코드`, m.`상품명`, m.`자재그룹`, m.`자재그룹명`
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`상품코드` = '175301'
            LIMIT 5
        """)
        result["check_175301"] = rows
    except Exception as e:
        result["check_175301_error"] = str(e)
    # 414624 직접 조회 - 배치/플랜트/자재그룹 확인
    try:
        rows = _q(f"""
            SELECT
                z.`상품코드`,
                z.`플랜트`,
                z.`배치`,
                z.`사용보류`,
                m.`상품명`,
                m.`자재그룹`,
                m.`자재그룹명`
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`상품코드` = '414624'
        """)
        result["check_414624"] = rows
    except Exception as e:
        result["check_414624_error"] = str(e)
    # 414624가 필터에 걸리는지 시뮬레이션
    try:
        rows = _q(f"""
            SELECT
                z.`상품코드`,
                z.`플랜트`,
                z.`배치`,
                COALESCE(m.`자재그룹`, '') AS 자재그룹,
                CASE
                    WHEN z.`플랜트` NOT IN ('4120','4123','4121') THEN '플랜트 제외'
                    WHEN z.`배치` NOT IN ('01','03')              THEN '배치 제외'
                    WHEN COALESCE(m.`자재그룹`,'') = '5140'       THEN '자재그룹5140 제외'
                    ELSE '통과 가능'
                END AS 필터결과
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`상품코드` = '414624'
        """)
        result["check_414624_filter"] = rows
    except Exception as e:
        result["check_414624_filter_error"] = str(e)
    # 4120 플랜트 전체 건수
    try:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {T_ZSDR} WHERE `플랜트`='4120'")
        result["zsdr_4120_total"] = rows[0]["cnt"] if rows else 0
    except Exception as e:
        result["zsdr_4120_count_error"] = str(e)
    # 온도조건 코드 분포 (냉동/냉장 필터 활성화용)
    try:
        rows = _q(f"""
            SELECT `온도조건` AS temp_cond, COUNT(*) AS cnt
            FROM {T_ZMM60}
            WHERE `온도조건` IS NOT NULL
            GROUP BY `온도조건`
            ORDER BY cnt DESC
            LIMIT 20
        """)
        result["zmm60_temp_cond_dist"] = rows
    except Exception as e:
        result["zmm60_temp_cond_error"] = str(e)
    # 온도조건 코드 의미 확인: 냉동 키워드 상품명 vs 코드 매핑
    try:
        rows = _q(f"""
            SELECT `온도조건` AS temp_cond, `상품명` AS product_name, `상품코드` AS code
            FROM {T_ZMM60}
            WHERE (`상품명` LIKE '%냉동%' OR `상품명` LIKE '%냉장%' OR `상품명` LIKE '%실온%')
              AND `온도조건` IS NOT NULL
            LIMIT 15
        """)
        result["zmm60_temp_cond_samples"] = rows
    except Exception as e:
        result["zmm60_temp_cond_samples_error"] = str(e)
    # [임시] 상품코드 직접 조회 (쿼리파라미터 product_code 지원)
    try:
        pc = request.query_params.get("product_code", "")
        kw = request.query_params.get("keyword", "")
        if pc:
            rows = _q(f"""
                SELECT z.`상품코드` AS code, m.`상품명` AS name,
                       m.`자재그룹명` AS group, m.`온도조건` AS temp,
                       m.`총중량` AS weight, m.`단위` AS unit
                FROM {T_ZSDR} z
                LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
                WHERE z.`상품코드` = '{pc}'
                LIMIT 5
            """)
            result["product_code_search"] = rows
        elif kw:
            rows = _q(f"""
                SELECT z.`상품코드` AS code, m.`상품명` AS name,
                       m.`자재그룹명` AS grp, m.`온도조건` AS temp
                FROM {T_ZSDR} z
                LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
                WHERE m.`상품명` LIKE '%{kw}%'
                LIMIT 10
            """)
            result["keyword_search"] = rows
    except Exception as e:
        result["product_search_error"] = str(e)
    return JSONResponse(result)


# ── API: 진단 (데이터소스 연결 확인) ─────────────────────────────────────

@router.get("/api/debug")
async def api_debug(request: Request):
    _require_pm_access(request)
    result = {}

    # 1. ZSDR 테이블
    try:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {T_ZSDR} WHERE `플랜트`='4120' LIMIT 1")
        result["zsdr_count"] = rows[0]["cnt"] if rows else 0
        result["zsdr_ok"] = True
    except Exception as e:
        result["zsdr_ok"] = False
        result["zsdr_error"] = str(e)

    # 2. ZMM60 테이블
    try:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {T_ZMM60} LIMIT 1")
        result["zmm60_count"] = rows[0]["cnt"] if rows else 0
        result["zmm60_ok"] = True
    except Exception as e:
        result["zmm60_ok"] = False
        result["zmm60_error"] = str(e)

    # 3. silver 플랫폼 상품 테이블
    try:
        rows = _q(f"SELECT MAX(crawl_date) AS max_date, COUNT(*) AS cnt FROM {T_SILVER}")
        result["silver_count"] = rows[0]["cnt"] if rows else 0
        result["silver_max_date"] = str(rows[0]["max_date"]) if rows else None
        result["silver_ok"] = True
    except Exception as e:
        result["silver_ok"] = False
        result["silver_error"] = str(e)

    return JSONResponse(result)


# ── API: 우리 상품 검색 (AJAX) ────────────────────────────────────────────

@router.get("/api/our-products")
async def api_our_products(request: Request, plant: str = "ALL", keyword: str = ""):
    _require_pm_access(request)
    try:
        error_msg = None
        try:
            products = _get_our_products(plant)  # 캐시 사용
        except Exception as e:
            error_msg = str(e)
            products = []
        # 전체센터 매출 합산 (ALL 고정, product_code 단위)
        try:
            sales_map = _get_prev_month_sales_totals('ALL')
        except Exception:
            sales_map = {}
        # 기준가(공급가/구매가) 포함
        try:
            base_prices = {r["product_code"]: r for r in (_get_base_prices(plant) or [])}
        except Exception:
            base_prices = {}
        # _get_our_products는 _serialize_rows를 거치지 않으므로 Decimal 제거
        products = _serialize_rows(products)
        # 매출 데이터 + 기준가 합치 (이미 float 보장)
        enriched = []
        for p in products:
            code = p.get("product_code", "")
            s  = sales_map.get(code, {})
            bp = base_prices.get(code, {})
            def _f(v):
                """Decimal/float/None 모두 float or None로 반환"""
                try: return float(v) if v is not None else None
                except Exception: return None
            enriched.append({**p,
                "prev_sales_amt": _f(s.get("prev_sales_amt")),
                "prev_sales_qty": _f(s.get("prev_sales_qty")),
                "avg_sale_price": _f(bp.get("avg_sale_price")),
                "avg_buy_price":  _f(bp.get("avg_buy_price")),
            })
        products = enriched
        if keyword and products:
            if '*' in keyword:
                # 와일드카드: *흔다리*500* → 모든 토큰이 포함되는 상품
                tokens = [t.lower() for t in keyword.split('*') if t.strip()]
                products = [
                    p for p in products
                    if all(t in (p.get("product_name") or "").lower() or t in (p.get("product_code") or "").lower()
                           for t in tokens)
                ]
            else:
                kw = keyword.lower()
                products = [
                    p for p in products
                    if kw in (p.get("product_name") or "").lower() or kw in (p.get("product_code") or "")
                ]
        # 매출 높은순 정렬
        products = sorted(products, key=lambda p: p.get("prev_sales_amt") or 0, reverse=True)
        # [N] 화성3배치 중복 제거: non-[N] 동일 기본명 상품이 존재하면 [N] 숨김
        import re as _re_nd
        _nd_non_n_set: set[str] = set()
        for _p in products:
            _nm = (_p.get("product_name") or "").strip()
            if not _nm.startswith("[N]"):
                _nd_non_n_set.add(_re_nd.sub(r'\s+', '', _nm.lower()))
        products = [
            p for p in products
            if not (p.get("product_name") or "").strip().startswith("[N]")
            or _re_nd.sub(r'\s+', '', (p.get("product_name") or "").strip()[3:].lower()) not in _nd_non_n_set
        ]
        return JSONResponse({"data": products[:100], "error": error_msg, "total_before_filter": len(products)})
    except Exception as e:
        import traceback as _tb
        return JSONResponse({"data": [], "error": str(e), "traceback": _tb.format_exc()[-2000:]})


# ── API: 플랫폼 상품 검색 (AJAX) ─────────────────────────────────────────

@router.get("/api/platform-products")
async def api_platform_products(
    request: Request,
    keyword: str = "",
    platform: str = "",
):
    _require_pm_access(request)
    if not keyword:
        return JSONResponse({"data": [], "error": None, "hint": "keyword 파라미터 필요"})
    error_msg = None
    rows = []
    try:
        like_clause = _build_like_clause(keyword, 'p.product_name')
        plat_filter = f"AND p.platform = '{platform}'" if platform else ""
        rows = _q(f"""
            SELECT p.*
            FROM {T_SILVER} p
            INNER JOIN (
                SELECT platform, MAX(crawl_date) AS max_date
                FROM {T_SILVER}
                {("WHERE platform = '" + platform + "'") if platform else ""}
                GROUP BY platform
            ) latest ON p.platform = latest.platform AND p.crawl_date = latest.max_date
            WHERE ({like_clause}) {plat_filter}
            ORDER BY p.platform, p.platform_seller_name, p.price_sale
            LIMIT 500
        """) or []
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"[price_monitor] platform_products 조회 실패: {e}")
    if platform and rows:
        rows = [r for r in rows if r.get("platform") == platform]
    rows = _serialize_rows(rows)
    return JSONResponse({"data": rows[:200], "error": error_msg, "total": len(rows)})


# ── API: 매핑 추가 (즉시 반영, 승인 불필요) ──────────────────────────────

@router.post("/api/mapping/add")
async def api_mapping_add(request: Request):
    _require_pm_access(request)
    session = _get_session(request)
    body = await request.json()
    our_product_code    = body.get("our_product_code", "").strip()
    plant               = body.get("plant", "4120").strip()
    product_key         = body.get("product_key", "").strip()
    platform            = body.get("platform", "").strip()
    platform_seller_id  = body.get("platform_seller_id", "")
    platform_product_id = body.get("platform_product_id", "")
    product_name        = body.get("product_name", "")
    seller_name         = body.get("seller_name", "")

    if not all([our_product_code, plant, product_key, platform]):
        return JSONResponse({"ok": False, "error": "필수값 누락"}, status_code=400)
    if plant not in PLANTS and plant != "ALL":
        return JSONResponse({"ok": False, "error": "허용되지 않은 플랜트"}, status_code=400)

    mapping_id = portal_db.pm_add_mapping(
        our_product_code=our_product_code,
        plant=plant,
        product_key=product_key,
        platform=platform,
        platform_seller_id=str(platform_seller_id),
        platform_product_id=str(platform_product_id),
        product_name=product_name,
        seller_name=seller_name,
        created_by=session.get("emp_code", ""),
        tag=body.get("tag", "normal"),
        multiplier=float(body.get("multiplier", 1.0)),
    )
    return JSONResponse({"ok": True, "mapping_id": mapping_id})


# ── API: 매핑 목록 조회 (AJAX) ────────────────────────────────────────────

@router.get("/api/mapping/{our_product_code}")
async def api_mapping_list(request: Request, our_product_code: str, plant: str = "ALL"):
    _require_pm_access(request)
    rows = portal_db.pm_list_mappings(our_product_code, plant)
    return JSONResponse(rows)


# ── API: 매핑 비활성화 (soft delete) ─────────────────────────────────────

@router.post("/api/mapping/remove")
async def api_mapping_remove(request: Request):
    _require_pm_access(request)
    body = await request.json()
    mapping_id = int(body.get("mapping_id", 0))
    if not mapping_id:
        return JSONResponse({"ok": False, "error": "mapping_id 필요"}, status_code=400)
    portal_db.pm_deactivate_mapping(mapping_id)
    return JSONResponse({"ok": True})


# ── 화면 4: 가격 이력 ──────────────────────────────────────────────────────

@router.get("/history/{product_code}", response_class=HTMLResponse)
async def pm_history(
    request: Request,
    product_code: str,
    plant: str = "ALL",
    days: int = 30,
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"

    mappings = portal_db.pm_list_mappings(product_code, plant)
    product_keys = [m["product_key"] for m in mappings]

    history_rows = []
    if product_keys:
        try:
            keys_str = ", ".join(f"'{k}'" for k in product_keys)
            history_rows = _q(f"""
                SELECT
                    product_key, platform, platform_seller_name,
                    product_name, spec, price_sale, price_original,
                    delivery_type, is_free_delivery, crawl_date
                FROM {T_SILVER}
                WHERE product_key IN ({keys_str})
                  AND crawl_date >= DATE_SUB(CURRENT_DATE(), {days})
                ORDER BY crawl_date DESC, platform, platform_seller_name
            """)
        except Exception as e:
            logger.exception(f"[price_monitor] history 조회 실패 (product_code={product_code}, plant={plant}, keys={product_keys}): {e}")

    # 가격 기준값
    price_rows = _get_base_prices(plant)
    price_map = {r["product_code"]: r for r in price_rows}
    price_info = price_map.get(product_code, {})

    # 제품명 / 세금분류 (면세 상품은 부가세 10% 차감 없이 GP 계산)
    our_products = _get_our_products(plant)
    product_info = next((p for p in our_products if p["product_code"] == product_code), {})
    _tax_status = product_info.get("tax_class") or "과세"

    # GP 계산 추가
    for row in history_rows:
        gp = _calc_gp(
            row.get("price_sale"),
            price_info.get("avg_buy_price"),
            row.get("delivery_type", "직배송"),
            row.get("platform", ""),
            row.get("platform_seller_name", ""),
            _tax_status,
        )
        row["gp_pct"] = gp
        row["gp_status"] = _gp_status(gp)
        row["crawl_date"] = str(row.get("crawl_date", ""))

    return _render(request, "pm_history.html",
                   product_code=product_code,
                   product_info=product_info,
                   price_info=price_info,
                   history_rows=history_rows,
                   plant=plant, plants=PLANTS, days=days)


# ── 화면 4-2: 상품 상세 (와이어프레임) ────────────────────────────────────

@router.get("/detail/{product_code}", response_class=HTMLResponse)
async def pm_detail(
    request: Request,
    product_code: str,
    plant: str = "ALL",
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"

    # 기준가
    price_rows = _get_base_prices(plant)
    price_map  = {r["product_code"]: r for r in price_rows}
    price_info = price_map.get(product_code, {})
    buy_price  = price_info.get("avg_buy_price")

    # 세금분류 (면세 상품은 부가세 10% 차감 없이 GP 계산)
    our_products = _get_our_products(plant)
    product_info_meta = next((p for p in our_products if p["product_code"] == product_code), {})
    tax_status = product_info_meta.get("tax_class", "과세") or "과세"

    # 매핑된 product_keys
    mappings     = portal_db.pm_list_mappings(product_code, plant)
    product_keys = [m["product_key"] for m in mappings]

    # 오늘(최신) 플랫폼 가격 – 셀러별
    today_rows = []
    history_rows = []
    if product_keys:
        keys_str = ", ".join(f"'{k}'" for k in product_keys)
        try:
            today_rows = _q(f"""
                SELECT p.product_key, p.platform, p.platform_seller_name,
                       p.product_name, p.spec,
                       p.price_sale, p.price_original, p.discount_rate,
                       p.delivery_type, p.is_free_delivery, p.crawl_date
                FROM {T_SILVER} p
                INNER JOIN (
                    SELECT product_key, MAX(crawl_date) AS max_date
                    FROM {T_SILVER}
                    WHERE product_key IN ({keys_str})
                    GROUP BY product_key
                ) md ON p.product_key = md.product_key
                       AND p.crawl_date = md.max_date
                ORDER BY p.platform, p.platform_seller_name
            """)
            history_rows = _q(f"""
                SELECT crawl_date, platform, platform_seller_name,
                       MIN(price_sale) AS min_price,
                       AVG(price_sale) AS avg_price,
                       COUNT(*) AS cnt
                FROM {T_SILVER}
                WHERE product_key IN ({keys_str})
                  AND crawl_date >= DATE_SUB(CURRENT_DATE(), 30)
                  AND price_sale IS NOT NULL
                GROUP BY crawl_date, platform, platform_seller_name
                ORDER BY crawl_date, platform, platform_seller_name
            """)
        except Exception as e:
            logger.exception(f"[detail] 조회 실패: {e}")

    # GP 계산
    for row in today_rows:
        _pf  = row.get("platform", "")
        _sn  = row.get("platform_seller_name", "")
        _dt  = row.get("delivery_type", "직배송")
        gp = _calc_gp(row.get("price_sale"), buy_price, _dt, _pf, _sn, tax_status)
        row["gp_pct"]    = gp
        row["gp_status"] = _gp_status(gp)
        row["crawl_date"] = str(row.get("crawl_date", ""))
        fee = _get_fee(_dt, _pf, _sn)
        row["fee_pct"] = round(fee * 100, 1)
        # 실판매가 (수수료 제외 쫐정)
        ps = row.get("price_sale")
        row["net_price"] = round(ps * (1 - fee), 0) if ps else None

    # 중복 제거: (platform, seller_name, spec, price_sale) 동일 행 하나만 표시
    _seen = set()
    _deduped = []
    for row in today_rows:
        _key = (row.get("platform"), row.get("platform_seller_name"),
                row.get("spec"), row.get("price_sale"))
        if _key not in _seen:
            _seen.add(_key)
            _deduped.append(row)
    today_rows = _deduped

    # 시장 통계
    our_sale = price_info.get("avg_sale_price")
    prices = [r["price_sale"] for r in today_rows if r.get("price_sale")]
    net_prices = [r["net_price"] for r in today_rows if r.get("net_price")]
    market_min  = min(prices) if prices else None
    market_avg  = round(sum(prices)/len(prices)) if prices else None
    market_min_net = round(min(net_prices)) if net_prices else None
    market_avg_net = round(sum(net_prices)/len(net_prices)) if net_prices else None
    market_min_gp = _calc_gp(market_min, buy_price, "직배송", tax_status=tax_status) if market_min else None

    # VAT 포함 판매가 (경쟁등급 산출용, 세금분류는 위에서 이미 조회함)
    vat_mult = 1.1 if tax_status == "과세" else 1.0
    our_sale_vat = round(our_sale * vat_mult) if our_sale else None  # VAT 포함 판매가

    # 경쟁등급: VAT 포함 판매가 vs 시장최저가 (외부 가격은 VAT 포함)
    if our_sale_vat and market_min:
        ratio = our_sale_vat / market_min
        if ratio <= 1.0:   grade = "A"
        elif ratio <= 1.05: grade = "B"
        elif ratio <= 1.10: grade = "C"
        else:               grade = "D"
    else:
        grade = "-"

    # Chart.js 데이터 (날짜 × 셀러 라인)
    import json as _json
    from collections import defaultdict as _defaultdict
    chart_dates = sorted({str(r["crawl_date"]) for r in history_rows})
    seller_keys = sorted({f"{r['platform']}|{r['platform_seller_name']}" for r in history_rows})
    chart_datasets = []
    COLORS = ["#3b82f6","#10b981","#f59e0b","#ef4444","#8b5cf6","#06b6d4","#f97316","#84cc16"]
    for i, sk in enumerate(seller_keys):
        pf, sn = sk.split("|", 1)
        date_price = {str(r["crawl_date"]): r["min_price"] for r in history_rows
                      if f"{r['platform']}|{r['platform_seller_name']}" == sk}
        data = [date_price.get(d) for d in chart_dates]
        label = f"{'배민' if pf=='baemin' else '식봄'} {sn}"
        chart_datasets.append({"label": label, "data": data,
                                "borderColor": COLORS[i % len(COLORS)],
                                "backgroundColor": "transparent",
                                "tension": 0.3, "spanGaps": True})

    # 플랫폼 집계 차트 (배민 vs 식봄: 최저/평균/최고)
    _pf_colors = {
        "baemin":     {"최저": "#1d4ed8", "평균": "#3b82f6", "최고": "#93c5fd"},
        "foodspring": {"최저": "#065f46", "평균": "#10b981", "최고": "#6ee7b7"},
    }
    chart_datasets_platform = []
    for pf_key in ["baemin", "foodspring"]:
        pf_label = "배민상회" if pf_key == "baemin" else "식봄"
        pf_rows  = [r for r in history_rows if r["platform"] == pf_key]
        if not pf_rows:
            continue
        _by_date = _defaultdict(list)
        for r in pf_rows:
            if r["min_price"] is not None:
                _by_date[str(r["crawl_date"])].append(float(r["min_price"]))
        for stat, fn, dash in [("최저", min, None), ("평균", lambda v: sum(v)/len(v), [5,3]), ("최고", max, [2,2])]:
            data = [round(fn(_by_date[d]), 0) if _by_date.get(d) else None for d in chart_dates]
            ds = {
                "label": f"{pf_label} {stat}",
                "data": data,
                "borderColor": _pf_colors[pf_key][stat],
                "backgroundColor": "transparent",
                "tension": 0.3,
                "spanGaps": True,
            }
            if dash:
                ds["borderDash"] = dash
            chart_datasets_platform.append(ds)

    # 당사 판매단가 수평선 (공통)
    if our_sale:
        _our_line = {
            "label": "당사 판매단가",
            "data": [our_sale] * len(chart_dates),
            "borderColor": "#0f172a", "borderDash": [6, 3],
            "backgroundColor": "transparent", "pointRadius": 0, "tension": 0,
        }
        chart_datasets.append(_our_line)
        chart_datasets_platform.append(_our_line)

    return _render(request, "pm_detail.html",
                   product_code=product_code,
                   product_info=product_info_meta,
                   price_info=price_info,
                   tax_status=tax_status,
                   our_sale_vat=our_sale_vat,
                   today_rows=today_rows,
                   market_min=market_min,
                   market_avg=market_avg,
                   market_min_net=market_min_net,
                   market_avg_net=market_avg_net,
                   market_min_gp=market_min_gp,
                   grade=grade,
                   chart_dates=_json.dumps(chart_dates),
                   chart_datasets=_json.dumps(chart_datasets),
                   chart_datasets_platform=_json.dumps(chart_datasets_platform),
                   gp_alert_pct=GP_ALERT_PCT,
                   gp_warn_pct=GP_WARN_PCT,
                   plant=plant, plants=PLANTS)


# ── 화면: 상품 가격/GP 요약 조회 (전체 계정 공용) ──────────────────────────
# "범용상품 제안 솔루션" 하위 메뉴 — 담당자가 직접 상품코드/상품명으로
# 검색해 매출순 목록을 보고, 상품 하나를 골라 가격모니터링 상세 페이지의
# 핵심 정보(당사 판매가/구매가/GP, 플랫폼 최저/평균/최고가, 추이, 셀러별 현황)를
# 요약된 형태로 확인할 수 있는 전체 계정 공용 화면.

@router.get("/gp-lookup", response_class=HTMLResponse)
async def pm_gp_lookup_page(request: Request, plant: str = "ALL"):
    _require_login(request)
    if plant not in PLANTS:
        plant = "ALL"
    return _render(request, "pm_gp_lookup.html", plant=plant, plants=PLANTS, PLANT_LABELS=PLANT_LABELS)


@router.get("/api/gp-lookup/search")
async def api_gp_lookup_search(request: Request, keyword: str = "", plant: str = "ALL"):
    """상품코드/상품명으로 검색 → 전월 매출액 기준 내림차순 목록 (최대 50건)."""
    _require_login(request)
    if plant not in PLANTS:
        plant = "ALL"
    keyword = (keyword or "").strip()
    our_products = _get_our_products(plant)
    prev_sales = _get_prev_month_sales_totals(plant)

    if keyword:
        kw_low = keyword.lower()
        matched = [
            p for p in our_products
            if kw_low in (p.get("product_code") or "").lower()
            or kw_low in (p.get("product_name") or "").lower()
        ]
    else:
        matched = list(our_products)

    results = []
    for p in matched:
        code = p.get("product_code")
        sales = prev_sales.get(code, {})
        results.append({
            "product_code":    code,
            "product_name":    p.get("product_name"),
            "brand":           p.get("brand"),
            "category":        p.get("category"),
            "unit":            p.get("unit"),
            "prev_sales_amt":  sales.get("prev_sales_amt") or 0,
            "prev_sales_qty":  sales.get("prev_sales_qty") or 0,
        })
    results.sort(key=lambda r: r["prev_sales_amt"], reverse=True)
    return JSONResponse({"items": results[:50], "total_matched": len(results)})


@router.get("/api/gp-lookup/detail/{product_code}")
async def api_gp_lookup_detail(request: Request, product_code: str, plant: str = "ALL"):
    """상품 1건에 대한 가격/GP 요약 데이터 (JSON).
    pm_detail 화면 계산 로직을 재사용해 요약형으로 반환."""
    _require_login(request)
    if plant not in PLANTS:
        plant = "ALL"

    price_rows = _get_base_prices(plant)
    price_map  = {r["product_code"]: r for r in price_rows}
    price_info = price_map.get(product_code, {})
    buy_price  = price_info.get("avg_buy_price")
    our_sale   = price_info.get("avg_sale_price")

    our_products = _get_our_products(plant)
    product_info_meta = next((p for p in our_products if p["product_code"] == product_code), {})
    tax_status = product_info_meta.get("tax_class", "과세") or "과세"

    mappings     = portal_db.pm_list_mappings(product_code, plant)
    product_keys = [m["product_key"] for m in mappings]

    today_rows = []
    history_rows = []
    if product_keys:
        keys_str = ", ".join(f"'{k}'" for k in product_keys)
        try:
            today_rows = _q(f"""
                SELECT p.product_key, p.platform, p.platform_seller_name,
                       p.product_name, p.spec,
                       p.price_sale, p.price_original, p.discount_rate,
                       p.delivery_type, p.is_free_delivery, p.crawl_date
                FROM {T_SILVER} p
                INNER JOIN (
                    SELECT product_key, MAX(crawl_date) AS max_date
                    FROM {T_SILVER}
                    WHERE product_key IN ({keys_str})
                    GROUP BY product_key
                ) md ON p.product_key = md.product_key
                       AND p.crawl_date = md.max_date
                ORDER BY p.platform, p.platform_seller_name
            """)
            history_rows = _q(f"""
                SELECT crawl_date, platform, platform_seller_name,
                       MIN(price_sale) AS min_price,
                       AVG(price_sale) AS avg_price,
                       COUNT(*) AS cnt
                FROM {T_SILVER}
                WHERE product_key IN ({keys_str})
                  AND crawl_date >= DATE_SUB(CURRENT_DATE(), 30)
                  AND price_sale IS NOT NULL
                GROUP BY crawl_date, platform, platform_seller_name
                ORDER BY crawl_date, platform, platform_seller_name
            """)
        except Exception as e:
            logger.exception(f"[gp-lookup] 조회 실패: {e}")

    for row in today_rows:
        _pf, _sn, _dt = row.get("platform", ""), row.get("platform_seller_name", ""), row.get("delivery_type", "직배송")
        gp = _calc_gp(row.get("price_sale"), buy_price, _dt, _pf, _sn, tax_status)
        row["gp_pct"]    = gp
        row["gp_status"] = _gp_status(gp)
        row["crawl_date"] = str(row.get("crawl_date", ""))
        fee = _get_fee(_dt, _pf, _sn)
        row["fee_pct"] = round(fee * 100, 1)
        ps = row.get("price_sale")
        row["net_price"] = round(ps * (1 - fee), 0) if ps else None

    _seen, _deduped = set(), []
    for row in today_rows:
        key = (row.get("platform"), row.get("platform_seller_name"), row.get("spec"), row.get("price_sale"))
        if key not in _seen:
            _seen.add(key)
            _deduped.append(row)
    today_rows = _deduped

    prices = [r["price_sale"] for r in today_rows if r.get("price_sale")]
    market_min = min(prices) if prices else None
    market_max = max(prices) if prices else None
    market_avg = round(sum(prices) / len(prices)) if prices else None
    market_min_gp = _calc_gp(market_min, buy_price, "직배송", tax_status=tax_status) if market_min else None
    market_max_gp = _calc_gp(market_max, buy_price, "직배송", tax_status=tax_status) if market_max else None
    market_avg_gp = _calc_gp(market_avg, buy_price, "직배송", tax_status=tax_status) if market_avg else None

    our_gp_pct = None
    if our_sale and buy_price and our_sale > 0:
        try:
            our_gp_pct = round((our_sale - buy_price) / our_sale * 100, 1)
        except Exception:
            our_gp_pct = None

    # 최저/최고가 셀러 식별
    min_seller = next((f"{'배민' if r['platform']=='baemin' else '식봄'} {r['platform_seller_name']}"
                        for r in today_rows if r.get("price_sale") == market_min), None) if market_min else None
    max_seller = next((f"{'배민' if r['platform']=='baemin' else '식봄'} {r['platform_seller_name']}"
                        for r in today_rows if r.get("price_sale") == market_max), None) if market_max else None

    import json as _json
    chart_dates = sorted({str(r["crawl_date"]) for r in history_rows})
    _pf_colors = {
        "baemin":     {"최저": "#1d4ed8", "평균": "#3b82f6", "최고": "#93c5fd"},
        "foodspring": {"최저": "#065f46", "평균": "#10b981", "최고": "#6ee7b7"},
    }
    from collections import defaultdict as _defaultdict
    chart_datasets = []
    for pf_key in ["baemin", "foodspring"]:
        pf_label = "배민상회" if pf_key == "baemin" else "식봄"
        pf_rows  = [r for r in history_rows if r["platform"] == pf_key]
        if not pf_rows:
            continue
        _by_date = _defaultdict(list)
        for r in pf_rows:
            if r["min_price"] is not None:
                _by_date[str(r["crawl_date"])].append(float(r["min_price"]))
        for stat, fn, dash in [("최저", min, None), ("평균", lambda v: sum(v) / len(v), [5, 3]), ("최고", max, [2, 2])]:
            data = [round(fn(_by_date[d]), 0) if _by_date.get(d) else None for d in chart_dates]
            ds = {"label": f"{pf_label} {stat}", "data": data,
                  "borderColor": _pf_colors[pf_key][stat], "backgroundColor": "transparent",
                  "tension": 0.3, "spanGaps": True}
            if dash:
                ds["borderDash"] = dash
            chart_datasets.append(ds)
    if our_sale:
        chart_datasets.append({"label": "당사 판매단가", "data": [our_sale] * len(chart_dates),
                                "borderColor": "#0f172a", "borderDash": [6, 3],
                                "backgroundColor": "transparent", "pointRadius": 0, "tension": 0})

    return JSONResponse({
        "product_code":  product_code,
        "product_name":  product_info_meta.get("product_name", product_code),
        "category":      product_info_meta.get("category"),
        "unit":          product_info_meta.get("unit"),
        "our_sale_price": our_sale,
        "buy_price":      buy_price,
        "our_gp_pct":     our_gp_pct,
        "market_min":     market_min,
        "market_avg":     market_avg,
        "market_max":     market_max,
        "market_min_gp":  market_min_gp,
        "market_avg_gp":  market_avg_gp,
        "market_max_gp":  market_max_gp,
        "min_seller":     min_seller,
        "max_seller":     max_seller,
        "gp_alert_pct":   GP_ALERT_PCT,
        "gp_warn_pct":    GP_WARN_PCT,
        "seller_rows":    _serialize_rows(today_rows),
        "chart_dates":    chart_dates,
        "chart_datasets": chart_datasets,
        "has_mapping":    bool(product_keys),
    })


# ── 화면 5: 매핑 수정요청 (DELETE/REPLACE) ────────────────────────────────

@router.get("/change-request/{product_code}", response_class=HTMLResponse)
async def pm_change_request_page(
    request: Request,
    product_code: str,
    plant: str = "ALL",
):
    _require_pm_access(request)
    mappings = portal_db.pm_list_mappings(product_code, plant)
    our_products = _get_our_products(plant)
    product_info = next((p for p in our_products if p["product_code"] == product_code), {})
    return _render(request, "pm_change_request.html",
                   product_code=product_code,
                   product_info=product_info,
                   current_mappings=mappings,
                   plant=plant, plants=PLANTS)


@router.post("/api/change-request/submit")
async def api_change_request_submit(request: Request):
    _require_pm_access(request)
    session = _get_session(request)
    body = await request.json()
    our_product_code    = body.get("our_product_code", "").strip()
    plant               = body.get("plant", "4120").strip()
    request_type        = body.get("request_type", "").strip()  # DELETE | REPLACE
    delete_keys         = body.get("delete_product_keys", [])
    add_items           = body.get("add_product_keys", [])
    reason              = body.get("reason", "").strip()

    if not our_product_code or not request_type or not reason:
        return JSONResponse({"ok": False, "error": "필수값 누락"}, status_code=400)
    if request_type not in ("DELETE", "REPLACE"):
        return JSONResponse({"ok": False, "error": "유효하지 않은 요청 유형"}, status_code=400)
    if not delete_keys:
        return JSONResponse({"ok": False, "error": "삭제 대상을 선택해주세요"}, status_code=400)

    emp_name = session.get("name", session.get("emp_code", ""))
    request_id = portal_db.pm_create_change_request(
        our_product_code=our_product_code,
        plant=plant,
        request_type=request_type,
        delete_product_keys=delete_keys,
        add_product_keys=add_items,
        reason=reason,
        requested_by=session.get("emp_code", ""),
        requested_by_name=emp_name,
    )
    return JSONResponse({"ok": True, "request_id": request_id})


# ── 화면 6: 관리자 수정요청 목록 ─────────────────────────────────────────

@router.get("/admin/requests", response_class=HTMLResponse)
async def pm_admin_requests(
    request: Request,
    status: str = "PENDING",
):
    _require_pm_access(request)
    rows = portal_db.pm_list_change_requests(status or None)
    return _render(request, "pm_admin_requests.html",
                   rows=rows, status_filter=status)


@router.post("/api/admin/review")
async def api_admin_review(request: Request):
    _require_pm_access(request)
    session = _get_session(request)
    body = await request.json()
    request_id = int(body.get("request_id", 0))
    action     = body.get("action", "").strip()   # APPROVE | REJECT
    admin_memo = body.get("admin_memo", "").strip()

    if not request_id or action not in ("APPROVE", "REJECT"):
        return JSONResponse({"ok": False, "error": "잘못된 요청"}, status_code=400)
    if action == "REJECT" and not admin_memo:
        return JSONResponse({"ok": False, "error": "반려 시 사유 입력 필수"}, status_code=400)

    result = portal_db.pm_review_change_request(
        request_id=request_id,
        action=action,
        reviewed_by=session.get("emp_code", ""),
        admin_memo=admin_memo,
    )
    if result is None:
        return JSONResponse({"ok": False, "error": "요청을 찾을 수 없거나 이미 처리됨"}, status_code=404)
    return JSONResponse({"ok": True, "request_id": request_id, "status": action})


# ── 화면 7: 관리자 셀러 설정 ─────────────────────────────────────────────

@router.get("/admin/sellers", response_class=HTMLResponse)
async def pm_admin_sellers(request: Request):
    _require_pm_access(request)
    sellers = portal_db.pm_list_baemin_sellers()
    return _render(request, "pm_admin_sellers.html", sellers=sellers)


@router.post("/api/admin/seller-toggle")
async def api_seller_toggle(request: Request):
    _require_pm_access(request)
    body = await request.json()
    seller_id = int(body.get("seller_id", 0))
    is_active = int(body.get("is_active", 1))
    if not seller_id:
        return JSONResponse({"ok": False, "error": "seller_id 필요"}, status_code=400)
    portal_db.pm_toggle_seller(seller_id, is_active)
    return JSONResponse({"ok": True})


# ── API: 스케줄러 상태 조회 ────────────────────────────────────────────────

def _check_scheduler_auth(request: Request):
    """스케줄러 API: 세션 OR X-Scheduler-Key 헤더 허용."""
    secret = os.getenv("SCHEDULER_SECRET_KEY", "")
    if secret and request.headers.get("X-Scheduler-Key") == secret:
        return  # 시크릿 키 일치 → 통과
    _require_pm_access(request)  # 세션 인증


@router.get("/api/scheduler/status")
async def api_scheduler_status(request: Request):
    _check_scheduler_auth(request)
    try:
        import crawl_scheduler
        return JSONResponse({"ok": True, **crawl_scheduler.status()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/api/scheduler/run-now")
async def api_scheduler_run_now(request: Request):
    """즉시 크롤링 실행 (테스트/수동 재수집용)"""
    _check_scheduler_auth(request)
    import threading, crawl_scheduler
    t = threading.Thread(target=crawl_scheduler._run_crawl, daemon=True, name="crawl-manual")
    t.start()
    return JSONResponse({"ok": True, "message": "크롤링 백그라운드 실행 시작됨"})


# ── 화면 6: 플랫폼 경쟁분석 ────────────────────────────────────────────────

@router.get("/competition", response_class=HTMLResponse)
async def pm_competition(request: Request, plant: str = "ALL"):
    """셀러별 매핑 상품 경쟁가격 비교 화면."""
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"
    return _render(request, "pm_competition.html",
                   active_page="competition", plant=plant, plants=PLANTS)


@router.get("/api/competition")
async def api_competition(request: Request, plant: str = "ALL"):
    """플랫폼 경쟁분석 JSON API.

    반환:
      crawl_date: 최신 수집일
      kpi: {total, win, tie, lose}
      sellers: [{platform, seller_name, seller_id, total, win, tie, lose, win_rate, items}]
        items: [{our_product_code, product_name, our_price, competitor_price, diff, diff_pct, status}]
    """
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"

    # ── 기준 데이터 수집 ──
    all_mappings = portal_db.pm_list_all_mappings(plant)
    if not all_mappings:
        return JSONResponse({"crawl_date": "", "kpi": {"total": 0, "win": 0, "tie": 0, "lose": 0}, "sellers": []})

    # 중복 제거: 동일 (우리상품코드, product_key) 조합은 1건만 사용
    # (plant=4120/4123/4121/ALL 각각에 매핑이 중복 등록된 경우 대응)
    _seen_pairs = set()
    _deduped = []
    for _m in all_mappings:
        _pair = (_m["our_product_code"], _m["product_key"])
        if _pair not in _seen_pairs:
            _seen_pairs.add(_pair)
            _deduped.append(_m)
    all_mappings = _deduped

    price_rows  = _get_base_prices(plant)
    price_map = {r["product_code"]: r for r in price_rows}

    # 상품명 맵
    _our_products_cache = _get_our_products(plant)
    our_name_map = {
        p["product_code"]: p["product_name"]
        for p in _our_products_cache
        if p.get("product_name")
    }
    # 세금분류 맵 (면세 상품은 부가세 10% 차감 없이 GP 계산)
    our_tax_map = {
        p["product_code"]: (p.get("tax_class") or "과세")
        for p in _our_products_cache
    }

    def _resolve(p_code: str) -> str:
        name = our_name_map.get(p_code)
        if name and name != p_code:
            return name
        try:
            rows_fb = _q(f"SELECT MAX(`상품명`) AS nm FROM {T_ZMM60} WHERE `상품코드` = '{p_code}'")
            nm = rows_fb[0]["nm"] if rows_fb else None
            if nm:
                our_name_map[p_code] = nm
                return nm
        except Exception:
            pass
        return p_code

    # 플랫폼 최신가 (전체 매핑 product_key)
    product_keys = [m["product_key"] for m in all_mappings]
    platform_rows = _get_platform_latest(product_keys=product_keys)
    platform_map = {r["product_key"]: r for r in platform_rows}

    # ── 셀러별 그룹화 ──
    # key: (platform, seller_name)
    from collections import defaultdict
    seller_groups = defaultdict(list)
    crawl_dates = {}  # platform → crawl_date

    for m in all_mappings:
        pk = m["product_key"]
        pf_data = platform_map.get(pk)
        if not pf_data:
            continue

        p_code        = m["our_product_code"]
        price_info    = price_map.get(p_code) or {}
        our_price     = price_info.get("avg_sale_price")   # 우리 공급가 (VAT 제외)
        buy_price     = price_info.get("avg_buy_price")    # 우리 구매가
        comp_price    = pf_data.get("price_sale")          # 경쟁사 소비자가 (VAT+수수료 포함)
        seller_name   = pf_data.get("platform_seller_name") or m.get("seller_name") or ""
        seller_id     = m.get("platform_seller_id") or ""
        platform      = pf_data.get("platform") or m.get("platform") or ""
        delivery_type = pf_data.get("delivery_type", "직배송")
        multiplier    = float(m.get("multiplier") or 1.0)
        tag           = m.get("tag", "normal")

        if platform and not crawl_dates.get(platform):
            crawl_dates[platform] = str(pf_data.get("crawl_date", ""))

        # ── 경쟁사 실수취가: 소비자가에서 수수료·VAT 제거 ──
        # comp_net = price_sale × (1 − fee) / VAT제수 / multiplier
        # VAT제수: 우리 상품이 면세면 1.0(차감 없음), 과세면 1.1
        fee = _get_fee(delivery_type, platform, seller_name)
        _tax_status = our_tax_map.get(p_code, "과세")
        _vat_mult = 1.1 if _tax_status == "과세" else 1.0
        if comp_price:
            comp_net = round(float(comp_price) * (1.0 - fee) / _vat_mult / multiplier, 0)
        else:
            comp_net = None

        # ── 맞출 시 이익률: 우리가 comp_net 가격에 팔 때 GP ──
        if comp_net and buy_price:
            match_margin = round((float(comp_net) - float(buy_price)) / float(comp_net) * 100, 1)
        else:
            match_margin = None

        # ── 판정 기준 (match_margin 기반) ──
        if match_margin is not None:
            status = "win" if match_margin >= 10.0 else "lose"
        else:
            status = "unknown"

        # ── 차이: 우리 공급가 vs 경쟁사 실수취가 (동일 단위) ──
        if our_price and comp_net:
            diff     = round(float(our_price) - float(comp_net), 0)
            diff_pct = round((float(our_price) - float(comp_net)) / float(comp_net) * 100, 1)
        else:
            diff = diff_pct = None

        # seller_id는 그룹 키에서 제외 — 동일 셀러명인데 ID 유무로 중복 방지
        seller_groups[(platform, seller_name)].append({
            "_seller_id": seller_id,
            "our_product_code":  p_code,
            "product_name":      _resolve(p_code),
            "our_price":         int(our_price)   if our_price   else None,  # 우리 공급가
            "buy_price":         int(buy_price)   if buy_price   else None,  # 우리 구매가
            "competitor_price":  int(comp_price)  if comp_price  else None,  # 경쟁사 소비자가
            "comp_net_price":    int(comp_net)     if comp_net    else None,  # 경쟁사 실수취가
            "fee_pct":           round(fee * 100, 1),
            "match_margin":      match_margin,                               # 맞출 시 이익률
            "diff":              int(diff)         if diff is not None else None,
            "diff_pct":          diff_pct,
            "status":            status,
            "product_key":       pk,
            "ext_product_name":  pf_data.get("product_name", ""),
            "delivery_type":     delivery_type,
            "tag":               tag,
            "multiplier":        multiplier,
        })

    # ── 셀러 요약 빌드 ──
    sellers = []
    for (platform, seller_name), items in seller_groups.items():
        # seller_id: 그룹 내에서 값이 있는 것 우선 사용
        seller_id = next((i["_seller_id"] for i in items if i.get("_seller_id")), "")
        win  = sum(1 for i in items if i["status"] == "win")
        tie  = sum(1 for i in items if i["status"] == "tie")
        lose = sum(1 for i in items if i["status"] == "lose")
        total_items = len(items)
        win_rate = round(win / total_items * 100) if total_items else 0

        # _seller_id 임시 키 제거
        for _it in items:
            _it.pop("_seller_id", None)

        # 열세 상품 상단 정렬
        items_sorted = sorted(items, key=lambda x: (
            0 if x["status"] == "lose" else 1 if x["status"] == "tie" else 2
        ))
        sellers.append({
            "platform":    platform,
            "seller_name": seller_name,
            "seller_id":   seller_id,
            "total":       total_items,
            "win":         win,
            "tie":         tie,
            "lose":        lose,
            "win_rate":    win_rate,
            "items":       items_sorted,
        })

    # 경쟁우위율 오름차순 (위험 셀러 상단)
    sellers.sort(key=lambda s: s["win_rate"])

    # 전체 KPI
    kpi = {
        "total": sum(s["total"] for s in sellers),
        "win":   sum(s["win"]   for s in sellers),
        "tie":   sum(s["tie"]   for s in sellers),
        "lose":  sum(s["lose"]  for s in sellers),
    }

    return JSONResponse({"crawl_dates": crawl_dates, "crawl_date": " / ".join(f"{k}: {v}" for k, v in crawl_dates.items()), "kpi": kpi, "sellers": sellers})


# ── 화면 7: 실전 경쟁 시뮬레이션 ──────────────────────────────────────────

@router.get("/simulation", response_class=HTMLResponse)
async def pm_simulation(
    request: Request,
    platform: str = "",
    seller_name: str = "",
    seller_id: str = "",
    plant: str = "ALL",
):
    """셀러별 실전 경쟁 시뮬레이션 화면."""
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"
    return _render(request, "pm_simulation.html",
                   active_page="competition",
                   platform=platform,
                   seller_name=seller_name,
                   seller_id=seller_id,
                   plant=plant, plants=PLANTS)


@router.get("/api/simulation")
async def api_simulation(
    request: Request,
    platform: str = "",
    seller_name: str = "",
    seller_id: str = "",
    plant: str = "ALL",
):
    """실전 경쟁 시뮬레이션 데이터 JSON API.

    반환:
      seller: {platform, seller_name, seller_id, total, win, lose, win_rate}
      crawl_date: 최신 수집일
      products: [
        {
          our_product_code, product_name, our_price, buy_price,
          current_margin, prev_sales_amt, prev_sales_qty,
          lines: [{product_key, ext_product_name, competitor_price,
                   comp_net_price, match_margin, delivery_type, fee_pct, status}]
        }
      ]
    """
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"

    # ── 전체 매핑 중 해당 셀러 필터링 ──
    all_mappings = portal_db.pm_list_all_mappings(plant)
    # dedup
    _seen = set()
    _deduped = []
    for _m in all_mappings:
        _pair = (_m["our_product_code"], _m["product_key"])
        if _pair not in _seen:
            _seen.add(_pair)
            _deduped.append(_m)
    all_mappings = _deduped

    # 셀러 필터 보완: 매핑 DB의 seller_name 외에 platform 크롤링 데이터의
    # platform_seller_name도 함께 확인 (경쟁분석과 동일 로직)
    # → 매핑 저장 당시 seller_name이 비어있거나 달랐던 레코드까지 포함
    _plat_pkeys = [m["product_key"] for m in all_mappings if m.get("platform") == platform]
    _plat_rows_for_filter = _get_platform_latest(product_keys=_plat_pkeys)
    _plat_name_map = {r["product_key"]: (r.get("platform_seller_name") or "") for r in _plat_rows_for_filter}

    seller_mappings = []
    for m in all_mappings:
        if m.get("platform") != platform:
            continue
        if seller_id:
            # seller_id 기준 필터 (우선순위 높음)
            if str(m.get("platform_seller_id", "")) == str(seller_id):
                seller_mappings.append(m)
        else:
            # seller_name 기준: 매핑 레코드 OR 크롤링 데이터 중 하나라도 일치하면 포함
            _map_seller  = (m.get("seller_name") or "")
            _plat_seller = _plat_name_map.get(m["product_key"], "")
            if _map_seller == seller_name or _plat_seller == seller_name:
                seller_mappings.append(m)

    if not seller_mappings:
        return JSONResponse({
            "seller": {"platform": platform, "seller_name": seller_name,
                       "seller_id": seller_id, "total": 0,
                       "win": 0, "lose": 0, "win_rate": 0},
            "crawl_date": "",
            "products": [],
        })

    # ── 기준가 / 전월 매출 ──
    price_map  = {r["product_code"]: r for r in _get_base_prices(plant)}
    prev_sales = _get_prev_month_sales_totals(plant)

    # 상품명 맵
    _our_products_cache = _get_our_products(plant)
    our_name_map = {
        p["product_code"]: p["product_name"]
        for p in _our_products_cache
        if p.get("product_name")
    }
    # 세금분류 맵 (면세 상품은 부가세 10% 차감 없이 GP 계산)
    our_tax_map = {
        p["product_code"]: (p.get("tax_class") or "과세")
        for p in _our_products_cache
    }
    def _resolve(p_code: str) -> str:
        name = our_name_map.get(p_code)
        if name and name != p_code:
            return name
        try:
            rows_fb = _q(f"SELECT MAX(`상품명`) AS nm FROM {T_ZMM60} WHERE `상품코드` = '{p_code}'")
            nm = rows_fb[0]["nm"] if rows_fb else None
            if nm:
                our_name_map[p_code] = nm
                return nm
        except Exception:
            pass
        return p_code

    # ── 플랫폼 최신가 (필터용으로 이미 로드된 데이터 재사용) ──
    product_keys = [m["product_key"] for m in seller_mappings]
    # _plat_rows_for_filter는 이미 platform 전체 product_key 기준으로 로드됨
    platform_map = {r["product_key"]: r for r in _plat_rows_for_filter}

    crawl_date = ""

    # ── 우리 상품코드 기준 그룹화 ──
    from collections import defaultdict as _dd
    prod_lines = _dd(list)

    for m in seller_mappings:
        pk = m["product_key"]
        pf_data = platform_map.get(pk)
        if not pf_data:
            continue
        if not crawl_date:
            crawl_date = str(pf_data.get("crawl_date", ""))
        p_code        = m["our_product_code"]
        comp_price    = pf_data.get("price_sale")
        delivery_type = pf_data.get("delivery_type", "직배송")
        _sn           = seller_name
        fee           = _get_fee(delivery_type, platform, _sn)
        buy_price     = (price_map.get(p_code) or {}).get("avg_buy_price")
        multiplier    = float(m.get("multiplier") or 1.0)
        tag           = m.get("tag", "normal")
        # 세금분류: 면세 상품은 부가세 10% 차감 없이 경쟁사 실수취가 계산
        _tax_status   = our_tax_map.get(p_code, "과세")
        _vat_mult     = 1.1 if _tax_status == "과세" else 1.0

        comp_net = round(float(comp_price) * (1.0 - fee) / _vat_mult / multiplier, 0) if comp_price else None
        if comp_net and buy_price:
            match_margin = round((float(comp_net) - float(buy_price)) / float(comp_net) * 100, 1)
        else:
            match_margin = None
        status = ("win" if match_margin is not None and match_margin >= 10.0
                  else "lose" if match_margin is not None else "unknown")

        prod_lines[p_code].append({
            "product_key":      pk,
            "ext_product_name": pf_data.get("product_name", ""),
            "competitor_price": int(comp_price) if comp_price else None,
            "comp_net_price":   int(comp_net)   if comp_net   else None,
            "match_margin":     match_margin,
            "delivery_type":    delivery_type,
            "fee_pct":          round(fee * 100, 1),
            "status":           status,
            "tag":              tag,
            "multiplier":       multiplier,
        })

    # ── 상품 목록 빌드 ──
    products = []
    for p_code, lines in prod_lines.items():
        pi = price_map.get(p_code) or {}
        our_price  = pi.get("avg_sale_price")
        buy_price  = pi.get("avg_buy_price")
        ps         = prev_sales.get(p_code) or {}
        # 현재 GP (VAT 제외 공급가 기준)
        if our_price and buy_price and float(our_price) > 0:
            cur_margin = round((float(our_price) - float(buy_price)) / float(our_price) * 100, 1)
        else:
            cur_margin = None
        # 라인: 열세 우선 정렬
        lines_sorted = sorted(lines, key=lambda x: (
            0 if x["status"] == "lose" else 1 if x["status"] == "win" else 2
        ))
        products.append({
            "our_product_code": p_code,
            "product_name":     _resolve(p_code),
            "our_price":        int(our_price)  if our_price  else None,
            "buy_price":        int(buy_price)  if buy_price  else None,
            "current_margin":   cur_margin,
            "prev_sales_amt":   int(ps.get("prev_sales_amt") or 0),
            "prev_sales_qty":   int(ps.get("prev_sales_qty") or 0),
            "lines":            lines_sorted,
        })

    # 전월 매출 기준 내림차순
    products.sort(key=lambda x: x["prev_sales_amt"], reverse=True)

    # 셀러 요약 (win/lose 집계)
    all_lines = [ln for p in products for ln in p["lines"]]
    win_cnt  = sum(1 for l in all_lines if l["status"] == "win")
    lose_cnt = sum(1 for l in all_lines if l["status"] == "lose")
    total_l  = len(all_lines)
    seller_summary = {
        "platform":    platform,
        "seller_name": seller_name,
        "seller_id":   seller_id,
        "total":       total_l,
        "win":         win_cnt,
        "lose":        lose_cnt,
        "win_rate":    round(win_cnt / total_l * 100) if total_l else 0,
    }

    return JSONResponse({
        "seller":     seller_summary,
        "crawl_date": crawl_date,
        "products":   products,
    })


@router.get("/api/simulation/seller-skus")
async def api_simulation_seller_skus(
    request: Request,
    platform: str = "",
    seller_name: str = "",
    seller_id: str = "",
    plant: str = "ALL",
):
    """셀러의 플랫폼 전체 SKU + 매핑률.
    ─ T_SILVER에서 해당 셀러의 최신 수집 상품 전체 조회
    ─ 매핑된 product_key 대비 얼마나 커버되는지 비율 반환
    """
    _require_pm_access(request)

    # 셀러명 보안 (SQL injection 예방)
    safe_seller = seller_name.replace("'", "''")

    try:
        # 전체 건수 별도 집계 (LIMIT 없이)
        cnt_rows = _q(f"""
            SELECT COUNT(*) AS cnt
            FROM {T_SILVER} p
            INNER JOIN (
                SELECT MAX(crawl_date) AS max_date
                FROM {T_SILVER}
                WHERE platform = '{platform}'
                  AND platform_seller_name = '{safe_seller}'
            ) md ON p.crawl_date = md.max_date
            WHERE p.platform = '{platform}'
              AND p.platform_seller_name = '{safe_seller}'
        """)
        actual_total = int((cnt_rows or [{}])[0].get("cnt", 0))

        rows = _q(f"""
            SELECT
                p.product_key,
                p.product_name,
                p.spec,
                p.price_sale,
                p.price_original,
                p.delivery_type,
                p.is_free_delivery,
                p.crawl_date
            FROM {T_SILVER} p
            INNER JOIN (
                SELECT MAX(crawl_date) AS max_date
                FROM {T_SILVER}
                WHERE platform = '{platform}'
                  AND platform_seller_name = '{safe_seller}'
            ) md ON p.crawl_date = md.max_date
            WHERE p.platform = '{platform}'
              AND p.platform_seller_name = '{safe_seller}'
            ORDER BY p.price_sale
        """)
        rows = _serialize_rows(rows or [])
    except Exception as e:
        logger.warning(f"[simulation/seller-skus] 조회 실패: {e}")
        rows = []
        actual_total = 0

    # 매핑된 product_key 집합
    # platform DB에서 이미 이 셀러의 product_key 목록을 조회했으므로
    # 그 키 셋과 매핑 레코드를 교차 확인 (seller_name 불일치 문제 우회)
    all_keys_for_seller = {r["product_key"] for r in rows}
    all_mappings = portal_db.pm_list_all_mappings(plant)
    mapped_keys = set()
    for m in all_mappings:
        if m.get("platform") == platform and m["product_key"] in all_keys_for_seller:
            mapped_keys.add(m["product_key"])

    # actual_total: COUNT 쿼리 기준 실제 전체 수 (rows가 잘리지 않았다면 같음)
    total   = actual_total if actual_total > len(rows) else len(rows)
    mapped  = sum(1 for r in rows if r["product_key"] in mapped_keys)
    mapping_rate = round(mapped / total * 100, 1) if total else 0

    # 매핑여부 플래그 추가
    for r in rows:
        r["is_mapped"] = r["product_key"] in mapped_keys

    crawl_date = str(rows[0]["crawl_date"]) if rows else ""

    return JSONResponse({
        "total":        total,
        "mapped":       mapped,
        "unmapped":     total - mapped,
        "mapping_rate": mapping_rate,
        "crawl_date":   crawl_date,
        "skus":         rows,
    })


@router.get("/api/debug/baemin")
async def api_debug_baemin(request: Request):
    """Azure 서버에서 배민 API 직접 호출 테스트."""
    _check_scheduler_auth(request)
    import requests as _req
    BAEMIN_API = "https://gw-api-mart.baemin.com/front-api/v1/sellers"
    BAEMIN_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://mart.baemin.com/",
        "Origin": "https://mart.baemin.com",
    }
    results = {}

    # 1) 셀러 목록 API
    try:
        r = _req.get(BAEMIN_API, headers=BAEMIN_HEADERS, timeout=10)
        results["sellers_api"] = {"status": r.status_code, "body": r.text[:300]}
    except Exception as e:
        results["sellers_api"] = {"error": str(e)}

    # 2) 상품 페이지 API (셀러 907 테스트)
    test_sellers = ["907", "2090", "2089"]
    results["product_api"] = {}
    for sid in test_sellers:
        try:
            url = f"https://gw-api-mart.baemin.com/front-api/v1/sellers/{sid}/goods/paging"
            r = _req.get(url, headers=BAEMIN_HEADERS,
                         params={"page": 0, "size": 5, "sortType": "RECOMMEND"},
                         timeout=10)
            body = r.json() if r.status_code == 200 else r.text[:200]
            total = None
            if r.status_code == 200 and isinstance(body, dict):
                total = (body.get("data") or {}).get("goodsList", {}).get("totalElements")
            results["product_api"][sid] = {
                "status": r.status_code,
                "totalElements": total,
                "success": (body.get("success") if isinstance(body, dict) else None),
            }
        except Exception as e:
            results["product_api"][sid] = {"error": str(e)}

    return JSONResponse({"ok": True, "results": results})


# ── API: T_SILVER 기준 셀러 목록 (워크스페이스용) ────────────────────────────

@router.get("/api/sellers-from-silver")
async def api_sellers_from_silver(request: Request, platform: str = ""):
    """실제 크롤링된 모든 셀러 목록 (T_SILVER 기준).
    platform 없으면 실제 platform 목록 반환.
    """
    _require_pm_access(request)
    try:
        # platform 없으면 실제 플랫폼 리스트 반환
        if not platform:
            plat_rows = _q(f"""
                SELECT platform, COUNT(DISTINCT platform_seller_name) AS seller_cnt,
                       COUNT(*) AS sku_cnt, MAX(crawl_date) AS last_crawl
                FROM {T_SILVER}
                GROUP BY platform
                ORDER BY sku_cnt DESC
            """) or []
            return JSONResponse({"platforms": [
                {"platform": r.get("platform", ""),
                 "seller_count": r.get("seller_cnt", 0),
                 "sku_count": r.get("sku_cnt", 0),
                 "last_crawl": str(r.get("last_crawl", ""))}
                for r in plat_rows
            ], "sellers": []})
        max_row = _q(f"SELECT MAX(crawl_date) AS md FROM {T_SILVER} WHERE platform='{platform}'") or []
        if not max_row or not max_row[0].get("md"):
            # 플랫폼은 있는데 데이터 없음 → 실제 플랫폼 목록도 처럴 반환
            plat_rows = _q(f"SELECT DISTINCT platform FROM {T_SILVER} ORDER BY platform") or []
            real_platforms = [r.get("platform", "") for r in plat_rows]
            return JSONResponse({"sellers": [], "error": f"'{platform}' 플랫폼 데이터 없음. 실제 플랫폼: {real_platforms}"})
        max_date = max_row[0]["md"]
        rows = _q(f"""
            SELECT platform_seller_name AS seller_name,
                   COUNT(*) AS sku_count
            FROM {T_SILVER}
            WHERE platform = '{platform}'
              AND crawl_date = '{max_date}'
            GROUP BY platform_seller_name
            ORDER BY sku_count DESC
        """) or []
        # 매핑 수 포함
        all_mappings = portal_db.pm_list_all_mappings("ALL")
        mapped_cnt = {}
        for m in all_mappings:
            sn = m.get("seller_name", "")
            if m.get("platform") == platform and sn:
                mapped_cnt[sn] = mapped_cnt.get(sn, 0) + 1
        result = []
        for r in rows:
            sn = r.get("seller_name", "")
            total = r.get("sku_count", 0)
            mapped = mapped_cnt.get(sn, 0)
            result.append({
                "seller_name": sn,
                "sku_count":   total,
                "mapped_count": mapped,
                "unmapped":    total - mapped,
            })
        return JSONResponse({"sellers": result, "crawl_date": str(max_date)})
    except Exception as e:
        logger.warning(f"[sellers-from-silver] 실패: {e}")
        import traceback
        return JSONResponse({"sellers": [], "error": str(e), "traceback": traceback.format_exc()[-1000:]})


# ── 화면 8: 매핑 워크스페이스 ────────────────────────────────────────────────

@router.get("/mapping-workspace", response_class=HTMLResponse)
async def pm_mapping_workspace(
    request: Request,
    platform: str = "",
    seller_name: str = "",
    seller_id: str = "",
    plant: str = "ALL",
):
    """AI 매핑 제안 워크스페이스 화면."""
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "ALL"
    return _render(request, "pm_mapping_workspace.html",
                   active_page="mapping-workspace",
                   platform=platform,
                   seller_name=seller_name,
                   seller_id=seller_id,
                   plant=plant, plants=PLANTS)


# ── AI 유사도 매핑 엔진 헬퍼 ────────────────────────────────────────────────

def _strip_seller(name: str, seller_name: str) -> str:
    """상품명에서 셀러명 및 대괄호([...]) 태그를 제거한 정제 상품명 반환.
    예) '[다봄푸드] 그린 돈까스소스 1.8L 1.8l' + '다봄푸드' → '그린 돈까스소스 1.8L'
    - [N], [행사], [냉장], [셀러명] 등 대괄호 구간 전부 제거
    - () 소괄호는 용량/규격 정보이므로 유지
    """
    import re as _re
    cleaned = (name or "").strip()
    # 1) 대괄호([...]) 안 내용 전부 제거
    cleaned = _re.sub(r'\[[^\]]*\]', ' ', cleaned)
    # 2) 셀러명 단어 제거 (2글자 이상)
    if seller_name:
        seller_words = [w.strip() for w in _re.split(r'[\s\-_/·]+', seller_name) if len(w.strip()) >= 2]
        for word in seller_words:
            cleaned = _re.sub(
                r'(?<![가-힣a-zA-Z0-9])' + _re.escape(word) + r'(?![가-힣a-zA-Z0-9])',
                ' ', cleaned, flags=_re.IGNORECASE
            )
    # 3) 남은 공백 정리
    cleaned = _re.sub(r'\s+', ' ', cleaned).strip()
    # 정제 후 너무 짧으면 원본 반환
    return cleaned if len(cleaned) >= 2 else (name or "").strip()


def _parse_volume(name: str):
    """상품명에서 단위 용량/중량을 ml 또는 g 단위로 추출.
    *N 수량은 무시하고 단위 용량만 반환 (이중계산 방지).
    예) 200g*10ea → 200g, 1.8l*6 → 1800ml
    반환: (ml_or_g: float, unit: str) 또는 (None, None)
    """
    import re as _r
    text = (name or "").lower()
    patterns = [
        (_r.compile(r'(\d[\d.]+)\s*l\b'),     lambda m: float(m.group(1)) * 1000, 'ml'),
        (_r.compile(r'(\d[\d.]*)\s*ml\b'),    lambda m: float(m.group(1)),        'ml'),
        (_r.compile(r'(\d[\d.]+)\s*kg\b'),    lambda m: float(m.group(1)) * 1000, 'g'),
        (_r.compile(r'(\d[\d.]*)\s*g\b'),     lambda m: float(m.group(1)),        'g'),
        (_r.compile(r'(\d[\d.]*)\s*키로\b'),  lambda m: float(m.group(1)) * 1000, 'g'),
    ]
    for pat, fn, unit in patterns:
        m = pat.search(text)
        if m:
            try:
                val = fn(m)
                if val > 0:
                    return val, unit
            except Exception:
                pass
    return None, None


def _tokenize(name: str) -> set[str]:
    """상품명을 의미 토큰으로 분리.
    bi/tri-gram은 한글 6자 이상 단어에만 적용
    ('슬라이스' 4자 등 짧은 단어의 n-gram 오염 방지).
    """
    import re
    name = (name or "").strip()
    name = re.sub(r'[/·•\-_,\(\)\[\]{}]', ' ', name)
    tokens = set()
    for t in re.split(r'\s+', name):
        t = t.strip()
        if len(t) >= 2:
            tokens.add(t.lower())
            korean = re.sub(r'[^가-힣]', '', t)
            # bi/tri-gram은 5자 이상 한글 단어에 적용 (불닭볶음, 치킨양념 등 5자 상품명 포함)
            if len(korean) >= 5:
                for i in range(len(korean) - 1):
                    tokens.add(korean[i:i+2])
                for i in range(len(korean) - 2):
                    tokens.add(korean[i:i+3])
    return tokens


# 모듈 레벨 regex 캐시 — _score_mapping 매 호출마다 re.compile 방지
_UNIT_PAT2 = __import__('re').compile(r'^약?\d[\d.]*[a-zA-Z]*$')


def _score_mapping(platform_name: str, platform_price: float | None,
                   our_name: str, our_sale_price: float | None,
                   buy_price: float | None,
                   delivery_type: str, platform: str, seller_name: str,
                   pattern_bonus: float = 0.0,
                   our_prod: dict | None = None) -> float:
    """플랫폼 상품 ↔ 우리 상품 유사도 점수 (0~100).

    가중치: 텍스트 60 + 키워드보너스 15 + 가격 20 + 패턴 10 = max 100
    our_prod 제공 시 v2 개선 적용: 카테고리 동치 보너스 + 총중량 직접 비교
    게이트: 텍스트+키워드 < 10점 시 0 반환 (가격만으로는 제안 불가)
    """
    import re as _re
    score = 0.0

    # 0. 온도조건 하드필터 (our_prod 있을 때)
    # 코드: 10=상온, 20=냉장, 30=냉동, 40=초저온냉동
    if our_prod:
        _our_temp = (our_prod.get("temp_cond") or "").strip()
        if _our_temp:
            _plat_frozen = "냉동" in (platform_name or "")
            _plat_chilled = "냉장" in (platform_name or "")
            if _plat_frozen and _our_temp not in {"30", "40"}:
                return 0.0  # 플랫폼=냉동, 우리=상온/냉장 → 불일치
            if _plat_chilled and _our_temp not in {"20"}:
                return 0.0  # 플랫폼=냉장, 우리=상온/냉동 → 불일치

    # 동의어(alias) 확장: 플랫폼 상품명의 별칭을 표준어로 병기
    _ALIAS = {
        '프리마': '프림',
        '프림': '프리마',
        '폰즈': '소스',
        '자반': '조림',
        '까르보': '까르보나라',
        '크럼블': '칩 토핑',
    }
    _pname_expanded = platform_name
    for _ak, _av in _ALIAS.items():
        if _ak in _pname_expanded:
            _pname_expanded = _pname_expanded.replace(_ak, _ak + ' ' + _av)

    pt = _tokenize(_pname_expanded)
    ot = _tokenize(our_name)

    # 1. 토큰 겹침 (60점) - Overlap Coefficient
    #    숫자+단위 토큰(1kg, 836g 등)과 범용 수식어(슬라이스, 냉동 등)는 텍스트 점수에서 제외
    _unit_pat2 = _UNIT_PAT2  # 모듈 레벨 캐시 사용
    _STOP = {
        # '냉동', '냉장' → 온도조건 하드필터로 처리, STOP에서 제거 (텍스트 점수에 반영)
        '슬라이스', '신선', '건조', '원물', '국산', '수입',
        '일반', '특대', '대용량', '소포장', '개별', '낱개', '원터치', '직배송',
        '무료배송', '당일배송', '묶음', '세트', '팩', '개입', '입점',
        '필리핀산', '국내산', '수입산', '베트남산', '미국산', '호주산',
        'new', 'ea',
    }
    text_score = 0.0
    # [v10] 브랜드 파생 토큰 필터용 집합 (블록 밖에서 정의하여 _plat_core 에서도 재사용)
    _bs_check = {
        'cj', '오뚜기', '청정원', '동원', '삼양', '사조', '오뗄', '에쓰푸드',
        '대림', '롯데', '하인즈', '풍전', '해표', '샘표', '한성', '미성',
        '굿프랜즈', '면사랑', '칠갑', '뚜레반', '영풍', '사옹원', '농심',
        '신송', '청솔', '범아', '조흥', '웅진', '코다노', '다봄', '현대',
        '삼양식품', '동원에프앤비', '사조대림', '사조해표', '사조오양',
        '롯데제과', '롯데푸드', '매일유업', '남양유업',
    }
    common = set()
    if pt and ot:
        common = pt & ot
        # 텍스트 점수용 공통 토큰: 숫자단위·범용수식어 제외
        # [v10] 브랜드 파생 토큰도 제외: 삼양식품 trigram(삼양식, 양식품 등)이
        #       공통 토큰으로 잘못 집계되어 text_score 부풀리는 문제 방지
        _brand_derived_tokens = {
            _ct for _ct in common
            if any(_bk in _ct or _ct in _bk for _bk in _bs_check if len(_bk) >= 2)
        }
        common_text = {t for t in common
                       if not _unit_pat2.match(t) and t not in _STOP
                       and t not in _brand_derived_tokens}
        if common_text:
            overlap = len(common_text) / min(len(pt), len(ot))
            text_score = overlap * 60.0
            short_ratio = sum(1 for t in common_text if len(t) <= 2) / max(len(common_text), 1)
            text_score -= short_ratio * 8.0
        text_score = max(0.0, text_score)

    # 2. 핵심 키워드 직접 포함 보너스 (15점)
    #    3자 이상 의미 토큰이 상대방 상품명에 substring으로 포함되면 부여
    #    범용 식품/상품 수식어(슬라이스, 냉동, 국산 등)는 _STOP으로 제외
    keyword_bonus = 0.0
    our_lower  = (our_name or "").lower()
    plat_lower = (platform_name or "").lower()
    plat_meaningful = [
        t for t in pt
        if len(t) >= 3
        and t not in _STOP
        and not _re.match(r'^[\d\.]+', t)
    ]
    our_meaningful = [
        t for t in ot
        if len(t) >= 3
        and t not in _STOP
        and not _re.match(r'^[\d\.]+', t)
    ]
    # 매칭된 키워드 수 집계 (중복 제외)
    _matched_kw: list = []
    for w in plat_meaningful:
        if w in our_lower:
            _matched_kw.append(w)
    for w in our_meaningful:
        if w in plat_lower and w not in _matched_kw:
            _matched_kw.append(w)
    _n_matched = len(_matched_kw)
    if _n_matched >= 3:   keyword_bonus = 15.0
    elif _n_matched == 2: keyword_bonus = 10.0
    elif _n_matched == 1: keyword_bonus = 5.0
    else:                 keyword_bonus = 0.0

    # 게이트: 텍스트+키워드 점수 15 미만 → 가격 유사도만으로는 제안하지 않음
    if text_score + keyword_bonus < 15.0:
        return 0.0

    score = text_score + keyword_bonus

    # 핵심 토큰 0겹침 패널티 (브랜드/산지 제외 후 비교 — 브랜드 낚임 방지)
    _BRAND_STOP = {
        'cj', '오뚜기', '청정원', '동원', '삼양', '사조', '오뗄', '에쓰푸드',
        '대림', '롯데', '하인즈', '풍전', '해표', '샘표', '한성', '미성',
        '굿프랜즈', '면사랑', '칠갑', '뚜레반', '영풍', '사옹원', '농심',
        '신송', '청솔', 'be', 'chef', 'sf', 'k', 'new',
        '범아', '조흥', '해인식품', '웅진', '룸모', '델가', '피니',
        '분다버그', '시미루', '코다노', '다봄', '현대',
        '국내산', '중국산', '이탈리아산', '태국산', '베트남산', '호주산',
        '미국산', '필리핀산', '뉴질랜드산', '칠레산', '오스트리아산',
        # [v10] 법인명 복합어 추가: 브랜드+식품/제과 등 compound가 핵심어 겹침으로 오판되는 문제 방지
        # 예: 삼양식품 사또밥 vs 삼양라면 → 삼양식품 공통 → 핵심어 패널티 미발동 버그 수정
        '삼양식품', '동원에프앤비', '청정원홈푸드', '사조대림', '사조해표', '사조오양',
        '롯데제과', '롯데푸드', '농심켈로그', '오뚜기라면', '씨제이제일제당',
        '대상주식회사', '매일유업', '남양유업', '서울우유', '빙그레',
    }
    # 포함 관계 체크: '자판기' ⊂ '자판기용' 처럼 한쪽이 다른 쪽을 포함하면 겹침으로 처리
    def _has_core_overlap(pc: set, oc: set) -> bool:
        if pc & oc:
            return True
        for _p in pc:
            for _o in oc:
                if _p in _o or _o in _p:
                    return True
        return False
    # [v8 Phase1] _our_core empty 버그 수정:
    # 기존: _our_core and → 자사 상품명이 짧아서(케찹 2자, 라면 2자 등) _our_core가 비면 패널티 스킵됨
    # [v10] _plat_core도 브랜드 파생 토큰 제외 (삼양식품 trigram 등)
    _plat_core = {t for t in plat_meaningful
                  if t not in _BRAND_STOP and len(t) >= 3
                  and not any(_bk in t or t in _bk for _bk in _bs_check if len(_bk) >= 2)}
    _our_all = {t for t in ot if len(t) >= 2}  # 2자 이상 전체 토큰 (케찹, 비엔나 등 포함)
    if _plat_core and not _has_core_overlap(_plat_core, _our_all):
        score -= 30.0   # 핵심어 0% 겹침 → 브랜드만 같은 다른 상품 (v8: _our_core empty 케이스도 적용)

    # 플레이버/라인 구분 패널티 (4자+ 고유어 불일치)
    # "키위애플드레싱" vs "유자파인드레싱" 처럼 브랜드·카테고리는 같지만 플레이버가 다른 경우 추가 패널티
    # [v7 버그수정] 기존: _has_core_overlap(_plat_long, _our_long) → 공통 브랜드 토큰(마리브리자드 등)이
    # 양쪽에 모두 있으면 겹침으로 판단해 패널티 미적용되는 버그 수정
    # 수정: 공통 토큰을 제거한 배타적 토큰끼리 비교 → 플레이버만 다른 케이스 정확히 감지
    _plat_long = {t for t in plat_meaningful if t not in _BRAND_STOP and len(t) >= 4}
    _our_long  = {t for t in our_meaningful  if t not in _BRAND_STOP and len(t) >= 4}
    # 배타적 토큰: 상대방에 없는(substring 포함) 토큰만 추출
    _plat_excl = {p for p in _plat_long if not any(p in o or o in p for o in _our_long)}
    _our_excl  = {o for o in _our_long  if not any(o in p or p in o for p in _plat_long)}
    # 양쪽 다 배타적 4자+ 토큰이 존재 → 플레이버/라인이 서로 다른 상품
    if _plat_excl and _our_excl:
        score -= 15.0   # 플레이버 불일치 패널티
    #      our_prod.total_weight(KG) 제공 시 직접 비교, 없으면 상품명 파싱
    # [v9-1] 텍스트 점수 0이면 volume_bonus 차단:
    # 연근(약500g) → 삼계닭(약500g) 처럼 제품명 토큰 겹침이 전혀 없고 용량만 같은 오매핑 방지
    # text_score=0 → 제품명 공통 토큰 없음 → 용량만 같은 우연의 일치 → 보너스 차단
    _vol_applied = False
    if our_prod:
        try:
            our_wkg = float(our_prod.get("total_weight") or our_prod.get("net_weight") or 0) or None
            our_wg  = our_wkg * 1000 if our_wkg else None  # KG → g
        except (TypeError, ValueError):
            our_wg = None
        if our_wg and our_wg > 0:
            pv, pu = _parse_volume(platform_name)
            if pv and pu == 'g':  # g↔g 비교만 (단위 불확실성 방지)
                rv = min(pv, our_wg) / max(pv, our_wg)
                if rv >= 0.9 and text_score > 0:  # [v9-1] 텍스트 점수 없으면 volume_bonus 차단
                    score += 15.0
                    _vol_applied = True
                # 불일치 시 패널티 없음 (폴백: 상품명 파싱으로 판단)
    if not _vol_applied:
        pv, pu = _parse_volume(platform_name)
        ov, ou = _parse_volume(our_name)
        if pv and ov and pu == ou:          # 같은 단위계(ml vs ml, g vs g)
            ratio_v = min(pv, ov) / max(pv, ov)
            if ratio_v >= 0.9 and text_score > 0:  # [v9-1] 텍스트 점수 없으면 volume_bonus 차단
                score += 15.0
            elif ratio_v >= 0.7:
                pass                        # 10~30% 차이: 중립
            elif ratio_v >= 0.4:
                score -= 12.0               # 30~60% 차이
            elif ratio_v >= 0.1:
                score -= 25.0               # 5배↑ 차이: 강한 패널티
            else:
                score -= 40.0               # 10배↑: 사실상 탈락

    # [v9-2 / v10 수정] 브랜드 매칭 보너스 (+5pt):
    # 플랫폼 상품명에 명시된 브랜드가 자사 상품명에도 있으면 +5pt
    # [v10] 조건 추가: text_score > 0 (제품명 토큰 겹침 있을 때만 적용)
    # → 사또밥 vs 삼양라면처럼 브랜드만 같고 제품이 전혀 다른 케이스에서 보너스 역효과 방지
    _KNOWN_BRANDS = {
        'cj','오뚜기','청정원','동원','삼양','사조','롯데','대상','해표','샘표',
        '한성','면사랑','농심','풍전','하인즈','삼진','모노링크','폰타나',
        '랜시','담두','이츠웰','셀플러스','매일','매일유업','빙그레','남양','서울우유',
    }
    if text_score > 0:  # [v10] 제품명 겹침 없으면 브랜드 보너스 차단
        for _br in _KNOWN_BRANDS:
            if _br in plat_lower and _br in our_lower:
                score += 5.0
                break  # 첫 번째 일치만 적용

    # 3-b. 카테고리 계층 보너스 (our_prod 제공 시, 최대 12점)
    #      대분류/중분류/소분류가 플랫폼 상품명 토큰과 겹칠 때 부여
    if our_prod:
        _cat_str = " ".join(filter(None, [
            our_prod.get("category") or "",
            our_prod.get("mid_category") or "",
            our_prod.get("sub_category") or "",
            our_prod.get("product_group") or "",
        ]))
        if _cat_str:
            _cat_tokens = _tokenize(_cat_str) & pt
            _cat_m = {t for t in _cat_tokens if len(t) >= 2 and t not in _STOP}
            score += min(len(_cat_m) * 4, 12)  # 토큰 1개당 4점, 최대 12점

    # 3. 가격 유사도 (20점): 플랫폼 실판매가 vs 우리 공급가
    #    우리 공급가 데이터 없으면 중립점수(10점) 부여 → 가격 데이터 없는 상품이 부당하게 밀리지 않도록
    if platform_price and our_sale_price and our_sale_price > 0:
        try:
            fee = _get_fee(delivery_type, platform, seller_name)
            comp_net = float(platform_price) * (1.0 - fee) / 1.1
            ratio = comp_net / float(our_sale_price)
            if 0.7 <= ratio <= 1.4:
                price_score = 20.0 * (1.0 - abs(ratio - 1.0) / 0.7)
            else:
                price_score = max(0.0, 20.0 * (1.0 - abs(ratio - 1.0) / 2.0))
            score += price_score
        except Exception:
            score += 10.0  # 계산 실패 시 중립
    elif platform_price and not our_sale_price:
        score += 10.0  # 가격 데이터 없는 상품 → 중립 10점 (0점으로 패널티 X)

    # 4. 기존 매핑 패턴 보너스 (10점)
    score += min(10.0, pattern_bonus * 10.0)

    return round(max(0.0, min(100.0, score)), 1)


def _build_pattern_map(all_mappings, our_products):
    """기존 매핑에서 (플랫폼상품명 토큰 → 우리상품코드) 패턴 추출.
    반환: {our_product_code → pattern_strength(0~1)}"""
    from collections import Counter
    our_name_map = {p["product_code"]: (p.get("product_name") or "") for p in our_products}
    token_code = {}
    for m in all_mappings:
        code = m.get("our_product_code", "")
        pname = m.get("product_name", "")
        for t in _tokenize(pname):
            if t not in token_code:
                token_code[t] = Counter()
            token_code[t][code] += 1
    # code → 토큰 매칭 강도 합산
    code_strength = {}
    for t, counter in token_code.items():
        total = sum(counter.values())
        for code, cnt in counter.items():
            code_strength[code] = code_strength.get(code, 0) + cnt / total
    return code_strength


# ── API: AI 매핑 제안 ─────────────────────────────────────────────────────

@router.get("/api/pm-ai/debug")
async def api_mapping_ai_debug(
    request: Request,
    platform: str = "",
    seller_name: str = "",
    plant: str = "ALL",
):
    """AI 매핑 진단용 엔드포인트: 각 단계별 결과 반환."""
    _require_pm_access(request)
    safe_seller = seller_name.replace("'", "''")
    steps = {}
    total_rows = 0
    plat_sample = []
    mapped_keys = set()
    our_products = []
    base_prices = {}
    sample_scores = []

    # 1단계: 플랫폼 SKU 카운트
    try:
        plat_count = _q(f"""
            SELECT COUNT(*) AS cnt FROM {T_SILVER}
            WHERE platform='{platform}' AND platform_seller_name='{safe_seller}'
        """) or []
        row0 = plat_count[0] if plat_count else {}
        total_rows = list(row0.values())[0] if row0 else 0
        steps["step1_plat_count"] = f"OK: {total_rows}개"
    except Exception as e:
        steps["step1_plat_count"] = f"ERROR: {e}"

    # 2단계: 샘플 SKU 조회
    try:
        plat_sample = _q(f"""
            SELECT product_key, product_name, price_sale, delivery_type FROM {T_SILVER}
            WHERE platform='{platform}' AND platform_seller_name='{safe_seller}'
            LIMIT 3
        """) or []
        steps["step2_plat_sample"] = f"OK: {len(plat_sample)}건"
    except Exception as e:
        steps["step2_plat_sample"] = f"ERROR: {e}"

    # 3단계: 매핑 목록
    try:
        all_mappings = portal_db.pm_list_all_mappings(plant)
        mapped_keys = {m["product_key"] for m in all_mappings if m.get("is_active", 1)}
        steps["step3_mappings"] = f"OK: 전체 {len(all_mappings)}개, 활성 {len(mapped_keys)}개"
    except Exception as e:
        steps["step3_mappings"] = f"ERROR: {e}"

    # 4단계: 우리 상품 목록
    try:
        our_products = _get_our_products(plant)
        steps["step4_our_products"] = f"OK: {len(our_products)}개"
    except Exception as e:
        steps["step4_our_products"] = f"ERROR: {e}"

    # 5단계: 기준가
    try:
        base_prices = {r["product_code"]: r for r in _get_base_prices(plant)}
        steps["step5_base_prices"] = f"OK: {len(base_prices)}개"
    except Exception as e:
        steps["step5_base_prices"] = f"ERROR: {e}"

    # 6단계: 샘플 점수
    try:
        if plat_sample and our_products:
            p_row = plat_sample[0]
            for op in our_products[:5]:
                bp = base_prices.get(op["product_code"], {})
                sc = _score_mapping(
                    p_row.get("product_name", ""), p_row.get("price_sale"),
                    op.get("product_name", ""), bp.get("avg_sale_price"), bp.get("avg_buy_price"),
                    p_row.get("delivery_type", "직배송"), platform, seller_name
                )
                sample_scores.append({"plat": p_row.get("product_name", ""), "our": op.get("product_name", ""), "score": sc})
            steps["step6_scores"] = f"OK: {len(sample_scores)}개 계산"
        else:
            steps["step6_scores"] = f"SKIP: plat_sample={len(plat_sample)}, our_products={len(our_products)}"
    except Exception as e:
        steps["step6_scores"] = f"ERROR: {e}"

    return JSONResponse({
        "plat_total_rows":    total_rows,
        "plat_sample":        _serialize_rows(plat_sample),
        "mapped_count":       len(mapped_keys),
        "our_products_count": len(our_products),
        "base_prices_count":  len(base_prices),
        "sample_scores":      sample_scores,
        "steps":              steps,
    })


# ── AI 매핑 잡 폴링 엔드포인트 ────────────────────────────────────────────────
@router.get("/api/pm-ai/suggest/poll/{job_id}")
async def api_suggest_poll(request: Request, job_id: str):
    _require_pm_access(request)
    job = _job_store.get(job_id)
    if job is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(job)


# ── AI 매핑 피드백 (수기 매핑 정답 저장 → 다음 분석에 반영) ────────────────────
# 저장 경로: /home/pm_cache/pm_feedback.json (디스크 영속)
_feedback_cache: dict = {}  # product_key → {our_product_code, product_name, ...}
_FEEDBACK_PATH = (_DISK_CACHE_DIR / "pm_feedback.json") if _DISK_CACHE_DIR else None


def _load_feedback() -> None:
    """앱 시작 시 또는 필요할 때 피드백 파일 로드."""
    global _feedback_cache
    if not _FEEDBACK_PATH:
        return
    try:
        if _FEEDBACK_PATH.exists():
            import json as _j
            with open(_FEEDBACK_PATH, "r", encoding="utf-8") as f:
                _feedback_cache = _j.load(f)
            logger.info(f"[feedback] 로드 완료 {len(_feedback_cache)}건")
    except Exception as e:
        logger.warning(f"[feedback] 로드 실패: {e}")


def _save_feedback() -> None:
    """피드백 메모리 → 디스크 저장."""
    if not _FEEDBACK_PATH:
        return
    try:
        import json as _j
        with _disk_lock:
            with open(_FEEDBACK_PATH, "w", encoding="utf-8") as f:
                _j.dump(_feedback_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[feedback] 저장 실패: {e}")


# 앱 시작 시 피드백 로드
_load_feedback()


@router.post("/api/pm-ai/feedback")
async def api_feedback_save(request: Request):
    """수기 매핑 피드백 저장. AI 제안과 다른 상품으로 매핑했을 때 기록."""
    _require_pm_access(request)
    data = await request.json()
    product_key = data.get("product_key", "").strip()
    if not product_key:
        return JSONResponse({"ok": False, "error": "product_key 필요"})

    _feedback_cache[product_key] = {
        "our_product_code": data.get("our_product_code", ""),
        "product_name":     data.get("product_name", ""),
        "category":         data.get("category", ""),
        "unit":             data.get("unit", ""),
        "our_sale_price":   data.get("our_sale_price"),
        "buy_price":        data.get("buy_price"),
        "ai_suggested_code": data.get("ai_suggested_code", ""),
        "ai_score":         data.get("ai_score"),
        "platform_name":    data.get("platform_name", ""),
        "created_at":       __import__('datetime').datetime.now().isoformat(),
    }
    _save_feedback()
    logger.info(f"[feedback] 저장 product_key={product_key} → {_feedback_cache[product_key]['our_product_code']}")
    return JSONResponse({"ok": True, "total": len(_feedback_cache)})


@router.get("/api/pm-ai/feedback")
async def api_feedback_list(request: Request):
    """저장된 피드백 목록 조회."""
    _require_pm_access(request)
    return JSONResponse({"data": list(_feedback_cache.values()), "total": len(_feedback_cache)})


@router.delete("/api/pm-ai/feedback/{product_key}")
async def api_feedback_delete(request: Request, product_key: str):
    """특정 피드백 삭제."""
    _require_pm_access(request)
    removed = _feedback_cache.pop(product_key, None)
    if removed:
        _save_feedback()
    return JSONResponse({"ok": bool(removed)})


# ── Databricks 웨어하우스 웜업 (분석 전 연결 확인) ──────────────────────────────
# 웨어하우스가 꺼진 상태에서 분석을 바로 시작하면 8분 타임아웃도 부족할 수 있음.
# 분석 전 SELECT 1로 웨어하우스를 먼저 깨우고, 완료 후 분석 시작.
_warmup_store: dict = {}  # warmup_id → {status, error}


@router.get("/api/pm-ai/warmup")
async def api_warmup_start(request: Request):
    """Databricks 웨어하우스 웜업 시작. 즉시 warmup_id 반환 → /warmup/poll/{id}로 폴링."""
    _require_pm_access(request)
    import uuid as _uuid
    wid = _uuid.uuid4().hex
    _warmup_store[wid] = {"status": "running"}

    # 오래된 웜업 잡 정리
    done = [k for k, v in list(_warmup_store.items())
            if v.get("status") != "running" and k != wid]
    for k in done[:-10]:
        _warmup_store.pop(k, None)

    def _do_warmup():
        try:
            _q("SELECT 1 AS ping")   # 웨어하우스 기동 대기
            logger.info(f"[pm-warmup] Databricks ping OK, 캐시 채우기 시작 wid={wid}")
            # 웜업 성공 시 모든 캐시 병렬로 채우기
            import concurrent.futures as _cf_w
            futs = []
            with _cf_w.ThreadPoolExecutor(max_workers=10) as _pex:
                for _plant in PLANTS:
                    futs.append(_pex.submit(_get_our_products, _plant))
                    futs.append(_pex.submit(_get_base_prices, _plant))
                    futs.append(_pex.submit(_get_prev_month_sales_totals, _plant))
                # 주요 셀러 plat_rows도 미리 캐시
                for _pf in ('baemin', 'foodspring'):
                    futs.append(_pex.submit(_get_plat_rows, _pf, '__ALL__'))
            # 결과 수집 (예외 무시 — 일부 실패해도 warmup 완료로 처리)
            for _f in futs:
                try: _f.result(timeout=300)
                except Exception: pass
            _warmup_store[wid]["status"] = "done"
            logger.info(f"[pm-warmup] 완료 wid={wid}")
        except Exception as _e:
            import traceback as _tb
            _warmup_store[wid].update({"status": "error", "error": str(_e),
                                        "traceback": _tb.format_exc()[-1000:]})
            logger.warning(f"[pm-warmup] 실패: {_e}")

    threading.Thread(target=_do_warmup, daemon=True).start()
    return JSONResponse({"warmup_id": wid, "status": "running"})


@router.get("/api/pm-ai/warmup/poll/{warmup_id}")
async def api_warmup_poll(request: Request, warmup_id: str):
    _require_pm_access(request)
    job = _warmup_store.get(warmup_id)
    if job is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(job)


@router.get("/api/pm-ai/suggest")
async def api_mapping_ai_suggest(
    request: Request,
    platform: str = "",
    seller_name: str = "",
    seller_id: str = "",
    plant: str = "ALL",
    limit: int = 10000,
):
    _require_pm_access(request)
    import uuid as _uuid
    job_id = _uuid.uuid4().hex
    _job_store[job_id] = {"status": "running", "progress": 0, "total_unmapped": 0, "items": []}

    # 완료된 오래된 잡 정리 (최대 30개 유지)
    done = [k for k, v in list(_job_store.items()) if v.get("status") in ("done", "error") and k != job_id]
    for k in done[:-30]:
        _job_store.pop(k, None)

    def _bg():
        import json as _json, concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            _fut = _ex.submit(_do_ai_suggest, None, platform, seller_name, plant, limit, job_id)
            try:
                result = _fut.result(timeout=1800)  # 30분 — 1844건 스코어링 완주 허용
                data = _json.loads(result.body)
                # _do_ai_suggest가 예외를 던지지 않고 {"items": [], "error": ...}로
                # 실패를 반환하는 경우가 있음(플랫폼/셀러 누락, DB 조회 실패 등).
                # 이 경우를 "done"으로 잘못 표시하면 프런트가 "미매핑 상품 없음"으로
                # 오인 표시하므로, error 키가 있으면 반드시 status=error로 표시한다.
                if data.get("error"):
                    data["status"] = "error"
                else:
                    data["status"] = "done"
                _job_store[job_id].update(data)
            except _cf.TimeoutError:
                # 중간 결과가 있으면 done으로 반환, 없으면 에러
                _partial = _job_store[job_id].get("_partial_items", [])
                _ptotal  = _job_store[job_id].get("_partial_total", 0)
                if _partial:
                    _partial.sort(
                        key=lambda x: x["suggestions"][0]["score"] if x["suggestions"] else -1,
                        reverse=True
                    )
                    _job_store[job_id].update({
                        "status":        "done",
                        "items":         _partial[:10000],
                        "total_unmapped": _ptotal,
                        "warning":       f"⏱ 시간 제한으로 {len(_partial)}건만 분석됨 (전체 {_ptotal}건 중) — 다시 분석 시 전체 결과 확인 가능"
                    })
                else:
                    _job_store[job_id].update({
                        "status": "error",
                        "error":  "분석 제한시간(8분) 초과 — Databricks 웨어하우스가 응답하지 않습니다. 잠시 후 다시 시도해 주세요.",
                        "traceback": ""
                    })
            except Exception as _e:
                import traceback as _tb
                _job_store[job_id].update({"status": "error", "error": str(_e),
                                            "traceback": _tb.format_exc()[-2000:]})

    threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({"job_id": job_id, "status": "running"})


def _do_ai_suggest(request, platform, seller_name, plant, limit, _job_id=None):
    _t0 = time.time()
    def _log(msg): logger.info(f"[pm-ai][{_job_id}] {msg} ({time.time()-_t0:.1f}s)")

    if not platform or not seller_name:
        return JSONResponse({"items": [], "error": "platform/seller_name 필요"})

    all_sellers = (seller_name == "__ALL__")
    safe_seller = seller_name.replace("'", "''")
    _log(f"시작 platform={platform} seller={seller_name} plant={plant}")

    # 플랫폼 SKU 조회 — 캐시 우선 (_get_plat_rows 10분 캐시)
    try:
        plat_rows = _get_plat_rows(platform, seller_name)
    except Exception as e:
        return JSONResponse({"items": [], "error": str(e)})

    # ── DB 콜 4개 병렬 실행 (순차 합산 → 가장 느린 1개 시간만 소요) ────────
    _log(f"플랫폼 상품 조회 완료 {len(plat_rows)}개 → DB 병렬 조회 시작")
    import concurrent.futures as _cf2
    with _cf2.ThreadPoolExecutor(max_workers=4) as _pex:
        _f_mappings     = _pex.submit(portal_db.pm_list_all_mappings, plant)
        _f_our_products = _pex.submit(_get_our_products, plant)
        _f_price_totals = _pex.submit(_get_prev_month_sales_totals, plant)
        _f_base_prices  = _pex.submit(_get_base_prices, plant)
        try:
            all_mappings  = _f_mappings.result(timeout=360)     or []
            our_products  = _f_our_products.result(timeout=360) or []
            price_totals  = _f_price_totals.result(timeout=360) or {}
            _bp_rows      = _f_base_prices.result(timeout=360)  or []
        except _cf2.TimeoutError:
            return JSONResponse({"items": [], "error": "DB 조회 제한시간(6분) 초과 — Databricks 웨어하우스 응답 지연"})
    base_prices = {r["product_code"]: r for r in _bp_rows}
    _log(f"DB 병렬 조회 완료 매핑={len(all_mappings)}건 우리상품={len(our_products)}개")

    # 이미 매핑된 product_key 집합
    mapped_keys = {m["product_key"] for m in all_mappings if m.get("is_active", 1)}

    # 미매핑만 필터
    unmapped = [r for r in plat_rows if r["product_key"] not in mapped_keys]
    if _job_id and _job_id in _job_store:
        _job_store[_job_id]["total_unmapped"] = len(unmapped)
    _log(f"미매핑={len(unmapped)}건 → 스코어링 시작")
    # 매출데이터 있는 상품 우선, 없으면 전체
    # 전체셀러(__ALL__) 분석은 연산량이 크므로 상품 수 제한
    our_with_sales = [p for p in our_products if p["product_code"] in base_prices]
    our_no_sales   = [p for p in our_products if p["product_code"] not in base_prices]
    # 사전 토큰 필터(하단)가 실제 스코어링을 ~95% 감소시키므로 전체 상품 스캔
    # (구: 3000개 임의 절삭 → 매출데이터 없는 상품이 통째로 누락되는 문제 해결)
    our_scan = our_with_sales + our_no_sales

    # ── [N] 화성3배치 중복 제거 ──────────────────────────────────────────
    # [N]xxx 상품이 있고 동일 기본명(xxx)의 non-[N] 상품이 our_scan에 존재하면
    # → non-[N] 상품을 우선, [N] 상품은 스캔에서 제외
    # (base_prices 유무 조건 제거: 최근 2주 판매 없어도 상품 자체가 있으면 대체)
    import re as _re_dedup
    _non_n_names: set[str] = set()   # 정규화 기본명 집합 (non-[N] 상품)
    for _p in our_scan:
        _pname = (_p.get("product_name") or "").strip()
        if not _pname.startswith("[N]"):
            _norm = _re_dedup.sub(r'\s+', '', _pname.lower())
            _non_n_names.add(_norm)
    _n_exclude: set[str] = set()     # 제외할 [N] 상품 코드
    for _p in our_scan:
        _pname = (_p.get("product_name") or "").strip()
        if _pname.startswith("[N]"):
            _base = _re_dedup.sub(r'\s+', '', _pname[3:].strip().lower())
            if _base in _non_n_names:
                _n_exclude.add(_p["product_code"])
    if _n_exclude:
        our_scan = [p for p in our_scan if p["product_code"] not in _n_exclude]
    # ─────────────────────────────────────────────────────────────────────

    # 기존 매핑 패턴
    pattern_strength = _build_pattern_map(all_mappings, our_scan) or {}

    # ── 역인덱스: 모듈 레벨 캐시(_inv_cache)에서 가져오기 ───────────────────
    # our_scan이 같으면(=product_cache TTL 내) 재구축 없이 재사용
    # → 매 요청마다 41,879번 tokenize하던 병목 완전 제거
    _inv_key = f"inv_{plant}"
    _inv_entry = _inv_cache.get(_inv_key)
    _prod_cache_entry = _product_cache.get(f"products_{plant}")
    _prod_cache_ts = _prod_cache_entry[0] if _prod_cache_entry else 0
    if _inv_entry is None or _inv_entry[0] < _prod_cache_ts:
        # 상품 캐시가 갱신됐거나 inv 캐시 없을 때만 재구축
        from collections import defaultdict as _ddict
        _tok_inv_new: dict = _ddict(set)
        _prod_map_new: dict = {}
        for _p in our_scan:
            _c = _p["product_code"]
            _prod_map_new[_c] = _p
            for _tok in _tokenize(_p.get("product_name") or ""):
                _tok_inv_new[_tok].add(_c)
        _inv_cache[_inv_key] = (time.time(), dict(_tok_inv_new), _prod_map_new)
        logger.info(f"[pm-ai] inv_cache 재구축 plant={plant} 상품={len(_prod_map_new):,}개")
    _tok_inv     = _inv_cache[_inv_key][1]
    _our_prod_map = _inv_cache[_inv_key][2]

    items = []
    scan_count = min(len(unmapped), limit)  # limit개만 스캔
    _score_lock = threading.Lock()
    _progress_counter = [0]  # 스레드 안전 카운터 (list로 mutable 참조)
    _score_start = time.time()  # 스코어링 시작 시각

    def _score_one(row):
        """row 1개 스코어링 — 피드백 캐시 우선, 없으면 유사도 계산."""
        pkey  = row.get("product_key", "")
        sname = row.get("platform_seller_name") or ("" if all_sellers else seller_name)
        p_price = row.get("price_sale")
        d_type  = row.get("delivery_type") or "직배송"
        fee     = _get_fee(d_type, platform, sname)
        net_price = round(float(p_price) * (1.0 - fee) / 1.1, 0) if p_price else None
        raw_pname   = row.get("product_name", "")
        clean_pname = _strip_seller(raw_pname, sname)

        # ── 피드백 캐시 우선: 수기 매핑 정답이 있으면 #1으로 반환 ──
        if pkey and pkey in _feedback_cache:
            fb = _feedback_cache[pkey]
            top3 = [{
                "our_product_code": fb["our_product_code"],
                "product_name":     fb["product_name"],
                "category":         fb.get("category", ""),
                "unit":             fb.get("unit", ""),
                "score":            100.0,  # 수기 매핑 = 신뢰도 100
                "our_sale_price":   fb.get("our_sale_price"),
                "buy_price":        fb.get("buy_price"),
                "net_comp_price":   int(net_price) if net_price else None,
                "prev_sales_amt":   0,
                "_feedback": True,  # 피드백 출처 표시
            }]
            return {
                "product_key":    pkey,
                "product_name":   raw_pname,
                "display_name":   clean_pname,
                "spec":           row.get("spec", ""),
                "price_sale":     int(p_price) if p_price else None,
                "net_price":      int(net_price) if net_price else None,
                "delivery_type":  d_type,
                "fee_pct":        round(fee * 100, 1),
                "seller_name":    sname,
                "suggestions":    top3,
                "has_suggestions": True,
                "from_feedback":  True,
            }

        plat_toks = _tokenize(clean_pname)
        candidate_codes: set = set()
        for _t in plat_toks:
            candidate_codes |= _tok_inv.get(_t, set())
        # 후보 폭발 방지: 역인덱스 히트 너무 많으면 상위 200개로 제한
        if len(candidate_codes) > 200:
            candidate_codes = set(list(candidate_codes)[:200])
        scored = []
        for code in candidate_codes:
            p = _our_prod_map.get(code)
            if p is None:
                continue
            bp       = base_prices.get(code, {})
            our_sale = bp.get("avg_sale_price")
            buy_p    = bp.get("avg_buy_price")
            pat_bonus = min(1.0, pattern_strength.get(code, 0) / 3.0)
            sc = _score_mapping(
                clean_pname, p_price,
                p.get("product_name", ""), our_sale, buy_p,
                d_type, platform, sname, pat_bonus,
                our_prod=p
            )
            if sc >= 20.0:
                our_name = p.get("product_name", code)
                is_n_prefix = our_name.lstrip().startswith('[N]')
                if is_n_prefix:
                    sc = max(0.0, sc - 5.0)
                scored.append({
                    "our_product_code": code,
                    "product_name":     our_name,
                    "category":         p.get("category", ""),
                    "unit":             p.get("unit", ""),
                    "score":            sc,
                    "our_sale_price":   int(our_sale) if our_sale else None,
                    "buy_price":        int(buy_p)    if buy_p    else None,
                    "net_comp_price":   int(net_price) if net_price else None,
                    "prev_sales_amt":   int(price_totals.get(code, {}).get("prev_sales_amt") or 0),
                    "_is_n":            is_n_prefix,
                })
        scored.sort(key=lambda x: (-x["score"], x["_is_n"]))
        for s in scored:
            s.pop("_is_n", None)
        top3 = scored[:3]

        return {
            "product_key":    row["product_key"],
            "product_name":   raw_pname,
            "display_name":   clean_pname,
            "spec":           row.get("spec", ""),
            "price_sale":     int(p_price) if p_price else None,
            "net_price":      int(net_price) if net_price else None,
            "delivery_type":  d_type,
            "fee_pct":        round(fee * 100, 1),
            "seller_name":    sname,
            "suggestions":    top3,
            "has_suggestions": len(top3) > 0,
        }

    try:
        # GIL로 인해 ThreadPoolExecutor는 순수 Python 연산에 효과 없음 → 순차 루프
        # candidate_codes 상한 200개로 제한 (역인덱스 히트 폭발 방지)
        for _row_idx in range(scan_count):
            row = unmapped[_row_idx]
            result_row = _score_one(row)
            items.append(result_row)
            _progress_counter[0] = _row_idx + 1
            done_cnt = _row_idx + 1
            if _job_id and _job_id in _job_store:
                _job_store[_job_id]["progress"] = done_cnt
            # 50개마다 중간 결과 저장 + 속도 측정
            if done_cnt % 50 == 0 and _job_id and _job_id in _job_store:
                _elapsed = time.time() - _score_start
                _ips = done_cnt / _elapsed if _elapsed > 0 else 0
                _eta = (scan_count - done_cnt) / _ips if _ips > 0 else 0
                _job_store[_job_id]["_partial_items"] = list(items)
                _job_store[_job_id]["_partial_total"] = len(unmapped)
                _job_store[_job_id]["elapsed_sec"] = round(_elapsed, 1)
                _job_store[_job_id]["items_per_sec"] = round(_ips, 2)
                _job_store[_job_id]["eta_sec"] = round(_eta)
                logger.info(f"[pm-ai][{_job_id}] scoring {done_cnt}/{scan_count} — {_ips:.1f}건/초, ETA {_eta:.0f}초")
    except Exception as e:
        import traceback as _tb
        return JSONResponse({"items": items, "total_unmapped": len(unmapped),
                             "error": str(e), "traceback": _tb.format_exc()[-3000:]})

    # 신뢰도 높은 순 정렬 (제안 없는 항목은 뒤로)
    items.sort(key=lambda x: x["suggestions"][0]["score"] if x["suggestions"] else -1, reverse=True)
    return JSONResponse({"items": items[:limit], "total_unmapped": len(unmapped)})


# ── AI 코드매핑 데모 (전체 계정 공용) ────────────────────────────────────────
# 영업담당자가 상품명 1건을 입력하면 매핑워크스페이스와 동일한 채점 로직으로
# 상위 3건의 제안(신뢰도 점수 포함)을 즉시 반환하는 셀프서비스 데모 화면.
# 결과에 대해 👍/👎 피드백을 남길 수 있고, 관리자는 별도 메뉴에서 피드백을 조회한다.

def _confidence_label(score: float) -> str:
    if score >= 70:
        return "높음"
    if score >= 40:
        return "보통"
    return "낮음"


@router.get("/ai-demo", response_class=HTMLResponse)
async def pm_ai_demo_page(request: Request):
    _require_login(request)
    return _render(request, "pm_ai_demo.html")


@router.post("/api/ai-demo/suggest")
async def api_ai_demo_suggest(request: Request):
    """상품명(+선택 입력) 1건 → 상위 3건 매핑 제안 (동기 처리, 1회 1건).
    입력: product_name(필수), spec(선택, 규격/중량 등 — 매칭 정확도 향상),
          category(선택, 대분류 — 검색 참고용), plant(선택, 기본 ALL)
    """
    session = _require_login(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    product_name = (body.get("product_name") or "").strip()
    spec         = (body.get("spec") or "").strip()
    category     = (body.get("category") or "").strip()
    plant        = body.get("plant") or "ALL"
    if plant not in PLANTS:
        plant = "ALL"
    if not product_name:
        raise HTTPException(status_code=400, detail="상품명을 입력해주세요.")

    combined_input = " ".join(filter(None, [product_name, spec]))

    our_products = _get_our_products(plant)
    base_prices  = {r["product_code"]: r for r in _get_base_prices(plant)}

    # 대분류가 입력되면 우선 해당 카테고리 내에서만 채점 (결과 없으면 전체로 폴백)
    candidates = our_products
    if category:
        cat_low = category.lower()
        narrowed = [p for p in our_products if cat_low in (p.get("category") or "").lower()]
        if narrowed:
            candidates = narrowed

    scored = []
    for p in candidates:
        code = p.get("product_code")
        bp = base_prices.get(code, {})
        try:
            s = _score_mapping(
                combined_input, None,
                p.get("product_name", ""), bp.get("avg_sale_price"), bp.get("avg_buy_price"),
                "직배송", "", "", 0.0, p,
            )
        except Exception:
            s = 0.0
        if s > 0:
            scored.append({
                "product_code": code,
                "product_name": p.get("product_name"),
                "brand":        p.get("brand"),
                "category":     p.get("category"),
                "unit":         p.get("unit"),
                "score":        s,
                "confidence":   _confidence_label(s),
            })
    scored.sort(key=lambda x: -x["score"])
    top3 = scored[:3]

    return JSONResponse({
        "input": {"product_name": product_name, "spec": spec, "category": category, "plant": plant},
        "suggestions": top3,
        "candidate_count": len(candidates),
        "emp_name": session.get("name", ""),
    })


@router.post("/api/ai-demo/feedback")
async def api_ai_demo_feedback_save(request: Request):
    """AI 코드매핑 데모 결과에 대한 사용자 피드백 저장 (👍/👎)."""
    session = _require_login(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    rating = (body.get("rating") or "").strip()
    if rating not in ("up", "down", ""):
        raise HTTPException(status_code=400, detail="rating 값이 올바르지 않습니다.")
    feedback_id = portal_db.pm_save_ai_demo_feedback(
        emp_code=session.get("emp_code", ""),
        emp_name=session.get("name", ""),
        team=session.get("team", ""),
        input_product_name=(body.get("product_name") or "").strip(),
        input_spec=(body.get("spec") or "").strip(),
        input_category=(body.get("category") or "").strip(),
        suggestions=body.get("suggestions") or [],
        rating=rating,
        comment=(body.get("comment") or "").strip(),
        correct_product_code=(body.get("correct_product_code") or "").strip(),
        correct_product_name=(body.get("correct_product_name") or "").strip(),
    )
    return JSONResponse({"ok": True, "feedback_id": feedback_id})


@router.get("/admin/ai-demo-feedback", response_class=HTMLResponse)
async def pm_admin_ai_demo_feedback_page(request: Request):
    """AI 코드매핑 데모 피드백 조회 (관리자 전용)."""
    _require_admin(request)
    return _render(request, "pm_ai_demo_admin.html")


@router.get("/api/ai-demo/feedback-list")
async def api_ai_demo_feedback_list(request: Request, rating: str = ""):
    """관리자용 피드백 목록 + 집계 (JSON)."""
    _require_admin(request)
    rating = rating.strip() or None
    if rating not in (None, "up", "down"):
        rating = None
    rows  = portal_db.pm_list_ai_demo_feedback(rating=rating)
    stats = portal_db.pm_ai_demo_feedback_stats()
    return JSONResponse({"items": _serialize_rows(rows), "stats": stats})


# ── API: 유사 플랫폼 상품 조회 (팝업용, 전체 셀러) ─────────────────────────

@router.get("/api/pm-ai/similar")
async def api_mapping_similar_platform(
    request: Request,
    product_name: str = "",
    platform: str = "",
    exclude_key: str = "",
):
    """특정 플랫폼 상품명과 유사한 전체 셀러 플랫폼 상품 반환 (매핑 팝업용).
    - 동일 product_name 포함 상품을 전 셀러에서 조회
    - 이미 매핑된 product_key는 is_mapped=True 표시
    """
    _require_pm_access(request)
    if not product_name:
        return JSONResponse({"data": []})

    import re as _re

    # ── 와일드카드 패턴 감지 (*오뚜기*1.8* 형식) ──────────────────────────
    _is_wildcard = '*' in product_name

    def _escape_sql(s):
        return s.replace("'", "''")

    def _build_like_clause(toks, operator="OR"):
        parts = [f"p.product_name LIKE '%{_escape_sql(t)}%'" for t in toks]
        return f" {operator} ".join(parts) if parts else "1=1"

    def _wildcard_to_like(pattern: str) -> str:
        """*오뚜기*1.8* → %오뚜기%1.8% (SQL LIKE 패턴)"""
        parts = [_escape_sql(p) for p in pattern.split('*')]
        return '%' + '%'.join(parts) + '%'

    def _word_tokens(name: str):
        """공백 분리 단어만 (bi/tri-gram 제외). 한글 2자+, 영문/숫자 2자+ 허용."""
        name = _re.sub(r'[/·•\-_,\(\)\[\]{}]', ' ', name or '')
        parts = []
        for t in _re.split(r'\s+', name):
            t = t.strip()
            if not t:
                continue
            # 숫자+단위(1.8L, 500g 등)는 검색 노이즈 → 제외
            if _re.fullmatch(r'[\d]+[\d.,]*[a-zA-Z]*', t):
                continue
            if len(t) >= 2:
                parts.append(t.lower())
        return parts

    def _name_similarity(q: str, target: str) -> float:
        """두 상품명 간 토큰 유사도(0~100). Overlap Coefficient 기반."""
        qt = _tokenize(q)
        tt = _tokenize(target)
        if not qt or not tt:
            return 0.0
        overlap = len(qt & tt) / min(len(qt), len(tt))
        # 단어 단위 직접 포함 보너스: 긴 단어일수록 신뢰
        for w in _word_tokens(q):
            if len(w) >= 3 and w in (target or '').lower():
                overlap = min(1.0, overlap + 0.15)
        return round(overlap * 100.0, 1)

    # 플랫폼 필터 옵션
    plat_filter = f"AND p.platform = '{platform}'" if platform else ""

    def _run_query(like_clause):
        max_rows = _q(f"SELECT platform, MAX(crawl_date) AS max_date FROM {T_SILVER} GROUP BY platform") or []
        if not max_rows:
            return []
        date_clauses = " OR ".join(
            f"(p.platform='{r['platform']}' AND p.crawl_date='{r['max_date']}')"
            for r in max_rows if r.get("platform") and r.get("max_date")
        )
        return _q(f"""
            SELECT p.product_key, p.platform, p.platform_seller_name,
                   p.product_name, p.spec, p.price_sale, p.delivery_type, p.is_free_delivery
            FROM {T_SILVER} p
            WHERE ({date_clauses})
              AND ({like_clause})
              {plat_filter}
            ORDER BY p.platform, p.platform_seller_name, p.price_sale
            LIMIT 300
        """) or []

    try:
        if _is_wildcard:
            # 와일드카드 모드: *오뚜기*1.8* → p.product_name LIKE '%오뚜기%1.8%'
            like_pattern = _wildcard_to_like(product_name)
            rows = _run_query(f"p.product_name LIKE '{like_pattern}'")
        else:
            # 일반 모드: 단어 토큰 OR 조건으로 넓게 조회
            word_toks = _word_tokens(product_name)
            if not word_toks:
                return JSONResponse({"data": []})
            like_or = _build_like_clause(word_toks, "OR")
            rows = _run_query(like_or)
            # fallback: 결과 없으면 가장 긴 단어 1개로 재시도
            if not rows and word_toks:
                longest = sorted(word_toks, key=len, reverse=True)[0]
                rows = _run_query(f"p.product_name LIKE '%{_escape_sql(longest)}%'")
    except Exception as e:
        return JSONResponse({"data": [], "error": str(e)})

    # 매핑 여부 표시
    all_mappings = portal_db.pm_list_all_mappings("ALL")
    mapped_keys = {m["product_key"] for m in all_mappings}

    result = []
    for r in _serialize_rows(rows):
        p_price   = r.get("price_sale")
        d_type    = r.get("delivery_type") or "직배송"
        _sn       = r.get("platform_seller_name", "")
        _pf       = r.get("platform", "")
        fee       = _get_fee(d_type, _pf, _sn)
        net_price = round(float(p_price) * (1.0 - fee) / 1.1, 0) if p_price else None
        r["net_price"]    = int(net_price) if net_price else None
        r["fee_pct"]      = round(fee * 100, 1)
        r["is_mapped"]    = r["product_key"] in mapped_keys
        r["is_excluded"]  = (r["product_key"] == exclude_key)
        # 셀러명 제거 후 정제 이름으로 유사도 계산
        clean_q      = _strip_seller(product_name, _sn)
        clean_target = _strip_seller(r.get("product_name", ""), _sn)
        r["display_name"]    = clean_target
        r["similarity_score"] = _name_similarity(clean_q, clean_target)
        result.append(r)

    # 유사도 높은 순 정렬
    result.sort(key=lambda x: x["similarity_score"], reverse=True)

    return JSONResponse({"data": result})


# ── API: 일괄 매핑 추가 (팝업 확정) ─────────────────────────────────────────

@router.post("/api/mapping/bulk-add")
async def api_mapping_bulk_add(request: Request):
    """팝업에서 선택한 여러 플랫폼 상품을 한 번에 매핑 등록.

    body: {
      our_product_code: str,
      plant: str,
      tag: str,          -- 'normal' | 'substitute' | 'multiple'
      multiplier: float, -- 배수 (직접 입력)
      items: [{
        product_key, platform, platform_seller_id, platform_product_id,
        product_name, seller_name
      }]
    }
    """
    _require_pm_access(request)
    session = _get_session(request)
    body = await request.json()

    our_product_code = body.get("our_product_code", "").strip()
    plant            = body.get("plant", "ALL").strip()
    tag              = body.get("tag", "normal")
    try:
        multiplier = float(body.get("multiplier", 1.0))
        if multiplier <= 0:
            multiplier = 1.0
    except (TypeError, ValueError):
        multiplier = 1.0
    items = body.get("items", [])

    if not our_product_code or not items:
        return JSONResponse({"ok": False, "error": "필수값 누락"}, status_code=400)

    results = []
    for item in items:
        pk = (item.get("product_key") or "").strip()
        pf = (item.get("platform") or "").strip()
        if not pk or not pf:
            continue
        try:
            mid = portal_db.pm_add_mapping(
                our_product_code=our_product_code,
                plant=plant,
                product_key=pk,
                platform=pf,
                platform_seller_id=str(item.get("platform_seller_id") or ""),
                platform_product_id=str(item.get("platform_product_id") or ""),
                product_name=item.get("product_name", ""),
                seller_name=item.get("seller_name", ""),
                created_by=session.get("emp_code", ""),
                tag=tag,
                multiplier=multiplier,
            )
            results.append({"product_key": pk, "mapping_id": mid, "ok": True})
        except Exception as e:
            results.append({"product_key": pk, "ok": False, "error": str(e)})

    ok_count = sum(1 for r in results if r["ok"])
    return JSONResponse({"ok": True, "added": ok_count, "results": results})


# ── POC 매칭 벤치마크 ────────────────────────────────────────────────────────
# GET /portal/price-monitor/api/poc-benchmark?n=100&seed=42&threshold=20
# 인증 불필요 (debug-pub과 동일하게 공개 엔드포인트로 운영)
@router.get("/api/poc-ping")
def poc_ping():
    return JSONResponse({"ok": True, "msg": "poc ping works"})


@router.get("/api/poc-result")
def poc_result(limit: int = 0, force_file: int = 0):
    """poc-benchmark 완료 후 저장된 결과 조회 (인메모리 우선, /tmp 백업)
    limit: 반환할 details 건수 (0=전체, 기본값)
    force_file: 1이면 인메모리 무시하고 /tmp 파일에서 읽기 (멀티인스턴스 환경에서 사용)
    """
    global _POC_LATEST
    import json as _json, os as _os
    def _slice(items: list) -> list:
        return items[:limit] if limit > 0 else items
    # 1) 인메모리 결과 우선 (force_file=0일 때만)
    if _POC_LATEST is not None and not force_file:
        all_details = _POC_LATEST.get("details", [])
        return JSONResponse({
            "meta":    _POC_LATEST.get("meta"),
            "summary": _POC_LATEST.get("summary"),
            "details": _slice(all_details),
            "total_details": len(all_details),
            "source": "memory",
        })
    # 2) /tmp 파일 폴백
    path = "/tmp/poc_latest.json"
    err_path = "/tmp/poc_async_error.txt"
    if not _os.path.exists(path):
        err = ""
        if _os.path.exists(err_path):
            with open(err_path) as _ef:
                err = _ef.read()
        return JSONResponse({"error": "결과 없음. /api/poc-benchmark?save=1 먼저 실행",
                             "async_error": err or None})
    with open(path, encoding="utf-8") as f:
        data = _json.load(f)
    all_details = data.get("details", [])
    return JSONResponse({
        "meta":    data.get("meta"),
        "summary": data.get("summary"),
        "details": _slice(all_details),
        "total_details": len(all_details),
        "source": "file",
    })


@router.get("/api/poc-run-async")
def poc_run_async(n: int = 100, seed: int = 42, threshold: float = 20.0):
    """백그라운드 스레드로 poc-benchmark 직접 실행 (localhost HTTP 호출 없음)."""
    import threading as _threading
    import asyncio as _asyncio
    def _run():
        _loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(_loop)
        try:
            resp = _loop.run_until_complete(poc_benchmark(n=n, seed=seed, threshold=threshold, save=1))
            # JSONResponse 내부 오류 캡처
            if hasattr(resp, 'body'):
                import json as _json
                _body = _json.loads(resp.body)
                if 'error' in _body:
                    with open("/tmp/poc_async_error.txt", "w") as _ef:
                        _ef.write(f"poc_benchmark returned error:\n{_json.dumps(_body, ensure_ascii=False, indent=2)}")
        except Exception as _e:
            import traceback as _tb
            with open("/tmp/poc_async_error.txt", "w") as _ef:
                _ef.write(str(_e) + "\n" + _tb.format_exc())
        finally:
            _loop.close()
    _threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"status": "started", "n": n, "seed": seed,
                         "note": "2~3분 후 /api/poc-result 로 결과 조회"})


@router.get("/api/poc-benchmark")
async def poc_benchmark(n: int = 100, seed: int = 42, threshold: float = 20.0,
                        save: int = 0):
    """
    현행(v1) vs 개선(v2) AI 매칭 알고리즘 POC 비교.
    save=1 이면 결과를 서버 파일(/tmp/poc_latest.json)에 저장하고 즉시 반환.
    /api/poc-result 엔드포인트로 결과 조회.
    """
    import time as _time
    import traceback as _tb
    import decimal as _decimal

    def _jsafe(v):
        """JSON 직렬화 안전 변환"""
        if isinstance(v, _decimal.Decimal):
            return float(v)
        if hasattr(v, 'isoformat'):   # date/datetime
            return v.isoformat()
        return v

    # ── v2 전용 개선 스코어링 ─────────────────────────────────────────────
    _STOP = {
        '슬라이스', '냉동', '냉장', '신선', '건조', '원물', '국산', '수입',
        '일반', '특대', '대용량', '소포장', '개별', '낱개', '원터치', '직배송',
        '무료배송', '당일배송', '묶음', '세트', '팩', '개입', '입점',
        '필리핀산', '국내산', '수입산', '베트남산', '미국산', '호주산',
        'new', 'ea',
    }
    def _score_v2(platform_name: str, our_name: str, our_prod: dict) -> float:
        # 온도조건 하드필터: 향후 코드 매핑 파악 후 활성화 예정
        # (현재 ZMM60의 온도조건 값이 코드값이라 "냉동" 텍스트 비교 불가)
        our_temp = (our_prod.get("temp_cond") or "").strip()
        plat_lower_tc = platform_name
        plat_frozen = "냉동" in plat_lower_tc
        plat_chilled = "냉장" in plat_lower_tc
        if our_temp and (plat_frozen or plat_chilled):
            _TEMP_FROZEN = {"30", "40"}  # 냉동/초냉동
            _TEMP_CHILLED = {"20"}       # 냉장
            our_is_frozen = our_temp in _TEMP_FROZEN
            our_is_chilled = our_temp in _TEMP_CHILLED
            if plat_frozen and not our_is_frozen:
                return 0.0  # 플랫폼=냉동, 우리=비냉동 → 불일치
            if plat_chilled and not our_is_chilled:
                return 0.0  # 플랫폼=냉장, 우리=비냉장 → 불일치
        # plat_frozen = "냉동" in (platform_name or "")
        # our_frozen  = "냉동" in our_temp
        # if plat_frozen and not our_frozen and our_temp:
        #     return 0.0
        # if not plat_frozen and our_frozen:
        #     return 0.0

        # 텍스트 + 키워드 (v1과 동일 로직)
        _unit_pat = __import__('re').compile(r'^\d[\d.]*[a-zA-Z]*$')
        pt = _tokenize(platform_name)
        ot = _tokenize(our_name)
        text_score = 0.0
        if pt and ot:
            common = pt & ot
            common_text = {t for t in common
                           if not _unit_pat.match(t) and t not in _STOP}
            if common_text:
                overlap = len(common_text) / min(len(pt), len(ot))
                text_score = overlap * 60.0
                short_r = sum(1 for t in common_text if len(t) <= 2) / max(len(common_text), 1)
                text_score -= short_r * 8.0
            text_score = max(0.0, text_score)

        import re as _re
        keyword_bonus = 0.0
        our_l  = (our_name or "").lower()
        plat_l = (platform_name or "").lower()
        for w in [t for t in pt if len(t) >= 3 and t not in _STOP and not _re.match(r'^[\d\.]+', t)]:
            if w in our_l:
                keyword_bonus = 15.0; break
        if not keyword_bonus:
            for w in [t for t in ot if len(t) >= 3 and t not in _STOP and not _re.match(r'^[\d\.]+', t)]:
                if w in plat_l:
                    keyword_bonus = 15.0; break

        if text_score + keyword_bonus < 10.0:
            return 0.0
        score = text_score + keyword_bonus

        # 용량: 총중량 직접 비교 우선, 없으면 파싱
        # ZMM60 총중량은 KG 단위 → g으로 변환 (*1000)
        # 페널티는 v1보다 완화 (-8pt): 단위 불확실성으로 인한 오패널티 방지
        try:
            our_wkg = float(our_prod.get("total_weight") or our_prod.get("net_weight") or 0) or None
            our_wg  = our_wkg * 1000 if our_wkg else None  # KG → g
        except (TypeError, ValueError):
            our_wg = None
        if our_wg and our_wg > 0:
            pv, pu = _parse_volume(platform_name)
            if pv and pu == 'g':    # g ↔ g 비교만 (ml는 단위 불일치로 스킵)
                rv = min(pv, our_wg) / max(pv, our_wg)
                if rv >= 0.9:
                    score += 15.0
                # 불일치해도 패널티 없음 (보너스만 적용)
        else:
            pv, pu = _parse_volume(platform_name)
            ov, ou = _parse_volume(our_name)
            if pv and ov and pu == ou:
                rv = min(pv, ov) / max(pv, ov)
                score += 15.0 if rv >= 0.9 else (-5.0 if rv >= 0.6 else -8.0)  # -20 → -8 완화

        # 카테고리 계층 보너스
        our_cat = " ".join(filter(None, [
            our_prod.get("category") or "",
            our_prod.get("mid_category") or "",
            our_prod.get("sub_category") or "",
            our_prod.get("product_group") or "",
        ]))
        if our_cat:
            cat_m = {t for t in (_tokenize(our_cat) & _tokenize(platform_name))
                     if len(t) >= 2 and t not in _STOP}
            score += min(len(cat_m) * 5, 15)

        score += 10.0  # 가격 중립
        return round(max(0.0, min(100.0, score)), 1)

    # ── 자사 상품 로드 (캐시 활용 — _get_our_products와 동일 전체 목록) ──────
    try:
      t0 = _time.time()
      # _get_our_products('ALL'): 이미 캐시된 전체 목록 사용 (LIMIT 100000)
      # POC 전용 필드 보정: cat1/cat2/cat3 → category/mid_category/sub_category
      _base_rows = _get_our_products('ALL')
      # 샘플링 없이 전체 자사 상품 사용 (정확도 극대화)
      # 실행 시간 제어: poc 호출 시 n 파라미터를 줄여서 조정 (n=30 권장)
      our_rows = []
      for _r in _base_rows:
          our_rows.append({
              "product_code":   _r.get("product_code", ""),
              "product_name":   _r.get("product_name", ""),
              "brand":          _r.get("brand", ""),
              "unit":           _r.get("unit", ""),
              "product_group":  _r.get("product_group", ""),
              "material_group": _r.get("material_group", ""),
              "cat1":           _r.get("category", ""),
              "cat2":           _r.get("mid_category", ""),
              "cat3":           _r.get("sub_category", ""),
              "category":       _r.get("category", ""),
              "mid_category":   _r.get("mid_category", ""),
              "sub_category":   _r.get("sub_category", ""),
              "total_weight":   _r.get("total_weight"),
              "net_weight":     _r.get("net_weight"),
              "temp_cond":      _r.get("temp_cond", ""),
          })
      logger.info(f"[poc] our_rows loaded from cache: {len(our_rows)}건")

      # ── 플랫폼 샘플 로드 ─────────────────────────────────────────────────────
      plat_rows = _q(f"""
        SELECT p.product_key, p.product_name, p.platform_seller_name AS seller_name,
               p.platform, p.price_sale AS price, p.delivery_type
        FROM {T_SILVER} p
        INNER JOIN (SELECT MAX(crawl_date) AS md FROM {T_SILVER}) l ON p.crawl_date = l.md
        WHERE p.product_name IS NOT NULL AND p.price_sale > 0
        ORDER BY RAND({seed})
        LIMIT {n}
      """)
    except Exception as _load_err:
        return JSONResponse({"error": f"데이터 로드 실패: {_load_err}",
                             "traceback": _tb.format_exc()}, status_code=200)  # 디버깅용 200

    if not our_rows or not plat_rows:
        return JSONResponse({"error": "데이터 로드 실패", "our_count": len(our_rows), "plat_count": len(plat_rows)})

    # ── 스코어링 ─────────────────────────────────────────────────────────────
    details = []
    try:
      for plat in plat_rows:
        pname  = plat.get("product_name", "")
        pprice = _jsafe(plat.get("price"))
        seller = plat.get("seller_name", "")

        best_v1 = {"score": 0.0, "code": "", "name": ""}
        best_v2 = {"score": 0.0, "code": "", "name": ""}

        for op in our_rows:
            oname = op.get("product_name") or op.get("product_code", "")
            s1 = _score_mapping(
                platform_name=pname, platform_price=pprice,
                our_name=oname, our_sale_price=None,
                buy_price=None, delivery_type=plat.get("delivery_type", "직배송"),
                platform=plat.get("platform", ""), seller_name=seller,
                pattern_bonus=0.0
            )
            if s1 > best_v1["score"]:
                best_v1 = {"score": s1, "code": op.get("product_code",""), "name": oname}
            s2 = _score_mapping(
                platform_name=pname, platform_price=pprice,
                our_name=oname, our_sale_price=None,
                buy_price=None, delivery_type=plat.get("delivery_type", "직배송"),
                platform=plat.get("platform", ""), seller_name=seller,
                pattern_bonus=0.0, our_prod=op
            )
            if s2 > best_v2["score"]:
                best_v2 = {"score": s2, "code": op.get("product_code",""), "name": oname,
                           "temp": op.get("temp_cond","")}

        # [v8 Phase3] v2 동점 다른제품 폴백
        if (best_v1["score"] > 0
                and best_v2.get("name") != best_v1.get("name")
                and abs(best_v2["score"] - best_v1["score"]) <= 0.5):
            best_v2 = {"score": best_v1["score"], "code": best_v1["code"],
                       "name": best_v1["name"], "temp": best_v2.get("temp", "")}
        # [v9-3] 저점수 하한 게이트:
        # 핵심어 겹침이 없는 상태(오매핑 의심)에서 30pt 미만이면 미매칭 처리
        # 사또밥→삼양라면(45pt→패널티 후 저점), 절단게→마늘쫑(27pt) 등 차단
        _LOW_SCORE_GATE = 30.0
        if best_v1["score"] < _LOW_SCORE_GATE:
            best_v1 = {"score": 0.0, "code": "", "name": ""}
        if best_v2["score"] < _LOW_SCORE_GATE:
            best_v2 = {"score": 0.0, "code": "", "name": "", "temp": ""}
        v1_ok = best_v1["score"] >= threshold
        v2_ok = best_v2["score"] >= threshold
        diff  = round(best_v2["score"] - best_v1["score"], 1)
        if not v1_ok and v2_ok:       trans = "신규매칭"
        elif v1_ok and not v2_ok:     trans = "매칭손실"
        elif v1_ok and v2_ok:
            trans = "점수상승" if diff > 5 else ("점수하락" if diff < -5 else "동일")
        else:                          trans = "미매칭유지"

        details.append({
            "plat_name":   pname[:80],
            "platform":    plat.get("platform",""),
            "seller":      seller[:30],
            "plat_price":  pprice,
            "v1_score":    best_v1["score"],
            "v1_name":     best_v1["name"][:60],
            "v1_code":     best_v1["code"],
            "v1_ok":       v1_ok,
            "v2_score":    best_v2["score"],
            "v2_name":     best_v2["name"][:60],
            "v2_code":     best_v2["code"],
            "v2_ok":       v2_ok,
            "diff":        diff,
            "transition":  trans,
            "v2_temp":     best_v2.get("temp",""),
        })

      # ── 통계 ───────────────────────────────────────────────────────────────
      total = len(details)
      v1_ok_n = sum(1 for d in details if d["v1_ok"])
      v2_ok_n = sum(1 for d in details if d["v2_ok"])
      v1_avg  = round(sum(d["v1_score"] for d in details) / max(total, 1), 1)
      v2_avg  = round(sum(d["v2_score"] for d in details) / max(total, 1), 1)
      v1_zero = sum(1 for d in details if d["v1_score"] == 0)
      v2_zero = sum(1 for d in details if d["v2_score"] == 0)
      trans_counts: dict = {}
      for d in details:
          trans_counts[d["transition"]] = trans_counts.get(d["transition"], 0) + 1

      def _bucket(scores):
          b = {"0": 0, "1-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
          for s in scores:
              if s == 0:      b["0"] += 1
              elif s <= 20:   b["1-20"] += 1
              elif s <= 40:   b["21-40"] += 1
              elif s <= 60:   b["41-60"] += 1
              elif s <= 80:   b["61-80"] += 1
              else:           b["81-100"] += 1
          return b

      elapsed = round(_time.time() - t0, 1)
      result_data = {
          "meta": {
              "sample_n": total, "our_products_n": len(our_rows),
              "threshold": threshold, "seed": seed, "elapsed_sec": elapsed,
          },
          "summary": {
              "v1_match_rate": f"{v1_ok_n}/{total} ({v1_ok_n/total*100:.1f}%)" if total else "0/0",
              "v2_match_rate": f"{v2_ok_n}/{total} ({v2_ok_n/total*100:.1f}%)" if total else "0/0",
              "match_delta":   f"{v2_ok_n - v1_ok_n:+d}건 ({(v2_ok_n-v1_ok_n)/total*100:+.1f}%p)" if total else "0",
              "v1_avg_score":  v1_avg,
              "v2_avg_score":  v2_avg,
              "score_delta":   round(v2_avg - v1_avg, 1),
              "v1_zero":       v1_zero,
              "v2_zero":       v2_zero,
              "transitions":   trans_counts,
              "v1_distribution": _bucket([d["v1_score"] for d in details]),
              "v2_distribution": _bucket([d["v2_score"] for d in details]),
          },
          "details": details,
      }
      if save:
          import json as _json
          global _POC_LATEST
          _POC_LATEST = result_data  # 인메모리 저장 (우선)
          try:
              with open("/tmp/poc_latest.json", "w", encoding="utf-8") as _f:
                  _json.dump(result_data, _f, ensure_ascii=False)
          except Exception:
              pass  # /tmp 저장 실패해도 메모리엔 있음
          return JSONResponse({"status": "saved", "elapsed_sec": elapsed,
                               "sample_n": total, "v1_match_rate": result_data["summary"]["v1_match_rate"],
                               "v2_match_rate": result_data["summary"]["v2_match_rate"],
                               "score_delta": result_data["summary"]["score_delta"]})
      return JSONResponse(result_data)
    except Exception as _score_err:
        return JSONResponse({
            "error": f"스코어링 실패: {_score_err}",
            "partial_details": len(details),
            "traceback": _tb.format_exc(),
        }, status_code=200)  # 200으로 반환해 body 확인 가능하게


@router.get("/api/poc-diag")
def poc_diag():
    """POC 진단: 각 단계별 에러 확인"""
    import traceback as _tb
    result = {}
    # step1: 자사 상품 쿼리
    try:
        rows = _q(f"""
            SELECT z.`상품코드` AS product_code,
                   COALESCE(MAX(m.`상품명`), z.`상품코드`) AS product_name,
                   MAX(m.`총중량`) AS total_weight,
                   MAX(m.`온도조건`) AS temp_cond,
                   MAX(m.`중분류`) AS cat2
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`플랜트` IN ('4120')
              AND z.`배치` = '01'
              AND COALESCE(m.`자재그룹`, '') != '5140'
            GROUP BY z.`상품코드`
            LIMIT 5
        """)
        result["step1_our_rows"] = "OK"
        result["step1_count"] = len(rows)
        result["step1_sample"] = [str(r) for r in rows[:2]]
    except Exception as e:
        result["step1_our_rows"] = f"FAIL: {e}"
        result["step1_tb"] = _tb.format_exc()
        return JSONResponse(result)
    # step2: 플랫폼 샘플
    try:
        plat = _q(f"""
            SELECT p.product_name, p.price, p.platform
            FROM {T_SILVER} p
            INNER JOIN (SELECT MAX(crawl_date) AS md FROM {T_SILVER}) l ON p.crawl_date = l.md
            WHERE p.price > 0 LIMIT 3
        """)
        result["step2_plat_rows"] = "OK"
        result["step2_sample"] = [str(r) for r in plat]
    except Exception as e:
        result["step2_plat_rows"] = f"FAIL: {e}"
        result["step2_tb"] = _tb.format_exc()
        return JSONResponse(result)
    # step3: 스코어링 테스트
    try:
        test_s = _score_mapping("사과식초 1.8L", 5000, "사과식초1.8L", None, None, "직배송", "쿠팡", "", 0.0)
        result["step3_score_v1"] = test_s
    except Exception as e:
        result["step3_score_v1"] = f"FAIL: {e}"
    return JSONResponse(result)
