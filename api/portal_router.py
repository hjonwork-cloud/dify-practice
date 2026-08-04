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
from pydantic import BaseModel

import access_control
import portal_db

import logging
logger = logging.getLogger(__name__)
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
    """팀 리더: 지점명 기준 팀 전체, 관리자: 전체 사업부, 일반: 영업사원 개인"""
    if access_control.is_admin_emp(emp_code):
        return f"`사업부명` = {_sql(access_control.AUTH_DEPT)}"
    if emp_code in _TEAM_LEADERS:
        team = _TEAM_LEADERS[emp_code]
        # LIKE 매칭으로 '(FC)영남지점' 등 prefix 변형도 포함
        return f"`지점명` LIKE {_sql('%' + team + '%')}"
    return f"`영업사원` = {_sql(emp_code)}"

_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 21600  # 6시간 캐시 (로그인 속도 개선)


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
    if not access_control.beta_access_allowed(code):
        return None
    # 베타테스터/팀리더는 Databricks 화이트리스트 쿼리 완전 스킵 (로그인 속도 개선)
    beta_testers = access_control.load_beta_testers()
    in_beta = code in beta_testers
    is_admin = access_control.is_admin_emp(code)
    is_leader = _is_team_leader(code)
    if in_beta or is_admin or is_leader:
        # 빠른 경로: Databricks 쿼리 없이 즉시 반환
        if in_beta:
            binfo = beta_testers[code]
            name = binfo.get("name", code)
            team = binfo.get("team", "") or _leader_team(code)
            role_raw = binfo.get("role", "user")
        else:
            name = access_control.ADMIN_EMP_NAME if is_admin else code
            team = access_control.ADMIN_TEAM if is_admin else _leader_team(code)
            role_raw = "admin" if is_admin else "user"
        return {
            "emp_code": code,
            "name": name,
            "team": team,
            "role": "admin" if is_admin else "user",
            "is_admin": is_admin,
        }
    # 일반 영업사원: Databricks 화이트리스트 확인 (캐시 6시간)
    allowed = _employee_whitelist()
    if code not in allowed:
        return None
    info = allowed.get(code) or {}
    return {
        "emp_code": code,
        "name": info.get("name") or code,
        "team": info.get("team") or "",
        "role": "user",
        "is_admin": False,
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


def _cache_clear_all():
    """인메모리 캐시 전체 초기화 (대시보드 테이블 refresh 후 호출)"""
    _cache.clear()


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


def _convert_brand_row(b: dict) -> dict:
    """T_BRANDS 원시 row → `_brand_rows` / warmup 공통 변환."""
    code = str(b.get("brand_code") or "")
    sales = float(b.get("sales_m") or 0) * 10000  # sales_m → raw
    return {
        "brand_code":        code,
        "brand_name":        str(b.get("brand_name") or ""),
        "customer_count":    int(b.get("customer_count") or 0),
        "sales":             sales,
        "sales_m":           int(b.get("sales_m") or 0),
        "my_customer_count": int(b.get("my_customer_count") or 0),
        "my_sales":          float(b.get("my_sales_m") or 0) * 10000,
        "my_sales_m":        int(b.get("my_sales_m") or 0),
        "generic_ratio":     float(b.get("generic_ratio") or 0),
        "cm_rate":           (round(float(b["cm_rate"]), 1) if b.get("cm_rate") is not None else None),
        "dedicated_sales":   0,
        "generic_sales":     0,
        "classified_sales":  0,
    }


def _bulk_warm_brand_rows() -> int:
    """T_BRANDS 를 **한 번의 SELECT** 로 전체 읽어서 사용자별 캐시에 채워 넣는다.

    이 함수를 부팅 warmup 과 주기적 refresh 에서 호출한다.
    한 번 실행하면 모든 영업사원의 다음 첫 클릭이 즉시(캐시 히트) 반환된다.

    Returns:
        캐시에 채운 사용자 수.
    """
    import main as _main  # portal_refresh.main 은 module attribute 가 아니므로 직접 import
    import portal_refresh as _pr
    rows = _main._safe_query(
        f"SELECT * FROM {_pr.T_BRANDS} ORDER BY emp_code, sales_m DESC",
        raw=True,
    ) or []
    # emp_code 별 그룹핑
    by_emp: dict[str, list[dict]] = {}
    for r in rows:
        emp = str(r.get("emp_code") or "").strip()
        if not emp:
            continue
        by_emp.setdefault(emp, []).append(_convert_brand_row(r))
    # 캐시 채우기 (LIMIT 200 유지)
    for emp, out in by_emp.items():
        _cache_set(f"brands:{emp}", out[:200])
    return len(by_emp)


def _brand_rows(emp_code: str = _DEFAULT_EMP_CODE) -> list[dict]:
    cached = _cache_get(f"brands:{emp_code}")
    if cached:  # 빈 리스트는 캐시 무효 처리
        return cached
    # ── 1순위: T_BRANDS 사전계산 테이블 ──────────────────────────────
    try:
        import main as _main
        import portal_refresh as _pr
        rows_pre = _main._safe_query(
            f"SELECT * FROM {_pr.T_BRANDS} WHERE emp_code = '{emp_code}' ORDER BY sales_m DESC LIMIT 200",
            raw=True,
        ) or []
        if rows_pre:
            out_pre = [_convert_brand_row(b) for b in rows_pre]
            _cache_set(f"brands:{emp_code}", out_pre)
            return out_pre
    except Exception:
        pass  # fallback to live query
    # ── 2순위: T_MAIN 실시간 쿼리 ────────────────────────────────────
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
            WHERE `사업부명` = {_sql(access_control.AUTH_DEPT)}
              AND `년월` = {_sql(latest)}
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
        LIMIT 200
    """)
    zc8_rows: list[dict] = []
    gen_sales = gen_my_sales = 0.0
    gen_count = gen_my_count = 0
    for r in rows:
        code = str(r.get("brand_code") or "")
        is_zc8 = code.lstrip("0")[:1] == "8"
        sales = float(r.get("sales") or 0)
        classified = float(r.get("classified_sales") or 0)
        row_out = {
            **r,
            "sales_m": _money_m(sales),
            "my_sales_m": _money_m(r.get("my_sales")),
            "generic_ratio": _pct((float(r.get("generic_sales") or 0) / classified) if classified else 0),
            "cm_rate": None,
        }
        if is_zc8:
            zc8_rows.append(row_out)
        else:
            gen_sales += sales
            gen_my_sales += float(r.get("my_sales") or 0)
            gen_count += int(r.get("customer_count") or 0)
            gen_my_count += int(r.get("my_customer_count") or 0)
    out = zc8_rows
    if gen_sales > 0 or gen_my_count > 0:
        out.append({
            "brand_code": "일반외식",
            "brand_name": "🧑\u200d🍳일반외식업장",
            "sales": gen_sales,
            "sales_m": _money_m(gen_sales),
            "my_sales": gen_my_sales,
            "my_sales_m": _money_m(gen_my_sales),
            "customer_count": gen_count,
            "my_customer_count": gen_my_count,
            "dedicated_sales": 0, "generic_sales": 0, "classified_sales": 0,
            "generic_ratio": 0.0, "cm_rate": None,
        })
    # 팀리더는 캐시 저장 안함, 일반도 빈 결과는 저장 안함
    if out:
        _cache_set(f"brands:{emp_code}", out)
    return out



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
    # 기본 pick: 가상 브랜드(일반외식) 는 사전계산 테이블에 없어서 30초 fallback 유발 →
    #           실제 ZC본부 브랜드 중 첫 번째를 선택
    def _is_virtual(b: dict) -> bool:
        return str(b.get("brand_code") or "") in ("", "일반외식")

    for b in brands:
        if _is_virtual(b):
            continue
        if "생활맥주" in str(b.get("brand_name") or ""):
            return b
    for b in brands:
        if not _is_virtual(b):
            return b
    # 진짜 브랜드가 없으면 (관리자/전체 아닌 경우) 마지막 fallback 으로 첫 항목
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
               SUM(COALESCE(`매출수량`, 0)) AS total_qty,
               SUM(COALESCE(`매출원가`, 0)) AS total_cost,
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
        LIMIT 30
    """)
    result = []
    for r in rows:
        sales      = float(r.get("sales") or 0)
        total_qty  = float(r.get("total_qty") or 0)
        total_cost = float(r.get("total_cost") or 0)
        # 단가/원가 단위 계산: raw 매출액 1단위 = 100원 (SUM/10,000 = 백만원 기준)
        # → 원(₩) 단위로 변환하려면 ×100 필요
        unit_price = round(sales / total_qty * 100)       if total_qty > 0 else 0
        unit_cost  = round(total_cost / total_qty * 100)  if total_qty > 0 else 0
        result.append({
            **r,
            "sales_m":    _money_m(sales),
            "gp_pct":     _pct(r.get("gp_rate")),
            "unit_price": unit_price,   # 평균 단가 (₩)
            "unit_cost":  unit_cost,    # 평균 단가 원가 (₩)
        })
    return result


