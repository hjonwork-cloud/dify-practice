"""영업사원 액션 제안 포털 라우터."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import portal_db

router = APIRouter(prefix="/portal", tags=["sales-portal"])
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(("html", "xml")),
)

_SESSION_COOKIE = "dongwon_portal_session"
_SESSION_MAX_AGE = 60 * 60 * 10
_SESSION_SECRET = os.getenv("PORTAL_SESSION_SECRET", "dongwon-portal-dev-secret-change-me")
_ALLOWED_EMP_CODE = "20230720"
_ALLOWED_EMP_NAME = "이 충규"
_ALLOWED_TEAM = "외식3팀"

_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300


def _asset_version() -> str:
    try:
        mtimes = [(_STATIC_DIR / "admin.css").stat().st_mtime, (_STATIC_DIR / "dongwon-homefood-logo.png").stat().st_mtime]
        return str(int(max(mtimes)))
    except OSError:
        return "1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_session(emp_code: str) -> str:
    exp = int(time.time()) + _SESSION_MAX_AGE
    nonce = secrets.token_urlsafe(12)
    payload = f"{emp_code}|{exp}|{nonce}"
    return f"{_b64(payload.encode('utf-8'))}.{_sign(payload)}"


def _read_session(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode("utf-8")
        emp_code, exp_raw, _nonce = payload.split("|", 2)
        if int(exp_raw) < int(time.time()):
            return None
        if not secrets.compare_digest(sig, _sign(payload)):
            return None
        return emp_code
    except Exception:
        return None


async def _read_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _current_user(request: Request) -> dict | None:
    emp_code = _read_session(request.cookies.get(_SESSION_COOKIE))
    if emp_code != _ALLOWED_EMP_CODE:
        return None
    return {"emp_code": _ALLOWED_EMP_CODE, "name": _ALLOWED_EMP_NAME, "team": _ALLOWED_TEAM}


def _require_user(request: Request) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="포털 로그인이 필요합니다.")
    return user


def _render(request: Request, name: str, **context) -> HTMLResponse:
    html = _jinja_env.get_template(name).render({
        "request": request,
        "user": _current_user(request),
        "asset_v": _asset_version(),
        **context,
    })
    return HTMLResponse(html)


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _redirect_msg(path: str, **params: str) -> RedirectResponse:
    if params:
        query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items())
        path = f"{path}?{query}"
    return _redirect(path)


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _cache_set(key: str, value):
    _cache[key] = (time.time(), value)
    return value


def _sql(value: str) -> str:
    import main
    return "'" + main._sql_literal(str(value or "")) + "'"


def _q(sql: str) -> list[dict]:
    import main
    return main._safe_query(sql)


def _money_m(value) -> int:
    """compat 매출금액을 챗봇 기준 백만원 단위로 변환.

    compat 뷰의 `매출액`은 기존 챗봇 매출 카드에서
    SUM(`매출액`) / 1,000,000 값을 '억원성 값'으로 보고 다시 100을 곱해
    백만원으로 표시해 왔다. 즉 백만원 표시는 SUM(`매출액`) / 10,000 기준이다.
    """
    try:
        return int(round(float(value or 0) / 10_000))
    except Exception:
        return 0


def _won_m(value) -> int:
    """원 단위 금액을 백만원 단위로 변환."""
    try:
        return int(round(float(value or 0) / 1_000_000))
    except Exception:
        return 0


def _pct(value) -> float:
    try:
        return round(float(value or 0) * 100, 1)
    except Exception:
        return 0.0


def _month_shift(ym: str, delta: int) -> str:
    y, m = int(ym[:4]), int(ym[4:6]) + delta
    while m <= 0:
        y -= 1
        m += 12
    while m > 12:
        y += 1
        m -= 12
    return f"{y}{m:02d}"


def _period_months(latest_ym: str, months: int = 3) -> list[str]:
    return [_month_shift(latest_ym, -i) for i in range(months - 1, -1, -1)]


def _in_months(months: list[str]) -> str:
    return ", ".join(f"'{m}'" for m in months)


def _latest_ym(emp_code: str = _ALLOWED_EMP_CODE) -> str:
    cached = _cache_get(f"latest:{emp_code}")
    if cached:
        return str(cached)
    import main
    rows = _q(f"""
        SELECT MAX(`년월`) AS ym
        FROM {main.T_MAIN}
        WHERE `영업사원` = {_sql(emp_code)}
          AND `매출액` IS NOT NULL
    """)
    ym = str((rows[0] or {}).get("ym") or "") if rows else ""
    return _cache_set(f"latest:{emp_code}", ym)


def _latest_bill_date(emp_code: str = _ALLOWED_EMP_CODE, ym: str = "") -> str:
    cached = _cache_get(f"billdate:{emp_code}:{ym}")
    if cached:
        return str(cached)
    import main
    where_ym = f"AND `년월` = {_sql(ym)}" if ym else ""
    rows = _q(f"""
        SELECT MAX(`대금청구일`) AS bill_date
        FROM {main.T_MAIN}
        WHERE `영업사원` = {_sql(emp_code)}
          {where_ym}
    """)
    bill_date = str((rows[0] or {}).get("bill_date") or "") if rows else ""
    return _cache_set(f"billdate:{emp_code}:{ym}", bill_date)


def _profit_latest_ym(emp_code: str = _ALLOWED_EMP_CODE) -> str:
    cached = _cache_get(f"profit_latest:{emp_code}")
    if cached:
        return str(cached)
    import main
    rows = _q(f"""
        WITH my_customers AS (
            SELECT DISTINCT `거래처`
            FROM {main.T_MAIN}
            WHERE `영업사원` = {_sql(emp_code)}
        )
        SELECT MAX(DATE_FORMAT(p.`날짜`, 'yyyyMM')) AS ym
        FROM {main.T_PROFIT} p
        INNER JOIN my_customers c ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(c.`거래처` AS STRING))
    """)
    ym = str((rows[0] or {}).get("ym") or "") if rows else ""
    return _cache_set(f"profit_latest:{emp_code}", ym)


def _brand_rows(emp_code: str = _ALLOWED_EMP_CODE) -> list[dict]:
    cached = _cache_get(f"brands:{emp_code}")
    if cached is not None:
        return cached
    import main
    latest = _latest_ym(emp_code)
    if not latest:
        return []
    rows = _q(f"""
        WITH my_brands AS (
            SELECT DISTINCT COALESCE(`ZC본부`, '') AS brand_code,
                            COALESCE(`ZC본부명`, '미분류') AS brand_name
            FROM {main.T_MAIN}
            WHERE `영업사원` = {_sql(emp_code)}
              AND `년월` = {_sql(latest)}
              AND COALESCE(`ZC본부명`, '') <> ''
        ),
        brand_all AS (
            SELECT
                COALESCE(`ZC본부`, '') AS brand_code,
                COALESCE(`ZC본부명`, '미분류') AS brand_name,
                SUM(`매출액`) AS sales,
                COUNT(DISTINCT `거래처`) AS customer_count,
                SUM(CASE WHEN COALESCE(`자재그룹명`, '') = 'FC전용상품' THEN `매출액` ELSE 0 END) AS dedicated_sales,
                SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END) AS generic_sales,
                SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) AS classified_sales
            FROM {main.T_MAIN}
            WHERE `년월` = {_sql(latest)}
              AND COALESCE(`ZC본부명`, '') <> ''
            GROUP BY COALESCE(`ZC본부`, ''), COALESCE(`ZC본부명`, '미분류')
        ),
        my_sales AS (
            SELECT
                COALESCE(`ZC본부`, '') AS brand_code,
                COALESCE(`ZC본부명`, '미분류') AS brand_name,
                SUM(`매출액`) AS my_sales,
                COUNT(DISTINCT `거래처`) AS my_customer_count
            FROM {main.T_MAIN}
            WHERE `영업사원` = {_sql(emp_code)}
              AND `년월` = {_sql(latest)}
              AND COALESCE(`ZC본부명`, '') <> ''
            GROUP BY COALESCE(`ZC본부`, ''), COALESCE(`ZC본부명`, '미분류')
        )
        SELECT
            a.brand_code,
            a.brand_name,
            a.sales,
            a.customer_count,
            a.dedicated_sales,
            a.generic_sales,
            a.classified_sales,
            COALESCE(ms.my_sales, 0) AS my_sales,
            COALESCE(ms.my_customer_count, 0) AS my_customer_count
        FROM brand_all a
        INNER JOIN my_brands b ON a.brand_code = b.brand_code AND a.brand_name = b.brand_name
        LEFT JOIN my_sales ms ON a.brand_code = ms.brand_code AND a.brand_name = ms.brand_name
        WHERE a.sales <> 0
        ORDER BY a.sales DESC
        LIMIT 50
    """)
    out = []
    for r in rows:
        sales = float(r.get("sales") or 0)
        classified = float(r.get("classified_sales") or 0)
        out.append({
            **r,
            "sales_m": _money_m(sales),
            "my_sales_m": _money_m(r.get("my_sales")),
            "generic_ratio": _pct((float(r.get("generic_sales") or 0) / classified) if classified else 0),
        })
    return _cache_set(f"brands:{emp_code}", out)


def portal_dashboard(emp_code: str = _ALLOWED_EMP_CODE) -> dict:
    cached = _cache_get(f"dashboard:{emp_code}")
    if cached is not None:
        return cached
    import main
    latest = _latest_ym(emp_code)
    brands = _brand_rows(emp_code)
    summary = {}
    latest_bill_date = _latest_bill_date(emp_code, latest) if latest else ""
    if latest:
        rows = _q(f"""
            SELECT SUM(`매출액`) AS sales,
                   COUNT(DISTINCT `거래처`) AS customers,
                   COUNT(DISTINCT `ZC본부명`) AS brands
            FROM {main.T_MAIN}
            WHERE `영업사원` = {_sql(emp_code)}
              AND `년월` = {_sql(latest)}
        """)
        summary = rows[0] if rows else {}
    profit_ym = _profit_latest_ym(emp_code)
    cm_rate = 0.0
    try:
        rows = _q(f"""
            WITH my_customers AS (
                SELECT DISTINCT `거래처`
                FROM {main.T_MAIN}
                WHERE `영업사원` = {_sql(emp_code)}
            )
            SELECT CASE WHEN SUM(p.`FI매출액`) = 0 THEN 0
                        ELSE SUM(p.`공헌이익`) / SUM(p.`FI매출액`) END AS cm_rate
            FROM {main.T_PROFIT} p
            INNER JOIN my_customers c ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(c.`거래처` AS STRING))
            WHERE DATE_FORMAT(p.`날짜`, 'yyyyMM') = {_sql(profit_ym)}
        """)
        cm_rate = _pct((rows[0] or {}).get("cm_rate")) if rows else 0.0
    except Exception:
        cm_rate = 0.0
    ar_balance = 0
    try:
        rows = _q(f"""
            SELECT SUM(`현재잔액`) AS balance
            FROM {main.T_AR}
            WHERE `영업사원` = {_sql(emp_code)}
              AND `년월` = {_sql(latest)}
        """)
        ar_balance = _won_m((rows[0] or {}).get("balance")) if rows else 0
    except Exception:
        ar_balance = 0
    data = {
        "latest_ym": latest,
        "latest_bill_date": latest_bill_date,
        "profit_ym": profit_ym,
        "period_months": [latest] if latest else [],
        "sales_m": _money_m(summary.get("sales")),
        "customer_count": int(summary.get("customers") or 0),
        "brand_count": int(summary.get("brands") or 0),
        "cm_rate": cm_rate,
        "ar_balance_m": ar_balance,
        "brands": brands,
    }
    return _cache_set(f"dashboard:{emp_code}", data)


def _pick_brand(brand_name: str | None, emp_code: str = _ALLOWED_EMP_CODE) -> dict | None:
    brands = _brand_rows(emp_code)
    if not brands:
        return None
    if brand_name:
        for b in brands:
            if b.get("brand_name") == brand_name or b.get("brand_code") == brand_name:
                return b
    for b in brands:
        if "생활맥주" in str(b.get("brand_name") or ""):
            return b
    return brands[0]


def _recommend_products(brand_name: str, customer_code: str, months: list[str]) -> list[dict]:
    import main
    rows = _q(f"""
        WITH target_products AS (
            SELECT DISTINCT `자재`
            FROM {main.T_MAIN}
            WHERE `영업사원` = {_sql(_ALLOWED_EMP_CODE)}
              AND `ZC본부명` = {_sql(brand_name)}
              AND `거래처` = {_sql(customer_code)}
              AND `년월` IN ({_in_months(months)})
              AND `자재그룹명` IS NOT NULL
              AND COALESCE(`자재그룹명`, '') <> 'FC전용상품'
        )
        SELECT `자재` AS product_code,
               MAX(`자재명`) AS product_name,
               COUNT(DISTINCT `거래처`) AS adopter_count,
               SUM(`매출액`) AS sales,
               CASE WHEN SUM(`매출액`) = 0 THEN 0
                    ELSE (SUM(`매출액`) - SUM(COALESCE(`매출원가`, 0))) / SUM(`매출액`) END AS gp_rate
        FROM {main.T_MAIN}
        WHERE `ZC본부명` = {_sql(brand_name)}
          AND `년월` IN ({_in_months(months)})
          AND `자재그룹명` IS NOT NULL
          AND COALESCE(`자재그룹명`, '') <> 'FC전용상품'
          AND `거래처` <> {_sql(customer_code)}
          AND `자재` NOT IN (SELECT `자재` FROM target_products)
        GROUP BY `자재`
        HAVING SUM(`매출액`) > 0
        ORDER BY adopter_count DESC, gp_rate DESC, sales DESC
        LIMIT 5
    """)
    return [{**r, "sales_m": _money_m(r.get("sales")), "gp_pct": _pct(r.get("gp_rate"))} for r in rows]


def _dm_message(brand_name: str, customer: dict, brand_avg: float, products: list[dict]) -> str:
    product_lines = "\n".join(f"• {p.get('product_name') or p.get('product_code')}" for p in products[:5]) or "• 추천 후보 상품 확인 필요"
    ratio = customer.get("generic_ratio", 0)
    return (
        f"안녕하세요, {customer.get('customer_name')} 사장님.\n\n"
        f"최근 {brand_name} 가맹점의 범용상품 평균 사용률은 약 {brand_avg:.1f}%인데, "
        f"{customer.get('customer_name')}은 현재 {ratio:.1f}% 수준으로 확인됩니다.\n\n"
        "동일 브랜드 내 다른 가맹점에서 사용 빈도가 높은 상품 중 아직 주문이 없는 품목이 있어 추천드립니다.\n\n"
        f"추천 품목\n{product_lines}\n\n"
        "해당 상품은 다른 가맹점에서 꾸준히 사용 중인 품목으로, 메뉴 운영 안정화와 원가 개선 관점에서 검토해보시면 좋겠습니다."
    )


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _page_items(items: list[dict], page: int, per_page: int = 10) -> tuple[list[dict], dict]:
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    current = min(max(1, int(page or 1)), total_pages)
    start = (current - 1) * per_page
    return items[start:start + per_page], {
        "page": current,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": current > 1,
        "has_next": current < total_pages,
        "prev_page": max(1, current - 1),
        "next_page": min(total_pages, current + 1),
        "start": start + 1 if total else 0,
        "end": min(total, start + per_page),
    }


def brand_report(
    brand_name: str | None = None,
    emp_code: str = _ALLOWED_EMP_CODE,
    threshold_pct: float | None = None,
    customer_page: int = 1,
    target_page: int = 1,
) -> dict:
    picked = _pick_brand(brand_name, emp_code)
    if not picked:
        return {
            "brand": None,
            "brands": [],
            "customers": [],
            "targets": [],
            "latest_ym": "",
            "period_months": [],
            "brand_avg": 0,
            "threshold": 0,
            "threshold_max": 0,
            "customer_page": [],
            "target_page": [],
            "customer_pagination": {},
            "target_pagination": {},
            "target_count": 0,
            "proposal_possible_sales_m": 0,
            "generic_gp_rate": 0,
            "expected_profit_increase_m": 0,
        }
    import main
    latest = _latest_ym(emp_code)
    months = _period_months(latest, 3)
    prev_ym = _month_shift(latest, -1) if latest else ""
    bname = str(picked.get("brand_name") or "")
    monthly_rows = _q(f"""
        SELECT `년월` AS ym,
               SUM(`매출액`) AS sales
        FROM {main.T_MAIN}
        WHERE `ZC본부명` = {_sql(bname)}
          AND `년월` IN ({_in_months(months)})
        GROUP BY `년월`
        ORDER BY `년월`
    """) if months else []
    monthly_sales = [{**r, "sales_m": _money_m(r.get("sales"))} for r in monthly_rows]
    current_month_sales_m = next((int(r.get("sales_m") or 0) for r in monthly_sales if str(r.get("ym")) == latest), 0)
    avg_rows = _q(f"""
           SELECT CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0
                    ELSE SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                        / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) END AS brand_avg,
               SUM(`매출액`) AS sales,
               COUNT(DISTINCT `거래처`) AS customer_count
        FROM {main.T_MAIN}
        WHERE `ZC본부명` = {_sql(bname)}
                    AND `년월` = {_sql(prev_ym)}
    """)
    avg = _pct((avg_rows[0] or {}).get("brand_avg")) if avg_rows else 0
    brand_total_sales_m = _money_m((avg_rows[0] or {}).get("sales")) if avg_rows else 0
    threshold_max = round(max(0.0, avg), 1)
    threshold = round(min(threshold_max, max(0.0, avg if threshold_pct is None else float(threshold_pct))), 1)
    gp_rows = _q(f"""
        SELECT CASE WHEN SUM(`매출액`) = 0 THEN 0
                    ELSE (SUM(`매출액`) - SUM(COALESCE(`매출원가`, 0))) / SUM(`매출액`) END AS generic_gp_rate
        FROM {main.T_MAIN}
        WHERE `ZC본부명` = {_sql(bname)}
          AND `년월` = {_sql(prev_ym)}
          AND `자재그룹명` IS NOT NULL
          AND COALESCE(`자재그룹명`, '') <> 'FC전용상품'
    """)
    generic_gp_rate = _pct((gp_rows[0] or {}).get("generic_gp_rate")) if gp_rows else 0
    rows = _q(f"""
        SELECT `거래처` AS customer_code,
               MAX(`거래처명`) AS customer_name,
               SUM(`매출액`) AS sales,
               SUM(CASE WHEN COALESCE(`자재그룹명`, '') = 'FC전용상품' THEN `매출액` ELSE 0 END) AS dedicated_sales,
               SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END) AS generic_sales,
               CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0
                    ELSE SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                         / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) END AS generic_ratio
        FROM {main.T_MAIN}
        WHERE `영업사원` = {_sql(emp_code)}
          AND `ZC본부명` = {_sql(bname)}
                    AND `년월` = {_sql(prev_ym)}
        GROUP BY `거래처`
        HAVING SUM(`매출액`) > 0
        ORDER BY sales DESC
    """)
    customers = []
    proposal_possible_sales_raw = 0.0
    target_ratio = min(0.999, max(0.0, avg / 100.0))
    for r in rows:
        sales = float(r.get("sales") or 0)
        dedicated_sales = float(r.get("dedicated_sales") or 0)
        generic_sales = float(r.get("generic_sales") or 0)
        classified_sales = max(0.0, dedicated_sales + generic_sales)
        ratio = _pct(r.get("generic_ratio"))
        is_target = ratio < threshold
        needed_generic_sales = 0.0
        if is_target and target_ratio > 0 and classified_sales > 0:
            needed_generic_sales = max(0.0, (target_ratio * classified_sales - generic_sales) / (1.0 - target_ratio))
            proposal_possible_sales_raw += needed_generic_sales
        c = {
            **r,
            "sales_m": _money_m(sales),
            "dedicated_sales_m": _money_m(dedicated_sales),
            "generic_sales_m": _money_m(generic_sales),
            "generic_ratio": ratio,
            "dedicated_ratio": round(max(0.0, 100.0 - ratio), 1),
            "gap": round(ratio - avg, 1),
            "is_target": is_target,
            "proposal_possible_sales_m": _money_m(needed_generic_sales),
        }
        customers.append(c)
    targets = [c for c in customers if c["is_target"]]
    is_fallback_targets = False
    customer_page_items, customer_pagination = _page_items(customers, customer_page, 10)
    target_page_items, target_pagination = _page_items(targets, target_page, 10)
    proposal_possible_sales_m = _money_m(proposal_possible_sales_raw)
    expected_profit_increase_m = int(round(proposal_possible_sales_m * (generic_gp_rate / 100.0)))
    return {
        "brand": picked,
        "brands": _brand_rows(emp_code),
        "latest_ym": latest,
        "prev_ym": prev_ym,
        "period_months": months,
        "monthly_sales": monthly_sales,
        "brand_avg": avg,
        "brand_total_sales_m": current_month_sales_m or brand_total_sales_m,
        "customers": customers,
        "customer_page": customer_page_items,
        "customer_pagination": customer_pagination,
        "targets": targets,
        "target_page": target_page_items,
        "target_pagination": target_pagination,
        "target_count": len(targets),
        "is_fallback_targets": is_fallback_targets,
        "threshold": threshold,
        "threshold_max": threshold_max,
        "proposal_possible_sales_m": proposal_possible_sales_m,
        "generic_gp_rate": generic_gp_rate,
        "expected_profit_increase_m": expected_profit_increase_m,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def portal_home(request: Request):
    _require_user(request)
    return _render(request, "portal_dashboard.html", data=portal_dashboard())


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", fresh: str = ""):
    if fresh:
        response = _render(request, "portal_login.html", error=error)
        response.delete_cookie(_SESSION_COOKIE)
        return response
    if _current_user(request):
        return _redirect("/portal")
    return _render(request, "portal_login.html", error=error)


@router.post("/login")
async def login(request: Request):
    form = await _read_form(request)
    emp_code = form.get("emp_code", "").strip()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")[:300]
    if emp_code != _ALLOWED_EMP_CODE:
        portal_db.record_login(emp_code, "", "", ip, ua, False, "not_allowed_test_user")
        return _redirect_msg("/portal/login", error="현재 테스트는 20230720 / 이 충규 데이터만 이용 가능합니다.")
    portal_db.record_login(_ALLOWED_EMP_CODE, _ALLOWED_EMP_NAME, _ALLOWED_TEAM, ip, ua, True, "")
    response = _redirect("/portal")
    response.set_cookie(
        _SESSION_COOKIE,
        _make_session(_ALLOWED_EMP_CODE),
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.getenv("PORTAL_SESSION_HTTPS_ONLY", "false").lower() == "true",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    response = _redirect("/portal/login")
    response.delete_cookie(_SESSION_COOKIE)
    return response


@router.get("/brand-report", response_class=HTMLResponse)
async def brand_report_page(
    request: Request,
    brand: str = "",
    threshold: float | None = None,
    customer_page: int = 1,
    target_page: int = 1,
    sent: str = "",
):
    _require_user(request)
    report = brand_report(brand or None, threshold_pct=threshold, customer_page=customer_page, target_page=target_page)
    return _render(request, "portal_brand_report.html", report=report, sent=sent)


@router.get("/target-detail")
async def target_detail(request: Request, brand: str = "", customer_code: str = ""):
    _require_user(request)
    report = brand_report(brand or None)
    if not report.get("brand"):
        raise HTTPException(status_code=404, detail="brand_not_found")
    code = str(customer_code or "").strip()
    customer = next((c for c in report.get("customers", []) if str(c.get("customer_code") or "") == code), None)
    if not customer:
        raise HTTPException(status_code=404, detail="customer_not_found")
    bname = str((report.get("brand") or {}).get("brand_name") or brand)
    products = _recommend_products(bname, code, report.get("period_months") or [])
    return JSONResponse(_json_safe({
        "customer_code": code,
        "customer_name": customer.get("customer_name") or "",
        "products": products,
        "product_names": ", ".join(str(p.get("product_name") or p.get("product_code") or "") for p in products),
        "dm_message": _dm_message(bname, customer, float(report.get("brand_avg") or 0), products),
    }))


@router.post("/dm-log")
async def dm_log(request: Request):
    user = _require_user(request)
    form = await _read_form(request)
    portal_db.record_dm_log(
        emp_code=user["emp_code"],
        emp_name=user["name"],
        team=user["team"],
        brand_code=form.get("brand_code", ""),
        brand_name=form.get("brand_name", ""),
        customer_code=form.get("customer_code", ""),
        customer_name=form.get("customer_name", ""),
        action_type="generic_product_mix_solution",
        product_names=form.get("product_names", ""),
        message=form.get("message", ""),
        status="test_logged",
    )
    return _redirect_msg("/portal/brand-report", brand=form.get("brand_name", ""), sent="1")
