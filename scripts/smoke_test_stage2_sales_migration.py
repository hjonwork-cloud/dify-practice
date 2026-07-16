"""Stage 2: action signals and forecast view migration smoke tests."""
import datetime
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

main_spec = importlib.util.spec_from_file_location("chatbot_main", ROOT / "api" / "main.py")
if main_spec is None or main_spec.loader is None:
    raise RuntimeError("api/main.py 모듈을 불러올 수 없습니다.")
main = importlib.util.module_from_spec(main_spec)
main_spec.loader.exec_module(main)

import action_signals
import forecast_engine_v7

result = {
    "main_table": main.T_MAIN,
    "action_signal_table": action_signals.T_MAIN,
    "forecast_table": forecast_engine_v7.T_MAIN_FE,
}

# Brand lookup confirms the shared ZC name field used by both modules.
result["salady_brand"] = main.run_query(f"""
    SELECT `ZC본부명`, COUNT(DISTINCT `ZB본지점`) AS store_count,
           ROUND(SUM(`매출액`)/1000000, 2) AS sales_eok
    FROM {action_signals.T_MAIN}
    WHERE `사업부명`='외식식재사업부' AND `년월`='202607'
      AND `ZC본부명` LIKE '%샐러디%'
    GROUP BY `ZC본부명` ORDER BY sales_eok DESC
""", raw=True)

# Forecast query function is executed with a current available period.
result["forecast_result"] = forecast_engine_v7.predict_single_brand(
    "샐러디(본사)", datetime.date(2026, 7, 14), lambda sql: main.run_query(sql, raw=True)
)

print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
