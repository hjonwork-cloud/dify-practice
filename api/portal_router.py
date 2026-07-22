"""영업사원 액션 제안 포털 라우터."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

import access_control
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
_DEFAULT_EMP_CODE = "20230720"

# ── 팀 리더: 자신의 팀 전체 데이터 조회 가능 ──────────────────────────
_TEAM_LEADERS: dict[str, str] = {
    "20115003": "외식1팀",   # 손상웅
    "20065782": "외식3팀",   # 권봉주
    "20145012": "외식2팀",   # 현승철
    "20135653": "영남지점",  # 김동영
}

def _is_team_leader(emp_code: str) -> bool:
    return emp_code in _TEAM_LEADERS

def _leader_team(emp_code: str) -> str:
    return _TEAM_LEADERS.get(emp_code, "")

def _scope_cond(emp_code: str) -> str:
    """팀 리더: 지점명 기준 팀 전체, 일반: 영업사원 개인"""
    if emp_code in _TEAM_LEADERS:
        return f"`지점명` = {_sql(_TEAM_LEADERS[emp_code])}"
    return f"`영업사원` = {_sql(emp_code)}"

_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 600  # 10분 캐시 (기존 5분 → 성능 개선)


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
    if not emp_code:
        return None
    return _portal_user(emp_code)


def _require_user(request: Request) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="포털 로그인이 필요합니다.")
    return user


def _employee_whitelist() -> dict[str, dict]:
    cached = _cache_get("employee_whitelist")
    if cached is not None:
        return cached
    import main
    import datetime as _dt
    _today = _dt.date.today()
    _months = []
    _y, _m = _today.year, _today.month
    for _ in range(3):
        _months.append(f"{_y:04d}{_m:02d}")
        _m -= 1
        if _m == 0:
            _m = 12
            _y -= 1
    _month_in = ", ".join(f"'{ym}'" for ym in _months)
    rows: list[dict] = []
    try:
        rows = _q(f"""
            SELECT `영업사원` AS emp_code,
                   MAX(`영업사원명`) AS emp_name,
                   MAX(`부서명`) AS dept_name,
                   MAX(`지점명`) AS branch_name,
                   COUNT(DISTINCT `거래처`) AS customer_count
            FROM {main.T_MAIN}
            WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
              AND `영업사원` IS NOT NULL
              AND TRIM(CAST(`영업사원` AS STRING)) <> ''
              AND `년월` IN ({_month_in})
            GROUP BY `영업사원`
            ORDER BY dept_name, emp_name
        """)
    except Exception:
        rows = []
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("emp_code") or "").strip()
        if not code:
            continue
        out[code] = {
            "emp_code": code,
            "name": str(r.get("emp_name") or code).strip(),
            "team": str(r.get("dept_name") or r.get("branch_name") or "").strip(),
            "customer_count": int(r.get("customer_count") or 0),
            "role": "user",
        }
    blacklist: set[str] = set()
    try:
        blacklist = {str(x).strip() for x in main._load_blacklist()}
    except Exception:
        blacklist = set()
    try:
        for code, info in main._load_whitelist().items():
            if str(code).strip() in blacklist:
                continue
            out.setdefault(str(code), {
                "emp_code": str(code),
                "name": info.get("name", str(code)),
                "team": info.get("team", ""),
                "customer_count": 0,
                "role": "user",
            })
    except Exception:
        pass
    for code in blacklist:
        out.pop(code, None)
    out[access_control.ADMIN_EMP_CODE] = {
        "emp_code": access_control.ADMIN_EMP_CODE,
        "name": access_control.ADMIN_EMP_NAME,
        "team": access_control.ADMIN_TEAM,
        "customer_count": 0,
        "role": "admin",
    }
    return _cache_set("employee_whitelist", out)


def _portal_user(emp_code: str) -> dict | None:
    code = str(emp_code or "").strip()
    if not code:
        return None
    allowed = _employee_whitelist()
    if code not in allowed and not access_control.is_admin_emp(code):
        return None
    if not access_control.beta_access_allowed(code):
        return None
    info = allowed.get(code) or {}
    role = "admin" if access_control.is_admin_emp(code) else str(info.get("role") or "user")
    return {
        "emp_code": code,
        "name": info.get("name") or (access_control.ADMIN_EMP_NAME if role == "admin" else code),
        "team": info.get("team") or (access_control.ADMIN_TEAM if role == "admin" else ""),
        "role": role,
        "is_admin": role == "admin",
    }


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


def _latest_ym(emp_code: str = _DEFAULT_EMP_CODE) -> str:
    cached = _cache_get(f"latest:{emp_code}")
    if cached:
        return str(cached)
    import main
    rows = _q(f"""
        SELECT MAX(`년월`) AS ym
        FROM {main.T_MAIN}
        WHERE {_scope_cond(emp_code)}
          AND `매출액` IS NOT NULL
    """)
    ym = str((rows[0] or {}).get("ym") or "") if rows else ""
    return _cache_set(f"latest:{emp_code}", ym)


def _latest_bill_date(emp_code: str = _DEFAULT_EMP_CODE, ym: str = "") -> str:
    cached = _cache_get(f"billdate:{emp_code}:{ym}")
    if cached:
        return str(cached)
    import main
    where_ym = f"AND `년월` = {_sql(ym)}" if ym else ""
    rows = _q(f"""
        SELECT MAX(`대금청구일`) AS bill_date
        FROM {main.T_MAIN}
        WHERE {_scope_cond(emp_code)}
          {where_ym}
    """)
    bill_date = str((rows[0] or {}).get("bill_date") or "") if rows else ""
    return _cache_set(f"billdate:{emp_code}:{ym}", bill_date)


def _profit_latest_ym(emp_code: str = _DEFAULT_EMP_CODE) -> str:
    cached = _cache_get(f"profit_latest:{emp_code}")
    if cached:
        return str(cached)
    import main
    rows = _q(f"""
        WITH my_customers AS (
            SELECT DISTINCT `거래처`
            FROM {main.T_MAIN}
            WHERE {_scope_cond(emp_code)}
        )
        SELECT MAX(DATE_FORMAT(p.`날짜`, 'yyyyMM')) AS ym
        FROM {main.T_PROFIT} p
        INNER JOIN my_customers c ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(c.`거래처` AS STRING))
    """)
    ym = str((rows[0] or {}).get("ym") or "") if rows else ""
    return _cache_set(f"profit_latest:{emp_code}", ym)


def _brand_cm_map(emp_code: str, profit_ym: str) -> dict[str, float]:
    """ZC본부코드 → CM% 맵. 대시보드 브랜드 섹션 CM% 표기용."""
    if not profit_ym:
        return {}
    cached = _cache_get(f"brand_cm:{emp_code}:{profit_ym}")
    if cached is not None:
        return cached
    import main
    try:
        rows = _q(f"""
            WITH scope_custs AS (
                SELECT DISTINCT `ZC본부`, `거래처`
                FROM {main.T_MAIN}
                WHERE {_scope_cond(emp_code)}
            )
            SELECT
                sc.`ZC본부` AS brand_code,
                CASE WHEN SUM(p.`FI매출액`) = 0 THEN 0
                     ELSE SUM(p.`공헌이익`) / SUM(p.`FI매출액`) END AS cm_rate
            FROM {main.T_PROFIT} p
            INNER JOIN scope_custs sc ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(sc.`거래처` AS STRING))
            WHERE DATE_FORMAT(p.`날짜`, 'yyyyMM') = {_sql(profit_ym)}
            GROUP BY sc.`ZC본부`
        """)
        result = {str(r.get("brand_code") or ""): _pct(r.get("cm_rate")) for r in rows}
    except Exception:
        result = {}
    return _cache_set(f"brand_cm:{emp_code}:{profit_ym}", result)


def _brand_rows(emp_code: str = _DEFAULT_EMP_CODE) -> list[dict]:
    cached = _cache_get(f"brands:{emp_code}")
    if cached is not None:
        return cached
    import main
    latest = _latest_ym(emp_code)
    if not latest:
        return []
    scope = _scope_cond(emp_code)
    rows = _q(f"""
        WITH my_brands AS (
            SELECT DISTINCT COALESCE(`ZC본부`, '') AS brand_code,
                            COALESCE(`ZC본부명`, '미분류') AS brand_name
            FROM {main.T_MAIN}
            WHERE {scope}
              AND `년월` = {_sql(latest)}
              AND COALESCE(`ZC본부명`, '') <> ''
        ),
        brand_all AS (
            SELECT
                COALESCE(`ZC본부`, '') AS brand_code,
                COALESCE(`ZC본부명`, '미분류') AS brand_name,
                SUM(`매출액`) AS sales,
                COUNT(DISTINCT `ZC본부`) AS customer_count,
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
                COUNT(DISTINCT `ZC본부`) AS my_customer_count
            FROM {main.T_MAIN}
            WHERE {scope}
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
            "cm_rate": None,  # portal_dashboard에서 채움
        })
    return _cache_set(f"brands:{emp_code}", out)



