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
_ALLOWED_EMP_CODES: set[str] = {access_control.ADMIN_EMP_CODE}

PLANTS = ["ALL", "4120", "4123", "4121"]
PLANT_LABELS = {"ALL": "전체센터", "4120": "4120(시화)", "4123": "4123(화성)", "4121": "4121(화성3배치)"}
PLANTS_REAL = ["4120", "4123", "4121"]  # ALL 제외 실제 플랜트
GP_ALERT_PCT = 10.0   # GP < 10% → 경보
GP_WARN_PCT  = 20.0   # GP < 20% → 주의

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
    """date/datetime 등 JSON 비직렬화 타입을 str로 변환."""
    import datetime
    result = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, (datetime.date, datetime.datetime)):
                new_row[k] = v.isoformat()
            else:
                new_row[k] = v
        result.append(new_row)
    return result


# ── 기준가/구매가 캐시 (5분) ─────────────────────────────────────────────────
_price_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 300


def _get_base_prices(plant: str) -> list[dict]:
    """최근 2주 기준가(매출액/매출수량) + 구매단가(매출원가/매출수량) (플랜트별)"""
    cache_key = f"base_prices_{plant}"
    if cache_key in _price_cache:
        ts, data = _price_cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data
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
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] base_prices 조회 실패 ({plant}): {e}")
        return []


# ── 전월 매출 캐시 (1시간) ───────────────────────────────────────────────────
_prev_sales_cache: dict[str, tuple[float, list]] = {}


def _get_prev_month_sales(plant: str) -> dict[str, dict]:
    """전월(1개월 전) 상품별 매출액·수량 합계 반환 (product_code → dict)"""
    cache_key = f"prev_sales_{plant}"
    if cache_key in _prev_sales_cache:
        ts, data = _prev_sales_cache[cache_key]
        if time.time() - ts < 3600:
            return data
    try:
        import main as _main
        plant_cond_ps = (f"`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
                         if plant == 'ALL' else f"`플랜트` = '{plant}'")
        rows = _q(f"""
            SELECT
                `자재`                                   AS product_code,
                SUM(CAST(`매출액`   AS DOUBLE)) * 100    AS prev_sales_amt,
                SUM(CAST(`매출수량` AS DOUBLE))          AS prev_sales_qty
            FROM {_main.T_MAIN}
            WHERE {plant_cond_ps}
              AND CAST(`년월` AS INT) = CAST(DATE_FORMAT(ADD_MONTHS(CURRENT_DATE(), -1), 'yyyyMM') AS INT)
              AND `자재` IS NOT NULL
              AND `매출수량` > 0
            GROUP BY `자재`
        """)
        result = {r["product_code"]: r for r in (rows or [])}
        _prev_sales_cache[cache_key] = (time.time(), result)
        return result
    except Exception as e:
        logger.warning(f"[price_monitor] prev_month_sales 조회 실패 ({plant}): {e}")
        return {}


# ── 운영상품 목록 캐시 (1시간) ─────────────────────────────────────────────
_product_cache: dict[str, tuple[float, list]] = {}
_PRODUCT_CACHE_TTL = 3600  # 1시간

T_ZSDR  = "h_hmfo_fsi.gd_fsi_ent.sap_zsdr0017_order_linkage_status_d"
T_ZMM60 = "h_hmfo_fsi.gd_fsi_ent.sap_zmm60_material_master_d"
T_SILVER = "h_hmfo_fsi_dm.gd_rst_ing.dim_platform_products"


def _preload_products_background():
    """앱 시작 시 백그라운드에서 양쪽 플랜트 상품 목록을 미리 로드."""
    def _load():
        import time as _t
        _t.sleep(5)  # 앱 startup 완료 후 실행
        for plant in PLANTS:
            try:
                _get_our_products(plant)
                _get_our_products_with_batch(plant)
                logger.info(f"[price_monitor] preload 완료: plant={plant}")
            except Exception as e:
                logger.warning(f"[price_monitor] preload 실패 ({plant}): {e}")
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
    if cache_key in _product_cache:
        ts, data = _product_cache[cache_key]
        if time.time() - ts < _PRODUCT_CACHE_TTL:
            return data
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
            LIMIT 30000
        """)
        _product_cache[cache_key] = (time.time(), rows)
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
            group_by_wb = "z.`상품코드`, z.`배치`"
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
            LIMIT 30000
        """)
        _product_cache[cache_key] = (time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] our_products_with_batch 조회 실패 ({plant}): {e}")
        return []


