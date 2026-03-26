"""
Databricks → Dify 연결용 FastAPI 미들웨어 서버
- 서버 시작 시 브라우저 OAuth 인증 1회 수행 후 토큰 캐시
- Dify HTTP Tool에서 이 서버의 엔드포인트를 호출
"""

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from databricks.sdk import WorkspaceClient
from databricks import sql as dbsql
import os, logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── 설정 ────────────────────────────────────────────────
HOST       = "https://adb-707807361397497.17.azuredatabricks.net"
HTTP_PATH  = "/sql/1.0/warehouses/acc2ec933ffef2d0"
API_KEY    = os.getenv("DIFY_API_KEY", "dify-secret-1234")   # Dify에서 호출 시 사용할 키
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

def run_query(sql: str) -> list[dict]:
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
    global _cached_token
    _cached_token = None
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
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
            FROM h_hmfo.gd_dcube.`02_sap_daily_performance_analysis`
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
        SELECT `사업부`, `사업부명`, `일자`,
               SUM(`매출액`) AS 매출액합계,
               SUM(`목표매출액`) AS 목표매출액합계
        FROM h_hmfo.gd_dcube.`02_sap_daily_performance_analysis`
        {where_sql}
        GROUP BY `사업부`, `사업부명`, `일자`
        ORDER BY `일자` DESC
        LIMIT 100
    """
    try:
        rows = run_query(sql)
        return {"count": len(rows), "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query", dependencies=[Depends(verify_key)])
def custom_query(req: QueryRequest):
    """자유 SQL 쿼리 (관리자용)"""
    try:
        rows = run_query(req.sql)
        return {"count": len(rows), "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*55)
    print(" Databricks-Dify Bridge 서버 시작")
    print("="*55)
    print(" http://localhost:8000/docs  ← API 문서")
    print(" http://localhost:8000/auth  ← 브라우저 인증 (최초 1회)")
    print("="*55 + "\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
