"""기존/신규 미출고 원천의 스키마와 최신 데이터 샘플 확인."""
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

OLD = "h_hmfo.gd_dcube.`46_helo_periodic_unshipped`"
NEW = "h_hmfo_fsi.gd_fsi_ent.helo_periodic_unshipped_hist_f"

result = {}
for label, table in (("old", OLD), ("new", NEW)):
    result[f"{label}_schema"] = main.run_query(f"DESCRIBE {table}", raw=True)
    result[f"{label}_sample"] = main.run_query(f"SELECT * FROM {table} LIMIT 3", raw=True)
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