def _get_platform_latest(product_keys: list[str] | None = None,
                          keyword: str = "") -> list[dict]:
    """silver.dim_platform_products 최신 가격 조회"""
    try:
        if product_keys is not None:
            if not product_keys:
                return []
            keys_str = ", ".join(f"'{k}'" for k in product_keys)
            where = f"WHERE product_key IN ({keys_str}) AND crawl_date = latest.max_date"
        elif keyword:
            safe_kw = keyword.replace("'", "''")
            where = f"WHERE product_name LIKE '%{safe_kw}%' AND crawl_date = latest.max_date"
        else:
            where = "WHERE crawl_date = latest.max_date"

        rows = _q(f"""
            SELECT p.*
            FROM {T_SILVER} p
            CROSS JOIN (SELECT MAX(crawl_date) AS max_date FROM {T_SILVER}) latest
            {where}
            ORDER BY platform, platform_seller_name, price_sale
            LIMIT 500
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
             platform: str = "", seller_name: str = "") -> float | None:
    """
    수수료 차감 후 GP율 계산.
    - 직배송: 외부판매가 × (1 - 0.066) / 1.1 = 수취액 A
    - 싱싱배송: 외부판매가 × (1 - 0.171) / 1.1 = 수취액 A
    - CJ프레시웨이(식봄): 외부판매가 × (1 - 0.048) / 1.1 = 수취액 A
    - GP% = (A - 구매단가) / A × 100
    """
    if not platform_price or not buy_price:
        return None
    try:
        fee = _get_fee(delivery_type, platform, seller_name)
        a = platform_price * (1.0 - fee) / 1.1   # 수수료 차감 후 부가세(10%) 제외
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
    price_map: dict[str, dict] = {
        r["product_code"]: r for r in price_rows
    }

    # 우리 상품명 맵 (T_ZMM60 기준) — miss 시 T_ZMM60 직접 fallback
    our_name_map: dict[str, str] = {
        p["product_code"]: p["product_name"]
        for p in _get_our_products(plant)
        if p.get("product_name")
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

    # 전월 매출 데이터
    prev_sales_map = _get_prev_month_sales(plant)

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
    platform_map: dict[str, dict] = {r["product_key"]: r for r in platform_rows}

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
        gp = _calc_gp(sale_price, buy_price, delivery_type, _platform, _seller_name)
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
    mapping_count: dict[str, dict] = {}
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

        ps = prev_sales.get(code, {})
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
    # 4120 플랜트 전체 건수
    try:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {T_ZSDR} WHERE `플랜트`='4120'")
        result["zsdr_4120_total"] = rows[0]["cnt"] if rows else 0
    except Exception as e:
        result["zsdr_4120_count_error"] = str(e)
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
    error_msg = None
    try:
        products = _get_our_products(plant)  # 캐시 사용
    except Exception as e:
        error_msg = str(e)
        products = []
    if keyword and products:
        if '*' in keyword:
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
    return JSONResponse({"data": products[:100], "error": error_msg, "total_before_filter": len(products)})


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
        rows = _q(f"""
            SELECT p.*
            FROM {T_SILVER} p
            CROSS JOIN (SELECT MAX(crawl_date) AS max_date FROM {T_SILVER}) latest
            WHERE ({like_clause}) AND p.crawl_date = latest.max_date
            ORDER BY platform, platform_seller_name, price_sale
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

    history_rows: list[dict] = []
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

    # GP 계산 추가
    for row in history_rows:
        gp = _calc_gp(
            row.get("price_sale"),
            price_info.get("avg_buy_price"),
            row.get("delivery_type", "직배송"),
            row.get("platform", ""),
            row.get("platform_seller_name", ""),
        )
        row["gp_pct"] = gp
        row["gp_status"] = _gp_status(gp)
        row["crawl_date"] = str(row.get("crawl_date", ""))

    # 제품명
    our_products = _get_our_products(plant)
    product_info = next((p for p in our_products if p["product_code"] == product_code), {})

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

    # 매핑된 product_keys
    mappings     = portal_db.pm_list_mappings(product_code, plant)
    product_keys = [m["product_key"] for m in mappings]

    # 오늘(최신) 플랫폼 가격 – 셀러별
    today_rows: list[dict] = []
    history_rows: list[dict] = []
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
        gp = _calc_gp(row.get("price_sale"), buy_price, _dt, _pf, _sn)
        row["gp_pct"]    = gp
        row["gp_status"] = _gp_status(gp)
        row["crawl_date"] = str(row.get("crawl_date", ""))
        fee = _get_fee(_dt, _pf, _sn)
        row["fee_pct"] = round(fee * 100, 1)
        # 실판매가 (수수료 제외 쫐정)
        ps = row.get("price_sale")
        row["net_price"] = round(ps * (1 - fee), 0) if ps else None

    # 중복 제거: (platform, seller_name, spec, price_sale) 동일 행 하나만 표시
    _seen: set = set()
    _deduped: list[dict] = []
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
    market_min_gp = _calc_gp(market_min, buy_price, "직배송") if market_min else None

    # 과세여부 및 VAT 포함 판매가
    our_products = _get_our_products(plant)
    product_info_meta = next((p for p in our_products if p["product_code"] == product_code), {})
    tax_status = product_info_meta.get("tax_class", "과세") or "과세"
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
    chart_dates: list[str] = sorted({str(r["crawl_date"]) for r in history_rows})
    seller_keys: list[str] = sorted({f"{r['platform']}|{r['platform_seller_name']}" for r in history_rows})
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
        _by_date: dict = _defaultdict(list)
        for r in pf_rows:
            if r["min_price"] is not None:
                _by_date[str(r["crawl_date"])].append(float(r["min_price"]))
        for stat, fn, dash in [("최저", min, None), ("평균", lambda v: sum(v)/len(v), [5,3]), ("최고", max, [2,2])]:
            data = [round(fn(_by_date[d]), 0) if _by_date.get(d) else None for d in chart_dates]
            ds: dict = {
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
    _seen_pairs: set = set()
    _deduped: list = []
    for _m in all_mappings:
        _pair = (_m["our_product_code"], _m["product_key"])
        if _pair not in _seen_pairs:
            _seen_pairs.add(_pair)
            _deduped.append(_m)
    all_mappings = _deduped

    price_rows  = _get_base_prices(plant)
    price_map: dict[str, dict] = {r["product_code"]: r for r in price_rows}

    # 상품명 맵
    our_name_map: dict[str, str] = {
        p["product_code"]: p["product_name"]
        for p in _get_our_products(plant)
        if p.get("product_name")
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
    platform_map: dict[str, dict] = {r["product_key"]: r for r in platform_rows}

    # ── 셀러별 그룹화 ──
    # key: (platform, seller_name)
    from collections import defaultdict
    seller_groups: dict[tuple, list] = defaultdict(list)
    crawl_date = ""

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

        if not crawl_date:
            crawl_date = str(pf_data.get("crawl_date", ""))

        # ── 경쟁사 실수취가: 소비자가에서 수수료·VAT 제거 ──
        # comp_net = price_sale × (1 − fee) / 1.1
        fee = _get_fee(delivery_type, platform, seller_name)
        if comp_price:
            comp_net = round(float(comp_price) * (1.0 - fee) / 1.1, 0)
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

        seller_groups[(platform, seller_name, seller_id)].append({
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
        })

    # ── 셀러 요약 빌드 ──
    sellers = []
    for (platform, seller_name, seller_id), items in seller_groups.items():
        win  = sum(1 for i in items if i["status"] == "win")
        tie  = sum(1 for i in items if i["status"] == "tie")
        lose = sum(1 for i in items if i["status"] == "lose")
        total_items = len(items)
        win_rate = round(win / total_items * 100) if total_items else 0

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

    return JSONResponse({"crawl_date": crawl_date, "kpi": kpi, "sellers": sellers})


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




