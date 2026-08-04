"""
대시보드 요약 테이블 사전 계산 모듈.
- run_refresh(): Azure 서버에서 main._safe_query 사용
- read_dashboard_from_table(): 요약 테이블 단순 SELECT
- read_brand_report_from_table(): 브랜드 리포트 사전계산 데이터 조회
테이블:
  h_hmfo_fsi_dm.gd_rst_ing.portal_emp_dashboard          (직원별 지표)
  h_hmfo_fsi_dm.gd_rst_ing.portal_emp_brands             (직원별 브랜드)
  h_hmfo_fsi_dm.gd_rst_ing.portal_brand_monthly          (브랜드 월별 매출)
  h_hmfo_fsi_dm.gd_rst_ing.portal_brand_summary          (브랜드 요약 통계)
  h_hmfo_fsi_dm.gd_rst_ing.portal_brand_customer_stats   (가맹점별 범용비중)
"""
from __future__ import annotations
import logging, time
logger = logging.getLogger(__name__)

T_DASH          = "h_hmfo_fsi_dm.gd_rst_ing.portal_emp_dashboard"
T_BRANDS        = "h_hmfo_fsi_dm.gd_rst_ing.portal_emp_brands"
T_BRAND_MONTHLY  = "h_hmfo_fsi_dm.gd_rst_ing.portal_brand_monthly"
T_BRAND_SUMMARY  = "h_hmfo_fsi_dm.gd_rst_ing.portal_brand_summary"
T_BRAND_CUST     = "h_hmfo_fsi_dm.gd_rst_ing.portal_brand_customer_stats"
T_ACTION_RESULTS = "h_hmfo_fsi_dm.gd_rst_ing.portal_action_results"


def _shift_ym(ym: str, months: int) -> str:
    """YM 문자열 (예: '202606') 을 N개월 이동."""
    if not ym or len(ym) < 6:
        return ym
    y, m = int(ym[:4]), int(ym[4:6])
    m += months
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return f"{y}{m:02d}"


