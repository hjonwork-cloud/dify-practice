"""외부 플랫폼 가격 모니터링 라우터 (배민상회/식봄 vs 우리 상품)."""
from __future__ import annotations

import os
import time
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

PLANTS = ["4120", "4123"]
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


# ── 기준가/구매가 캐시 (5분) ─────────────────────────────────────────────────
_price_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 300


def _get_base_prices(plant: str) -> list[dict]:
    """compat 최근 2주 평균 판매단가(기준가) + 평균 구매단가 (플랜트별)"""
    cache_key = f"base_prices_{plant}"
    if cache_key in _price_cache:
        ts, data = _price_cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data
    try:
        import main as _main
        rows = _q(f"""
            SELECT
                `상품코드`                                          AS product_code,
                ROUND(AVG(`단가`), 2)                              AS avg_sale_price,
                ROUND(
                    SUM(CAST(`매출원가` AS DOUBLE)) /
                    NULLIF(SUM(CAST(`매출수량` AS DOUBLE)), 0)
                , 2)                                               AS avg_buy_price
            FROM {_main.T_MAIN}
            WHERE `사업장` = '{plant}'
              AND `년월` >= DATE_FORMAT(DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY), '%Y%m')
              AND `상품코드` IS NOT NULL
              AND `매출수량` IS NOT NULL
              AND `매출원가` IS NOT NULL
            GROUP BY `상품코드`
        """)
        _price_cache[cache_key] = (time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] base_prices 조회 실패 ({plant}): {e}")
        return []


# ── 운영상품 목록 캐시 (5분) ─────────────────────────────────────────────────
_product_cache: dict[str, tuple[float, list]] = {}

T_ZSDR = "h_hmfo_fsi.gd_fsi_ent.sap_zsdr0017_order_linkage_status_d"
T_ZMM60 = "h_hmfo_fsi.gd_fsi_ent.sap_zmm60_material_master_d"
T_SILVER = "silver.dim_platform_products"


def _get_our_products(plant: str) -> list[dict]:
    """플랜트 기준 운영상품 목록 (SAP zsdr0017 + zmm60 JOIN, 전용상품 5140 제외)"""
    cache_key = f"products_{plant}"
    if cache_key in _product_cache:
        ts, data = _product_cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data
    try:
        rows = _q(f"""
            SELECT
                z.`상품코드`        AS product_code,
                m.`상품명`          AS product_name,
                m.`자재유형명`      AS brand,
                m.`단위`            AS unit,
                m.`자재그룹명`      AS product_group,
                m.`자재그룹`        AS material_group,
                z.`플랜트`          AS plant,
                COALESCE(z.`사용보류`, '') AS use_hold
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`플랜트` = '{plant}'
              AND COALESCE(m.`자재그룹`, '') != '5140'
            ORDER BY m.`상품명`
            LIMIT 2000
        """)
        _product_cache[cache_key] = (time.time(), rows)
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] our_products 조회 실패 ({plant}): {e}")
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
        return rows
    except Exception as e:
        logger.warning(f"[price_monitor] platform_latest 조회 실패: {e}")
        return []


