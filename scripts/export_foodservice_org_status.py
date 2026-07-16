"""Export 외식식재사업부 조직현황 to Excel for a target year-month.

Columns: 사번, 영업사원명, 부서명
Source: h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

import main  # noqa: E402

SOURCE_TABLE = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"
YEAR_MONTH = "202606"
OUTPUT_DIR = ROOT / "exports"
OUTPUT_FILE = OUTPUT_DIR / f"foodservice_org_status_{YEAR_MONTH}.xlsx"

SQL = f"""
WITH grouped AS (
    SELECT
                `영업사원` AS `사번`,
        `영업사원명`,
        `부서명`,
                COUNT(*) AS row_count,
                SUM(CAST(`매출액` AS DOUBLE)) AS sales_amount
    FROM {SOURCE_TABLE}
        WHERE `사업부명` = '외식식재사업부'
            AND `년월` = '{YEAR_MONTH}'
            AND `영업사원` IS NOT NULL
            AND TRIM(`영업사원`) <> ''
      AND `영업사원명` IS NOT NULL
      AND TRIM(`영업사원명`) <> ''
      AND `부서명` IS NOT NULL
      AND TRIM(`부서명`) <> ''
        GROUP BY `영업사원`, `영업사원명`, `부서명`
),
ranked AS (
    SELECT
        `사번`,
        `영업사원명`,
        `부서명`,
        ROW_NUMBER() OVER (
            PARTITION BY `사번`
            ORDER BY row_count DESC, sales_amount DESC, `부서명`, `영업사원명`
        ) AS rn
    FROM grouped
)
SELECT
    `사번`,
    `영업사원명`,
    `부서명`
FROM ranked
WHERE rn = 1
ORDER BY `부서명`, `영업사원명`, `사번`
"""


def main_export() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = main.run_query(SQL, raw=True)
    df = pd.DataFrame(rows, columns=["사번", "영업사원명", "부서명"])

    output_file = OUTPUT_FILE
    try:
        writer = pd.ExcelWriter(output_file, engine="openpyxl")
    except PermissionError:
        output_file = OUTPUT_DIR / f"{OUTPUT_FILE.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{OUTPUT_FILE.suffix}"
        writer = pd.ExcelWriter(output_file, engine="openpyxl")

    with writer:
        df.to_excel(writer, sheet_name=YEAR_MONTH, index=False)

    print(f"created={output_file}")
    print(f"rows={len(df)}")
    if not df.empty:
        print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    main_export()