def portal_dashboard(emp_code: str = _DEFAULT_EMP_CODE) -> dict:
    cached = _cache_get(f"dashboard:{emp_code}")
    if cached is not None:
        return cached
    import main
    is_leader = _is_team_leader(emp_code)
    team_name = _leader_team(emp_code)
    scope = _scope_cond(emp_code)

    # ── Phase 1: 독립 쿼리 4개 병렬 실행 ──────────────────────────
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_latest  = ex.submit(_latest_ym, emp_code)
        f_brands  = ex.submit(_brand_rows, emp_code)
        f_profit  = ex.submit(_profit_latest_ym, emp_code)
    latest     = f_latest.result()
    brands     = f_brands.result()
    profit_ym  = f_profit.result()

    # ── Phase 2: latest/profit_ym 확정 후 나머지 4개 + CM맵 병렬 실행 ──
    def _q_summary():
        if not latest:
            return {}
        rows = _q(f"""
            WITH base AS (
                SELECT `ZC본부`, `거래처`, `매출액`
                FROM {main.T_MAIN}
                WHERE {scope}
                  AND `년월` = {_sql(latest)}
            )
            SELECT
                SUM(`매출액`) AS sales,
                COUNT(DISTINCT CASE
                    WHEN `ZC본부` IS NOT NULL
                     AND LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'
                    THEN `ZC본부` ELSE NULL END) AS brand_count,
                COUNT(DISTINCT CASE
                    WHEN `ZC본부` IS NOT NULL
                     AND LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'
                    THEN `거래처` ELSE NULL END) AS franchise_count,
                COUNT(DISTINCT CASE
                    WHEN `ZC본부` IS NULL
                      OR LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) <> '8'
                    THEN `거래처` ELSE NULL END) AS general_count,
                COUNT(DISTINCT `거래처`) AS total_count
            FROM base
        """)
        return rows[0] if rows else {}

    def _q_bill_date():
        return _latest_bill_date(emp_code, latest) if latest else ""

    def _q_cm():
        if not profit_ym:
            return 0.0
        try:
            rows = _q(f"""
                WITH my_customers AS (
                    SELECT DISTINCT `거래처`
                    FROM {main.T_MAIN}
                    WHERE {scope}
                )
                SELECT CASE WHEN SUM(p.`FI매출액`) = 0 THEN 0
                            ELSE SUM(p.`공헌이익`) / SUM(p.`FI매출액`) END AS cm_rate
                FROM {main.T_PROFIT} p
                INNER JOIN my_customers c ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(c.`거래처` AS STRING))
                WHERE DATE_FORMAT(p.`날짜`, 'yyyyMM') = {_sql(profit_ym)}
            """)
            return _pct((rows[0] or {}).get("cm_rate")) if rows else 0.0
        except Exception:
            return 0.0

    def _q_ar():
        if not latest:
            return 0
        try:
            if is_leader:
                rows = _q(f"""
                    WITH team_emps AS (
                        SELECT DISTINCT `영업사원`
                        FROM {main.T_MAIN}
                        WHERE {scope} AND `년월` = {_sql(latest)}
                    )
                    SELECT SUM(a.`현재잔액`) AS balance
                    FROM {main.T_AR} a
                    INNER JOIN team_emps t ON a.`영업사원` = t.`영업사원`
                    WHERE a.`년월` = {_sql(latest)}
                """)
            else:
                rows = _q(f"""
                    SELECT SUM(`현재잔액`) AS balance
                    FROM {main.T_AR}
                    WHERE `영업사원` = {_sql(emp_code)}
                      AND `년월` = {_sql(latest)}
                """)
            return _won_m((rows[0] or {}).get("balance")) if rows else 0
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_summary   = ex.submit(_q_summary)
        f_bill_date = ex.submit(_q_bill_date)
        f_cm        = ex.submit(_q_cm)
        f_ar        = ex.submit(_q_ar)
        f_cm_map    = ex.submit(_brand_cm_map, emp_code, profit_ym)
    summary          = f_summary.result()
    latest_bill_date = f_bill_date.result()
    cm_rate          = f_cm.result()
    ar_balance       = f_ar.result()
    cm_map           = f_cm_map.result()

    # 브랜드 목록에 CM% 머지
    brands_with_cm = []
    for b in brands:
        brands_with_cm.append({**b, "cm_rate": cm_map.get(str(b.get("brand_code") or ""), None)})

    data = {
        "latest_ym": latest,
        "latest_bill_date": latest_bill_date,
        "profit_ym": profit_ym,
        "period_months": [latest] if latest else [],
        "sales_m": _money_m(summary.get("sales")),
        "brand_count": int(summary.get("brand_count") or 0),
        "franchise_count": int(summary.get("franchise_count") or 0),
        "general_count": int(summary.get("general_count") or 0),
        "customer_count": int(summary.get("total_count") or 0),
        "cm_rate": cm_rate,
        "ar_balance_m": ar_balance,
        "brands": brands_with_cm,
        "is_leader": is_leader,
        "team_name": team_name,
    }
    return _cache_set(f"dashboard:{emp_code}", data)


