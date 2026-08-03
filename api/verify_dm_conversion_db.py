"""
검증: SF송도갈비본점 DM 전환 집계 누락 원인
- Databricks SQL Connector 사용 (포털과 동일 연결 방식)
"""
import os, sys

# ── 환경 변수에서 연결 정보 읽기 ──────────────────────────────────
sys.path.insert(0, r"e:\git-copilot\dify-practice\api")

# config 로드
try:
    from config import DATABRICKS_HOST, DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN
except ImportError:
    DATABRICKS_HOST      = os.getenv("DATABRICKS_HOST", "")
    DATABRICKS_HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH", "")
    DATABRICKS_TOKEN     = os.getenv("DATABRICKS_TOKEN", "")

if not all([DATABRICKS_HOST, DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN]):
    print("[ERROR] Databricks 연결 정보 없음. config.py 또는 환경변수 확인 필요")
    sys.exit(1)

from databricks import sql as dbsql

TARGET_CUST  = "0000193241"
TARGET_MATNR = "100914"
DM_YM        = "202607"
T_MAIN       = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"

def run_query(cursor, sql: str, label: str):
    print(f"\n  [{label}]")
    print(f"  SQL: {sql[:120].strip()}...")
    cursor.execute(sql)
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    if not rows:
        print("  → 결과 없음")
    else:
        header = "  " + " | ".join(f"{c:>20}" for c in cols)
        print(header)
        print("  " + "-" * (len(header) - 2))
        for row in rows:
            print("  " + " | ".join(f"{str(v):>20}" for v in row))
    return rows, cols


print("=" * 70)
print("Databricks 연결 중...")
print("=" * 70)

try:
    conn = dbsql.connect(
        server_hostname=DATABRICKS_HOST.replace("https://", ""),
        http_path=DATABRICKS_HTTP_PATH,
        access_token=DATABRICKS_TOKEN,
    )
    cur = conn.cursor()
    print("연결 성공\n")

    # ── STEP 1: 거래처 기본 정보 ────────────────────────────────────────
    print("=" * 70)
    print("STEP 1. 거래처 기본 정보 확인")
    print("=" * 70)
    run_query(cur, f"""
        SELECT MAX(`거래처명`) AS cust_name, MAX(`ZC본부`) AS brand_code,
               MAX(`ZC본부명`) AS brand_name, MAX(`지점명`) AS team,
               MAX(`영업사원명`) AS sales_rep
        FROM {T_MAIN} WHERE `거래처` = '{TARGET_CUST}'
    """, "거래처 정보")

    # ── STEP 2: DM 발송 이후 월별 매출 ──────────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 2. DM 발송(202607) 이후 월별 매출")
    print("=" * 70)
    rows2, _ = run_query(cur, f"""
        SELECT `년월`,
               ROUND(SUM(`매출액`)) AS 매출액,
               COUNT(DISTINCT `상품코드`) AS 품목수
        FROM {T_MAIN}
        WHERE `거래처` = '{TARGET_CUST}' AND `년월` >= '{DM_YM}'
        GROUP BY `년월` ORDER BY `년월`
    """, "DM 이후 월별 매출")

    has_sales_after = len(rows2) > 0

    # ── STEP 3: 추천 상품 100914 전체 구매 이력 ─────────────────────────
    print("\n" + "=" * 70)
    print(f"STEP 3. 추천 상품 {TARGET_MATNR} 구매 이력 (2026년 전체)")
    print("=" * 70)
    rows3, _ = run_query(cur, f"""
        SELECT `년월`, `상품코드`, `상품명`,
               ROUND(SUM(`매출액`)) AS 매출액, SUM(`수량`) AS 수량
        FROM {T_MAIN}
        WHERE `거래처` = '{TARGET_CUST}'
          AND `상품코드` = '{TARGET_MATNR}'
          AND `년월` >= '202601'
        GROUP BY `년월`, `상품코드`, `상품명` ORDER BY `년월`
    """, f"상품코드 {TARGET_MATNR} 이력")

    has_dm_product = any(str(r[0]) >= DM_YM for r in rows3) if rows3 else False

    # ── STEP 4: 수정 후 dry-run ──────────────────────────────────────────
    print("\n" + "=" * 70)
    print("STEP 4. 수정 후 T_ACTION_RESULTS dry-run")
    print(f"  조건: 거래처={TARGET_CUST}, action_ym={DM_YM}(DM 발송월 기반)")
    print("=" * 70)
    rows4, _ = run_query(cur, f"""
        SELECT
          '{TARGET_CUST}' AS customer_code,
          '{DM_YM}' AS action_ym,
          ROUND(SUM(COALESCE(`매출액`, 0)) / 10000) AS sales_after_만원,
          ROUND(SUM(CASE WHEN `상품코드` = '{TARGET_MATNR}'
                         THEN COALESCE(`매출액`, 0) ELSE 0 END) / 10000) AS dm추천상품_만원
        FROM {T_MAIN}
        WHERE `거래처` = '{TARGET_CUST}' AND `년월` >= '{DM_YM}'
    """, "수정 후 집계 결과")

    # ── 최종 판정 ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("최종 판정")
    print("=" * 70)

    if not has_sales_after:
        print("  ❌ DM 발송 이후 매출 데이터 자체가 없음")
        print("     원인 가능성:")
        print("     1) T_MAIN에 7월 데이터 아직 미반영 (SAP 익일 반영)")
        print("     2) 거래처코드 0000193241 이 T_MAIN의 '거래처' 컬럼과 불일치")
        print("     3) DM 발송일(7/31)이 7월 마지막날 → 8월 실적은 202608부터")
        print()
        print("  → 코드 수정 전 데이터 존재 여부 재확인 필요")
    elif has_dm_product:
        print("  ✅ 추천 상품 실제 구매 확인됨 → 수정 후 집계 가능")
        print("     수정 내용: dm_only_sent 로그에서 created_at → action_ym 사용")
    else:
        print("  ⚠️  DM 이후 전체 매출은 있으나, 추천상품 구매는 확인 안 됨")
        print("     → 전체 매출 기준 전환율 집계는 수정 후 가능")
        print("     → 추천상품 구체적 구매 추적은 추가 개발 필요")

    cur.close()
    conn.close()

except Exception as e:
    print(f"\n[ERROR] {e}")
    import traceback; traceback.print_exc()
