"""신규 미출고 호환 뷰와 챗봇 조회 SQL 스모크 테스트."""
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

signals_spec = importlib.util.spec_from_file_location("action_signals", ROOT / "api" / "action_signals.py")
if signals_spec is None or signals_spec.loader is None:
    raise RuntimeError("api/action_signals.py 모듈을 불러올 수 없습니다.")
action_signals = importlib.util.module_from_spec(signals_spec)
signals_spec.loader.exec_module(action_signals)

T = main.T_MISULGO
queries = {
    "latest_date": f"SELECT MAX(`출고일자`) AS latest_date FROM {T}",
    "salesperson_unshipped": f"""
        SELECT `출고일자`, `통합배송처명`, `플랜트`, `플랜트명`, `상품코드`, `상품명`, `미출수량`,
               `미출사유명`, `귀책사유`, `주문미출내용`, `영업담당자명`
        FROM {T}
        WHERE `출고일자`=(SELECT MAX(`출고일자`) FROM {T})
          AND REPLACE(`영업담당자명`, ' ', '') LIKE '%이충규%'
          AND `미출수량` > 0
        ORDER BY `귀책사유` DESC, `통합배송처명`
        LIMIT 10
    """,
    "team_unshipped": f"""
        SELECT `출고일자`, `부서명`, `영업담당자명`, `통합배송처명`, `상품명`, `미출수량`,
               `미출사유명`, `귀책사유`, `주문미출내용`
        FROM {T}
        WHERE `출고일자`=(SELECT MAX(`출고일자`) FROM {T})
          AND REPLACE(`부서명`, ' ', '') LIKE '%외식3팀%'
          AND `미출수량` > 0
        LIMIT 10
    """,
    "brand_unshipped": f"""
        SELECT `ZC본부명`, COUNT(*) AS cnt, COUNT(DISTINCT `거래처명`) AS shop_cnt,
               SUM(`미출고수량`) AS total_qty
        FROM {action_signals.T_MISULGO}
        WHERE `ZC본부명` LIKE '%생활맥주%'
        GROUP BY `ZC본부명`
        ORDER BY total_qty DESC
    """,
}
result = {"main_table": main.T_MISULGO, "action_table": action_signals.T_MISULGO}
for name, sql in queries.items():
    result[name] = main.run_query(sql, raw=True)
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
