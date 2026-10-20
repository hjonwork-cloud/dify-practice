"""배민·오늘얼마 경쟁 시뮬레이터(BT) 라우터.

prefix: /portal/price-monitor/bt

기준
  - 운영 SKU(공급): 수발주(ZSDR0017) 플랜트 4121(화성 외식_3배치) · 배치 '03' · 마감시간 22:00 ·
                   사용보류/삭제구분≠X · 종료일 미경과 · 자재그룹≠5140 · 수기 제외 아님
  - 예상수량(수요): 매출(T_MAIN) 최근 N일(기본 28일) 전체센터 일평균. 3배치 SKU 코드 + [N] 1배치 쌍둥이 코드 합산,
                    센터(플랜트)·배치별 내역 함께 제공. 일평균 = 합계 ÷ 영업일(월~토)
  - 비교가: 평균 판매단가(VAT 제외 — T_MAIN 최근 2주 실적 매출액 ÷ 매출수량, 당사 공급가)
  - 경쟁사 실수취가 = 판매가 × (1−수수료) ÷ VAT ÷ 배수
      수수료: 배민상회 타사 6.6% · 오늘얼마 4.8% · 식봄 기존 규칙(직배송 6.6 / 싱싱 17.1 / CJ 4.8)
  - 판정: d = (경쟁사 실수취가 − 당사 평균 판매단가) ÷ 경쟁사 실수취가
          d ≥ +밴드 → 승 / |d| < 밴드 → 무 / d ≤ −밴드 → 패 (밴드 기본 2%)
  - 당사 플랫폼 등록가 환산 = 평균 판매단가 × VAT ÷ (1 − 당사 수수료)  (배민상회 5.5% · 오늘얼마 4.8%)
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import bt_db
import portal_db
import price_monitor_router as pm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portal/price-monitor/bt", tags=["price-monitor-bt"])

BT_PLANT = "4121"
BT_BATCH = "03"          # 3배치
BT_CUTOFF = "220000"     # 주문 마감시간 22:00 (3배치 실제 운영 대상)
EXCLUDED_MATERIAL_GROUP = "5140"
PLATFORMS = ["baemin", "todaysales", "foodspring"]
FOCUS_PLATFORMS = ["baemin", "todaysales"]
PF_LABEL = pm.PF_LABEL_FULL
MONTH_DAYS = 26  # 월 환산 영업일 (월~토)

try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover
    _KST = None


def _now_kst() -> datetime:
    return datetime.now(_KST) if _KST else datetime.now()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _session(request: Request) -> dict:
    return pm._require_pm_access(request)


# ── 캐시 ────────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, object]] = {}
_build_lock = threading.Lock()
_UNIVERSE_TTL = 600


def _cached(key: str, ttl: int, loader, disk_ttl: int | None = None, allow_empty: bool = False):
    ent = _cache.get(key)
    if ent and time.time() - ent[0] < ttl:
        return ent[1]
    if disk_ttl:
        d = pm._disk_get(f"bt_{key}")
        if d and time.time() - d[0] < disk_ttl:
            _cache[key] = d
            return d[1]
    data = loader()
    if data or allow_empty:
        ts = time.time()
        _cache[key] = (ts, data)
        if disk_ttl:
            pm._disk_set(f"bt_{key}", ts, data)
    return data


def invalidate(*keys: str) -> None:
    if not keys:
        _cache.clear()
        return
    for k in keys:
        _cache.pop(k, None)


def _md5(items) -> str:
    return hashlib.md5(",".join(items).encode("utf-8")).hexdigest()[:12]


# ── 1. 4121 수발주 원천 ─────────────────────────────────────────────────────

# ZMM60(material_master_d)에 일부 코드(4121/03/22시 기준 162건)가 누락되어 자재그룹이 NULL → 5140 제외가 안 됨.
# 확장 마스터(materials_master_d, 코드당 1행·자재그룹 99.97% 일치, 동일 컬럼 체계)로
# 상품명·자재그룹·분류·온도조건·세금분류·단위 모두 누락분만 보완한다.
T_ZMM60_ALL = "h_hmfo_fsi.gd_fsi_ent.sap_zmm60_materials_master_d"


def _query_4121() -> list[dict]:
    """플랜트 4121 · 배치 '03' · 마감시간 22:00 수발주 전체(사용보류·자재그룹 무관).
    4121에는 배치 01/03/04/06, 마감시간 00:01/12:00/16:00/17:00/22:00 행이 섞여 있고
    실제 3배치 운영 대상은 '배치 03 + 마감시간 22:00' 조합뿐이므로 여기서 먼저 걸러낸다.
    (전체 4121 = 18.5천 코드 → 03/22시 = 6.4천 코드 → 유효·자재그룹≠5140 = 약 2.2천 코드)
    사용보류·삭제구분·종료일은 이 행들 기준으로 판정하며, 막힘/제외 상태도 변화 추적을 위해
    모두 수집한 뒤 _classify 단계에서 분류한다."""
    rows = pm._q(f"""
        SELECT
            z.`상품코드`                                AS product_code,
            COALESCE(MAX(m.`상품명`), MAX(m2.`상품명`), MAX(z.`상품명`), z.`상품코드`) AS product_name,
            COALESCE(MAX(m.`자재그룹`), MAX(m2.`자재그룹`))     AS material_group,
            COALESCE(MAX(m.`자재그룹명`), MAX(m2.`자재그룹명`)) AS product_group,
            COALESCE(MAX(m.`대분류`), MAX(m2.`대분류`))         AS category,
            COALESCE(MAX(m.`중분류`), MAX(m2.`중분류`))         AS mid_category,
            COALESCE(MAX(m.`온도조건`), MAX(m2.`온도조건`))     AS temp_cond,
            COALESCE(MAX(m.`세금분류명`), MAX(m2.`세금분류명`), '과세') AS tax_class,
            COALESCE(MAX(m.`단위`), MAX(m2.`단위`))             AS unit,
            1                                           AS has03,
            MAX(z.`마감시간`)                            AS cutoff_time,
            MIN(CASE WHEN COALESCE(z.`사용보류`, '') = 'X'
                       OR COALESCE(z.`삭제구분`, '') = 'X'
                       OR z.`종료일` < current_date() THEN 'X' ELSE '' END)
                                                        AS use_hold
        FROM {pm.T_ZSDR} z
        LEFT JOIN {pm.T_ZMM60} m ON z.`상품코드` = m.`상품코드`
        LEFT JOIN {T_ZMM60_ALL} m2 ON z.`상품코드` = m2.`상품코드`
        WHERE z.`플랜트` = '{BT_PLANT}'
          AND z.`배치`   = '{BT_BATCH}'
          AND z.`마감시간` = '{BT_CUTOFF}'
        GROUP BY z.`상품코드`
    """) or []
    out = []
    for r in pm._serialize_rows(rows):
        code = str(r.get("product_code") or "").strip()
        if not code:
            continue
        r["product_code"] = code
        r["use_hold"] = str(r.get("use_hold") or "").strip().upper()
        r["material_group"] = str(r.get("material_group") or "").strip()
        out.append(r)
    return out


def get_raw(force: bool = False) -> list[dict]:
    # 캐시 키에 배치·마감시간 조건을 포함 → 기준 변경 시 구 캐시를 자동 폐기
    key = f"raw4121_b{BT_BATCH}c{BT_CUTOFF}_m2"
    if force:
        data = _query_4121()
        if data:
            ts = time.time()
            _cache[key] = (ts, data)
            pm._disk_set(f"bt_{key}", ts, data)
        return data
    return _cached(key, 600, _query_4121, disk_ttl=6 * 3600)


# ── 2. [N] 쌍둥이(1배치 코드) 연결 ──────────────────────────────────────────

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", "", (name or "").lower())


def _twin_map(raw: list[dict]) -> dict[str, tuple[str, str]]:
    """[N]상품명 3배치 SKU → 동일 기본명의 non-[N] 1배치 상품코드.
    동명 상품이 여럿이면 전월 매출수량이 가장 큰 코드를 선택."""
    try:
        prods = pm._get_our_products("ALL") or []
        sales = pm._get_prev_month_sales_totals("ALL") or {}
    except Exception as e:
        logger.warning(f"[bt] twin map 기초데이터 실패: {e}")
        return {}
    cand: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in prods:
        nm = (p.get("product_name") or "").strip()
        if not nm or nm.startswith("[N]"):
            continue
        cand[_norm_name(nm)].append((str(p["product_code"]), nm))
    out = {}
    for r in raw:
        nm = (r.get("product_name") or "").strip()
        if not nm.startswith("[N]"):
            continue
        lst = [c for c in cand.get(_norm_name(nm[3:]), []) if c[0] != r["product_code"]]
        if not lst:
            continue
        lst.sort(key=lambda c: _f((sales.get(c[0]) or {}).get("prev_sales_qty")) or 0, reverse=True)
        out[r["product_code"]] = lst[0]
    return out


def _classify(r: dict, excl: set[str]) -> str:
    if r.get("material_group") == EXCLUDED_MATERIAL_GROUP:
        return "group5140"
    if r.get("use_hold") == "X":
        return "blocked"
    if r["product_code"] in excl:
        return "excluded"
    return "active"


STATUS_LABEL = {"active": "운영", "blocked": "막힘(사용보류)", "excluded": "수기 제외", "group5140": "자재그룹 5140"}


def get_skus(force: bool = False) -> list[dict]:
    raw = get_raw(force)
    excl = bt_db.active_exclusion_codes()
    twins = _twin_map(raw)
    out = []
    for r in raw:
        tw = twins.get(r["product_code"])
        out.append({
            **r,
            "status": _classify(r, excl),
            "twin_code": tw[0] if tw else "",
            "twin_name": tw[1] if tw else "",
        })
    return out


# ── 3. 예상수량(전체센터 일평균 + 센터·배치별) ────────────────────────────────

def _workdays(days: int) -> int:
    today = _now_kst().date()
    return max(1, sum(1 for i in range(1, days + 1) if (today - timedelta(days=i)).weekday() != 6))


def get_demand(codes: list[str], days: int) -> dict[str, list[dict]]:
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    key = f"demand_{days}_{_md5(codes)}"

    def _load():
        import main as _main
        out: dict[str, list[dict]] = defaultdict(list)
        for i in range(0, len(codes), 800):
            chunk = codes[i:i + 800]
            in_list = ", ".join("'" + c.replace("'", "''") + "'" for c in chunk)
            rows = pm._q(f"""
                SELECT `자재`                              AS product_code,
                       `플랜트`                            AS plant,
                       MAX(`플랜트명`)                     AS plant_name,
                       SUM(CAST(`매출수량` AS DOUBLE))     AS qty
                FROM {_main.T_MAIN}
                WHERE `대금청구일` >= DATE_FORMAT(DATE_SUB(CURRENT_DATE(), {int(days)}), 'yyyyMMdd')
                  AND `대금청구일` <  DATE_FORMAT(CURRENT_DATE(), 'yyyyMMdd')
                  AND `자재` IN ({in_list})
                GROUP BY `자재`, `플랜트`
            """) or []
            for r in pm._serialize_rows(rows):
                q = _f(r.get("qty")) or 0.0
                if q <= 0:
                    continue
                out[str(r.get("product_code"))].append({
                    "plant": str(r.get("plant") or ""),
                    "plant_name": r.get("plant_name") or "",
                    "qty": q,
                })
        return dict(out)

    return _cached(key, 3600, _load, disk_ttl=6 * 3600, allow_empty=True)


# ── 4. 플랫폼 최신가 ────────────────────────────────────────────────────────

def _platform_prices(keys) -> dict[str, dict]:
    keys = sorted({k for k in keys if k})
    if not keys:
        return {}
    key = f"plat_{_md5(keys)}"

    def _load():
        out = {}
        for i in range(0, len(keys), 1000):
            for r in pm._get_platform_latest(product_keys=keys[i:i + 1000]) or []:
                out[r["product_key"]] = r
        return out

    return _cached(key, 600, _load, allow_empty=True)


# ── 5. 수수료 · 판정 ────────────────────────────────────────────────────────

def comp_fee(P: dict, platform: str, delivery_type: str, seller: str) -> float:
    if platform == "baemin":
        return float(P["fee_comp_baemin"]) / 100
    if platform == "todaysales":
        return float(P["fee_comp_todaysales"]) / 100
    return pm._get_fee(delivery_type or "직배송", platform, seller)


def our_fee(P: dict, platform: str) -> float:
    if platform == "baemin":
        return float(P["fee_our_baemin"]) / 100
    if platform == "todaysales":
        return float(P["fee_our_todaysales"]) / 100
    return pm._FEE_DIRECT


def verdict(our: float | None, comp_net: float | None, band: float) -> tuple[str, float | None]:
    if not our or not comp_net:
        return "unknown", None
    d = (comp_net - our) / comp_net * 100
    if d >= band:
        return "win", round(d, 1)
    if d <= -band:
        return "lose", round(d, 1)
    return "tie", round(d, 1)


# ── 5-1. 총액(회계) 환산 + 손익 구조 ────────────────────────────────────────
#  재경팀 검토 결과(09.22): 홈푸드 = 본인(principal)
#    → 판매대가 총액을 매출 인식, 플랫폼 수수료는 비용(브랜드수수료)으로 차감
#  순액 공급가 p(= 수수료 차감 후 수취액, VAT 제외) 기준으로
#    등록가(VAT 포함) L = p × VAT ÷ (1 − 수수료)
#    총액 회계매출(VAT 제외) G = L ÷ VAT = p ÷ (1 − 수수료)
#
#  손익 구조 (모두 G 대비 비율)
#    매출총이익 = G − 매입원가
#    변동비     = 운반비(콜단가÷회당매출) + 하역비 + 기타1(브랜드수수료 + 보험료 + 포장비)
#    공헌이익   = 매출총이익 − 변동비
#    고정비     = 물류간접비 + 기타2(원천 − 수수료 공통매입세액)
#    영업이익   = 공헌이익 − 고정비
#  ※ 브랜드수수료 고지율(5.5% / 4.9%)은 VAT 포함이므로 ÷VAT 하여 공급가 기준으로 환산

VAT_RATE = 1.1


def cost_rates(P: dict, platform: str) -> dict:
    """플랫폼별 비용률(소수). 매출액 G(VAT 제외) 대비."""
    def g(k: str, d: float) -> float:
        try:
            return float(P.get(k, d)) / 100
        except (TypeError, ValueError):
            return d / 100

    fee = our_fee(P, platform)                       # 정산 차감율 (VAT 포함 고지율)
    try:
        call_cost = float(P.get("call_cost", 11000) or 0)
        call_rev = float(P.get("call_revenue", 150000) or 0)
    except (TypeError, ValueError):
        call_cost, call_rev = 11000.0, 150000.0
    freight = (call_cost / call_rev) if call_rev else 0.0

    unload = g("unload_pct", 4.05)
    brand = fee / VAT_RATE                           # VAT 포함 고지율 → 공급가 기준
    insurance = g("insurance_pct", 0.0)
    packing = g("packing_pct", 0.06)
    etc1 = brand + insurance + packing
    var = freight + unload + etc1

    indirect = g("logi_indirect_pct", 1.3)
    etc2 = g("other2_pct", 2.33) - g("vat_credit_pct", 0.19)
    fix = indirect + etc2

    return {"fee": fee, "freight": freight, "unload": unload, "brand": brand,
            "insurance": insurance, "packing": packing, "etc1": etc1, "var": var,
            "indirect": indirect, "etc2": etc2, "fix": fix}


def gross_of(net: float | None, fee: float) -> float | None:
    """순액 공급가 → 총액 회계매출(VAT 제외)."""
    return None if not net else net / (1 - fee)


def list_of(net: float | None, fee: float, vat: float) -> float | None:
    """순액 공급가 → 플랫폼 등록가(VAT 포함)."""
    return None if not net else net * vat / (1 - fee)


def pnl_of(net: float | None, buy: float | None, R: dict) -> dict | None:
    """순액 공급가 1개 기준 손익 (금액·비율)."""
    g = gross_of(net, R["fee"])
    if not g or not buy:
        return None
    gp = g - buy
    var = g * R["var"]
    cm = gp - var
    fix = g * R["fix"]
    op = cm - fix
    r = lambda v: round(v / g * 100, 1)  # noqa: E731
    return {"gross": round(g, 1), "cogs": round(buy, 1),
            "gp": round(gp, 1), "gp_pct": r(gp),
            "freight": round(g * R["freight"], 1), "unload": round(g * R["unload"], 1),
            "brand": round(g * R["brand"], 1), "packing": round(g * R["packing"], 1),
            "insurance": round(g * R["insurance"], 1),
            "var": round(var, 1), "var_pct": r(var),
            "cm": round(cm, 1), "cm_pct": r(cm),
            "indirect": round(g * R["indirect"], 1), "etc2": round(g * R["etc2"], 1),
            "fix": round(fix, 1), "fix_pct": r(fix),
            "op": round(op, 1), "op_pct": r(op)}


def cm_pct_of(net: float | None, buy: float | None, R: dict) -> float | None:
    """공헌이익율(%) — 총액매출 대비."""
    p = pnl_of(net, buy, R)
    return p["cm_pct"] if p else None


def op_pct_of(net: float | None, buy: float | None, R: dict) -> float | None:
    """영업이익율(%) — 총액매출 대비."""
    p = pnl_of(net, buy, R)
    return p["op_pct"] if p else None


def cm_floor_price(buy: float | None, R: dict, cm_floor: float) -> float | None:
    """공헌이익율 하한을 만족하는 최저 순액 공급가.
       G×(1−변동비율) − 원가 ≥ 하한×G  →  p = 원가 × (1−수수료) ÷ (1 − 변동비율 − 하한)"""
    if not buy:
        return None
    k = 1 - R["var"] - cm_floor
    if k <= 0:
        return None
    return round(buy * (1 - R["fee"]) / k, 1)


VERDICT_RANK = {"lose": 0, "tie": 1, "win": 2, "unknown": 3}


# ── 6. 유니버스(전 SKU × 매핑 × 가격 × 수요) ────────────────────────────────

def _build_universe() -> dict:
    t0 = time.time()
    P = bt_db.get_params()
    band = float(P["tie_band_pct"])
    days = int(P["demand_days"])
    warnings: list[str] = []

    all_skus = get_skus()
    status_counts = defaultdict(int)
    for s in all_skus:
        status_counts[s["status"]] += 1
    skus = [s for s in all_skus if s["status"] != "group5140"]

    try:
        base = {str(r["product_code"]): r for r in (pm._get_base_prices("ALL") or [])}
    except Exception as e:
        base = {}
        warnings.append(f"평균 판매단가 조회 실패: {e}")

    by_code: dict[str, list[dict]] = defaultdict(list)
    for m in portal_db.pm_list_all_mappings("ALL"):
        by_code[str(m["our_product_code"])].append(m)

    sku_maps: dict[str, list[dict]] = {}
    keys: set[str] = set()
    for s in skus:
        seen: set[str] = set()
        lst = []
        for c in (s["product_code"], s["twin_code"]):
            if not c:
                continue
            for m in by_code.get(c, []):
                if m["product_key"] in seen:
                    continue
                seen.add(m["product_key"])
                lst.append(m)
                keys.add(m["product_key"])
        sku_maps[s["product_code"]] = lst

    try:
        plat = _platform_prices(keys)
    except Exception as e:
        plat = {}
        warnings.append(f"플랫폼 가격 조회 실패: {e}")

    try:
        dem = get_demand([s["product_code"] for s in skus] + [s["twin_code"] for s in skus], days)
    except Exception as e:
        dem = {}
        warnings.append(f"예상수량(매출) 조회 실패: {e}")
    wd = _workdays(days)

    crawl_dates: dict[str, str] = {}
    out = []
    for s in skus:
        code, twin = s["product_code"], s["twin_code"]
        if code in base:
            bp, price_src = base[code], "own"
        elif twin and twin in base:
            bp, price_src = base[twin], "twin"
        else:
            bp, price_src = {}, ""
        our = _f(bp.get("avg_sale_price"))
        buy = _f(bp.get("avg_buy_price"))
        tax = s.get("tax_class") or "과세"
        vat = 1.1 if tax == "과세" else 1.0
        gp = round((our - buy) / our * 100, 1) if our and buy and our > 0 else None

        plants: dict[tuple[str, str], float] = defaultdict(float)
        for c in {code, twin} - {""}:
            for d in dem.get(c, []):
                plants[(d["plant"], d["plant_name"] or d["plant"])] += d["qty"]
        plant_list = sorted(
            ({"plant": k[0], "label": k[1], "daily": round(v / wd, 2)} for k, v in plants.items()),
            key=lambda x: -x["daily"],
        )
        demand_daily = round(sum(p["daily"] for p in plant_list), 2)

        lines = []
        cnt = {pf: 0 for pf in PLATFORMS}
        for m in sku_maps.get(code, []):
            pk = m["product_key"]
            row = plat.get(pk) or {}
            platform = row.get("platform") or m.get("platform") or ""
            if platform in cnt:
                cnt[platform] += 1
            seller = row.get("platform_seller_name") or m.get("seller_name") or ""
            price = _f(row.get("price_sale"))
            dtype = row.get("delivery_type") or "직배송"
            mult = _f(m.get("multiplier")) or 1.0
            fee = comp_fee(P, platform, dtype, seller)
            comp_net = round(price * (1 - fee) / vat / mult, 1) if price else None
            v, diff = verdict(our, comp_net, band)
            match_gp = round((comp_net - buy) / comp_net * 100, 1) if comp_net and buy else None
            if platform and row.get("crawl_date") and platform not in crawl_dates:
                crawl_dates[platform] = str(row.get("crawl_date"))
            lines.append({
                "mapping_id": m.get("mapping_id"),
                "mapped_to": str(m.get("our_product_code")),
                "platform": platform,
                "seller": seller,
                "product_key": pk,
                "ext_name": row.get("product_name") or "",
                "spec": row.get("spec") or "",
                "comp_price": price,
                "delivery_type": dtype,
                "fee_pct": round(fee * 100, 1),
                "multiplier": mult,
                "tag": m.get("tag") or "normal",
                "comp_net": comp_net,
                "diff_pct": diff,
                "verdict": v,
                "match_gp": match_gp,
                "crawl_date": str(row.get("crawl_date") or ""),
                "stale": not bool(row),
            })

        out.append({
            "product_code": code,
            "product_name": s.get("product_name") or code,
            "status": s["status"],
            "use_hold": s.get("use_hold") or "",
            "material_group": s.get("material_group") or "",
            "product_group": s.get("product_group") or "",
            "category": s.get("category") or "",
            "temp_cond": s.get("temp_cond") or "",
            "tax_class": tax,
            "unit": s.get("unit") or "",
            "twin_code": twin,
            "twin_name": s.get("twin_name") or "",
            "our_price": round(our, 1) if our else None,
            "buy_price": round(buy, 1) if buy else None,
            "price_src": price_src,
            "gp_pct": gp,
            "demand_daily": demand_daily,
            "demand_plants": plant_list,
            "map_cnt": cnt,
            "map_total": len(lines),
            "lines": lines,
        })

    return {
        "built_at": _now_kst().strftime("%Y-%m-%d %H:%M"),
        "build_sec": round(time.time() - t0, 1),
        "params": P,
        "workdays": wd,
        "status_counts": dict(status_counts),
        "crawl_dates": crawl_dates,
        "warnings": warnings,
        "skus": out,
    }


def get_universe(force: bool = False) -> dict:
    ent = _cache.get("universe")
    if not force and ent and time.time() - ent[0] < _UNIVERSE_TTL:
        return ent[1]
    with _build_lock:
        ent = _cache.get("universe")
        if not force and ent and time.time() - ent[0] < _UNIVERSE_TTL:
            return ent[1]
        data = _build_universe()
        _cache["universe"] = (time.time(), data)
        return data


def _best_by_platform(s: dict) -> dict:
    """플랫폼별 최저 실수취가 라인 (= 가장 저가로 거는 경쟁자)."""
    best: dict[str, dict] = {}
    for ln in s["lines"]:
        if ln["comp_net"] is None:
            continue
        b = best.get(ln["platform"])
        if b is None or ln["comp_net"] < b["comp_net"]:
            best[ln["platform"]] = ln
    return {pf: {"comp_net": b["comp_net"], "seller": b["seller"], "verdict": b["verdict"],
                 "platform": pf,
                 "diff_pct": b["diff_pct"], "match_gp": b["match_gp"],
                 # 계산 근거(툴팁용): 실수취 = 판매가(VAT 포함) × (1−수수료) ÷ VAT ÷ 배수
                 "comp_price": b["comp_price"], "fee_pct": b["fee_pct"],
                 "multiplier": b["multiplier"], "ext_name": b.get("ext_name", ""),
                 # 해당 셀러가 실제로 올려둔 등록가 (원단위 환산 전 원본 / 1단위 환산)
                 "seller_price": b["comp_price"],
                 "seller_unit_price": (round(b["comp_price"] / (b["multiplier"] or 1))
                                       if b.get("comp_price") else None)}
            for pf, b in best.items()}


# ── 6-1. 당사 등록가(플랫폼별) 결정 우선순위 ────────────────────────────────
#   ① decided : 「상품별·플랫폼별 등록가 결정」 메뉴에서 확정한 가격
#   ② lowest  : 해당 플랫폼 매핑 중 최저가 셀러의 1단위 등록가
#   ③ calc    : 평균 판매단가 p 에서 역산 L = p × VAT ÷ (1−수수료)
#   수익성은 적용 등록가 L 기준 → 순액 공급가 p = L × (1−수수료) ÷ VAT

def _decided_map() -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    try:
        for r in bt_db.list_listing_prices():
            out[str(r["product_code"])][r["platform"]] = r
    except Exception as e:
        logger.warning(f"[bt] 등록가 결정 조회 실패: {e}")
    return out


def listing_of(s: dict, best: dict, decided: dict, P: dict) -> dict:
    vat = 1.1 if (s.get("tax_class") or "과세") == "과세" else 1.0
    out = {}
    for pf in FOCUS_PLATFORMS:
        fee = our_fee(P, pf)
        d = decided.get(pf) or {}
        b = best.get(pf) or {}
        calc = list_of(s.get("our_price"), fee, vat)
        if d.get("list_price"):
            L, src = float(d["list_price"]), "decided"
        elif b.get("seller_unit_price"):
            L, src = float(b["seller_unit_price"]), "lowest"
        elif calc:
            L, src = calc, "calc"
        else:
            L, src = None, ""
        out[pf] = {
            "list_price": round(L) if L else None,
            "src": src,
            "net": round(L * (1 - fee) / vat, 1) if L else None,
            "calc_price": round(calc) if calc else None,
            "lowest_price": b.get("seller_unit_price"),
            "decided_price": d.get("list_price"),
            "basis": d.get("basis") or "",
            "memo": d.get("memo") or "",
            "updated_by": d.get("updated_by") or "",
            "updated_at": d.get("updated_at") or "",
        }
    return out


def _sku_row(s: dict, P: dict | None = None, D: dict | None = None) -> dict:
    r = {k: v for k, v in s.items() if k != "lines"}
    r["best"] = _best_by_platform(s)
    if P is not None:
        r["listing"] = listing_of(s, r["best"], (D or {}).get(s["product_code"], {}), P)
    return r


# ── 7. 시간당 스냅샷 · 변동 감지 ────────────────────────────────────────────

def run_snapshot(run_key: str, trigger: str) -> dict:
    if not bt_db.try_start_run(run_key, trigger):
        return {"ok": True, "skipped": True, "run_key": run_key}
    events = []
    try:
        get_raw(force=True)
        skus = get_skus()
        if not skus:
            raise RuntimeError("4121 수발주 조회 결과 0건 — 원천 지연 또는 조회 실패")
        prev = bt_db.load_state()
        baseline = not prev

        maps_by_code: dict[str, int] = defaultdict(int)
        for m in portal_db.pm_list_all_mappings("ALL"):
            maps_by_code[str(m["our_product_code"])] += 1

        def _mapped(s):
            return maps_by_code.get(s["product_code"], 0) + (maps_by_code.get(s["twin_code"], 0) if s["twin_code"] else 0)

        cur = {s["product_code"]: s for s in skus}
        if not baseline:
            for code, s in cur.items():
                p = prev.get(code)
                mc = _mapped(s)
                hold, mg = s["use_hold"], s["material_group"]
                if not p or not p.get("present"):
                    events.append((s, "new", "yellow", "", f"사용보류={hold or '-'} · 자재그룹={mg or '-'}", mc))
                    continue
                ph = (p.get("use_hold") or "").upper()
                pmg = p.get("material_group") or ""
                if ph != hold:
                    if hold == "X":
                        events.append((s, "block", "red" if mc else "yellow", ph or "-", "X", mc))
                    elif ph == "X":
                        events.append((s, "open", "yellow", "X", hold or "-", mc))
                if (pmg == EXCLUDED_MATERIAL_GROUP) != (mg == EXCLUDED_MATERIAL_GROUP):
                    et = "group_in" if mg == EXCLUDED_MATERIAL_GROUP else "group_out"
                    events.append((s, et, "yellow", pmg, mg, mc))
            for code, p in prev.items():
                if p.get("present") and code not in cur:
                    mc = maps_by_code.get(code, 0)
                    events.append(({"product_code": code, "product_name": p.get("product_name")},
                                   "removed", "red" if mc else "yellow", "존재", "삭제", mc))

        missing = [c for c, p in prev.items() if p.get("present") and c not in cur]
        bt_db.save_state(skus, missing)
        for s, et, sev, bv, av, mc in events:
            bt_db.add_event(s["product_code"], s.get("product_name"), et, sev,
                            before_val=bv, after_val=av, run_key=run_key, mapped_cnt=mc)

        now = _now_kst()
        active = [s for s in skus if s["status"] == "active"]
        today = now.strftime("%Y-%m-%d")
        if now.hour >= 6 and not bt_db.has_daily(today):
            bt_db.save_daily(today, active)

        msg = "기준선 생성" if baseline else f"이벤트 {len(events)}건"
        bt_db.finish_run(run_key, "ok", len(skus), len(active), len(events), msg)
        if events:
            invalidate("universe")
            _notify(run_key, events)
        return {"ok": True, "run_key": run_key, "baseline": baseline,
                "sku_total": len(skus), "sku_active": len(active), "events": len(events)}
    except Exception as e:
        logger.warning(f"[bt] snapshot 실패 {run_key}: {e}")
        bt_db.finish_run(run_key, "error", 0, 0, len(events), str(e))
        return {"ok": False, "run_key": run_key, "error": str(e)}


def _notify(run_key: str, events: list) -> None:
    red = [e for e in events if e[2] == "red"]
    if not red:
        return
    to = [x.strip() for x in str(bt_db.get_params().get("alert_email") or "").split(",") if x.strip()]
    if not to:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        import crawl_mailer as cm
        if not cm._smtp_configured():
            logger.info("[bt] SMTP 미설정 — 알림 메일 생략")
            return
        rows = "".join(
            f"<tr><td style='padding:4px 8px'>{bt_db.EVENT_LABELS.get(et, et)}</td>"
            f"<td style='padding:4px 8px'>{s['product_code']}</td>"
            f"<td style='padding:4px 8px'>{s.get('product_name') or ''}</td>"
            f"<td style='padding:4px 8px'>{bv} → {av}</td><td style='padding:4px 8px'>매핑 {mc}</td></tr>"
            for s, et, sev, bv, av, mc in red
        )
        html = (f"<h3>[3배치 수발주] 🔴 변동 {len(red)}건 ({run_key})</h3>"
                f"<p>매핑(경쟁 판매 대상) SKU의 막힘/삭제가 감지되었습니다. 셀러센터 판매중지 여부를 확인하세요.</p>"
                f"<table border='1' style='border-collapse:collapse;font-size:13px'>{rows}</table>")
        msg = MIMEText(html, "html", "utf-8")
        from_addr = cm._env("SMTP_FROM") or cm._env("SMTP_USER")
        msg["Subject"] = f"[3배치 수발주] 🔴 막힘/삭제 {len(red)}건"
        msg["From"] = from_addr
        msg["To"] = ", ".join(to)
        host, port = cm._env("SMTP_HOST"), int(cm._env("SMTP_PORT", "587"))
        if cm._env("SMTP_TLS", "true").lower() != "false":
            with smtplib.SMTP(host, port, timeout=30) as sm:
                sm.ehlo(); sm.starttls(); sm.login(cm._env("SMTP_USER"), cm._env("SMTP_PASSWORD"))
                sm.sendmail(from_addr, to, msg.as_string())
        else:
            with smtplib.SMTP_SSL(host, port, timeout=30) as sm:
                sm.login(cm._env("SMTP_USER"), cm._env("SMTP_PASSWORD"))
                sm.sendmail(from_addr, to, msg.as_string())
    except Exception as e:
        logger.warning(f"[bt] 알림 메일 실패: {e}")


def _scheduler_loop() -> None:
    time.sleep(90)
    while True:
        try:
            run_snapshot(_now_kst().strftime("%Y%m%d%H"), "hourly")
            try:
                get_universe(force=True)
            except Exception as e:
                logger.warning(f"[bt] universe 워밍업 실패: {e}")
        except Exception as e:
            logger.warning(f"[bt] scheduler 오류: {e}")
        now = _now_kst()
        nxt = now.replace(minute=5, second=0, microsecond=0)
        if now >= nxt:
            nxt += timedelta(hours=1)
        time.sleep(max(60, (nxt - now).total_seconds()))


if os.getenv("BT_SCHEDULER", "1") == "1":
    threading.Thread(target=_scheduler_loop, daemon=True, name="bt-snapshot").start()


# ═══════════════════════════════════════════════════════════════════════════
# 화면
# ═══════════════════════════════════════════════════════════════════════════

_PAGES = {
    "":            ("bt_home.html",        "bt_home",        "배민·오늘얼마 시뮬레이터 홈"),
    "skus":        ("bt_skus.html",        "bt_skus",        "1-1 · 3배치 운영 SKU"),
    "monitor":     ("bt_monitor.html",     "bt_monitor",     "1-2 · 수발주 변동 모니터"),
    "exclusions":  ("bt_exclusions.html",  "bt_exclusions",  "1-3 · 수기 제외 관리"),
    "coverage":    ("bt_coverage.html",    "bt_coverage",    "2-1 · 매핑 커버리지"),
    "mapping":     ("bt_mapping.html",     "bt_mapping",     "2-2 · 상품별 매핑"),
    "ai-queue":    ("bt_ai_queue.html",    "bt_ai_queue",    "3-1 · 미매핑 큐 · AI 추천"),
    "competition": ("bt_competition.html", "bt_competition", "4-1 · 셀러별 경쟁 현황"),
    "simulator":   ("bt_simulator.html",   "bt_simulator",   "4-2 · 셀러 시뮬레이터"),
    "profit":      ("bt_profit.html",      "bt_profit",      "5-1 · SKU 수익성 · 추천가"),
    "listing":     ("bt_listing.html",     "bt_listing",     "5-2 · 상품별·플랫폼별 등록가 결정"),
    "settings":    ("bt_settings.html",    "bt_settings",    "6·7 · 파라미터 · 알림"),
}


def _page(request: Request, slug: str, **ctx) -> HTMLResponse:
    _session(request)
    tpl, active, title = _PAGES[slug]
    return pm._render(request, tpl, active_page=active, page_title=title,
                      bt_params=bt_db.get_params(),
                      exclude_reasons=bt_db.EXCLUDE_REASONS,
                      event_labels=bt_db.EVENT_LABELS, **ctx)


@router.get("/", response_class=HTMLResponse)
def bt_home(request: Request):
    return _page(request, "")


@router.get("/{slug}", response_class=HTMLResponse)
def bt_page(request: Request, slug: str):
    if slug not in _PAGES or slug == "":
        raise HTTPException(status_code=404)
    return _page(request, slug, qp=dict(request.query_params))


# ═══════════════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════════════

def _ok(data: dict | None = None, **kw) -> JSONResponse:
    return JSONResponse({"ok": True, **(data or {}), **kw})


def _universe_or_error():
    try:
        return get_universe(), None
    except Exception as e:
        logger.exception("[bt] universe 빌드 실패")
        return None, JSONResponse({"ok": False, "error": f"데이터 구성 실패: {e}"}, status_code=500)


def _meta(U: dict) -> dict:
    return {"built_at": U["built_at"], "build_sec": U["build_sec"], "workdays": U["workdays"],
            "crawl_dates": U["crawl_dates"], "warnings": U["warnings"], "params": U["params"]}


@router.post("/api/refresh")
def api_refresh(request: Request):
    _session(request)
    invalidate()
    U, err = _universe_or_error()
    if err:
        return err
    return _ok(_meta(U))


# ── 0. 홈 ───────────────────────────────────────────────────────────────────

@router.get("/api/home")
def api_home(request: Request):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    active = [s for s in U["skus"] if s["status"] == "active"]
    n = len(active) or 1
    map_rate = {}
    wtl = {pf: {"win": 0, "tie": 0, "lose": 0, "none": 0} for pf in PLATFORMS}
    risks = []
    for s in active:
        best = _best_by_platform(s)
        for pf in PLATFORMS:
            b = best.get(pf)
            wtl[pf][b["verdict"] if b and b["verdict"] in ("win", "tie", "lose") else "none"] += 1
            if pf in FOCUS_PLATFORMS and b and b["verdict"] == "lose":
                risks.append({"product_code": s["product_code"], "product_name": s["product_name"],
                              "platform": pf, "seller": b["seller"], "diff_pct": b["diff_pct"],
                              "our_price": s["our_price"], "comp_net": b["comp_net"],
                              "demand_daily": s["demand_daily"],
                              "stake": round((s["demand_daily"] or 0) * (s["our_price"] or 0))})
    for pf in PLATFORMS:
        mapped = sum(1 for s in active if s["map_cnt"].get(pf))
        map_rate[pf] = {"mapped": mapped, "rate": round(mapped / n * 100, 1)}
    gps = [s["gp_pct"] for s in active if s["gp_pct"] is not None]
    w = [(s["gp_pct"], (s["demand_daily"] or 0) * (s["our_price"] or 0)) for s in active if s["gp_pct"] is not None]
    wsum = sum(x[1] for x in w)
    ev = bt_db.list_events(days=1)
    runs = bt_db.list_runs(1)
    risks.sort(key=lambda r: -r["stake"])
    return _ok(_meta(U),
               status_counts=U["status_counts"],
               active_cnt=len(active),
               map_rate=map_rate,
               wtl=wtl,
               gp_avg=round(sum(gps) / len(gps), 1) if gps else None,
               gp_wavg=round(sum(a * b for a, b in w) / wsum, 1) if wsum else None,
               gp_alert_cnt=sum(1 for g in gps if g < float(U["params"]["gp_alert"])),
               demand_total=round(sum(s["demand_daily"] or 0 for s in active), 1),
               revenue_daily=round(sum((s["demand_daily"] or 0) * (s["our_price"] or 0) for s in active)),
               events_today={"total": len(ev),
                             "red": sum(1 for e in ev if e["severity"] == "red"),
                             "unacked": sum(1 for e in ev if not e["ack_at"]),
                             "block": sum(1 for e in ev if e["event_type"] == "block"),
                             "open": sum(1 for e in ev if e["event_type"] == "open"),
                             "new": sum(1 for e in ev if e["event_type"] == "new")},
               last_run=runs[0] if runs else None,
               due_releases=bt_db.due_release_codes(_now_kst().strftime("%Y-%m-%d")),
               unmapped_active=sum(1 for s in active if not s["map_total"]),
               risks=risks[:15])


# ── 1-1. 운영 SKU ───────────────────────────────────────────────────────────

@router.get("/api/skus")
def api_skus(request: Request, include_5140: int = 0):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    D = _decided_map()
    rows = [_sku_row(s, U["params"], D) for s in U["skus"]]
    if include_5140:
        for r in get_skus():
            if r["status"] == "group5140":
                rows.append({"product_code": r["product_code"], "product_name": r.get("product_name"),
                             "status": "group5140", "use_hold": r.get("use_hold"),
                             "material_group": r.get("material_group"), "category": r.get("category") or "",
                             "map_cnt": {}, "map_total": 0, "demand_daily": 0, "demand_plants": [], "best": {}})
    return _ok(_meta(U), status_counts=U["status_counts"], rows=rows)


@router.get("/api/sku/{code}")
def api_sku_detail(request: Request, code: str):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    s = next((x for x in U["skus"] if x["product_code"] == code), None)
    if not s:
        return JSONResponse({"ok": False, "error": "SKU 없음"}, status_code=404)
    hist = bt_db.list_events(product_code=code)
    excl = next((e for e in bt_db.list_exclusions(active_only=False) if e["product_code"] == code), None)
    listing = listing_of(s, _best_by_platform(s), _decided_map().get(code, {}), U["params"])
    return _ok(_meta(U), sku={**s, "listing": listing}, events=hist, exclusion=excl)


# ── 1-2. 수발주 변동 ────────────────────────────────────────────────────────

@router.get("/api/events")
def api_events(request: Request, days: int = 7, event_type: str = "", unacked: int = 0):
    _session(request)
    return _ok(events=bt_db.list_events(days=max(1, min(days, 180)), event_type=event_type,
                                        unacked_only=bool(unacked)),
               runs=bt_db.list_runs(48), daily=bt_db.daily_counts(30))


@router.post("/api/events/ack")
async def api_events_ack(request: Request):
    sess = _session(request)
    body = await request.json()
    n = bt_db.ack_events([int(i) for i in body.get("event_ids", [])], sess.get("emp_code", ""),
                         (body.get("note") or "")[:300])
    return _ok(acked=n)


@router.post("/api/snapshot/run")
def api_snapshot_run(request: Request):
    _session(request)
    key = _now_kst().strftime("%Y%m%d%H%M%S") + "_manual"
    return JSONResponse(run_snapshot(key, "manual"))


# ── 1-3. 수기 제외 ──────────────────────────────────────────────────────────

@router.get("/api/exclusions")
def api_exclusions(request: Request, include_released: int = 0):
    _session(request)
    return _ok(rows=bt_db.list_exclusions(active_only=not include_released),
               due=bt_db.due_release_codes(_now_kst().strftime("%Y-%m-%d")))


@router.post("/api/exclusions/add")
async def api_exclusions_add(request: Request):
    sess = _session(request)
    body = await request.json()
    codes = body.get("codes") or ([body.get("code")] if body.get("code") else [])
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if not codes:
        return JSONResponse({"ok": False, "error": "상품코드 필요"}, status_code=400)
    names = {r["product_code"]: r.get("product_name") for r in get_raw()}
    added, unknown = [], []
    for c in codes:
        if c not in names:
            unknown.append(c)
            continue
        bt_db.add_exclusion(c, names.get(c) or "", body.get("reason_code") or "etc",
                            (body.get("memo") or "")[:300], body.get("release_date") or "",
                            sess.get("emp_code", ""))
        added.append(c)
    invalidate("universe")
    return _ok(added=added, unknown=unknown)


@router.post("/api/exclusions/release")
async def api_exclusions_release(request: Request):
    sess = _session(request)
    body = await request.json()
    codes = body.get("codes") or ([body.get("code")] if body.get("code") else [])
    n = sum(1 for c in codes if bt_db.release_exclusion(str(c), sess.get("emp_code", "")))
    invalidate("universe")
    return _ok(released=n)


# ── 2-1. 커버리지 ───────────────────────────────────────────────────────────

def _seller_sku_counts() -> dict[tuple[str, str], int]:
    def _load():
        rows = pm._q(f"""
            SELECT p.platform, p.platform_seller_name AS seller, COUNT(*) AS cnt
            FROM {pm.T_SILVER} p
            INNER JOIN (
                SELECT platform, platform_seller_name, MAX(crawl_date) AS md
                FROM {pm.T_SILVER} GROUP BY platform, platform_seller_name
            ) m ON p.platform = m.platform AND p.platform_seller_name = m.platform_seller_name
               AND p.crawl_date = m.md
            GROUP BY p.platform, p.platform_seller_name
        """) or []
        return {f"{r['platform']}|{r['seller']}": int(r["cnt"] or 0) for r in rows}
    try:
        d = _cached("seller_cnt", 3600, _load, disk_ttl=6 * 3600)
    except Exception as e:
        logger.warning(f"[bt] seller sku count 실패: {e}")
        d = {}
    return {tuple(k.split("|", 1)): v for k, v in (d or {}).items()}


@router.get("/api/coverage")
def api_coverage(request: Request, top: int = 150):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    active = [s for s in U["skus"] if s["status"] == "active"]
    n = len(active) or 1
    by_pf = {}
    for pf in PLATFORMS:
        mapped = [s for s in active if s["map_cnt"].get(pf)]
        priced = [s for s in mapped if any(ln["platform"] == pf and ln["comp_net"] for ln in s["lines"])]
        dsum = sum(s["demand_daily"] or 0 for s in active) or 1
        by_pf[pf] = {"mapped": len(mapped), "priced": len(priced), "rate": round(len(mapped) / n * 100, 1),
                     "demand_cover": round(sum(s["demand_daily"] or 0 for s in mapped) / dsum * 100, 1)}

    seller_skus: dict[tuple[str, str], set] = defaultdict(set)
    for s in active:
        for ln in s["lines"]:
            if ln["platform"]:
                seller_skus[(ln["platform"], ln["seller"])].add(s["product_code"])
    sc = _seller_sku_counts()
    sellers = []
    for (pf, sn), codes in seller_skus.items():
        sellers.append({"platform": pf, "seller": sn, "overlap": len(codes), "seller_total": sc.get((pf, sn))})
    for (pf, sn), cnt in sc.items():
        if pf in FOCUS_PLATFORMS and (pf, sn) not in seller_skus:
            sellers.append({"platform": pf, "seller": sn, "overlap": 0, "seller_total": cnt})
    sellers.sort(key=lambda x: (PLATFORMS.index(x["platform"]) if x["platform"] in PLATFORMS else 9, -x["overlap"]))

    focus_sellers = [x for x in sellers if x["platform"] in FOCUS_PLATFORMS]
    top_skus = sorted(active, key=lambda s: -(s["demand_daily"] or 0))[:max(10, min(top, 500))]
    matrix = []
    for s in top_skus:
        cells = {}
        for ln in s["lines"]:
            k = f"{ln['platform']}|{ln['seller']}"
            if ln["platform"] in FOCUS_PLATFORMS:
                prev = cells.get(k)
                if prev is None or VERDICT_RANK.get(ln["verdict"], 3) < VERDICT_RANK.get(prev, 3):
                    cells[k] = ln["verdict"]
        matrix.append({"product_code": s["product_code"], "product_name": s["product_name"],
                       "demand_daily": s["demand_daily"], "map_cnt": s["map_cnt"], "cells": cells})
    unmapped = sorted([s for s in active if not any(s["map_cnt"].get(pf) for pf in FOCUS_PLATFORMS)],
                      key=lambda s: -(s["demand_daily"] or 0))[:100]
    return _ok(_meta(U), active_cnt=len(active), by_platform=by_pf, sellers=sellers,
               focus_sellers=[{"platform": x["platform"], "seller": x["seller"]} for x in focus_sellers],
               matrix=matrix,
               unmapped=[{k: s[k] for k in ("product_code", "product_name", "demand_daily", "our_price", "category")}
                         for s in unmapped])


# ── 2-2 · 3-1. 매핑 ─────────────────────────────────────────────────────────

@router.get("/api/our-skus")
def api_our_skus(request: Request, keyword: str = "", unmapped_pf: str = "", status: str = "active",
                 limit: int = 100):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    kw = keyword.strip().lower()
    toks = [t for t in kw.split("*") if t] if "*" in kw else ([kw] if kw else [])
    rows = []
    for s in U["skus"]:
        if status and status != "all" and s["status"] != status:
            continue
        if unmapped_pf and s["map_cnt"].get(unmapped_pf):
            continue
        hay = f"{s['product_code']} {s['product_name']} {s['twin_code']} {s['twin_name']}".lower()
        if toks and not all(t in hay for t in toks):
            continue
        rows.append(s)
    rows.sort(key=lambda s: -(s["demand_daily"] or 0))
    return _ok(_meta(U), total=len(rows), rows=rows[:max(1, min(limit, 500))])


@router.post("/api/mapping/add")
async def api_mapping_add(request: Request):
    """3배치 SKU 기준 매핑 등록.
    [N] 쌍둥이(1배치 코드)가 있으면 기본적으로 1배치 코드에 등록 (기존 메뉴와 매핑 공유)."""
    sess = _session(request)
    body = await request.json()
    sku = str(body.get("sku_code") or "").strip()
    items = body.get("items") or []
    if not sku or not items:
        return JSONResponse({"ok": False, "error": "sku_code · items 필요"}, status_code=400)
    s = next((x for x in get_skus() if x["product_code"] == sku), None)
    if not s:
        return JSONResponse({"ok": False, "error": "4121 SKU가 아닙니다"}, status_code=400)
    target = str(body.get("target_code") or s["twin_code"] or sku).strip()
    if target not in {sku, s["twin_code"]}:
        return JSONResponse({"ok": False, "error": "등록 대상 코드는 3배치 코드 또는 [N] 1배치 코드만 가능"},
                            status_code=400)
    tag = body.get("tag") or "normal"
    try:
        mult = float(body.get("multiplier") or 1.0)
    except (TypeError, ValueError):
        mult = 1.0
    added = 0
    for it in items:
        pk = str(it.get("product_key") or "").strip()
        pf = str(it.get("platform") or "").strip()
        if not pk or pf not in PLATFORMS:
            continue
        portal_db.pm_add_mapping(
            our_product_code=target, plant="ALL", product_key=pk, platform=pf,
            platform_seller_id=str(it.get("platform_seller_id") or ""),
            platform_product_id=str(it.get("platform_product_id") or ""),
            product_name=s.get("product_name") or "", seller_name=it.get("seller_name") or "",
            created_by=sess.get("emp_code", ""), tag=tag,
            multiplier=float(it.get("multiplier") or mult),
        )
        added += 1
    invalidate("universe")
    return _ok(added=added, target_code=target)


@router.post("/api/mapping/remove")
async def api_mapping_remove(request: Request):
    _session(request)
    body = await request.json()
    ids = body.get("mapping_ids") or ([body.get("mapping_id")] if body.get("mapping_id") else [])
    for i in ids:
        portal_db.pm_deactivate_mapping(int(i))
    invalidate("universe")
    return _ok(removed=len(ids))


# ── 4-1. 셀러별 경쟁 현황 ───────────────────────────────────────────────────

def _seller_pairs(U: dict, platform: str = "") -> dict[tuple[str, str], list[tuple[dict, dict]]]:
    """(platform, seller) → [(sku, 최저 실수취가 라인)] — 운영 SKU만."""
    pairs: dict[tuple[str, str], dict[str, tuple[dict, dict]]] = defaultdict(dict)
    for s in U["skus"]:
        if s["status"] != "active":
            continue
        for ln in s["lines"]:
            if not ln["platform"] or (platform and ln["platform"] != platform) or ln["comp_net"] is None:
                continue
            key = (ln["platform"], ln["seller"])
            cur = pairs[key].get(s["product_code"])
            if cur is None or ln["comp_net"] < cur[1]["comp_net"]:
                pairs[key][s["product_code"]] = (s, ln)
    return {k: list(v.values()) for k, v in pairs.items()}


@router.get("/api/competition")
def api_competition(request: Request, platform: str = ""):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    P = U["params"]
    cm_floor = float(P["cm_floor_pct"])
    RATES = {pf: cost_rates(P, pf) for pf in PLATFORMS}
    sellers = []
    for (pf, sn), lst in _seller_pairs(U, platform).items():
        c = defaultdict(int)
        diffs, bleed, stake_lose, rev = [], 0, 0.0, 0.0
        for s, ln in lst:
            c[ln["verdict"]] += 1
            if ln["diff_pct"] is not None:
                diffs.append(ln["diff_pct"])
            cmp_cm = cm_pct_of(s["our_price"], s["buy_price"], RATES.get(pf) or cost_rates(P, pf))
            if ln["verdict"] == "win" and cmp_cm is not None and cmp_cm < cm_floor:
                bleed += 1
            amt = (s["demand_daily"] or 0) * (s["our_price"] or 0)
            rev += amt
            if ln["verdict"] == "lose":
                stake_lose += amt
        judged = c["win"] + c["tie"] + c["lose"]
        sellers.append({
            "platform": pf, "seller": sn, "overlap": len(lst),
            "win": c["win"], "tie": c["tie"], "lose": c["lose"], "unknown": c["unknown"],
            "win_rate": round(c["win"] / judged * 100, 1) if judged else None,
            "avg_diff": round(sum(diffs) / len(diffs), 1) if diffs else None,
            "bleed_win": bleed,
            "revenue_daily": round(rev), "stake_lose_daily": round(stake_lose),
        })
    sellers.sort(key=lambda x: (-(x["stake_lose_daily"] or 0), -x["overlap"]))
    kpi = {k: sum(x[k] for x in sellers) for k in ("overlap", "win", "tie", "lose", "bleed_win")}
    return _ok(_meta(U), sellers=sellers, kpi=kpi)


@router.get("/api/seller-detail")
def api_seller_detail(request: Request, platform: str, seller: str):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    lst = _seller_pairs(U, platform).get((platform, seller), [])
    rows = [{
        "product_code": s["product_code"], "product_name": s["product_name"], "tax_class": s["tax_class"],
        "our_price": s["our_price"], "buy_price": s["buy_price"], "gp_pct": s["gp_pct"],
        "demand_daily": s["demand_daily"], **{k: ln[k] for k in (
            "ext_name", "comp_price", "fee_pct", "multiplier", "tag", "comp_net", "diff_pct", "verdict", "match_gp")},
    } for s, ln in lst]
    rows.sort(key=lambda r: (VERDICT_RANK.get(r["verdict"], 3), -(r["demand_daily"] or 0)))
    return _ok(_meta(U), rows=rows)


# ── 4-2. 셀러 시뮬레이터 ────────────────────────────────────────────────────

@router.get("/api/sellers")
def api_sellers(request: Request):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    out = [{"platform": pf, "seller": sn, "overlap": len(lst)}
           for (pf, sn), lst in _seller_pairs(U).items()]
    out.sort(key=lambda x: (PLATFORMS.index(x["platform"]) if x["platform"] in PLATFORMS else 9, -x["overlap"]))
    return _ok(sellers=out)


@router.get("/api/sim-data")
def api_sim_data(request: Request, sellers: str = ""):
    """sellers = 'baemin|다봄푸드,todaysales|대상(주)_수도권(직배송)'"""
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    sel = [tuple(x.split("|", 1)) for x in sellers.split(",") if "|" in x]
    pairs = _seller_pairs(U)
    by_sku: dict[str, dict] = {}
    for key in sel:
        for s, ln in pairs.get(key, []):
            row = by_sku.setdefault(s["product_code"], {
                **{k: s[k] for k in ("product_code", "product_name", "our_price", "buy_price", "gp_pct",
                                     "tax_class", "demand_daily", "demand_plants", "twin_code", "category")},
                "comps": [],
            })
            row["comps"].append({"platform": key[0], "seller": key[1], "comp_net": ln["comp_net"],
                                 "comp_price": ln["comp_price"], "fee_pct": ln["fee_pct"],
                                 "multiplier": ln["multiplier"], "ext_name": ln["ext_name"]})
    rows = sorted(by_sku.values(), key=lambda r: -(r["demand_daily"] or 0))
    return _ok(_meta(U), rows=rows, sellers=[{"platform": a, "seller": b} for a, b in sel])


@router.get("/api/scenarios")
def api_scenarios(request: Request):
    _session(request)
    return _ok(rows=bt_db.list_scenarios())


@router.post("/api/scenarios")
async def api_scenario_save(request: Request):
    sess = _session(request)
    body = await request.json()
    name = (body.get("name") or "").strip()[:100]
    if not name:
        return JSONResponse({"ok": False, "error": "시나리오명 필요"}, status_code=400)
    sid = bt_db.save_scenario(name, body.get("platform") or "", body.get("sellers") or [],
                              body.get("params") or {}, body.get("summary") or {}, sess.get("emp_code", ""))
    return _ok(scenario_id=sid)


@router.post("/api/scenarios/delete")
async def api_scenario_delete(request: Request):
    _session(request)
    body = await request.json()
    bt_db.delete_scenario(int(body.get("scenario_id") or 0))
    return _ok()


# ── 5. 수익성 · 추천가 ──────────────────────────────────────────────────────

@router.get("/api/profit")
def api_profit(request: Request, platform: str = "baemin"):
    _session(request)
    if platform not in PLATFORMS:
        platform = "baemin"
    U, err = _universe_or_error()
    if err:
        return err
    P = U["params"]
    band = float(P["tie_band_pct"])
    cm_floor = float(P["cm_floor_pct"]) / 100
    R = cost_rates(P, platform)
    fee_our = R["fee"]
    rows = []
    for s in U["skus"]:
        if s["status"] != "active":
            continue
        our, buy = s["our_price"], s["buy_price"]
        vat = 1.1 if s["tax_class"] == "과세" else 1.0
        best = _best_by_platform(s).get(platform)
        comp = best["comp_net"] if best else None
        # 하한 = 공헌이익율(매출총이익 − 변동비) 기준
        floor = cm_floor_price(buy, R, cm_floor)
        win_price = round(comp * (1 - band / 100), 1) if comp else None
        tie_top = round(comp * (1 + band / 100), 1) if comp else None
        rec, note = None, ""
        if comp is None:
            note = "경쟁 없음"
        elif floor is None:
            note = "구매가 없음"
        elif win_price >= floor:
            rec, note = win_price, "승 가능"
        elif tie_top > floor:
            rec, note = floor, "하한 · 무승부권"
        else:
            rec, note = floor, "승 불가(하한 초과)"
        pl = pnl_of(our, buy, R)
        pl_rec = pnl_of(rec, buy, R)
        dem = s["demand_daily"] or 0
        rows.append({
            "product_code": s["product_code"], "product_name": s["product_name"], "category": s["category"],
            "tax_class": s["tax_class"], "our_price": our, "buy_price": buy, "gp_pct": s["gp_pct"],
            "gross_price": round(gross_of(our, fee_our), 1) if our else None,
            "gross_gp_pct": pl["gp_pct"] if pl else None,
            "var_pct": pl["var_pct"] if pl else None,
            "cm_pct": pl["cm_pct"] if pl else None,
            "fix_pct": pl["fix_pct"] if pl else None,
            "op_pct": pl["op_pct"] if pl else None,
            "pnl": pl,
            "list_price": round(list_of(our, fee_our, vat)) if our else None,
            "comp_list_price": round(best["comp_price"] / (best["multiplier"] or 1)) if best and best.get("comp_price") else None,
            "demand_daily": s["demand_daily"], "demand_plants": s["demand_plants"],
            "comp_net": comp, "comp_seller": best["seller"] if best else "",
            "verdict": best["verdict"] if best else "none", "diff_pct": best["diff_pct"] if best else None,
            "floor": floor, "win_price": win_price, "rec_price": rec,
            "rec_gp": pl_rec["gp_pct"] if pl_rec else None,
            "rec_cm": pl_rec["cm_pct"] if pl_rec else None,
            "rec_op": pl_rec["op_pct"] if pl_rec else None,
            "rec_list_price": round(list_of(rec, fee_our, vat)) if rec else None,
            "rec_change_pct": round((rec - our) / our * 100, 1) if rec and our else None,
            "note": note,
            "rev_daily": round(dem * (our or 0)),
            "gross_rev_daily": round(dem * (gross_of(our, fee_our) or 0)),
            "gp_daily": round(dem * pl["gp"]) if pl else None,
            "cm_daily": round(dem * pl["cm"]) if pl else None,
            "op_daily": round(dem * pl["op"]) if pl else None,
        })
    rows.sort(key=lambda r: -(r["rev_daily"] or 0))
    cat = defaultdict(lambda: {"cnt": 0, "rev": 0.0, "gross_rev": 0.0, "gp": 0.0,
                               "cm": 0.0, "op": 0.0, "lose": 0})
    for r in rows:
        c = cat[r["category"] or "(미분류)"]
        g = r["gross_rev_daily"] or 0
        c["cnt"] += 1
        c["rev"] += r["rev_daily"] or 0
        c["gross_rev"] += g
        c["gp"] += r["gp_daily"] or 0
        c["cm"] += r["cm_daily"] or 0
        c["op"] += r["op_daily"] or 0
        c["lose"] += 1 if r["verdict"] == "lose" else 0
    cats = sorted(({"category": k, **v,
                    "gp_pct": round(v["gp"] / v["gross_rev"] * 100, 1) if v["gross_rev"] else None,
                    "cm_pct": round(v["cm"] / v["gross_rev"] * 100, 1) if v["gross_rev"] else None,
                    "op_pct": round(v["op"] / v["gross_rev"] * 100, 1) if v["gross_rev"] else None}
                   for k, v in cat.items()), key=lambda x: -x["gross_rev"])
    return _ok(_meta(U), platform=platform, fee_our_pct=round(fee_our * 100, 2),
               rates={k: round(v * 100, 4) for k, v in R.items()}, rows=rows, categories=cats)


# ── 5-2. 상품별·플랫폼별 등록가 결정 ───────────────────────────────────────

@router.get("/api/listing")
def api_listing(request: Request):
    _session(request)
    U, err = _universe_or_error()
    if err:
        return err
    P = U["params"]
    D = _decided_map()
    rows = []
    for s in U["skus"]:
        if s["status"] != "active":
            continue
        best = _best_by_platform(s)
        rows.append({
            **{k: s.get(k) for k in ("product_code", "product_name", "twin_code", "twin_name", "category",
                                     "tax_class", "our_price", "buy_price", "gp_pct", "demand_daily", "map_cnt")},
            "best": {pf: best[pf] for pf in FOCUS_PLATFORMS if pf in best},
            "listing": listing_of(s, best, D.get(s["product_code"], {}), P),
        })
    rows.sort(key=lambda r: -(r["demand_daily"] or 0))
    return _ok(_meta(U), rows=rows, basis_labels=bt_db.LISTING_BASIS)


@router.post("/api/listing")
async def api_listing_save(request: Request):
    """items = [{product_code, platform, list_price(없으면 해제), basis, memo}]"""
    sess = _session(request)
    body = await request.json()
    valid = {s["product_code"] for s in get_skus()}
    saved, skipped = 0, 0
    for it in body.get("items") or []:
        code = str(it.get("product_code") or "").strip()
        pf = str(it.get("platform") or "").strip()
        if code not in valid or pf not in FOCUS_PLATFORMS:
            skipped += 1
            continue
        try:
            price = float(it.get("list_price") or 0)
        except (TypeError, ValueError):
            skipped += 1
            continue
        bt_db.set_listing_price(code, pf, round(price) if price > 0 else None,
                                str(it.get("basis") or "manual"), str(it.get("memo") or "")[:300],
                                sess.get("emp_code", ""))
        saved += 1
    return _ok(saved=saved, skipped=skipped)


# ── 6·7. 설정 ───────────────────────────────────────────────────────────────

@router.get("/api/params")
def api_params(request: Request):
    _session(request)
    return _ok(params=bt_db.get_params(), defaults=bt_db.DEFAULT_PARAMS,
               runs=bt_db.list_runs(24), daily=bt_db.daily_counts(14))


@router.post("/api/params")
async def api_params_save(request: Request):
    sess = _session(request)
    body = await request.json()
    P = bt_db.set_params(body.get("params") or {}, sess.get("emp_code", ""))
    invalidate("universe")
    return _ok(params=P)
