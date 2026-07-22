"""
portal_refresh 독립 실행 스크립트
- main.py / fastapi 없이 Databricks에 직접 연결
- 요약 테이블 2개 생성
"""
import os, sys, time, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HOST      = "https://adb-707807361397497.17.azuredatabricks.net"
HTTP_PATH = "/sql/1.0/warehouses/acc2ec933ffef2d0"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".token_cache")

T_MAIN   = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"
T_PROFIT = "h_hmfo.gd_dcube.`00_customers_cm`"
T_AR     = "h_hmfo_fsi.gd_rst_ing.sap_zfird015_monthly_accounts_receivable_history_rst_ing_f"
T_DASH   = "h_hmfo_fsi_dm.gd_rst_ing.portal_emp_dashboard"
T_BRANDS = "h_hmfo_fsi_dm.gd_rst_ing.portal_emp_brands"

def get_token():
    t = os.getenv("DATABRICKS_TOKEN", "").strip()
    if t:
        return t
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    # 브라우저 인증 (Databricks SDK 필요)
    log.info("저장된 토큰 없음 → 브라우저 인증 시작 (팝업 창에서 로그인)")
    from databricks.sdk import WorkspaceClient
    wc = WorkspaceClient(host=HOST, auth_type="external-browser")
    me = wc.current_user.me()
    log.info(f"로그인: {me.user_name}")
    headers = wc.config.authenticate()
    tok = headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not tok:
        raise RuntimeError("토큰 추출 실패")
    with open(TOKEN_FILE, "w") as f:
        f.write(tok)
    log.info("토큰 저장 완료")
    return tok

def run_sql(conn, sql: str, label: str = ""):
    log.info(f"실행 중: {label or sql[:60]}")
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(sql)
        try:
            rows = cur.fetchall()
            desc = cur.description or []
            cols = [d[0] for d in desc]
            result = [dict(zip(cols, r)) for r in rows]
        except Exception:
            result = []
    log.info(f"  → 완료 ({time.time()-t0:.1f}초), {len(result)}행")
    return result