def run_refresh(force: bool = False) -> dict:
    """요약 테이블 재계산 (서버 내부 호출용)."""
    import main
    import access_control as _ac
    T_MAIN   = main.T_MAIN
    T_PROFIT = main.T_PROFIT
    T_AR     = main.T_AR
    _admin_code = _ac.ADMIN_EMP_CODE
    _auth_dept  = _ac.AUTH_DEPT
    start = time.time()

    rows = main._safe_query(f"SELECT MAX(`년월`) AS ym FROM {T_MAIN} WHERE `매출액` IS NOT NULL", raw=True)
    latest_ym = str((rows[0] or {}).get("ym") or "") if rows else ""
    if not latest_ym:
        return {"status": "error", "reason": "latest_ym 없음"}

    rows = main._safe_query(f"SELECT DATE_FORMAT(MAX(`날짜`), 'yyyyMM') AS ym FROM {T_PROFIT}", raw=True)
    profit_ym = str((rows[0] or {}).get("ym") or "") if rows else ""

    _zc8  = "LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'"
    _zc8a = "LEFT(TRIM(LEADING '0' FROM TRIM(CAST(zc_code AS STRING))), 1) = '8'"
    # LIKE 조건: (FC)영남지점 등 prefix 변형 포함
    _leaders_where = "(`지점명` LIKE '%외식1팀%' OR `지점명` LIKE '%외식3팀%' OR `지점명` LIKE '%외식2팀%' OR `지점명` LIKE '%영남지점%')"
    _leaders_in    = "'외식1팀','외식3팀','외식2팀','영남지점'"   # 사전계산 CASE fallback 용
    _leader_case   = """CASE
               WHEN `지점명` LIKE '%외식1팀%'  THEN '20115003'
               WHEN `지점명` LIKE '%외식3팀%'  THEN '20065782'
               WHEN `지점명` LIKE '%외식2팀%'  THEN '20145012'
               WHEN `지점명` LIKE '%영남지점%' THEN '20135653'
           END"""

    cm_cte = f"""
    cm_base AS (
        SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_t,
               `공헌이익`, `FI매출액`
        FROM {T_PROFIT} WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}'
    ),
    emp_cust AS (
        SELECT DISTINCT `영업사원` AS emp_code,
               TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_t
        FROM {T_MAIN} WHERE `년월` = '{latest_ym}'
    ),
    cm_emp AS (
        SELECT ec.emp_code,
               CASE WHEN SUM(cb.`FI매출액`)=0 THEN 0
                    ELSE ROUND(SUM(cb.`공헌이익`)/SUM(cb.`FI매출액`)*100,1)
               END AS cm_rate
        FROM emp_cust ec JOIN cm_base cb ON ec.cust_t = cb.cust_t
        GROUP BY ec.emp_code
    ),
    leader_cust AS (
        SELECT DISTINCT {_leader_case} AS emp_code,
               TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_t
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND {_leaders_where}
    ),
    cm_leader AS (
        SELECT lc.emp_code,
               CASE WHEN SUM(cb.`FI매출액`)=0 THEN 0
                    ELSE ROUND(SUM(cb.`공헌이익`)/SUM(cb.`FI매출액`)*100,1)
               END AS cm_rate
        FROM leader_cust lc JOIN cm_base cb ON lc.cust_t = cb.cust_t
        WHERE lc.emp_code IS NOT NULL
        GROUP BY lc.emp_code
    ),""" if profit_ym else \
    """cm_emp    AS (SELECT CAST(NULL AS STRING) AS emp_code, CAST(0 AS DOUBLE) AS cm_rate WHERE 1=0),
    cm_leader AS (SELECT CAST(NULL AS STRING) AS emp_code, CAST(0 AS DOUBLE) AS cm_rate WHERE 1=0),"""

    dash_sql = f"""
    CREATE OR REPLACE TABLE {T_DASH} AS
    WITH
    base AS (
        SELECT `영업사원` AS emp_code, `지점명` AS team_name,
               `ZC본부` AS zc_code, `거래처` AS cust_code, `매출액` AS sales_raw
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
    ),
    leader_map(emp_code, team_name) AS (
        VALUES ('20115003','외식1팀'),('20065782','외식3팀'),
               ('20145012','외식2팀'),('20135653','영남지점')
    ),
    emp_agg AS (
        SELECT emp_code, MAX(team_name) AS team_name,
               ROUND(SUM(sales_raw)/10000) AS sales_m,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN zc_code END) AS brand_count,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN cust_code END) AS franchise_count,
               COUNT(DISTINCT CASE WHEN NOT ({_zc8a}) THEN cust_code END) AS general_count,
               COUNT(DISTINCT cust_code) AS customer_count
        FROM base
        WHERE emp_code NOT IN (SELECT emp_code FROM leader_map)
        GROUP BY emp_code
    ),
    bill AS (
        SELECT `영업사원` AS emp_code, MAX(`대금청구일`) AS latest_bill_date
        FROM {T_MAIN} WHERE `년월` = '{latest_ym}' AND `대금청구일` IS NOT NULL
        GROUP BY `영업사원`
    ),
    {cm_cte}
    ar_emp AS (
        SELECT `영업사원` AS emp_code, ROUND(SUM(`현재잔액`)/1000000) AS ar_balance_m
        FROM {T_AR} WHERE `년월` = '{latest_ym}' GROUP BY `영업사원`
    ),
    leader_base AS (
        SELECT lm.emp_code, lm.team_name,
               m.`ZC본부` AS zc_code, m.`거래처` AS cust_code, m.`매출액` AS sales_raw
        FROM {T_MAIN} m JOIN leader_map lm ON m.`지점명` LIKE CONCAT('%', lm.team_name, '%')
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
    admin_agg AS (
        SELECT '{_admin_code}' AS emp_code, '{_auth_dept}' AS team_name,
               ROUND(SUM(sales_raw)/10000) AS sales_m,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN zc_code END) AS brand_count,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN cust_code END) AS franchise_count,
               COUNT(DISTINCT CASE WHEN NOT ({_zc8a}) THEN cust_code END) AS general_count,
               COUNT(DISTINCT cust_code) AS customer_count
        FROM base
    ),
    all_emp AS (SELECT * FROM emp_agg UNION ALL SELECT * FROM leader_agg UNION ALL SELECT * FROM admin_agg)
    SELECT e.emp_code, e.team_name,
           '{latest_ym}' AS latest_ym, '{profit_ym}' AS profit_ym,
           COALESCE(CAST(b.latest_bill_date AS STRING), '') AS latest_bill_date,
           e.sales_m, e.brand_count, e.franchise_count, e.general_count, e.customer_count,
           COALESCE(lcm.cm_rate, cm.cm_rate, 0.0) AS cm_rate,
           COALESCE(ar.ar_balance_m, 0) AS ar_balance_m,
           CURRENT_TIMESTAMP() AS updated_at
    FROM all_emp e
    LEFT JOIN bill      b   ON e.emp_code = b.emp_code
    LEFT JOIN cm_emp    cm  ON e.emp_code = cm.emp_code
    LEFT JOIN cm_leader lcm ON e.emp_code = lcm.emp_code
    LEFT JOIN ar_emp    ar  ON e.emp_code = ar.emp_code
    """

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
            UNION ALL
            SELECT DISTINCT {_leader_case} AS emp_code, `ZC본부` AS brand_code,
                   TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_t
            FROM {T_MAIN} WHERE `년월` = '{latest_ym}' AND {_leaders_where}
        ) ec
        JOIN (SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_t,
                     `공헌이익`, `FI매출액`
              FROM {T_PROFIT} WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}') cb
        ON ec.cust_t = cb.cust_t
        WHERE ec.emp_code IS NOT NULL
        GROUP BY ec.emp_code, ec.brand_code
    )""" if profit_ym else \
    ", cm_brand AS (SELECT CAST(NULL AS STRING) AS emp_code, CAST(NULL AS STRING) AS brand_code, CAST(NULL AS DOUBLE) AS cm_rate WHERE 1=0)"

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
    my_b_emp AS (
        SELECT `영업사원` AS emp_code, `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               COUNT(DISTINCT `거래처`) AS my_customer_count,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8}
        GROUP BY `영업사원`, `ZC본부`, `ZC본부명`
    ),
    my_b_leader AS (
        SELECT {_leader_case} AS emp_code,
               `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               COUNT(DISTINCT `거래처`) AS my_customer_count,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8} AND {_leaders_where}
        GROUP BY `지점명`, `ZC본부`, `ZC본부명`
    ),
    my_b AS (
        SELECT * FROM my_b_emp
        UNION ALL
        SELECT * FROM my_b_leader WHERE emp_code IS NOT NULL
    ),
    gr_emp AS (
        SELECT `영업사원` AS emp_code, `ZC본부` AS brand_code,
               CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)=0 THEN 0
                    ELSE ROUND(SUM(CASE WHEN COALESCE(`자재그룹명`,'') <> 'FC전용상품' AND `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)
                        / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)*100, 1)
               END AS generic_ratio
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8}
        GROUP BY `영업사원`, `ZC본부`
    ),
    gr_leader AS (
        SELECT {_leader_case} AS emp_code, `ZC본부` AS brand_code,
               CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)=0 THEN 0
                    ELSE ROUND(SUM(CASE WHEN COALESCE(`자재그룹명`,'') <> 'FC전용상품' AND `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)
                        / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END)*100, 1)
               END AS generic_ratio
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8} AND {_leaders_where}
        GROUP BY `지점명`, `ZC본부`
    ),
    gr AS (SELECT * FROM gr_emp UNION ALL SELECT * FROM gr_leader WHERE emp_code IS NOT NULL)
    {cm_brand_cte}
    , gen_all AS (
        SELECT ROUND(SUM(`매출액`)/10000) AS sales_m,
               COUNT(DISTINCT `거래처`) AS customer_count
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8}))
    )
    , gen_emp AS (
        SELECT `영업사원` AS emp_code,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m,
               COUNT(DISTINCT `거래처`) AS my_customer_count
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8}))
        GROUP BY `영업사원`
    )
    , gen_leader AS (
        SELECT {_leader_case} AS emp_code,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m,
               COUNT(DISTINCT `거래처`) AS my_customer_count
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8})) AND {_leaders_where}
        GROUP BY `지점명`
    )
    , gen_b AS (
        SELECT * FROM gen_emp
        UNION ALL SELECT * FROM gen_leader WHERE emp_code IS NOT NULL
        UNION ALL SELECT '{_admin_code}' AS emp_code, sales_m AS my_sales_m, customer_count AS my_customer_count FROM gen_all
    )
    , admin_b AS (
        SELECT '{_admin_code}' AS emp_code, brand_code, brand_name, customer_count AS my_customer_count, sales_m AS my_sales_m
        FROM all_b
    )
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
    UNION ALL
    SELECT gb.emp_code, '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
           COALESCE(ga.customer_count, 0) AS customer_count,
           COALESCE(ga.sales_m, 0) AS sales_m,
           gb.my_customer_count, gb.my_sales_m,
           0.0 AS generic_ratio, CAST(NULL AS DOUBLE) AS cm_rate,
           CURRENT_TIMESTAMP() AS updated_at
    FROM gen_b gb CROSS JOIN gen_all ga
    UNION ALL
    SELECT adb.emp_code, adb.brand_code, adb.brand_name,
           COALESCE(ab3.customer_count, 0) AS customer_count,
           COALESCE(ab3.sales_m, 0) AS sales_m,
           adb.my_customer_count, adb.my_sales_m,
           0.0 AS generic_ratio, CAST(NULL AS DOUBLE) AS cm_rate,
           CURRENT_TIMESTAMP() AS updated_at
    FROM admin_b adb LEFT JOIN all_b ab3 ON adb.brand_code = ab3.brand_code
    """

    try:
        main._safe_query(dash_sql, raw=True)
        logger.info(f"[refresh] {T_DASH} 생성 완료")
    except Exception as e:
        return {"status": "error", "reason": str(e), "step": "emp_dashboard"}

    try:
        main._safe_query(brands_sql, raw=True)
        logger.info(f"[refresh] {T_BRANDS} 생성 완료")
    except Exception as e:
        return {"status": "error", "reason": str(e), "step": "emp_brands"}

    # ── Step 3: 브랜드 월별 매출 (T_BRAND_MONTHLY) ─────────────────────────
    six_months_ago = _shift_ym(latest_ym, -5)  # 최근 6개월
    brand_monthly_sql = f"""
        CREATE OR REPLACE TABLE {T_BRAND_MONTHLY} AS
        WITH
        emp_real AS (
                SELECT `영업사원` AS emp_code,
                             `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
                             `년월` AS ym, ROUND(SUM(`매출액`)/10000) AS sales_m
                FROM {main.T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                    AND `ZC본부` IS NOT NULL AND {_zc8}
                    AND `년월` >= '{six_months_ago}'
                GROUP BY `영업사원`, `ZC본부`, `ZC본부명`, `년월`
        ),
        leader_real AS (
                SELECT {_leader_case} AS emp_code,
                             `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
                             `년월` AS ym, ROUND(SUM(`매출액`)/10000) AS sales_m
                FROM {main.T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                    AND `ZC본부` IS NOT NULL AND {_zc8}
                    AND `년월` >= '{six_months_ago}'
                    AND {_leaders_where}
                GROUP BY `지점명`, `ZC본부`, `ZC본부명`, `년월`
        ),
        admin_real AS (
                SELECT '{_admin_code}' AS emp_code,
                             `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
                             `년월` AS ym, ROUND(SUM(`매출액`)/10000) AS sales_m
                FROM {main.T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                    AND `ZC본부` IS NOT NULL AND {_zc8}
                    AND `년월` >= '{six_months_ago}'
                GROUP BY `ZC본부`, `ZC본부명`, `년월`
        ),
        emp_gen AS (
                SELECT `영업사원` AS emp_code,
                             '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
                             `년월` AS ym, ROUND(SUM(`매출액`)/10000) AS sales_m
                FROM {main.T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                    AND (`ZC본부` IS NULL OR NOT ({_zc8}))
                    AND `년월` >= '{six_months_ago}'
                GROUP BY `영업사원`, `년월`
        ),
        leader_gen AS (
                SELECT {_leader_case} AS emp_code,
                             '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
                             `년월` AS ym, ROUND(SUM(`매출액`)/10000) AS sales_m
                FROM {main.T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                    AND (`ZC본부` IS NULL OR NOT ({_zc8}))
                    AND `년월` >= '{six_months_ago}'
                    AND {_leaders_where}
                GROUP BY `지점명`, `년월`
        ),
        admin_gen AS (
                SELECT '{_admin_code}' AS emp_code,
                             '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
                             `년월` AS ym, ROUND(SUM(`매출액`)/10000) AS sales_m
                FROM {main.T_MAIN}
                WHERE `사업부명` = '외식식재사업부'
                    AND (`ZC본부` IS NULL OR NOT ({_zc8}))
                    AND `년월` >= '{six_months_ago}'
                GROUP BY `년월`
        )
        SELECT emp_code, brand_code, brand_name, ym, sales_m, CURRENT_TIMESTAMP() AS updated_at FROM emp_real
        UNION ALL
        SELECT emp_code, brand_code, brand_name, ym, sales_m, CURRENT_TIMESTAMP() AS updated_at FROM leader_real WHERE emp_code IS NOT NULL
        UNION ALL
        SELECT emp_code, brand_code, brand_name, ym, sales_m, CURRENT_TIMESTAMP() AS updated_at FROM admin_real
        UNION ALL
        SELECT emp_code, brand_code, brand_name, ym, sales_m, CURRENT_TIMESTAMP() AS updated_at FROM emp_gen
        UNION ALL
        SELECT emp_code, brand_code, brand_name, ym, sales_m, CURRENT_TIMESTAMP() AS updated_at FROM leader_gen WHERE emp_code IS NOT NULL
        UNION ALL
        SELECT emp_code, brand_code, brand_name, ym, sales_m, CURRENT_TIMESTAMP() AS updated_at FROM admin_gen
        """
    try:
        main._safe_query(brand_monthly_sql, raw=True)
        logger.info(f"[refresh] {T_BRAND_MONTHLY} 생성 완료")
    except Exception as e:
        logger.warning(f"[refresh] {T_BRAND_MONTHLY} 생성 실패 (무시): {e}")

    # ── Step 4: 브랜드 요약 통계 (T_BRAND_SUMMARY) ─────────────────────────
    # 당월+전월 두 달치를 `ym` 컬럼으로 함께 저장 → 프론트 토글 즉시 응답 가능
    prev_ym = _shift_ym(latest_ym, -1)
    _target_yms_in = f"('{latest_ym}','{prev_ym}')"
    # 실브랜드 8종 + 가상 브랜드 '일반외식' 을 emp_code 스코프로 요약한다.
    brand_summary_sql = f"""
    CREATE OR REPLACE TABLE {T_BRAND_SUMMARY} AS
        WITH
        emp_real AS (
                SELECT
                        `영업사원` AS emp_code,
                        `년월` AS ym,
                        `ZC본부` AS brand_code,
                        `ZC본부명` AS brand_name,
                        COUNT(DISTINCT `거래처`) AS customer_count,
                        ROUND(SUM(`매출액`)/10000) AS brand_total_sales_m,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND(
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS brand_avg,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND((
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     - SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN COALESCE(`매출원가`,0) ELSE 0 END)
                                 ) / SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS generic_gp_rate
                FROM {main.T_MAIN}
                WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
                    AND `ZC본부` IS NOT NULL AND {_zc8}
                GROUP BY `영업사원`, `년월`, `ZC본부`, `ZC본부명`
        ),
        leader_real AS (
                SELECT
                        {_leader_case} AS emp_code,
                        `년월` AS ym,
                        `ZC본부` AS brand_code,
                        `ZC본부명` AS brand_name,
                        COUNT(DISTINCT `거래처`) AS customer_count,
                        ROUND(SUM(`매출액`)/10000) AS brand_total_sales_m,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND(
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS brand_avg,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND((
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     - SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN COALESCE(`매출원가`,0) ELSE 0 END)
                                 ) / SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS generic_gp_rate
                FROM {main.T_MAIN}
                WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
                    AND `ZC본부` IS NOT NULL AND {_zc8} AND {_leaders_where}
                GROUP BY `지점명`, `년월`, `ZC본부`, `ZC본부명`
        ),
        admin_real AS (
                SELECT
                        '{_admin_code}' AS emp_code,
                        `년월` AS ym,
                        `ZC본부` AS brand_code,
                        `ZC본부명` AS brand_name,
                        COUNT(DISTINCT `거래처`) AS customer_count,
                        ROUND(SUM(`매출액`)/10000) AS brand_total_sales_m,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND(
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS brand_avg,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND((
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     - SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN COALESCE(`매출원가`,0) ELSE 0 END)
                                 ) / SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS generic_gp_rate
                FROM {main.T_MAIN}
                WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
                    AND `ZC본부` IS NOT NULL AND {_zc8}
                GROUP BY `년월`, `ZC본부`, `ZC본부명`
        ),
        emp_gen AS (
                SELECT
                        `영업사원` AS emp_code,
                        `년월` AS ym,
                        '일반외식' AS brand_code,
                        '🧑‍🍳일반외식업장' AS brand_name,
                        COUNT(DISTINCT `거래처`) AS customer_count,
                        ROUND(SUM(`매출액`)/10000) AS brand_total_sales_m,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND(
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS brand_avg,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND((
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     - SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN COALESCE(`매출원가`,0) ELSE 0 END)
                                 ) / SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS generic_gp_rate
                FROM {main.T_MAIN}
                WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
                    AND (`ZC본부` IS NULL OR NOT ({_zc8}))
                GROUP BY `영업사원`, `년월`
        ),
        leader_gen AS (
                SELECT
                        {_leader_case} AS emp_code,
                        `년월` AS ym,
                        '일반외식' AS brand_code,
                        '🧑‍🍳일반외식업장' AS brand_name,
                        COUNT(DISTINCT `거래처`) AS customer_count,
                        ROUND(SUM(`매출액`)/10000) AS brand_total_sales_m,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND(
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS brand_avg,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND((
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     - SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN COALESCE(`매출원가`,0) ELSE 0 END)
                                 ) / SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS generic_gp_rate
                FROM {main.T_MAIN}
                WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
                    AND (`ZC본부` IS NULL OR NOT ({_zc8})) AND {_leaders_where}
                GROUP BY `지점명`, `년월`
        ),
        admin_gen AS (
                SELECT
                        '{_admin_code}' AS emp_code,
                        `년월` AS ym,
                        '일반외식' AS brand_code,
                        '🧑‍🍳일반외식업장' AS brand_name,
                        COUNT(DISTINCT `거래처`) AS customer_count,
                        ROUND(SUM(`매출액`)/10000) AS brand_total_sales_m,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND(
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS brand_avg,
                        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) = 0 THEN 0.0
                                 ELSE ROUND((
                                         SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
                                     - SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN COALESCE(`매출원가`,0) ELSE 0 END)
                                 ) / SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) * 100, 1)
                        END AS generic_gp_rate
                FROM {main.T_MAIN}
                WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
                    AND (`ZC본부` IS NULL OR NOT ({_zc8}))
                GROUP BY `년월`
        )
        SELECT emp_code, ym, brand_code, brand_name, customer_count, brand_total_sales_m, brand_avg, generic_gp_rate,
                     CURRENT_TIMESTAMP() AS updated_at
        FROM emp_real
        UNION ALL
        SELECT emp_code, ym, brand_code, brand_name, customer_count, brand_total_sales_m, brand_avg, generic_gp_rate,
                     CURRENT_TIMESTAMP() AS updated_at
        FROM leader_real WHERE emp_code IS NOT NULL
        UNION ALL
        SELECT emp_code, ym, brand_code, brand_name, customer_count, brand_total_sales_m, brand_avg, generic_gp_rate,
                     CURRENT_TIMESTAMP() AS updated_at
        FROM admin_real
        UNION ALL
        SELECT emp_code, ym, brand_code, brand_name, customer_count, brand_total_sales_m, brand_avg, generic_gp_rate,
                     CURRENT_TIMESTAMP() AS updated_at
        FROM emp_gen
        UNION ALL
        SELECT emp_code, ym, brand_code, brand_name, customer_count, brand_total_sales_m, brand_avg, generic_gp_rate,
                     CURRENT_TIMESTAMP() AS updated_at
        FROM leader_gen WHERE emp_code IS NOT NULL
        UNION ALL
        SELECT emp_code, ym, brand_code, brand_name, customer_count, brand_total_sales_m, brand_avg, generic_gp_rate,
                     CURRENT_TIMESTAMP() AS updated_at
        FROM admin_gen
    """
    try:
        main._safe_query(brand_summary_sql, raw=True)
        logger.info(f"[refresh] {T_BRAND_SUMMARY} 생성 완료 (yms={latest_ym},{prev_ym})")
    except Exception as e:
        logger.warning(f"[refresh] {T_BRAND_SUMMARY} 생성 실패 (무시): {e}")

    # ── Step 5: 가맹점별 범용비중 (T_BRAND_CUST) ───────────────────────────
    # 당월+전월 두 달치 저장 (ym 컬럼)
    _generic_ratio_expr = """
        CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0.0
             ELSE ROUND(
                 SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END)
               / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
        END"""
    _sales_exprs = f"""
        SUM(`매출액`) AS sales,
        SUM(CASE WHEN COALESCE(`자재그룹명`,'') = 'FC전용상품' THEN `매출액` ELSE 0 END) AS dedicated_sales,
        SUM(CASE WHEN `자재그룹명` IS NOT NULL AND COALESCE(`자재그룹명`,'') <> 'FC전용상품' THEN `매출액` ELSE 0 END) AS generic_sales,
        {_generic_ratio_expr} AS generic_ratio,
        MAX(`플랜트`) AS plant_code"""
    _where_brand_2m = f"""
        WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL AND {_zc8}"""
    _where_gen_2m = f"""
        WHERE `년월` IN {_target_yms_in} AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8}))"""

    brand_cust_sql = f"""
    CREATE OR REPLACE TABLE {T_BRAND_CUST} AS
    WITH
    emp_data AS (
        SELECT `영업사원` AS emp_code, `년월` AS ym,
               `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               `거래처` AS customer_code, MAX(`거래처명`) AS customer_name,
               {_sales_exprs}
        FROM {main.T_MAIN}
        {_where_brand_2m}
        GROUP BY `영업사원`, `년월`, `ZC본부`, `ZC본부명`, `거래처`
        HAVING SUM(`매출액`) > 0
    ),
    leader_data AS (
        SELECT {_leader_case} AS emp_code, `년월` AS ym,
               `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               `거래처` AS customer_code, MAX(`거래처명`) AS customer_name,
               {_sales_exprs}
        FROM {main.T_MAIN}
        {_where_brand_2m} AND {_leaders_where}
        GROUP BY `지점명`, `년월`, `ZC본부`, `ZC본부명`, `거래처`
        HAVING SUM(`매출액`) > 0
    ),
    admin_data AS (
        SELECT '{_admin_code}' AS emp_code, `년월` AS ym,
               `ZC본부` AS brand_code, `ZC본부명` AS brand_name,
               `거래처` AS customer_code, MAX(`거래처명`) AS customer_name,
               {_sales_exprs}
        FROM {main.T_MAIN}
        {_where_brand_2m}
        GROUP BY `년월`, `ZC본부`, `ZC본부명`, `거래처`
        HAVING SUM(`매출액`) > 0
    ),
    -- 가상 브랜드: 일반외식업장 (ZC본부 미분류 or 8대 브랜드 이외)
    gen_emp_data AS (
        SELECT `영업사원` AS emp_code, `년월` AS ym,
               '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
               `거래처` AS customer_code, MAX(`거래처명`) AS customer_name,
               {_sales_exprs}
        FROM {main.T_MAIN}
        {_where_gen_2m}
        GROUP BY `영업사원`, `년월`, `거래처`
        HAVING SUM(`매출액`) > 0
    ),
    gen_leader_data AS (
        SELECT {_leader_case} AS emp_code, `년월` AS ym,
               '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
               `거래처` AS customer_code, MAX(`거래처명`) AS customer_name,
               {_sales_exprs}
        FROM {main.T_MAIN}
        {_where_gen_2m} AND {_leaders_where}
        GROUP BY `지점명`, `년월`, `거래처`
        HAVING SUM(`매출액`) > 0
    ),
    gen_admin_data AS (
        SELECT '{_admin_code}' AS emp_code, `년월` AS ym,
               '일반외식' AS brand_code, '🧑‍🍳일반외식업장' AS brand_name,
               `거래처` AS customer_code, MAX(`거래처명`) AS customer_name,
               {_sales_exprs}
        FROM {main.T_MAIN}
        {_where_gen_2m}
        GROUP BY `년월`, `거래처`
        HAVING SUM(`매출액`) > 0
    )
    SELECT *, CURRENT_TIMESTAMP() AS updated_at FROM emp_data
    UNION ALL
    SELECT *, CURRENT_TIMESTAMP() AS updated_at FROM leader_data WHERE emp_code IS NOT NULL
    UNION ALL
    SELECT *, CURRENT_TIMESTAMP() AS updated_at FROM admin_data
    UNION ALL
    SELECT *, CURRENT_TIMESTAMP() AS updated_at FROM gen_emp_data
    UNION ALL
    SELECT *, CURRENT_TIMESTAMP() AS updated_at FROM gen_leader_data WHERE emp_code IS NOT NULL
    UNION ALL
    SELECT *, CURRENT_TIMESTAMP() AS updated_at FROM gen_admin_data
    """
    try:
        main._safe_query(brand_cust_sql, raw=True)
        logger.info(f"[refresh] {T_BRAND_CUST} 생성 완료 (yms={latest_ym},{prev_ym})")
    except Exception as e:
        logger.warning(f"[refresh] {T_BRAND_CUST} 생성 실패 (무시): {e}")

    # ── Step 6: 판가설정 액션 이후 실적 집계 (T_ACTION_RESULTS) ──────────────
    try:
        ar_result = run_action_results_refresh()
        logger.info(f"[refresh] {T_ACTION_RESULTS} 갱신 완료: {ar_result}")
    except Exception as e:
        logger.warning(f"[refresh] {T_ACTION_RESULTS} 갱신 실패 (무시): {e}")

    try:
        import portal_router
        portal_router._cache_clear_all()
    except Exception:
        pass

    elapsed = round(time.time() - start, 1)
    try:
        n = (main._safe_query(f"SELECT COUNT(*) AS n FROM {T_DASH}", raw=True) or [{}])[0].get("n", 0)
    except Exception:
        n = "?"
    logger.info(f"[refresh] 완료: {n}명, {elapsed}초")
    return {"status": "ok", "latest_ym": latest_ym, "profit_ym": profit_ym,
            "emp_count": n, "elapsed_sec": elapsed}


