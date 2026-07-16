"""Stage 1: core chatbot-sales view smoke tests."""
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
T = main.T_MAIN

queries = {
    "latest_month": f"SELECT MAX(`년월`) AS latest_ym FROM {T}",
    "foodservice_monthly_sales": f"""
        SELECT `년월`, ROUND(SUM(`매출액`)/1000000, 2) AS sales_eok
        FROM {T}
        WHERE `사업부명`='외식식재사업부' AND `년월` IN ('202605','202606','202607')
        GROUP BY `년월` ORDER BY `년월`
    """,
    "salady_brand_lookup": f"""
        SELECT `ZC본부명`, ROUND(SUM(`매출액`)/1000000, 2) AS sales_eok
        FROM {T}
        WHERE `사업부명`='외식식재사업부' AND `지점명`='외식3팀'
          AND `년월`='202604' AND `ZC본부명` LIKE '%샐러디%'
        GROUP BY `ZC본부명` ORDER BY sales_eok DESC
    """,
    "salesperson_lookup": f"""
        SELECT `영업사원명`, ROUND(SUM(`매출액`)/1000000, 2) AS sales_eok
        FROM {T}
        WHERE `사업부명`='외식식재사업부' AND `년월`='202607'
        GROUP BY `영업사원명` ORDER BY sales_eok DESC LIMIT 5
    """,
    "product_lookup": f"""
        SELECT `자재명`, ROUND(SUM(`매출액`)/1000000, 2) AS sales_eok
        FROM {T}
        WHERE `사업부명`='외식식재사업부' AND `년월`='202607'
        GROUP BY `자재명` ORDER BY sales_eok DESC LIMIT 5
    """,
}
results = {name: main.run_query(sql, raw=True) for name, sql in queries.items()}
print(json.dumps({"table": T, "results": results}, ensure_ascii=False, default=str, indent=2))