def _pick_brand(brand_name: str | None, emp_code: str = _DEFAULT_EMP_CODE) -> dict | None:
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


def _recommend_products(brand_name: str, customer_code: str, months: list[str], emp_code: str = _DEFAULT_EMP_CODE) -> list[dict]:
    import main
    rows = _q(f"""
        WITH target_products AS (
            SELECT DISTINCT `자재`
            FROM {main.T_MAIN}
            WHERE `영업사원` = {_sql(emp_code)}
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
    emp_code: str = _DEFAULT_EMP_CODE,
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
               COUNT(DISTINCT `ZC본부`) AS customer_count
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
    # ZC본부명 기준 집계: 개별 거래처 대신 ZC본부 단위로 집계
    scope = _scope_cond(emp_code)
    rows = _q(f"""
        SELECT COALESCE(`ZC본부`, '') AS customer_code,
               MAX(COALESCE(`ZC본부명`, '')) AS customer_name,
               SUM(`매출액`) AS sales,
               SUM(CASE WHEN COALESCE(`자재그룹명`, '') = 'FC전용상품' THEN `매출액` ELSE 0 END) AS dedicated_sales,
               SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END) AS generic_sales,
               CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0
                    ELSE SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                         / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) END AS generic_ratio
        FROM {main.T_MAIN}
        WHERE {scope}
          AND `ZC본부명` = {_sql(bname)}
          AND `년월` = {_sql(prev_ym)}
        GROUP BY `ZC본부`, `ZC본부명`
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
        "is_leader": _is_team_leader(emp_code),
        "team_name": _leader_team(emp_code),
    }


