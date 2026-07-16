"""챗봇 매출 테이블 이관 적합성 검증: 기존 테이블과 FSI 호환 뷰 비교."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
spec = importlib.util.spec_from_file_location("chatbot_main", ROOT / "api" / "main.py")
if spec is None or spec.loader is None:
    raise RuntimeError("api/main.py 모듈을 불러올 수 없습니다.")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

OLD = "h_hmfo.gd_dcube.`01_sap_sales_custmasters`"
NEW = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"
MONTHS = "'202604', '202605'"


def query(sql: str):
    return main.run_query(sql, raw=True)


def main_review():
    result = {}

    result["period_coverage"] = {
        "old": query(f"SELECT MIN(`년월`) AS min_ym, MAX(`년월`) AS max_ym, COUNT(DISTINCT `년월`) AS ym_count FROM {OLD}"),
        "new": query(f"SELECT MIN(`년월`) AS min_ym, MAX(`년월`) AS max_ym, COUNT(DISTINCT `년월`) AS ym_count FROM {NEW}"),
    }

    result["master_duplicate_customer_codes"] = query("""
        SELECT COUNT(*) AS duplicate_code_count,
               COALESCE(SUM(row_count), 0) AS duplicate_row_count
        FROM (
            SELECT `고객코드`, COUNT(*) AS row_count
            FROM h_hmfo_fsi.gd_rst_ing.sap_zsdrxd03_customer_master_rst_ing_d
            GROUP BY `고객코드`
            HAVING COUNT(*) > 1
        ) d
    """)

    result["monthly_foodservice"] = {
        "old": query(f"""
            SELECT `년월`, COUNT(*) AS row_count,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS sales_raw,
                   ROUND(SUM(CAST(`매출수량` AS DOUBLE)), 2) AS qty,
                   COUNT(DISTINCT `거래처`) AS customer_count,
                   COUNT(DISTINCT `ZB본지점`) AS store_count,
                   COUNT(DISTINCT `ZC본부`) AS brand_count
            FROM {OLD}
            WHERE `사업부명` = '외식식재사업부' AND `년월` IN ({MONTHS})
            GROUP BY `년월` ORDER BY `년월`
        """),
        "new": query(f"""
            SELECT `년월`, COUNT(*) AS row_count,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS sales_raw,
                   ROUND(SUM(CAST(`매출수량` AS DOUBLE)), 2) AS qty,
                   COUNT(DISTINCT `거래처`) AS customer_count,
                   COUNT(DISTINCT `ZB본지점`) AS store_count,
                   COUNT(DISTINCT `ZC본부`) AS brand_count
            FROM {NEW}
            WHERE `사업부명` = '외식식재사업부' AND `년월` IN ({MONTHS})
            GROUP BY `년월` ORDER BY `년월`
        """),
    }

    for ym in ("202604", "202605"):
        result[f"top_brand_sales_differences_{ym}"] = query(f"""
            WITH old_sales AS (
                SELECT `ZC본부`, MAX(`ZC본부명`) AS old_name,
                       ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS old_sales
                FROM {OLD}
                WHERE `사업부명`='외식식재사업부' AND `년월`='{ym}'
                GROUP BY `ZC본부`
            ), new_sales AS (
                SELECT `ZC본부`, MAX(`ZC본부명`) AS new_name,
                       ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS new_sales
                FROM {NEW}
                WHERE `사업부명`='외식식재사업부' AND `년월`='{ym}'
                GROUP BY `ZC본부`
            )
            SELECT COALESCE(o.`ZC본부`, n.`ZC본부`) AS zc_code,
                   o.old_name, n.new_name, o.old_sales, n.new_sales,
                   COALESCE(n.new_sales, 0) - COALESCE(o.old_sales, 0) AS sales_diff
            FROM old_sales o FULL OUTER JOIN new_sales n ON o.`ZC본부` = n.`ZC본부`
            ORDER BY ABS(COALESCE(n.new_sales, 0) - COALESCE(o.old_sales, 0)) DESC
            LIMIT 20
        """)

    result["new_view_required_column_null_rates_202604"] = query(f"""
        SELECT
            COUNT(*) AS row_count,
            ROUND(100.0 * SUM(CASE WHEN `ZC본부명` IS NULL OR TRIM(`ZC본부명`) = '' THEN 1 ELSE 0 END) / COUNT(*), 4) AS zc_name_blank_pct,
            ROUND(100.0 * SUM(CASE WHEN `자재명` IS NULL OR TRIM(`자재명`) = '' THEN 1 ELSE 0 END) / COUNT(*), 4) AS material_name_blank_pct,
            ROUND(100.0 * SUM(CASE WHEN `자재그룹명` IS NULL OR TRIM(`자재그룹명`) = '' THEN 1 ELSE 0 END) / COUNT(*), 4) AS material_group_blank_pct,
            ROUND(100.0 * SUM(CASE WHEN `기존자재번호` IS NULL OR TRIM(`기존자재번호`) = '' THEN 1 ELSE 0 END) / COUNT(*), 4) AS legacy_material_no_blank_pct,
            ROUND(100.0 * SUM(CASE WHEN `영업사원명` IS NULL OR TRIM(`영업사원명`) = '' THEN 1 ELSE 0 END) / COUNT(*), 4) AS salesperson_blank_pct
        FROM {NEW}
        WHERE `사업부명`='외식식재사업부' AND `년월`='202604'
    """)

    result["brand_name_mapping_difference_202604"] = query(f"""
        WITH old_names AS (
            SELECT `ZC본부`, MAX(`ZC본부명`) AS old_name
            FROM {OLD}
            WHERE `사업부명`='외식식재사업부' AND `년월`='202604'
            GROUP BY `ZC본부`
        ), new_names AS (
            SELECT `ZC본부`, MAX(`ZC본부명`) AS new_name
            FROM {NEW}
            WHERE `사업부명`='외식식재사업부' AND `년월`='202604'
            GROUP BY `ZC본부`
        )
        SELECT
            COUNT(*) AS common_zc_count,
            SUM(CASE WHEN COALESCE(o.old_name, '') = COALESCE(n.new_name, '') THEN 1 ELSE 0 END) AS exact_name_match_count,
            SUM(CASE WHEN COALESCE(o.old_name, '') <> COALESCE(n.new_name, '') THEN 1 ELSE 0 END) AS name_mismatch_count
        FROM old_names o INNER JOIN new_names n ON o.`ZC본부`=n.`ZC본부`
    """)

    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main_review()
