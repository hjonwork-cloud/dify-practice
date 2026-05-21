"""
Databricks → Dify 연결용 FastAPI 미들웨어 서버
- 서버 시작 시 브라우저 OAuth 인증 1회 수행 후 토큰 캐시
- Dify HTTP Tool에서 이 서버의 엔드포인트를 호출
"""

from fastapi import FastAPI, HTTPException, Security, Depends, BackgroundTasks, Request
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from databricks.sdk import WorkspaceClient
from databricks import sql as dbsql
import os, logging, time, hashlib
import re
import calendar
import datetime as _dt_mod
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.error
import json as json_mod
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── 설정 ────────────────────────────────────────────────
HOST       = "https://adb-707807361397497.17.azuredatabricks.net"
HTTP_PATH  = "/sql/1.0/warehouses/acc2ec933ffef2d0"
API_KEY    = os.getenv("DIFY_API_KEY", "dify-secret-1234")   # Dify에서 호출 시 사용할 키
DIFY_BASE  = os.getenv("DIFY_BASE_URL", "https://api-dify-poc.dongwon.com")  # Dify Enterprise API
DIFY_TOKEN = os.getenv("DIFY_API_TOKEN", "app-jyij8qDVuJHBQojM8Hxj7wgu")
# ────────────────────────────────────────────────────────

app = FastAPI(title="Databricks-Dify Bridge", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 인증 토큰 캐시 ──────────────────────────────────────
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".token_cache")
_cached_token: str | None = None

# /query 응답 캐시 (SQL hash → (저장시각, 결과)) — 60초 유효
_query_cache: dict[str, tuple[float, dict]] = {}
_QUERY_CACHE_TTL = 60  # 초
_workspace_client: WorkspaceClient | None = None

def _load_token_from_file() -> str | None:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            t = f.read().strip()
            return t if t else None
    return None

def _save_token_to_file(token: str):
    with open(TOKEN_FILE, "w") as f:
        f.write(token)

def _clear_token_cache():
    global _cached_token
    _cached_token = None
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

def get_token() -> str:
    global _cached_token, _workspace_client
    if _cached_token:
        return _cached_token
    # 파일 캐시에서 먼저 시도
    saved = _load_token_from_file()
    if saved:
        _cached_token = saved
        logger.info("✅ 저장된 토큰 로드 완료")
        return _cached_token
    # 없으면 브라우저 인증
    logger.info("브라우저 인증 시작... 팝업 창에서 로그인해주세요.")
    _workspace_client = WorkspaceClient(host=HOST, auth_type="external-browser")
    me = _workspace_client.current_user.me()
    logger.info(f"✅ 로그인 계정: {me.user_name}")
    # SDK 헤더에서 Bearer 토큰 추출
    headers = _workspace_client.config.authenticate()
    token = headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise ValueError("토큰 추출 실패. /auth/reset 후 다시 시도해주세요.")
    _cached_token = token
    _save_token_to_file(token)
    logger.info("✅ 토큰 저장 완료")
    return _cached_token

# 서버 시작 시 파일 캐시 자동 로드
_cached_token = _load_token_from_file()
if _cached_token:
    logger.info("✅ 시작 시 저장된 토큰 로드됨")


NAME_FILTER_COLUMNS = [
    "ZA거래처명",
    "거래처명",
    "영업사원",
    "영업사원명",
    "담당자",
]


def _normalize_name_filter_sql(sql_text: str) -> str:
    col_group = "|".join(re.escape(col) for col in NAME_FILTER_COLUMNS)
    identifier = r"(?:[A-Za-z_][A-Za-z0-9_]*|`[^`]+`)"
    qualified_col = rf"((?:{identifier}\s*\.\s*)?`?(?:{col_group})`?)"

    like_pattern = re.compile(
        rf"{qualified_col}\s+LIKE\s+'%([^']+)%'",
        flags=re.IGNORECASE,
    )
    eq_pattern = re.compile(
        rf"{qualified_col}\s*=\s*'([^']+)'",
        flags=re.IGNORECASE,
    )

    def _replace_like(match: re.Match) -> str:
        column = match.group(1)
        value = match.group(2)
        compact = re.sub(r"\s+", "", value)
        return f"regexp_replace({column}, ' ', '') LIKE '%{compact}%'"

    def _replace_eq(match: re.Match) -> str:
        column = match.group(1)
        value = match.group(2)
        compact = re.sub(r"\s+", "", value)
        return f"regexp_replace({column}, ' ', '') = '{compact}'"

    rewritten = like_pattern.sub(_replace_like, sql_text)
    rewritten = eq_pattern.sub(_replace_eq, rewritten)
    if rewritten != sql_text:
        logger.info("이름 공백 무시 보정 SQL 적용")
    return rewritten


def _replace_za_with_zc(sql: str) -> str:
    """ZA거래처 참조를 ZC본부로 변환 (신규매출 기준 통일)"""
    # ZA거래처명 → ZC본부명 (먼저 처리)
    result = re.sub(r'`?ZA거래처명`?', '`ZC본부명`', sql)
    # ZA거래처 → ZC본부 (뒤에 '명'이 안 붙는 경우만)
    result = re.sub(r'`?ZA거래처`?(?!명)', '`ZC본부`', result)
    if result != sql:
        logger.info("ZA→ZC 변환 적용")
    return result