def _division_latest_ym() -> str:
    cached = _cache_get("division_latest")
    if cached:
        return str(cached)
    import main
    rows = _q(f"""
        SELECT MAX(`년월`) AS ym
        FROM {main.T_MAIN}
        WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
          AND `매출액` IS NOT NULL
    """)
    ym = str((rows[0] or {}).get("ym") or "") if rows else ""
    return _cache_set("division_latest", ym)


def _division_bill_date(ym: str) -> str:
    cached = _cache_get(f"division_bill:{ym}")
    if cached:
        return str(cached)
    import main
    rows = _q(f"""
        SELECT MAX(`대금청구일`) AS bill_date
        FROM {main.T_MAIN}
        WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
          AND `년월` = {_sql(ym)}
    """) if ym else []
    bill_date = str((rows[0] or {}).get("bill_date") or "") if rows else ""
    return _cache_set(f"division_bill:{ym}", bill_date)


def portal_admin_overview(thresholds: dict[str, float] | None = None) -> dict:
    # thresholds가 없는 기본 조회는 캐시 적용 (10분)
    _th = thresholds or {}
    _cache_key = f"admin_overview:{hashlib.md5(json.dumps(_th, sort_keys=True).encode()).hexdigest()[:8]}"
    cached = _cache_get(_cache_key)
    if cached is not None:
        return cached
    import main
    latest = _division_latest_ym()
    prev_ym = _month_shift(latest, -1) if latest else ""

    # ── Phase 1: latest 확정 후 독립 쿼리 3개 병렬 실행 ──────────────
    def _q_summary():
        if not latest:
            return {}
        rows = _q(f"""
            SELECT SUM(`매출액`) AS sales,
                   COUNT(DISTINCT `거래처`) AS customers,
                   COUNT(DISTINCT `ZC본부명`) AS brands,
                   COUNT(DISTINCT `영업사원`) AS employees
            FROM {main.T_MAIN}
            WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
              AND `년월` = {_sql(latest)}
        """)
        return rows[0] if rows else {}

    def _q_profit_ym():
        try:
            rows = _q(f"""
                WITH div_customers AS (
                    SELECT DISTINCT `거래처`
                    FROM {main.T_MAIN}
                    WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
                )
                SELECT MAX(DATE_FORMAT(p.`날짜`, 'yyyyMM')) AS ym
                FROM {main.T_PROFIT} p
                INNER JOIN div_customers c ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(c.`거래처` AS STRING))
            """)
            return str((rows[0] or {}).get("ym") or "") if rows else ""
        except Exception:
            return ""

    def _q_ar():
        if not latest:
            return 0
        try:
            rows = _q(f"""
                WITH div_sales AS (
                    SELECT DISTINCT `영업사원`
                    FROM {main.T_MAIN}
                    WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
                      AND `년월` = {_sql(latest)}
                )
                SELECT SUM(a.`현재잔액`) AS balance
                FROM {main.T_AR} a
                INNER JOIN div_sales s ON a.`영업사원` = s.`영업사원`
                WHERE a.`년월` = {_sql(latest)}
            """)
            return _won_m((rows[0] or {}).get("balance")) if rows else 0
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_summary   = ex.submit(_q_summary)
        f_profit_ym = ex.submit(_q_profit_ym)
        f_ar        = ex.submit(_q_ar)
        f_bill_date = ex.submit(_division_bill_date, latest)
    summary    = f_summary.result()
    profit_ym  = f_profit_ym.result()
    ar_balance = f_ar.result()
    bill_date  = f_bill_date.result()

    # ── Phase 2: profit_ym 확정 후 CM율 + solution 병렬 ──────────────
    def _q_cm():
        if not profit_ym:
            return 0.0
        try:
            rows = _q(f"""
                WITH div_customers AS (
                    SELECT DISTINCT `거래처`
                    FROM {main.T_MAIN}
                    WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
                )
                SELECT CASE WHEN SUM(p.`FI매출액`) = 0 THEN 0
                            ELSE SUM(p.`공헌이익`) / SUM(p.`FI매출액`) END AS cm_rate
                FROM {main.T_PROFIT} p
                INNER JOIN div_customers c ON TRIM(LEADING '0' FROM CAST(p.`고객` AS STRING)) = TRIM(LEADING '0' FROM CAST(c.`거래처` AS STRING))
                WHERE DATE_FORMAT(p.`날짜`, 'yyyyMM') = {_sql(profit_ym)}
            """)
            return _pct((rows[0] or {}).get("cm_rate")) if rows else 0.0
        except Exception:
            return 0.0

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_cm       = ex.submit(_q_cm)
        f_solution = ex.submit(admin_proposal_solution, prev_ym, thresholds or {})
    cm_rate  = f_cm.result()
    solution = f_solution.result()

    result = {
        "latest_ym": latest,
        "prev_ym": prev_ym,
        "latest_bill_date": bill_date,
        "profit_ym": profit_ym,
        "sales_m": _money_m(summary.get("sales")),
        "customer_count": int(summary.get("customers") or 0),
        "brand_count": int(summary.get("brands") or 0),
        "employee_count": int(summary.get("employees") or 0),
        "cm_rate": cm_rate,
        "ar_balance_m": ar_balance,
        "solution": solution,
    }
    return _cache_set(_cache_key, result)


