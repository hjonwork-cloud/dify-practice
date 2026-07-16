"""신규 미출고의 통합배송처 ID를 고객마스터에 연결할 수 있는지 확인한다."""
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

UNSHIPPED = "h_hmfo_fsi.gd_fsi_ent.helo_periodic_unshipped_hist_f"
MASTER = "h_hmfo_fsi.gd_rst_ing.sap_zsdrxd03_customer_master_rst_ing_d"
query = f"""
WITH latest AS (
    SELECT * FROM {UNSHIPPED}
    WHERE `출고일자`=(SELECT MAX(`출고일자`) FROM {UNSHIPPED})
      AND `총미출수량` > 0
), mapped AS (
    SELECT u.*, m.`고객명`, m.`FC본부`, m.`FC본부명`
    FROM latest u
    LEFT JOIN {MASTER} m
      ON LPAD(CAST(u.`통합배송처ID` AS STRING), 10, '0')
       = LPAD(CAST(m.`고객코드` AS STRING), 10, '0')
)
SELECT COUNT(*) AS total_rows,
       SUM(CASE WHEN `고객명` IS NOT NULL THEN 1 ELSE 0 END) AS customer_mapped_rows,
       ROUND(100.0 * SUM(CASE WHEN `고객명` IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS mapping_pct,
       SUM(CASE WHEN `FC본부명` IS NOT NULL AND TRIM(`FC본부명`)<>'' THEN 1 ELSE 0 END) AS fc_name_rows
FROM mapped
"""
examples = f"""
SELECT u.`통합배송처ID`, u.`통합배송처명`, m.`고객명`, m.`FC본부`, m.`FC본부명`
FROM {UNSHIPPED} u
LEFT JOIN {MASTER} m
  ON LPAD(CAST(u.`통합배송처ID` AS STRING), 10, '0')
   = LPAD(CAST(m.`고객코드` AS STRING), 10, '0')
WHERE u.`출고일자`=(SELECT MAX(`출고일자`) FROM {UNSHIPPED})
  AND u.`총미출수량` > 0
  AND m.`고객명` IS NOT NULL
LIMIT 10
"""
print(json.dumps({"mapping": main.run_query(query, raw=True), "examples": main.run_query(examples, raw=True)}, ensure_ascii=False, default=str, indent=2))