def run_query(sql: str, *, raw: bool = False) -> list[dict]:
    sql = _normalize_name_filter_sql(sql)
    if not raw:
        sql = _replace_za_with_zc(sql)

    def _execute_once() -> list[dict]:
        token = get_token()
        hostname = HOST.replace("https://", "")
        with dbsql.connect(
            server_hostname=hostname,
            http_path=HTTP_PATH,
            access_token=token
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    try:
        return _execute_once()
    except Exception as e:
        message = str(e).lower()
        should_retry = any(
            keyword in message
            for keyword in [
                "403",
                "forbidden",
                "invalid access token",
                "open session",
                "error during request to server",
                "opensession",
            ]
        )
        if not should_retry:
            raise
        logger.warning("토큰/세션 오류 감지. 토큰 초기화 후 1회 재시도합니다.")
        _clear_token_cache()
        return _execute_once()


# ─── 신규매출 분석 ─────────────────────────────────────────
T_MAIN    = "h_hmfo.gd_dcube.`01_sap_sales_custmasters`"
T_MISULGO = "h_hmfo.gd_dcube.`46_helo_periodic_unshipped`"
T_PROFIT  = "h_hmfo.gd_dcube.`00_customers_cm`"          # 수익성(공헌이익) 테이블
_NEW_CUST_DATE = "20251001"


def _is_new_sales_shape(rows: list[dict]) -> bool:
    if not rows:
        return False
    base = {"년월", "신규매출액_억원"}
    sample_keys = set(rows[0].keys())
    if not base.issubset(sample_keys):
        return False
    return "ZC본부명" in sample_keys or "ZA거래처명" in sample_keys


def _is_team_new_sales_shape(rows: list[dict]) -> bool:
    """팀/지점별 영업사원 신규매출 결과 감지"""
    if not rows:
        return False
    required = {"년월", "영업사원명", "신규매출액_억원"}
    sample_keys = set(rows[0].keys())
    return required.issubset(sample_keys) and "ZC본부명" not in sample_keys and "ZA거래처명" not in sample_keys


def _is_monthly_sales_shape(rows: list[dict]) -> bool:
    """사업부/지점 단일 월 전체매출 결과 감지 (전월비/전년비 포맷 적용 대상)
    - 컬럼명이 다양할 수 있으므로 '매출' 포함 컬럼 유연하게 감지
    """
    if not rows or len(rows) != 1:
        return False
    keys = set(rows[0].keys())
    has_sales_col = any("매출" in k for k in keys)
    has_target    = "사업부명" in keys or "지점명" in keys
    has_month     = "년월" in keys
    return has_sales_col and has_target and has_month


def _extract_monthly_sales_value(row: dict) -> float:
    """행에서 매출액 컬럼을 동적으로 찾아 억원 단위로 반환"""
    # 정확히 일치하는 컬럼 우선
    for exact in ("매출액_억원", "매출액합계_억", "매출합계_억", "total_억"):
        if exact in row and row[exact] is not None:
            return float(row[exact])
    # '매출' 포함 컬럼 중 첫 번째
    for k, v in row.items():
        if "매출" in k and v is not None:
            val = float(v)
            # 단위 보정: 10억 이상이면 원 단위로 판단 → 억 변환
            if val >= 1_000_000_000:
                return round(val / 100_000_000, 2)
            # 1만 이상이면 만원 단위 → 억 변환
            if val >= 10_000:
                return round(val / 10_000, 2)
            return val
    return 0.0


def _format_value(value: float) -> str:
    """억원 단위 값 → 백만원 정수(천단위 쉼표) 변환. 예: 49.63 → '4,963'"""
    val_man = round(float(value) * 100)
    return f"{val_man:,}"


def _safe_query(sql: str, *, raw: bool = False) -> list[dict]:
    """에러 시 빈 리스트 반환"""
    try:
        return run_query(sql, raw=raw)
    except Exception as e:
        logger.warning(f"추가 분석 쿼리 실패: {e}")
        return []


def _extract_sp_compact(sql: str) -> str | None:
    """SQL에서 영업사원 이름(공백 제거) 추출"""
    m = re.search(r"`?영업사원명`?\s+LIKE\s+'%([^']+)%'", sql, re.IGNORECASE)
    if m:
        name = m.group(1).replace("%", "")
        if name and re.match(r'^[\w가-힣]+$', name):
            return name
    return None


def _extract_team_name(sql: str) -> str | None:
    """SQL에서 지점명/팀명 추출"""
    m = re.search(r"`?지점명`?\s+LIKE\s+'%([^']+)%'", sql, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"`?지점명`?\s*=\s*'([^']+)'", sql, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _is_personal_zc(zc_code: str) -> bool:
    """ZC코드가 개인형인지 판별 (앞자리 0 제외 후 8로 시작하지 않으면 개인형)"""
    stripped = zc_code.lstrip('0')
    return bool(stripped) and stripped[0] != '8'


def _fetch_zc_code_mapping(sp_compact: str) -> dict[str, str]:
    """영업사원의 ZC본부명 → ZC본부 코드 매핑 조회"""
    rows = _safe_query(f"""
        SELECT DISTINCT `ZC본부`, `ZC본부명`
        FROM {T_MAIN}
        WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
          AND `사업부명` = '외식식재사업부'
    """)
    return {r['ZC본부명']: r['ZC본부'] for r in rows}


def _fetch_personal_new_sales(sp_compact: str, year: str) -> dict[str, float]:
    """개인형(ZC코드 1xxx) 신규 매출 월별 합계 직접 조회.
    Returns: {년월: 매출액_억}
    """
    rows = _safe_query(f"""
        WITH new_custs AS (
            SELECT `영업사원명`, `ZC본부`
            FROM {T_MAIN}
            WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
              AND `사업부명` = '외식식재사업부'
            GROUP BY `영업사원명`, `ZC본부`
            HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
        )
        SELECT t.`년월`,
               ROUND(COALESCE(SUM(t.`매출액`), 0) / 1000000, 2) AS `매출액_억`
        FROM {T_MAIN} t
        JOIN new_custs nc ON t.`영업사원명` = nc.`영업사원명`
                         AND t.`ZC본부` = nc.`ZC본부`
        WHERE t.`년도` = '{year}'
          AND t.`사업부명` = '외식식재사업부'
          AND TRIM(LEADING '0' FROM nc.`ZC본부`) NOT LIKE '8%'
        GROUP BY t.`년월`
        ORDER BY t.`년월`
    """)
    result: dict[str, float] = {}
    for r in rows:
        result[str(r['년월'])] = float(r.get('매출액_억', 0))
    return result


def _fetch_personal_za_count(sp_compact: str) -> int:
    """개인형 신규 거래처(ZA거래처) 수 조회 — 세부내역과 일치하는 카운트"""
    rows = _safe_query(f"""
        SELECT COUNT(DISTINCT nc.`ZA거래처`) AS `cnt`
        FROM (
            SELECT `영업사원명`, `ZC본부`, `ZA거래처`
            FROM {T_MAIN}
            WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
              AND `사업부명` = '외식식재사업부'
            GROUP BY `영업사원명`, `ZC본부`, `ZA거래처`
            HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
        ) nc
        WHERE TRIM(LEADING '0' FROM nc.`ZC본부`) NOT LIKE '8%'
    """)
    return int(rows[0]["cnt"]) if rows else 0


def _fetch_store_monthly(sp_compact: str, year: str) -> dict:
    """ZC본부별 월별 가맹점(ZB본지점) 데이터 조회
    Returns: {ZC본부명: {월: {가맹점수: int, 점당매출_억: float}}}
    """
    sql = f"""
    WITH sp_new AS (
        SELECT `영업사원명`, `ZC본부`
        FROM {T_MAIN}
        WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
          AND `사업부명` = '외식식재사업부'
        GROUP BY `영업사원명`, `ZC본부`
        HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
    )
    SELECT
        t.`ZC본부명`,
        t.`년월`,
        COUNT(DISTINCT t.`ZB본지점`) AS `가맹점수`,
        ROUND(COALESCE(SUM(t.`매출액`),0)/1000000
              / NULLIF(COUNT(DISTINCT t.`ZB본지점`), 0), 4) AS `점당매출_억`
    FROM {T_MAIN} t
    JOIN sp_new n ON t.`영업사원명` = n.`영업사원명`
                 AND t.`ZC본부` = n.`ZC본부`
    WHERE t.`년도` = '{year}'
      AND t.`사업부명` = '외식식재사업부'
    GROUP BY t.`ZC본부명`, t.`년월`
    ORDER BY t.`ZC본부명`, t.`년월`
    """
    rows = _safe_query(sql)
    result: dict = {}
    for r in rows:
        cust = r["ZC본부명"]
        if cust not in result:
            result[cust] = {}
        result[cust][r["년월"]] = {
            "가맹점수": int(r["가맹점수"]),
            "점당매출_억": float(r["점당매출_억"]) if r["점당매출_억"] else 0.0,
        }
    return result


def _fetch_peer_stats(sp_compact: str) -> dict | None:
    """동료비교: 같은 사업부 내 신규매출 통계 + 개인 수치 (ZC본부 기준)"""
    # 1) 사업부 확인
    dept_rows = _safe_query(f"""
        SELECT DISTINCT `사업부명` FROM {T_MAIN}
        WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
          AND `사업부명` IS NOT NULL AND `사업부명` != ''
        LIMIT 1
    """)
    if not dept_rows:
        return None
    dept = dept_rows[0]["사업부명"]

    # 2) 사업부 평균 + 개인 수치 (ZC본부 기준)
    combined_rows = _safe_query(f"""
    WITH new_cust AS (
        SELECT `영업사원명`, `ZC본부`
        FROM {T_MAIN}
        WHERE `사업부명` = '{dept}'
        GROUP BY `영업사원명`, `ZC본부`
        HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
    ),
    sp_stats AS (
        SELECT nc.`영업사원명`,
            COUNT(DISTINCT nc.`ZC본부`) AS `브랜드수`,
            ROUND(COALESCE(SUM(t.`매출액`),0)/1000000, 2) AS `매출합_억`,
            COUNT(DISTINCT t.`ZB본지점`) AS `가맹점수`
        FROM new_cust nc
        JOIN {T_MAIN} t ON nc.`영업사원명` = t.`영업사원명`
                       AND nc.`ZC본부` = t.`ZC본부`
        WHERE t.`년도` = CAST(YEAR(CURRENT_DATE) AS STRING)
          AND t.`사업부명` = '{dept}'
        GROUP BY nc.`영업사원명`
    )
    SELECT
        (SELECT COUNT(*) FROM sp_stats) AS `사원수`,
        (SELECT ROUND(AVG(`브랜드수`),1) FROM sp_stats) AS `평균_브랜드수`,
        (SELECT ROUND(AVG(`매출합_억`),2) FROM sp_stats) AS `평균_매출_억`,
        (SELECT ROUND(AVG(`가맹점수`),1) FROM sp_stats) AS `평균_가맹점수`,
        sp.`영업사원명`,
        sp.`브랜드수`  AS `내_브랜드수`,
        sp.`매출합_억` AS `내_매출_억`,
        sp.`가맹점수`  AS `내_가맹점수`
    FROM sp_stats sp
    WHERE regexp_replace(sp.`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
    """)
    if not combined_rows:
        return None
    r = combined_rows[0]
    return {
        "사업부명": dept,
        "사원수":   int(r["사원수"]),
        "평균_브랜드수": float(r["평균_브랜드수"] or 0),
        "평균_매출_억":  float(r["평균_매출_억"] or 0),
        "평균_가맹점수": float(r["평균_가맹점수"] or 0),
        "영업사원명": r["영업사원명"] or sp_compact,
        "내_브랜드수": int(r["내_브랜드수"] or 0),
        "내_매출_억":  float(r["내_매출_억"] or 0),
        "내_가맹점수": int(r["내_가맹점수"] or 0),
    }


def _build_new_sales_markdown(rows: list[dict], original_sql: str = "") -> str:
    """신규매출 분석 마크다운 (가맹점 · 동료비교 · 리스크 포함)"""

    # ── 1. 기본 매출 데이터 ──
    month_set = sorted(
        {str(r.get("년월", "")) for r in rows if r.get("년월") is not None}
    )
    # 거래처×월 매출 — ZC본부명 기준 (ZA→ZC 변환은 run_query에서 자동 처리)
    cust_key = "ZC본부명" if "ZC본부명" in rows[0] else "ZA거래처명"
    mcv: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        m = str(row.get("년월", ""))
        c = str(row.get(cust_key, ""))
        try:
            v = float(row.get("신규매출액_억원", 0))
        except (TypeError, ValueError):
            v = 0.0
        mcv[c][m] = mcv[c].get(m, 0.0) + v

    # ── 2. 추가 쿼리 (가맹점 + 동료비교 + ZC코드 + 개인형, 병렬) ──
    sp_compact = _extract_sp_compact(original_sql) if original_sql else None
    store_data: dict = {}
    peer_info: dict | None = None
    zc_mapping: dict[str, str] = {}
    personal_monthly: dict[str, float] = {}
    personal_za_count: int = 0

    if sp_compact:
        year = month_set[0][:4] if month_set else "2026"
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                f_store    = pool.submit(_fetch_store_monthly, sp_compact, year)
                f_peer     = pool.submit(_fetch_peer_stats, sp_compact)
                f_zc       = pool.submit(_fetch_zc_code_mapping, sp_compact)
                f_personal = pool.submit(_fetch_personal_new_sales, sp_compact, year)
                f_pza_cnt  = pool.submit(_fetch_personal_za_count, sp_compact)
                store_data       = f_store.result(timeout=60)
                peer_info        = f_peer.result(timeout=60)
                zc_mapping       = f_zc.result(timeout=60)
                personal_monthly = f_personal.result(timeout=60)
                personal_za_count = f_pza_cnt.result(timeout=60)
        except Exception as e:
            logger.warning(f"추가 분석 실패(기본 출력 사용): {e}")

    # ── 2.5 개인형 그룹화 ──
    # 1) Dify 결과에서 개인형 ZC 제거 (중복 방지)
    _PERSONAL_LABEL = "개인형"
    personal_from_rows: set[str] = set()
    if zc_mapping:
        for c in list(mcv.keys()):
            zc_code = zc_mapping.get(c, "")
            if _is_personal_zc(zc_code):
                personal_from_rows.add(c)
                del mcv[c]
                if c in store_data:
                    del store_data[c]

    # 2) DB에서 직접 조회한 개인형 월별 합계 삽입
    personal_count = 0
    if personal_monthly and sum(personal_monthly.values()) > 0:
        mcv[_PERSONAL_LABEL] = personal_monthly
        # 개인형 거래처 수: ZA거래처 기준 (세부내역 화면과 일치)
        personal_count = personal_za_count
        logger.info(f"[마크다운] 개인형 {personal_count}개 합산 (월별: {personal_monthly})")

    # 모든 월 매출이 100만원(0.01억) 이하인 브랜드 제외
    customer_set = sorted(
        c for c in mcv.keys()
        if max(mcv[c].values(), default=0) > 0.01
    )

    has_stores = bool(store_data)

    # ── 3. 합계 계산 ──
    totals: dict[str, float] = {}
    for c in customer_set:
        totals[c] = sum(mcv[c].values())
    grand_total = sum(totals.values())
    cust_count = len([c for c in customer_set if c != _PERSONAL_LABEL])

    last_month = month_set[-1] if month_set else ""
    total_stores = 0
    if has_stores:
        total_stores = sum(
            store_data.get(c, {}).get(last_month, {}).get("가맹점수", 0)
            for c in customer_set
        )

    # ── 4. 요약 헤더 ──
    sp_display = peer_info["영업사원명"] if peer_info else (sp_compact or "")
    dept_name  = peer_info["사업부명"] if peer_info else ""
    year_label = month_set[0][:4] if month_set else "2026"

    lines: list[str] = []
    title_parts = [f"{year_label}년"]
    if dept_name:
        title_parts.append(dept_name)
    lines.append(f"📊 {sp_display} 님 신규매출 현황 ({', '.join(title_parts)})")
    lines.append("")
    # 가맹점 수: peer_info 있으면 연간 기준 (동료비교와 일치)
    display_stores = peer_info["내_가맹점수"] if peer_info else total_stores
    brand_label = f"신규브랜드: {cust_count}개"
    if personal_count > 0:
        brand_label += f" (개인형 {personal_count}개 합산)"
    summary = [f"총 신규매출: {_format_value(grand_total)}백만원",
               brand_label]
    if display_stores > 0:
        summary.append(f"가맹점: {display_stores}개")
    lines.append(" | ".join(summary))
    lines.append("")

    # ── 5. 표 ──
    month_labels = []
    for m in month_set:
        mn = int(m[4:6]) if len(m) >= 6 else m
        month_labels.append(f"{mn}월")

    cols = ["브랜드명"] + month_labels + ["합계"]
    if has_stores:
        cols += ["가맹점", "점당매출"]

    lines.append("| " + " | ".join(cols) + " |")
    sep = ["---"] + ["---:"] * (len(cols) - 1)
    lines.append("| " + " | ".join(sep) + " |")

    sorted_custs = sorted(customer_set, key=lambda c: totals[c], reverse=True)
    for c in sorted_custs:
        val_strs = []
        for m in month_set:
            v = mcv[c].get(m)
            val_strs.append("-" if v is None else _format_value(v))
        parts = [c] + val_strs + [_format_value(totals[c])]

        if has_stores:
            # 가맹점 증감
            s_vals = [
                (m, store_data.get(c, {}).get(m, {}).get("가맹점수", 0))
                for m in month_set
            ]
            nz = [(m, s) for m, s in s_vals if s > 0]
            if len(nz) >= 2 and nz[0][1] != nz[-1][1]:
                store_str = f"{nz[0][1]}→{nz[-1][1]}"
            elif nz:
                store_str = str(nz[-1][1])
            else:
                store_str = "-"

            # 점당매출 (최근월, 만원 단위)
            last_ps = 0.0
            for m in reversed(month_set):
                ps = store_data.get(c, {}).get(m, {}).get("점당매출_억", 0)
                if ps > 0:
                    last_ps = ps
                    break
            ps_str = f"{round(last_ps * 10000):,}만" if last_ps > 0 else "-"
            parts += [store_str, ps_str]

        lines.append("| " + " | ".join(str(p) for p in parts) + " |")
    lines.append("")

    # ── 6. 동료비교 ──
    if peer_info:
        sp_cnt      = peer_info["사원수"]
        avg_cust    = peer_info["평균_브랜드수"]
        avg_sales   = peer_info["평균_매출_억"]
        avg_stores  = peer_info["평균_가맹점수"]
        my_cust     = peer_info["내_브랜드수"]
        my_sales    = round(grand_total, 2)   # 표 합계 재사용 (불일치 방지)
        my_stores   = peer_info["내_가맹점수"]

        lines.append(f"📋 {dept_name} 동료 비교 ({sp_cnt}명 중):")
        lines.append(
            f"- 평균: 브랜드 {avg_cust}개 / 매출 {avg_sales}억 / "
            f"가맹점 {avg_stores}개"
        )

        cmp_parts = []
        if avg_sales > 0:
            cmp_parts.append(
                f"매출 {_format_value(my_sales)}백만({(my_sales/avg_sales-1)*100:+.0f}%)"
            )
        if avg_stores > 0:
            cmp_parts.append(
                f"가맹점 {my_stores}개({(my_stores/avg_stores-1)*100:+.0f}%)"
            )
        if avg_cust > 0:
            cmp_parts.append(
                f"브랜드 {my_cust}개({(my_cust/avg_cust-1)*100:+.0f}%)"
            )
        if cmp_parts:
            if avg_sales > 0 and my_sales > avg_sales * 1.5:
                emoji, comment = "🏆", "신규 개척력이 탁월합니다!"
            elif avg_sales > 0 and my_sales > avg_sales:
                emoji, comment = "👏", "평균 이상의 활약입니다!"
            elif avg_sales > 0 and my_sales > avg_sales * 0.7:
                emoji, comment = "💪", "꾸준히 성장 중입니다!"
            else:
                emoji, comment = "📈", "앞으로의 성장이 기대됩니다!"
            lines.append(
                f"- {sp_display} 님: {' / '.join(cmp_parts)} {emoji}"
            )
            lines.append(f"→ {comment}")
        lines.append("")

    # ── 7. 주요 포인트 (브랜드별 1줄 통합) ──
    insights: list[str] = []
    for c in sorted_custs:
        month_vals = [(m, mcv[c].get(m)) for m in month_set]
        active_months = sum(1 for _, v in month_vals if v is not None and v > 0)
        total = totals[c]
        cust_points: list[str] = []

        # 소규모 라벨
        if total < 0.5:
            cust_points.append(f"소규모 거래 (합계 {round(total * 10000):,}만원)")

        # 3개월 미만 → 추세 분석 스킵, 소규모 라벨만 출력 후 다음
        if active_months < 3:
            if cust_points:
                insights.append(f"- {c}: {', '.join(cust_points)}")
            continue

        # 매출 변화
        real_vals = [v for _, v in month_vals if v is not None and v > 0]
        if len(real_vals) >= 3:
            prev, last = real_vals[-2], real_vals[-1]
            if prev > 0:
                chg = (last - prev) / prev * 100
                if last == 0:
                    cust_points.append("당월 거래 중단")
                elif chg <= -30:
                    cust_points.append(
                        f"매출 급감 ({_format_value(prev)}→{_format_value(last)}백만, {chg:+.0f}%)"
                    )
                elif chg <= -10:
                    # 연속 하락 확인
                    if len(real_vals) >= 3 and real_vals[-3] > real_vals[-2]:
                        cust_points.append(f"3개월 연속 하락 ({chg:+.0f}%)")
                    else:
                        cust_points.append(f"매출 소폭 하락 ({chg:+.0f}%)")

        # 가맹점 분석 (3개월+ only)
        if has_stores and c in store_data:
            sm = [
                (m, store_data[c].get(m, {}).get("가맹점수", 0))
                for m in month_set
            ]
            nz_sm = [(m, s) for m, s in sm if s > 0]
            if len(nz_sm) >= 3:
                f_s, l_s = nz_sm[0][1], nz_sm[-1][1]
                if l_s < f_s:
                    cust_points.append(f"가맹점 {f_s}→{l_s}개 축소")

            # 점당매출 변화
            ps = [
                store_data[c].get(m, {}).get("점당매출_억", 0)
                for m in month_set
            ]
            nz_ps = [p for p in ps if p > 0]
            if len(nz_ps) >= 3 and nz_ps[0] > 0:
                ps_chg = (nz_ps[-1] - nz_ps[0]) / nz_ps[0] * 100
                if ps_chg <= -15:
                    cust_points.append(
                        f"점당매출 하락 "
                        f"({round(nz_ps[0]*10000)}만→{round(nz_ps[-1]*10000)}만, "
                        f"{ps_chg:+.0f}%)"
                    )

        if cust_points:
            insights.append(f"- {c}: {', '.join(cust_points)}")

    if insights:
        lines.append("💡 주요 포인트:")
        lines.extend(insights)

    if personal_count > 0:
        lines.append("")
        lines.append("💡 개인형 거래처의 세부내역을 보려면:")
        lines.append('"개인형 세부내역 보여줘" 라고 입력하세요.')

    return "\n".join(lines)


def _build_team_new_sales_markdown(rows: list[dict], original_sql: str = "") -> str:
    """팀/지점 기준 영업사원별 신규매출 마크다운"""

    team_name = _extract_team_name(original_sql) if original_sql else ""

    month_set = sorted(
        {str(r.get("년월", "")) for r in rows if r.get("년월") is not None}
    )

    # 영업사원×월 매출
    mcv: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        m = str(row.get("년월", ""))
        sp = str(row.get("영업사원명", ""))
        try:
            v = float(row.get("신규매출액_억원", 0))
        except (TypeError, ValueError):
            v = 0.0
        mcv[sp][m] = mcv[sp].get(m, 0.0) + v

    sp_set = sorted(mcv.keys())

    # 합계 계산
    totals: dict[str, float] = {}
    for sp in sp_set:
        totals[sp] = sum(mcv[sp].values())
    grand_total = sum(totals.values())
    sp_count = len(sp_set)

    year_label = month_set[0][:4] if month_set else "2026"

    lines: list[str] = []
    title = f"📊 {team_name} 영업사원별 신규매출 현황 ({year_label}년)" if team_name else f"📊 영업사원별 신규매출 현황 ({year_label}년)"
    lines.append(title)
    lines.append("")
    lines.append(f"총 신규매출: {_format_value(grand_total)}백만원 | 영업사원: {sp_count}명")
    lines.append("")

    # 표
    month_labels = []
    for m in month_set:
        mn = int(m[4:6]) if len(m) >= 6 else m
        month_labels.append(f"{mn}월")

    cols = ["영업사원"] + month_labels + ["합계"]
    lines.append("| " + " | ".join(cols) + " |")
    sep = ["---"] + ["---:"] * (len(cols) - 1)
    lines.append("| " + " | ".join(sep) + " |")

    sorted_sps = sorted(sp_set, key=lambda s: totals[s], reverse=True)
    for sp in sorted_sps:
        val_strs = []
        for m in month_set:
            v = mcv[sp].get(m)
            val_strs.append("-" if v is None else _format_value(v))
        parts = [sp] + val_strs + [_format_value(totals[sp])]
        lines.append("| " + " | ".join(str(p) for p in parts) + " |")
    lines.append("")

    # 간단한 인사이트
    if sorted_sps:
        top = sorted_sps[0]
        lines.append(f"💡 최고 실적: {top} ({_format_value(totals[top])}백만원)")
        if len(sorted_sps) >= 2:
            avg = grand_total / sp_count
            above_avg = sum(1 for s in sp_set if totals[s] >= avg)
            lines.append(f"   평균 {_format_value(avg)}백만원 이상: {above_avg}명/{sp_count}명")

    return "\n".join(lines)


# ─── 월별 전체매출 분석 ──────────────────────────────────────

def _fetch_monthly_comparison(target_key: str, target_name: str, yearmonth: str) -> dict:
    """전월/전년 동월 매출 조회. Returns: {전월_ym, 전년동월_ym, 전월, 전년동월}"""
    ym = str(yearmonth)
    year, month = int(ym[:4]), int(ym[4:6])
    prev_ym = f"{year-1}12" if month == 1 else f"{year}{month-1:02d}"
    yoy_ym  = f"{year-1}{month:02d}"
    rows = _safe_query(f"""
        SELECT `년월`,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS `매출액_억원`
        FROM {T_MAIN}
        WHERE `{target_key}` = '{target_name}'
          AND `년월` IN ('{prev_ym}', '{yoy_ym}')
        GROUP BY `년월`
    """)
    data = {str(r["년월"]): float(r.get("매출액_억원", 0)) for r in rows}
    return {
        "전월_ym":      prev_ym,
        "전년동월_ym":  yoy_ym,
        "전월":         data.get(prev_ym, 0.0),
        "전년동월":     data.get(yoy_ym, 0.0),
    }


def _fetch_monthly_total(target_key: str, target_name: str, yearmonth: str) -> list[dict]:
    """사업부/지점 단일 월 전체매출 직접 조회 → _build_monthly_sales_markdown 에 넘길 rows 반환"""
    rows = _safe_query(f"""
        SELECT '{target_name}' AS `{target_key}`,
               '{yearmonth}'  AS `년월`,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS `매출액_억원`
        FROM {T_MAIN}
        WHERE `{target_key}` = '{target_name}' AND `년월` = '{yearmonth}'
    """)
    if rows:
        return rows
    # 데이터가 없어도 0으로 채운 행 반환 (빈 월 표시용)
    return [{target_key: target_name, "년월": yearmonth, "매출액_억원": 0.0}]

def _fetch_brand_daily_sales(brand_name: str, date_str: str) -> tuple[str, float, str] | None | list[str]:
    """대금청구일 기준 브랜드 일별 매출 (YYYYMMDD 형식)
    - 정확히 1건 매칭 → (브랜드명, 매출, 집계단위) 반환
    - 2건 이상 매칭   → 후보 브랜드명 리스트 반환
    - 0건             → None
    """
    brand_name = brand_name.replace('&', '＆')
    exact_candidates = [brand_name, f"{brand_name}(본사)", f"{brand_name} 본사"]
    for cand in exact_candidates:
        rows = _safe_query(f"""
            SELECT `ZC본부명` AS name,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND `ZC본부명` = '{cand}'
              AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
              AND `대금청구일` = '{date_str}'
            GROUP BY `ZC본부명`
        """)
        if rows and float(rows[0].get("sales", 0)) > 0:
            return str(rows[0]["name"]), float(rows[0]["sales"]), "브랜드(ZC)"
    like_rows = _safe_query(f"""
        SELECT `ZC본부명` AS name,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `ZC본부명` LIKE '%{brand_name}%'
          AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
          AND `대금청구일` = '{date_str}'
        GROUP BY `ZC본부명`
        ORDER BY SUM(`매출액`) DESC
    """)
    like_rows = [r for r in like_rows if float(r.get("sales", 0)) > 0]
    if not like_rows:
        # 2.5) ZA거래처명 검색 (개인형 ZC)
        za_rows = _safe_query(f"""
            SELECT `ZA거래처명` AS name,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND `ZA거래처명` LIKE '%{brand_name}%'
              AND `대금청구일` = '{date_str}'
            GROUP BY `ZA거래처명`
            ORDER BY SUM(`매출액`) DESC
        """, raw=True)
        za_rows = [r for r in za_rows if float(r.get("sales", 0)) > 0]
        if za_rows:
            if len(za_rows) == 1:
                return str(za_rows[0]["name"]), float(za_rows[0]["sales"]), "거래처(ZA)"
            return [str(r["name"]) for r in za_rows]
    if not like_rows:
        # 3) 거래처명 fallback (단일 점포명 등, 공백 제거 비교 포함)
        _bn_ns = brand_name.replace(' ', '')
        cust_rows = _safe_query(f"""
            SELECT `거래처명` AS name,
                   MAX(`ZC본부명`) AS zc,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND (`거래처명` LIKE '%{brand_name}%'
                OR REPLACE(`거래처명`, ' ', '') LIKE '%{_bn_ns}%')
              AND `대금청구일` = '{date_str}'
            GROUP BY `거래처명`
            ORDER BY SUM(`매출액`) DESC
        """)
        cust_rows = [r for r in cust_rows if float(r.get("sales", 0)) > 0]
        if not cust_rows:
            return None
        if len(cust_rows) == 1:
            return str(cust_rows[0]["name"]), float(cust_rows[0]["sales"]), "단일 거래처"
        # 4) 동일 ZC 다건 → ZC 아래 전체 거래처가 검색어 포함 시만 ZC 집계로 승격
        zc_names = {str(r.get("zc", "") or "") for r in cust_rows}
        zc_names.discard("")
        if len(zc_names) == 1:
            zc_name = zc_names.pop()
            # ZC 승격 가드: 검색어 토큰이 ZC명에 포함돼야 승격 허용
            _zc_guard = any(t in zc_name for t in brand_name.replace('(', '').replace(')', '').split())
            if not _zc_guard:
                total_sales = sum(float(r.get("sales", 0)) for r in cust_rows)
                display_name = f"{brand_name} (전체 {len(cust_rows)}개 점포 합계)"
                return display_name, total_sales, "점포합산"
            _bn_ns2 = brand_name.replace(' ', '')
            all_custs = _safe_query(f"""
                SELECT DISTINCT `거래처명`
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `ZC본부명` = '{zc_name}'
                  AND `대금청구일` = '{date_str}'
            """)
            all_match = all(
                brand_name in str(r.get("거래처명", ""))
                or _bn_ns2 in str(r.get("거래처명", "")).replace(' ', '')
                for r in all_custs
            )
            if all_match:
                zc_row = _safe_query(f"""
                    SELECT `ZC본부명` AS name,
                           ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
                    FROM {T_MAIN}
                    WHERE `사업부명` = '외식식재사업부'
                      AND `ZC본부명` = '{zc_name}'
                      AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
                      AND `대금청구일` = '{date_str}'
                    GROUP BY `ZC본부명`
                """)
                if zc_row and float(zc_row[0].get("sales", 0)) > 0:
                    return str(zc_row[0]["name"]), float(zc_row[0]["sales"]), "브랜드(ZC)"
            else:
                total_sales = sum(float(r.get("sales", 0)) for r in cust_rows)
                display_name = f"{brand_name} (전체 {len(cust_rows)}개 점포 합계)"
                return display_name, total_sales, "점포합산"
        return [str(r["name"]) for r in cust_rows]
    if len(like_rows) == 1:
        return str(like_rows[0]["name"]), float(like_rows[0]["sales"]), "브랜드(ZC)"
    return [str(r["name"]) for r in like_rows]


def _get_store_agg_alternatives(keyword: str, yearmonth: str) -> tuple:
    """점포합산 결과에서 ZC/ZA 후보 반환.
    Returns: (zc_list, za_list)  각각 [(name, store_cnt), ...]
    """
    from collections import Counter as _Ctr
    kw_ns = keyword.replace(' ', '')
    try:
        rows = _safe_query(f"""
            SELECT `ZC본부명` AS zc, `ZA거래처명` AS za
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND (`거래처명` LIKE '%{keyword}%'
                OR REPLACE(`거래처명`, ' ', '') LIKE '%{kw_ns}%')
              AND `년월` = '{yearmonth}'
            GROUP BY `ZC본부명`, `ZA거래처명`
        """, raw=True)
    except Exception:
        return [], []
    zc_cnt: "_Ctr" = _Ctr()
    za_cnt: "_Ctr" = _Ctr()
    for r in rows:
        zc = str(r.get("zc") or "").strip()
        za = str(r.get("za") or "").strip()
        if zc:
            zc_cnt[zc] += 1
        if za and za != zc:
            za_cnt[za] += 1
    return zc_cnt.most_common(3), za_cnt.most_common(3)


def _build_store_agg_suggestion(keyword: str, yearmonth: str) -> tuple:
    """점포합산 후 ZC/ZA 제안 텍스트 + QR 버튼 반환.
    Returns: (suggestion_text: str, qr_buttons: list)
    """
    import time as _time_sas
    zc_list, za_list = _get_store_agg_alternatives(keyword, yearmonth)
    if not zc_list and not za_list:
        return "", []
    # 이번달이면 월 생략, 과거월이면 "N월" 붙임
    _cur_ym = _time_sas.strftime("%Y%m")
    _mo_suffix = f" {int(yearmonth[4:6])}월" if yearmonth != _cur_ym else ""
    parts = []
    qr_btns = []
    for zc_name, _ in zc_list:
        parts.append(f"{zc_name}(ZC)")
        qr_btns.append({"label": f"{zc_name}(ZC)", "action": "message",
                        "messageText": f"{zc_name}{_mo_suffix} 매출"})
    for za_name, _ in za_list:
        parts.append(f"{za_name}(ZA)")
        qr_btns.append({"label": f"{za_name}(ZA)", "action": "message",
                        "messageText": f"{za_name}{_mo_suffix} 매출"})
    # 메인 메뉴 버튼 추가
    qr_btns.append({"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"})
    candidates_str = ", ".join(parts)
    suggestion = (
        "\n─────────────────────\n"
        "위 결과는 고객명 키워드 유사도에 기반한 집계입니다.\n"
        "ZC / ZA 집계를 원하시나요? 아래에서 선택해 주세요.\n"
        f"({candidates_str})"
    )
    return suggestion, qr_btns


def _build_brand_forecast_card(matched_name: str, sales_so_far: float,
                               ym_now: str, today: _dt_mod.date,
                               level_label: str = "브랜드(ZC)") -> str:
    """당월 누계+v7 예측+비교 카드 문자열 반환.
    sales_so_far : 백만원 단위 (월별 집계 실적)
    ym_now       : 'YYYYMM'
    today        : 예측 기준일
    level_label  : '브랜드(ZC)' | '거래처(ZA)' | '단일 거래처' | '점포합산'
    """
    mo   = today.month
    year = today.year
    day  = today.day

    # 집계 조건 헬퍼: level_label에 따라 WHERE 절 반환
    import re as _re_bfc
    def _where(col_ym: str = "", ym_val: str = "",
               col_date: str = "", date_from: str = "", date_to: str = "") -> str:
        """level_label 기반 집계 조건 생성"""
        base = "`사업부명`='외식식재사업부'"
        # 점포합산: matched_name 에서 "(전체 N개 점포 합계)" 제거 후 거래처명 LIKE
        if level_label == "점포합산":
            _base_name = _re_bfc.sub(r'\s*\(전체\s*\d+개\s*점포\s*합계\)', '', matched_name).strip()
            cond = f"`거래처명` LIKE '%{_base_name}%'"
        elif level_label == "단일 거래처":
            cond = f"`거래처명`='{matched_name}'"
        elif level_label == "거래처(ZA)":
            cond = f"`ZA거래처명`='{matched_name}'"
        else:  # 브랜드(ZC) 기본
            cond = f"`ZC본부명`='{matched_name}'"
        sql = f"{base} AND {cond}"
        if col_ym and ym_val:
            sql += f" AND `{col_ym}`='{ym_val}'"
        if col_date and date_from and date_to:
            sql += f" AND `대금청구일` BETWEEN '{date_from}' AND '{date_to}'"
        return sql

    # ZA 레이블이면 내부 쿼리도 ZA→ZC 변환 없이 실행
    _raw_q = (level_label == "거래처(ZA)")

    # v7 예측 호출 (ZC 레이블일 때만 의미 있음 — ZA/점포합산/단일 거래처는 스킵)
    fc_result = None
    if level_label == "브랜드(ZC)":
        try:
            import forecast_engine_v7 as _fe_v7
            fc_result = _fe_v7.predict_single_brand(matched_name, today, _safe_query)
        except Exception as _e:
            logger.warning(f"[예측카드] predict_single_brand 실패: {_e}")

    prev_date = (today.replace(day=1) - _dt_mod.timedelta(days=1))
    prev_ym   = prev_date.strftime("%Y%m")
    prev_mo   = prev_date.month
    yoy_ym    = f"{year - 1}{mo:02d}"

    # 전월 전체 매출
    prev_total = 0.0
    try:
        r = _safe_query(f"""
            SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS sales
            FROM {T_MAIN} WHERE {_where(col_ym='년월', ym_val=prev_ym)}""", raw=_raw_q)
        prev_total = float(r[0]["sales"]) if r else 0.0
    except Exception: pass

    # 전년동월 전체 매출
    yoy_total = 0.0
    try:
        r = _safe_query(f"""
            SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS sales
            FROM {T_MAIN} WHERE {_where(col_ym='년월', ym_val=yoy_ym)}""", raw=_raw_q)
        yoy_total = float(r[0]["sales"]) if r else 0.0
    except Exception: pass

    # 올해 YTD (완성 월 Jan~prev + 당월 누계)
    ytd_this = sales_so_far
    if mo > 1:
        try:
            r = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS sales
                FROM {T_MAIN}
                WHERE {_where()} AND `년월` >= '{year}01' AND `년월` < '{ym_now}'""", raw=_raw_q)
            ytd_this += float(r[0]["sales"]) if r else 0.0
        except Exception: pass

    # 전년 동기 YTD (Jan~prev월 + 당월 1~day일)
    ytd_last = 0.0
    try:
        if mo > 1:
            r = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS sales
                FROM {T_MAIN}
                WHERE {_where()} AND `년월` >= '{year-1}01' AND `년월` < '{yoy_ym}'""", raw=_raw_q)
            ytd_last += float(r[0]["sales"]) if r else 0.0
        day_from = f"{year-1}{mo:02d}01"
        day_to   = f"{year-1}{mo:02d}{day:02d}"
        r = _safe_query(f"""
            SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS sales
            FROM {T_MAIN}
            WHERE {_where(col_date='대금청구일', date_from=day_from, date_to=day_to)}""", raw=_raw_q)
        ytd_last += float(r[0]["sales"]) if r else 0.0
    except Exception: pass

    # 예측값 결정
    days_in_m = calendar.monthrange(year, mo)[1]
    _simple_forecast = (sales_so_far / day * days_in_m) if day > 0 else sales_so_far
    if fc_result and fc_result.get("forecast") is not None and fc_result["forecast"] > 0:
        forecast = fc_result["forecast"]
    else:
        forecast  = _simple_forecast

    def _pct(new_val, old_val):
        if old_val <= 0: return "N/A"
        rate = (new_val - old_val) / old_val * 100
        arrow = "↑" if rate >= 0 else "↓"
        return f"{rate:+.1f}% {arrow}"

    so_far_s   = f"{_format_value(sales_so_far)}\ubc31\ub9cc"
    forecast_s = f"{_format_value(forecast)}\ubc31\ub9cc"
    prev_s     = f"{_format_value(prev_total)}\ubc31\ub9cc"
    yoy_s      = f"{_format_value(yoy_total)}\ubc31\ub9cc"
    ytd_this_s = f"{_format_value(ytd_this)}\ubc31\ub9cc"
    ytd_last_s = f"{_format_value(ytd_last)}\ubc31\ub9cc"

    lines = [
        f"\U0001f4ca {matched_name} \ub9e4\ucd9c \ud604\ud669 ({mo}\uc6d4 1~{day}\uc77c \uae30\uc900)\n",
        f"\uc774\ubc88\ub2ec \ub204\uacc4       {so_far_s}",
        f"\uc774\ubc88\ub2ec \uc608\uc0c1       {forecast_s}",
        "",
        f"\uc804\uc6d4({prev_mo}\uc6d4) \u6bd4      {prev_s} \u2192 {_pct(forecast, prev_total)}  (\uc608\uc0c1 \uae30\uc900)",
        f"\uc804\ub144 \ub3d9\uc6d4 \u6bd4      {yoy_s} \u2192 {_pct(forecast, yoy_total)}  (\uc608\uc0c1 \uae30\uc900)",
        "",
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        f"\uc62c\ud574 \ub204\uacc4         {ytd_this_s} (1\uc6d4~{mo}\uc6d4 {day}\uc77c)",
        f"\uc804\ub144 \ub3d9\uae30 \u6bd4      {ytd_last_s} \u2192 {_pct(ytd_this, ytd_last)}",
    ]
    return "\n".join(lines)


def _build_dept_forecast_card(
    dept_key: str,
    dept_name: str,
    sales_so_far: float,
    ym_now: str,
    today,
) -> str:
    """사업부/지점 당월 예상 매출 카드 — 요일별 가중 런레이트"""
    import calendar as _cal_dept
    import datetime as _dt_dept
    import statistics as _stat_dept
    from collections import defaultdict as _dd_dept

    # v7 공휴일 집합 (배송 있지만 boost 적용 대상)
    try:
        from forecast_engine_v7 import KOR_HOLIDAYS as _HOLI
    except Exception:
        _HOLI = set()
    # 실배송 없는 날 (설날·추석 전날+당일) → DB 조회
    _HOLIDAY_BOOST = 1.5
    try:
        _nd_rows = _safe_query(
            "SELECT no_delivery_date AS dt FROM h_hmfo_fsi_dm.gd_rst_ing.dim_holidays"
            f" WHERE holiday_year IN ({int(ym_now[:4])-1}, {int(ym_now[:4])})",
            raw=True,
        )
        import datetime as _dt_nd
        def _to_date_nd(v):
            if isinstance(v, _dt_nd.date): return v
            s = str(v).replace("-", "")
            return _dt_nd.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        _NO_DELIVERY = {_to_date_nd(r["dt"]) for r in _nd_rows}
    except Exception:
        _NO_DELIVERY = set()

    year = int(ym_now[:4])
    mo   = int(ym_now[4:])
    days_in_m = _cal_dept.monthrange(year, mo)[1]

    prev_date = (today.replace(day=1) - _dt_dept.timedelta(days=1))
    prev_ym   = prev_date.strftime("%Y%m")
    prev_mo   = prev_date.month
    yoy_ym    = f"{year - 1}{mo:02d}"

    def _dq(where_extra: str) -> float:
        try:
            r = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS sales
                FROM {T_MAIN} WHERE `{dept_key}` = '{dept_name}' {where_extra}"""
            )
            return float(r[0]["sales"]) if r else 0.0
        except Exception:
            return 0.0

    # ── ① 당월 일별 매출 조회 (HAVING 0.01 이상) ──────────────────
    try:
        _daily_rows = _safe_query(f"""
            SELECT `대금청구일` AS dt,
                   ROUND(SUM(`매출액`)/1000000, 4) AS daily
            FROM {T_MAIN}
            WHERE `{dept_key}` = '{dept_name}' AND `년월` = '{ym_now}'
            GROUP BY `대금청구일`
            HAVING ROUND(SUM(`매출액`)/1000000, 4) >= 0.01
            ORDER BY `대금청구일`
        """, raw=True)
    except Exception:
        _daily_rows = []

    # data_day = 의미 있는 데이터의 마지막 날
    data_day = today.day  # fallback
    _dow_sales: dict = _dd_dept(list)  # {0(Mon)..6(Sun): [sales,...]}
    for _r in _daily_rows:
        _ds = str(_r["dt"])
        _dd = int(_ds[6:8])
        _dval = float(_r["daily"])
        if _dval >= 0.1:           # 소액 잔존 제외한 실질 반영일 추적
            data_day = max(data_day if not _daily_rows else 0, _dd)
        _dt_obj = _dt_dept.date(int(_ds[:4]), int(_ds[4:6]), _dd)
        # 일요일·NO_DELIVERY는 DOW 평균에서 제외 (0값으로 왜곡 방지)
        # KOR_HOLIDAYS(공휴일)는 배송 있으므로 DOW 평균에 포함
        if _dt_obj.weekday() != 6 and _dt_obj not in _NO_DELIVERY:
            _dow_sales[_dt_obj.weekday()].append(_dval)

    # data_day 재계산: HAVING >=0.1인 마지막 날
    data_day = 0
    for _r in _daily_rows:
        if float(_r["daily"]) >= 0.1:
            data_day = max(data_day, int(str(_r["dt"])[6:8]))
    if data_day == 0:
        data_day = today.day

    # ── ② 전월 일별 DOW 평균 (당월 DOW 데이터 부족 시 fallback) ──
    _prev_dow_sales: dict = _dd_dept(list)
    try:
        _prev_rows = _safe_query(f"""
            SELECT `대금청구일` AS dt,
                   ROUND(SUM(`매출액`)/1000000, 4) AS daily
            FROM {T_MAIN}
            WHERE `{dept_key}` = '{dept_name}' AND `년월` = '{prev_ym}'
            GROUP BY `대금청구일`
            HAVING ROUND(SUM(`매출액`)/1000000, 4) >= 0.1
        """, raw=True)
        for _r in _prev_rows:
            _ds = str(_r["dt"])
            _dt_obj = _dt_dept.date(int(_ds[:4]), int(_ds[4:6]), int(_ds[6:8]))
            if _dt_obj.weekday() != 6 and _dt_obj not in _NO_DELIVERY:
                _prev_dow_sales[_dt_obj.weekday()].append(float(_r["daily"]))
    except Exception:
        pass

    # ── ③ DOW 평균 산출 (당월 우선, 관측 <2이면 전월 평균, 없으면 단순평균) ──
    _simple_avg = sales_so_far / data_day if data_day > 0 else 0.0
    _dow_avg: dict = {}
    for _d in range(7):
        _this = _dow_sales.get(_d, [])
        _prev = _prev_dow_sales.get(_d, [])
        if len(_this) >= 2:
            _dow_avg[_d] = _stat_dept.mean(_this)
        elif len(_this) == 1 and len(_prev) >= 1:
            # 1개 관측 + 전월 평균 블렌딩 (60:40)
            _dow_avg[_d] = _this[0] * 0.6 + _stat_dept.mean(_prev) * 0.4
        elif len(_prev) >= 1:
            _dow_avg[_d] = _stat_dept.mean(_prev)
        else:
            _dow_avg[_d] = _simple_avg

    # ── ④ 남은 날 예측 합산 ──────────────────────────────────────
    _remaining = 0.0
    _working_remain = 0   # 남은 영업일 수 (정보 표시용)
    for _dn in range(data_day + 1, days_in_m + 1):
        _dt_r = _dt_dept.date(year, mo, _dn)
        if _dt_r.weekday() == 6 or _dt_r in _NO_DELIVERY:
            continue          # 일요일·실배송없는날 → 0
        _base = _dow_avg.get(_dt_r.weekday(), _simple_avg)
        if _dt_r in _HOLI:
            _remaining += _base * _HOLIDAY_BOOST   # 공휴일 boost ×1.5
        else:
            _remaining += _base
        _working_remain += 1

    forecast = sales_so_far + _remaining

    prev_total = _dq(f"AND `년월` = '{prev_ym}'")
    yoy_total  = _dq(f"AND `년월` = '{yoy_ym}'")

    # YTD 이번 해
    ytd_this = sales_so_far
    if mo > 1:
        ytd_this += _dq(f"AND `년월` >= '{year}01' AND `년월` < '{ym_now}'")

    # YTD 전년 동기
    ytd_last = 0.0
    if mo > 1:
        ytd_last += _dq(f"AND `년월` >= '{year-1}01' AND `년월` < '{yoy_ym}'")
    _day_from = f"{year-1}{mo:02d}01"
    _day_to   = f"{year-1}{mo:02d}{data_day:02d}"
    ytd_last += _dq(f"AND `대금청구일` >= '{_day_from}' AND `대금청구일` <= '{_day_to}'")

    def _pct(new_val, old_val):
        if old_val <= 0:
            return "N/A"
        rate  = (new_val - old_val) / old_val * 100
        arrow = "↑" if rate >= 0 else "↓"
        return f"{rate:+.1f}% {arrow}"

    so_far_s   = f"{_format_value(sales_so_far)}백만"
    forecast_s = f"{_format_value(forecast)}백만"
    prev_s     = f"{_format_value(prev_total)}백만"
    yoy_s      = f"{_format_value(yoy_total)}백만"
    ytd_this_s = f"{_format_value(ytd_this)}백만"
    ytd_last_s = f"{_format_value(ytd_last)}백만"

    lines = [
        f"📊 {dept_name} 매출 현황 ({mo}월 1~{data_day}일 기준)\n",
        f"이번달 누계       {so_far_s}",
        f"이번달 예상       {forecast_s}  (잔여 영업일 {_working_remain}일 반영)",
        "",
        f"전월({prev_mo}월) 比      {prev_s} → {_pct(forecast, prev_total)}  (예상 기준)",
        f"전년 동월 比      {yoy_s} → {_pct(forecast, yoy_total)}  (예상 기준)",
        "",
        "─────────────────────",
        f"올해 누계         {ytd_this_s} (1월~{mo}월 {data_day}일)",
        f"전년 동기 比      {ytd_last_s} → {_pct(ytd_this, ytd_last)}",
    ]
    return "\n".join(lines), forecast


def _fetch_brand_monthly_sales(brand_name: str, yearmonth: str) -> tuple[str, float, str] | None | list[str]:
    """ZC본부명 LIKE 유사검색 (외식식재사업부 한정)
    - 정확히 1건 매칭 → (브랜드명, 매출, 집계단위) 반환
    - 2건 이상 매칭   → 후보 브랜드명 리스트 반환  (list[str])
    - 0건             → None
    """
    # 입력 정규화: 반각 → 전각 (DB는 SAP 입력으로 전각 저장)
    brand_name = brand_name.replace('&', '＆')
    # 1) 정확 매칭 먼저 시도
    exact_candidates = [brand_name, f"{brand_name}(본사)", f"{brand_name} 본사"]
    for cand in exact_candidates:
        rows = _safe_query(f"""
            SELECT `ZC본부명` AS name,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND `ZC본부명` = '{cand}'
              AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
              AND `년월` = '{yearmonth}'
            GROUP BY `ZC본부명`
        """)
        if rows and float(rows[0].get("sales", 0)) > 0:
            return str(rows[0]["name"]), float(rows[0]["sales"]), "브랜드(ZC)"

    # 2) LIKE 유사검색
    like_rows = _safe_query(f"""
        SELECT `ZC본부명` AS name,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `ZC본부명` LIKE '%{brand_name}%'
          AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
          AND `년월` = '{yearmonth}'
        GROUP BY `ZC본부명`
        ORDER BY SUM(`매출액`) DESC
    """)
    # 매출 > 0 인 것만
    like_rows = [r for r in like_rows if float(r.get("sales", 0)) > 0]
    if not like_rows:
        # 2.5) ZA거래처명 검색 (개인형 ZC → DB에서 ZC본부명='개인형'으로 치환된 경우)
        za_rows = _safe_query(f"""
            SELECT `ZA거래처명` AS name,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND `ZA거래처명` LIKE '%{brand_name}%'
              AND `년월` = '{yearmonth}'
            GROUP BY `ZA거래처명`
            ORDER BY SUM(`매출액`) DESC
        """, raw=True)  # raw=True: ZA→ZC 변환 스킵
        za_rows = [r for r in za_rows if float(r.get("sales", 0)) > 0]
        if za_rows:
            if len(za_rows) == 1:
                return str(za_rows[0]["name"]), float(za_rows[0]["sales"]), "거래처(ZA)"
            return [str(r["name"]) for r in za_rows]
    if not like_rows:
        # 3) 거래처명 fallback (단일 점포명 등, 공백 제거 비교 포함)
        _bn_ns = brand_name.replace(' ', '')
        cust_rows = _safe_query(f"""
            SELECT `거래처명` AS name,
                   MAX(`ZC본부명`) AS zc,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND (`거래처명` LIKE '%{brand_name}%'
                OR REPLACE(`거래처명`, ' ', '') LIKE '%{_bn_ns}%')
              AND `년월` = '{yearmonth}'
            GROUP BY `거래처명`
            ORDER BY SUM(`매출액`) DESC
        """)
        cust_rows = [r for r in cust_rows if float(r.get("sales", 0)) > 0]
        if not cust_rows:
            return None
        if len(cust_rows) == 1:
            return str(cust_rows[0]["name"]), float(cust_rows[0]["sales"]), "단일 거래처"
        # 4) 동일 ZC 다건 → ZC 아래 전체 거래처가 검색어 포함 시만 ZC 집계로 승격
        #    그렇지 않으면 매칭 거래처명들 합산 반환
        zc_names = {str(r.get("zc", "") or "") for r in cust_rows}
        zc_names.discard("")
        if len(zc_names) == 1:
            zc_name = zc_names.pop()
            # ZC 승격 가드: 검색어 토큰이 ZC명에 포함돼야 승격 허용
            _zc_guard = any(t in zc_name for t in brand_name.replace('(', '').replace(')', '').split())
            if not _zc_guard:
                total_sales = sum(float(r.get("sales", 0)) for r in cust_rows)
                display_name = f"{brand_name} (전체 {len(cust_rows)}개 점포 합계)"
                return display_name, total_sales, "점포합산"
            # ZC 아래 거래처가 모두 검색어 포함인지 확인
            _bn_ns2 = brand_name.replace(' ', '')
            all_custs = _safe_query(f"""
                SELECT DISTINCT `거래처명`
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `ZC본부명` = '{zc_name}'
                  AND `년월` = '{yearmonth}'
            """)
            all_match = all(
                brand_name in str(r.get("거래처명", ""))
                or _bn_ns2 in str(r.get("거래처명", "")).replace(' ', '')
                for r in all_custs
            )
            if all_match:
                # ZC 전체 집계 승격 (개인형 ZC 제외: 코드 8 시작만)
                zc_row = _safe_query(f"""
                    SELECT `ZC본부명` AS name,
                           ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                    FROM {T_MAIN}
                    WHERE `사업부명` = '외식식재사업부'
                      AND `ZC본부명` = '{zc_name}'
                      AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
                      AND `년월` = '{yearmonth}'
                    GROUP BY `ZC본부명`
                """)
                if zc_row and float(zc_row[0].get("sales", 0)) > 0:
                    return str(zc_row[0]["name"]), float(zc_row[0]["sales"]), "브랜드(ZC)"
            else:
                # ZC 아래 다른 브랜드 포함 → 매칭 거래처명만 합산
                total_sales = sum(float(r.get("sales", 0)) for r in cust_rows)
                display_name = f"{brand_name} (전체 {len(cust_rows)}개 점포 합계)"
                return display_name, total_sales, "점포합산"
        return [str(r["name"]) for r in cust_rows]
    if len(like_rows) == 1:
        return str(like_rows[0]["name"]), float(like_rows[0]["sales"]), "브랜드(ZC)"
    # 여러 건 → 후보 리스트 반환
    return [str(r["name"]) for r in like_rows]


def _fuzzy_search_candidates(name_query: str, yearmonth: str) -> list[tuple[str, float, str]]:
    """
    토큰 분리 후 LIKE 패턴 조합으로 거래처명/ZA/ZC 3단계 검색
    Returns: list of (name, sales, level_label) sorted by sales desc, max 8
    """
    name_query = name_query.replace('&', '＆')
    tokens = name_query.replace('(', '').replace(')', '').split()
    if not tokens:
        return []

    def make_like(toks):
        return '%' + '%'.join(toks) + '%'

    like_fwd = make_like(tokens)
    like_rev = make_like(list(reversed(tokens)))

    results: list[tuple[str, float, str]] = []
    seen: set[str] = set()

    # 1) 거래처명 검색
    rows1 = _safe_query(f"""
        SELECT `거래처명` AS name,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부' AND `년월` = '{yearmonth}'
          AND (`거래처명` LIKE '{like_fwd}' OR `거래처명` LIKE '{like_rev}')
        GROUP BY `거래처명`
        ORDER BY SUM(`매출액`) DESC LIMIT 8
    """)
    for r in rows1:
        n = str(r["name"])
        if n not in seen and float(r.get("sales", 0)) > 0:
            seen.add(n)
            results.append((n, float(r["sales"]), "단일 거래처"))

    # 거래처명에서 결과가 나왔으면 ZA/ZC 추가검색 생략 (속도 최적화)
    if results:
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:8]

    # 2) ZA거래처명 검색
    if len(results) < 8:
        rows2 = _safe_query(f"""
            SELECT `ZA거래처명` AS name,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부' AND `년월` = '{yearmonth}'
              AND (`ZA거래처명` LIKE '{like_fwd}' OR `ZA거래처명` LIKE '{like_rev}')
            GROUP BY `ZA거래처명`
            ORDER BY SUM(`매출액`) DESC LIMIT 8
        """)
        for r in rows2:
            n = str(r["name"])
            if n not in seen and float(r.get("sales", 0)) > 0:
                seen.add(n)
                results.append((n, float(r["sales"]), "거래처(ZA)"))

    # 3) ZC본부명 검색
    if len(results) < 8:
        rows3 = _safe_query(f"""
            SELECT `ZC본부명` AS name,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부' AND `년월` = '{yearmonth}'
              AND (`ZC본부명` LIKE '{like_fwd}' OR `ZC본부명` LIKE '{like_rev}')
            GROUP BY `ZC본부명`
            ORDER BY SUM(`매출액`) DESC LIMIT 8
        """)
        for r in rows3:
            n = str(r["name"])
            if n not in seen and float(r.get("sales", 0)) > 0:
                seen.add(n)
                results.append((n, float(r["sales"]), "브랜드(ZC)"))

    # 단일 토큰이면 ZC명 앞부분으로 자동 분리 시도
    if len(tokens) == 1 and not results:
        raw = tokens[0]
        zc_rows = _safe_query(f"""
            SELECT DISTINCT `ZC본부명` FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND `ZC본부명` LIKE '%{raw[:4]}%'
              AND `년월` = '{yearmonth}'
            LIMIT 5
        """)
        for zr in zc_rows:
            zc_n = str(zr.get("ZC본부명", ""))
            zc_clean = re.sub(r'[()（）].*', '', zc_n).strip()
            if zc_clean and zc_clean != raw and zc_clean in raw:
                remainder = raw[len(zc_clean):]
                if len(remainder) >= 2:
                    sub_tokens = [zc_clean, remainder]
                    sub_fwd = make_like(sub_tokens)
                    sub_rows = _safe_query(f"""
                        SELECT `거래처명` AS name,
                               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                        FROM {T_MAIN}
                        WHERE `사업부명` = '외식식재사업부' AND `년월` = '{yearmonth}'
                          AND `거래처명` LIKE '{sub_fwd}'
                        GROUP BY `거래처명`
                        ORDER BY SUM(`매출액`) DESC LIMIT 8
                    """)
                    for r in sub_rows:
                        n = str(r["name"])
                        if n not in seen and float(r.get("sales", 0)) > 0:
                            seen.add(n)
                            results.append((n, float(r["sales"]), "단일 거래처"))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:8]


def _build_monthly_sales_markdown(rows: list[dict]) -> str:
    """사업부/지점 월별 전체매출 — 전월비/전년비 한 줄 포맷 + 컨텍스트 태그 포함"""
    row = rows[0]
    target_key  = "사업부명" if "사업부명" in row else "지점명"
    target_name = str(row.get(target_key, ""))
    yearmonth   = str(row.get("년월", ""))
    sales       = _extract_monthly_sales_value(row)
    month_label = f"{int(yearmonth[4:6])}월" if len(yearmonth) >= 6 else yearmonth

    cmp  = _fetch_monthly_comparison(target_key, target_name, yearmonth)
    prev = cmp["전월"]
    yoy  = cmp["전년동월"]

    parts = [f"{target_name}의 {month_label} 매출액은 {_format_value(sales)}백만원입니다."]
    if prev > 0:
        chg_mom = (sales - prev) / prev * 100
        word = "증가" if chg_mom >= 0 else "감소"
        parts.append(f"전월 {_format_value(prev)}백만 대비 {chg_mom:+.1f}% {word}.")
    if yoy > 0:
        chg_yoy = (sales - yoy) / yoy * 100
        word = "증가했습니다." if chg_yoy >= 0 else "감소했습니다."
        parts.append(f"전년 동월 {_format_value(yoy)}백만 대비 {chg_yoy:+.1f}% {word}")

    text = "\n".join(parts)
    text += "\n\n💡 증가/감소 사유가 궁금하시면 \"증가사유 알려줘\" 라고 입력하세요."
    # 컨텍스트 태그: _call_dify_and_callback에서 파싱하여 user context 저장
    text += f"\n<<<SALES_CTX:{target_key}|{target_name}|{yearmonth}>>>"
    return text


def _fetch_sales_reason(target_key: str, target_name: str, yearmonth: str,
                        forecast_total: float | None = None) -> str:
    """증가사유 상세 — 전월비(증가TOP3·감소TOP3) + 전년비(신규·중단·기존증감)"""
    ym    = str(yearmonth)
    year  = int(ym[:4])
    month = int(ym[4:6])
    prev_ym = f"{year-1}12" if month == 1 else f"{year}{month-1:02d}"
    yoy_ym  = f"{year-1}{month:02d}"
    month_label      = f"{month}월"
    prev_month_label = f"{int(prev_ym[4:6])}월"
    yoy_label        = f"{year-1}년 {month_label}"

    def _q_by_month(target_ym: str) -> dict[str, dict]:
        """ZC본부별 매출 맵: {zc코드: {name, sales}}"""
        rows = _safe_query(f"""
            SELECT `ZC본부` AS zc, `ZC본부명` AS nm,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 6) AS sales
            FROM {T_MAIN}
            WHERE `{target_key}` = '{target_name}' AND `년월` = '{target_ym}'
            GROUP BY `ZC본부`, `ZC본부명`
        """)
        return {str(r["zc"]): {"name": str(r["nm"]), "sales": float(r.get("sales", 0))} for r in rows}

    def _q_new_zc() -> set[str]:
        """신규 거래처 ZC본부 코드 집합 (최초 대금청구 >= _NEW_CUST_DATE)"""
        rows = _safe_query(f"""
            SELECT `ZC본부` AS zc
            FROM {T_MAIN}
            WHERE `{target_key}` = '{target_name}'
            GROUP BY `ZC본부`
            HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
        """)
        return {str(r["zc"]) for r in rows}

    # 4개 쿼리 병렬 실행
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_cur  = ex.submit(_q_by_month, ym)
        f_prev = ex.submit(_q_by_month, prev_ym)
        f_yoy  = ex.submit(_q_by_month, yoy_ym)
        f_new  = ex.submit(_q_new_zc)

    cur_map  = f_cur.result()
    prev_map = f_prev.result()
    yoy_map  = f_yoy.result()
    new_zc   = f_new.result()

    # ── 당월 조회 시 예상 배율 적용 (요일별 가중 런레이트) ──────────
    import calendar as _cal_reason, datetime as _dt_reason, statistics as _stat_reason
    from collections import defaultdict as _dd_reason
    _today_r  = _dt_reason.date.today()
    _cur_ym_r = _today_r.strftime("%Y%m")
    _forecast_factor = 1.0
    _data_day_r = None
    if ym == _cur_ym_r:
        try:
            from forecast_engine_v7 import KOR_HOLIDAYS as _HOLI_R
        except Exception:
            _HOLI_R = set()
        _HOLIDAY_BOOST_R = 1.5
        try:
            _nd_rows_r = _safe_query(
                "SELECT no_delivery_date AS dt FROM h_hmfo_fsi_dm.gd_rst_ing.dim_holidays"
                f" WHERE holiday_year IN ({int(ym[:4])-1}, {int(ym[:4])})",
                raw=True,
            )
            import datetime as _dt_nd_r
            def _to_date_ndr(v):
                if isinstance(v, _dt_nd_r.date): return v
                s = str(v).replace("-", "")
                return _dt_nd_r.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            _NO_DEL_R = {_to_date_ndr(r["dt"]) for r in _nd_rows_r}
        except Exception:
            _NO_DEL_R = set()
        try:
            _yr_r = int(ym[:4]);  _mo_r = int(ym[4:])
            _dim_r = _cal_reason.monthrange(_yr_r, _mo_r)[1]
            # 당월 일별 매출
            _dr_rows = _safe_query(f"""
                SELECT `대금청구일` AS dt, ROUND(SUM(`매출액`)/1000000,4) AS daily
                FROM {T_MAIN}
                WHERE `{target_key}` = '{target_name}' AND `년월` = '{ym}'
                GROUP BY `대금청구일`
                HAVING ROUND(SUM(`매출액`)/1000000,4) >= 0.01
                ORDER BY `대금청구일`
            """, raw=True)
            # data_day 산출
            _data_day_r = 0
            _sales_total_r = 0.0
            _dow_s_r: dict = _dd_reason(list)
            for _rr in _dr_rows:
                _ds = str(_rr["dt"]);  _dd2 = int(_ds[6:8]);  _dv = float(_rr["daily"])
                if _dv >= 0.1:
                    _data_day_r = max(_data_day_r, _dd2)
                    _sales_total_r += _dv
                _dt2 = _dt_reason.date(_yr_r, _mo_r, _dd2)
                if _dt2.weekday() != 6 and _dt2 not in _NO_DEL_R:
                    _dow_s_r[_dt2.weekday()].append(_dv)
            if _data_day_r == 0:
                _data_day_r = _today_r.day
                _sales_total_r = sum(float(_rr["daily"]) for _rr in _dr_rows)
            # 전월 DOW 평균 (fallback)
            _prev_ym_r = (_dt_reason.date(_yr_r, _mo_r, 1) - _dt_reason.timedelta(days=1)).strftime("%Y%m")
            _prev_dow_r: dict = _dd_reason(list)
            try:
                _pr_rows = _safe_query(f"""
                    SELECT `대금청구일` AS dt, ROUND(SUM(`매출액`)/1000000,4) AS daily
                    FROM {T_MAIN}
                    WHERE `{target_key}` = '{target_name}' AND `년월` = '{_prev_ym_r}'
                    GROUP BY `대금청구일`
                    HAVING ROUND(SUM(`매출액`)/1000000,4) >= 0.1
                """, raw=True)
                for _rr in _pr_rows:
                    _ds = str(_rr["dt"])
                    _dt2 = _dt_reason.date(int(_ds[:4]), int(_ds[4:6]), int(_ds[6:8]))
                    if _dt2.weekday() != 6 and _dt2 not in _NO_DEL_R:
                        _prev_dow_r[_dt2.weekday()].append(float(_rr["daily"]))
            except Exception:
                pass
            # DOW 평균
            _simple_avg_r = _sales_total_r / _data_day_r if _data_day_r else 0.0
            _dow_avg_r: dict = {}
            for _d in range(7):
                _th = _dow_s_r.get(_d, []);  _pv = _prev_dow_r.get(_d, [])
                if len(_th) >= 2:
                    _dow_avg_r[_d] = _stat_reason.mean(_th)
                elif len(_th) == 1 and len(_pv) >= 1:
                    _dow_avg_r[_d] = _th[0] * 0.6 + _stat_reason.mean(_pv) * 0.4
                elif len(_pv) >= 1:
                    _dow_avg_r[_d] = _stat_reason.mean(_pv)
                else:
                    _dow_avg_r[_d] = _simple_avg_r
            # 남은 날 합산
            _rem_r = 0.0
            for _dn in range(_data_day_r + 1, _dim_r + 1):
                _dt2 = _dt_reason.date(_yr_r, _mo_r, _dn)
                if _dt2.weekday() == 6 or _dt2 in _NO_DEL_R:
                    continue
                _base_r = _dow_avg_r.get(_dt2.weekday(), _simple_avg_r)
                if _dt2 in _HOLI_R:
                    _rem_r += _base_r * _HOLIDAY_BOOST_R
                else:
                    _rem_r += _base_r
            _forecast_r = _sales_total_r + _rem_r
            if _sales_total_r > 0:
                _forecast_factor = _forecast_r / _sales_total_r
        except Exception:
            _forecast_factor = 1.0
    # forecast_total이 외부에서 주입된 경우 그 값을 우선 사용 (카드와 일치)
    if forecast_total is not None and forecast_total > 0:
        _actual_sum = sum(d["sales"] for d in cur_map.values())
        if _actual_sum > 0:
            _forecast_factor = forecast_total / _actual_sum
    if _forecast_factor != 1.0:
        cur_map = {
            zc: {"name": d["name"], "sales": d["sales"] * _forecast_factor}  # round 미적용: 누적 오차 방지
            for zc, d in cur_map.items()
        }

    def is_brand(zc: str) -> bool:
        """ZC본부 코드 앞자리가 8 → 브랜드, 아니면 개인형"""
        return zc.lstrip("0").startswith("8")

    # ─── 전월 대비 증감 계산 ──────────────────────────────
    mom_list: list[dict] = []
    for zc in set(cur_map) | set(prev_map):
        cv = cur_map.get(zc, {}).get("sales", 0.0)
        pv = prev_map.get(zc, {}).get("sales", 0.0)
        nm = (cur_map.get(zc) or prev_map.get(zc) or {}).get("name", zc)
        mom_list.append({"zc": zc, "name": nm, "diff": round(cv - pv, 2), "cur": cv})

    mom_inc  = sorted([x for x in mom_list if x["diff"] > 0], key=lambda x: x["diff"], reverse=True)
    mom_dec  = sorted([x for x in mom_list if x["diff"] < 0], key=lambda x: x["diff"])

    # ─── 전년 대비: 신규/중단/기존 분류 ──────────────────
    # 신규: new_zc 집합(MIN 청구일 기준 진짜 신규)에 속하고 yoy에 없음
    # 복귀: cur에 있고 yoy에 없지만 new_zc 아님 → exist_rows에 diff=cur로 추가
    # 기존: cur/yoy 둘 다 있음
    # 중단: yoy에 있고 cur에 없음
    new_rows: list[dict] = []
    stopped_rows: list[dict] = []
    exist_rows: list[dict] = []

    for zc, d in cur_map.items():
        if zc not in yoy_map:
            if zc in new_zc:
                new_rows.append({"zc": zc, "name": d["name"], "sales": d["sales"]})
            else:
                # 복귀 거래처: 기존 증감에 포함 (전년 0 대비 전액 증가)
                exist_rows.append({"zc": zc, "name": d["name"], "diff": d["sales"],
                                    "cur": d["sales"], "yoy": 0.0})
        else:
            diff = round(d["sales"] - yoy_map[zc]["sales"], 2)
            exist_rows.append({"zc": zc, "name": d["name"], "diff": diff,
                                "cur": d["sales"], "yoy": yoy_map[zc]["sales"]})

    for zc, d in yoy_map.items():
        if zc not in cur_map:
            stopped_rows.append({"zc": zc, "name": d["name"], "sales": d["sales"]})

    new_rows.sort(key=lambda x: x["sales"], reverse=True)
    stopped_rows.sort(key=lambda x: x["sales"], reverse=True)
    exist_inc = sorted([x for x in exist_rows if x["diff"] > 0], key=lambda x: x["diff"], reverse=True)
    exist_dec = sorted([x for x in exist_rows if x["diff"] < 0], key=lambda x: x["diff"])

    def _brand(lst: list[dict]) -> list[dict]:
        return [x for x in lst if is_brand(x["zc"])]

    def _pers(lst: list[dict]) -> list[dict]:
        return [x for x in lst if not is_brand(x["zc"])]

    def _sum_sales(lst: list[dict]) -> float:
        return round(sum(x.get("sales", x.get("diff", 0)) for x in lst), 2)

    # 전년 순증감: forecast_total 주입 시 카드와 동일한 직접 SUM 기준 (% 포함)
    # 전년 순증감 = 세부합 기준 (신규+기존증감-중단 = 순증감 항상 일치)
    _new_total     = round(sum(x["sales"] for x in new_rows), 2)
    _stopped_total = round(sum(x["sales"] for x in stopped_rows), 2)
    _exist_net     = round(sum(x["diff"]  for x in exist_rows), 2)
    yoy_net        = round(_new_total - _stopped_total + _exist_net, 2)
    # % 기준: forecast_total 주입 시 카드와 동일한 직접 SUM 사용
    if forecast_total is not None and forecast_total > 0:
        _yoy_direct = _safe_query(f"""
            SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS s
            FROM {T_MAIN}
            WHERE `{target_key}` = '{target_name}' AND `년월` = '{yoy_ym}'
        """)
        _yoy_base = float(_yoy_direct[0]["s"]) if _yoy_direct else round(sum(v["sales"] for v in yoy_map.values()), 2)
    else:
        _yoy_base = round(sum(v["sales"] for v in yoy_map.values()), 2)

    lines = [f"📊 {target_name} {month_label} 매출 변동 분석", ""]
    if _forecast_factor != 1.0 and _data_day_r:
        lines.append(f"※ 월말 예상매출액 기준\n")

    # ─── 섹션 1: 전월 대비 ────────────────────────────────
    lines.append(f"【전월({prev_month_label}) 대비】")
    mom_net = round(sum(x["diff"] for x in mom_list), 2)
    # forecast_total 주입 시 전월도 직접 SUM으로 계산 (카드와 일치)
    if forecast_total is not None and forecast_total > 0:
        _prev_direct = _safe_query(f"""
            SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000,2) AS s
            FROM {T_MAIN}
            WHERE `{target_key}` = '{target_name}' AND `년월` = '{prev_ym}'
        """)
        _prev_total = float(_prev_direct[0]["s"]) if _prev_direct else round(sum(v["sales"] for v in prev_map.values()), 2)
        mom_net = round(forecast_total - _prev_total, 2)
    else:
        _prev_total = round(sum(v["sales"] for v in prev_map.values()), 2)
    mom_sign = "+" if mom_net >= 0 else ""
    _mom_pct_str = ""
    if _prev_total > 0:
        _mom_pct = mom_net / _prev_total * 100
        _arr = "↑" if _mom_pct >= 0 else "↓"
        _mom_pct_str = f"({_mom_pct:+.1f}% {_arr})"
    lines.append(f"  💰 전월 대비 순증감: {mom_sign}{_format_value(mom_net)}백만 {_mom_pct_str}")

    def _pct_str(diff: float, base: float) -> str:
        if base <= 0:
            return ""
        p = diff / base * 100
        arr = "↑" if p >= 0 else "↓"
        return f"({p:+.1f}% {arr})"

    # 증가 TOP3
    ib = _brand(mom_inc);  ip = _pers(mom_inc)
    if ib or ip:
        lines.append(f"  📈 증가 TOP 3")
        for x in ib[:3]:
            prev_v = x['cur'] - x['diff']
            lines.append(f"    · {x['name']}  +{_format_value(x['diff'])}백만 (당월 {_format_value(x['cur'])}백만) {_pct_str(x['diff'], prev_v)}")
        if len(ib) > 3:
            lines.append(f"    · 브랜드 외 {len(ib)-3}개")
        if ip:
            _ip_diff = _sum_sales(ip)
            _ip_prev = sum((x['cur'] - x['diff']) for x in ip)
            lines.append(f"    · 개인형 {len(ip)}개  +{_format_value(_ip_diff)}백만 {_pct_str(_ip_diff, _ip_prev)}")

    # 감소 TOP3
    db = _brand(mom_dec);  dp = _pers(mom_dec)
    if db or dp:
        lines.append(f"  📉 감소 TOP 3")
        for x in db[:3]:
            prev_v = x['cur'] - x['diff']
            lines.append(f"    · {x['name']}  {_format_value(x['diff'])}백만 (당월 {_format_value(x['cur'])}백만) {_pct_str(x['diff'], prev_v)}")
        if len(db) > 3:
            lines.append(f"    · 브랜드 외 {len(db)-3}개")
        if dp:
            _dp_diff = _sum_sales(dp)
            _dp_prev = sum((x['cur'] - x['diff']) for x in dp)
            lines.append(f"    · 개인형 {len(dp)}개  {_format_value(_dp_diff)}백만 {_pct_str(_dp_diff, _dp_prev)}")

    lines.append("")

    # ─── 섹션 2: 전년 대비 ────────────────────────────────
    lines.append(f"【전년({yoy_label}) 대비】")
    sign = "+" if yoy_net >= 0 else ""
    _yoy_pct_str = _pct_str(yoy_net, _yoy_base)
    lines.append(f"  💰 전년 동월 순증감: {sign}{_format_value(yoy_net)}백만 {_yoy_pct_str}")

    # 신규 브랜드 — ZC 개수만, 금액은 전체 합산
    if new_rows:
        nb = _brand(new_rows);  np_ = _pers(new_rows)
        total_new = _sum_sales(new_rows)
        lines.append(f"  🆕 신규 ZC {len(nb)}개  +{_format_value(total_new)}백만 (개인형 {len(np_)}개 포함)")
        for x in nb[:3]:
            lines.append(f"    · {x['name']}  +{_format_value(x['sales'])}백만")
        if len(nb) > 3:
            lines.append(f"    · 브랜드 외 {len(nb)-3}개")

    # 중단 브랜드 — ZC 개수만, 금액은 전체 합산
    if stopped_rows:
        sb = _brand(stopped_rows);  sp_ = _pers(stopped_rows)
        total_stop = _sum_sales(stopped_rows)
        lines.append(f"  🔻 중단 ZC {len(sb)}개  -{_format_value(total_stop)}백만 (개인형 {len(sp_)}개 포함)")
        for x in sb[:3]:
            lines.append(f"    · {x['name']}  -{_format_value(x['sales'])}백만")
        if len(sb) > 3:
            lines.append(f"    · 브랜드 외 {len(sb)-3}개")

    # 기존 브랜드 증감
    if exist_rows:
        exist_total = round(sum(x["diff"] for x in exist_rows), 2)
        sign = "+" if exist_total >= 0 else ""
        lines.append(f"  🔄 기존 브랜드 증감 ({sign}{_format_value(exist_total)}백만)")
        eib = _brand(exist_inc);  eip = _pers(exist_inc)
        edb = _brand(exist_dec);  edp = _pers(exist_dec)
        if eib or eip:
            lines.append(f"    증가 TOP3")
            for x in eib[:3]:
                lines.append(f"      · {x['name']}  +{_format_value(x['diff'])}백만 {_pct_str(x['diff'], x.get('yoy', 0))}")
            if eip:
                _eip_diff = sum(x['diff'] for x in eip)
                _eip_yoy  = sum(x.get('yoy', 0) for x in eip)
                lines.append(f"      · 개인형 {len(eip)}개  +{_format_value(_eip_diff)}백만 {_pct_str(_eip_diff, _eip_yoy)}")
        if edb or edp:
            lines.append(f"    감소 TOP3")
            for x in edb[:3]:
                lines.append(f"      · {x['name']}  {_format_value(x['diff'])}백만 {_pct_str(x['diff'], x.get('yoy', 0))}")
            if edp:
                _edp_diff = sum(x['diff'] for x in edp)
                _edp_yoy  = sum(x.get('yoy', 0) for x in edp)
                lines.append(f"      · 개인형 {len(edp)}개  {_format_value(_edp_diff)}백만 {_pct_str(_edp_diff, _edp_yoy)}")

    return "\n".join(lines)


# ─── 공통 유틸 (쿼리 파싱) ──────────────────────────────────

def _extract_month_year(query: str) -> tuple[int, str]:
    """쿼리에서 월/년월 추출. 없으면 현재 월.
    - '24년 12월', '2024년 3월' → (12, '202412')
    - '3월' → (3, '<올해>03')
    """
    cur_year = int(time.strftime("%Y"))
    # 년 + 월 세트 우선 (예: 24년 12월, 2024년 3월)
    ym = re.search(r'(\d{2,4})년\s*(\d{1,2})월', query)
    if ym:
        yr = int(ym.group(1))
        mo = int(ym.group(2))
        if yr < 100:          # 2자리 연도 예) 24 → 2024
            yr = 2000 + yr
        if 1 <= mo <= 12:
            return mo, f"{yr}{mo:02d}"
    m = re.search(r'(\d{1,2})월', query)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            return month, f"{cur_year}{month:02d}"
    now = time.localtime()
    return now.tm_mon, f"{cur_year}{now.tm_mon:02d}"


def _resolve_org_context(query: str) -> tuple[str, str]:
    """쿼리에서 조직 컨텍스트(target_key, target_name) 추출.
    지점명 > 사업부명 순으로 매칭. 기본값: 사업부명='외식식재사업부'
    """
    m = re.search(r'(외식\d팀|영남지점|신규개발파트)', query)
    if m:
        return "지점명", m.group(1).strip()
    m = re.search(r'([가-힣A-Za-z0-9]+(?:팀|지점|파트))', query)
    if m:
        name = m.group(1).strip()
        if name not in ("본사",):
            return "지점명", name
    m = re.search(r'([가-힣A-Za-z0-9]+사업부)', query)
    if m:
        return "사업부명", m.group(1).strip()
    return "사업부명", "외식식재사업부"


# ─── 자재/상품 분석 ─────────────────────────────────────────

def _fetch_product_ranking(target_key: str, target_name: str,
                           yearmonth: str, top_n: int = 10) -> list[dict]:
    """자재그룹별 매출 TOP N"""
    return _safe_query(f"""
        SELECT `자재그룹명`,
               ROUND(SUM(`매출액`) / 1000000, 2)     AS `매출_억`,
               COUNT(DISTINCT `자재명`)               AS `품목수`,
               ROUND(SUM(`매출수량`), 0)              AS `총수량`
        FROM {T_MAIN}
        WHERE `{target_key}` = '{target_name}'
          AND `년월` = '{yearmonth}'
          AND `자재그룹명` IS NOT NULL
        GROUP BY `자재그룹명`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
        LIMIT {top_n}
    """, raw=True)


def _fetch_product_detail(product_keyword: str, yearmonth: str) -> list[dict]:
    """특정 자재명 LIKE 검색으로 매출 조회"""
    return _safe_query(f"""
        SELECT `자재명`, `자재그룹명`,
               ROUND(SUM(`매출액`) / 1000000, 2) AS `매출_억`,
               ROUND(SUM(`매출수량`), 0)          AS `총수량`,
               COUNT(DISTINCT `ZC본부명`)          AS `거래처수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `자재명` LIKE '%{product_keyword}%'
          AND `년월` = '{yearmonth}'
        GROUP BY `자재명`, `자재그룹명`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
        LIMIT 15
    """, raw=True)


def _build_product_ranking_markdown(rows: list[dict],
                                    target_name: str, yearmonth: str) -> str:
    if not rows:
        return f"{target_name}의 자재그룹별 매출 데이터가 없습니다."
    month = int(yearmonth[4:6])
    total = sum(float(r.get("매출_억", 0)) for r in rows)
    lines = [f"📦 {target_name} {month}월 자재그룹별 매출 TOP{len(rows)}", ""]
    lines.append(f"총 매출: {_format_value(total)}백만원")
    lines.append("")
    lines.append("| 자재그룹 | 매출(백만) | 비중 | 품목수 |")
    lines.append("| --- | ---: | ---: | ---: |")
    for r in rows:
        s = float(r.get("매출_억", 0))
        pct = (s / total * 100) if total > 0 else 0
        cnt = int(r.get("품목수", 0))
        lines.append(f"| {r.get('자재그룹명', '')} | {_format_value(s)} | {pct:.1f}% | {cnt} |")
    return "\n".join(lines)


def _build_product_detail_markdown(rows: list[dict],
                                   keyword: str, yearmonth: str) -> str:
    if not rows:
        return f"'{keyword}' 자재를 찾을 수 없습니다."
    month = int(yearmonth[4:6])
    lines = [f"🔍 '{keyword}' 검색 결과 ({month}월, {len(rows)}건)", ""]
    for r in rows:
        name  = r.get("자재명", "")
        group = r.get("자재그룹명", "")
        sales = float(r.get("매출_억", 0))
        qty   = int(r.get("총수량", 0))
        custs = int(r.get("거래처수", 0))
        lines.append(f"• {name}")
        lines.append(f"  분류: {group} | 매출: {_format_value(sales)}백만 | 수량: {qty:,} | 거래처: {custs}개")
    return "\n".join(lines)


# ─── 범용상품 수익성 분석 ────────────────────────────────────

def _fetch_generic_margin(target_key: str, target_name: str,
                          yearmonth: str) -> dict:
    """범용상품(FC전용상품 제외) 이익률 — 자재그룹별"""
    rows = _safe_query(f"""
        SELECT `자재그룹명`,
               ROUND(SUM(`매출액`) / 1000000, 2)                AS `매출_억`,
               ROUND(SUM(`매출원가` * `매출수량`) / 1000000, 2) AS `원가_억`,
               COUNT(DISTINCT `자재명`)                          AS `품목수`
        FROM {T_MAIN}
        WHERE `{target_key}` = '{target_name}'
          AND `년월` = '{yearmonth}'
          AND `자재그룹명` != 'FC전용상품'
          AND `자재그룹명` IS NOT NULL
        GROUP BY `자재그룹명`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
    """, raw=True)
    total_s = sum(float(r.get("매출_억", 0)) for r in rows)
    total_c = sum(float(r.get("원가_억", 0)) for r in rows)
    total_gp = total_s - total_c
    gp_rate = (total_gp / total_s * 100) if total_s > 0 else 0
    return {
        "rows": rows,
        "total_sales": total_s,
        "total_cost": total_c,
        "total_gp": total_gp,
        "gp_rate": gp_rate,
    }


def _build_generic_margin_markdown(data: dict,
                                   target_name: str, yearmonth: str) -> str:
    rows = data.get("rows", [])
    if not rows:
        return f"{target_name}의 범용상품 수익성 데이터가 없습니다."
    month = int(yearmonth[4:6])
    ts = data["total_sales"]
    tc = data["total_cost"]
    gp = data["total_gp"]
    gp_rate = data["gp_rate"]
    lines = [
        f"💰 {target_name} {month}월 범용상품 수익성 (FC전용 제외)",
        "",
        f"매출: {_format_value(ts)}백만 | 원가: {_format_value(tc)}백만 | "
        f"GP: {_format_value(gp)}억 ({gp_rate:.1f}%)",
        "",
        "| 자재그룹 | 매출(백만) | 원가(백만) | GP(백만) | GP율 | 품목수 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        s    = float(r.get("매출_억", 0))
        c    = float(r.get("원가_억", 0))
        g    = s - c
        rate = (g / s * 100) if s > 0 else 0
        cnt  = int(r.get("품목수", 0))
        lines.append(
            f"| {r.get('자재그룹명', '')} | {_format_value(s)} | "
            f"{_format_value(c)} | {_format_value(g)} | {rate:.1f}% | {cnt} |"
        )
    return "\n".join(lines)


# ─── 고객 수익성 분석 (00_customers_cm) ────────────────────

def _profit_period_cond(period: str) -> str:
    """기간 조건 SQL 문자열 반환 (날짜 DATE 컬럼 기준)"""
    import datetime as _dt
    today = _dt.datetime.now()
    if period == "이번달":
        first = today.replace(day=1).strftime("%Y-%m-%d")
        return f"`날짜` = '{first}'"
    elif period == "지난달":
        if today.month == 1:
            prev = today.replace(year=today.year - 1, month=12, day=1)
        else:
            prev = today.replace(month=today.month - 1, day=1)
        return f"`날짜` = '{prev.strftime('%Y-%m-%d')}'"
    elif period == "올해":
        return f"YEAR(`날짜`) = {today.year}"
    elif re.match(r'^\d{6}$', period):  # 202603 형식
        date_str = f"{period[:4]}-{period[4:6]}-01"
        return f"`날짜` = '{date_str}'"
    else:
        return "`날짜` IS NOT NULL"


def _period_label(period: str) -> str:
    import datetime as _dt
    today = _dt.datetime.now()
    if period == "이번달":
        return f"{today.year}년 {today.month}월"
    elif period == "지난달":
        if today.month == 1:
            return f"{today.year-1}년 12월"
        else:
            return f"{today.year}년 {today.month-1}월"
    elif period == "올해":
        return f"{today.year}년 누계"
    elif re.match(r'^\d{6}$', period):  # 202603 형식
        return f"{period[:4]}년 {int(period[4:6])}월"
    return period


def _fmt_mil(v) -> str:
    """원 단위 → 백만원 포맷 (콤마 3자리)"""
    if v is None:
        return "-"
    return f"{int(v) // 1_000_000:,}백만"


def _pct(part, total) -> str:
    """비율(%) 문자열, total=0이면 -"""
    if total and int(total) > 0:
        return f"{int(part)/int(total)*100:.1f}%"
    return "-"


def _fmt_won(v) -> str:
    """원 단위 bigint → 억/만원 포맷 (NULL 처리)"""
    if v is None:
        return "-"
    v = int(v)
    if abs(v) >= 100_000_000:
        return f"{v/100_000_000:.2f}억"
    elif abs(v) >= 10_000:
        return f"{v/10_000:.0f}만"
    return f"{v:,}"


def _fmt_pct(cm, fi) -> str:
    """공헌이익률 (%)"""
    if fi and int(fi) > 0:
        return f"{int(cm)/int(fi)*100:.1f}%"
    return "-"


def _fetch_profit_branch(branch: str, period: str) -> str:
    """지점 전체 수익성 요약 (1행 합계) — 백만원 단위, CO기준"""
    cond = _profit_period_cond(period)
    rows = _safe_query(f"""
        SELECT
            SUM(`FI매출액`)     AS fi,
            SUM(`매출총이익`)   AS gp,
            SUM(`총운송비`)     AS trans,
            SUM(`총하역비`)     AS unload,
            SUM(`변동비`)       AS varfee,
            SUM(`공헌이익`)     AS cm
        FROM {T_PROFIT}
        WHERE `지점명` = '{branch}'
          AND {cond}
    """, raw=True)
    if not rows or rows[0].get("fi") is None:
        return f"📊 {branch} {_period_label(period)} 수익성 데이터가 없습니다.\n※ 최신 데이터: 2026년 3월"
    r = rows[0]
    fi     = int(r["fi"]     or 0)
    gp     = int(r["gp"]     or 0)
    trans  = int(r["trans"]  or 0)
    unload = int(r["unload"] or 0)
    var    = int(r["varfee"] or 0)
    cm     = int(r["cm"]     or 0)
    lines = [
        f"💰 {branch} {_period_label(period)} 수익성 분석",
        "",
        f"📌 매출액(CO): {_fmt_mil(fi)}",
        f"📌 매출총이익: {_fmt_mil(gp)} ({_pct(gp, fi)})",
        f"🔧 변동비:     {_fmt_mil(var)} ({_pct(var, fi)})",
        f"🚚 운송비:     {_fmt_mil(trans)} ({_pct(trans, fi)})",
        f"🏗️ 하역비:     {_fmt_mil(unload)} ({_pct(unload, fi)})",
        f"✅ 공헌이익:   {_fmt_mil(cm)} ({_pct(cm, fi)})",
        "",
        "※확정실적/CM 데이터 기준",
    ]
    return "\n".join(lines)


def _fetch_profit_by_brand(branch: str, period: str, top_n: int = 10) -> str:
    """브랜드별 수익성 (Zc본부명 GROUP BY, 상위 N) — 텍스트 포맷"""
    cond = _profit_period_cond(period)
    rows = _safe_query(f"""
        SELECT
            `Zc본부명`,
            SUM(`FI매출액`)   AS fi,
            SUM(`매출총이익`) AS gp,
            SUM(`변동비`)     AS var,
            SUM(`공헌이익`)   AS cm
        FROM {T_PROFIT}
        WHERE `지점명` = '{branch}'
          AND {cond}
          AND `FI매출액` IS NOT NULL
          AND `FI매출액` > 0
        GROUP BY `Zc본부명`
        ORDER BY fi DESC
        LIMIT {top_n}
    """, raw=True)
    if not rows:
        return f"📊 {branch} {_period_label(period)} 브랜드별 수익성 데이터가 없습니다."
    lines = [
        f"💰 {branch} {_period_label(period)} 브랜드별 수익성 (상위 {top_n})",
        "",
    ]
    for r in rows:
        brand = r.get("Zc본부명") or "-"
        fi  = int(r.get("fi") or 0)
        gp  = int(r.get("gp") or 0)
        cm  = int(r.get("cm") or 0)
        cmr = f"{cm/fi*100:.1f}%" if fi else "-"
        lines.append(f"■ {brand[:12]}")
        lines.append(f"  FI: {_fmt_won(fi)} | GP: {_fmt_won(gp)} | CM: {_fmt_won(cm)} ({cmr})")
        lines.append("")
    lines.append("※ SAP 기준 확정 데이터")
    return "\n".join(lines)


def _fetch_profit_by_customer(branch: str, period: str, top_n: int = 10) -> str:
    """거래처별 수익성 (거래처명 GROUP BY, 상위 N) — 텍스트 포맷"""
    cond = _profit_period_cond(period)
    rows = _safe_query(f"""
        SELECT
            `거래처명`,
            SUM(`FI매출액`)   AS fi,
            SUM(`매출총이익`) AS gp,
            SUM(`공헌이익`)   AS cm
        FROM {T_PROFIT}
        WHERE `지점명` = '{branch}'
          AND {cond}
          AND `FI매출액` IS NOT NULL
          AND `FI매출액` > 0
        GROUP BY `거래처명`
        ORDER BY fi DESC
        LIMIT {top_n}
    """, raw=True)
    if not rows:
        return f"📊 {branch} {_period_label(period)} 거래처별 수익성 데이터가 없습니다."
    lines = [
        f"💰 {branch} {_period_label(period)} 거래처별 수익성 (상위 {top_n})",
        "",
    ]
    for r in rows:
        cust = r.get("거래처명") or "-"
        fi  = int(r.get("fi") or 0)
        gp  = int(r.get("gp") or 0)
        cm  = int(r.get("cm") or 0)
        cmr = f"{cm/fi*100:.1f}%" if fi else "-"
        lines.append(f"■ {cust[:14]}")
        lines.append(f"  FI: {_fmt_won(fi)} | GP: {_fmt_won(gp)} | CM: {_fmt_won(cm)} ({cmr})")
        lines.append("")
    lines.append("※ SAP 기준 확정 데이터")
    return "\n".join(lines)


def _fetch_profit_by_name(keyword: str, period: str, branch: str = "") -> str:
    """브랜드명/거래처명 키워드로 수익성 조회"""
    cond = _profit_period_cond(period)
    _VALID_BRANCHES = {"외식1팀", "외식2팀", "외식3팀", "영남지점"}
    if branch in _VALID_BRANCHES:
        branch_cond = f"AND `지점명` = '{branch}'"
    else:
        branch_cond = "AND (`지점명` LIKE '%외식%' OR `지점명` LIKE '%영남%')"
    rows = _safe_query(f"""
        SELECT SUM(`FI매출액`) AS fi, SUM(`매출총이익`) AS gp,
               SUM(`총운송비`) AS trans, SUM(`총하역비`) AS unload,
               SUM(`변동비`) AS varfee, SUM(`공헌이익`) AS cm
        FROM {T_PROFIT}
        WHERE {cond} {branch_cond}
          AND (`Zc본부명` LIKE '%{keyword}%' OR `거래처명` LIKE '%{keyword}%')
    """, raw=True)
    if not rows or rows[0].get("fi") is None:
        return (f"📊 [{keyword}] {_period_label(period)} 수익성 데이터가 없습니다.\n"
                f"※ 현재 수익성 데이터는 26년 3월까지 제공되고 있습니다.")
    r = rows[0]
    fi    = int(r.get("fi") or 0)
    gp    = int(r.get("gp") or 0)
    trans = int(r.get("trans") or 0)
    unload = int(r.get("unload") or 0)
    var   = int(r.get("varfee") or 0)
    cm    = int(r.get("cm") or 0)
    return "\n".join([
        f"💰 [{keyword}] {_period_label(period)} 수익성 분석",
        "",
        f"📌 매출액(CO): {_fmt_mil(fi)}",
        f"📌 매출총이익: {_fmt_mil(gp)} ({_pct(gp, fi)})",
        f"🔧 변동비:     {_fmt_mil(var)} ({_pct(var, fi)})",
        f"🚚 운송비:     {_fmt_mil(trans)} ({_pct(trans, fi)})",
        f"🏗️ 하역비:     {_fmt_mil(unload)} ({_pct(unload, fi)})",
        f"✅ 공헌이익:   {_fmt_mil(cm)} ({_pct(cm, fi)})",
        "",
        "※확정실적/CM 데이터 기준",
    ])


_PROFIT_NAME_GUIDE = (
    "\n\n💡 특정 브랜드/거래처 수익성 조회:\n"
    "  예) '3월 신화푸드 수익성 알려줘'\n"
    "  예) '24년 12월 샐러디 수익성 알려줘'"
)


# ─── 판매구역 분석 ──────────────────────────────────────────

def _fetch_region_sales(region_keyword: str, yearmonth: str) -> list[dict]:
    """판매구역명 LIKE 검색"""
    return _safe_query(f"""
        SELECT `판매구역명`,
               ROUND(SUM(`매출액`) / 1000000, 2) AS `매출_억`,
               COUNT(DISTINCT `ZC본부명`)          AS `브랜드수`,
               COUNT(DISTINCT `ZA거래처`)          AS `거래처수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `판매구역명` LIKE '%{region_keyword}%'
          AND `년월` = '{yearmonth}'
        GROUP BY `판매구역명`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
        LIMIT 20
    """, raw=True)


def _fetch_region_ranking(yearmonth: str) -> list[dict]:
    """시도별 매출 랭킹 (판매구역명 첫 단어 기준)"""
    return _safe_query(f"""
        SELECT SUBSTRING_INDEX(`판매구역명`, ' ', 1) AS `시도`,
               ROUND(SUM(`매출액`) / 1000000, 2) AS `매출_억`,
               COUNT(DISTINCT `ZC본부명`)          AS `브랜드수`,
               COUNT(DISTINCT `ZA거래처`)          AS `거래처수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `년월` = '{yearmonth}'
          AND `판매구역명` IS NOT NULL AND `판매구역명` != ''
        GROUP BY SUBSTRING_INDEX(`판매구역명`, ' ', 1)
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
    """, raw=True)


def _build_region_sales_markdown(rows: list[dict], keyword: str,
                                 yearmonth: str,
                                 is_ranking: bool = False) -> str:
    if not rows:
        return f"'{keyword}' 지역의 매출 데이터가 없습니다."
    month = int(yearmonth[4:6])
    total = sum(float(r.get("매출_억", 0)) for r in rows)
    loc_key = "시도" if "시도" in rows[0] else "판매구역명"
    title = (f"📍 {month}월 시도별 매출 현황" if is_ranking
             else f"📍 '{keyword}' {month}월 매출 현황")
    col_label = "지역" if loc_key == "시도" else "판매구역"
    lines = [title, "", f"총 매출: {_format_value(total)}백만원", ""]
    lines.append(f"| {col_label} | 매출(백만) | 비중 | 브랜드 | 거래처 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for r in rows:
        s   = float(r.get("매출_억", 0))
        pct = (s / total * 100) if total > 0 else 0
        bc  = int(r.get("브랜드수", 0))
        cc  = int(r.get("거래처수", 0))
        lines.append(
            f"| {r.get(loc_key, '')} | {_format_value(s)} | "
            f"{pct:.1f}% | {bc} | {cc} |"
        )
    return "\n".join(lines)


# ─── 조직 심화 분석 ─────────────────────────────────────────

def _fetch_org_ranking(org_key: str, yearmonth: str) -> list[dict]:
    """조직 단위별(부서명/팀명/MD명/지점그룹명) 매출 랭킹 (월별)"""
    return _safe_query(f"""
        SELECT `{org_key}`,
               ROUND(SUM(`매출액`) / 1000000, 2) AS `매출_억`,
               COUNT(DISTINCT `영업사원명`)        AS `사원수`,
               COUNT(DISTINCT `ZC본부명`)          AS `브랜드수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `년월` = '{yearmonth}'
          AND `{org_key}` IS NOT NULL AND `{org_key}` != ''
        GROUP BY `{org_key}`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
    """, raw=True)


def _fetch_org_ranking_daily(org_key: str, date_str: str) -> list[dict]:
    """조직 단위별 매출 랭킹 (일별, 대금청구일 기준)"""
    return _safe_query(f"""
        SELECT `{org_key}`,
               ROUND(SUM(`매출액`) / 1000000, 4) AS `매출_억`,
               COUNT(DISTINCT `영업사원명`)        AS `사원수`,
               COUNT(DISTINCT `ZC본부명`)          AS `브랜드수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `대금청구일` = '{date_str}'
          AND `{org_key}` IS NOT NULL AND `{org_key}` != ''
        GROUP BY `{org_key}`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
    """, raw=True)


def _build_org_ranking_markdown(rows: list[dict],
                                org_key: str, yearmonth: str,
                                date_label: str = "") -> str:
    if not rows:
        return f"{org_key}별 매출 데이터가 없습니다."
    month = int(yearmonth[4:6]) if len(yearmonth) >= 6 else 0
    total = sum(float(r.get("매출_억", 0)) for r in rows)
    label_map = {
        "부서명": "팀", "MD명": "MD",
        "지점그룹명": "지점그룹", "지점명": "지점",
    }
    label = label_map.get(org_key, org_key)
    period_str = date_label if date_label else f"{month}월"
    # 부서명 기준 팀별 매출은 고정 순서로 표시
    if org_key == "부서명":
        _TEAM_ORDER = ["외식1팀", "외식2팀", "외식3팀", "영남지점"]
        _order_map = {name: i for i, name in enumerate(_TEAM_ORDER)}
        rows = sorted(rows, key=lambda r: _order_map.get(str(r.get("부서명", "")), 99))
    lines = [f"🏢 외식식재사업부 {period_str} 팀별 매출", ""]
    lines.append(f"총 매출: {_format_value(total)}백만원")
    lines.append("")
    for r in rows:
        s   = float(r.get("매출_억", 0))
        pct = (s / total * 100) if total > 0 else 0
        sp  = int(r.get("사원수", 0))
        bc  = int(r.get("브랜드수", 0))
        lines.append(
            f"■ {r.get(org_key, '')} ({_format_value(s)}백만, {pct:.1f}%)"
        )
    return "\n".join(lines)


# ─── 거래처 심화 분석 ────────────────────────────────────────

def _fetch_zp_ranking(yearmonth: str, top_n: int = 15) -> list[dict]:
    """ZP대표고객(본사)별 매출 TOP"""
    return _safe_query(f"""
        SELECT `ZP대표고객명`,
               ROUND(SUM(`매출액`) / 1000000, 2) AS `매출_억`,
               COUNT(DISTINCT `ZA거래처`)          AS `거래처수`,
               COUNT(DISTINCT `ZB본지점`)          AS `가맹점수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `년월` = '{yearmonth}'
          AND `ZP대표고객명` IS NOT NULL AND `ZP대표고객명` != ''
          AND `ZP대표고객명` NOT LIKE '(삭제)%'
        GROUP BY `ZP대표고객명`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
        LIMIT {top_n}
    """, raw=True)


def _fetch_customer_tier_sales(yearmonth: str) -> list[dict]:
    """고객계층별 매출"""
    return _safe_query(f"""
        SELECT `고객계층1명`,
               ROUND(SUM(`매출액`) / 1000000, 2) AS `매출_억`,
               COUNT(DISTINCT `ZC본부명`)          AS `브랜드수`,
               COUNT(DISTINCT `ZA거래처`)          AS `거래처수`
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `년월` = '{yearmonth}'
          AND `고객계층1명` IS NOT NULL AND `고객계층1명` != ''
        GROUP BY `고객계층1명`
        HAVING SUM(`매출액`) > 0
        ORDER BY `매출_억` DESC
    """, raw=True)


def _build_zp_ranking_markdown(rows: list[dict], yearmonth: str) -> str:
    if not rows:
        return "ZA본사별 매출 데이터가 없습니다."
    month = int(yearmonth[4:6])
    total = sum(float(r.get("매출_억", 0)) for r in rows)
    lines = [f"🏬 {month}월 ZA본사별 매출 TOP{len(rows)}", ""]
    lines.append(f"총 매출: {_format_value(total)}백만원")
    lines.append("")
    lines.append("| ZA본사 | 매출(백만) | 거래처 | 가맹점 |")
    lines.append("| --- | ---: | ---: | ---: |")
    for r in rows:
        s    = float(r.get("매출_억", 0))
        cc   = int(r.get("거래처수", 0))
        sc   = int(r.get("가맹점수", 0))
        name = r.get("ZP대표고객명", "")
        if len(name) > 25:
            name = name[:23] + "…"
        lines.append(f"| {name} | {_format_value(s)} | {cc} | {sc} |")
    return "\n".join(lines)


def _build_customer_tier_markdown(rows: list[dict], yearmonth: str) -> str:
    if not rows:
        return "고객계층별 매출 데이터가 없습니다."
    month = int(yearmonth[4:6])
    total = sum(float(r.get("매출_억", 0)) for r in rows)
    lines = [f"👥 {month}월 고객계층별 매출", ""]
    lines.append(f"총 매출: {_format_value(total)}백만원")
    lines.append("")
    lines.append("| 고객계층 | 매출(백만) | 비중 | 브랜드 | 거래처 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for r in rows:
        s   = float(r.get("매출_억", 0))
        pct = (s / total * 100) if total > 0 else 0
        bc  = int(r.get("브랜드수", 0))
        cc  = int(r.get("거래처수", 0))
        lines.append(
            f"| {r.get('고객계층1명', '')} | {_format_value(s)} | "
            f"{pct:.1f}% | {bc} | {cc} |"
        )
    return "\n".join(lines)


# ─── 미출고 현황 ─────────────────────────────────────────
def _fetch_unshipped(
    sp_name: str,
    date_str: str | None = None,
    only_gyucheck: bool = False,
) -> list[dict]:
    """미출고 현황 조회 (영업담당자명 기준)
    - sp_name: 영업담당자명 (LIKE 검색)
    - date_str: 'YYYY-MM-DD', None이면 테이블 최신일자 사용
    - only_gyucheck: True면 영업귀책(자책) 건만 반환
    """
    gyucheck_cond = (
        "AND (`미출사유명` LIKE '%영업귀책%' OR `귀책사유` = '자책')"
        if only_gyucheck
        else ""
    )
    # SAP 원본 데이터에 이름 중간 공백이 있을 수 있으므로 공백 제거 후 비교
    sp_name_nospace = sp_name.replace(' ', '')

    def _run_query(dc):
        return _safe_query(
            f"""
            SELECT `출고일자`, `통합배송처명`, `플랜트`, `플랜트명`, `상품코드`, `상품명`, `미출수량`,
                   `미출사유명`, `귀책사유`, `주문미출내용`, `영업담당자명`
            FROM {T_MISULGO}
            WHERE {dc}
              AND REPLACE(`영업담당자명`, ' ', '') LIKE '%{sp_name_nospace}%'
              {gyucheck_cond}
              AND `미출수량` > 0
            ORDER BY `귀책사유` DESC, `통합배송처명`
            LIMIT 50
            """,
            raw=True,
        )

    if date_str:
        return _run_query(f"`출고일자` = '{date_str}'")
    # 날짜 미지정: 오늘 먼저 시도 → 없으면 MAX 날짜로 fallback
    import datetime as _dt
    today_str = _dt.date.today().strftime("%Y-%m-%d")
    rows = _run_query(f"`출고일자` = '{today_str}'")
    if rows:
        return rows
    return _run_query(f"`출고일자` = (SELECT MAX(`출고일자`) FROM {T_MISULGO})")


def _fetch_unshipped_by_team(
    team_name: str,
    date_str: str | None = None,
    only_gyucheck: bool = False,
) -> list[dict]:
    """미출고 현황 조회 (부서명 기준 - 우리팀 조회용)"""
    gyucheck_cond = (
        "AND (`미출사유명` LIKE '%영업귀책%' OR `귀책사유` = '자책')"
        if only_gyucheck
        else ""
    )
    team_name_nospace = team_name.replace(' ', '')

    def _run_team_query(dc: str) -> list[dict]:
        return _safe_query(
            f"""
            SELECT `출고일자`, `부서명`, `영업담당자명`, `통합배송처명`, `상품명`, `미출수량`,
                   `미출사유명`, `귀책사유`, `주문미출내용`
            FROM {T_MISULGO}
            WHERE {dc}
              AND REPLACE(`부서명`, ' ', '') LIKE '%{team_name_nospace}%'
              {gyucheck_cond}
              AND `미출수량` > 0
            ORDER BY `귀책사유` DESC, `영업담당자명`, `통합배송처명`
            LIMIT 100
            """,
            raw=True,
        )

    if date_str:
        return _run_team_query(f"`출고일자` = '{date_str}'")
    # fallback: 오늘 먼저, 없으면 MAX 날짜
    import datetime as _dt
    today_str = _dt.date.today().strftime("%Y-%m-%d")
    rows = _run_team_query(f"`출고일자` = '{today_str}'")
    if rows:
        return rows
    return _run_team_query(f"`출고일자` = (SELECT MAX(`출고일자`) FROM {T_MISULGO})")


def _build_unshipped_markdown(
    rows: list[dict],
    sp_name: str,
    only_gyucheck: bool = False,
    is_team: bool = False,
) -> str:
    # is_team이면 이미 팀명 그대로 사용 (중복 '팀' 방지), 개인이면 '님' 접미사
    label    = sp_name                               # 화면 표시용
    honorific = "" if is_team else "님"
    if not rows:
        filter_txt = " (영업귀책)" if only_gyucheck else ""
        return f"✅ {label}{honorific} 담당 미출고{filter_txt} 건이 없습니다."

    date_val = rows[0].get("출고일자", "")
    total_qty = sum(float(r.get("미출수량", 0)) for r in rows)
    gyucheck_rows = [
        r for r in rows
        if str(r.get("귀책사유", "")).strip() == "자책"
        or "영업귀책" in str(r.get("미출사유명", ""))
    ]

    lines = [f"📦 {label}{honorific} 미출고 현황 ({date_val})", ""]
    lines.append(f"• 전체 {len(rows)}건 / 총 {int(total_qty)}개")
    if gyucheck_rows:
        lines.append(f"• ⚠️ 영업귀책 {len(gyucheck_rows)}건 → 직접 조치 필요")
    lines.append("")
    if is_team:
        from collections import Counter as _Counter
        _member_cnt: dict[str, int] = {}
        _member_qty: dict[str, int] = {}
        for r in rows:
            mn = str(r.get("영업담당자명", "") or "").strip()
            if not mn:
                continue
            _member_cnt[mn] = _member_cnt.get(mn, 0) + 1
            _member_qty[mn] = _member_qty.get(mn, 0) + int(float(r.get("미출수량", 0)))
        _sorted_members = sorted(_member_cnt.items(), key=lambda x: -x[1])
        lines.append("[ 담당자별 미출고 현황 ]")
        for mn, cnt in _sorted_members:
            qty = _member_qty[mn]
            lines.append(f"  • {mn}: {cnt}건 / {qty}개")
        lines.append("")
        # 미출사유 TOP5
        _reason_cnt: dict[str, int] = {}
        for r in rows:
            rs = str(r.get("미출사유명", "") or "").strip()
            if rs:
                _reason_cnt[rs] = _reason_cnt.get(rs, 0) + 1
        if _reason_cnt:
            _top_reasons = sorted(_reason_cnt.items(), key=lambda x: -x[1])[:5]
            lines.append("[ 주요 미출사유 ]")
            for rs, cnt in _top_reasons:
                lines.append(f"  • {rs[:20]}: {cnt}건")
    else:
        # 플랜트가 전부 동일하면 헤더에 1회만, 다르면 각 항목에 표시
        _plant_names = list({str(r.get("플랜트명", "") or "").strip() for r in rows if r.get("플랜트명")})
        _plant_codes = list({str(r.get("플랜트", "") or "").strip() for r in rows if r.get("플랜트")})
        _single_plant = len(_plant_names) == 1
        if _single_plant and _plant_names[0]:
            pcode = _plant_codes[0] if _plant_codes else ""
            lines.append(f"• 물류센터: {_plant_names[0]}({pcode})")
            lines.append("")

        # 같은 거래처+상품코드 반복 행을 그룹핑 (수량 합산, 사유는 첫 번째 사용)
        from collections import OrderedDict
        grouped: OrderedDict = OrderedDict()
        for r in rows:
            key = (str(r.get("통합배송처명", "")), str(r.get("상품코드", "") or ""))
            if key not in grouped:
                grouped[key] = {"row": r, "qty": 0, "cnt": 0}
            grouped[key]["qty"] += float(r.get("미출수량", 0) or 0)
            grouped[key]["cnt"] += 1
        grouped_rows = list(grouped.values())

        _circles = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
        show_max = min(12, len(grouped_rows))
        for i, g in enumerate(grouped_rows[:show_max]):
            r      = g["row"]
            total_cnt = g["cnt"]
            num    = _circles[i] if i < len(_circles) else f"{i+1}."
            loc    = str(r.get("통합배송처명", ""))[:18]
            code   = str(r.get("상품코드", "") or "").strip()
            # 상품명: 첫 '(' 이전 핵심 이름만
            raw_prod = str(r.get("상품명", ""))
            prod = raw_prod.split("(")[0].strip()[:18] if "(" in raw_prod else raw_prod[:18]
            qty    = int(g["qty"])
            reason = str(r.get("미출사유명", "") or "").strip()
            detail = str(r.get("주문미출내용", "") or "").strip()
            # 상세사유 접두어 정리: (영업안내), 영업귀책( 반복 제거
            detail = re.sub(r'^\(영업안내\)\s*', '', detail)
            detail = re.sub(r'^영업귀책\(', '', detail).lstrip('(')
            gyuk   = "⚠️자책" if str(r.get("귀책사유", "")).strip() == "자책" else "타책"
            reason_str = reason[:20] if reason else "-"
            if detail and detail not in ("-", reason):
                reason_str += f" ({detail[:22]})"
            code_str = f"({code}) " if code else ""
            cnt_str = f"×{total_cnt}건" if total_cnt > 1 else ""
            if not _single_plant:
                plant_name = str(r.get("플랜트명", "") or "").strip()
                plant_code = str(r.get("플랜트", "") or "").strip()
                plant_str = f" [{plant_name}({plant_code})]" if plant_name else ""
            else:
                plant_str = ""
            lines.append(f"{num} {loc}{plant_str} / {code_str}{prod} {qty}개{cnt_str} [{gyuk}]")
            lines.append(f"  └ {reason_str}")
        hidden = len(rows) - sum(g["cnt"] for g in grouped_rows[:show_max])
        if hidden > 0:
            lines.append(f"  … 외 {hidden}건")
    return "\n".join(lines)


# ─── API Key 검증 ─────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return key

# ─── 요청/응답 모델 ───────────────────────────────────────
class QueryRequest(BaseModel):
    sql: str

class SalesRequest(BaseModel):
    사업부코드: str | None = None
    사업부명: str | None = None
    시작일: str | None = None   # YYYY-MM-DD
    종료일: str | None = None

# ─── 엔드포인트 ───────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "auth": "cached" if _cached_token else "not_authenticated"}


@app.get("/auth")
def auth():
    """서버 시작 후 최초 1회 브라우저 인증 트리거"""
    try:
        get_token()
        return {"status": "ok", "message": "인증 완료"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/auth/reset")
def auth_reset():
    """토큰 초기화 후 재인증 (토큰 만료 시 사용)"""
    _clear_token_cache()
    try:
        get_token()
        return {"status": "ok", "message": "재인증 완료"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/divisions", dependencies=[Depends(verify_key)])
def get_divisions():
    """사업부 목록 조회"""
    try:
        rows = run_query("""
            SELECT DISTINCT `사업부`, `사업부명`
            FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
            ORDER BY `사업부`
        """)
        return {"divisions": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sales", dependencies=[Depends(verify_key)])
def get_sales(req: SalesRequest):
    """매출 데이터 조회"""
    where_clauses = []
    if req.사업부코드:
        where_clauses.append(f"`사업부` = '{req.사업부코드}'")
    if req.사업부명:
        where_clauses.append(f"`사업부명` LIKE '%{req.사업부명}%'")
    if req.시작일:
        where_clauses.append(f"`일자` >= '{req.시작일}'")
    if req.종료일:
        where_clauses.append(f"`일자` <= '{req.종료일}'")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT `사업부`, `사업부명`, `대금청구일`,
               SUM(`매출액`) AS 매출액합계
        FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
        {where_sql}
        GROUP BY `사업부`, `사업부명`, `대금청구일`
        ORDER BY `대금청구일` DESC
        LIMIT 100
    """
    try:
        rows = run_query(sql)
        return {"count": len(rows), "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query", dependencies=[Depends(verify_key)])