def _calc_gp(platform_price: float | None, buy_price: float | None) -> float | None:
    """GP율 계산: (판매가 - 구매가) / 판매가 × 100"""
    if not platform_price or not buy_price:
        return None
    try:
        return round((platform_price - buy_price) / platform_price * 100, 1)
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
    plant: str = "4120",
    platform: str = "",
    alert_only: str = "",
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "4120"

    # 기준가/구매가
    price_rows = _get_base_prices(plant)
    price_map: dict[str, dict] = {
        r["product_code"]: r for r in price_rows
    }

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
        gp = _calc_gp(sale_price, buy_price)
        status = _gp_status(gp)
        if alert_only and status not in ("alert", "warn"):
            continue
        rows.append({
            "our_product_code":   p_code,
            "product_name":       m.get("product_name") or "",
            "platform":           pf_data.get("platform", ""),
            "seller_name":        pf_data.get("platform_seller_name", ""),
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
    plant: str = "4120",
    keyword: str = "",
    map_filter: str = "",  # "mapped" | "unmapped" | ""
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "4120"

    products = _get_our_products(plant)
    all_mappings = portal_db.pm_list_all_mappings(plant)
    # 상품코드별 매핑 수 집계
    mapping_count: dict[str, dict] = {}
    for m in all_mappings:
        c = m["our_product_code"]
        if c not in mapping_count:
            mapping_count[c] = {"baemin": 0, "foodspring": 0, "total": 0}
        mapping_count[c][m["platform"]] = mapping_count[c].get(m["platform"], 0) + 1
        mapping_count[c]["total"] += 1

    rows = []
    for p in products:
        code = p["product_code"]
        if keyword and keyword.lower() not in (p.get("product_name") or "").lower() \
                   and keyword not in code:
            continue
        mc = mapping_count.get(code, {"baemin": 0, "foodspring": 0, "total": 0})
        if map_filter == "mapped" and mc["total"] == 0:
            continue
        if map_filter == "unmapped" and mc["total"] > 0:
            continue
        rows.append({
            **p,
            "mapping_baemin":    mc["baemin"],
            "mapping_foodspring": mc["foodspring"],
            "mapping_total":     mc["total"],
            "is_stopped":        p.get("use_hold") == "X",
        })

    return _render(request, "pm_products.html",
                   rows=rows, plant=plant, plants=PLANTS,
                   keyword=keyword, map_filter=map_filter,
                   total=len(rows))


# ── 화면 3: 매핑 등록 ───────────────────────────────────────────────────────

@router.get("/mapping", response_class=HTMLResponse)
async def pm_mapping_page(request: Request, plant: str = "4120"):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "4120"
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
    return JSONResponse(result)


# ── API: 진단 (데이터소스 연결 확인) ─────────────────────────────────────

@router.get("/api/debug")
async def api_debug(request: Request):
    _require_pm_access(request)
    result = {}

    # 1. 우리 상품 테이블
    try:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {T_ZSDR} WHERE `플랜트`='4120' LIMIT 1")
        result["zsdr_count"] = rows[0]["cnt"] if rows else 0
        result["zsdr_ok"] = True
    except Exception as e:
        result["zsdr_ok"] = False
        result["zsdr_error"] = str(e)

    # 2. 자재마스터 테이블 + 컬럼 확인 + JOIN 테스트
    try:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {T_ZMM60} LIMIT 1")
        result["zmm60_count"] = rows[0]["cnt"] if rows else 0
        result["zmm60_ok"] = True
    except Exception as e:
        result["zmm60_ok"] = False
        result["zmm60_error"] = str(e)

    # 2-1. ZMM60 샘플 1건 (컬럼명 확인)
    try:
        rows = _q(f"SELECT * FROM {T_ZMM60} LIMIT 1")
        result["zmm60_columns"] = list(rows[0].keys()) if rows else []
        result["zmm60_sample"] = rows[0] if rows else {}
    except Exception as e:
        result["zmm60_columns_error"] = str(e)

    # 2-2. 상품코드로 JOIN 테스트
    try:
        rows = _q(f"""
            SELECT z.`상품코드`, m.`자재내역`, m.`기본단위`
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`플랜트` = '4120'
            LIMIT 3
        """)
        result["join_test_by_상품코드"] = rows
        result["join_test_ok"] = True
    except Exception as e:
        result["join_test_ok"] = False
        result["join_test_error"] = str(e)

    # 3. 플랫폼 상품 테이블 (크롤링 결과)
    try:
        rows = _q(f"SELECT MAX(crawl_date) AS max_date, COUNT(*) AS cnt FROM {T_SILVER}")
        result["silver_count"] = rows[0]["cnt"] if rows else 0
        result["silver_max_date"] = str(rows[0]["max_date"]) if rows else None
        result["silver_ok"] = True
    except Exception as e:
        result["silver_ok"] = False
        result["silver_error"] = str(e)

    # 4. compat 테이블 (기준가)
    try:
        import main as _main
        rows = _q(f"SELECT MAX(`년월`) AS max_ym FROM {_main.T_MAIN} WHERE `사업장`='4120' LIMIT 1")
        result["compat_max_ym"] = rows[0]["max_ym"] if rows else None
        result["compat_ok"] = True
    except Exception as e:
        result["compat_ok"] = False
        result["compat_error"] = str(e)

    return JSONResponse(result)


# ── API: 우리 상품 검색 (AJAX) ────────────────────────────────────────────

@router.get("/api/our-products")
async def api_our_products(request: Request, plant: str = "4120", keyword: str = ""):
    _require_pm_access(request)
    error_msg = None
    products = []
    try:
        rows = _q(f"""
            SELECT
                z.`상품코드`        AS product_code,
                m.`상품명`          AS product_name,
                m.`자재유형명`      AS brand,
                m.`단위`            AS unit,
                m.`자재그룹명`      AS product_group,
                m.`자재그룹`        AS material_group,
                z.`플랜트`          AS plant,
                COALESCE(z.`사용보류`, '') AS use_hold
            FROM {T_ZSDR} z
            LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
            WHERE z.`플랜트` = '{plant}'
              AND COALESCE(m.`자재그룹`, '') != '5140'
            ORDER BY m.`상품명`
            LIMIT 2000
        """)
        products = rows or []
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"[price_monitor] our_products 조회 실패 ({plant}): {e}")
    kw = keyword.lower()
    if kw and products:
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
        safe_kw = keyword.replace("'", "''")
        rows = _q(f"""
            SELECT p.*
            FROM {T_SILVER} p
            CROSS JOIN (SELECT MAX(crawl_date) AS max_date FROM {T_SILVER}) latest
            WHERE p.product_name LIKE '%{safe_kw}%' AND p.crawl_date = latest.max_date
            ORDER BY platform, platform_seller_name, price_sale
            LIMIT 500
        """) or []
    except Exception as e:
        error_msg = str(e)
        logger.warning(f"[price_monitor] platform_products 조회 실패: {e}")
    if platform and rows:
        rows = [r for r in rows if r.get("platform") == platform]
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
    if plant not in PLANTS:
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
async def api_mapping_list(request: Request, our_product_code: str, plant: str = "4120"):
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
    plant: str = "4120",
    days: int = 30,
):
    _require_pm_access(request)
    if plant not in PLANTS:
        plant = "4120"

    mappings = portal_db.pm_list_mappings(product_code, plant)
    product_keys = [m["product_key"] for m in mappings]

    history_rows: list[dict] = []
    if product_keys:
        try:
            keys_str = ", ".join(f"'{k}'" for k in product_keys)
            cutoff = f"DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)"
            history_rows = _q(f"""
                SELECT
                    product_key, platform, platform_seller_name,
                    product_name, spec, price_sale, price_original,
                    delivery_type, is_free_delivery, crawl_date
                FROM {T_SILVER}
                WHERE product_key IN ({keys_str})
                  AND crawl_date >= {cutoff}
                ORDER BY crawl_date DESC, platform, platform_seller_name
            """)
        except Exception as e:
            logger.warning(f"[price_monitor] history 조회 실패: {e}")

    # 가격 기준값
    price_rows = _get_base_prices(plant)
    price_map = {r["product_code"]: r for r in price_rows}
    price_info = price_map.get(product_code, {})

    # GP 계산 추가
    for row in history_rows:
        gp = _calc_gp(row.get("price_sale"), price_info.get("avg_buy_price"))
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


# ── 화면 5: 매핑 수정요청 (DELETE/REPLACE) ────────────────────────────────

@router.get("/change-request/{product_code}", response_class=HTMLResponse)
async def pm_change_request_page(
    request: Request,
    product_code: str,
    plant: str = "4120",
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