def main():
    from databricks import sql as dbsql

    token = get_token()
    log.info(f"Databricks 연결 중: {HOST}")
    conn = dbsql.connect(
        server_hostname=HOST.replace("https://",""),
        http_path=HTTP_PATH,
        access_token=token,
    )
    log.info("연결 성공")

    # ── 1. 최신 년월 확인 ──────────────────────────────────────────────
    rows = run_sql(conn, f"SELECT MAX(`년월`) AS ym FROM {T_MAIN} WHERE `매출액` IS NOT NULL", "최신 년월")
    latest_ym = str((rows[0] or {}).get("ym","")) if rows else ""
    if not latest_ym:
        log.error("latest_ym 없음 - 종료")
        sys.exit(1)
    log.info(f"latest_ym = {latest_ym}")

    # ── 2. 최신 profit 년월 ────────────────────────────────────────────
    rows = run_sql(conn, f"SELECT DATE_FORMAT(MAX(`날짜`), 'yyyyMM') AS ym FROM {T_PROFIT}", "profit 년월")
    profit_ym = str((rows[0] or {}).get("ym","")) if rows else ""
    log.info(f"profit_ym = {profit_ym}")

    _zc8 = "LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'"
    _zc8a= "LEFT(TRIM(LEADING '0' FROM TRIM(CAST(zc_code AS STRING))), 1) = '8'"

    # ── 3. portal_emp_dashboard 생성 ──────────────────────────────────
    cm_cte = ""
    if profit_ym:
        cm_cte = f"""
    cm_base AS (
        SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_t,
               `공헌이익`, `FI매출액`
        FROM {T_PROFIT}
        WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}'
    ),
    emp_cust AS (
        SELECT DISTINCT `영업사원` AS emp_code,
               TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_t
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
    ),
    cm_emp AS (
        SELECT ec.emp_code,
               CASE WHEN SUM(cb.`FI매출액`)=0 THEN 0
                    ELSE ROUND(SUM(cb.`공헌이익`)/SUM(cb.`FI매출액`)*100,1)
               END AS cm_rate
        FROM emp_cust ec JOIN cm_base cb ON ec.cust_t = cb.cust_t
        GROUP BY ec.emp_code
    ),"""
    else:
        cm_cte = "cm_emp AS (SELECT CAST(NULL AS STRING) AS emp_code, CAST(0 AS DOUBLE) AS cm_rate WHERE 1=0),"

    dash_sql = f"""
    CREATE OR REPLACE TABLE {T_DASH} AS
    WITH
    base AS (
        SELECT `영업사원` AS emp_code,
               `지점명` AS team_name,
               `ZC본부` AS zc_code,
               `거래처` AS cust_code,
               `매출액` AS sales_raw
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
          AND `사업부명` = '외식식재사업부'
    ),
    emp_agg AS (
        SELECT emp_code,
               MAX(team_name) AS team_name,
               ROUND(SUM(sales_raw)/10000) AS sales_m,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN zc_code END) AS brand_count,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN cust_code END) AS franchise_count,
               COUNT(DISTINCT CASE WHEN NOT ({_zc8a}) THEN cust_code END) AS general_count,
               COUNT(DISTINCT cust_code) AS customer_count
        FROM base
        GROUP BY emp_code
    ),
    bill AS (
        SELECT `영업사원` AS emp_code, MAX(`대금청구일`) AS latest_bill_date
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `대금청구일` IS NOT NULL
        GROUP BY `영업사원`
    ),
    {cm_cte}
    ar_emp AS (
        SELECT `영업사원` AS emp_code,
               ROUND(SUM(`현재잔액`)/1000000) AS ar_balance_m
        FROM {T_AR}
        WHERE `년월` = '{latest_ym}'
        GROUP BY `영업사원`
    ),
    -- 팀 리더 행 (지점명 기준 집계)
    leader_map(emp_code, team_name) AS (
        VALUES ('20115003','외식1팀'),('20065782','외식3팀'),
               ('20145012','외식2팀'),('20135653','영남지점')
    ),
    leader_base AS (
        SELECT lm.emp_code, lm.team_name,
               m.`ZC본부` AS zc_code, m.`거래처` AS cust_code, m.`매출액` AS sales_raw
        FROM {T_MAIN} m JOIN leader_map lm ON m.`지점명` = lm.team_name
        WHERE m.`년월` = '{latest_ym}' AND m.`사업부명` = '외식식재사업부'
    ),
    leader_agg AS (
        SELECT emp_code, MAX(team_name) AS team_name,
               ROUND(SUM(sales_raw)/10000) AS sales_m,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN zc_code END) AS brand_count,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN cust_code END) AS franchise_count,
               COUNT(DISTINCT CASE WHEN NOT ({_zc8a}) THEN cust_code END) AS general_count,
               COUNT(DISTINCT cust_code) AS customer_count
        FROM leader_base GROUP BY emp_code
    ),
    all_emp AS (
        SELECT * FROM emp_agg
        UNION ALL
        SELECT * FROM leader_agg
    )
    SELECT
        e.emp_code,
        e.team_name,
        '{latest_ym}'  AS latest_ym,
        '{profit_ym}'  AS profit_ym,
        COALESCE(CAST(b.latest_bill_date AS STRING), '') AS latest_bill_date,
        e.sales_m,
        e.brand_count,
        e.franchise_count,
        e.general_count,
        e.customer_count,
        COALESCE(cm.cm_rate, 0.0) AS cm_rate,
        COALESCE(ar.ar_balance_m, 0) AS ar_balance_m,
        CURRENT_TIMESTAMP() AS updated_at
    FROM all_emp e
    LEFT JOIN bill   b  ON e.emp_code = b.emp_code
    LEFT JOIN cm_emp cm ON e.emp_code = cm.emp_code
    LEFT JOIN ar_emp ar ON e.emp_code = ar.emp_code
    """

    run_sql(conn, dash_sql, f"CREATE {T_DASH}")

    # ── 4. portal_emp_brands 생성 ─────────────────────────────────────
    cm_brand_cte = ""
    if profit_ym:
        cm_brand_cte = f"""
    , cm_brand AS (
        SELECT ec.emp_code, ec.brand_code,
               CASE WHEN SUM(cb.`FI매출액`)=0 THEN NULL
                    ELSE ROUND(SUM(cb.`공헌이익`)/SUM(cb.`FI매출액`)*100,1)
               END AS cm_rate
        FROM (
            SELECT DISTINCT `영업사원` AS emp_code, `ZC본부` AS brand_code,
                   TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_t
            FROM {T_MAIN} WHERE `년월` = '{latest_ym}'
        ) ec
        JOIN (
            SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_t,
                   `공헌이익`, `FI매출액`
            FROM {T_PROFIT} WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}'
        ) cb ON ec.cust_t = cb.cust_t
        GROUP BY ec.emp_code, ec.brand_code
    )"""
    else:
        cm_brand_cte = ", cm_brand AS (SELECT CAST(NULL AS STRING) AS emp_code, CAST(NULL AS STRING) AS brand_code, CAST(NULL AS DOUBLE) AS cm_rate WHERE 1=0)"

    brands_sql = f"""
    CREATE OR REPLACE TABLE {T_BRANDS} AS
    WITH
    all_b AS (
        SELECT `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               COUNT(DISTINCT `거래처`) AS customer_count,
               ROUND(SUM(`매출액`)/10000) AS sales_m
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8}
        GROUP BY `ZC본부`, `ZC본부명`
    ),
    my_b AS (
        SELECT `영업사원` AS emp_code, `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               COUNT(DISTINCT `거래처`) AS my_customer_count,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8}
        GROUP BY `영업사원`, `ZC본부`, `ZC본부명`
    ),
    gr AS (
        SELECT `영업사원` AS emp_code, `ZC본부` AS brand_code,
               CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)=0 THEN 0
                    ELSE ROUND(
                        SUM(CASE WHEN COALESCE(`자재그룹명`,'') <> 'FC전용상품'
                                      AND `자재그룹명` IS NOT NULL
                                 THEN `매출액` ELSE 0 END)
                        / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)*100, 1)
               END AS generic_ratio
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8}
        GROUP BY `영업사원`, `ZC본부`
    )
    {cm_brand_cte}
    SELECT mb.emp_code, mb.brand_code, mb.brand_name,
           COALESCE(ab.customer_count,0) AS customer_count,
           COALESCE(ab.sales_m,0) AS sales_m,
           mb.my_customer_count, mb.my_sales_m,
           COALESCE(gr.generic_ratio,0.0) AS generic_ratio,
           cm.cm_rate,
           CURRENT_TIMESTAMP() AS updated_at
    FROM my_b mb
    LEFT JOIN all_b ab ON mb.brand_code = ab.brand_code
    LEFT JOIN gr    ON mb.emp_code = gr.emp_code AND mb.brand_code = gr.brand_code
    LEFT JOIN cm_brand cm ON mb.emp_code = cm.emp_code AND mb.brand_code = cm.brand_code
    ORDER BY mb.emp_code, COALESCE(ab.sales_m,0) DESC
    """

    run_sql(conn, brands_sql, f"CREATE {T_BRANDS}")

    # ── 5. 결과 확인 ──────────────────────────────────────────────────
    r1 = run_sql(conn, f"SELECT COUNT(*) AS n FROM {T_DASH}", "row count dashboard")
    r2 = run_sql(conn, f"SELECT COUNT(*) AS n FROM {T_BRANDS}", "row count brands")
    log.info(f"완료 → dashboard {r1[0].get('n')}행, brands {r2[0].get('n')}행")

    conn.close()
    print(f"\n✅ 완료: dashboard {r1[0].get('n')}명, brands {r2[0].get('n')}행")

if __name__ == "__main__":
    main()