def custom_query(req: QueryRequest):
    """자유 SQL 쿼리 (관리자용) — 동일 SQL 60초 캐시 적용"""
    sql_hash = hashlib.md5(req.sql.strip().encode()).hexdigest()
    now = time.time()
    if sql_hash in _query_cache:
        cached_at, cached_resp = _query_cache[sql_hash]
        if now - cached_at < _QUERY_CACHE_TTL:
            logger.info(f"[쿼리캐시] HIT ({now - cached_at:.1f}초 전 결과 재사용)")
            return cached_resp
    try:
        rows = run_query(req.sql)
        if rows:
            logger.info(f"[쿼리] 결과 컬럼: {list(rows[0].keys())} | 행수: {len(rows)}")
        response = {"count": len(rows), "data": rows}
        if _is_new_sales_shape(rows):
            markdown = _build_new_sales_markdown(rows, original_sql=req.sql)
            response["rendered_markdown"] = markdown
            response["final_answer_block"] = (
                f"<<<ANSWER_START>>>\n{markdown}\n<<<ANSWER_END>>>"
            )
        elif _is_team_new_sales_shape(rows):
            markdown = _build_team_new_sales_markdown(rows, original_sql=req.sql)
            response["rendered_markdown"] = markdown
            response["final_answer_block"] = (
                f"<<<ANSWER_START>>>\n{markdown}\n<<<ANSWER_END>>>"
            )
        elif _is_monthly_sales_shape(rows):
            markdown = _build_monthly_sales_markdown(rows)
            response["rendered_markdown"] = markdown
            response["final_answer_block"] = (
                f"<<<ANSWER_START>>>\n{markdown}\n<<<ANSWER_END>>>"
            )
        _query_cache[sql_hash] = (time.time(), response)  # 캐시 저장
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── 사용자 인증/등록 ────────────────────────────────────────
AUTH_DEPT = "외식식재사업부"  # 허용 사업부
_USERS_FILE = os.path.join(os.path.dirname(__file__), "_registered_users.json")
_WHITELIST_FILE = os.path.join(os.path.dirname(__file__), "_admin_whitelist.json")
_BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "_admin_blacklist.json")
_users_lock = threading.Lock()

