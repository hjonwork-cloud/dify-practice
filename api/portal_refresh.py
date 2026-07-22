"""
대시보드 요약 테이블 사전 계산 모듈.
- run_refresh(): Azure 서버에서 main._safe_query 사용
- read_dashboard_from_table(): 요약 테이블 단순 SELECT
테이블:
  h_hmfo_fsi_dm.gd_rst_ing.portal_emp_dashboard   (직원별 지표)
  h_hmfo_fsi_dm.gd_rst_ing.portal_emp_brands       (직원별 브랜드)
"""
from __future__ import annotations
import logging, time
logger = logging.getLogger(__name__)

T_DASH   = "h_hmfo_fsi_dm.gd_rst_ing.portal_emp_dashboard"
T_BRANDS = "h_hmfo_fsi_dm.gd_rst_ing.portal_emp_brands"


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
    _leaders_in  = "'외식1팀','외식3팀','외식2팀','영남지점'"
    _leader_case = """CASE `지점명`
               WHEN '외식1팀'  THEN '20115003'
               WHEN '외식3팀'  THEN '20065782'
               WHEN '외식2팀'  THEN '20145012'
               WHEN '영남지점' THEN '20135653'
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
        WHERE `년월` = '{latest_ym}' AND `지점명` IN ({_leaders_in})
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
    emp_agg AS (
        SELECT emp_code, MAX(team_name) AS team_name,
               ROUND(SUM(sales_raw)/10000) AS sales_m,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN zc_code END) AS brand_count,
               COUNT(DISTINCT CASE WHEN {_zc8a} THEN cust_code END) AS franchise_count,
               COUNT(DISTINCT CASE WHEN NOT ({_zc8a}) THEN cust_code END) AS general_count,
               COUNT(DISTINCT cust_code) AS customer_count
        FROM base GROUP BY emp_code
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
            FROM {T_MAIN} WHERE `년월` = '{latest_ym}' AND `지점명` IN ({_leaders_in})
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
          AND `ZC본부` IS NOT NULL AND {_zc8} AND `지점명` IN ({_leaders_in})
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
          AND `ZC본부` IS NOT NULL AND {_zc8} AND `지점명` IN ({_leaders_in})
        GROUP BY `지점명`, `ZC본부`
    ),
    gr AS (SELECT * FROM gr_emp UNION ALL SELECT * FROM gr_leader WHERE emp_code IS NOT NULL)
    {cm_brand_cte}
    , gen_all AS (
        SELECT ROUND(SUM(`매출액`)/10000) AS sales_m,
               COUNT(DISTINCT `거래체`) AS customer_count
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8}))
    )
    , gen_emp AS (
        SELECT `영업사원` AS emp_code,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m,
               COUNT(DISTINCT `거래체`) AS my_customer_count
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8}))
        GROUP BY `영업사원`
    )
    , gen_leader AS (
        SELECT {_leader_case} AS emp_code,
               ROUND(SUM(`매출액`)/10000) AS my_sales_m,
               COUNT(DISTINCT `거래체`) AS my_customer_count
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}' AND `사업부명` = '외식식재사업부'
          AND (`ZC본부` IS NULL OR NOT ({_zc8})) AND `지점명` IN ({_leaders_in})
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
            f"SELECT * FROM {T_DASH} WHERE emp_code = '{emp_code}' LIMIT 1", raw=True)
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