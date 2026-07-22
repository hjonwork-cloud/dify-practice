"""
대시보드 요약 테이블 사전 계산 스크립트
─────────────────────────────────────────────────────────────────────
실행 방법:
  1. Databricks Job 스케줄러 (권장): 매일 오전 6시 등 데이터 갱신 후 실행
  2. 관리자 엔드포인트 호출: POST /portal/admin/refresh-dashboard
  3. 로컬/서버 직접 실행: python portal_refresh.py

생성 테이블:
  - portal_emp_dashboard  : 직원별 지표 요약 (1 row per emp_code)
  - portal_emp_brands     : 직원별 브랜드 목록 (N rows per emp_code)

이 테이블을 읽으면 /portal/dashboard-data 응답이 < 1초로 단축됨.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 요약 테이블이 저장될 Databricks 카탈로그/스키마 ──────────────────────
# 기존 T_MAIN 과 같은 카탈로그 사용 (쓰기 권한 필요)
_CATALOG = "h_hmfo_fsi_dm"
_SCHEMA  = "gd_rst_ing"
T_DASH   = f"{_CATALOG}.{_SCHEMA}.portal_emp_dashboard"
T_BRANDS = f"{_CATALOG}.{_SCHEMA}.portal_emp_brands"


def run_refresh(force: bool = False) -> dict:
    """
    대시보드 요약 테이블 전체 재계산.
    force=True 이면 스로틀링 무시하고 즉시 실행.
    반환: {"status": "ok"|"skipped", "elapsed_sec": float, "emp_count": int}
    """
    import main

    T_MAIN   = main.T_MAIN
    T_PROFIT = main.T_PROFIT
    T_AR     = main.T_AR

    start = time.time()

    # ─── 1. 최신 년월 확보 ────────────────────────────────────────────────
    latest_rows = main._safe_query(
        f"SELECT MAX(`년월`) AS ym FROM {T_MAIN} WHERE `매출액` IS NOT NULL",
        raw=True,
    )
    latest_ym = str((latest_rows[0] or {}).get("ym") or "") if latest_rows else ""
    if not latest_ym:
        return {"status": "error", "reason": "latest_ym 없음"}

    profit_rows = main._safe_query(
        f"SELECT DATE_FORMAT(MAX(`날짜`), 'yyyyMM') AS ym FROM {T_PROFIT}",
        raw=True,
    )
    profit_ym = str((profit_rows[0] or {}).get("ym") or "") if profit_rows else ""

    logger.info(f"[refresh] latest_ym={latest_ym}, profit_ym={profit_ym}")

    # ─── 2. 직원별 지표 한 번에 계산 ────────────────────────────────────
    # compat 뷰의 매출액은 / 10,000 → 백만원 단위 (portal_router._money_m 기준)
    # AR 테이블은 원 단위이므로 / 1,000,000 → 백만원
    _zc8 = "LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'"

    summary_sql = f"""
    CREATE OR REPLACE TABLE {T_DASH} AS
    WITH
    -- 1) 현월 범위
    base AS (
        SELECT
            `영업사원`                AS emp_code,
            `지점명`                  AS team_name,
            `ZC본부`                  AS zc_code,
            `거래처`                  AS cust_code,
            `매출액`                  AS sales_raw
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
          AND `사업부명` = '외식식재사업부'
    ),
    -- 2) 영업사원별 집계
    emp_summary AS (
        SELECT
            emp_code,
            MAX(team_name)                                    AS team_name,
            ROUND(SUM(sales_raw) / 10000)                    AS sales_m,
            COUNT(DISTINCT CASE WHEN {_zc8} THEN zc_code END) AS brand_count,
            COUNT(DISTINCT CASE WHEN {_zc8} THEN cust_code END) AS franchise_count,
            COUNT(DISTINCT CASE WHEN NOT ({_zc8}) THEN cust_code END) AS general_count,
            COUNT(DISTINCT cust_code)                         AS customer_count
        FROM base
        GROUP BY emp_code
    ),
    -- 3) 최신 대금청구일
    bill_date AS (
        SELECT `영업사원` AS emp_code,
               MAX(`청구일`) AS latest_bill_date
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
          AND `사업부명` = '외식식재사업부'
          AND `청구일` IS NOT NULL
        GROUP BY `영업사원`
    ),
    -- 4) CM 공헌이익률 (profit_ym 기준)
    {f"""
    cm_base AS (
        SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_trimmed,
               `공헌이익`, `FI매출액`
        FROM {T_PROFIT}
        WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}'
    ),
    emp_customers AS (
        SELECT DISTINCT
            `영업사원` AS emp_code,
            TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_trimmed
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
    ),
    cm_emp AS (
        SELECT ec.emp_code,
               CASE WHEN SUM(cb.`FI매출액`) = 0 THEN 0
                    ELSE ROUND(SUM(cb.`공헌이익`) / SUM(cb.`FI매출액`) * 100, 1)
               END AS cm_rate
        FROM emp_customers ec
        JOIN cm_base cb ON ec.cust_trimmed = cb.cust_trimmed
        GROUP BY ec.emp_code
    ),
    """ if profit_ym else "cm_emp AS (SELECT '' AS emp_code, 0.0 AS cm_rate WHERE 1=0),"}
    -- 5) 미수채권
    ar_emp AS (
        SELECT a.`영업사원` AS emp_code,
               ROUND(SUM(a.`현재잔액`) / 1000000) AS ar_balance_m
        FROM {T_AR} a
        WHERE a.`년월` = '{latest_ym}'
        GROUP BY a.`영업사원`
    )
    SELECT
        e.emp_code,
        e.team_name,
        '{latest_ym}'   AS latest_ym,
        '{profit_ym}'   AS profit_ym,
        COALESCE(bd.latest_bill_date, '') AS latest_bill_date,
        e.sales_m,
        e.brand_count,
        e.franchise_count,
        e.general_count,
        e.customer_count,
        COALESCE(cm.cm_rate, 0.0)     AS cm_rate,
        COALESCE(ar.ar_balance_m, 0)  AS ar_balance_m,
        CURRENT_TIMESTAMP()           AS updated_at
    FROM emp_summary e
    LEFT JOIN bill_date   bd ON e.emp_code = bd.emp_code
    LEFT JOIN cm_emp      cm ON e.emp_code = cm.emp_code
    LEFT JOIN ar_emp      ar ON e.emp_code = ar.emp_code
    """

    # ─── 3. 직원별 브랜드 목록 ──────────────────────────────────────────
    brands_sql = f"""
    CREATE OR REPLACE TABLE {T_BRANDS} AS
    WITH
    all_brands AS (
        SELECT
            `ZC본부`    AS brand_code,
            `ZC본부명`  AS brand_name,
            COUNT(DISTINCT `거래처`)          AS customer_count,
            ROUND(SUM(`매출액`) / 10000)      AS sales_m
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
          AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL
          AND LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'
        GROUP BY `ZC본부`, `ZC본부명`
    ),
    my_brands AS (
        SELECT
            `영업사원`  AS emp_code,
            `ZC본부`    AS brand_code,
            `ZC본부명`  AS brand_name,
            COUNT(DISTINCT `거래처`)          AS my_customer_count,
            ROUND(SUM(`매출액`) / 10000)      AS my_sales_m
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
          AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL
          AND LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'
        GROUP BY `영업사원`, `ZC본부`, `ZC본부명`
    ),
    generic_ratio AS (
        SELECT
            `영업사원`  AS emp_code,
            `ZC본부`    AS brand_code,
            CASE WHEN SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) = 0 THEN 0
                 ELSE ROUND(
                     SUM(CASE WHEN COALESCE(`자재그룹명`, '') <> 'FC전용상품'
                                  AND `자재그룹명` IS NOT NULL
                              THEN `매출액` ELSE 0 END)
                     / SUM(CASE WHEN `자재그룹명` IS NOT NULL THEN `매출액` ELSE 0 END) * 100, 1)
            END AS generic_ratio
        FROM {T_MAIN}
        WHERE `년월` = '{latest_ym}'
          AND `사업부명` = '외식식재사업부'
          AND `ZC본부` IS NOT NULL
          AND LEFT(TRIM(LEADING '0' FROM TRIM(CAST(`ZC본부` AS STRING))), 1) = '8'
        GROUP BY `영업사원`, `ZC본부`
    )
    {f"""
    , cm_brands AS (
        SELECT
            ec.emp_code,
            ec.brand_code,
            CASE WHEN SUM(cb.`FI매출액`) = 0 THEN NULL
                 ELSE ROUND(SUM(cb.`공헌이익`) / SUM(cb.`FI매출액`) * 100, 1)
            END AS cm_rate
        FROM (
            SELECT DISTINCT `영업사원` AS emp_code,
                            `ZC본부`   AS brand_code,
                            TRIM(LEADING '0' FROM CAST(`거래처` AS STRING)) AS cust_trimmed
            FROM {T_MAIN}
            WHERE `년월` = '{latest_ym}'
        ) ec
        JOIN (
            SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_trimmed,
                   `공헌이익`, `FI매출액`
            FROM {T_PROFIT}
            WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}'
        ) cb ON ec.cust_trimmed = cb.cust_trimmed
        GROUP BY ec.emp_code, ec.brand_code
    )
    """ if profit_ym else ", cm_brands AS (SELECT '' AS emp_code, '' AS brand_code, CAST(NULL AS DOUBLE) AS cm_rate WHERE 1=0)"}
    SELECT
        mb.emp_code,
        mb.brand_code,
        mb.brand_name,
        COALESCE(ab.customer_count, 0)  AS customer_count,
        COALESCE(ab.sales_m, 0)         AS sales_m,
        mb.my_customer_count,
        mb.my_sales_m,
        COALESCE(gr.generic_ratio, 0.0) AS generic_ratio,
        cm.cm_rate,
        CURRENT_TIMESTAMP()             AS updated_at
    FROM my_brands mb
    LEFT JOIN all_brands     ab ON mb.brand_code = ab.brand_code
    LEFT JOIN generic_ratio  gr ON mb.emp_code = gr.emp_code AND mb.brand_code = gr.brand_code
    LEFT JOIN cm_brands      cm ON mb.emp_code = cm.emp_code AND mb.brand_code = cm.brand_code
    ORDER BY mb.emp_code, COALESCE(ab.sales_m, 0) DESC
    """

    # ─── 4. 팀 리더 행 추가 (팀 전체 집계) ─────────────────────────────
    # 리더는 emp_code 자체가 없으면 team_name 기준으로 별도 집계
    leaders_sql = f"""
    INSERT INTO {T_DASH}
    WITH
    leader_map(emp_code, team_name) AS (
        VALUES
          ('20115003', '외식1팀'),
          ('20065782', '외식3팀'),
          ('20145012', '외식2팀'),
          ('20135653', '영남지점')
    ),
    base AS (
        SELECT t.emp_code,
               t.team_name,
               m.`ZC본부`   AS zc_code,
               m.`거래처`   AS cust_code,
               m.`매출액`   AS sales_raw
        FROM {T_MAIN} m
        JOIN leader_map t ON m.`지점명` = t.team_name
        WHERE m.`년월` = '{latest_ym}'
          AND m.`사업부명` = '외식식재사업부'
    ),
    agg AS (
        SELECT emp_code, team_name,
               ROUND(SUM(sales_raw) / 10000)                     AS sales_m,
               COUNT(DISTINCT CASE WHEN LEFT(TRIM(LEADING '0' FROM TRIM(CAST(zc_code AS STRING))),1)='8' THEN zc_code END)   AS brand_count,
               COUNT(DISTINCT CASE WHEN LEFT(TRIM(LEADING '0' FROM TRIM(CAST(zc_code AS STRING))),1)='8' THEN cust_code END) AS franchise_count,
               COUNT(DISTINCT CASE WHEN LEFT(TRIM(LEADING '0' FROM TRIM(CAST(zc_code AS STRING))),1)<>'8' THEN cust_code END) AS general_count,
               COUNT(DISTINCT cust_code)                          AS customer_count
        FROM base
        GROUP BY emp_code, team_name
    ),
    bill AS (
        SELECT t.emp_code, MAX(m.`청구일`) AS latest_bill_date
        FROM {T_MAIN} m
        JOIN leader_map t ON m.`지점명` = t.team_name
        WHERE m.`년월` = '{latest_ym}' AND m.`청구일` IS NOT NULL
        GROUP BY t.emp_code
    ),
    {f"""
    cm_leader AS (
        SELECT t.emp_code,
               CASE WHEN SUM(p.`FI매출액`)=0 THEN 0
                    ELSE ROUND(SUM(p.`공헌이익`)/SUM(p.`FI매출액`)*100, 1)
               END AS cm_rate
        FROM (
            SELECT DISTINCT lm.emp_code,
                   TRIM(LEADING '0' FROM CAST(m.`거래처` AS STRING)) AS cust_trimmed
            FROM {T_MAIN} m
            JOIN leader_map lm ON m.`지점명` = lm.team_name
            WHERE m.`년월` = '{latest_ym}'
        ) t
        JOIN (
            SELECT TRIM(LEADING '0' FROM CAST(`고객` AS STRING)) AS cust_trimmed,
                   `공헌이익`, `FI매출액`
            FROM {T_PROFIT}
            WHERE DATE_FORMAT(`날짜`, 'yyyyMM') = '{profit_ym}'
        ) p ON t.cust_trimmed = p.cust_trimmed
        GROUP BY t.emp_code
    ),
    """ if profit_ym else "cm_leader AS (SELECT '' AS emp_code, 0.0 AS cm_rate WHERE 1=0),"}
    ar_leader AS (
        SELECT t.emp_code, ROUND(SUM(a.`현재잔액`)/1000000) AS ar_balance_m
        FROM {T_AR} a
        JOIN (
            SELECT DISTINCT lm.emp_code, m.`영업사원`
            FROM {T_MAIN} m
            JOIN leader_map lm ON m.`지점명` = lm.team_name
            WHERE m.`년월` = '{latest_ym}'
        ) t ON a.`영업사원` = t.`영업사원`
        WHERE a.`년월` = '{latest_ym}'
        GROUP BY t.emp_code
    )
    SELECT
        a.emp_code, a.team_name,
        '{latest_ym}'  AS latest_ym,
        '{profit_ym}'  AS profit_ym,
        COALESCE(b.latest_bill_date, '') AS latest_bill_date,
        a.sales_m, a.brand_count, a.franchise_count, a.general_count, a.customer_count,
        COALESCE(cm.cm_rate, 0.0)    AS cm_rate,
        COALESCE(ar.ar_balance_m, 0) AS ar_balance_m,
        CURRENT_TIMESTAMP()          AS updated_at
    FROM agg a
    LEFT JOIN bill       b  ON a.emp_code = b.emp_code
    LEFT JOIN cm_leader  cm ON a.emp_code = cm.emp_code
    LEFT JOIN ar_leader  ar ON a.emp_code = ar.emp_code
    """

    # ─── 5. 실행 ─────────────────────────────────────────────────────────
    logger.info(f"[refresh] 대시보드 요약 테이블 생성 시작 (latest_ym={latest_ym})")

    try:
        main._safe_query(summary_sql, raw=True)
        logger.info(f"[refresh] {T_DASH} 생성 완료")
    except Exception as e:
        logger.error(f"[refresh] {T_DASH} 생성 실패: {e}")
        return {"status": "error", "reason": str(e), "step": "emp_dashboard"}

    try:
        main._safe_query(brands_sql, raw=True)
        logger.info(f"[refresh] {T_BRANDS} 생성 완료")
    except Exception as e:
        logger.error(f"[refresh] {T_BRANDS} 생성 실패: {e}")
        return {"status": "error", "reason": str(e), "step": "emp_brands"}

    try:
        main._safe_query(leaders_sql, raw=True)
        logger.info(f"[refresh] 팀 리더 행 INSERT 완료")
    except Exception as e:
        logger.warning(f"[refresh] 팀 리더 행 INSERT 실패 (무시): {e}")

    # ─── 6. 완료 후 인메모리 캐시 초기화 ────────────────────────────────
    try:
        import portal_router
        portal_router._cache_clear_all()
    except Exception:
        pass

    elapsed = round(time.time() - start, 1)
    count_rows = main._safe_query(f"SELECT COUNT(*) AS n FROM {T_DASH}", raw=True)
    emp_count = int((count_rows[0] or {}).get("n") or 0) if count_rows else 0

    logger.info(f"[refresh] 완료: {emp_count}명, {elapsed}초 소요")
    return {"status": "ok", "latest_ym": latest_ym, "profit_ym": profit_ym,
            "emp_count": emp_count, "elapsed_sec": elapsed}


def read_dashboard_from_table(emp_code: str) -> dict | None:
    """
    요약 테이블에서 대시보드 데이터 조회.
    테이블이 없거나 해당 emp_code 행이 없으면 None 반환 → fallback 처리.
    """
    import main
    try:
        rows = main._safe_query(
            f"SELECT * FROM {T_DASH} WHERE emp_code = '{emp_code}' LIMIT 1",
            raw=True,
        )
        if not rows:
            return None
        row = rows[0]

        brand_rows = main._safe_query(
            f"SELECT * FROM {T_BRANDS} WHERE emp_code = '{emp_code}' ORDER BY sales_m DESC LIMIT 50",
            raw=True,
        )

        from portal_router import _is_team_leader, _leader_team
        is_leader = _is_team_leader(emp_code)
        team_name = str(row.get("team_name") or "")

        brands = []
        for b in (brand_rows or []):
            brands.append({
                "brand_code":       str(b.get("brand_code") or ""),
                "brand_name":       str(b.get("brand_name") or ""),
                "customer_count":   int(b.get("customer_count") or 0),
                "sales_m":          int(b.get("sales_m") or 0),
                "my_customer_count":int(b.get("my_customer_count") or 0),
                "my_sales_m":       int(b.get("my_sales_m") or 0),
                "generic_ratio":    float(b.get("generic_ratio") or 0),
                "cm_rate":          (round(float(b["cm_rate"]), 1)
                                     if b.get("cm_rate") is not None else None),
            })

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
            "is_leader":        is_leader,
            "team_name":        team_name,
            "_source":          "precomputed",  # 디버그용
        }
    except Exception as e:
        logger.warning(f"[refresh] 요약 테이블 조회 실패 ({emp_code}): {e}")
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    result = run_refresh(force=True)
    print(result)
    sys.exit(0 if result.get("status") == "ok" else 1)
