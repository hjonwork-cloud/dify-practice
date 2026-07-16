"""상품코드 선행 0 차이를 제거한 뒤 브랜드 매출 원천 차이를 확인한다."""
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
SAMPLES = [("0000801070", "차백도 본사"), ("0000800012", "얌샘(본사)")]

out = {"yearmonth": YM, "samples": {}}
for zc, name in SAMPLES:
    rows = main.run_query(f"""
        WITH old_prod AS (
            SELECT regexp_replace(`자재`, '^0+', '') AS material_key,
                   MAX(`자재명`) AS material_name,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS old_sales
            FROM {OLD}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{zc}'
            GROUP BY regexp_replace(`자재`, '^0+', '')
        ), new_prod AS (
            SELECT regexp_replace(`자재`, '^0+', '') AS material_key,
                   MAX(`자재명`) AS material_name,
                   ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS new_sales
            FROM {NEW}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{zc}'
            GROUP BY regexp_replace(`자재`, '^0+', '')
        )
        SELECT COALESCE(o.material_key,n.material_key) AS material_key,
               COALESCE(o.material_name,n.material_name) AS material_name,
               o.old_sales, n.new_sales,
               COALESCE(n.new_sales,0)-COALESCE(o.old_sales,0) AS diff
        FROM old_prod o FULL OUTER JOIN new_prod n ON o.material_key=n.material_key
        WHERE COALESCE(n.new_sales,0) <> COALESCE(o.old_sales,0)
        ORDER BY ABS(COALESCE(n.new_sales,0)-COALESCE(o.old_sales,0)) DESC
        LIMIT 15
    """, raw=True)
    out["samples"][name] = rows
print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
