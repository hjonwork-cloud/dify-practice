"""수익성 원천의 조직 컬럼과 외식식재사업부 집계 가능 여부 확인."""
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
T = main.T_PROFIT
schema = main.run_query(f"DESCRIBE {T}", raw=True)
org = [r for r in schema if any(k in str(r.get('col_name', '')) for k in ('사업부', '지점', '부서', '날짜'))]
queries = {
    "schema_org_columns": org,
    "latest_by_business": f"""
        SELECT `사업부`, MAX(`날짜`) AS latest_date, COUNT(*) AS row_count
        FROM {T}
        GROUP BY `사업부`
        ORDER BY row_count DESC
        LIMIT 20
    """,
    "foodservice_latest_summary": f"""
        SELECT `사업부`, `지점명`, SUM(`FI매출액`) AS fi, SUM(`공헌이익`) AS cm
        FROM {T}
        WHERE `사업부`='외식식재사업부'
          AND `날짜`=(SELECT MAX(`날짜`) FROM {T})
        GROUP BY `사업부`, `지점명`
        ORDER BY fi DESC
    """,
}
result = {name: main.run_query(sql, raw=True) if name != 'schema_org_columns' else sql for name, sql in queries.items()}
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
