"""브랜드 매출 차이를 상품/거래처 단위로 분해한다."""
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
YM = "202605"
SAMPLES = [("0000801070", "차백도 본사"), ("0000800012", "얌샘(본사)"), ("0000189103", "나래F＆B")]


def run(sql: str):
    return main.run_query(sql, raw=True)


out = {"yearmonth": YM, "samples": {}}
for zc, name in SAMPLES:
    # 새 뷰의 동일 ZC에서, 기존에는 다른 ZC였던 고객을 찾는다.
    reassigned = run(f"""
        WITH old_cust AS (
            SELECT `거래처`, MAX(`ZC본부`) AS old_zc, MAX(`ZC본부명`) AS old_name,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS old_sales
            FROM {OLD}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}'
            GROUP BY `거래처`
        ), new_cust AS (
            SELECT `거래처`, MAX(`ZC본부`) AS new_zc, MAX(`ZC본부명`) AS new_name,
                   MAX(`거래처명`) AS customer_name,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS new_sales
            FROM {NEW}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{zc}'
            GROUP BY `거래처`
        )
        SELECT n.`거래처`, n.customer_name, o.old_zc, o.old_name, n.new_zc, n.new_name,
               o.old_sales, n.new_sales, COALESCE(n.new_sales,0)-COALESCE(o.old_sales,0) AS diff
        FROM new_cust n LEFT JOIN old_cust o ON n.`거래처`=o.`거래처`
        WHERE COALESCE(o.old_zc, '') <> n.new_zc
        ORDER BY ABS(COALESCE(n.new_sales,0)-COALESCE(o.old_sales,0)) DESC
        LIMIT 15
    """)
    # 동일 ZC 내 상품 집계 차이. 원천 fact 차이인지 확인.
    product_diff = run(f"""
        WITH old_prod AS (
            SELECT `자재` AS material_code, MAX(`자재명`) AS material_name,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS old_sales
            FROM {OLD}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{zc}'
            GROUP BY `자재`
        ), new_prod AS (
            SELECT `자재` AS material_code, MAX(`자재명`) AS material_name,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS new_sales
            FROM {NEW}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{zc}'
            GROUP BY `자재`
        )
        SELECT COALESCE(o.material_code,n.material_code) AS material_code,
               COALESCE(o.material_name,n.material_name) AS material_name,
               o.old_sales, n.new_sales,
               COALESCE(n.new_sales,0)-COALESCE(o.old_sales,0) AS diff
        FROM old_prod o FULL OUTER JOIN new_prod n ON o.material_code=n.material_code
        ORDER BY ABS(COALESCE(n.new_sales,0)-COALESCE(o.old_sales,0)) DESC
        LIMIT 10
    """)
    out["samples"][name] = {"reassigned_customers": reassigned, "top_product_differences": product_diff}

print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