def _dm_message(brand_name: str, customer: dict, brand_avg: float, products: list[dict]) -> str:
    product_lines = "\n".join(
        f"• [{p.get('product_code','')}] {p.get('product_name') or p.get('product_code')}"
        for p in products[:5]
    ) or "• 추천 후보 상품 확인 필요"
    return (
        f"안녕하세요, {customer.get('customer_name')} 사장님.\n\n"
        f"{brand_name}을 운영해주셔서 감사드립니다. "
        f"동원홈푸드 영업담당자입니다.\n\n"
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
    ym_mode: str = "prev",  # "prev"=전월 / "current"=당월
) -> dict:
    # ── 사전계산 테이블 우선 조회 (팀리더는 실시간 쿼리 강제 — 캐시 테이블이 개인 범위로 잘못 캐싱될 수 있음) ──────────
    picked = _pick_brand(brand_name, emp_code)
    if picked:
        try:
            from portal_refresh import read_brand_report_from_table
            cached = read_brand_report_from_table(
                emp_code,
                str(picked.get("brand_name") or ""),
                threshold_pct=threshold_pct,
                customer_page=customer_page,
                target_page=target_page,
                ym_mode=ym_mode,
            )
            if cached is not None:
                return cached
        except Exception as _pre_err:
            logger.warning(f"[brand_report] precomputed fallback ({emp_code}): {_pre_err}")
    # ── fallback: 실시간 쿼리 ─────────────────────────────────────────
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
    # ym_mode 에 따라 라이브 쿼리도 대상 월 스위칭
    _ym_mode = (ym_mode or "prev").lower()
    selected_ym = latest if _ym_mode == "current" else (prev_ym or latest)
    bname = str(picked.get("brand_name") or "")
    bcode = str(picked.get("brand_code") or "")
    # ── 가상 브랜드 '일반외식업장' 대응 ────────────────────────────────
    # T_MAIN 의 ZC본부명 에는 '🧑‍🍳일반외식업장' 값이 존재하지 않음 (가상 브랜드).
    # 따라서 실 브랜드는 `ZC본부명 = <bname>` 로, 일반외식은 ZC본부 8*이 아닌 조건으로 필터해야 함.
    _is_generic = (bcode == "일반외식")
    _brand_where = (
        "(`ZC본부` IS NULL OR LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) <> '8')"
        if _is_generic
        else f"`ZC본부명` = {_sql(bname)}"
    )
    # 일반외식업장은 외식식재사업부 로 스코프 한정 필요 (다른 사업부 매출 유입 방지)
    _div_where = " AND `사업부명` = '외식식재사업부'" if _is_generic else ""

    monthly_rows = _q(f"""
        SELECT `년월` AS ym,
               SUM(`매출액`) AS sales
        FROM {main.T_MAIN}
        WHERE {_brand_where}
          AND `년월` IN ({_in_months(months)}){_div_where}
        GROUP BY `년월`
        ORDER BY `년월`
    """) if months else []
    monthly_sales = [{**r, "sales_m": _money_m(r.get("sales"))} for r in monthly_rows]
    current_month_sales_m = next((int(r.get("sales_m") or 0) for r in monthly_sales if str(r.get("ym")) == selected_ym), 0)
    avg_rows = _q(f"""
           SELECT CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0
                    ELSE SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                        / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) END AS brand_avg,
               SUM(`매출액`) AS sales,
               COUNT(DISTINCT `ZC본부`) AS customer_count
        FROM {main.T_MAIN}
        WHERE {_brand_where}
                    AND `년월` = {_sql(selected_ym)}{_div_where}
    """)
    avg = _pct((avg_rows[0] or {}).get("brand_avg")) if avg_rows else 0
    brand_total_sales_m = _money_m((avg_rows[0] or {}).get("sales")) if avg_rows else 0
    threshold_max = round(max(0.0, avg), 1)
    threshold = round(min(threshold_max, max(0.0, avg if threshold_pct is None else float(threshold_pct))), 1)
    gp_rows = _q(f"""
        SELECT CASE WHEN SUM(`매출액`) = 0 THEN 0
                    ELSE (SUM(`매출액`) - SUM(COALESCE(`매출원가`, 0))) / SUM(`매출액`) END AS generic_gp_rate
        FROM {main.T_MAIN}
        WHERE {_brand_where}
          AND `년월` = {_sql(selected_ym)}
          AND `자재그룹명` IS NOT NULL
          AND COALESCE(`자재그룹명`, '') <> 'FC전용상품'{_div_where}
    """)
    generic_gp_rate = _pct((gp_rows[0] or {}).get("generic_gp_rate")) if gp_rows else 0
    # 거래처(고객코드) 기준 집계: 개별 가맹점 단위로 집계
    scope = _scope_cond(emp_code)
    rows = _q(f"""
        SELECT COALESCE(`거래처`, '') AS customer_code,
               MAX(COALESCE(`거래처명`, '')) AS customer_name,
               SUM(`매출액`) AS sales,
               SUM(CASE WHEN COALESCE(`자재그룹명`, '') = 'FC전용상품' THEN `매출액` ELSE 0 END) AS dedicated_sales,
               SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END) AS generic_sales,
               CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0
                    ELSE SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`, '') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                         / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) END AS generic_ratio
        FROM {main.T_MAIN}
        WHERE {scope}
          AND {_brand_where}
          AND `년월` = {_sql(selected_ym)}{_div_where}
        GROUP BY `거래처`, `거래처명`
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
            "customer_code": str(r.get("customer_code") or ""),
            "customer_name": str(r.get("customer_name") or ""),
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
        "selected_ym": selected_ym,
        "ym_mode": _ym_mode,
        "period_months": months,
        "monthly_sales": monthly_sales,
        "brand_avg": avg,
        "brand_total_sales_m": _money_m(sum(float(r.get("sales") or 0) for r in rows)),  # selected_ym 기준 내 담당 합산
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
                   COUNT(DISTINCT CASE WHEN LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8' THEN `ZC본부` ELSE NULL END) AS brands,
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
          AND LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'
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
    _require_user(request)          # 인증 확인만 (데이터 로드 없음 → 즉시 반환)
    return _render(request, "portal_dashboard.html")


@router.get("/dashboard-data")
async def dashboard_data_api(request: Request):
    """대시보드 데이터를 JSON으로 반환 (AJAX 전용).

    1순위: portal_refresh.py 가 사전 계산한 요약 테이블 (< 1초)
    2순위: 실시간 Databricks 쿼리 fallback (30~60초)
    """
    user = _require_user(request)
    emp_code = user["emp_code"]

    # ── 1순위: 사전 계산 테이블 (T_DASH) — 팀리더 포함 모두 활용 ────────────
    try:
        import portal_refresh
        precomputed = portal_refresh.read_dashboard_from_table(emp_code)
        if precomputed:
            return JSONResponse(content=precomputed)
    except Exception as _e:
        pass  # 테이블 미존재 / 행 없음 → fallback

    # ── 2순위: 실시간 쿼리 (T_DASH refresh 전 or 업데이트 전) ────────────
    data = portal_dashboard(emp_code)
    return JSONResponse(content=data)


@router.post("/admin/refresh-dashboard")
async def admin_refresh_dashboard(request: Request):
    """대시보드 요약 테이블 강제 재계산 (관리자 전용).
    Databricks Job 대신 수동으로 refresh 할 때 사용.
    """
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    import asyncio
    import portal_refresh

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: portal_refresh.run_refresh(force=True))
        return JSONResponse(content=result)
    except Exception as e:
        import traceback
        return JSONResponse(
            status_code=500,
            content={"status": "error", "reason": str(e), "trace": traceback.format_exc()[-800:]}
        )


@router.get("/admin/diag-brand")
async def admin_diag_brand(request: Request, name: str = "", emp: str = ""):
    """[진단] 특정 브랜드가 사전계산 테이블에 실제로 존재하는지 확인.

    사용:
      /portal/admin/diag-brand?name=크레이지빙수＆오늘도김볶만 본사
      /portal/admin/diag-brand?name=크레이지빙수&emp=20230720

    확인 항목:
      1) T_BRAND_SUMMARY 에 exact 매칭되는가 → 매칭되면 fallback 원인은 다른 것
      2) 전각 ＆(U+FF06) vs 반각 &(U+0026) 정규화 후 매칭되는가
      3) 유사 브랜드명이 존재하는가 (LIKE 검색)
      4) 다른 테이블(T_BRAND_MONTHLY, T_BRAND_CUST, T_BRANDS)에도 존재하는가
      5) 입력 문자열과 저장된 문자열의 유니코드 코드포인트(HEX) 비교
    """
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")

    if not name:
        return JSONResponse(
            {"error": "name 파라미터 필요. 예: /portal/admin/diag-brand?name=브랜드명"},
            status_code=400,
        )

    import main
    from portal_refresh import T_BRAND_SUMMARY, T_BRAND_MONTHLY, T_BRAND_CUST, T_BRANDS

    def _hex(s: str) -> str:
        return " ".join(f"U+{ord(c):04X}" for c in (s or ""))

    def _norm(s: str) -> str:
        # 전각 ＆(U+FF06) → 반각 &, ZWJ 제거, 공백 정리
        return (s or "").replace('＆', '&').replace('\u200d', '').strip()

    def _sample(rows: list, limit: int = 3) -> list:
        out = []
        for r in (rows or [])[:limit]:
            bn = str(r.get('brand_name') or '')
            out.append({
                "brand_code":       str(r.get('brand_code') or ''),
                "brand_name":       bn,
                "brand_name_hex":   _hex(bn),
                "exact_eq_input":   bn == name,
                "norm_eq_input":    _norm(bn) == _norm(name),
            })
        return out

    def _count_safe(sql: str) -> int:
        try:
            r = main._safe_query(sql, raw=True) or []
            return int((r[0] or {}).get('n', 0)) if r else 0
        except Exception:
            return -1

    input_norm = _norm(name)

    # 1) 전체 행수 (테이블 존재 확인)
    total_n = _count_safe(f"SELECT COUNT(*) AS n FROM {T_BRAND_SUMMARY}")

    # 2) 정확 매칭
    try:
        exact = main._safe_query(
            f"SELECT * FROM {T_BRAND_SUMMARY} WHERE brand_name = {_sql(name)}",
            raw=True,
        ) or []
    except Exception as e:
        exact = []
        exact_err = str(e)
    else:
        exact_err = None

    # 3) 정규화(전각→반각) 매칭
    try:
        normalized = main._safe_query(
            f"SELECT * FROM {T_BRAND_SUMMARY} "
            f"WHERE REPLACE(brand_name, '＆', '&') = {_sql(input_norm)}",
            raw=True,
        ) or []
    except Exception as e:
        normalized = []
        norm_err = str(e)
    else:
        norm_err = None

    # 4) LIKE 유사 검색 (앞 3~5글자)
    like_key = input_norm[:4] if len(input_norm) >= 4 else input_norm
    try:
        like_rows = main._safe_query(
            f"SELECT brand_code, brand_name FROM {T_BRAND_SUMMARY} "
            f"WHERE brand_name LIKE {_sql('%' + like_key + '%')} LIMIT 20",
            raw=True,
        ) or []
    except Exception as e:
        like_rows = []
        like_err = str(e)
    else:
        like_err = None

    # 5) 다른 테이블에도 존재하는지
    monthly_n = _count_safe(
        f"SELECT COUNT(*) AS n FROM {T_BRAND_MONTHLY} WHERE brand_name = {_sql(name)}")
    cust_n = _count_safe(
        f"SELECT COUNT(*) AS n FROM {T_BRAND_CUST} WHERE brand_name = {_sql(name)}")
    brands_n_exact = _count_safe(
        f"SELECT COUNT(*) AS n FROM {T_BRANDS} WHERE brand_name = {_sql(name)}")
    brands_n_emp = -1
    if emp:
        brands_n_emp = _count_safe(
            f"SELECT COUNT(*) AS n FROM {T_BRANDS} "
            f"WHERE brand_name = {_sql(name)} AND emp_code = {_sql(emp)}")

    # 6) 진단 결론
    if exact_err:
        diagnosis = f"❌ T_BRAND_SUMMARY 쿼리 자체가 예외 발생: {exact_err} → fallback 확정 원인"
    elif exact:
        diagnosis = ("✅ T_BRAND_SUMMARY 에 exact 매칭 존재. "
                     "그럼에도 fallback 이 걸린다면 원인은 (a) T_BRAND_CUST cust_rows empty (지금 코드는 fallback 안 시킴) "
                     "(b) 병렬 쿼리 중 예외 발생 → read_brand_report_from_table 이 None 반환.")
    elif normalized:
        diagnosis = ("⚠️ 정규화(전각＆→반각&) 후 매칭됨. "
                     "저장된 brand_name 이 입력과 문자 다름. refresh 스크립트에서 정규화하거나 "
                     "조회 시 REPLACE 적용 필요.")
    elif like_rows:
        diagnosis = (f"⚠️ 유사 브랜드 {len(like_rows)}건 발견. 저장된 실제 brand_name 을 확인해 "
                     "정확한 문자열로 조회하도록 매칭 로직 수정 필요.")
    elif total_n == 0:
        diagnosis = "❌ T_BRAND_SUMMARY 가 비어있음. refresh 실행 필요."
    elif total_n < 0:
        diagnosis = "❌ T_BRAND_SUMMARY 테이블 조회 실패. 테이블 미존재 또는 권한 문제."
    else:
        diagnosis = (f"❌ T_BRAND_SUMMARY({total_n}행) 에 이 브랜드가 아예 없음. "
                     "refresh 스크립트의 GROUP BY 조건에서 누락됨 (예: ZC본부명 필터/집계 기준).")

    return JSONResponse({
        "input": {
            "raw":                        name,
            "hex":                        _hex(name),
            "length":                     len(name),
            "normalized":                 input_norm,
            "normalized_hex":             _hex(input_norm),
            "contains_fullwidth_amp":     '＆' in name,
            "contains_halfwidth_amp":     '&' in name,
            "contains_zwj":               '\u200d' in name,
            "contains_trailing_space":    name != name.strip(),
        },
        "T_BRAND_SUMMARY": {
            "total_rows_in_table":        total_n,
            "exact_match_count":          len(exact),
            "exact_samples":              _sample(exact),
            "exact_query_error":          exact_err,
            "normalized_match_count":     len(normalized),
            "normalized_samples":         _sample(normalized),
            "normalized_query_error":     norm_err,
            "like_search_key":            like_key,
            "like_match_count":           len(like_rows),
            "like_samples":               _sample(like_rows, limit=20),
            "like_query_error":           like_err,
        },
        "other_tables_exact_match_count": {
            "T_BRAND_MONTHLY":            monthly_n,
            "T_BRAND_CUST":               cust_n,
            "T_BRANDS_all_emp":           brands_n_exact,
            "T_BRANDS_this_emp":          brands_n_emp,
        },
        "diagnosis": diagnosis,
    })


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
    password = form.get("password", "").strip()
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")[:300]
    user = _portal_user(emp_code)
    if not user:
        # 실패 이유 판단 - 베타게이트 활성 중이면 베타 미등록으로 표기
        in_beta = emp_code in access_control.load_beta_testers()
        if in_beta and access_control.beta_gate_active():
            reason = "beta_not_allowed"
        else:
            reason = "not_in_sales_whitelist"
        portal_db.record_login(emp_code, "", "", ip, ua, False, reason)
        if reason == "beta_not_allowed":
            return _redirect_msg("/portal/login", error=access_control.beta_denied_message("세일즈 액션 플랫폼"))
        return _redirect_msg("/portal/login", error="외식식재사업부 화이트리스트에 등록된 사번만 이용 가능합니다.")

    # ── 비밀번호 미설정 계정: 비밀번호 설정 페이지로 ──────────────────────
    if not portal_db.has_password(emp_code):
        portal_db.record_login(emp_code, user["name"], user["team"], ip, ua, True, "no_password_redirect")
        response = _redirect("/portal/set-password")
        response.set_cookie(_SESSION_COOKIE, _make_session(emp_code), max_age=600,
                            httponly=True, samesite="lax")
        return response

    # ── 비밀번호 검증 ──────────────────────────────────────────────────
    pw_result = portal_db.check_password(emp_code, password)
    if not pw_result["ok"]:
        portal_db.record_login(emp_code, user["name"], user["team"], ip, ua, False, "wrong_password")
        return _redirect_msg("/portal/login", error=pw_result["reason"])

    # ── 비밀번호 변경 요구 (관리자 초기화 후 첫 로그인) ───────────────────
    if pw_result.get("must_change"):
        portal_db.record_login(emp_code, user["name"], user["team"], ip, ua, True, "must_change_pw")
        response = _redirect("/portal/set-password?must_change=1")
        response.set_cookie(_SESSION_COOKIE, _make_session(emp_code), max_age=600,
                            httponly=True, samesite="lax")
        return response

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
    # 첫 로그인 시 사이드바 펼침 강제 (JS에서 sb_fresh 감지 후 localStorage 초기화)
    response.set_cookie("sb_fresh", "1", max_age=10, samesite="lax")

    # ── 백그라운드 prefetch: 사용자가 대시보드 보는 동안 분포분석용 캐시를 미리 채움 ─────
    # 목적: 로그인 → 분포분석 클릭 시 `pick_s` 를 캐시 히트로 만들어 첫 접속 지연 제거
    def _prefetch_user_data(_emp: str):
        _lg = logging.getLogger("portal_prefetch")
        try:
            _t0 = time.time()
            # 1) 브랜드 목록 캐시 (분포분석의 pick_s 를 즉시 응답으로 만듦)
            brands = _brand_rows(_emp)
            _t1 = time.time()
            _lg.info(f"[prefetch] {_emp} brands={len(brands)} in {_t1-_t0:.2f}s")

            # 2) 대표 브랜드 1개의 사전계산 데이터도 미리 채워두면 첫 클릭이 즉시 반환
            first_brand = None
            for _b in brands:
                bn = str(_b.get("brand_name") or "")
                if bn and bn != "🧑\u200d🍳일반외식업장":  # 가상 브랜드 제외
                    first_brand = bn
                    break
            if first_brand:
                try:
                    from portal_refresh import read_brand_report_from_table
                    read_brand_report_from_table(_emp, first_brand, ym_mode="prev")
                    _t2 = time.time()
                    _lg.info(f"[prefetch] {_emp} brand={first_brand} in {_t2-_t1:.2f}s")
                except Exception as _pe:
                    _lg.warning(f"[prefetch] {_emp} brand prefetch 실패: {_pe}")
        except Exception as _e:
            _lg.warning(f"[prefetch] {_emp} 실패: {_e}")

    threading.Thread(
        target=_prefetch_user_data, args=(user["emp_code"],),
        daemon=True, name=f"prefetch-{user['emp_code']}",
    ).start()

    return response


@router.post("/logout")
async def logout(request: Request):
    response = _redirect("/portal/login")
    response.delete_cookie(_SESSION_COOKIE)
    return response


# ── 비밀번호 설정 (최초 로그인 / 관리자 초기화 후) ──────────────────────────

@router.get("/set-password", response_class=HTMLResponse)
async def set_password_page(request: Request, must_change: str = ""):
    user = _require_user(request)
    return _render(request, "portal_set_password.html", user=user, must_change=bool(must_change),
                   error=request.query_params.get("error", ""))

@router.post("/set-password", response_class=HTMLResponse)
async def set_password_post(request: Request):
    user = _require_user(request)
    form = await _read_form(request)
    pw1 = form.get("password", "").strip()
    pw2 = form.get("password2", "").strip()
    if len(pw1) < 6:
        return _render(request, "portal_set_password.html", user=user, must_change=False,
                       error="비밀번호는 6자 이상이어야 합니다.")
    if pw1 != pw2:
        return _render(request, "portal_set_password.html", user=user, must_change=False,
                       error="비밀번호가 일치하지 않습니다.")
    portal_db.set_password(user["emp_code"], pw1, must_change=False)
    return _redirect("/portal")


# ── 관리자 기능 ──────────────────────────────────────────────────────────────

def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if user["emp_code"] != access_control.ADMIN_EMP_CODE:
        raise HTTPException(status_code=403, detail="관리자 전용입니다.")
    return user

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    _require_admin(request)
    users = portal_db.list_login_users()
    pw_status = portal_db.list_password_status([u["emp_code"] for u in users])
    login_logs = portal_db.list_login_logs(limit=100)
    return _render(request, "portal_admin_users.html",
                   users=users, pw_status=pw_status, login_logs=login_logs)

@router.post("/admin/reset-password")
async def admin_reset_password(request: Request):
    _require_admin(request)
    form = await _read_form(request)
    emp_code = form.get("emp_code", "").strip()
    if not emp_code:
        raise HTTPException(status_code=400, detail="emp_code 필요")
    temp_pw = portal_db.reset_password(emp_code)
    return JSONResponse({"ok": True, "temp_pw": temp_pw, "emp_code": emp_code})

@router.post("/admin/unlock-account")
async def admin_unlock_account(request: Request):
    _require_admin(request)
    form = await _read_form(request)
    emp_code = form.get("emp_code", "").strip()
    if not emp_code:
        raise HTTPException(status_code=400, detail="emp_code 필요")
    portal_db.unlock_account(emp_code)
    return JSONResponse({"ok": True, "emp_code": emp_code})


@router.get("/brand-report", response_class=HTMLResponse)
async def brand_report_page(
    request: Request,
    brand: str = "",
    threshold: float | None = None,
    sent: str = "",
):
    """DB 쿼리 없이 스켈레톤 HTML 즉시 반환. 브랜드 목록+데이터는 /brand-report-data AJAX로 로딩."""
    _require_user(request)
    response = _render(request, "portal_brand_report.html",
                   selected_brand=brand,
                   threshold=threshold or "", sent=sent)
    # 브라우저 캐시 방지 (배포 후 즉시 반영)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@router.get("/brand-report-data")
async def brand_report_data_api(
    request: Request,
    brand: str = "",
    threshold: float | None = None,
    customer_page: int = 1,
    target_page: int = 1,
    ym: str = "prev",  # "prev"=전월 / "current"=당월
):
    """brand-report 데이터 JSON 반환 (AJAX 전용)."""
    user = _require_user(request)
    emp_code = user["emp_code"]
    ym_mode = (ym or "prev").lower()
    if ym_mode not in ("prev", "current"):
        ym_mode = "prev"
    t0 = time.time()
    try:
        # ── 1단계: _pick_brand (브랜드 목록 조회) ───────────────────
        t1 = time.time()
        picked = _pick_brand(brand or None, emp_code)
        t2 = time.time()
        logger.info(f"[brand-data] {emp_code} _pick_brand={t2-t1:.2f}s picked={picked.get('brand_name') if picked else None} ym_mode={ym_mode}")

        if not picked:
            # T_BRANDS 없음 → live query fallback으로 브랜드 목록 시도
            logger.warning(f"[brand-data] {emp_code} no brand in T_BRANDS, trying live fallback")
            try:
                data = brand_report(brand or None, emp_code=emp_code,
                                    threshold_pct=threshold,
                                    customer_page=customer_page, target_page=target_page,
                                    ym_mode=ym_mode)
                t_fb = time.time()
                data["_timing"] = {"pick_s": round(t2-t1,2), "query_s": round(t_fb-t2,2),
                                   "total_s": round(t_fb-t0,2), "source": "live_no_cache"}
                return JSONResponse(content=_json_safe(data))
            except Exception as _fe:
                logger.error(f"[brand-data] {emp_code} live fallback failed: {_fe}")
                return JSONResponse(content={"error": f"브랜드 데이터 없음 (refresh 필요). {_fe}"}, status_code=404)

        # ── 2단계: 사전계산 테이블 조회 ──────────────────────────────
        t3 = time.time()
        from portal_refresh import read_brand_report_from_table
        cached = read_brand_report_from_table(
            emp_code,
            str(picked.get("brand_name") or ""),
            threshold_pct=threshold,
            customer_page=customer_page, target_page=target_page,
            ym_mode=ym_mode,
        )
        t4 = time.time()
        logger.info(f"[brand-data] {emp_code} precomputed={t4-t3:.2f}s hit={'YES' if cached else 'NO(fallback)'}")

        if cached is not None:
            cached["_timing"] = {"pick_s": round(t2-t1,2), "query_s": round(t4-t3,2), "total_s": round(t4-t0,2)}
            return JSONResponse(content=_json_safe(cached))

        # ── 3단계: fallback 실시간 쿼리 ──────────────────────────────
        logger.warning(f"[brand-data] {emp_code} fallback to live query brand={picked.get('brand_name')} ym_mode={ym_mode}")
        data = brand_report(
            str(picked.get("brand_name") or ""), emp_code=emp_code,
            threshold_pct=threshold,
            customer_page=customer_page, target_page=target_page,
            ym_mode=ym_mode,
        )
        t5 = time.time()
        logger.info(f"[brand-data] {emp_code} fallback_live={t5-t4:.2f}s total={t5-t0:.2f}s")
        data["_timing"] = {"pick_s": round(t2-t1,2), "query_s": round(t5-t3,2), "total_s": round(t5-t0,2), "source": "fallback"}
        return JSONResponse(content=_json_safe(data))
    except Exception as _e:
        logger.error(f"[brand-report-data] {emp_code} error={_e} elapsed={time.time()-t0:.2f}s", exc_info=True)
        return JSONResponse(content={"error": str(_e)}, status_code=500)


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

    # plant_code: 사전계산 테이블 우선, fallback 실시간
    plant_code = str(customer.get("plant_code") or "")
    if not plant_code:
        import main
        prev_ym = report.get("prev_ym") or ""
        _prows = _q(f"""
            SELECT MAX(`플랜트`) AS plant_code
            FROM {main.T_MAIN}
            WHERE `거래처` = {_sql(code)} AND `년월` = {_sql(prev_ym)} LIMIT 1
        """) if prev_ym else []
        plant_code = str((_prows[0] or {}).get("plant_code") or "") if _prows else ""

    return JSONResponse(_json_safe({
        "customer_code": code,
        "customer_name": customer.get("customer_name") or "",
        "plant_code": plant_code,
        "brand_avg": float(report.get("brand_avg") or 0),
        "products": products,
        "product_names": ", ".join(str(p.get("product_name") or p.get("product_code") or "") for p in products),
        "dm_message": _dm_message(bname, customer, float(report.get("brand_avg") or 0), products),
    }))


@router.get("/brand-report/action", response_class=HTMLResponse)
async def brand_report_action_page(
    request: Request,
    brand: str = "",
    threshold: float | None = None,
    customer_page: int = 1,
    target_page: int = 1,
):
    user = _require_user(request)
    report = brand_report(brand or None, emp_code=user["emp_code"],
                          threshold_pct=threshold, customer_page=customer_page, target_page=target_page)
    return _render(request, "portal_brand_report_action.html", report=report)


@router.get("/brand-report/results", response_class=HTMLResponse)
async def brand_report_results_page(
    request: Request,
    brand_code: str = "",
    action_ym: str = "",
):
    """액션 실적 상세 페이지."""
    import traceback as _tb
    user = _require_user(request)
    try:
        from portal_refresh import read_action_results
        rows = read_action_results(user["emp_code"], brand_code=brand_code, action_ym=action_ym)
    except Exception:
        rows = []
    try:
        brands = _brand_rows(user["emp_code"])
    except Exception:
        brands = []
    total_sales   = sum(r.get("sales_after_m") or 0 for r in rows)
    total_gp      = sum(r.get("gp_after_m") or 0 for r in rows)
    total_generic = sum(r.get("generic_sales_after_m") or 0 for r in rows)
    summary = {
        "count":                 len(rows),
        "total_sales_after_m":   total_sales,
        "total_gp_after_m":      total_gp,
        "total_generic_after_m": total_generic,
        "avg_gp_rate":           round(total_gp / total_sales * 100, 1) if total_sales else 0,
    }
    try:
        dm_logs = portal_db.list_dm_logs(emp_code=user["emp_code"], brand_code=brand_code or None, action_ym=action_ym or None)
    except Exception:
        dm_logs = []
    try:
        return _render(request, "portal_brand_report_results.html",
                       rows=rows, brands=brands, summary=summary,
                       sel_brand_code=brand_code, sel_action_ym=action_ym,
                       dm_logs=dm_logs)
    except Exception as e:
        logger.error(f"[results page] render error: {e}", exc_info=True)
        return HTMLResponse(f"<pre style='color:red'>렌더링 오류:\n{_tb.format_exc()}</pre>", status_code=500)


@router.get("/action-results")
async def action_results(
    request: Request,
    brand_code: str = "",
    action_ym: str = "",
):
    """판가설정 액션 이후 실적 조회 (T_ACTION_RESULTS)."""
    user = _require_user(request)
    try:
        from portal_refresh import read_action_results
        rows = read_action_results(user["emp_code"], brand_code=brand_code, action_ym=action_ym)
    except Exception as e:
        rows = []
    total_sales = sum(r.get("sales_after_m", 0) for r in rows)
    total_gp    = sum(r.get("gp_after_m", 0) for r in rows)
    total_generic = sum(r.get("generic_sales_after_m", 0) for r in rows)
    return JSONResponse(_json_safe({
        "rows": rows,
        "summary": {
            "count":             len(rows),
            "total_sales_after_m":   total_sales,
            "total_gp_after_m":      total_gp,
            "total_generic_after_m": total_generic,
        },
    }))


@router.get("/action-matnr-sales")
async def action_matnr_sales(
    request: Request,
    customer_code: str = "",
    brand_code: str = "",
    action_date: str = "",
):
    """자재별 상세 실적 조회: dm_send_logs 판가 + T_MAIN 집계."""
    user = _require_user(request)
    if not customer_code or not brand_code or not action_date:
        return JSONResponse({"rows": [], "error": "customer_code/brand_code/action_date 필수"})
    # 1) dm_send_logs 에서 price_items 좌회 → 자재별 설정판가 맵
    logs = portal_db.list_dm_logs(emp_code=user["emp_code"], brand_code=brand_code or None)
    price_map: dict[str, int | None] = {}  # matnr → 판가(없으면 None)
    for log in logs:
        if str(log.get("customer_code") or "") != customer_code:
            continue
        pj = log.get("price_items_json") or ""
        if pj and pj not in ("[]", "null"):
            try:
                items = json.loads(pj)
                for it in items:
                    m = str(it.get("matnr") or "").strip()
                    p = it.get("price")
                    if m and m not in price_map:
                        price_map[m] = int(p) if p is not None else None
            except Exception:
                pass
        # product_names fallback
        if not price_map:
            for m in (log.get("product_names") or "").split(","):
                m = m.strip()
                if m:
                    price_map.setdefault(m, None)
    if not price_map:
        return JSONResponse({"rows": []})
    # 2) T_MAIN 에서 자재별 집계
    import main
    matnrs = list(price_map.keys())
    matnr_in = ", ".join(f"'{m}'" for m in matnrs)
    try:
        db_rows = main._safe_query(f"""
            SELECT TRIM(`자재`) AS matnr,
                   MAX(`자재명`) AS matnr_name,
                   ROUND(SUM(CASE WHEN `매출액` > 0 THEN `매출액` ELSE 0 END) / 10000.0, 2) AS sales_m,
                   ROUND((SUM(CASE WHEN `매출액` > 0 THEN `매출액` ELSE 0 END)
                         - SUM(CASE WHEN `매출액` > 0 THEN COALESCE(`매출원가`,0) ELSE 0 END)) / 10000.0, 2) AS gp_m,
                   COALESCE(SUM(CASE WHEN `매출액` > 0 THEN CAST(`매출수량` AS BIGINT) ELSE 0 END), 0) AS sales_qty,
                   COALESCE(SUM(CASE WHEN `매출액` = 0 AND `매출수량` > 0 THEN CAST(`매출수량` AS BIGINT) ELSE 0 END), 0) AS sample_qty
            FROM {main.T_MAIN}
            WHERE `거래처` = '{customer_code}'
              AND `ZC본부` = '{brand_code}'
              AND `대금청구일` >= '{action_date}'
              AND TRIM(`자재`) IN ({matnr_in})
            GROUP BY TRIM(`자재`)
            HAVING sales_m > 0 OR sample_qty > 0
        """, raw=True) or []
    except Exception as e:
        return JSONResponse({"rows": [], "error": str(e)})
    result_map = {str(r.get("matnr") or ""): r for r in db_rows}
    out = []
    for matnr in matnrs:
        r = result_map.get(matnr, {})
        sales_m   = float(r.get("sales_m") or 0)
        gp_m      = float(r.get("gp_m") or 0)
        sales_qty = int(r.get("sales_qty") or 0)
        sample_qty= int(r.get("sample_qty") or 0)
        net_qty   = sales_qty  # 매출액>0인 행만 이미 필터됨
        gp_rate   = round(gp_m / sales_m * 100, 1) if sales_m > 0 else 0.0
        out.append({
            "matnr":        matnr,
            "matnr_name":   str(r.get("matnr_name") or "-"),
            "set_price":    price_map.get(matnr),
            "sales_after_m": sales_m,
            "gp_after_m":   gp_m,
            "gp_rate":      gp_rate,
            "sales_qty":    sales_qty,
            "sample_qty":   sample_qty,
            "net_qty":      net_qty,
            "converted":    net_qty > 0,
        })
    return JSONResponse({"rows": out})


@router.post("/refresh-action-results")
async def portal_refresh_action_results(request: Request):
    """포털 사용자용 T_ACTION_RESULTS 수동 리프레시 (로그인 필요)."""
    _require_user(request)
    result_holder: dict = {}

    def _do():
        try:
            from portal_refresh import run_action_results_refresh
            result_holder.update(run_action_results_refresh())
        except Exception as e:
            result_holder.update({"status": "error", "reason": str(e)})

    t = threading.Thread(target=_do, daemon=False)
    t.start()
    t.join(timeout=120)
    if not result_holder:
        return JSONResponse({"status": "timeout", "action_rows": 0})
    return JSONResponse(result_holder)


@router.post("/dm-log")
async def dm_log(request: Request):
    user = _require_user(request)
    form = await _read_form(request)
    # DM 발송 기능 준비 중 - 로그 저장 제거
    return _redirect_msg("/portal/brand-report", brand=form.get("brand_name", ""))


def _call_sap_upload(items: list[dict]) -> dict:
    """SAP Bridge Agent (localhost:7788) 판가 등록 호출."""
    import httpx, os
    sap_url = os.getenv("SAP_BRIDGE_URL", "http://localhost:7788")
    try:
        r = httpx.post(
            f"{sap_url}/sap/upload-price",
            json={"items": items, "mode": "N"},
            timeout=120,
        )
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e), "saved_count": 0}


class _DmSendPayload(BaseModel):
    customer_code: str
    customer_name: str = ""
    brand_code: str = ""
    brand_name: str = ""
    action_type: str = "dm_only"      # "dm_only" | "price_and_dm" | "price_only"
    dm_message: str = ""
    price_items: list[dict] = []      # [{plant,kunnr,matnr,price,date_from,date_to}]
    dm_matnr_list: list[str] = []     # dm_only 시 추천 상품코드 목록
    sap_result: dict = {}             # 브라우저에서 SAP Bridge 직접 호출 후 결과 전달


@router.post("/dm-send-with-price")
async def dm_send_with_price(request: Request, body: _DmSendPayload):
    import json as _json
    user = _require_user(request)
    emp = user["emp_code"]
    emp_name = user.get("emp_name") or ""
    team = user.get("team") or ""

    # SAP 호출은 브라우저가 직접 수행 (localhost:7788), 서버는 결과만 수신·저장
    sap_result: dict = body.sap_result or {}
    saved_count = int(sap_result.get("saved_count") or (len(body.price_items) if body.price_items else 0))

    # product_names: price_items 있으면 matnr, dm_only면 dm_matnr_list, 없으면 메시지에서 추출 시도
    if body.price_items:
        _product_names = ", ".join(str(p.get("matnr") or "") for p in body.price_items)
    elif body.dm_matnr_list:
        _product_names = ", ".join(str(m) for m in body.dm_matnr_list)
    else:
        # 메시지에서 상품코드 패턴 추출 ([숫자] 형태)
        import re as _re_dm
        _product_names = ", ".join(_re_dm.findall(r'\[(\d{5,10})\]', body.dm_message))

    # ── DM 로그 저장 ──────────────────────────────────
    from portal_db import record_dm_log_v2
    record_dm_log_v2(
        emp_code=emp,
        emp_name=emp_name,
        team=team,
        brand_code=body.brand_code,
        brand_name=body.brand_name,
        customer_code=body.customer_code,
        customer_name=body.customer_name,
        action_type=body.action_type,
        product_names=_product_names,
        message=body.dm_message,
        price_items_json=_json.dumps(body.price_items, ensure_ascii=False),
        sap_saved_count=saved_count,
        sap_result_json=_json.dumps(sap_result, ensure_ascii=False),
        status=("price_applied_dm_sent" if body.action_type == "price_and_dm"
            else "price_only" if body.action_type == "price_only"
            else "dm_only_sent"),
    )

    # ── 판가설정 또는 DM 발송 액션 시 실적 테이블 백그라운드 갱신 ──────────
    if body.action_type in ("price_and_dm", "price_only", "dm_only"):
        def _refresh_action_results():
            try:
                from portal_refresh import run_action_results_refresh
                run_action_results_refresh()
            except Exception as e:
                logger.warning(f"[dm-send] action_results 백그라운드 갱신 실패: {e}")
        threading.Thread(target=_refresh_action_results, daemon=True, name="action-results-refresh").start()

    return JSONResponse({
        "success": True,
        "action_type": body.action_type,
        "sap_saved_count": saved_count,
        "dm_status": "logged",
    })


@router.get("/dm-log-list")
async def dm_log_list(request: Request, brand_code: str = "", cust_code: str = "", limit: int = 100):
    """판가설정/DM 발송 이력 조회 (브랜드 + 고객 필터)."""
    import json as _json
    _require_user(request)
    from portal_db import list_dm_logs
    all_rows = list_dm_logs(limit=500)
    rows = [
        r for r in all_rows
        if (not brand_code or r.get("brand_code") == brand_code)
        and (not cust_code or r.get("customer_code") == cust_code)
    ][:limit]
    # price_items_json → 품목명 목록 파싱
    for r in rows:
        try:
            items = _json.loads(r.get("price_items_json") or "[]")
            r["product_list"] = [str(i.get("matnr") or "") for i in items if i.get("matnr")]
            r["price_item_count"] = len(items)
        except Exception:
            r["product_list"] = []
            r["price_item_count"] = 0
    return JSONResponse({"rows": rows})


# ── 백그라운드 스레드 싱글턴 가드 ────────────────────────────────
# 배경(2026-07-30):
#   Azure App Service 에서 gunicorn -w 4 로 뜨면 각 워커가 이 모듈을 import 하면서
#   warmup / keepalive / refresh 스케쥴러가 4번 중복 실행되어
#   - Databricks 커넥션/워크로드 4배 부담
#   - CREATE OR REPLACE TABLE 이 4개 워커에서 동시에 → Delta MetadataChangedException
#   문제가 발생했다.
#
# 해결:
#   1) gunicorn.conf.py 로 workers=1 을 기본값으로 고정 (1차 방어)
#   2) 그럼에도 누군가 워커를 늘렸을 때를 대비, POSIX 파일락으로 프로세스간
#      싱글턴을 보장 (2차 방어). 락을 얻지 못한 워커는 해당 스레드를 스킵.
#   3) Windows 로컬 개발환경에는 fcntl 이 없으므로 락 없이 그대로 실행
#      (로컬은 단일 프로세스라 문제 없음).
_SINGLETON_FDS: dict[str, int] = {}


def _acquire_singleton_lock(name: str) -> bool:
    """서버 프로세스 간 배타 락. True 반환 시 이 프로세스가 리더."""
    # Windows(개발환경) → fcntl 없음 → 항상 True (단일 프로세스라 안전)
    try:
        import fcntl  # type: ignore
    except ImportError:
        return True

    lock_dir = os.getenv("DATA_DIR", "/tmp")
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError:
        lock_dir = "/tmp"
    lock_path = os.path.join(lock_dir, f".portal_singleton_{name}.lock")
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError as e:
        logger.warning(f"[singleton] {name} 락 파일 열기 실패({e}) → 허용")
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        logger.info(f"[singleton] {name}: 다른 워커가 보유 중 → 스킵")
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    # fd 를 모듈 전역에 보관해서 프로세스 종료 시까지 락 유지
    _SINGLETON_FDS[name] = fd
    logger.info(f"[singleton] {name}: pid={os.getpid()} 리더 획득")
    return True


# ── 서버 시작 시 백그라운드 캐시 워밍업 ──────────────────────────────
def _warmup_cache():
    """서버 시작 직후 Databricks Warehouse 를 즉시 깨우고 주요 캐시를 채운다.

    순서가 매우 중요:
      (1) T_BRANDS / T_DASH ping = warehouse-wake  ← 사용자 첫 클릭 대응
      (2) 무거운 admin/whitelist 캐시              ← 로그인/관리 페이지 대응
    이전에는 sleep(30) + admin_overview 를 먼저 돌려 warehouse-wake 가
    부팅 후 60초 이상 지나서야 발생 → 첫 사용자가 콜드 스타트 세금을 그대로 냈다.
    """
    import logging
    _log = logging.getLogger("portal_warmup")
    # gunicorn 워커가 완전히 앱을 import 하고 라우터가 준비된 직후에 시작.
    # 굳이 30초를 기다릴 필요가 없다. (짧게 2초만 대기해서 다른 부팅 스레드와 순서만 정렬)
    time.sleep(2)

    # ── (1) 최우선: Databricks Warehouse 예열 + 사전계산 테이블 세션 오픈 ──
    #   이게 첫 사용자 pick_s 를 결정한다. 다른 warmup 보다 먼저 실행.
    try:
        _log.info("[warmup] 브랜드 리포트 사전계산 테이블 워밍업 시작 (warehouse-wake)")
        import portal_refresh as _pr
        import main as _main
        # T_BRANDS 를 첫 번째로 ping — 이 쿼리 하나가 warehouse 를 깨우고
        # 커넥션 풀 슬롯 하나를 warm 상태로 유지시킨다.
        for _t in (_pr.T_BRANDS, _pr.T_DASH, _pr.T_BRAND_SUMMARY, _pr.T_BRAND_CUST, _pr.T_BRAND_MONTHLY):
            try:
                _t_ping = time.time()
                _main._safe_query(f"SELECT 1 FROM {_t} LIMIT 1", raw=True)
                _log.info(f"[warmup] {_t} ping ok ({time.time()-_t_ping:.2f}s)")
            except Exception as _te:
                _log.warning(f"[warmup] {_t} ping 실패: {_te}")
        _log.info("[warmup] 브랜드 리포트 사전계산 테이블 워밍업 완료")
    except Exception as e:
        _log.warning(f"[warmup] 브랜드 리포트 워밍업 실패: {e}")

    # ── (1.2) 커넥션 풀 병렬 예열 ──────────────────────────────
    #   Databricks Serverless SQL Warehouse 는 세션마다 다른 컴퓨트 인스턴스에
    #   라우팅될 수 있다. 위 (1) 은 커넥션 1개만 warm 시키므로, 두 번째 사용자가
    #   다른 세션을 요청하면 다시 콜드 스핀업 (15~30s) 겪는다.
    #   → DB_POOL_SIZE 만큼 병렬로 SELECT 실행해서 풀의 모든 슬롯을 warm 유지.
    try:
        _log.info("[warmup] 커넥션 풀 병렬 예열 시작")
        import portal_refresh as _pr
        import main as _main
        _pool_size = int(os.getenv("DB_POOL_SIZE", "5"))

        def _warm_slot(_idx: int) -> float:
            _t = time.time()
            _main._safe_query(f"SELECT {_idx} AS x FROM {_pr.T_DASH} LIMIT 1", raw=True)
            return time.time() - _t

        _t_pool = time.time()
        with ThreadPoolExecutor(max_workers=_pool_size) as _ex:
            _futures = [_ex.submit(_warm_slot, i) for i in range(_pool_size)]
            _slot_times = []
            for _f in as_completed(_futures):
                try:
                    _slot_times.append(_f.result())
                except Exception as _pe:
                    _log.warning(f"[warmup] pool slot 예열 실패: {_pe}")
        _log.info(
            f"[warmup] 커넥션 풀 병렬 예열 완료 ({_pool_size} slots, "
            f"total={time.time()-_t_pool:.2f}s, max_slot={max(_slot_times or [0]):.2f}s)"
        )
    except Exception as e:
        _log.warning(f"[warmup] 커넥션 풀 예열 실패: {e}")

    # ── (1.5) 결정타: T_BRANDS 를 한 번에 읽어 전 사용자 캐시 populate ──
    #   이렇게 하면 부팅 후 처음 로그인하는 어떤 사용자든 분포분석 첫 클릭이
    #   메모리 캐시 히트 → pick_s ≈ 0.00s 로 즉시 응답.
    try:
        _t_bulk = time.time()
        _n = _bulk_warm_brand_rows()
        _log.info(f"[warmup] T_BRANDS bulk cache 채움: {_n}명 in {time.time()-_t_bulk:.2f}s")
    except Exception as e:
        _log.warning(f"[warmup] T_BRANDS bulk cache 실패: {e}")

    # ── (2) 화이트리스트: 로그인 속도 개선 ────────────────────────
    try:
        _log.info("[warmup] 영업사원 화이트리스트 캐시 워밍업 시작")
        _employee_whitelist()
        _log.info("[warmup] 영업사원 화이트리스트 캐시 완료")
    except Exception as e:
        _log.warning(f"[warmup] 화이트리스트 실패: {e}")

    # ── (3) 관리자 대시보드: 관리자 접속 시 즉시 응답 ──────────────
    try:
        _log.info("[warmup] 관리자 대시보드 캐시 워밍업 시작")
        portal_admin_overview({})
        _log.info("[warmup] 관리자 대시보드 캐시 완료")
    except Exception as e:
        _log.warning(f"[warmup] 관리자 대시보드 실패: {e}")

    # ── (4) 사업부 기준 데이터 ────────────────────────────────────
    try:
        _log.info("[warmup] 사업부 기준 데이터 워밍업 시작")
        _division_latest_ym()
        _employee_whitelist()
        _log.info("[warmup] 사업부 기준 데이터 완료")
    except Exception as e:
        _log.warning(f"[warmup] 사업부 데이터 실패: {e}")


# ── Databricks SQL Warehouse Keepalive ──────────────────────────────
# Warehouse idle timeout(기본 10분) 전에 가벼운 쿼리를 계속 날려서 항상 warm 유지.
# 이걸 안 하면 첫 접속자마다 15~30초 콜드 스타트를 겪게 된다.
def _databricks_keepalive():
    import logging
    _log = logging.getLogger("portal_keepalive")
    interval_sec = int(os.getenv("DBX_KEEPALIVE_SEC", "300"))  # 기본 5분
    # T_BRANDS bulk cache 재갱신 주기 (기본 30분) — 6h TTL 만료 전에 계속 채워둠
    bulk_refresh_sec = int(os.getenv("BULK_WARM_SEC", "1800"))
    _last_bulk = time.time()  # warmup 에서 이미 채웠으므로 지금 시각으로 시작
    # 첫 사이클: 부팅 warmup(30초) 완료 후 약 60초 시점에 실행 → 이후 interval_sec 간격
    time.sleep(60)
    while True:
        try:
            import portal_refresh as _pr
            import main as _main
            # 초경량 SELECT — 인덱스/파티션 필요 없고 캐시된 metadata 로 즉시 응답
            _t0 = time.time()
            _main._safe_query(f"SELECT 1 AS x FROM {_pr.T_DASH} LIMIT 1", raw=True)
            _elapsed = round(time.time() - _t0, 2)
            _log.info(f"[keepalive] ok ({_elapsed}s)")
            # bulk_refresh_sec 간격으로 T_BRANDS 캐시 재populate
            if time.time() - _last_bulk >= bulk_refresh_sec:
                try:
                    _t_b = time.time()
                    _n = _bulk_warm_brand_rows()
                    _log.info(f"[keepalive] T_BRANDS bulk cache 재갱신: {_n}명 in {time.time()-_t_b:.2f}s")
                    _last_bulk = time.time()
                except Exception as _be:
                    _log.warning(f"[keepalive] bulk cache 재갱신 실패: {_be}")
        except Exception as e:
            _log.warning(f"[keepalive] 실패 (다음 주기 재시도): {e}")
        time.sleep(interval_sec)


def _auto_refresh_scheduler():
    """6시간마다 사전계산 테이블 자동 재생성. 서버 시작 시 자동 실행."""
    import logging
    _log = logging.getLogger("portal_refresh_scheduler")
    # 첫 실행: 서버 기동 2분 후 (warmup 완료 대기)
    time.sleep(120)
    while True:
        try:
            _log.info("[scheduler] 사전계산 테이블 자동 refresh 시작")
            import portal_refresh
            result = portal_refresh.run_refresh(force=True)
            _log.info(f"[scheduler] refresh 완료: {result.get('status')} / {result.get('emp_count')}명 / {result.get('elapsed_sec')}s")
            # refresh 성공 시 T_BRANDS bulk cache 도 즉시 재갱신 (stale data 방지)
            if result.get("status") in ("ok", "success"):
                try:
                    _t_b = time.time()
                    _n = _bulk_warm_brand_rows()
                    _log.info(f"[scheduler] T_BRANDS bulk cache 재갱신: {_n}명 in {time.time()-_t_b:.2f}s")
                except Exception as _be:
                    _log.warning(f"[scheduler] bulk cache 재갱신 실패: {_be}")
        except Exception as e:
            _log.warning(f"[scheduler] refresh 실패 (6시간 후 재시도): {e}")
        time.sleep(6 * 3600)  # 6시간 대기


# ── 백그라운드 스레드 기동 (싱글턴 락으로 보호) ────────────────
# workers=1 이 기본이지만 만약 -w N 으로 뜨더라도 아래 락이 리더 워커 하나만
# 아래 세 스레드를 돌리도록 보장한다. 리더가 아닌 워커는 요청 처리만 수행.
#
# 주의: warmup 은 **각 워커별 커넥션 풀 예열**을 위해 리더 여부와 무관하게
# 모두 실행하는 편이 콜드스타트 완화에 유리하다. 반면 keepalive/scheduler 는
# Databricks 자원과 Delta 커밋 충돌을 유발하므로 리더 하나만 실행한다.
threading.Thread(target=_warmup_cache, daemon=True, name="portal-warmup").start()

if _acquire_singleton_lock("keepalive"):
    threading.Thread(target=_databricks_keepalive, daemon=True, name="portal-dbx-keepalive").start()
else:
    logger.info("[boot] keepalive 스레드 스킵 (다른 워커가 리더)")

if _acquire_singleton_lock("refresh-scheduler"):
    threading.Thread(target=_auto_refresh_scheduler, daemon=True, name="portal-refresh-scheduler").start()
else:
    logger.info("[boot] refresh-scheduler 스레드 스킵 (다른 워커가 리더)")