def read_dashboard_from_table(emp_code: str) -> dict | None:
    """요약 테이블에서 대시보드 데이터 조회. 없으면 None 반환."""
    import main
    try:
        rows = main._safe_query(
            f"SELECT * FROM {T_DASH} WHERE emp_code = '{emp_code}' ORDER BY sales_m DESC LIMIT 1", raw=True)
        if not rows:
            return None
        row = rows[0]
        brand_rows = main._safe_query(
            f"SELECT * FROM {T_BRANDS} WHERE emp_code = '{emp_code}' ORDER BY sales_m DESC LIMIT 200",
            raw=True) or []

        from portal_router import _is_team_leader
        brands = [{
            "brand_code":       str(b.get("brand_code") or ""),
            "brand_name":       str(b.get("brand_name") or ""),
            "customer_count":   int(b.get("customer_count") or 0),
            "sales_m":          int(b.get("sales_m") or 0),
            "my_customer_count":int(b.get("my_customer_count") or 0),
            "my_sales_m":       int(b.get("my_sales_m") or 0),
            "generic_ratio":    float(b.get("generic_ratio") or 0),
            "cm_rate":          (round(float(b["cm_rate"]), 1)
                                 if b.get("cm_rate") is not None else None),
        } for b in brand_rows]

        return {
            "latest_ym":        str(row.get("latest_ym") or ""),
            "latest_bill_date": str(row.get("latest_bill_date") or ""),
            "profit_ym":        str(row.get("profit_ym") or ""),
            "period_months":    [str(row.get("latest_ym") or "")],
            "sales_m":          int(row.get("sales_m") or 0),
            "brand_count":      int(row.get("brand_count") or 0),
            "franchise_count":  int(row.get("franchise_count") or 0),
            "general_count":    int(row.get("general_count") or 0),
            "customer_count":   int(row.get("customer_count") or 0),
            "cm_rate":          float(row.get("cm_rate") or 0),
            "ar_balance_m":     int(row.get("ar_balance_m") or 0),
            "brands":           brands,
            "is_leader":        _is_team_leader(emp_code),
            "team_name":        str(row.get("team_name") or ""),
            "_source":          "precomputed",
        }
    except Exception as e:
        logger.warning(f"[refresh] 요약 테이블 조회 실패 ({emp_code}): {e}")
        return None