# 관리자급 수기 등록 화이트리스트 (DB에 영업사원 매출 없어도 등록 허용)
_MANAGER_WHITELIST: dict[str, dict] = {
    "20115003": {"영업사원명": "손상웅", "영업사원": "20115003", "지점명": "외식1팀"},
    "20065782": {"영업사원명": "권봉주", "영업사원": "20065782", "지점명": "외식3팀"},
    "20191191": {"영업사원명": "박상천", "영업사원": "20191191", "지점명": "외식식재사업부"},
    "20115029": {"영업사원명": "강동민", "영업사원": "20115029", "지점명": "외식식재사업부"},
    "20210054": {"영업사원명": "최희조", "영업사원": "20210054", "지점명": "외식식재사업부"},
    "20190061": {"영업사원명": "박지웅", "영업사원": "20190061", "지점명": "신규개발파트"},
    "20190801": {"영업사원명": "김남우", "영업사원": "20190801", "지점명": "신규개발파트"},
}

# 퇴사자 블랙리스트 (등록 차단)
_BLOCKED_EMPLOYEES: set[str] = {
    "20065629",  # 엄철용
}


def _load_users() -> dict:
    """등록 사용자 목록 로드. {kakao_id: {name, emp_code, registered_at}}"""
    if os.path.exists(_USERS_FILE):
        with open(_USERS_FILE, "r", encoding="utf-8") as f:
            return json_mod.load(f)
    return {}


def _save_users(users: dict):
    with open(_USERS_FILE, "w", encoding="utf-8") as f:
        json_mod.dump(users, f, ensure_ascii=False, indent=2)


def _load_whitelist() -> dict:
    """관리자 등록 화이트리스트. {emp_code: {name, team, added_at}}"""
    if os.path.exists(_WHITELIST_FILE):
        with open(_WHITELIST_FILE, "r", encoding="utf-8") as f:
            return json_mod.load(f)
    return {}


def _save_whitelist(wl: dict):
    with open(_WHITELIST_FILE, "w", encoding="utf-8") as f:
        json_mod.dump(wl, f, ensure_ascii=False, indent=2)