def admin_proposal_solution(prev_ym: str, thresholds: dict[str, float]) -> dict:
    if not prev_ym:
        return {"brands": [], "proposal_possible_sales_m": 0, "generic_gp_rate": 0, "expected_profit_increase_m": 0}
    # thresholds가 기본값(빈 dict)이면 캐시 사용
    _th_key = hashlib.md5(json.dumps(thresholds, sort_keys=True).encode()).hexdigest()[:8]
    _cache_key = f"admin_solution:{prev_ym}:{_th_key}"
    cached = _cache_get(_cache_key)
    if cached is not None:
        return cached
    import main
    rows = _q(f"""
        SELECT COALESCE(`ZC본부`, '') AS brand_code,
               COALESCE(`ZC본부명`, '미분류') AS brand_name,
               `거래처` AS customer_code,
               MAX(`거래처명`) AS customer_name,
               SUM(`매출액`) AS sales,
               SUM(CASE WHEN COALESCE(`자재그룹명`, '') = 'FC전용상품' THEN `매출액` ELSE 0 END) AS dedicated_sales,
               SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END) AS generic_sales,
               SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) AS classified_sales,
               SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN COALESCE(`매출원가`, 0) ELSE 0 END) AS generic_cost
        FROM {main.T_MAIN}
        WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
          AND `년월` = {_sql(prev_ym)}
          AND COALESCE(`ZC본부명`, '') <> ''
        GROUP BY COALESCE(`ZC본부`, ''), COALESCE(`ZC본부명`, '미분류'), `거래처`
        HAVING SUM(`매출액`) > 0
    """)
    grouped: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("brand_code") or "")
        name = str(r.get("brand_name") or "미분류")
        key = hashlib.md5(f"{code}|{name}".encode("utf-8")).hexdigest()[:12]
        g = grouped.setdefault(key, {
            "key": key,
            "brand_code": code,
            "brand_name": name,
            "sales": 0.0,
            "classified_sales": 0.0,
            "generic_sales": 0.0,
            "generic_cost": 0.0,
            "customer_count": 0,
            "targets": 0,
            "proposal_possible_sales": 0.0,
        })
        sales = float(r.get("sales") or 0)
        generic = float(r.get("generic_sales") or 0)
        classified = float(r.get("classified_sales") or 0)
        cost = float(r.get("generic_cost") or 0)
        g["sales"] += sales
        g["classified_sales"] += classified
        g["generic_sales"] += generic
        g["generic_cost"] += cost
        g["customer_count"] += 1
    # 2-pass: threshold default = each brand current generic ratio.
    for g in grouped.values():
        avg_pct = _pct((g["generic_sales"] / g["classified_sales"]) if g["classified_sales"] else 0)
        g["brand_avg"] = avg_pct
        g["threshold"] = round(max(0.0, min(50.0, float(thresholds.get(g["key"], avg_pct)))), 1)
    for r in rows:
        code = str(r.get("brand_code") or "")
        name = str(r.get("brand_name") or "미분류")
        key = hashlib.md5(f"{code}|{name}".encode("utf-8")).hexdigest()[:12]
        g = grouped[key]
        classified = float(r.get("classified_sales") or 0)
        generic = float(r.get("generic_sales") or 0)
        ratio_pct = _pct((generic / classified) if classified else 0)
        threshold = float(g["threshold"] or 0)
        target_ratio = min(0.999, max(0.0, threshold / 100.0))
        if classified > 0 and target_ratio > 0 and ratio_pct < threshold:
            needed = max(0.0, (target_ratio * classified - generic) / (1.0 - target_ratio))
            g["proposal_possible_sales"] += needed
            g["targets"] += 1
    brands = []
    total_proposal = 0.0
    total_expected_profit = 0.0
    for g in grouped.values():
        gp_rate = ((g["generic_sales"] - g["generic_cost"]) / g["generic_sales"]) if g["generic_sales"] else 0.0
        expected = g["proposal_possible_sales"] * gp_rate
        total_proposal += g["proposal_possible_sales"]
        total_expected_profit += expected
        brands.append({
            **g,
            "sales_m": _money_m(g["sales"]),
            "generic_sales_m": _money_m(g["generic_sales"]),
            "proposal_possible_sales_m": _money_m(g["proposal_possible_sales"]),
            "generic_gp_rate": _pct(gp_rate),
            "expected_profit_increase_m": _money_m(expected),
        })
    brands.sort(key=lambda x: x.get("proposal_possible_sales", 0), reverse=True)
    total_gp = (total_expected_profit / total_proposal) if total_proposal else 0.0
    result = {
        "brands": brands,
        "proposal_possible_sales_m": _money_m(total_proposal),
        "generic_gp_rate": _pct(total_gp),
        "expected_profit_increase_m": _money_m(total_expected_profit),
        "target_count": sum(int(b.get("targets") or 0) for b in brands),
    }
    return _cache_set(_cache_key, result)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def portal_home(request: Request):
    user = _require_user(request)
    return _render(request, "portal_dashboard.html", data=portal_dashboard(user["emp_code"]))


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
    user = _portal_user(emp_code)
    if not user:
        reason = "beta_not_allowed" if emp_code in _employee_whitelist() and access_control.beta_gate_active() else "not_in_sales_whitelist"
        portal_db.record_login(emp_code, "", "", ip, ua, False, reason)
        if reason == "beta_not_allowed":
            return _redirect_msg("/portal/login", error=access_control.beta_denied_message("세일즈 액션 플랫폼"))
        return _redirect_msg("/portal/login", error="외식식재사업부 화이트리스트에 등록된 사번만 이용 가능합니다.")
    portal_db.record_login(user["emp_code"], user["name"], user["team"], ip, ua, True, "")
    response = _redirect("/portal")
    response.set_cookie(
        _SESSION_COOKIE,
        _make_session(user["emp_code"]),
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
    user = _require_user(request)
    report = brand_report(brand or None, emp_code=user["emp_code"], threshold_pct=threshold, customer_page=customer_page, target_page=target_page)
    return _render(request, "portal_brand_report.html", report=report, sent=sent)


@router.get("/admin", response_class=HTMLResponse)
async def portal_admin_page(request: Request):
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    thresholds: dict[str, float] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("threshold__"):
            try:
                thresholds[key.replace("threshold__", "", 1)] = float(value)
            except (TypeError, ValueError):
                continue
    data = portal_admin_overview(thresholds)
    return _render(request, "portal_admin.html", data=data)


@router.get("/target-detail")
async def target_detail(request: Request, brand: str = "", customer_code: str = ""):
    user = _require_user(request)
    report = brand_report(brand or None, emp_code=user["emp_code"])
    if not report.get("brand"):
        raise HTTPException(status_code=404, detail="brand_not_found")
    code = str(customer_code or "").strip()
    customer = next((c for c in report.get("customers", []) if str(c.get("customer_code") or "") == code), None)
    if not customer:
        raise HTTPException(status_code=404, detail="customer_not_found")
    bname = str((report.get("brand") or {}).get("brand_name") or brand)
    products = _recommend_products(bname, code, report.get("period_months") or [], user["emp_code"])
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
    # DM 발송 기능 준비 중 - 로그 저장 제거
    return _redirect_msg("/portal/brand-report", brand=form.get("brand_name", ""))


# ── 서버 시작 시 백그라운드 캐시 워밍업 ──────────────────────────────
def _warmup_cache():
    """서버 시작 후 30초 대기 후 주요 캐시를 미리 채운다."""
    import logging
    _log = logging.getLogger("portal_warmup")
    time.sleep(30)  # 서버 완전 기동 대기
    try:
        _log.info("[warmup] 관리자 대시보드 캐시 워밍업 시작")
        portal_admin_overview({})
        _log.info("[warmup] 관리자 대시보드 캐시 완료")
    except Exception as e:
        _log.warning(f"[warmup] 관리자 대시보드 실패: {e}")
    try:
        _log.info("[warmup] 사업부 기준 데이터 워밍업 시작")
        _division_latest_ym()
        _employee_whitelist()
        _log.info("[warmup] 사업부 기준 데이터 완료")
    except Exception as e:
        _log.warning(f"[warmup] 사업부 데이터 실패: {e}")


threading.Thread(target=_warmup_cache, daemon=True, name="portal-warmup").start()