def read_brand_report_from_table(
    emp_code: str,
    brand_name: str,
    threshold_pct: float | None = None,
    customer_page: int = 1,
    target_page: int = 1,
    ym_mode: str = "prev",  # "prev"=전월 / "current"=당월
) -> dict | None:
    """
    사전 계산 테이블에서 brand_report() 결과를 조회.
    T_BRAND_SUMMARY + T_BRAND_CUST + T_BRAND_MONTHLY + T_BRANDS 병렬 조회.
    T_BRAND_SUMMARY / T_BRAND_CUST 는 당월+전월 두 달치를 담고 있어 ym_mode 로 즉시 전환.
    데이터 없으면 None 반환 → brand_report() fallback 실시간 처리.
    """
    import main
    from concurrent.futures import ThreadPoolExecutor
    try:
        # ── Phase 1: summary (두 달치) + monthly + brands 병렬 조회 ──
        def _q_summary():
            # 당월/전월 두 행 모두 조회 (ym DESC → [0]=latest, [1]=prev)
            try:
                return main._safe_query(
                    f"SELECT * FROM {T_BRAND_SUMMARY}"
                    f" WHERE emp_code = '{emp_code}' AND brand_name = '{brand_name}'"
                    f" ORDER BY ym DESC LIMIT 2",
                    raw=True,
                ) or []
            except Exception:
                # 구 스키마(emp_code 미존재)에서는 잘못된 글로벌 집계를 반환하지 않도록 fail-close
                return []

        def _q_monthly():
            try:
                return main._safe_query(
                    f"SELECT ym, sales_m FROM {T_BRAND_MONTHLY}"
                    f" WHERE emp_code = '{emp_code}' AND brand_name = '{brand_name}'"
                    f" ORDER BY ym DESC LIMIT 6",
                    raw=True,
                ) or []
            except Exception:
                return []

        def _q_brands():
            return main._safe_query(
                f"SELECT * FROM {T_BRANDS} WHERE emp_code = '{emp_code}' ORDER BY sales_m DESC LIMIT 200",
                raw=True,
            ) or []

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_summary = ex.submit(_q_summary)
            f_monthly = ex.submit(_q_monthly)
            f_brands  = ex.submit(_q_brands)
            summary_rows = f_summary.result()
            monthly_all  = f_monthly.result()
            brands_raw   = f_brands.result()

        # ── 결과 검증 ──────────────────────────────────────────────
        if not summary_rows:
            logger.warning(f"[brand_report_table] summary empty: brand={brand_name} → None")
            return None

        # ── ym 결정: summary 두 행에서 latest/prev 추출 ─────────────
        summary_by_ym = {str(r.get("ym") or ""): r for r in summary_rows}
        yms_desc = sorted(summary_by_ym.keys(), reverse=True)
        latest_ym = yms_desc[0] if yms_desc else ""
        prev_ym   = yms_desc[1] if len(yms_desc) > 1 else _shift_ym(latest_ym, -1)

        mode = (ym_mode or "prev").lower()
        if mode == "current":
            selected_ym = latest_ym
        else:
            mode = "prev"
            selected_ym = prev_ym or latest_ym  # prev 없으면 latest 로 폴백

        s = summary_by_ym.get(selected_ym) or summary_rows[0]
        brand_avg           = float(s.get("brand_avg") or 0)
        generic_gp_rate     = float(s.get("generic_gp_rate") or 0)
        brand_total_sales_m = int(s.get("brand_total_sales_m") or 0)
        brand_code          = str(s.get("brand_code") or "")

        # ── Phase 2: cust (선택된 ym 만) 별도 조회 ─────────────────
        cust_rows = main._safe_query(
            f"SELECT customer_code, customer_name, plant_code,"
            f"       sales, dedicated_sales, generic_sales, generic_ratio"
            f" FROM {T_BRAND_CUST}"
            f" WHERE emp_code = '{emp_code}'"
            f"   AND brand_name = '{brand_name}'"
            f"   AND ym = '{selected_ym}'"
            f" ORDER BY sales DESC",
            raw=True,
        ) or []

        if not cust_rows:
            logger.warning(f"[brand_report_table] cust_rows empty: emp={emp_code} brand={brand_name} ym={selected_ym} → returning empty customer list (no fallback)")

        # ── 월별 매출 가공 (DESC로 받아서 최신 3개월 추출) ───────────
        monthly_all_sorted = sorted(monthly_all, key=lambda r: str(r.get("ym") or ""), reverse=True)
        months_3 = [latest_ym, _shift_ym(latest_ym, -1), _shift_ym(latest_ym, -2)]
        months_3 = [m for m in months_3 if m]
        months_3_set = set(months_3)
        monthly_sales = sorted(
            [{"ym": str(r.get("ym") or ""), "sales_m": int(r.get("sales_m") or 0)}
             for r in monthly_all if str(r.get("ym") or "") in months_3_set],
            key=lambda r: r["ym"],
        )
        # selected_ym 의 월 매출 (카드 표시용)
        selected_month_sales_m = next(
            (int(r.get("sales_m") or 0) for r in monthly_sales if str(r.get("ym")) == selected_ym), 0
        )

        # ── threshold 계산 ─────────────────────────────────────────
        threshold_max = round(max(0.0, brand_avg), 1)
        threshold = round(min(threshold_max, max(0.0, brand_avg if threshold_pct is None else float(threshold_pct))), 1)
        target_ratio = min(0.999, max(0.0, brand_avg / 100.0))

        # ── 고객 목록 조합 ─────────────────────────────────────────
        def _money_m(v) -> int:
            return int(round(float(v or 0) / 10000))

        customers = []
        proposal_possible_raw = 0.0
        for r in cust_rows:
            sales = float(r.get("sales") or 0)
            dedicated = float(r.get("dedicated_sales") or 0)
            generic = float(r.get("generic_sales") or 0)
            classified = max(0.0, dedicated + generic)
            ratio = float(r.get("generic_ratio") or 0)
            is_target = ratio < threshold
            needed = 0.0
            if is_target and target_ratio > 0 and classified > 0:
                needed = max(0.0, (target_ratio * classified - generic) / (1.0 - target_ratio))
                proposal_possible_raw += needed
            customers.append({
                "customer_code":          str(r.get("customer_code") or ""),
                "customer_name":          str(r.get("customer_name") or ""),
                "plant_code":             str(r.get("plant_code") or ""),
                "sales_m":                _money_m(sales),
                "dedicated_sales_m":      _money_m(dedicated),
                "generic_sales_m":        _money_m(generic),
                "generic_ratio":          ratio,
                "dedicated_ratio":        round(max(0.0, 100.0 - ratio), 1),
                "gap":                    round(ratio - brand_avg, 1),
                "is_target":              is_target,
                "proposal_possible_sales_m": _money_m(needed),
            })

        targets = [c for c in customers if c["is_target"]]
        proposal_possible_sales_m = _money_m(proposal_possible_raw)
        expected_profit_increase_m = int(round(proposal_possible_sales_m * (generic_gp_rate / 100.0)))

        def _page(items, page, per=10):
            total = len(items)
            tp = max(1, (total + per - 1) // per)
            p = min(max(1, int(page or 1)), tp)
            s_ = (p - 1) * per
            return items[s_:s_ + per], {
                "page": p, "per_page": per, "total": total, "total_pages": tp,
                "has_prev": p > 1, "has_next": p < tp,
                "prev_page": max(1, p - 1), "next_page": min(tp, p + 1),
                "start": s_ + 1 if total else 0, "end": min(total, s_ + per),
            }

        from portal_router import _is_team_leader, _leader_team
        brands_list = [{
            "brand_code":        str(b.get("brand_code") or ""),
            "brand_name":        str(b.get("brand_name") or ""),
            "customer_count":    int(b.get("customer_count") or 0),
            "sales_m":           int(b.get("sales_m") or 0),
            "my_customer_count": int(b.get("my_customer_count") or 0),
            "my_sales_m":        int(b.get("my_sales_m") or 0),
            "generic_ratio":     float(b.get("generic_ratio") or 0),
            "cm_rate":           (round(float(b["cm_rate"]), 1) if b.get("cm_rate") is not None else None),
        } for b in brands_raw]
        customer_page_items, customer_pagination = _page(customers, customer_page)
        target_page_items, target_pagination = _page(targets, target_page)

        # 내 담당 매출 = T_BRANDS 의 my_sales_m (emp 스코프)
        picked_my_sales_m = int(next(
            (b.get("my_sales_m") for b in brands_list if b.get("brand_code") == brand_code), 0
        ) or 0)
        return {
            "brand": {"brand_code": brand_code, "brand_name": brand_name},
            "brands": brands_list,
            "latest_ym": latest_ym,
            "prev_ym": prev_ym,
            "selected_ym": selected_ym,
            "ym_mode": mode,
            "period_months": months_3,
            "monthly_sales": monthly_sales,
            "brand_avg": brand_avg,
            "brand_total_sales_m": picked_my_sales_m,
            "customers": customers,
            "customer_page": customer_page_items,
            "customer_pagination": customer_pagination,
            "targets": targets,
            "target_page": target_page_items,
            "target_pagination": target_pagination,
            "target_count": len(targets),
            "is_fallback_targets": False,
            "threshold": threshold,
            "threshold_max": threshold_max,
            "proposal_possible_sales_m": proposal_possible_sales_m,
            "generic_gp_rate": generic_gp_rate,
            "expected_profit_increase_m": expected_profit_increase_m,
            "is_leader": _is_team_leader(emp_code),
            "team_name": _leader_team(emp_code),
            "_source": "precomputed",
        }
    except Exception as e:
        logger.warning(f"[refresh] brand_report 사전계산 조회 실패 ({emp_code}, {brand_name}): {e}")
        return None


def run_action_results_refresh() -> dict:
    """
    판가설정 액션 이후 실적 집계 테이블 갱신.
    SQLite dm_send_logs → Databricks T_MAIN JOIN → T_ACTION_RESULTS CREATE OR REPLACE
    """
    import json as _json
    from collections import defaultdict
    try:
        from portal_db import list_dm_logs
        logs = list_dm_logs(limit=5000)
    except Exception as e:
        return {"status": "error", "reason": str(e), "step": "read_logs"}

    # 판가설정 또는 DM 발송 로그 → (emp, customer, brand) 기준으로 가장 이른 action_ym 추출
    agg: dict = {}
    for log in logs:
        pj = log.get("price_items_json") or ""
        action_type = str(log.get("action_type") or "")
        product_names = str(log.get("product_names") or "")

        # action_ym 결정
        action_ym = ""
        if pj and pj not in ("[]", "null"):
            try:
                items = _json.loads(pj)
                if items:
                    dates = [str(it.get("date_from") or "")[:6] for it in items if it.get("date_from")]
                    action_ym = min((d for d in dates if len(d) == 6), default="")
            except Exception:
                pass
        # dm_only_sent: price_items 없음 → created_at 기반 fallback
        if not action_ym:
            action_ym = str(log.get("created_at") or "")[:7].replace("-", "")
        if len(action_ym) != 6 or not action_ym.isdigit():
            continue

        # dm_matnr_csv: price_items matnr 우선, 없으면 product_names 사용
        dm_matnr_csv = ""
        if pj and pj not in ("[]", "null"):
            try:
                items = _json.loads(pj)
                dm_matnr_csv = ",".join(str(it.get("matnr") or "") for it in items if it.get("matnr"))
            except Exception:
                pass
        if not dm_matnr_csv and product_names:
            dm_matnr_csv = product_names.replace(", ", ",").replace(" ", ",")

        key = (
            str(log.get("emp_code") or ""),
            str(log.get("customer_code") or ""),
            str(log.get("brand_code") or ""),
        )
        entry = {
            "emp_code":      key[0],
            "customer_code": key[1],
            "customer_name": str(log.get("customer_name") or "").replace("'", "''"),
            "brand_code":    key[2],
            "brand_name":    str(log.get("brand_name") or "").replace("'", "''"),
            "action_ym":     action_ym,
            "item_count":    len(_json.loads(pj)) if (pj and pj not in ("[]", "null")) else 0,
            "dm_matnr_csv":  dm_matnr_csv.replace("'", ""),
        }
        if key not in agg or action_ym < agg[key]["action_ym"]:
            agg[key] = entry

    import main
    if not agg:
        # 데이터 없으면 빈 테이블 생성
        empty_sql = f"""
        CREATE OR REPLACE TABLE {T_ACTION_RESULTS} AS
        SELECT '' AS emp_code, '' AS customer_code, '' AS customer_name,
               '' AS brand_code, '' AS brand_name, '' AS action_ym,
               0 AS action_item_count, '' AS dm_matnr_csv,
               CAST(0 AS BIGINT) AS sales_after_m, CAST(0 AS BIGINT) AS gp_after_m,
               CAST(0.0 AS DOUBLE) AS gp_rate_after, CAST(0 AS BIGINT) AS generic_sales_after_m,
               CAST(0 AS BIGINT) AS sample_qty, CAST(0 AS BIGINT) AS sample_count,
               CAST(0 AS BIGINT) AS dm_product_qty,
               CURRENT_TIMESTAMP() AS updated_at
        WHERE 1=0
        """
        try:
            main._safe_query(empty_sql, raw=True)
        except Exception as e:
            logger.warning(f"[refresh] {T_ACTION_RESULTS} 빈 테이블 생성 실패: {e}")
        return {"status": "ok", "action_rows": 0}

    rows = list(agg.values())
    values_parts = ", ".join(
        f"('{r['emp_code']}', '{r['customer_code']}', '{r['customer_name']}', "
        f"'{r['brand_code']}', '{r['brand_name']}', '{r['action_ym']}', {r['item_count']}, '{r['dm_matnr_csv']}')"
        for r in rows
    )

    sql = f"""
    CREATE OR REPLACE TABLE {T_ACTION_RESULTS} AS
    WITH actions AS (
      SELECT column1 AS emp_code, column2 AS customer_code, column3 AS customer_name,
             column4 AS brand_code, column5 AS brand_name,
             column6 AS action_ym,  column7 AS action_item_count,
             column8 AS dm_matnr_csv
      FROM VALUES {values_parts}
    )
    SELECT
      a.emp_code, a.customer_code, a.customer_name,
      a.brand_code, a.brand_name, a.action_ym, a.action_item_count, a.dm_matnr_csv,
      ROUND(SUM(COALESCE(m.`매출액`, 0)) / 10000)    AS sales_after_m,
      ROUND((SUM(COALESCE(m.`매출액`, 0)) - SUM(COALESCE(m.`매출원가`, 0))) / 10000) AS gp_after_m,
      CASE WHEN SUM(COALESCE(m.`매출액`, 0)) = 0 THEN 0.0
           ELSE ROUND((SUM(COALESCE(m.`매출액`, 0)) - SUM(COALESCE(m.`매출원가`, 0)))
                      / SUM(COALESCE(m.`매출액`, 0)) * 100, 1)
      END AS gp_rate_after,
      ROUND(SUM(CASE WHEN m.`자재그룹명` IS NOT NULL
                     AND COALESCE(m.`자재그룹명`, '') <> 'FC전용상품'
                     THEN COALESCE(m.`매출액`, 0) ELSE 0 END) / 10000) AS generic_sales_after_m,
      -- 샘플출고: 매출액=0 이고 수량>0 인 행 (추천 상품 샘플 출고 확인)
      COALESCE(SUM(CASE WHEN COALESCE(m.`매출액`, 0) = 0
                             AND COALESCE(m.`매출수량`, 0) > 0
                        THEN CAST(m.`매출수량` AS BIGINT) ELSE 0 END), 0) AS sample_qty,
      COALESCE(SUM(CASE WHEN COALESCE(m.`매출액`, 0) = 0
                             AND COALESCE(m.`매출수량`, 0) > 0
                        THEN 1 ELSE 0 END), 0) AS sample_count,
      -- DM 추천 상품 구매 수량 (상품코드가 dm_matnr_csv 내에 있는 경우)
      COALESCE(SUM(CASE WHEN a.dm_matnr_csv <> ''
                             AND ARRAY_CONTAINS(SPLIT(a.dm_matnr_csv, ','), m.`상품코드`)
                        THEN CAST(m.`매출수량` AS BIGINT) ELSE 0 END), 0) AS dm_product_qty,
      CURRENT_TIMESTAMP() AS updated_at
    FROM actions a
    LEFT JOIN {main.T_MAIN} m
           ON m.`거래처`  = a.customer_code
          AND m.`ZC본부`  = a.brand_code
          AND m.`년월`    >= a.action_ym
    GROUP BY a.emp_code, a.customer_code, a.customer_name,
             a.brand_code, a.brand_name, a.action_ym, a.action_item_count, a.dm_matnr_csv
    """
    try:
        main._safe_query(sql, raw=True)
        logger.info(f"[refresh] {T_ACTION_RESULTS} 생성 완료 ({len(rows)}건)")
        return {"status": "ok", "action_rows": len(rows)}
    except Exception as e:
        logger.warning(f"[refresh] {T_ACTION_RESULTS} 생성 실패: {e}")
        return {"status": "error", "reason": str(e), "step": "create_table"}


def read_action_results(emp_code: str, brand_code: str = "", action_ym: str = "") -> list[dict]:
    """T_ACTION_RESULTS에서 액션 실적 조회."""
    import main
    _emp = str(emp_code or "").strip()
    # emp_code 필터: SQLite 저장값과 세션값 불일치 대비 → LIKE 병행
    conds = [f"(emp_code = '{_emp}' OR emp_code LIKE '%{_emp}%' OR '{_emp}' LIKE CONCAT('%', emp_code, '%'))"]
    if brand_code:
        conds.append(f"brand_code = '{brand_code}'")
    if action_ym:
        conds.append(f"action_ym >= '{action_ym}'")
    where = " AND ".join(conds)
    try:
        rows = main._safe_query(
            f"SELECT * FROM {T_ACTION_RESULTS} WHERE {where} ORDER BY action_ym DESC LIMIT 200",
            raw=True,
        ) or []
        # emp_code 필터 결과 없으면 전체 조회로 fallback (첫 사용자 등)
        if not rows:
            logger.info(f"[read_action_results] emp_code={_emp} 필터 결과 없음, 전체 조회 fallback")
            fb_conds = []
            if brand_code:
                fb_conds.append(f"brand_code = '{brand_code}'")
            if action_ym:
                fb_conds.append(f"action_ym >= '{action_ym}'")
            fb_where = " AND ".join(fb_conds) if fb_conds else "1=1"
            rows = main._safe_query(
                f"SELECT * FROM {T_ACTION_RESULTS} WHERE {fb_where} ORDER BY action_ym DESC LIMIT 200",
                raw=True,
            ) or []
        return [
            {
                "customer_code":         str(r.get("customer_code") or ""),
                "customer_name":         str(r.get("customer_name") or ""),
                "brand_code":            str(r.get("brand_code") or ""),
                "brand_name":            str(r.get("brand_name") or ""),
                "action_ym":             str(r.get("action_ym") or ""),
                "action_item_count":     int(r.get("action_item_count") or 0),
                "sales_after_m":         int(r.get("sales_after_m") or 0),
                "gp_after_m":            int(r.get("gp_after_m") or 0),
                "gp_rate_after":         float(r.get("gp_rate_after") or 0),
                "generic_sales_after_m": int(r.get("generic_sales_after_m") or 0),
                "sample_qty":            int(r.get("sample_qty") or 0),
                "sample_count":          int(r.get("sample_count") or 0),
                "dm_product_qty":        int(r.get("dm_product_qty") or 0),
                "dm_matnr_csv":          str(r.get("dm_matnr_csv") or ""),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[refresh] read_action_results 실패: {e}")
        return []