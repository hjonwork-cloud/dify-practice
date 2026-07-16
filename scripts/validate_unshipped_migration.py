"""미출고 원천의 최신일·공통 기간 요약을 비교한다."""
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

queries = {
    "date_range": f"""
        SELECT 'old' AS source, MIN(`출고일자`) AS min_date, MAX(`출고일자`) AS max_date,
               COUNT(DISTINCT `출고일자`) AS date_count
        FROM {OLD}
        UNION ALL
        SELECT 'new' AS source, MIN(`출고일자`), MAX(`출고일자`), COUNT(DISTINCT `출고일자`)
        FROM {NEW}
    """,
    "daily_summary_common_period": f"""
        WITH dates AS (
            SELECT `출고일자` FROM {OLD}
            INTERSECT
            SELECT `출고일자` FROM {NEW}
        ), old_daily AS (
            SELECT `출고일자`, COUNT(*) AS row_count, SUM(CAST(`미출수량` AS DOUBLE)) AS qty
            FROM {OLD} WHERE `출고일자` IN (SELECT `출고일자` FROM dates)
            GROUP BY `출고일자`
        ), new_daily AS (
            SELECT `출고일자`, COUNT(*) AS row_count, SUM(CAST(`총미출수량` AS DOUBLE)) AS qty
            FROM {NEW} WHERE `출고일자` IN (SELECT `출고일자` FROM dates)
            GROUP BY `출고일자`
        )
        SELECT o.`출고일자`, o.row_count AS old_rows, n.row_count AS new_rows,
               o.qty AS old_qty, n.qty AS new_qty,
               n.qty-o.qty AS qty_diff
        FROM old_daily o JOIN new_daily n ON o.`출고일자`=n.`출고일자`
        ORDER BY ABS(n.qty-o.qty) DESC
        LIMIT 10
    """,
    "new_latest_foodservice_sample": f"""
        SELECT `출고일자`, `부서명`, `영업담당자명`, `통합배송처명`, `상품명`,
               `총미출수량`, `미출사유명`, `귀책사유`
        FROM {NEW}
        WHERE `부서명` LIKE '%외식%'
          AND `출고일자`=(SELECT MAX(`출고일자`) FROM {NEW})
          AND `총미출수량` > 0
        LIMIT 10
    """,
}
print(json.dumps({name: main.run_query(sql, raw=True) for name, sql in queries.items()}, ensure_ascii=False, default=str, indent=2))