def _load_blacklist() -> list:
    """관리자 블랙리스트. [emp_code, ...] — 영구 등록 차단"""
    if os.path.exists(_BLACKLIST_FILE):
        with open(_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            return json_mod.load(f)
    return []


def _save_blacklist(bl: list):
    with open(_BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json_mod.dump(bl, f, ensure_ascii=False, indent=2)


def _is_registered(user_id: str) -> bool:
    users = _load_users()
    return user_id in users


def _get_registered_name(user_id: str) -> str | None:
    users = _load_users()
    entry = users.get(user_id)
    return entry.get("name") if entry else None


def _get_user_role(user_id: str) -> str:
    """사용자 역할 반환. 'admin' 또는 'user'"""
    users = _load_users()
    return users.get(user_id, {}).get("role", "user")


def _is_admin(user_id: str) -> bool:
    """관리자 여부 확인"""
    return _get_user_role(user_id) == "admin"


def _set_user_role(user_id: str, role: str) -> bool:
    """사용자 역할 변경. 성공 시 True."""
    with _users_lock:
        users = _load_users()
        if user_id not in users:
            return False
        users[user_id]["role"] = role
        _save_users(users)
    return True


def _find_user_by_emp_code(emp_code: str) -> str | None:
    """사번으로 이미 등록된 사용자가 있는지 확인. 있으면 이름 반환."""
    users = _load_users()
    for uid, info in users.items():
        if info.get("emp_code") == emp_code:
            return info.get("name")
    return None


def _verify_employee(name: str, emp_code: str) -> dict | None:
    """사원명+사번 인증. 화이트리스트 → DB 순서로 확인.
    Returns: {영업사원명, 영업사원, 지점명} or None
    """
    # 퇴사자 차단 (하드코딩 + 관리자 블랙리스트)
    if emp_code in _BLOCKED_EMPLOYEES:
        logger.warning(f"[인증] 블랙리스트 차단(하드코딩): emp_code={emp_code}")
        return None
    if emp_code in _load_blacklist():
        logger.warning(f"[인증] 블랙리스트 차단(관리자): emp_code={emp_code}")
        return None

    # 관리자 동적 화이트리스트 확인
    _dyn_wl = _load_whitelist()
    if emp_code in _dyn_wl:
        wl_entry = _dyn_wl[emp_code]
        compact_input = re.sub(r"\s+", "", name)
        compact_wl = re.sub(r"\s+", "", wl_entry.get("name", ""))
        if compact_input in compact_wl or compact_wl in compact_input:
            logger.info(f"[인증] 동적 화이트리스트 매칭: {wl_entry.get('name')}")
            return {"영업사원명": wl_entry.get("name", name), "영업사원": emp_code, "지점명": wl_entry.get("team", "")}

    # 관리자 화이트리스트 우선 확인 (DB 조회 불필요)
    if emp_code in _MANAGER_WHITELIST:
        wl = _MANAGER_WHITELIST[emp_code]
        compact_input = re.sub(r"\s+", "", name)
        compact_wl = re.sub(r"\s+", "", wl["영업사원명"])
        if compact_input in compact_wl or compact_wl in compact_input:
            logger.info(f"[인증] 화이트리스트 매칭: {wl['영업사원명']}")
            return wl

    # DB 조회
    compact_name = re.sub(r"\s+", "", name)
    rows = _safe_query(f"""
        SELECT DISTINCT `영업사원명`, `영업사원`, `지점명`
        FROM {T_MAIN}
        WHERE `사업부명` = '{AUTH_DEPT}'
          AND `영업사원` = '{emp_code}'
          AND regexp_replace(`영업사원명`, ' ', '') LIKE '%{compact_name}%'
        LIMIT 1
    """)
    if rows:
        return rows[0]
    return None


def _register_user(user_id: str, name: str, emp_code: str, db_info: dict) -> str:
    """사용자 등록 후 환영 메시지 반환"""
    with _users_lock:
        users = _load_users()
        users[user_id] = {
            "name": db_info.get("영업사원명", name),
            "emp_code": emp_code,
            "team": db_info.get("지점명", ""),
            "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_users(users)
    display_name = db_info.get("영업사원명", name)
    team = db_info.get("지점명", "")
    return (
        f"✅ 등록 완료!\n\n"
        f"이름: {display_name}\n"
        f"소속: {team}\n\n"
        f"저는 이런 질문에 답할 수 있어요:\n"
        f"• 영업사원별 신규매출액\n"
        f"• 팀별/지점별 매출 현황\n"
        f"• 거래처(ZC본부) 매출 내역\n\n"
        f"예시: 조윤식 신규매출액 알려줘"
    )


_REGISTER_GUIDE = (
    "🔒 이 챗봇은 외식식재사업부 전용입니다.\n\n"
    "사용하시려면 아래 형식으로 등록해주세요:\n"
    "등록 [이름] [사번]\n\n"
    "예시: 등록 홍길동 20160637"
)

_REGISTER_PATTERN = re.compile(
    r"^등록\s+([가-힣]{2,5})\s+(\d{6,10})$"
)


# ─── 카카오톡 연동 ─────────────────────────────────────────

def _shorten_brand(name: str) -> str:
    """브랜드명 간소화: (주)아진그룹_수암골쪽갈비마을 본사 → 수암골쪽갈비마을"""
    s = re.sub(r'^\([^)]*\)[^_]*_', '', name)
    s = re.sub(r'\s*본사\s*$', '', s)
    return s.strip() or name


def _to_kakao_text(answer: str) -> str:
    """마크다운 답변 → 카카오톡 카드형 텍스트 변환 (1000자 제한 준수)"""
    if not answer:
        return answer

    # "💡 인사이트:" 접두어 제거
    answer = re.sub(r'💡\s*인사이트\s*:?\s*', '💡 ', answer)

    _MAX_BRANDS = 7  # 상세 표시 최대 브랜드 수
    _MAX_LEN = 950   # 카카오 simpleText 여유 한도

    # 마크다운 테이블이 없으면 기본 마크다운 문법만 제거
    has_table = bool(re.search(r'^\|.+\|', answer, re.MULTILINE))
    if not has_table:
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', answer)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        if len(text) > _MAX_LEN:
            text = text[:_MAX_LEN - 3] + '\n...'
        return text.strip()

    lines = answer.split('\n')
    out: list[str] = []
    in_table = False
    headers: list[str] = []
    brand_count = 0
    skipped_brands: list[tuple[str, str]] = []  # (name, total)

    for line in lines:
        stripped = line.strip()

        # 테이블 구분선 스킵
        if re.match(r'^\|\s*[-:]+\s*\|', stripped):
            continue

        # 테이블 헤더 행
        if stripped.startswith('|') and '|' in stripped[1:] and not in_table:
            headers = [h.strip() for h in stripped.split('|')[1:-1]]
            in_table = True
            continue

        # 테이블 데이터 행 → 카드 블록
        if stripped.startswith('|') and in_table:
            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            if len(cells) >= 2 and len(headers) >= 2:
                brand = _shorten_brand(cells[0])

                # ── 매출값 추출: 합계 > 매출(억) > GP(억) ──
                total_val = ""
                if '합계' in headers:
                    ti = headers.index('합계')
                    if ti < len(cells):
                        total_val = cells[ti]
                elif '매출(억)' in headers:
                    ti = headers.index('매출(억)')
                    if ti < len(cells):
                        total_val = cells[ti]
                elif 'GP(억)' in headers:
                    ti = headers.index('GP(억)')
                    if ti < len(cells):
                        total_val = cells[ti]

                # ── 비중/GP율 추출 ──
                pct_val = ""
                for pct_h in ('비중', 'GP율'):
                    if pct_h in headers:
                        pi = headers.index(pct_h)
                        if pi < len(cells) and cells[pi] not in ('-', ''):
                            pct_val = cells[pi] if pct_h == '비중' else f"GP {cells[pi]}"
                            break

                brand_count += 1

                # 최대 브랜드 수 초과 → 요약 리스트에 저장
                if brand_count > _MAX_BRANDS:
                    skipped_brands.append((brand, total_val))
                    continue

                # ── 메인 라인: 값 + 비중/GP ──
                if pct_val:
                    out.append(f'■ {brand} ({total_val}백만, {pct_val})')
                else:
                    out.append(f'■ {brand} ({total_val}백만)')

                # 월별 추이
                month_parts = []
                for hi, h in enumerate(headers):
                    if re.match(r'\d+월$', h) and hi < len(cells) and cells[hi] != '-':
                        month_parts.append(f'{h} {cells[hi]}')
                if month_parts:
                    out.append(f'  {" → ".join(month_parts)}억')

                # 가맹점/점당매출 + 범용 extras
                extras = []
                if '가맹점' in headers:
                    gi = headers.index('가맹점')
                    if gi < len(cells) and cells[gi] not in ('-', ''):
                        extras.append(f'가맹점 {cells[gi]}개')
                if '점당매출' in headers:
                    pi = headers.index('점당매출')
                    if pi < len(cells) and cells[pi] not in ('-', ''):
                        extras.append(f'점당 {cells[pi]}')
                for extra_h, extra_sfx in [('품목수','품목'), ('사원수','명'),
                                            ('거래처','곳'), ('브랜드','곳'),
                                            ('거래처수','곳'), ('브랜드수','곳')]:
                    if extra_h in headers:
                        ei = headers.index(extra_h)
                        if ei > 0 and ei < len(cells) and cells[ei] not in ('-', ''):
                            extras.append(f'{extra_h} {cells[ei]}{extra_sfx}')
                if extras:
                    out.append(f'  {" | ".join(extras)}')
                out.append('')
            continue

        # 테이블 종료
        if in_table and not stripped.startswith('|'):
            in_table = False
            # 생략된 브랜드 요약 추가
            if skipped_brands:
                summary_parts = [f'{n}({v}백만)' for n, v in skipped_brands]
                out.append(f'외 {len(skipped_brands)}개: {", ".join(summary_parts)}')
                out.append('')
                skipped_brands = []

        # 요약 행: | 구분 → 줄 분리 (모바일 가독성)
        if '|' in stripped and ('총' in stripped or '신규' in stripped):
            for part in stripped.split('|'):
                p = part.strip()
                if p:
                    out.append(p)
            continue

        # 인사이트 행: 브랜드명 간소화
        if stripped.startswith('- ') and ': ' in stripped:
            rest = stripped[2:]
            ci = rest.index(': ')
            short = _shorten_brand(rest[:ci])
            out.append(f'- {short}: {rest[ci + 2:]}')
            continue

        out.append(stripped if stripped else '')

    # 테이블이 파일 끝까지면 닫기
    if in_table:
        if skipped_brands:
            summary_parts = [f'{n}({v}백만)' for n, v in skipped_brands]
            out.append(f'외 {len(skipped_brands)}개: {", ".join(summary_parts)}')
            out.append('')
        # 테이블 끝 처리 완료

    result = '\n'.join(out)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()

    # 최종 안전장치: 1000자 초과 시 스마트 잘림
    if len(result) > _MAX_LEN:
        # 마지막 완전한 블록(■) 또는 줄바꿈에서 자르기
        cut = result[:_MAX_LEN].rfind('\n\n')
        if cut < 500:
            cut = result[:_MAX_LEN].rfind('\n')
        if cut < 300:
            cut = _MAX_LEN - 3
        result = result[:cut].rstrip() + '\n...'
    return result


def _auto_backtick_korean(sql: str) -> str:
    """SQL 내 백틱 없는 한글 식별자에 자동으로 백틱을 감싼다.
    이미 백틱으로 감싸진 부분, 문자열 리터럴('...' 내부)은 건드리지 않는다."""
    # 문자열 리터럴과 백틱 구간을 먼저 보호
    protected: list[tuple[int, int]] = []
    for m in re.finditer(r"'[^']*'", sql):
        protected.append((m.start(), m.end()))
    for m in re.finditer(r'`[^`]*`', sql):
        protected.append((m.start(), m.end()))

    def _in_protected(start: int, end: int) -> bool:
        for ps, pe in protected:
            if start >= ps and end <= pe:
                return True
        return False

    # 한글이 포함된 식별자 패턴: 연속된 (한글|영문|숫자|_) 중 한글 1자 이상 포함
    pattern = re.compile(r'(?<![`\'\w])([\w가-힣]*[가-힣][\w가-힣]*)(?![`\w])')
    parts = []
    last = 0
    for m in pattern.finditer(sql):
        if _in_protected(m.start(), m.end()):
            continue
        parts.append(sql[last:m.start()])
        parts.append(f'`{m.group(0)}`')
        last = m.end()
    parts.append(sql[last:])
    return ''.join(parts)


def _format_dify_rows(rows: list[dict], query: str = "") -> str:
    """Dify SQL 결과를 카카오톡용 깔끔한 텍스트로 변환한다.

    - 1행: 단일 수치 → "외식식재사업부 전체 매출: 123.45억원"
    - 다행: 랭킹/리스트 → 번호 매기기 "1. 브랜드 — 12.5억원"
    - 최대 20행, 950자 제한
    """
    if not rows:
        return "조회 결과가 없습니다."

    cols = list(rows[0].keys())

    # ── 단일 행 + 단일/소수 컬럼: 요약형 ──
    if len(rows) == 1 and len(cols) <= 3:
        parts = []
        for c in cols:
            v = rows[0][c]
            if v is None:
                v = "-"
            parts.append(f"{c}: {v}")
        return "📊 " + " / ".join(parts)

    # ── 다중 행: 번호 매기기 리스트 ──
    # 첫 번째 컬럼 = 라벨(이름/브랜드 등), 나머지 = 수치
    label_col = cols[0]
    value_cols = cols[1:]

    lines = []
    # 제목 추출 시도
    title_parts = []
    if query:
        title_parts.append("📊 조회 결과")
    lines.append(title_parts[0] if title_parts else "📊 조회 결과")
    lines.append("")

    for i, row in enumerate(rows[:20], 1):
        label = str(row.get(label_col, ""))
        vals = []
        for vc in value_cols:
            v = row.get(vc, "")
            if v is None:
                v = "-"
            # 컬럼명에서 단위 힌트 추출 (억, 원, % 등)
            unit = ""
            vc_lower = str(vc)
            if "억" in vc_lower:
                unit = "백만원"
            elif "원" in vc_lower and "억" not in vc_lower:
                unit = "원"
            elif "%" in vc_lower or "율" in vc_lower or "비중" in vc_lower:
                unit = "%"
            elif "건" in vc_lower or "수" in vc_lower:
                unit = "건"
            vals.append(f"{v}{unit}")
        val_str = " / ".join(vals)
        lines.append(f"{i}. {label} — {val_str}")

    if len(rows) > 20:
        lines.append(f"\n... 외 {len(rows) - 20}건")

    result = "\n".join(lines)
    if len(result) > 950:
        result = result[:947] + "\n..."
    return result


def _http_post_json(url: str, data: dict,
                    headers: dict | None = None, timeout: int = 120) -> dict:
    """stdlib urllib 기반 JSON POST"""
    body = json_mod.dumps(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json_mod.loads(resp.read())


# ─── 메인 메뉴 QuickReplies ────────────────────────────────────
_MAIN_MENU_QR = [
    {"label": "📊 매출 실적",    "action": "message", "messageText": "매출 실적 메뉴"},
    {"label": "💰 수익성 분석",  "action": "message", "messageText": "수익성 분석 메뉴"},
    {"label": "📦 미출고 현황",  "action": "message", "messageText": "미출고 현황"},
    {"label": "💬 도움말",       "action": "message", "messageText": "도움말"},
]
_UNSHIPPED_FOLLOW_QR = [
    {"label": "📅 어제 현황",    "action": "message", "messageText": "어제 미출고 알려줘"},
    {"label": "🏠 메인 메뉴",    "action": "message", "messageText": "메뉴"},
]
_SALES_FOLLOW_QR = [
    {"label": "📊 매출 메뉴",    "action": "message", "messageText": "매출 실적 메뉴"},
    {"label": "🏠 메인 메뉴",    "action": "message", "messageText": "메뉴"},
]
_REASON_QR = [
    {"label": "📈 증가사유 확인", "action": "message", "messageText": "증가사유 알려줘"},
    {"label": "🏠 메인 메뉴",    "action": "message", "messageText": "메뉴"},
]


def _send_kakao_callback(callback_url: str, text: str, label: str = "콜백"):
    """카카오 콜백 전송 (실패해도 로그만) — 범용"""
    try:
        logger.info(f"[{label}] 전송 시도: url={callback_url[:80]}")
        payload = {
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": text}}]}
        }
        body = json_mod.dumps(payload).encode('utf-8')
        req = urllib.request.Request(callback_url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            resp_body = resp.read().decode('utf-8', errors='replace')[:500]
            logger.info(
                f"[{label}] 전송 완료: "
                f"{len(text)}자, HTTP {status}, 응답={resp_body}"
            )
    except urllib.error.HTTPError as he:
        err_body = he.read().decode('utf-8', errors='replace')[:500] if he.fp else ''
        logger.error(
            f"[{label}] 전송 HTTP 에러: "
            f"HTTP {he.code}, 응답={err_body}"
        )
    except Exception as cb_err:
        logger.error(f"[{label}] 전송 실패: {cb_err}")


def _send_kakao_callback_qr(
    callback_url: str, text: str, quickreplies: list, label: str = "콜백"
):
    """카카오 콜백 전송 + QuickReply 버튼 포함"""
    try:
        payload = {
            "version": "2.0",
            "template": {
                "outputs": [{"simpleText": {"text": text}}],
                "quickReplies": quickreplies,
            },
        }
        body = json_mod.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(callback_url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json; charset=utf-8')
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"[{label}+QR] 전송 완료: {len(text)}자, HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[{label}+QR] 전송 실패: {e}")
        _send_kakao_callback(callback_url, text, label)  # fallback


def _register_and_callback(
    name: str, emp_code: str, user_id: str, callback_url: str
):
    """백그라운드: DB 인증 → 등록 → 카카오 콜백 전송"""
    logger.info(f"[인증콜백] 시작: name={name}, emp_code={emp_code}")
    try:
        db_info = _verify_employee(name, emp_code)
        if db_info:
            msg = _register_user(user_id, name, emp_code, db_info)
            logger.info(f"[인증콜백] 등록 성공: {db_info.get('영업사원명')}")
            _send_kakao_callback_qr(callback_url, msg, _MAIN_MENU_QR, "인증콜백")
        else:
            logger.warning(f"[인증콜백] 등록 실패: name={name}, emp_code={emp_code}")
            msg = (
                "❌ 인증에 실패했습니다.\n\n"
                "이름과 사번을 다시 확인해주세요.\n"
                "(외식식재사업부 소속 사원만 등록 가능합니다)\n\n"
                "형식: 등록 [이름] [사번]\n"
                "예시: 등록 홍길동 20160637"
            )
            _send_kakao_callback(callback_url, msg, "인증콜백")
    except Exception as e:
        logger.error(f"[인증콜백] 오류: {e}")
        _send_kakao_callback(
            callback_url,
            "⚠️ 인증 처리 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.",
            "인증콜백",
        )


# ─── 플랜트별 매출 분해 ─────────────────────────────────────
# level_label → (DB 컬럼명, messageText 코드)
_PLANT_LEVEL_MAP: dict[str, tuple[str, str]] = {
    "브랜드(ZC)":  ("ZC본부명", "ZC"),
    "거래처(ZA)":  ("ZA거래처명", "ZA"),
    "단일 거래처": ("거래처명",   "ZT"),
}

# 사용자가 입력할 수 있는 플랜트 키워드 → DB LIKE 검색어 매핑
# 예) "대구센터" → "대구"  (뒤에 센터/공장/물류 붙어도 LIKE '%대구%' 로 검색)
_PLANT_KEYWORDS: list[str] = [
    "서이천",  # 앞 순서 중요: '이천'보다 먼저 매칭
    "대구", "양산", "호남", "이천", "아산", "충주", "삼조", "위탁", "시화",
    "화성",  # 화성(식재), 화성(외식_3배치), 화성(키즈) 포함
]

# 사용자 별칭 → DB LIKE 검색어 (정확 매핑 우선 적용)
# 예) "HL센터" / "H/L" → "위탁",  "아라센터" / "아라로지스" → "서이천"
_PLANT_ALIASES: dict[str, str] = {
    # 위탁2센터(H/L) 별칭
    "hl":       "위탁",
    "h/l":      "위탁",
    "hl센터":   "위탁",
    "h/l센터":  "위탁",
    "위탁센터": "위탁",
    # 서이천센터 별칭
    "아라":       "서이천",
    "아라센터":   "서이천",
    "아라로지스": "서이천",
    "아라물류":   "서이천",
}

def _extract_plant_keyword(word: str) -> str | None:
    """단어에서 플랜트 키워드 추출.
    1) 별칭 사전 우선 확인 (예: 'HL센터' → '위탁', '아라센터' → '서이천')
    2) _PLANT_KEYWORDS 접두어 매칭 (예: '대구센터' → '대구')
    """
    # 별칭 사전 (소문자 정규화)
    lower = word.lower().strip()
    lower_clean = re.sub(r'(센터|공장|물류|창고)$', '', lower).strip()
    if lower in _PLANT_ALIASES:
        return _PLANT_ALIASES[lower]
    if lower_clean in _PLANT_ALIASES:
        return _PLANT_ALIASES[lower_clean]
    # 일반 키워드 접두어 매칭 (센터/공장 등 suffix 제거 후)
    cleaned = re.sub(r'(센터|공장|물류|창고)$', '', word).strip()
    for kw in _PLANT_KEYWORDS:
        if cleaned == kw or cleaned.startswith(kw):
            return kw
    return None


def _build_plant_breakdown_card(brand_name: str, level_label: str, ym: str) -> str:
    """브랜드/거래처의 플랜트별 매출 분해 카드 반환."""
    col_info = _PLANT_LEVEL_MAP.get(level_label)
    if not col_info:
        return "플랜트별 집계가 지원되지 않는 집계단위입니다."
    col_name, _ = col_info
    mo = int(ym[4:6])
    rows = _safe_query(f"""
        SELECT `플랜트`, `플랜트명`,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `{col_name}` = '{brand_name}'
          AND `년월` = '{ym}'
        GROUP BY `플랜트`, `플랜트명`
        ORDER BY sales DESC
    """)
    rows = [r for r in rows if float(r.get("sales", 0)) > 0]
    if not rows:
        return f"📦 {brand_name}\n{mo}월 플랜트별 데이터가 없습니다."
    total = sum(float(r["sales"]) for r in rows)
    lines = [f"📦 {brand_name}", f"{mo}월 플랜트별 매출액\n"]
    for r in rows:
        val = float(r["sales"])
        pct = round(val / total * 100) if total > 0 else 0
        plant_label = r.get("플랜트명") or r.get("플랜트", "?")
        lines.append(f"• {plant_label}")
        lines.append(f"  {_format_value(val)}백만원 ({pct}%)")
    lines.append("─" * 16)
    lines.append(f"합계  {_format_value(total)}백만원")
    lines.append(f"📌 집계단위: {level_label}")
    return "\n".join(lines)


def _build_brand_plant_sales_card(brand_keyword: str, plant_keyword: str, ym: str) -> str:
    """'브랜드명 대구센터 매출액' 형태 쿼리 처리.
    brand_keyword: ZC본부명 LIKE 검색어 (예: '닭동가리')
    plant_keyword: 플랜트명 LIKE 검색어 (예: '대구')
    ym: 'YYYYMM'
    """
    mo = int(ym[4:6])
    # ZC 브랜드 + 플랜트 조합 조회
    rows = _safe_query(f"""
        SELECT `ZC본부명`, `플랜트명`,
               ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
        FROM {T_MAIN}
        WHERE `사업부명` = '외식식재사업부'
          AND `ZC본부명` LIKE '%{brand_keyword}%'
          AND TRIM(LEADING '0' FROM `ZC본부`) LIKE '8%'
          AND `플랜트명` LIKE '%{plant_keyword}%'
          AND `년월` = '{ym}'
        GROUP BY `ZC본부명`, `플랜트명`
        ORDER BY sales DESC
    """)
    rows = [r for r in rows if float(r.get("sales", 0)) > 0]

    # ZC 없으면 ZA / 거래처명 으로 fallback
    if not rows:
        rows = _safe_query(f"""
            SELECT `ZA거래처명` AS ZC본부명, `플랜트명`,
                   ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 4) AS sales
            FROM {T_MAIN}
            WHERE `사업부명` = '외식식재사업부'
              AND `ZA거래처명` LIKE '%{brand_keyword}%'
              AND `플랜트명` LIKE '%{plant_keyword}%'
              AND `년월` = '{ym}'
            GROUP BY `ZA거래처명`, `플랜트명`
            ORDER BY sales DESC
        """)
        rows = [r for r in rows if float(r.get("sales", 0)) > 0]

    if not rows:
        return (
            f"'{brand_keyword}' 브랜드의 {plant_keyword} 플랜트\n"
            f"{mo}월 매출 데이터가 없습니다.\n"
            f"• 브랜드명 또는 센터명을 확인해주세요."
        )

    # 브랜드가 1개인 경우
    brands = list({r["ZC본부명"] for r in rows})
    if len(brands) > 1:
        brand_list = "\n".join(f"  {i+1}. {b}" for i, b in enumerate(brands[:5]))
        return (
            f"'{brand_keyword}' 브랜드가 여러 개 있습니다:\n{brand_list}\n\n"
            f"정확한 브랜드명으로 다시 입력해주세요."
        )

    brand_name = brands[0]
    total = sum(float(r["sales"]) for r in rows)
    plant_names = ", ".join(r.get("플랜트명", "?") for r in rows)
    lines = [
        f"📦 {brand_name}",
        f"{plant_names} {mo}월 매출액\n",
    ]
    for r in rows:
        val = float(r["sales"])
        lines.append(f"{_format_value(val)}백만원")
    if len(rows) > 1:
        lines.append("─" * 16)
        lines.append(f"합계  {_format_value(total)}백만원")
    return "\n".join(lines)


def _brand_send(callback_url: str, card: str, level_label: str,
                brand_name: str, ym: str, label: str = "브랜드매출"):
    """브랜드 카드 전송. ZC/ZA/단일 거래처 레벨이면 📦 플랜트별 버튼 추가."""
    if level_label in _PLANT_LEVEL_MAP:
        level_code = _PLANT_LEVEL_MAP[level_label][1]
        plant_btn = {
            "label": "📦 플랜트별 매출액",
            "action": "message",
            "messageText": f"플랜트별 {level_code} {brand_name} {ym}",
        }
        qr = _SALES_FOLLOW_QR + [plant_btn]
    else:
        qr = _SALES_FOLLOW_QR
    _send_kakao_callback_qr(callback_url, card, qr, label)


def _bg_candidate_query(
    matched_cand: str, cand_level: str, cand_ym: str, cand_mo: int,
    callback_url: str,
):
    """백그라운드: pending_candidates 후보 선택 후 DB 조회 → 콜백 전송"""
    try:
        if cand_level == "단일 거래처":
            rows = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `거래처명` = '{matched_cand}'
                  AND `년월` = '{cand_ym}'
            """)
        elif cand_level == "거래처(ZA)":
            rows = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `ZA거래처명` = '{matched_cand}'
                  AND `년월` = '{cand_ym}'
            """)
        else:
            rows = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `ZC본부명` = '{matched_cand}'
                  AND `년월` = '{cand_ym}'
            """)
        sales = float(rows[0]["sales"]) if rows else 0.0
        cur_ym = time.strftime("%Y%m")
        if cand_ym == cur_ym:
            try:
                import datetime as _dt_bg
                card = _build_brand_forecast_card(matched_cand, sales, cand_ym, _dt_bg.date.today(), level_label=cand_level)
                card += f"\n📌 집계단위: {cand_level}"
            except Exception:
                card = (
                    f"{matched_cand}의 {cand_mo}월 매출액은 "
                    f"{_format_value(sales)}백만원입니다."
                    f"\n📌 집계단위: {cand_level}"
                )
        else:
            card = (
                f"{matched_cand}의 {cand_mo}월 매출액은 "
                f"{_format_value(sales)}백만원입니다."
                f"\n📌 집계단위: {cand_level}"
            )
        if cand_level == "점포합산":
            _agg_kw = matched_cand.split(' (전체')[0]
            _sugg, _sugg_qr = _build_store_agg_suggestion(_agg_kw, cand_ym)
            if _sugg:
                card += _sugg
                _send_kakao_callback_qr(callback_url, card, _sugg_qr, "브랜드매출")
            else:
                _send_kakao_callback_qr(callback_url, card, _SALES_FOLLOW_QR, "브랜드매출")
        else:
            _brand_send(callback_url, card, cand_level, matched_cand, cand_ym)
    except Exception as e:
        logger.error(f"[bg_candidate] 오류: {e}")
        _send_kakao_callback(callback_url, "⚠️ 매출 조회 중 오류가 발생했습니다.", "브랜드매출")


def _bg_confirm_query(
    p_name: str, p_level: str, p_ym: str, p_mo: int,
    callback_url: str,
):
    """백그라운드: pending_confirm 예 응답 후 DB 조회 → 콜백 전송"""
    try:
        if p_level == "단일 거래처":
            rows = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `거래처명` = '{p_name}'
                  AND `년월` = '{p_ym}'
            """)
        elif p_level == "거래처(ZA)":
            rows = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `ZA거래처명` = '{p_name}'
                  AND `년월` = '{p_ym}'
            """)
        else:
            rows = _safe_query(f"""
                SELECT ROUND(COALESCE(SUM(`매출액`), 0) / 1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `ZC본부명` = '{p_name}'
                  AND `년월` = '{p_ym}'
            """)
        sales = float(rows[0]["sales"]) if rows else 0.0
        cur_ym = time.strftime("%Y%m")
        if p_ym == cur_ym:
            try:
                import datetime as _dt_bg
                card = _build_brand_forecast_card(p_name, sales, p_ym, _dt_bg.date.today(), level_label=p_level)
                card += f"\n📌 집계단위: {p_level}"
            except Exception:
                card = (
                    f"{p_name}의 {p_mo}월 매출액은 "
                    f"{_format_value(sales)}백만원입니다."
                    f"\n📌 집계단위: {p_level}"
                )
        else:
            card = (
                f"{p_name}의 {p_mo}월 매출액은 "
                f"{_format_value(sales)}백만원입니다."
                f"\n📌 집계단위: {p_level}"
            )
        if p_level == "점포합산":
            _agg_kw = p_name.split(' (전체')[0]
            _sugg, _sugg_qr = _build_store_agg_suggestion(_agg_kw, p_ym)
            if _sugg:
                card += _sugg
                _send_kakao_callback_qr(callback_url, card, _sugg_qr, "브랜드매출")
            else:
                _send_kakao_callback_qr(callback_url, card, _SALES_FOLLOW_QR, "브랜드매출")
        else:
            _brand_send(callback_url, card, p_level, p_name, p_ym)
    except Exception as e:
        logger.error(f"[bg_confirm] 오류: {e}")
        _send_kakao_callback(callback_url, "⚠️ 매출 조회 중 오류가 발생했습니다.", "브랜드매출")


# ─── 개인형 세부내역 ────────────────────────────────────────
_user_last_sp: dict[str, str] = {}    # user_id → 최근 조회 영업사원명(공백제거)
_user_last_sales: dict[str, dict] = {}  # user_id → {target_key, target_name, yearmonth}
_user_last_reason: dict[str, str] = {}  # user_id → 전년대비 섹션 텍스트 (2번째 버블용)
_user_pending_confirm: dict[str, dict] = {}    # user_id → {exact_name, month_num, yearmonth, level_label}
_user_pending_candidates: dict[str, dict] = {} # user_id → {이름 → level_label}

_PERSONAL_DETAIL_PATTERN = re.compile(
    r'개인형\s*(세부|내역|상세|세부내역|디테일|목록)',
    re.IGNORECASE,
)

_SALES_REASON_PATTERN = re.compile(
    r'(증가|감소)?\s*(사유|이유|원인|왜\s+|주요\s*브랜드|브랜드\s*내역)',
    re.IGNORECASE,
)

# 사업부/지점 월별 전체매출 직접 처리 패턴 (Dify 바이패스)
# 예: "외식식재사업부 3월 매출액", "외식1팀 3월 전체매출"
_MONTHLY_TOTAL_PATTERN = re.compile(
    r'([가-힣A-Za-z0-9]+(?:사업부|지점))\s*(?:의)?\s*(\d{1,2})월'
    r'|(\d{1,2})월\s*(?:[가-힣A-Za-z0-9\s]*?)\s*([가-힣A-Za-z0-9]+(?:사업부|지점))',
    re.IGNORECASE,
)

# 브랜드명 단독 월 매출 조회 패턴 (Dify 바이패스) — 사업부/지점 미포함 브랜드명
# 예: "샐러디는 2월에 매출", "위드저니 3월 실적"
_BRAND_SALES_PATTERN = re.compile(
    # 브랜드/거래처명: 앞 괄호(직·폐업 등) 허용, 뒤 괄호(점명·본사 등) 허용, & ＆ 포함
    r'((?:\([가-힣A-Za-z0-9&＆\s]+\))?[가-힣A-Za-z0-9&＆]+(?:\([가-힣A-Za-z0-9&＆\s]+\))?)'
    r'(?:는|은|의)?\s*(\d{1,2})월\s*(?:에\s*)?(?:매출|실적)'
    r'|(\d{1,2})월\s*(?:[가-힣A-Za-z0-9&＆\s]*?)'
    r'((?:\([가-힣A-Za-z0-9&＆\s]+\))?[가-힣A-Za-z0-9&＆]+(?:\([가-힣A-Za-z0-9&＆\s]+\))?)\s*(?:매출|실적)',
    re.IGNORECASE,
)

_SALES_CTX_RE = re.compile(
    r'<<<SALES_CTX:([^|]+)\|([^|]+)\|(\d{6})>>>',
)


def _build_personal_detail(sp_compact: str) -> str:
    """개인형 세부내역 – ZA거래처 기준으로 조회 및 포맷"""
    year = time.strftime("%Y")
    rows = _safe_query(f"""
        WITH new_custs AS (
            SELECT `영업사원명`, `ZC본부`, `ZA거래처`, `ZA거래처명`
            FROM {T_MAIN}
            WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp_compact}%'
              AND `사업부명` = '외식식재사업부'
            GROUP BY `영업사원명`, `ZC본부`, `ZA거래처`, `ZA거래처명`
            HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
        )
        SELECT nc.`ZA거래처명`, t.`년월`,
               ROUND(COALESCE(SUM(t.`매출액`), 0) / 1000000, 2) AS `신규매출액_억원`
        FROM {T_MAIN} t
        JOIN new_custs nc ON t.`영업사원명` = nc.`영업사원명`
                         AND t.`ZA거래처` = nc.`ZA거래처`
        WHERE t.`년도` = '{year}'
          AND t.`사업부명` = '외식식재사업부'
          AND TRIM(LEADING '0' FROM nc.`ZC본부`) NOT LIKE '8%'
        GROUP BY nc.`ZA거래처명`, t.`년월`
        ORDER BY nc.`ZA거래처명`, t.`년월`
    """, raw=True)

    if not rows:
        return "개인형 거래처 데이터가 없습니다."

    # 월별 정리
    month_set = sorted({str(r.get("년월", "")) for r in rows if r.get("년월")})
    mcv: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        c = str(r.get("ZA거래처명", ""))
        m = str(r.get("년월", ""))
        try:
            v = float(r.get("신규매출액_억원", 0))
        except (TypeError, ValueError):
            v = 0.0
        mcv[c][m] = mcv[c].get(m, 0.0) + v

    custs = sorted(mcv.keys(), key=lambda c: sum(mcv[c].values()), reverse=True)
    grand = sum(sum(mcv[c].values()) for c in custs)

    lines = [f"📋 개인형 세부내역 ({len(custs)}개 거래처)", ""]

    month_labels = []
    for m in month_set:
        mn = int(m[4:6]) if len(m) >= 6 else m
        month_labels.append(f"{mn}월")

    cols = ["거래처명"] + month_labels + ["합계"]
    lines.append("| " + " | ".join(cols) + " |")
    sep = ["---"] + ["---:"] * (len(cols) - 1)
    lines.append("| " + " | ".join(sep) + " |")

    for c in custs:
        vals = []
        for m in month_set:
            v = mcv[c].get(m)
            vals.append("-" if v is None else _format_value(v))
        total = _format_value(sum(mcv[c].values()))
        lines.append("| " + " | ".join([c] + vals + [total]) + " |")

    lines.append("")
    lines.append(f"개인형 합계: {_format_value(grand)}백만원")
    return "\n".join(lines)


def _call_dify_and_callback(query: str, user_id: str, callback_url: str):
    """백그라운드: Dify 호출 → 카드형 변환 → 카카오 콜백 전송
    카카오 콜백 타임아웃은 약 1분이므로 Dify 호출을 50초로 제한.
    """
    t0 = time.time()
    logger.info(f"[콜백] 시작: user={user_id}, query={query[:80]}")

    # ── 플랜트별 매출 조회 (QR 버튼 messageText: "플랜트별 ZC 브랜드명 YYYYMM") ──
    _plant_m = re.match(r'^플랜트별\s+(ZC|ZA|ZT)\s+(.+)\s+(\d{6})$', query.strip())
    if _plant_m:
        _pl_code, _pl_brand, _pl_ym = _plant_m.group(1), _plant_m.group(2).strip(), _plant_m.group(3)
        _level_reverse = {"ZC": "브랜드(ZC)", "ZA": "거래처(ZA)", "ZT": "단일 거래처"}
        _pl_level = _level_reverse.get(_pl_code, "브랜드(ZC)")
        card = _build_plant_breakdown_card(_pl_brand, _pl_level, _pl_ym)
        _send_kakao_callback_qr(callback_url, card, _SALES_FOLLOW_QR, "브랜드매출")
        return

    # ── 브랜드+플랜트 복합 조회: "닭동가리 대구센터 매출액" ──
    if '매출' in query:
        # 쿼리에서 플랜트 키워드 단어를 탐지
        _q_words = query.strip().split()
        _found_plant_kw: str | None = None
        _found_plant_word_idx: int = -1
        for _wi, _word in enumerate(_q_words):
            _pkw_found = _extract_plant_keyword(_word)
            if _pkw_found:
                _found_plant_kw = _pkw_found
                _found_plant_word_idx = _wi
                break
        if _found_plant_kw and _found_plant_word_idx > 0:
            # 플랜트 키워드 앞 단어들 = 브랜드 키워드
            _brand_kw_words = _q_words[:_found_plant_word_idx]
            # 뒤 단어에서 '매출', '실적' 등 제거
            _brand_kw_words = [w for w in _brand_kw_words
                               if not re.match(r'^(매출|실적|액|알려|줘|주세).*$', w)]
            _brand_kw = ' '.join(_brand_kw_words).strip()
            _brand_kw = re.sub(r'[는은의이가을를로에서만]$', '', _brand_kw).strip()
            # 년월 추출
            _ym_bp = time.strftime("%Y%m")
            _mo_bp_m = re.search(r'(\d{1,2})월', query)
            _yr_bp_m = re.search(r'(\d{2,4})년', query)
            if _mo_bp_m:
                _mo_bp = int(_mo_bp_m.group(1))
                _yr_bp = int(_yr_bp_m.group(1)) if _yr_bp_m else int(time.strftime("%Y"))
                if _yr_bp < 100:
                    _yr_bp += 2000
                _ym_bp = f"{_yr_bp}{_mo_bp:02d}"
            if _brand_kw and len(_brand_kw) >= 2:
                logger.info(f"[콜백] 브랜드+플랜트 조회: brand={_brand_kw}, plant={_found_plant_kw}, ym={_ym_bp}")
                card = _build_brand_plant_sales_card(_brand_kw, _found_plant_kw, _ym_bp)
                _send_kakao_callback_qr(callback_url, card, _SALES_FOLLOW_QR, "브랜드매출")
                return

    # 매출 증가사유 추가질문 처리
    if _SALES_REASON_PATTERN.search(query):
        ctx = _user_last_sales.get(user_id)
        if ctx:
            logger.info(f"[콜백] 증가사유 요청: ctx={ctx}")
            try:
                detail = _fetch_sales_reason(ctx["target_key"], ctx["target_name"], ctx["yearmonth"],
                                            forecast_total=ctx.get("forecast_total"))
                # 전월 대비 / 전년 대비 분리
                _split_marker = "\n【전년("
                if _split_marker in detail:
                    _idx = detail.index(_split_marker)
                    part1 = detail[:_idx].rstrip()
                    part2 = detail[_idx:].lstrip()
                    _user_last_reason[user_id] = part2
                    _yoy_qr = [
                        {"label": "📊 전년대비 보기", "action": "message", "messageText": "전년대비 보기"},
                        {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"},
                    ]
                    _send_kakao_callback_qr(callback_url, part1, _yoy_qr, "매출증가사유")
                else:
                    _user_last_reason[user_id] = ""
                    _send_kakao_callback_qr(callback_url, detail[:1900], _REASON_QR, "매출증가사유")
            except Exception as e:
                logger.error(f"[콜백] 증가사유 조회 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 증가사유 조회 중 오류가 발생했습니다.", "매출증가사유")
            return
        else:
            _send_kakao_callback(
                callback_url,
                "먼저 매출 조회를 해주세요.\n(예: 외식식재사업부 3월 매출액 알려줘)",
                "매출증가사유",
            )
            return

    # 전년대비 보기 (증가사유 2번째 버블)
    if re.search(r'전년\s*대비\s*보기', query):
        part2 = _user_last_reason.get(user_id)
        if part2:
            _send_kakao_callback_qr(callback_url, part2, _REASON_QR, "매출증가사유전년")
        else:
            _send_kakao_callback(callback_url, "먼저 증가사유를 조회해주세요.", "매출증가사유전년")
        return

    # 개인형 세부내역 요청 처리
    if _PERSONAL_DETAIL_PATTERN.search(query):
        sp = _user_last_sp.get(user_id)
        if sp:
            logger.info(f"[콜백] 개인형 세부내역 요청: sp={sp}")
            try:
                detail = _build_personal_detail(sp)
                card = _to_kakao_text(detail)
                _send_kakao_callback(callback_url, card, "개인형세부")
            except Exception as e:
                logger.error(f"[콜백] 개인형 세부내역 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 개인형 세부내역 조회 중 오류가 발생했습니다.", "개인형세부")
            return
        else:
            _send_kakao_callback(
                callback_url,
                "먼저 영업사원의 신규매출을 조회해주세요.\n(예: 조윤식 신규매출액 알려줘)",
                "개인형세부",
            )
            return

    # ─── 품목/자재그룹별 매출 (Dify 바이패스) ─────────────────────
    if re.search(r'(?:품목|자재|상품)\s*(?:별|그룹)', query) and \
       re.search(r'매출|실적|순위|TOP|탑|랭킹', query, re.IGNORECASE):
        month_num, yearmonth = _extract_month_year(query)
        target_key, target_name = _resolve_org_context(query)
        logger.info(f"[콜백] 품목별매출: target={target_name}, ym={yearmonth}")
        try:
            rows = _fetch_product_ranking(target_key, target_name, yearmonth)
            text = _build_product_ranking_markdown(rows, target_name, yearmonth)
            _user_last_sales[user_id] = {"target_key": target_key, "target_name": target_name, "yearmonth": yearmonth}
            _send_kakao_callback(callback_url, _to_kakao_text(text), "품목매출")
        except Exception as e:
            logger.error(f"[콜백] 품목별매출 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 품목별 매출 조회 중 오류가 발생했습니다.", "품목매출")
        return

    # ─── 특정 자재 검색 (Dify 바이패스) ─────────────────────
    product_search_m = re.search(
        r'(.+?)\s*(?:자재|품목)\s*(?:매출|실적|검색|조회)', query
    )
    if not product_search_m:
        product_search_m = re.search(
            r'(?:자재|품목)\s*(.+?)\s*(?:매출|실적|검색|조회)', query
        )
    if product_search_m:
        keyword = product_search_m.group(1).strip()
        keyword = re.sub(r'[의는은이가을를]$', '', keyword).strip()
        if keyword and len(keyword) >= 2:
            month_num, yearmonth = _extract_month_year(query)
            logger.info(f"[콜백] 자재검색: keyword={keyword}, ym={yearmonth}")
            try:
                rows = _fetch_product_detail(keyword, yearmonth)
                text = _build_product_detail_markdown(rows, keyword, yearmonth)
                _send_kakao_callback(callback_url, _to_kakao_text(text), "자재검색")
            except Exception as e:
                logger.error(f"[콜백] 자재검색 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 자재 검색 중 오류가 발생했습니다.", "자재검색")
            return

    # ─── 특정 팀+N월 수익성 직접 조회 ────────────────────────
    _PROFIT_TEAMS = ["외식1팀", "외식2팀", "외식3팀", "영남지점"]
    _pt_team = next((t for t in _PROFIT_TEAMS if t in query), None)
    if _pt_team and re.search(r'수익성', query):
        _, _pt_ym = _extract_month_year(query)
        if re.search(r'이번달|이번\s*달', query):
            _pt_period = "이번달"
        elif re.search(r'지난달|지난\s*달|전달|전월', query):
            _pt_period = "지난달"
        else:
            _pt_period = _pt_ym
        logger.info(f"[콜백] 팀수익성: team={_pt_team}, period={_pt_period}")
        try:
            _pt_text = _fetch_profit_branch(_pt_team, _pt_period)
            _pt_follow_qr = [{"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"}]
            _send_kakao_callback_qr(callback_url, _to_kakao_text(_pt_text + _PROFIT_NAME_GUIDE), _pt_follow_qr, "팀수익성")
        except Exception as e:
            logger.error(f"[콜백] 팀수익성 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 수익성 조회 중 오류가 발생했습니다.", "팀수익성")
        return

    # ─── 특정 브랜드/거래처명 수익성 ─────────────────────────
    _PROFIT_TEAMS_SET = {"외식1팀", "외식2팀", "외식3팀", "영남지점"}
    _biz_profit_m = re.search(
        r'(?:(\d{1,2})월|(\d{2,4})년\s*(\d{1,2})월)\s*(.{2,10}?)\s*수익성|'
        r'(.{2,10}?)\s*(?:(\d{1,2})월|(\d{2,4})년\s*(\d{1,2})월)\s*수익성',
        query
    )
    if _biz_profit_m and not any(t in query for t in _PROFIT_TEAMS_SET):
        _, _biz_ym = _extract_month_year(query)
        _biz_kw = None
        for g in _biz_profit_m.groups():
            if g and not g.isdigit() and len(g) >= 2 and not re.fullmatch(r'\d+', g):
                _biz_kw = g.strip()
                break
        if _biz_kw and re.search(r'[가-힣A-Za-z]', _biz_kw):
            _bp_user = _load_users().get(user_id, {})
            _bp_branch = _bp_user.get("team", "")
            logger.info(f"[콜백] 이름수익성: kw={_biz_kw}, ym={_biz_ym}, branch={_bp_branch}")
            try:
                _bp_text = _fetch_profit_by_name(_biz_kw, _biz_ym, _bp_branch)
                _bp_qr = [{"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"}]
                _send_kakao_callback_qr(callback_url, _to_kakao_text(_bp_text), _bp_qr, "이름수익성")
            except Exception as e:
                logger.error(f"[콜백] 이름수익성 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 수익성 조회 중 오류가 발생했습니다.", "이름수익성")
            return

    # ─── 고객 수익성 분석 (Dify 바이패스) ─────────────────────
    _profit_m = re.match(
        r'^(지점|브랜드별|거래처별)\s*수익성\s*(이번달|지난달|올해)$',
        query.strip()
    )
    if _profit_m:
        _pdim    = _profit_m.group(1)   # 지점|브랜드별|거래처별
        _pperiod = _profit_m.group(2)   # 이번달|지난달|올해
        _pu   = _load_users().get(user_id, {})
        _pbranch = _pu.get("team", "")
        if not _pbranch:
            _send_kakao_callback(callback_url, "⚠️ 지점 정보가 등록되지 않았습니다. 관리자에게 문의해주세요.", "수익성")
            return
        logger.info(f"[콜백] 수익성: dim={_pdim}, period={_pperiod}, branch={_pbranch}")
        try:
            if _pdim == "지점":
                text = _fetch_profit_branch(_pbranch, _pperiod)
            elif _pdim == "브랜드별":
                text = _fetch_profit_by_brand(_pbranch, _pperiod)
            else:
                text = _fetch_profit_by_customer(_pbranch, _pperiod)
            _profit_follow_qr = [
                {"label": "� 메인 메뉴", "action": "message", "messageText": "메뉴"},
            ]
            _send_kakao_callback_qr(callback_url, _to_kakao_text(text + _PROFIT_NAME_GUIDE), _profit_follow_qr, "수익성")
        except Exception as e:
            logger.error(f"[콜백] 수익성 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 수익성 조회 중 오류가 발생했습니다.", "수익성")
        return

    # ─── 범용상품 수익성 (Dify 바이패스) ─────────────────────
    if re.search(r'마진|이익률|수익성|GP율?|원가율', query, re.IGNORECASE):
        month_num, yearmonth = _extract_month_year(query)
        target_key, target_name = _resolve_org_context(query)
        logger.info(f"[콜백] 범용마진: target={target_name}, ym={yearmonth}")
        try:
            data = _fetch_generic_margin(target_key, target_name, yearmonth)
            text = _build_generic_margin_markdown(data, target_name, yearmonth)
            _user_last_sales[user_id] = {"target_key": target_key, "target_name": target_name, "yearmonth": yearmonth}
            _send_kakao_callback(callback_url, _to_kakao_text(text), "범용마진")
        except Exception as e:
            logger.error(f"[콜백] 범용마진 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 범용상품 수익성 조회 중 오류가 발생했습니다.", "범용마진")
        return

    # ─── 판매구역/지역별 매출 (Dify 바이패스) ─────────────────────
    region_ranking_m = re.search(
        r'(?:지역|구역|판매구역|시도)\s*(?:별)?\s*(?:매출|실적|순위|현황)', query
    )
    # 실제 행정구역 prefix 화이트리스트 — 영업사원명 오매칭 방지
    _REGION_PREFIX = r'(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청북|충청남|전북|전남|전라북|전라남|경북|경남|경상북|경상남|제주|수원|성남|고양|용인|안산|안양|화성|평택|파주|시흥|김포|광명|포천|양주|구리|의정부|남양주|하남|이천|안성|오산|의왕|여주)'
    region_keyword_m = re.search(
        rf'({_REGION_PREFIX}[가-힣]*(?:시|군|구|도)?)\s*(?:의?\s*)?(?:매출|실적)', query
    )
    if region_ranking_m or region_keyword_m:
        month_num, yearmonth = _extract_month_year(query)
        if region_keyword_m and not region_ranking_m:
            keyword = region_keyword_m.group(1)
            logger.info(f"[콜백] 지역매출: keyword={keyword}, ym={yearmonth}")
            try:
                rows = _fetch_region_sales(keyword, yearmonth)
                text = _build_region_sales_markdown(rows, keyword, yearmonth)
                _send_kakao_callback(callback_url, _to_kakao_text(text), "지역매출")
            except Exception as e:
                logger.error(f"[콜백] 지역매출 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 지역 매출 조회 중 오류가 발생했습니다.", "지역매출")
            return
        else:
            logger.info(f"[콜백] 지역별 랭킹: ym={yearmonth}")
            try:
                rows = _fetch_region_ranking(yearmonth)
                text = _build_region_sales_markdown(rows, "시도", yearmonth, is_ranking=True)
                _send_kakao_callback(callback_url, _to_kakao_text(text), "지역매출")
            except Exception as e:
                logger.error(f"[콜백] 지역별매출 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 지역별 매출 조회 중 오류가 발생했습니다.", "지역매출")
            return

    # ─── 특정 팀/지점 단독 매출 (조직별 랭킹보다 먼저 체크) ──
    _SPECIFIC_TEAMS = ["외식1팀", "외식2팀", "외식3팀", "영남지점"]
    _specific_team_m = next(
        (t for t in _SPECIFIC_TEAMS if t in query), None
    )
    # ─── 팀 미지정 영업사원별 매출 전체 랭킹 ──
    _is_sp_all = bool(re.search(
        r'영업사원별|영업담당별|사원별|담당자별|담당별', query
    )) and not _specific_team_m
    if _is_sp_all and re.search(r'매출|실적', query):
        import datetime as _dt_spa
        _today_spa = _dt_spa.date.today()
        try:
            _, ym_spa = _extract_month_year(query)
            _mo_spa = int(ym_spa[4:6])
            _cur_ym_spa = _today_spa.strftime("%Y%m")
            _period_spa = (f"{_mo_spa}월 1~{_today_spa.day}일 기준"
                           if ym_spa == _cur_ym_spa else f"{_mo_spa}월")
            sp_all_rows = _safe_query(f"""
                SELECT `영업사원명`, `부서명`,
                       ROUND(COALESCE(SUM(`매출액`),0)/1000000, 2) AS sales
                FROM {T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                  AND `년월` = '{ym_spa}'
                GROUP BY `영업사원명`, `부서명`
                ORDER BY sales DESC
            """)
            sp_all_rows = [r for r in sp_all_rows if float(r.get("sales", 0)) > 0]
            if not sp_all_rows:
                _send_kakao_callback(callback_url,
                    f"외식식재사업부 {_period_spa} 영업사원별 매출 데이터가 없습니다.", "전체SP매출")
            else:
                total_spa = sum(float(r.get("sales", 0)) for r in sp_all_rows)
                lines_spa = [f"📊 외식식재사업부 {_period_spa} 영업사원별 매출\n"]
                for i, r in enumerate(sp_all_rows, 1):
                    s = float(r.get("sales", 0))
                    team_label = f" ({r['부서명']})" if r.get("부서명") else ""
                    lines_spa.append(f"{i}. {r['영업사원명']}{team_label} — {_format_value(s)}백만원")
                lines_spa.append(f"\n합계: {_format_value(total_spa)}백만원 | {len(sp_all_rows)}명")
                _send_kakao_callback(callback_url, "\n".join(lines_spa), "전체SP매출")
        except Exception as e:
            logger.error(f"[콜백] 전체SP매출 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 영업사원별 매출 조회 중 오류가 발생했습니다.", "전체SP매출")
        return
    if _specific_team_m and re.search(r'매출|실적', query):
        import datetime as _dt_st
        _today_st = _dt_st.date.today()
        _is_today_st = bool(re.search(r'오늘(?![가-힣])', query))
        _is_yesterday_st = bool(re.search(r'어제(?![가-힣])', query))

        # ── 영업사원별 매출 랭킹 분기 ──
        _is_sp_breakdown = bool(re.search(
            r'영업사원별|영업담당별|사원별|담당자별|담당별|사원.*매출|담당.*매출', query
        ))
        if _is_sp_breakdown:
            try:
                _, ym_sp = _extract_month_year(query)
                _cur_ym_sp = _today_st.strftime("%Y%m")
                _mo_sp = int(ym_sp[4:6])
                if ym_sp == _cur_ym_sp:
                    _period_sp = f"{_mo_sp}월 1~{_today_st.day}일 기준"
                else:
                    _period_sp = f"{_mo_sp}월"
                sp_rows = _safe_query(f"""
                    SELECT `영업사원명`,
                           ROUND(COALESCE(SUM(`매출액`),0)/1000000, 2) AS sales
                    FROM {T_MAIN}
                    WHERE `사업부명` = '외식식재사업부'
                      AND `부서명` = '{_specific_team_m}'
                      AND `년월` = '{ym_sp}'
                    GROUP BY `영업사원명`
                    ORDER BY sales DESC
                """)
                sp_rows = [r for r in sp_rows if float(r.get("sales", 0)) >= 0]
                if not sp_rows:
                    _send_kakao_callback(callback_url,
                        f"{_specific_team_m}의 {_period_sp} 영업사원별 매출 데이터가 없습니다.", "팀SP매출")
                else:
                    total_sp = sum(float(r.get("sales", 0)) for r in sp_rows)
                    lines_sp = [f"📊 {_specific_team_m} {_period_sp} 영업사원별 매출\n"]
                    for i, r in enumerate(sp_rows, 1):
                        s = float(r.get("sales", 0))
                        lines_sp.append(f"{i}. {r['영업사원명']} — {_format_value(s)}백만원")
                    lines_sp.append(f"\n합계: {_format_value(total_sp)}백만원 | {len(sp_rows)}명")
                    _send_kakao_callback(callback_url, "\n".join(lines_sp), "팀SP매출")
            except Exception as e:
                logger.error(f"[콜백] 팀SP매출 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 영업사원별 매출 조회 중 오류가 발생했습니다.", "팀SP매출")
            return

        try:
            if _is_today_st or _is_yesterday_st:
                _date_st = (_today_st - _dt_st.timedelta(days=1)) if _is_yesterday_st else _today_st
                _date_str_st = _date_st.strftime("%Y%m%d")
                _label_st = f"{_date_st.month}월 {_date_st.day}일 (" + ("어제" if _is_yesterday_st else "오늘") + ")"
                rows_st = _safe_query(f"""
                    SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000, 4) AS sales
                    FROM {T_MAIN}
                    WHERE `사업부명` = '외식식재사업부'
                      AND `부서명` = '{_specific_team_m}'
                      AND `대금청구일` = '{_date_str_st}'
                """)
                sales_st = float((rows_st[0].get("sales") or 0) if rows_st else 0)
                text_st = f"{_specific_team_m}의 {_label_st} 매출액은 {_format_value(sales_st)}백만원입니다."
            else:
                _, ym_st = _extract_month_year(query)
                rows_st = _safe_query(f"""
                    SELECT ROUND(COALESCE(SUM(`매출액`),0)/1000000, 2) AS sales
                    FROM {T_MAIN}
                    WHERE `사업부명` = '외식식재사업부'
                      AND `부서명` = '{_specific_team_m}'
                      AND `년월` = '{ym_st}'
                """)
                sales_st = float((rows_st[0].get("sales") or 0) if rows_st else 0)
                month_st = int(ym_st[4:6])
                _cur_ym_st = _today_st.strftime("%Y%m")
                if ym_st == _cur_ym_st:
                    try:
                        text_st, _fc_st = _build_dept_forecast_card(
                            "부서명", _specific_team_m, sales_st, ym_st, _today_st
                        )
                    except Exception as _fe:
                        logger.warning(f"[팀카드] 빌드 실패({_fe}), 기본 포맷 사용")
                        text_st = f"{_specific_team_m}의 {month_st}월 매출액은 {_format_value(sales_st)}백만원입니다."
                        _fc_st = None
                else:
                    text_st = f"{_specific_team_m}의 {month_st}월 매출액은 {_format_value(sales_st)}백만원입니다."
                    _fc_st = None
            _user_last_sales[user_id] = {
                "target_key":  "부서명",
                "target_name": _specific_team_m,
                "yearmonth":   ym_st,
                "forecast_total": _fc_st,
            }
            if _is_today_st or _is_yesterday_st:
                _send_kakao_callback_qr(callback_url, text_st, _REASON_QR, "팀단독매출")
            else:
                _month_for_profit = int(ym_st[4:6])
                _team_profit_qr = [
                    {"label": "💰 GP+공헌이익", "action": "message", "messageText": f"{_specific_team_m} {_month_for_profit}월 수익성"},
                    {"label": "🏷️ 브랜드별 수익성", "action": "message", "messageText": f"{_specific_team_m} {_month_for_profit}월 브랜드별 수익성"},
                ] + _REASON_QR
                _send_kakao_callback_qr(callback_url, text_st, _team_profit_qr, "팀단독매출")
        except Exception as e:
            logger.error(f"[콜백] 팀단독매출 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 팀 매출 조회 중 오류가 발생했습니다.", "팀단독매출")
        return

    # ─── 조직별 매출 (Dify 바이패스) ─────────────────────
    org_m = re.search(
        r'(부서|팀|MD|지점그룹|지점)\s*(?:별)?\s*(?:매출|실적|현황|순위)', query
    )
    if org_m:
        org_label = org_m.group(1)
        org_key_map = {
            "부서": "부서명", "팀": "부서명", "MD": "MD명",
            "지점그룹": "지점그룹명", "지점": "지점명",
        }
        org_key = org_key_map.get(org_label, f"{org_label}명")
        import datetime as _dt_org
        _today_org = _dt_org.date.today()
        _is_today = bool(re.search(r'오늘(?![가-힣])', query))
        _is_yesterday = bool(re.search(r'어제(?![가-힣])', query))
        logger.info(f"[콜백] 조직별매출: org_key={org_key}, today={_is_today}, yesterday={_is_yesterday}")
        try:
            if _is_today or _is_yesterday:
                _date_org = (_today_org - _dt_org.timedelta(days=1)) if _is_yesterday else _today_org
                _date_str_org = _date_org.strftime("%Y%m%d")
                _label_org = f"{_date_org.month}월 {_date_org.day}일 (" + ("어제" if _is_yesterday else "오늘") + ")"
                rows = _fetch_org_ranking_daily(org_key, _date_str_org)
                yearmonth_org = _date_str_org[:6]
                text = _build_org_ranking_markdown(rows, org_key, yearmonth_org, date_label=_label_org)
            else:
                _, yearmonth_org = _extract_month_year(query)
                rows = _fetch_org_ranking(org_key, yearmonth_org)
                _cur_ym = _today_org.strftime("%Y%m")
                if yearmonth_org == _cur_ym:
                    _mo_org = int(yearmonth_org[4:6])
                    _label_org = f"{_mo_org}월 1~{_today_org.day}일 기준"
                else:
                    _label_org = f"{int(yearmonth_org[4:6])}월"
                text = _build_org_ranking_markdown(rows, org_key, yearmonth_org, date_label=_label_org)
            _user_last_sales[user_id] = {"target_key": "사업부명", "target_name": "외식식재사업부", "yearmonth": yearmonth_org}
            _send_kakao_callback(callback_url, _to_kakao_text(text), "조직매출")
        except Exception as e:
            logger.error(f"[콜백] 조직별매출 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 조직별 매출 조회 중 오류가 발생했습니다.", "조직매출")
        return

    # ─── ZA본사별 매출 (Dify 바이패스) ─────────────────────
    if re.search(r'(?:ZA\s*본사별|본사별|ZA별|ZA\s*(?:매출|실적)\s*(?:순위|TOP))', query, re.IGNORECASE):
        month_num, yearmonth = _extract_month_year(query)
        logger.info(f"[콜백] ZA본사매출: ym={yearmonth}")
        try:
            rows = _fetch_zp_ranking(yearmonth)
            text = _build_zp_ranking_markdown(rows, yearmonth)
            _user_last_sales[user_id] = {"target_key": "사업부명", "target_name": "외식식재사업부", "yearmonth": yearmonth}
            _send_kakao_callback(callback_url, _to_kakao_text(text), "ZA매출")
        except Exception as e:
            logger.error(f"[콜백] ZA본사매출 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ ZA본사별 매출 조회 중 오류가 발생했습니다.", "ZA매출")
        return

    # ─── 고객계층별 매출 (Dify 바이패스) ─────────────────────
    if re.search(r'고객\s*(?:계층|유형|타입)\s*(?:별)?\s*(?:매출|실적|현황)', query):
        month_num, yearmonth = _extract_month_year(query)
        logger.info(f"[콜백] 고객계층매출: ym={yearmonth}")
        try:
            rows = _fetch_customer_tier_sales(yearmonth)
            text = _build_customer_tier_markdown(rows, yearmonth)
            _user_last_sales[user_id] = {"target_key": "사업부명", "target_name": "외식식재사업부", "yearmonth": yearmonth}
            _send_kakao_callback(callback_url, _to_kakao_text(text), "고객계층")
        except Exception as e:
            logger.error(f"[콜백] 고객계층매출 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 고객계층별 매출 조회 중 오류가 발생했습니다.", "고객계층")
        return

    # ─── 미출고 현황 (Dify 바이패스) ─────────────────────────────
    if re.search(r'미출고|미출\s*현황|미출\s*건|미출\s*있어|출고\s*안\s*된|안\s*나간\s*물건|오늘\s*미출|어제\s*미출', query):
        # 영업귀책 여부 감지
        only_gyucheck = bool(
            re.search(r'귀책|영업귀책|자책|내\s*잘못|내\s*귀책', query)
        )
        # 날짜 감지 (어제/YYYY-MM-DD/MM월 DD일/오늘)
        import datetime as _dt
        date_str: str | None = None
        date_literal_m = re.search(r'(\d{4}-\d{2}-\d{2})', query)
        date_md_m = re.search(r'(\d{1,2})월\s*(\d{1,2})일', query)
        date_slash_m = re.search(r'\b(\d{1,2})/(\d{1,2})\b', query)
        if date_literal_m:
            date_str = date_literal_m.group(1)
        elif date_md_m:
            cur_year = int(time.strftime("%Y"))
            mm = int(date_md_m.group(1))
            dd = int(date_md_m.group(2))
            date_str = f"{cur_year}-{mm:02d}-{dd:02d}"
        elif date_slash_m:
            cur_year = int(time.strftime("%Y"))
            mm = int(date_slash_m.group(1)); dd = int(date_slash_m.group(2))
            date_str = f"{cur_year}-{mm:02d}-{dd:02d}"
        elif re.search(r'어제(?![가-힣])', query):
            date_str = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif re.search(r'오늘(?![가-힣])', query):
            date_str = _dt.date.today().strftime("%Y-%m-%d")
        # ── 이름/팀 결정 (우선순위: 쿼리 명시 이름 > 우리팀 > 등록 이름) ──
        _reg_users_u = _load_users()
        _uinfo = _reg_users_u.get(user_id, {})
        _my_name = _uinfo.get("name", "").strip()
        _my_team = _uinfo.get("team", "").strip()
        # "우리팀/우리지점/우리부서" 감지 — 이름 추출보다 먼저 확인
        is_team_query = bool(re.search(r'우리\s*(?:팀|지점|영업소|부서|사업부)', query))
        # 팀명 직접 입력 감지 (예: "외식1팀 미출고", "1팀 미출")
        _direct_team_m = re.search(
            r'([가-힣A-Za-z0-9]{1,10}(?:팀|지점|영업소|사업부))\s*(?:전체\s*)?(?:미출|미출고)',
            query,
        )
        _TEAM_NORMALIZE = {
            "1팀": "외식1팀", "2팀": "외식2팀", "3팀": "외식3팀",
        }
        # 쿼리에서 이름 명시 여부: 미출고 앞에 오는 한글 이름
        _NAME_BLACKLIST = {'오늘', '어제', '전체', '모든', '우리팀', '우리지점', '우리부서',
                           '우리영업소', '우리사업부', '미출고', '알려줘', '현황', '조회'}
        # 공백 포함 이름 우선 (예: "홍 길동 미출고")
        _qname_m = re.search(
            r'([가-힣]{1,2}\s+[가-힣]{1,3})(?:\s*씨|\s*님)?\s*(?:오늘\s*|어제\s*|\d+월\s*\d+일\s*|\d+/\d+\s*)?미출',
            query,
        )
        if not _qname_m:
            _qname_m = re.search(
                r'([가-힣]{2,4})(?:\s*씨|\s*님)?\s*(?:오늘\s*|어제\s*|\d+월\s*\d+일\s*|\d+/\d+\s*)?미출',
                query,
            )
        query_name = _qname_m.group(1).strip() if _qname_m else ""
        if query_name in _NAME_BLACKLIST or re.search(r'^우리', query_name):
            query_name = ""
        # 우선순위: 팀명 직접입력 > 우리팀 > 쿼리 이름 > 등록 이름(본인)
        if _direct_team_m and not is_team_query:
            _raw_team = _direct_team_m.group(1).strip()
            _normalized_team = _TEAM_NORMALIZE.get(_raw_team, _raw_team)
            sp_name_u = _normalized_team
            is_team = True
        elif is_team_query and _my_team:
            # 우리팀 전체 조회
            sp_name_u = _my_team
            is_team = True
        elif query_name:
            # 쿼리에 명시된 타인 이름
            sp_name_u = query_name
            is_team = False
        elif _my_name:
            # 등록 이름으로 본인 조회
            sp_name_u = _my_name
            is_team = False
        else:
            _send_kakao_callback(
                callback_url,
                "미출고 조회를 위해 먼저 챗봇에 등록해 주세요.\n(등록 방법: /등록 명령어 사용)",
                "미출고",
            )
            return
        logger.info(f"[콜백] 미출고: sp={sp_name_u}, is_team={is_team}, date={date_str}, gyucheck={only_gyucheck}")
        # 타인 이름 조회일 경우 follow-up QR에 이름 포함 (문맥 유지)
        _name_prefix = f"{sp_name_u} " if (query_name and query_name != _my_name) else ""
        _unshipped_ctx_qr = [
            {"label": " 어제 현황",    "action": "message", "messageText": f"{_name_prefix}어제 미출고 알려줘"},
            {"label": "🏠 메인 메뉴",    "action": "message", "messageText": "메뉴"},
        ]
        try:
            if is_team:
                rows_u = _fetch_unshipped_by_team(sp_name_u, date_str, only_gyucheck)
            else:
                rows_u = _fetch_unshipped(sp_name_u, date_str, only_gyucheck)
            text_u = _build_unshipped_markdown(rows_u, sp_name_u, only_gyucheck, is_team=is_team)
            if is_team and rows_u:
                # 담당자 건수 내림차순으로 개별 QR 동적 생성 (최대 9명)
                from collections import Counter as _Counter_qr
                _mn_cnt = _Counter_qr(
                    str(r.get("영업담당자명", "")).strip() for r in rows_u
                    if r.get("영업담당자명")
                )
                _date_suffix = f" {date_str} 미출고 알려줘" if date_str else " 미출고 알려줘"
                _team_member_qr = [
                    {"label": f"👤 {mn}", "action": "message", "messageText": f"{mn}{_date_suffix}"}
                    for mn, _ in _mn_cnt.most_common(9)
                ]
                _team_member_qr.append({"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"})
                _send_kakao_callback_qr(callback_url, _to_kakao_text(text_u), _team_member_qr, "미출고")
            else:
                _send_kakao_callback_qr(callback_url, _to_kakao_text(text_u), _unshipped_ctx_qr, "미출고")
        except Exception as e:
            logger.error(f"[콜백] 미출고 조회 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 미출고 조회 중 오류가 발생했습니다.", "미출고")
        return

    # 월별 전체매출 직접 처리 (Dify 바이패스) ─────────────────────
    # 예: "외식식재사업부 3월 매출액", "외식1팀 3월 전체매출 알려줘"
    if '매출' in query:
        # ── 월 미명시: "사업부 매출액" / "외식식재사업부 매출액" → 이번 달 기준 ──
        _dept_no_month_m = re.search(
            r'([가-힣A-Za-z0-9]*(?:사업부|지점))\s*(?:의)?\s*(?:전체)?\s*매출(?:액)?'
            r'(?:\s+(?:알려|줘|주세|얼마|가).*)?$',
            query.strip(),
        )
        if _dept_no_month_m and not re.search(r'\d{1,2}월', query):
            import datetime as _dt_dnm
            _today_dnm  = _dt_dnm.date.today()
            _ym_dnm     = _today_dnm.strftime("%Y%m")
            _mo_dnm     = _today_dnm.month
            _day_dnm    = _today_dnm.day
            _tname_dnm  = _dept_no_month_m.group(1).strip()
            # "사업부" 단독이면 "외식식재사업부"로 확장
            if _tname_dnm == "사업부":
                _tname_dnm = "외식식재사업부"
            _tkey_dnm   = "사업부명" if "사업부" in _tname_dnm else "지점명"
            logger.info(f"[콜백] 사업부매출(월미명시): target={_tname_dnm}, ym={_ym_dnm}")
            try:
                rows_dnm = _fetch_monthly_total(_tkey_dnm, _tname_dnm, _ym_dnm)
                _sales_so_far_dnm = float(rows_dnm[0]["매출액_억원"]) if rows_dnm else 0.0
                if _sales_so_far_dnm > 0:
                    text_dnm, _fc_dnm = _build_dept_forecast_card(
                        _tkey_dnm, _tname_dnm, _sales_so_far_dnm, _ym_dnm, _today_dnm
                    )
                    text_dnm += "\n※ SAP 익일 반영. 예상 매출은 일평균 기준 단순 추정입니다."
                    _user_last_sales[user_id] = {
                        "target_key":  _tkey_dnm,
                        "target_name": _tname_dnm,
                        "yearmonth":   _ym_dnm,
                        "forecast_total": _fc_dnm,
                    }
                    _send_kakao_callback_qr(callback_url, _to_kakao_text(text_dnm), _REASON_QR, "사업부매출")
                else:
                    _send_kakao_callback(callback_url,
                        f"{_tname_dnm}의 {_mo_dnm}월 매출 데이터가 없습니다.", "사업부매출")
            except Exception as e:
                logger.error(f"[콜백] 사업부매출(월미명시) 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 사업부 매출 조회 중 오류가 발생했습니다.", "사업부매출")
            return

        mt_m = _MONTHLY_TOTAL_PATTERN.search(query)
        if mt_m:
            # 그룹 1+2: "사업부/지점 N월" 순서  /  그룹 3+4: "N월 ... 사업부/지점" 순서
            if mt_m.group(1) and mt_m.group(2):
                target_name = mt_m.group(1).strip()
                month_num   = int(mt_m.group(2))
            elif mt_m.group(3) and mt_m.group(4):
                month_num   = int(mt_m.group(3))
                target_name = mt_m.group(4).strip()
            else:
                target_name = ""
                month_num   = 0

            if target_name and 1 <= month_num <= 12:
                cur_year    = int(time.strftime("%Y"))
                yearmonth   = f"{cur_year}{month_num:02d}"
                target_key  = "사업부명" if "사업부" in target_name else "지점명"
                logger.info(f"[콜백] 월별매출 직접 처리: target={target_name}, ym={yearmonth}")
                try:
                    rows = _fetch_monthly_total(target_key, target_name, yearmonth)
                    text = _build_monthly_sales_markdown(rows)
                    # SALES_CTX 태그 파싱 → user context 저장 후 태그 제거
                    ctx_m = _SALES_CTX_RE.search(text)
                    if ctx_m:
                        _user_last_sales[user_id] = {
                            "target_key":  ctx_m.group(1),
                            "target_name": ctx_m.group(2),
                            "yearmonth":   ctx_m.group(3),
                        }
                        text = _SALES_CTX_RE.sub("", text).strip()
                    card = _to_kakao_text(text)
                    _send_kakao_callback_qr(callback_url, card, _REASON_QR, "월별매출")
                except Exception as e:
                    logger.error(f"[콜백] 월별매출 직접 조회 오류: {e}")
                    _send_kakao_callback(callback_url, "⚠️ 매출 조회 중 오류가 발생했습니다.", "월별매출")
                return

    # ─── 내 담당브랜드 매출 (바이패스) ─────────────────────────────────
    # query는 3-3 대명사치환 후 = '이름 담당브랜드 OO달'
    if '담당브랜드' in query:
        _ui_bm = _load_users().get(user_id, {})
        emp_bm = _ui_bm.get("emp_code", "")
        name_bm = _ui_bm.get("name", "")
        import datetime as _dt_bm
        _now_bm = _dt_bm.date.today()
        if re.search(r'이번\s*달|당월', query):
            _period_bm = f"{_now_bm.month}월"
            _dcond_bm = f"t.`년월` = '{_now_bm.strftime('%Y%m')}'"
        elif re.search(r'지난\s*달|전월', query):
            _prev_bm = (_now_bm.replace(day=1) - _dt_bm.timedelta(days=1))
            _period_bm = f"{_prev_bm.year}년 {_prev_bm.month}월"
            _dcond_bm = f"t.`년월` = '{_prev_bm.strftime('%Y%m')}'"
        else:  # 올해 또는 기간 미지정
            _period_bm = f"{_now_bm.year}년 전체"
            _dcond_bm = f"t.`년도` = '{_now_bm.year}'"
        _bm_follow_qr = [
            {"label": "📅 이번 달",    "action": "message", "messageText": "내 담당브랜드 이번달"},
            {"label": "📅 지난 달",    "action": "message", "messageText": "내 담당브랜드 지난달"},
            {"label": "📅 올해 전체",  "action": "message", "messageText": "내 담당브랜드 올해"},
            {"label": "🏠 메인 메뉴",  "action": "message", "messageText": "메뉴"},
        ]
        if emp_bm:
            try:
                rows_bm = _safe_query(f"""
                    SELECT t.`ZC본부명`,
                           ROUND(COALESCE(SUM(t.`매출액`),0)/1000000, 2) AS `매출액_억원`
                    FROM {T_MAIN} t
                    WHERE t.`영업사원` = '{emp_bm}'
                      AND {_dcond_bm}
                      AND t.`사업부명` = '외식식재사업부'
                    GROUP BY t.`ZC본부명`
                    ORDER BY `매출액_억원` DESC
                    LIMIT 30
                """)
                if rows_bm:
                    total_bm = sum(float(r.get("매출액_억원", 0)) for r in rows_bm)
                    lines_bm = [f"📊 [{name_bm}] 담당 브랜드 매출 ({_period_bm})\n"]
                    for i, r in enumerate(rows_bm, 1):
                        lines_bm.append(f"{i}. {r['ZC본부명']} — {_format_value(float(r['매출액_억원']))}백만원")
                    lines_bm.append(f"\n요약: {len(rows_bm)}개 브랜드 | 합계 {_format_value(total_bm)}백만원")
                    _send_kakao_callback_qr(callback_url, "\n".join(lines_bm), _bm_follow_qr, "담당브랜드")
                else:
                    _send_kakao_callback_qr(
                        callback_url,
                        f"{name_bm}님의 담당 브랜드 매출 데이터가 없습니다. ({_period_bm})\n담당 거래처가 없거나 해당 기간 매출이 없을 수 있습니다.",
                        _bm_follow_qr, "담당브랜드"
                    )
            except Exception as _e_bm:
                logger.error(f"[콜백] 담당브랜드 오류: {_e_bm}")
                _send_kakao_callback(callback_url, "⚠️ 담당브랜드 조회 중 오류가 발생했습니다.", "담당브랜드")
        else:
            _send_kakao_callback(callback_url, "⚠️ 사번 정보를 찾을 수 없습니다.", "담당브랜드")
        return

    # ─── 소속팀 브랜드 매출 (바이패스) ─────────────────────────────────
    if re.match(r'^소속팀\s*브랜드', query):
        _ui_tm = _load_users().get(user_id, {})
        team_tm = _ui_tm.get("team", "")
        name_tm = _ui_tm.get("name", "")
        import datetime as _dt_tm
        _now_tm = _dt_tm.date.today()
        if re.search(r'이번\s*달|당월', query):
            _period_tm = f"{_now_tm.month}월"
            _dcond_tm = f"t.`년월` = '{_now_tm.strftime('%Y%m')}'"
        elif re.search(r'지난\s*달|전월', query):
            _prev_tm = (_now_tm.replace(day=1) - _dt_tm.timedelta(days=1))
            _period_tm = f"{_prev_tm.year}년 {_prev_tm.month}월"
            _dcond_tm = f"t.`년월` = '{_prev_tm.strftime('%Y%m')}'"
        else:
            _period_tm = f"{_now_tm.year}년 전체"
            _dcond_tm = f"t.`년도` = '{_now_tm.year}'"
        _tm_follow_qr = [
            {"label": "📅 이번 달",    "action": "message", "messageText": "소속팀 브랜드 이번달"},
            {"label": "📅 지난 달",    "action": "message", "messageText": "소속팀 브랜드 지난달"},
            {"label": "📅 올해 전체",  "action": "message", "messageText": "소속팀 브랜드 올해"},
            {"label": "🏠 메인 메뉴",  "action": "message", "messageText": "메뉴"},
        ]
        if team_tm:
            try:
                rows_tm = _safe_query(f"""
                    SELECT t.`ZC본부명`,
                           ROUND(COALESCE(SUM(t.`매출액`),0)/1000000, 2) AS `매출액_억원`
                    FROM {T_MAIN} t
                    WHERE t.`지점명` = '{team_tm}'
                      AND {_dcond_tm}
                      AND t.`사업부명` = '외식식재사업부'
                    GROUP BY t.`ZC본부명`
                    ORDER BY `매출액_억원` DESC
                    LIMIT 30
                """)
                if rows_tm:
                    total_tm = sum(float(r.get("매출액_억원", 0)) for r in rows_tm)
                    lines_tm = [f"📊 [{team_tm}] 브랜드 매출 ({_period_tm})\n"]
                    for i, r in enumerate(rows_tm, 1):
                        lines_tm.append(f"{i}. {r['ZC본부명']} — {_format_value(float(r['매출액_억원']))}백만원")
                    lines_tm.append(f"\n요약: {len(rows_tm)}개 브랜드 | 합계 {_format_value(total_tm)}백만원")
                    _send_kakao_callback_qr(callback_url, "\n".join(lines_tm), _tm_follow_qr, "소속팀브랜드")
                else:
                    _send_kakao_callback_qr(
                        callback_url,
                        f"{team_tm} 브랜드 매출 데이터가 없습니다. ({_period_tm})",
                        _tm_follow_qr, "소속팀브랜드"
                    )
            except Exception as _e_tm:
                logger.error(f"[콜백] 소속팀브랜드 오류: {_e_tm}")
                _send_kakao_callback(callback_url, "⚠️ 소속팀 브랜드 조회 중 오류가 발생했습니다.", "소속팀브랜드")
        else:
            _send_kakao_callback(callback_url, "⚠️ 소속팀 정보를 찾을 수 없습니다.", "소속팀브랜드")
        return

    # ─── 팀 전체 매출 (바이패스) ───────────────────────────────────────
    if re.match(r'^팀\s*전체\s*매출', query):
        _ui_tt = _load_users().get(user_id, {})
        team_tt = _ui_tt.get("team", "")
        name_tt = _ui_tt.get("name", "")
        import datetime as _dt_tt
        _now_tt = _dt_tt.date.today()
        if re.search(r'오늘(?![가-힣])', query):
            _period_tt = f"{_now_tt.strftime('%Y-%m-%d')} (오늘)"
            _dcond_tt = f"`대금청구일` = '{_now_tt.strftime('%Y%m%d')}'"
        elif re.search(r'어제(?![가-힣])', query):
            _yest_tt = _now_tt - _dt_tt.timedelta(days=1)
            _period_tt = f"{_yest_tt.strftime('%Y-%m-%d')} (어제)"
            _dcond_tt = f"`대금청구일` = '{_yest_tt.strftime('%Y%m%d')}'"
        elif re.search(r'이번\s*달|당월', query):
            _period_tt = f"{_now_tt.year}년 {_now_tt.month}월 누계"
            _dcond_tt = f"`년월` = '{_now_tt.strftime('%Y%m')}'"
        elif re.search(r'지난\s*달|전월', query):
            _prev_tt = (_now_tt.replace(day=1) - _dt_tt.timedelta(days=1))
            _period_tt = f"{_prev_tt.year}년 {_prev_tt.month}월 누계"
            _dcond_tt = f"`년월` = '{_prev_tt.strftime('%Y%m')}'"
        else:  # 올해 또는 기간 미지정
            _period_tt = f"{_now_tt.year}년 전체"
            _dcond_tt = f"`년도` = '{_now_tt.year}'"
        _tt_follow_qr = [
            {"label": "📆 오늘",       "action": "message", "messageText": "팀 전체 매출 오늘"},
            {"label": "📅 어제",       "action": "message", "messageText": "팀 전체 매출 어제"},
            {"label": "📅 이번 달",    "action": "message", "messageText": "팀 전체 매출 이번달"},
            {"label": "📅 지난 달",    "action": "message", "messageText": "팀 전체 매출 지난달"},
            {"label": "📅 올해 전체",  "action": "message", "messageText": "팀 전체 매출 올해"},
            {"label": "🏠 메인 메뉴",  "action": "message", "messageText": "메뉴"},
        ]
        if team_tt:
            try:
                rows_tt = _safe_query(f"""
                    SELECT `부서명` AS `팀`,
                           ROUND(COALESCE(SUM(`매출액`),0)/1000000, 2) AS `매출액_억원`
                    FROM {T_MAIN}
                    WHERE `사업부명` = '외식식재사업부'
                      AND {_dcond_tt}
                    GROUP BY `부서명`
                    ORDER BY `매출액_억원` DESC
                """)
                if rows_tt:
                    _TT_ORDER = ["외식1팀", "외식2팀", "외식3팀", "영남지점"]
                    _tt_ord_map = {n: i for i, n in enumerate(_TT_ORDER)}
                    rows_tt = sorted(rows_tt, key=lambda r: _tt_ord_map.get(str(r.get("팀", "")), 99))
                    total_tt = sum(float(r.get("매출액_억원", 0)) for r in rows_tt)
                    # 조회자 소속팀 비교 (부서명 기준)
                    lines_tt = [f"📊 외식식재사업부 팀별 매출 ({_period_tt} 누계)\n※ SAP 기준, 당일 매출은 익일 반영\n"]
                    for i, r in enumerate(rows_tt, 1):
                        my_mark = " ◀" if team_tt and team_tt in r['팀'] else ""
                        lines_tt.append(f"{i}. {r['팀']} — {_format_value(float(r['매출액_억원']))}백만원{my_mark}")
                    lines_tt.append(f"\n합계: {_format_value(total_tt)}백만원 | {len(rows_tt)}개 팀")
                    _send_kakao_callback_qr(callback_url, "\n".join(lines_tt), _tt_follow_qr, "팀전체매출")
                else:
                    _send_kakao_callback_qr(
                        callback_url,
                        f"외식식재사업부 팀별 매출 데이터가 없습니다. ({_period_tt})",
                        _tt_follow_qr, "팀전체매출"
                    )
            except Exception as _e_tt:
                logger.error(f"[콜백] 팀전체매출 오류: {_e_tt}")
                _send_kakao_callback(callback_url, "⚠️ 팀 전체 매출 조회 중 오류가 발생했습니다.", "팀전체매출")
        else:
            _send_kakao_callback(callback_url, "⚠️ 소속팀 정보를 찾을 수 없습니다.", "팀전체매출")
        return

    # 브랜드명 ZC본부명 직접 조회 (Dify 바이패스) ─────────────────────
    # 오늘/어제 일별 브랜드 매출 (대금청구일 기준)
    if '매출' in query or '실적' in query:
        import datetime as _dt_br
        _today_br = _dt_br.date.today()
        _br_date_str: str | None = None
        _br_date_label: str = ""
        if re.search(r'오늘(?![가-힣])', query) and not re.search(r'\d{1,2}월', query):
            _br_date_str   = _today_br.strftime('%Y%m%d')
            _br_date_label = f"{_today_br.strftime('%Y-%m-%d')} (오늘)"
        elif re.search(r'어제(?![가-힣])', query) and not re.search(r'\d{1,2}월', query):
            _yest_br       = _today_br - _dt_br.timedelta(days=1)
            _br_date_str   = _yest_br.strftime('%Y%m%d')
            _br_date_label = f"{_yest_br.strftime('%Y-%m-%d')} (어제)"
        if _br_date_str:
            _br_name_m = re.search(
                r'([가-힣A-Za-z0-9]+(?:\([가-힣A-Za-z0-9]+\))?)'
                r'(?:는|은|의)?\s*(?:오늘|어제)?\s*(?:매출|실적)',
                query
            )
            _br_name = _br_name_m.group(1).strip() if _br_name_m else ""
            _br_name = re.sub(r'[는은의이가을를로에서만]$', '', _br_name).strip()
            _BRAND_DAY_BL = {'오늘', '어제', '브랜드', '사업부', '매출', '실적', '팀', '우리팀'}
            if _br_name and _br_name not in _BRAND_DAY_BL:
                logger.info(f"[콜백] 브랜드일별매출: brand={_br_name}, date={_br_date_str}")
                try:
                    _br_res = _fetch_brand_daily_sales(_br_name, _br_date_str)
                    if isinstance(_br_res, list):
                        options = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(_br_res))
                        _send_kakao_callback(callback_url,
                            f"'{_br_name}'와(과) 유사한 브랜드가 여러 개 있습니다.\n{options}\n\n정확한 브랜드명을 입력해주세요.",
                            "브랜드매출")
                    elif isinstance(_br_res, tuple):
                        _br_matched, _br_val, _br_level = _br_res
                        _br_card = f"{_br_matched}의 {_br_date_label} 매출액은 {_format_value(_br_val)}백만원입니다.\n📌 집계단위: {_br_level}"
                        if _br_level == "점포합산":
                            _br_ym = _br_date_str[:6]
                            _sugg, _sugg_qr = _build_store_agg_suggestion(_br_name, _br_ym)
                            if _sugg:
                                _br_card += _sugg
                                _send_kakao_callback_qr(callback_url, _br_card, _sugg_qr, "브랜드매출")
                            else:
                                _send_kakao_callback_qr(callback_url, _br_card, _SALES_FOLLOW_QR, "브랜드매출")
                        else:
                            _brand_send(callback_url, _br_card, _br_level, _br_matched, _br_date_str[:6])
                    else:
                        _send_kakao_callback_qr(callback_url,
                            f"'{_br_name}' {_br_date_label} 매출 데이터가 없습니다.\n(당일 데이터는 익일 반영 기준)",
                            _SALES_FOLLOW_QR, "브랜드매출")
                except Exception as _e_brd:
                    logger.error(f"[콜백] 브랜드일별매출 오류: {_e_brd}")
                    _send_kakao_callback(callback_url, "⚠️ 브랜드 매출 조회 중 오류가 발생했습니다.", "브랜드매출")
                return

    # 브랜드명 ZC본부명 직접 조회 (Dify 바이패스) ─────────────────────
    # 예: "샐러디는 2월에 매출액 얼마야"
    _BRAND_BYPASS_BLACKLIST = {'전년', '전체', '월별', '분기', '추이', '비교', '대비', '합계', '거래처', '영업사원', '사업부', '브랜드', '품목별', '팀별', '본사별', '신규'}
    if '매출' in query or '실적' in query:
        # 연도 먼저 추출 ("26년", "2026년", "25년" 등) → 브랜드명으로 오인 방지
        _year_m = re.search(r'(\d{2,4})년', query)
        if _year_m:
            _year_raw = int(_year_m.group(1))
            cur_year = (_year_raw + 2000) if _year_raw < 100 else _year_raw
        else:
            cur_year = int(time.strftime("%Y"))
        # 연도 표현을 쿼리에서 제거한 뒤 브랜드/월 파싱
        query_for_brand = re.sub(r'\d{2,4}년\s*', '', query)

        bm_m = _BRAND_SALES_PATTERN.search(query_for_brand)
        if bm_m:
            if bm_m.group(1) and bm_m.group(2):
                _prefix    = query_for_brand[:bm_m.start()].strip()
                brand_name = (f"{_prefix} {bm_m.group(1).strip()}".strip() if _prefix else bm_m.group(1).strip())
                month_num  = int(bm_m.group(2))
            elif bm_m.group(3) and bm_m.group(4):
                month_num  = int(bm_m.group(3))
                brand_name = bm_m.group(4).strip()
            else:
                brand_name = ""
                month_num  = 0

            # 끝에 붙은 한국어 조사 제거 (는/은/의/이/가/을/를/로/에서/만)
            # ※ '도'는 브랜드명(차백도, 신선도) 끝에 자주 쓰이므로 제거하지 않음
            brand_name = re.sub(r'[는은의이가을를로에서만]$', '', brand_name).strip()

            if brand_name and brand_name in _BRAND_BYPASS_BLACKLIST:
                brand_name = ""  # 블랙리스트 → Dify로 넘기기

            if brand_name and 1 <= month_num <= 12:
                yearmonth = f"{cur_year}{month_num:02d}"
                logger.info(f"[콜백] 브랜드매출 직접 처리: brand={brand_name}, ym={yearmonth}")
                try:
                    res = _fetch_brand_monthly_sales(brand_name, yearmonth)
                    if isinstance(res, list):
                        # 여러 브랜드가 매칭 → QR 버튼 + pending 저장
                        _short = res[:5]
                        options = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(_short))
                        _user_pending_candidates[user_id] = {
                            "month_num": month_num,
                            "yearmonth": yearmonth,
                            "candidates": {n: "브랜드(ZC)" for n in _short},
                        }
                        _qr_list = [{"label": n, "action": "message", "messageText": n} for n in _short]
                        card = (
                            f"'{brand_name}'와(과) 유사한 브랜드가 여러 개 있습니다.\n"
                            f"{options}\n\n"
                            f"버튼으로 선택하거나 정확한 이름으로 입력해주세요."
                        )
                        _send_kakao_callback_qr(callback_url, card, _qr_list, "브랜드매출")
                        return
                    elif isinstance(res, tuple):
                        matched_name, sales, level_label = res
                        card = (
                            f"{matched_name}의 {month_num}월 매출액은 "
                            f"{_format_value(sales)}백만원입니다."
                            f"\n📌 집계단위: {level_label}"
                        )
                        # 후속 증가사유 질문용 컨텍스트
                        _user_last_sales[user_id] = {
                            "target_key": "사업부명",
                            "target_name": "외식식재사업부",
                            "yearmonth": yearmonth,
                        }
                        if level_label == "점포합산":
                            _sugg, _sugg_qr = _build_store_agg_suggestion(brand_name, yearmonth)
                            if _sugg:
                                card += _sugg
                                _send_kakao_callback_qr(callback_url, card, _sugg_qr, "브랜드매출")
                            else:
                                _send_kakao_callback_qr(callback_url, card, _SALES_FOLLOW_QR, "브랜드매출")
                        else:
                            _brand_send(callback_url, card, level_label, matched_name, yearmonth)
                    else:
                        # None → 퍼지 검색
                        _candidates = _fuzzy_search_candidates(brand_name, yearmonth)
                        if not _candidates:
                            card = (
                                f"'{brand_name}' 관련 항목을 찾을 수 없습니다.\n"
                                f"정확한 브랜드명으로 다시 입력해주세요."
                            )
                            _send_kakao_callback(callback_url, card, "브랜드매출")
                        elif len(_candidates) == 1:
                            exact_name, _, exact_level = _candidates[0]
                            _user_pending_confirm[user_id] = {
                                "exact_name": exact_name,
                                "month_num": month_num,
                                "yearmonth": yearmonth,
                                "level_label": exact_level,
                            }
                            _send_kakao_callback_qr(
                                callback_url,
                                f"'{exact_name}'을(를) 원하시는 것 맞나요?",
                                [{"label": "예", "action": "message", "messageText": "예"},
                                 {"label": "아니오", "action": "message", "messageText": "아니오"}],
                                "브랜드매출",
                            )
                        else:
                            _user_pending_candidates[user_id] = {
                                "month_num": month_num,
                                "yearmonth": yearmonth,
                                "candidates": {c[0]: c[2] for c in _candidates},
                            }
                            qr_btns = [{"label": c[0], "action": "message", "messageText": c[0]} for c in _candidates]
                            candidate_lines = "\n".join(
                                f"  {i+1}. {c[0]} [{c[2]}]" for i, c in enumerate(_candidates)
                            )
                            _send_kakao_callback_qr(
                                callback_url,
                                f"'{brand_name}'과(와) 유사한 항목입니다. 하나를 선택해주세요.\n\n"
                                f"{candidate_lines}\n\n"
                                f"버튼으로 선택하거나 정확한 이름으로 입력해주세요.",
                                qr_btns,
                                "브랜드매출",
                            )
                except Exception as e:
                    logger.error(f"[콜백] 브랜드매출 직접 조회 오류: {e}")
                    _send_kakao_callback(callback_url, "⚠️ 브랜드 매출 조회 중 오류가 발생했습니다.", "브랜드매출")
                return

        # 월 미명시: "브랜드명 매출" → 이번달 기준 조회 (예: "신화푸드 매출액")
        if not bm_m:
            _ym_now = time.strftime("%Y%m")
            _mo_now = int(time.strftime("%m"))
            _bno_m = re.search(
                r'^((?:\([가-힣A-Za-z0-9&＆\s]+\))?[가-힣A-Za-z0-9&＆]{2,}(?:\([가-힣A-Za-z0-9&＆\s]+\))?)'
                r'\s*(?:는|은|의|이|가)?\s*(?:매출|실적)(?:액)?'
                r'(?:\s+(?:알려|줘|주세|얼마|가).*)?$',
                query_for_brand.strip(),
                re.IGNORECASE,
            )
            if _bno_m:
                _bnm = re.sub(r'[는은의이가을를로에서만]$', '', _bno_m.group(1)).strip()
            else:
                # 공백 포함 이름(예: 주식회사 XX) 처리
                _bno_m_sp = re.search(
                    r'^(.+?)\s*(?:는|은|의|이|가)?\s*(?:매출|실적)(?:액)?(?:\s+(?:알려|줘|주세|얼마|가).*)?$',
                    query_for_brand.strip(), re.IGNORECASE,
                )
                _bnm = re.sub(r'[는은의이가을를로에서만]$', '', _bno_m_sp.group(1)).strip() if _bno_m_sp else ''
            if _bnm and _bnm not in _BRAND_BYPASS_BLACKLIST and len(_bnm) >= 2:
                logger.info(f"[콜백] 브랜드매출(월미명시) 직접 처리: brand={_bnm}, ym={_ym_now}")
                try:
                    res = _fetch_brand_monthly_sales(_bnm, _ym_now)
                    if isinstance(res, list):
                        _short2 = res[:5]
                        options = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(_short2))
                        _user_pending_candidates[user_id] = {
                            "month_num": _mo_now,
                            "yearmonth": _ym_now,
                            "candidates": {n: "브랜드(ZC)" for n in _short2},
                        }
                        _qr_list2 = [{"label": n, "action": "message", "messageText": n} for n in _short2]
                        card = (
                            f"'{_bnm}'와(과) 유사한 브랜드가 여러 개 있습니다.\n"
                            f"{options}\n\n"
                            f"버튼으로 선택하거나 정확한 이름으로 입력해주세요."
                        )
                        _send_kakao_callback_qr(callback_url, card, _qr_list2, "브랜드매출")
                        return
                    elif isinstance(res, tuple):
                        matched_name, sales, level_label = res
                        _today_dt = _dt_mod.date.today()
                        try:
                            card = _build_brand_forecast_card(
                                matched_name, sales, _ym_now, _today_dt, level_label=level_label
                            )
                            card += f"\n📌 집계단위: {level_label}"
                        except Exception as _ce:
                            logger.warning(f"[브랜드카드] 빌드 실패({_ce}), 기본 포맷 사용")
                            card = (
                                f"{matched_name}의 {_mo_now}월 매출액은 "
                                f"{_format_value(sales)}백만원입니다."
                                f"\n📌 집계단위: {level_label}"
                            )
                        _user_last_sales[user_id] = {
                            "target_key": "사업부명",
                            "target_name": "외식식재사업부",
                            "yearmonth": _ym_now,
                        }
                        if level_label == "점포합산":
                            _sugg, _sugg_qr = _build_store_agg_suggestion(_bnm, _ym_now)
                            if _sugg:
                                card += _sugg
                                _send_kakao_callback_qr(callback_url, card, _sugg_qr, "브랜드매출")
                            else:
                                _send_kakao_callback_qr(callback_url, card, _SALES_FOLLOW_QR, "브랜드매출")
                        else:
                            _brand_send(callback_url, card, level_label, matched_name, _ym_now)
                        return
                    else:
                        # None → 퍼지 검색
                        _candidates2 = _fuzzy_search_candidates(_bnm, _ym_now)
                        if not _candidates2:
                            pass  # fall through to 영업사원 총매출 bypass
                        elif len(_candidates2) == 1:
                            exact_name2, _, exact_level2 = _candidates2[0]
                            _user_pending_confirm[user_id] = {
                                "exact_name": exact_name2,
                                "month_num": _mo_now,
                                "yearmonth": _ym_now,
                                "level_label": exact_level2,
                            }
                            _send_kakao_callback_qr(
                                callback_url,
                                f"'{exact_name2}'을(를) 원하시는 것 맞나요?",
                                [{"label": "예", "action": "message", "messageText": "예"},
                                 {"label": "아니오", "action": "message", "messageText": "아니오"}],
                                "브랜드매출",
                            )
                            return
                        else:
                            _user_pending_candidates[user_id] = {
                                "month_num": _mo_now,
                                "yearmonth": _ym_now,
                                "candidates": {c[0]: c[2] for c in _candidates2},
                            }
                            qr_btns2 = [{"label": c[0], "action": "message", "messageText": c[0]} for c in _candidates2]
                            candidate_lines2 = "\n".join(
                                f"  {i+1}. {c[0]} [{c[2]}]" for i, c in enumerate(_candidates2)
                            )
                            _send_kakao_callback_qr(
                                callback_url,
                                f"'{_bnm}'과(와) 유사한 항목입니다. 하나를 선택해주세요.\n\n"
                                f"{candidate_lines2}\n\n"
                                f"버튼으로 선택하거나 정확한 이름으로 입력해주세요.",
                                qr_btns2,
                                "브랜드매출",
                            )
                            return
                except Exception as e:
                    logger.error(f"[콜백] 브랜드매출(월미명시) 오류: {e}")

    # ─── 영업사원 신규매출 (Dify 바이패스) ─────────────────────
    _SP_BLACKLIST = {'전체', '사업부', '외식식재', '브랜드', '품목별', '월별', '팀별', '본사별', '사업부별', '매출액', '영업사원'}
    sp_match = re.search(r'([가-힣]{2,5})\s*(?:신규매출|신규실적|신규 매출|신규 실적)', query)
    if sp_match and '신규' in query and sp_match.group(1) not in _SP_BLACKLIST:
        sp = re.sub(r'\s+', '', sp_match.group(1))
        _user_last_sp[user_id] = sp
        logger.info(f"[콜백] 영업사원 신규매출 바이패스: sp={sp}")
        try:
            import datetime as _dt_ns
            _now = _dt_ns.date.today()
            year = str(_now.year)
            # 월 키워드 파싱
            _date_filter = f"t.`년도` = '{year}'"
            _period_label = f"{year}년 전체"
            if re.search(r'이번\s*달|이번\s*월|당월', query):
                _ym = _now.strftime("%Y%m")
                _date_filter = f"t.`년월` = '{_ym}'"
                _period_label = f"{_now.month}월"
            elif re.search(r'지난\s*달|지난\s*월|전월', query):
                _prev = (_now.replace(day=1) - _dt_ns.timedelta(days=1))
                _ym = _prev.strftime("%Y%m")
                _date_filter = f"t.`년월` = '{_ym}'"
                _period_label = f"{_prev.year}년 {_prev.month}월"
            elif re.search(r'올해|금년', query):
                _date_filter = f"t.`년도` = '{year}'"
                _period_label = f"{year}년 전체"
            _ns_follow_qr = [
                {"label": "📅 이번 달",   "action": "message", "messageText": "내 신규매출 이번달"},
                {"label": "📅 지난 달",   "action": "message", "messageText": "내 신규매출 지난달"},
                {"label": "📅 올해 전체", "action": "message", "messageText": "내 신규매출 올해"},
                {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"},
            ]
            # Dify가 원래 생성하던 SQL과 동일한 형태의 rows 직접 조회
            rows = _safe_query(f"""
                WITH new_cust AS (
                    SELECT `영업사원명`, `ZC본부`, `ZC본부명`
                    FROM {T_MAIN}
                    WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{sp}%'
                      AND `사업부명` = '외식식재사업부'
                    GROUP BY `영업사원명`, `ZC본부`, `ZC본부명`
                    HAVING MIN(`대금청구일`) >= '{_NEW_CUST_DATE}'
                )
                SELECT t.`년월`,
                       nc.`ZC본부명`,
                       ROUND(COALESCE(SUM(t.`매출액`),0)/1000000, 2) AS `신규매출액_억원`
                FROM {T_MAIN} t
                JOIN new_cust nc ON t.`영업사원명` = nc.`영업사원명`
                                AND t.`ZC본부` = nc.`ZC본부`
                WHERE {_date_filter}
                  AND t.`사업부명` = '외식식재사업부'
                GROUP BY t.`년월`, nc.`ZC본부명`
                ORDER BY t.`년월`, nc.`ZC본부명`
            """)
            if rows:
                fake_sql = f"영업사원명 LIKE '%{sp}%'"
                text = _build_new_sales_markdown(rows, fake_sql)
                card = _to_kakao_text(text)
                _send_kakao_callback_qr(callback_url, card, _ns_follow_qr, "신규매출")
            else:
                _send_kakao_callback_qr(
                    callback_url,
                    f"'{sp}'님의 신규매출 데이터가 없습니다. ({_period_label})\n담당 거래처가 없거나 해당 기간 매출이 없을 수 있습니다.",
                    _ns_follow_qr,
                    "신규매출",
                )
        except Exception as e:
            logger.error(f"[콜백] 신규매출 바이패스 오류: {e}")
            _send_kakao_callback(callback_url, "⚠️ 신규매출 조회 중 오류가 발생했습니다.", "신규매출")
        return

    # ─── 영업사원 총매출 (Dify 바이패스) ─────────────────────
    # 예: "이충규 매출액", "강동민 매출 알려줘" → 이번달 기본값
    _SP_TOTAL_BL = {
        '전체', '사업부', '외식식재', '브랜드', '품목별', '월별', '팀별', '본사별',
        '사업부별', '영업사원', '지점별', '부서별', '순위', '합계', '거래처',
        '브랜드별', '전년', '대비', '비교', '추이', '분기',
    }
    _sp_tot_m = re.search(r'([가-힣]{2,4})\s*(?:의|은|는)?\s*(?:매출|실적)(?:액)?', query)
    if (
        _sp_tot_m
        and '신규' not in query
        and '브랜드' not in query
        and '팀' not in query
        and _sp_tot_m.group(1) not in _SP_TOTAL_BL
    ):
        _sp_t = re.sub(r'\s+', '', _sp_tot_m.group(1))
        # DB에 실제 영업사원명 존재 여부 확인 (person인지 brand인지 구분)
        _sp_chk = _safe_query(f"""
            SELECT MAX(regexp_replace(`영업사원명`, ' ', '')) AS sp_nm
            FROM {T_MAIN}
            WHERE regexp_replace(`영업사원명`, ' ', '') LIKE '%{_sp_t}%'
              AND `사업부명` = '외식식재사업부'
        """)
        if _sp_chk and _sp_chk[0].get("sp_nm"):
            _real_sp = _sp_chk[0]["sp_nm"]
            import datetime as _dt_sp
            _now_sp = _dt_sp.date.today()
            _ym_sp = _now_sp.strftime("%Y%m")
            _mo_sp = _now_sp.month
            _date_filter_sp = f"t.`년월` = '{_ym_sp}'"
            _period_sp = f"{_mo_sp}월"
            # 기간 키워드 오버라이드
            if re.search(r'지난\s*달|지난\s*월|전월', query):
                _prev_sp = (_now_sp.replace(day=1) - _dt_sp.timedelta(days=1))
                _ym_sp = _prev_sp.strftime("%Y%m")
                _date_filter_sp = f"t.`년월` = '{_ym_sp}'"
                _period_sp = f"{_prev_sp.month}월"
            elif re.search(r'올해|금년', query):
                _date_filter_sp = f"t.`년도` = '{_now_sp.year}'"
                _period_sp = f"{_now_sp.year}년 전체"
            elif re.search(r'이번\s*달|이번\s*월|당월', query):
                pass  # 기본값 그대로 (이번달)
            logger.info(f"[콜백] 영업사원 총매출 바이패스: sp={_sp_t}, filter={_date_filter_sp}")
            try:
                _st_rows = _safe_query(f"""
                    SELECT ROUND(COALESCE(SUM(t.`매출액`), 0) / 1000000, 2) AS `매출_억원`
                    FROM {T_MAIN} t
                    WHERE regexp_replace(t.`영업사원명`, ' ', '') LIKE '%{_sp_t}%'
                      AND t.`사업부명` = '외식식재사업부'
                      AND {_date_filter_sp}
                """)
                _sp_sales = float(_st_rows[0].get("매출_억원") or 0) if _st_rows else 0.0
                _sp_tot_qr = [
                    {"label": "📅 이번 달",   "action": "message", "messageText": f"{_sp_t} 매출 이번달"},
                    {"label": "📅 지난 달",   "action": "message", "messageText": f"{_sp_t} 매출 지난달"},
                    {"label": "📅 올해 전체", "action": "message", "messageText": f"{_sp_t} 매출 올해"},
                    {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"},
                ]
                card = f"{_real_sp}님의 {_period_sp} 매출액은 {_format_value(_sp_sales)}백만원입니다."
                _send_kakao_callback_qr(callback_url, card, _sp_tot_qr, "영업사원총매출")
            except Exception as e:
                logger.error(f"[콜백] 영업사원 총매출 오류: {e}")
                _send_kakao_callback(callback_url, "⚠️ 매출 조회 중 오류가 발생했습니다.", "영업사원총매출")
            return

    # 질문에서 영업사원명 추출하여 저장 (후속 '개인형 세부내역' 용)
    _NAME_BLACKLIST = {'전체', '월별', '분기', '추이', '비교', '대비', '합계', '거래처', '영업사원', '사업부', '브랜드', '브랜드별', '품목별', '팀별', '지점별', '본사별', '부서별', '순위'}
    name_match = re.search(r'([가-힣]{2,5})\s*(신규|매출|실적|현황)', query)
    if name_match and name_match.group(1) not in _NAME_BLACKLIST:
        sp = re.sub(r'\s+', '', name_match.group(1))
        _user_last_sp[user_id] = sp
        logger.info(f"[콜백] 영업사원명 저장: {sp}")

    def _send_callback(text: str):
        _send_kakao_callback(callback_url, text, "콜백")

    # Dify 쿼리에 사업부 제한 항상 주입 (이 챗봇은 외식식재사업부 전용)
    ctx = _user_last_sales.get(user_id)
    scope = ctx["target_name"] if ctx else "외식식재사업부"
    scope_key = ctx["target_key"] if ctx else "사업부명"
    current_year = time.strftime('%Y')
    prev_year = str(int(current_year) - 1)
    dify_query = (
        f"[SQL 작성 규칙]\n"
        f"- 테이블: h_hmfo.gd_dcube.`01_sap_sales_custmasters`\n"
        f"- 한글 컬럼명에는 반드시 백틱(`) 사용\n"
        f"- 조회 범위: `{scope_key}`='{scope}' 필터 필수\n"
        f"- `년월` 컬럼은 'yyyyMM' 형식 문자열 (ex: '202603')\n"
        f"- 날짜 필터: 달만 지정시 `년월` = '{current_year}MM' (ex: 3월 → `년월`='{current_year}03')\n"
        f"- 년도 미지정이면 기본 {current_year}년: `년월` LIKE '{current_year}%'\n"
        f"  ※ 단, '최초매출일/처음매출/언제부터' 질문은 전 기간 조회 → 년월/년도 필터 없이 MIN(`대금청구일`) 또는 MIN(`년월`) 사용\n"
        f"  예) 거래처명 LIKE '%키워드%' GROUP BY 거래처명 → MIN(`대금청구일`) AS 최초매출일\n"
        f"- 브랜드별 매출: `ZC본부명` GROUP BY\n"
        f"- 매출액 단위: ROUND(SUM(`매출액`)/1000000, 2) AS 매출_억원\n"
        f"- 브랜드명 검색: `ZC본부명` LIKE '%키워드%'\n"
        f"- '월별 추이' 질문: `년월` GROUP BY로 월별 합계 (브랜드 GROUP BY 하지 말 것)\n"
        f"- 거래처 단위: `ZA거래처명` (거래처 수 = COUNT(DISTINCT `ZA거래처명`))\n"
        f"- 신규 거래처: `대금청구일` 컬럼으로 판정, MIN(`대금청구일`)>='{prev_year}1001'인 거래처\n"
        f"  예: SELECT COUNT(DISTINCT `ZA거래처명`) FROM (...서브쿼리에서 HAVING MIN(`대금청구일`)>='{prev_year}1001'...)\n"
        f"- '전년 대비' 비교: {prev_year}년 동월 vs {current_year}년 동월 비교\n"
        f"[질문] {query}"
    )
    logger.info(f"[콜백] Dify 쿼리 범위 제한: {scope}, 년도: {current_year}")

    try:
        # ── Dify Enterprise: SQL 생성 요청 (streaming SSE) ──
        sse_body = json_mod.dumps({
            "inputs": {},
            "query": dify_query,
            "user": user_id,
            "response_mode": "streaming",
        }).encode('utf-8')
        sse_req = urllib.request.Request(
            f"{DIFY_BASE}/v1/chat-messages",
            data=sse_body, method='POST',
        )
        sse_req.add_header('Content-Type', 'application/json')
        sse_req.add_header('Authorization', f'Bearer {DIFY_TOKEN}')

        answer_chunks = []
        with urllib.request.urlopen(sse_req, timeout=55) as sse_resp:
            for raw_line in sse_resp:
                line = raw_line.decode('utf-8', errors='replace').strip()
                if not line.startswith('data: '):
                    continue
                try:
                    evt = json_mod.loads(line[6:])
                except json_mod.JSONDecodeError:
                    continue
                if evt.get('event') == 'agent_message':
                    answer_chunks.append(evt.get('answer', ''))
                elif evt.get('event') == 'message_end':
                    break
                elif evt.get('event') == 'error':
                    logger.error(f"[콜백] Dify SSE error: {evt}")
                    break

        answer = ''.join(answer_chunks)
        dify_sec = time.time() - t0
        logger.info(f"[콜백] Dify 응답 수신 ({dify_sec:.1f}초): {answer[:200]}")

        # ── SQL 추출 → 직접 실행 ──
        sql_match = re.search(r'```sql\s*\n(.+?)\n```', answer, re.DOTALL)
        if sql_match:
            generated_sql = sql_match.group(1).strip()
            # 한글 컬럼명 자동 백틱 처리
            generated_sql = _auto_backtick_korean(generated_sql)
            # 매출액 단위 보정: /100000000 → /1000000
            generated_sql = generated_sql.replace('/100000000', '/1000000')
            # 년도 필터 누락 보정: WHERE에 년월/년도 필터가 없으면 추가
            # ※ 단, '최초/처음/언제부터/전체기간' 질문은 전 기간 조회가 필요하므로 주입 안 함
            _NO_YEAR_INJECT = re.search(
                r'최초|처음|언제부터|시작일|첫.?매출|최초매출|전체.?기간|연도별|년도별|연간|전년|분기별', query
            )
            if not _NO_YEAR_INJECT and '`년월`' not in generated_sql and '`년도`' not in generated_sql:
                if 'WHERE' in generated_sql.upper():
                    inject = f"`년월` LIKE '{current_year}%' AND "
                    generated_sql = re.sub(
                        r'(WHERE\s+)',
                        r'\1' + inject,
                        generated_sql, count=1, flags=re.IGNORECASE
                    )
                    logger.info(f"[콜백] 년도 필터 자동 주입: {current_year}")
            elif _NO_YEAR_INJECT:
                logger.info(f"[콜백] 년도 필터 주입 SKIP (전기간 조회 질문): {_NO_YEAR_INJECT.group()}")
            logger.info(f"[콜백] Dify SQL (최종): {generated_sql[:300]}")
            try:
                rows = run_query(generated_sql, raw=True)
                if rows:
                    # 결과 형태에 따라 적절한 마크다운 빌더 선택
                    if _is_new_sales_shape(rows):
                        md = _build_new_sales_markdown(rows, original_sql=generated_sql)
                    elif _is_team_new_sales_shape(rows):
                        md = _build_team_new_sales_markdown(rows, original_sql=generated_sql)
                    elif _is_monthly_sales_shape(rows):
                        md = _build_monthly_sales_markdown(rows)
                    else:
                        # 범용 Dify 결과 → 깔끔한 텍스트 직접 생성
                        card = _format_dify_rows(rows, query)
                        _send_callback(card)
                        return
                    card = _to_kakao_text(md)
                    _send_callback(card)
                else:
                    _send_callback("조회 결과가 없습니다.")
            except Exception as sql_e:
                logger.error(f"[콜백] Dify SQL 실행 오류: {sql_e}")
                _send_callback(f"⚠️ SQL 실행 중 오류: {str(sql_e)[:100]}")
        else:
            # SQL 블록이 없으면 Dify 응답 텍스트를 그대로 전달
            logger.info("[콜백] Dify 응답에 SQL 블록 없음 → 텍스트 그대로 전달")
            # SALES_CTX 태그 파싱 → user context 저장 후 태그 제거
            ctx_match = _SALES_CTX_RE.search(answer)
            if ctx_match:
                _user_last_sales[user_id] = {
                    "target_key":  ctx_match.group(1),
                    "target_name": ctx_match.group(2),
                    "yearmonth":   ctx_match.group(3),
                }
                answer = _SALES_CTX_RE.sub("", answer).strip()
            card = _to_kakao_text(answer) if answer else "응답을 생성하지 못했습니다."
            _send_callback(card)

    except urllib.error.URLError as e:
        elapsed = time.time() - t0
        if "timed out" in str(e).lower() or elapsed >= 48:
            logger.warning(f"[콜백] Dify 타임아웃 ({elapsed:.1f}초)")
            _send_callback(
                "⏳ 데이터 조회에 시간이 걸리고 있습니다.\n"
                "잠시 후 같은 질문을 다시 보내주세요."
            )
        else:
            logger.error(f"[콜백] Dify 호출 실패 ({elapsed:.1f}초): {e}")
            _send_callback("⚠️ 데이터 조회 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")

    except Exception as e:
        elapsed = time.time() - t0
        err_str = str(e).lower()
        if "timed out" in err_str or "timeout" in err_str or elapsed >= 48:
            logger.warning(f"[콜백] Dify 타임아웃(Exception) ({elapsed:.1f}초): {e}")
            _send_callback(
                "⏳ 데이터 조회에 시간이 걸리고 있습니다.\n"
                "잠시 후 같은 질문을 다시 보내주세요."
            )
        else:
            logger.error(f"[콜백] 실패 ({elapsed:.1f}초): {e}")
            _send_callback("⚠️ 데이터 조회 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")


def _kakao_simple(text: str) -> dict:
    """카카오 simpleText 응답 래퍼"""
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]}
    }


def _kakao_quickreply(text: str, quickreplies: list) -> dict:
    """카카오 simpleText + QuickReply 버튼 응답 래퍼"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}],
            "quickReplies": quickreplies,
        },
    }


@app.post("/kakao/skill")
async def kakao_skill(request: Request, background_tasks: BackgroundTasks):
    """카카오 오픈빌더 스킬 엔드포인트 (비동기 콜백 + 사원 인증)"""
    try:
        body = await request.json()
        user_req = body.get("userRequest", {})
        utterance = user_req.get("utterance", "").strip()
        callback_url = user_req.get("callbackUrl", "")
        user_id = user_req.get("user", {}).get("id", "kakao-unknown")

        if not utterance:
            return _kakao_simple("질문을 입력해주세요.")

        logger.info(f"[카카오] 수신: user={user_id[:12]}, utterance={utterance[:60]}, callback={'Y' if callback_url else 'N'}")

        # ── 1) 등록 요청 처리 ──
        reg_match = _REGISTER_PATTERN.match(utterance)
        if reg_match:
            name, emp_code = reg_match.group(1), reg_match.group(2)
            logger.info(f"[인증] 등록 시도: name={name}, emp_code={emp_code}")

            # 이미 등록된 사용자
            if _is_registered(user_id):
                existing = _get_registered_name(user_id)
                return _kakao_simple(f"이미 등록되어 있습니다. ({existing})\n질문을 입력해주세요.")

            # 사번 중복 등록 방지 (1인 1계정)
            existing_user = _find_user_by_emp_code(emp_code)
            if existing_user:
                logger.warning(f"[인증] 사번 중복 차단: emp_code={emp_code}, 기존={existing_user}")
                return _kakao_simple(
                    "❌ 이 사번은 이미 다른 계정에서 등록되어 있습니다.\n\n"
                    "1인 1계정만 허용됩니다.\n"
                    "본인이 등록한 적이 없다면 관리자에게 문의해주세요."
                )

            # DB 인증 (콜백 있으면 비동기, 없으면 동기)
            if callback_url:
                background_tasks.add_task(
                    _register_and_callback, name, emp_code, user_id, callback_url
                )
                return {
                    "version": "2.0",
                    "useCallback": True,
                    "template": {
                        "outputs": [
                            {"simpleText": {"text": "🔐 인증 중입니다... 잠시만 기다려주세요."}}
                        ]
                    }
                }

            # 콜백 없음 → 동기 처리
            db_info = _verify_employee(name, emp_code)
            if db_info:
                msg = _register_user(user_id, name, emp_code, db_info)
                logger.info(f"[인증] 등록 성공: {db_info.get('영업사원명')}")
                return _kakao_simple(msg)
            else:
                logger.warning(f"[인증] 등록 실패: name={name}, emp_code={emp_code}")
                return _kakao_simple(
                    "❌ 인증에 실패했습니다.\n\n"
                    "이름과 사번을 다시 확인해주세요.\n"
                    "(외식식재사업부 소속 사원만 등록 가능합니다)\n\n"
                    "형식: 등록 [이름] [사번]\n"
                    "예시: 등록 홍길동 20160637"
                )

        # ── 2) 미등록 사용자 차단 ──
        if not _is_registered(user_id):
            logger.info(f"[인증] 미등록 사용자 차단: {user_id[:12]}")
            return _kakao_simple(_REGISTER_GUIDE)

        # ── 3) 등록된 사용자 → 정상 처리 ──

        # ── 3-0-admin) 관리자 전용 명령어 ──
        if _is_admin(user_id):
            utt_strip = utterance.strip()

            # 관리자 메뉴 화면
            if re.match(r'^관리자\s*메뉴$', utt_strip):
                return _kakao_quickreply(
                    "🔑 관리자 메뉴\n\n원하시는 기능을 선택해주세요.",
                    [
                        {"label": "📋 사용자 목록", "action": "message", "messageText": "사용자 목록"},
                        {"label": "➕ 사용자 등록", "action": "message", "messageText": "사용자등록 안내"},
                        {"label": "🚫 사용자 등록취소", "action": "message", "messageText": "등록취소 안내"},
                    ]
                )

            # 사용자 등록 안내
            if re.match(r'^사용자\s*등록\s*안내$', utt_strip):
                return _kakao_quickreply(
                    "➕ [관리자] 사용자 등록 (화이트리스트)\n\n"
                    "형식: 사용자등록 이름 사번\n"
                    "예시: 사용자등록 홍길동 20230001\n\n"
                    "등록된 사람은 챗봇에서 '등록 이름 사번' 입력 시\n"
                    "화이트리스트로 즉시 인증 통과됩니다.",
                    [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                )

            # 사용자 등록 실행: 사용자등록 이름 사번
            _add_m = re.match(r'^사용자\s*등록\s+([가-힣a-zA-Z\s]{2,10})\s+(\d{6,10})$', utt_strip)
            if _add_m:
                _add_name = _add_m.group(1).strip()
                _add_emp = _add_m.group(2).strip()
                # 블랙리스트 확인
                if _add_emp in _load_blacklist():
                    return _kakao_quickreply(
                        f"⛔ {_add_emp}는 블랙리스트에 등록된 사번입니다. 등록 불가.",
                        [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                    )
                # 이미 등록된 사용자 확인
                _existing = _find_user_by_emp_code(_add_emp)
                if _existing:
                    return _kakao_quickreply(
                        f"ℹ️ {_add_emp}는 이미 '{_existing}'으로 등록된 사용자입니다.",
                        [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                    )
                # DB 조회로 소속 파악
                _add_team = ""
                _add_rows = _safe_query(f"""
                    SELECT DISTINCT `지점명` FROM {T_MAIN}
                    WHERE `사업부명` = '{AUTH_DEPT}' AND `영업사원` = '{_add_emp}' LIMIT 1
                """)
                if _add_rows:
                    _add_team = _add_rows[0].get("지점명", "")
                # 이미 화이트리스트에 있는지 확인
                _wl_check = _load_whitelist()
                if _add_emp in _wl_check:
                    return _kakao_quickreply(
                        f"ℹ️ {_add_name}({_add_emp})는 이미 화이트리스트에 있습니다.",
                        [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                    )
                # 화이트리스트에 추가
                with _users_lock:
                    _wl = _load_whitelist()
                    _wl[_add_emp] = {"name": _add_name, "team": _add_team, "added_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    _save_whitelist(_wl)
                return _kakao_quickreply(
                    f"✅ 화이트리스트 등록 완료!\n\n"
                    f"이름: {_add_name}\n"
                    f"사번: {_add_emp}\n"
                    f"소속: {_add_team or '(DB 없음)'}\n\n"
                    f"해당 사용자가 챗봇에서\n'등록 {_add_name} {_add_emp}' 입력 시 바로 사용 가능합니다.",
                    [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                )

            # 등록취소 안내
            if re.match(r'^등록취소\s*안내$', utt_strip):
                return _kakao_quickreply(
                    "🚫 [관리자] 사용자 등록취소 (블랙리스트)\n\n"
                    "형식: 등록취소 이름 사번\n"
                    "예시: 등록취소 홍길동 20230001\n\n"
                    "⚠️ 취소된 사용자는 이후 재등록이 영구 차단됩니다.",
                    [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                )

            # 사용자 목록 조회
            if re.match(r'^(사용자\s*목록|등록자\s*목록|관리자\s*명단|유저\s*목록)$', utt_strip):
                users_all = _load_users()
                lines = ["🔑 [관리자] 등록 사용자 목록", f"총 {len(users_all)}명\n"]
                for i, (uid, info) in enumerate(users_all.items(), 1):
                    role_tag = " 👑" if info.get("role") == "admin" else ""
                    lines.append(
                        f"{i}. {info.get('name','?')} ({info.get('emp_code','?')}){role_tag}\n"
                        f"   소속: {info.get('team', '-')}\n"
                        f"   등록: {info.get('registered_at','?')[:10]}"
                    )
                return _kakao_quickreply("\n".join(lines), [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}, {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"}])

            # 사용자 등록 취소 (블랙리스트)
            _del_m = re.match(r'^등록취소\s+([가-힣a-zA-Z\s]{2,10})\s+(\d{6,10})$', utt_strip)
            if _del_m:
                _del_name, _del_emp = _del_m.group(1).strip(), _del_m.group(2).strip()
                users_all = _load_users()
                _del_uid = next((uid for uid, info in users_all.items()
                                 if info.get("emp_code") == _del_emp), None)
                # 등록된 사용자면 삭제
                _del_display = _del_name
                if _del_uid:
                    _del_info = users_all.pop(_del_uid)
                    _del_display = _del_info.get("name", _del_name)
                    with _users_lock:
                        _save_users(users_all)
                # 화이트리스트에서도 제거
                with _users_lock:
                    _wl = _load_whitelist()
                    if _del_emp in _wl:
                        _wl.pop(_del_emp)
                        _save_whitelist(_wl)
                # 블랙리스트에 추가 (영구 차단)
                with _users_lock:
                    _bl = _load_blacklist()
                    if _del_emp not in _bl:
                        _bl.append(_del_emp)
                        _save_blacklist(_bl)
                if _del_uid:
                    return _kakao_quickreply(
                        f"🚫 {_del_display}({_del_emp}) 등록 취소 완료\n"
                        f"블랙리스트에 추가 — 재등록 영구 차단됩니다.",
                        [{"label": "📋 사용자 목록", "action": "message", "messageText": "사용자 목록"},
                         {"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                    )
                else:
                    return _kakao_quickreply(
                        f"⚠️ 사번 {_del_emp}로 등록된 사용자가 없습니다.\n"
                        f"블랙리스트에는 추가했습니다 (신규 등록 차단).",
                        [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
                    )

            # 서버 상태 확인
            if re.match(r'^(서버\s*상태|상태\s*확인|ping)$', utt_strip, re.IGNORECASE):
                import datetime as _dt_adm
                now_str = _dt_adm.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                users_all = _load_users()
                return _kakao_quickreply(
                    f"🟢 서버 정상 운영 중\n"
                    f"시각: {now_str}\n"
                    f"등록 사용자: {len(users_all)}명",
                    [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"},
                     {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"}]
                )

        # ── 3-0) 메뉴 키워드 → 메인 메뉴 즉시 표시 ──
        if re.match(r'^(메뉴|메인메뉴|메인\s*메뉴|메인|main|menu|홈|처음으로|도움말)[\s!~]*$', utterance.strip(), re.IGNORECASE):
            _reg_info_tmp = _load_users().get(user_id, {})
            _name_tmp = _reg_info_tmp.get("name", "")
            _team_tmp = _reg_info_tmp.get("team", "")
            _role_tmp = _reg_info_tmp.get("role", "user")
            _role_badge = " 👑" if _role_tmp == "admin" else ""
            menu_text = (
                f"{'👋 ' + _name_tmp + _role_badge + '님 ' if _name_tmp else ''}무엇을 조회할까요?\n"
                f"{'[' + _team_tmp + '] ' if _team_tmp else ''}\n"
                "아래 버튼을 누르거나 직접 질문해주세요."
            )
            _menu_qr = list(_MAIN_MENU_QR)
            if _role_tmp == "admin":
                _menu_qr = _menu_qr + [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
            return _kakao_quickreply(menu_text, _menu_qr)

        # ── 3-0b) 매출 실적 메뉴 버튼 클릭 ──
        if utterance.strip() == "매출 실적 메뉴":
            return _kakao_quickreply(
                "📊 매출 실적 — 어떤 기준으로 조회할까요?",
                [
                    {"label": "👤 내 신규매출", "action": "message", "messageText": "내 신규매출 알려줘"},
                    {"label": "🏢 브랜드 매출", "action": "message", "messageText": "브랜드 매출 메뉴"},
                    {"label": "👥 팀 전체 매출", "action": "message", "messageText": "팀 전체 매출 기간선택"},
                    {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"},
                ],
            )

        # ── 3-0d) 내 신규매출 알려줘 (날짜 미지정) → 날짜 선택 QR ──
        if utterance.strip() in ("내 신규매출 알려줘", "내 신규매출"):
            return _kakao_quickreply(
                "📊 내 신규매출 — 어떤 기간을 조회할까요?",
                [
                    {"label": "📅 이번 달",    "action": "message", "messageText": "내 신규매출 이번달"},
                    {"label": "📅 지난 달",    "action": "message", "messageText": "내 신규매출 지난달"},
                    {"label": "📅 올해 전체",  "action": "message", "messageText": "내 신규매출 올해"},
                    {"label": "🏠 메인 메뉴",  "action": "message", "messageText": "메뉴"},
                ],
            )

        # ── 3-0e) 브랜드 매출 메뉴 → 브랜드 유형 선택 QR ──
        if utterance.strip() == "브랜드 매출 메뉴":
            return _kakao_quickreply(
                "🏢 브랜드 매출 — 어떤 브랜드를 조회할까요?",
                [
                    {"label": "👤 내 담당브랜드", "action": "message", "messageText": "내 담당브랜드 기간선택"},
                    {"label": "🏢 소속팀 브랜드",  "action": "message", "messageText": "소속팀 브랜드 기간선택"},
                    {"label": "✏️ 특정 브랜드",   "action": "message", "messageText": "특정 브랜드 직접입력"},
                    {"label": "🏠 메인 메뉴",     "action": "message", "messageText": "메뉴"},
                ],
            )

        # ── 3-0f) 내 담당브랜드 기간선택 ──
        if utterance.strip() == "내 담당브랜드 기간선택":
            return _kakao_quickreply(
                "📅 내 담당브랜드 — 어떤 기간을 조회할까요?",
                [
                    {"label": "📅 이번 달",    "action": "message", "messageText": "내 담당브랜드 이번달"},
                    {"label": "📅 지난 달",    "action": "message", "messageText": "내 담당브랜드 지난달"},
                    {"label": "📅 올해 전체",  "action": "message", "messageText": "내 담당브랜드 올해"},
                    {"label": "← 브랜드 선택", "action": "message", "messageText": "브랜드 매출 메뉴"},
                ],
            )

        # ── 3-0g) 소속팀 브랜드 기간선택 ──
        if utterance.strip() == "소속팀 브랜드 기간선택":
            return _kakao_quickreply(
                "📅 소속팀 브랜드 — 어떤 기간을 조회할까요?",
                [
                    {"label": "📅 이번 달",    "action": "message", "messageText": "소속팀 브랜드 이번달"},
                    {"label": "📅 지난 달",    "action": "message", "messageText": "소속팀 브랜드 지난달"},
                    {"label": "📅 올해 전체",  "action": "message", "messageText": "소속팀 브랜드 올해"},
                    {"label": "← 브랜드 선택", "action": "message", "messageText": "브랜드 매출 메뉴"},
                ],
            )

        # ── 3-0h) 특정 브랜드 직접입력 안내 ──
        if utterance.strip() == "특정 브랜드 직접입력":
            return _kakao_quickreply(
                "✏️ 조회할 브랜드명과 기간을 입력해주세요.\n\n"
                "예시:\n"
                "• 샐러디 5월 매출\n"
                "• 위드저니파트너스 지난달 매출\n"
                "• 생활맥주 26년 3월",
                [{"label": "← 브랜드 선택", "action": "message", "messageText": "브랜드 매출 메뉴"}],
            )

        # ── 3-0i) 팀 전체 매출 기간선택 ──
        if utterance.strip() in ("팀 전체 매출 기간선택", "팀 전체 매출 알려줘"):
            return _kakao_quickreply(
                "📅 팀 전체 매출 — 어떤 기간을 조회할까요?",
                [
                    {"label": " 오늘",       "action": "message", "messageText": "팀 전체 매출 오늘"},
                    {"label": "📅 어제",       "action": "message", "messageText": "팀 전체 매출 어제"},
                    {"label": "📅 이번 달",    "action": "message", "messageText": "팀 전체 매출 이번달"},
                    {"label": "📅 지난 달",    "action": "message", "messageText": "팀 전체 매출 지난달"},
                    {"label": "📅 올해 전체",  "action": "message", "messageText": "팀 전체 매출 올해"},
                    {"label": "← 매출 메뉴",   "action": "message", "messageText": "매출 실적 메뉴"},
                ],
            )

        # ── 3-0j) 수익성 분석 메뉴 ──
        if utterance.strip() == "수익성 분석 메뉴":
            return _kakao_quickreply(
                "💰 수익성 분석 — 어떤 기준으로 조회할까요?",
                [
                    {"label": "🏢 지점 전체",    "action": "message", "messageText": "지점 수익성 기간선택"},
                    {"label": "🏷️ 브랜드별",    "action": "message", "messageText": "브랜드별 수익성 기간선택"},
                    {"label": "🏪 거래처별",     "action": "message", "messageText": "거래처별 수익성 기간선택"},
                    {"label": "🏠 메인 메뉴",   "action": "message", "messageText": "메뉴"},
                ],
            )

        # ── 3-0j-1) 내 지점 수익성 기간선택 ──
        if utterance.strip() == "지점 수익성 기간선택":
            return _kakao_quickreply(
                "📅 지점 수익성 — 어떤 기간을 조회할까요?",
                [
                    {"label": "📅 이번 달",   "action": "message", "messageText": "지점 수익성 이번달"},
                    {"label": "📅 지난 달",   "action": "message", "messageText": "지점 수익성 지난달"},
                    {"label": "📅 올해 전체", "action": "message", "messageText": "지점 수익성 올해"},
                    {"label": "← 수익성 메뉴","action": "message", "messageText": "수익성 분석 메뉴"},
                ],
            )

        # ── 3-0j-2) 브랜드별 수익성 기간선택 ──
        if utterance.strip() == "브랜드별 수익성 기간선택":
            return _kakao_quickreply(
                "📅 브랜드별 수익성 — 어떤 기간을 조회할까요?",
                [
                    {"label": "📅 이번 달",   "action": "message", "messageText": "브랜드별 수익성 이번달"},
                    {"label": "📅 지난 달",   "action": "message", "messageText": "브랜드별 수익성 지난달"},
                    {"label": "📅 올해 전체", "action": "message", "messageText": "브랜드별 수익성 올해"},
                    {"label": "← 수익성 메뉴","action": "message", "messageText": "수익성 분석 메뉴"},
                ],
            )

        # ── 3-0j-3) 거래처별 수익성 기간선택 ──
        if utterance.strip() == "거래처별 수익성 기간선택":
            return _kakao_quickreply(
                "📅 거래처별 수익성 — 어떤 기간을 조회할까요?",
                [
                    {"label": "📅 이번 달",   "action": "message", "messageText": "거래처별 수익성 이번달"},
                    {"label": "📅 지난 달",   "action": "message", "messageText": "거래처별 수익성 지난달"},
                    {"label": "📅 올해 전체", "action": "message", "messageText": "거래처별 수익성 올해"},
                    {"label": "← 수익성 메뉴","action": "message", "messageText": "수익성 분석 메뉴"},
                ],
            )

        # ── 3-0c) 미출고 현황 버튼 클릭 ──        if utterance.strip() == "미출고 현황":
            return _kakao_quickreply(
                "📦 미출고 현황 — 어떤 기준으로 조회할까요?",
                [
                    {"label": "📋 내 미출고", "action": "message", "messageText": "미출고 알려줘"},
                    {"label": "👥 우리팀 미출고", "action": "message", "messageText": "우리팀 미출고 알려줘"},
                    {"label": "⚠️ 귀책 건만", "action": "message", "messageText": "귀책 미출고 알려줘"},
                    {"label": "🏠 메인 메뉴", "action": "message", "messageText": "메뉴"},
                ],
            )

        # 등록된 사용자 정보 로드 (이름/소속)
        _reg_users = _load_users()
        _reg_info = _reg_users.get(user_id, {})
        _reg_name = _reg_info.get("name", "")
        _reg_team = _reg_info.get("team", "")

        # ── 3-0z) 퍼지 확인 인터셉터 (예/아니오 + 다수후보 직접조회) ──
        # A) 다수 후보: 버튼에서 후보명 직접 선택
        if user_id in _user_pending_candidates:
            _pending_cands = _user_pending_candidates[user_id]
            if isinstance(_pending_cands, dict) and "candidates" in _pending_cands:
                _cands = _pending_cands.get("candidates", {})
                _cand_ym = str(_pending_cands.get("yearmonth", time.strftime("%Y%m")))
                _cand_mo = int(_pending_cands.get("month_num", int(time.strftime("%m"))))
            else:
                _cands = _pending_cands
                _cand_ym = time.strftime("%Y%m")
                _cand_mo = int(time.strftime("%m"))
            _matched_cand = None
            for _cand_name in _cands:
                if _cand_name in utterance or utterance in _cand_name:
                    _matched_cand = _cand_name
                    break
            if _matched_cand:
                _cand_level = _cands[_matched_cand]
                _user_pending_candidates.pop(user_id, None)
                if callback_url:
                    background_tasks.add_task(
                        _bg_candidate_query,
                        _matched_cand, _cand_level, _cand_ym, _cand_mo,
                        callback_url,
                    )
                    return {"version": "2.0", "useCallback": True}
                # callback 없음: 동기 실행
                _bg_candidate_query(_matched_cand, _cand_level, _cand_ym, _cand_mo, "")
                # (non-callback 환경에설 fallback - 커스텋)

        # B) 예/아니오 처리
        if user_id in _user_pending_confirm:
            _utt_s = utterance.strip()
            if re.match(r'^(예|네|ㅇ|ㅇㅇ|응|맞아|맞아요|맞습)[\s!~]*$', _utt_s):
                _pending = _user_pending_confirm.pop(user_id)
                _p_name  = _pending["exact_name"]
                _p_ym    = _pending["yearmonth"]
                _p_mo    = _pending["month_num"]
                _p_level = _pending["level_label"]
                if callback_url:
                    background_tasks.add_task(
                        _bg_confirm_query,
                        _p_name, _p_level, _p_ym, _p_mo,
                        callback_url,
                    )
                    return {"version": "2.0", "useCallback": True}
                _bg_confirm_query(_p_name, _p_level, _p_ym, _p_mo, "")
            elif re.match(r'^(아니|아니오|ㄴ|취소)[\s!~]*$', _utt_s):
                _user_pending_confirm.pop(user_id, None)
                if callback_url:
                    background_tasks.add_task(_send_kakao_callback_qr, callback_url, "다시 정확한 이름으로 입력해주세요.", _MAIN_MENU_QR, "안내")
                    return {"version": "2.0", "useCallback": True}
                return _kakao_quickreply("다시 정확한 이름으로 입력해주세요.", _MAIN_MENU_QR)

        # ── 3-1) 인사/잡담 즉시 응답 (DB/Dify 불필요) ──
        _GREET_M = re.search(
            r'^(안녕|하이|ㅎㅇ|헬로|hello|hi|반가워|좋은\s*(아침|오전|오후|저녁|밤)|수고|감사|고마워|고맙습|짱이야|최고|잘됐|응|ㅇㅇ|ㅋㅋ|ㅎㅎ|네|예|아|오|어|우와|와|오케|ok|ㅇㅋ)[\s!~♡]*$',
            utterance.strip(), re.IGNORECASE
        )
        if _GREET_M:
            greet_word = utterance.strip()
            logger.info(f"[인사] 즉시 응답: {greet_word}")
            if re.search(r'안녕|하이|ㅎㅇ|헬로|hello|hi', greet_word, re.IGNORECASE):
                reply = f"안녕하세요, {_reg_name}님! 😊"
            elif re.search(r'감사|고마워|고맙습', greet_word, re.IGNORECASE):
                reply = "천만에요! 😄"
            elif re.search(r'수고', greet_word, re.IGNORECASE):
                reply = "수고하세요! 😊"
            else:
                reply = f"네, {_reg_name}님! 😊"
            reply += "\n\n확인하고 싶은 것들을 아래에서 선택하거나\n편하게 질문을 입력해 주세요."
            _greet_qr = list(_MAIN_MENU_QR)
            if _is_admin(user_id):
                _greet_qr = _greet_qr + [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
            return _kakao_quickreply(reply, _greet_qr)

        # ── 3-2) 본인 확인 질문 즉시 응답 ──
        _WHO_M = re.search(
            r'(내가\s*누구|나\s*누구야?|나를?\s*알아|내\s*정보|내\s*이름|나\s*몇\s*번|내\s*사번|내\s*소속|누군지\s*알|날\s*알아)',
            utterance
        )
        if _WHO_M:
            logger.info(f"[본인확인] 즉시 응답: {_reg_name}")
            reply = (
                f"네, 알고 있어요! 😊\n\n"
                f"👤 이름: {_reg_name}\n"
                f"🏢 소속: {_reg_team}\n\n"
                f"아래에서 선택하거나 편하게 질문을 입력해 주세요."
            )
            _who_qr = list(_MAIN_MENU_QR)
            if _is_admin(user_id):
                _who_qr = _who_qr + [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
            return _kakao_quickreply(reply, _who_qr)

        # ── 3-3) '내/나의' 대명사 → 실제 이름으로 치환 ──
        _resolved_utterance = utterance
        if _reg_name and re.search(r'(^|\s)(내|나의|나한테|내꺼|제|저의)\s', utterance):
            _resolved_utterance = re.sub(r'(^|\s)(내|나의|나한테|내꺼|제|저의)\s', rf'\1{_reg_name} ', utterance)
            logger.info(f"[대명사치환] '{utterance}' → '{_resolved_utterance}'")

        # ── 3-4) 실제 데이터 조회 → 콜백 비동기 처리 ──
        if callback_url:
            background_tasks.add_task(
                _call_dify_and_callback, _resolved_utterance, user_id, callback_url
            )
            return {
                "version": "2.0",
                "useCallback": True,
                "template": {
                    "outputs": [
                        {"simpleText": {"text": "🔍 데이터를 조회하고 있습니다. 잠시만 기다려주세요."}}
                    ]
                }
            }

        # 콜백 없음 → 즉시 응답
        _fb_qr = list(_MAIN_MENU_QR)
        if _is_admin(user_id):
            _fb_qr = _fb_qr + [{"label": "🔑 관리자 메뉴", "action": "message", "messageText": "관리자 메뉴"}]
        return _kakao_quickreply(
            "💬 매출봇입니다.\n확인하고 싶은 것들을 아래에서 선택하거나\n편하게 질문을 입력해 주세요.",
            _fb_qr
        )

    except Exception as e:
        logger.error(f"[카카오] 스킬 처리 오류: {e}")
        return _kakao_simple("⚠️ 일시적인 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")


if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*55)
    print(" Databricks-Dify Bridge 서버 시작")
    print("="*55)
    print(" http://localhost:8000/docs  ← API 문서")
    print(" http://localhost:8000/auth  ← 브라우저 인증 (최초 1회)")
    print(" POST /kakao/skill           ← 카카오 오픈빌더")
    print("="*55 + "\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
