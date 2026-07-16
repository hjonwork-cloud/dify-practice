"""기존 ZC본부와 신규 FC본부 기반 ZC본부의 브랜드 귀속 차이 샘플 분석."""
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
# 기존/신규 양쪽에서 차이가 큰 브랜드 가운데 대표 표본.
SAMPLES = [
    ("0000801070", "차백도 본사"),
    ("0000800012", "얌샘(본사)"),
    ("0000189103", "나래F＆B"),
]


def run(sql: str):
    return main.run_query(sql, raw=True)


result = {"yearmonth": YM, "samples": {}}
for old_zc, label in SAMPLES:
    # 기존 브랜드로 잡힌 거래처가 신규 뷰에서는 어느 FC/ZC로 귀속되는지
    old_to_new = run(f"""
        WITH old_customers AS (
            SELECT DISTINCT `거래처`
            FROM {OLD}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}'
              AND `ZC본부`='{old_zc}'
        )
        SELECT n.`ZC본부` AS new_zc, MAX(n.`ZC본부명`) AS new_zc_name,
               COUNT(DISTINCT n.`거래처`) AS customer_count,
               ROUND(SUM(CAST(n.`매출액` AS DOUBLE)), 0) AS new_sales
        FROM {NEW} n
        INNER JOIN old_customers o ON n.`거래처`=o.`거래처`
        WHERE n.`사업부명`='외식식재사업부' AND n.`년월`='{YM}'
        GROUP BY n.`ZC본부`
        ORDER BY new_sales DESC
        LIMIT 15
    """)
    # 기존 ZC와 신규 ZC가 동일한 고객들 중 고객마스터 FC 값 확인
    master = run(f"""
        WITH old_customers AS (
            SELECT DISTINCT `거래처`
            FROM {OLD}
            WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}'
              AND `ZC본부`='{old_zc}'
        )
        SELECT LPAD(CAST(m.`고객코드` AS STRING), 10, '0') AS customer_code,
               m.`고객명`, m.`FC본부`, m.`FC본부명`, m.`ZP본사`,
               m.`ZA대표거래처`, m.`ZB본부`
        FROM h_hmfo_fsi.gd_rst_ing.sap_zsdrxd03_customer_master_rst_ing_d m
        INNER JOIN old_customers o
          ON LPAD(CAST(m.`고객코드` AS STRING), 10, '0')=o.`거래처`
        ORDER BY customer_code
        LIMIT 10
    """)
    old_summary = run(f"""
        SELECT MAX(`ZC본부명`) AS old_zc_name, COUNT(DISTINCT `거래처`) AS customer_count,
               ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS old_sales
        FROM {OLD}
        WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{old_zc}'
    """)
    new_same_code = run(f"""
        SELECT MAX(`ZC본부명`) AS new_zc_name, COUNT(DISTINCT `거래처`) AS customer_count,
               ROUND(SUM(CAST(`매출액` AS DOUBLE)), 0) AS new_sales
        FROM {NEW}
        WHERE `사업부명`='외식식재사업부' AND `년월`='{YM}' AND `ZC본부`='{old_zc}'
    """)
    result["samples"][label] = {
        "old_zc": old_zc,
        "old_summary": old_summary,
        "new_same_zc_summary": new_same_code,
        "old_customers_to_new_zc": old_to_new,
        "customer_master_samples": master,
    }

print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
