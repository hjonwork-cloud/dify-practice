"""Export yearly 신규/기존/중단 customer lifecycle status to Excel.

Business rules
- 신규: target year has sales, and first sales date is on/after Oct 1 of previous year.
- 기존: target year has sales, and first sales date is before Oct 1 of previous year.
- 중단: previous year has sales, and target year has no sales.
- 개인형/FC: if significant ZC code (leading zero stripped) starts with 8, classify as FC.
    Otherwise classify as 개인형 and replace ZC code/name with '개인형'.
- FC rows are classified by ZC code and keep ZA code/name blank so each ZC code appears once.
- 개인형 rows are classified by ZA code.
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
OUTPUT_DIR = ROOT / "exports"
OUTPUT_FILE = OUTPUT_DIR / "customer_lifecycle_2020_2025.xlsx"
YEARS = tuple(range(2020, 2026))


def build_sql() -> str:
    year_values = " UNION ALL\n    ".join(f"SELECT {year} AS target_year" for year in YEARS)
    return f"""
WITH target_years AS (
    {year_values}
),
sales_rows AS (
    SELECT
        CAST(`년도` AS INT) AS sales_year,
        TO_DATE(CAST(`대금청구일` AS STRING), 'yyyyMMdd') AS bill_date,
        LPAD(CAST(`ZA거래처` AS STRING), 10, '0') AS za_code,
        COALESCE(NULLIF(`ZA거래처명`, ''), `거래처명`, '') AS za_name,
        LPAD(CAST(`ZC본부` AS STRING), 10, '0') AS zc_code_raw,
        COALESCE(NULLIF(`ZC본부명`, ''), '') AS zc_name_raw,
        CASE
            WHEN REGEXP_REPLACE(COALESCE(LPAD(CAST(`ZC본부` AS STRING), 10, '0'), ''), '^0+', '') LIKE '8%'
                THEN 'FC'
            ELSE '개인형'
        END AS entity_type,
        CASE
            WHEN REGEXP_REPLACE(COALESCE(LPAD(CAST(`ZC본부` AS STRING), 10, '0'), ''), '^0+', '') LIKE '8%'
                THEN LPAD(CAST(`ZC본부` AS STRING), 10, '0')
            ELSE LPAD(CAST(`ZA거래처` AS STRING), 10, '0')
        END AS entity_key,
        CAST(`매출액` AS DOUBLE) AS sales_amt
    FROM {SOURCE_TABLE}
    WHERE `사업부명` = '외식식재사업부'
      AND `대금청구일` IS NOT NULL
      AND CAST(`매출액` AS DOUBLE) > 0
      AND `ZA거래처` IS NOT NULL
),
first_sales AS (
    SELECT
        entity_type,
        entity_key,
        MIN(bill_date) AS first_bill_date
    FROM sales_rows
    GROUP BY entity_type, entity_key
),
yearly_sales AS (
    SELECT
        entity_type,
        entity_key,
        sales_year,
        SUM(sales_amt) AS year_sales_amt,
        MAX(bill_date) AS last_bill_date
    FROM sales_rows
    GROUP BY entity_type, entity_key, sales_year
),
yearly_rep AS (
    SELECT
        entity_type,
        entity_key,
        sales_year,
        za_name,
        zc_code_raw,
        zc_name_raw
    FROM (
        SELECT
            entity_type,
            entity_key,
            sales_year,
            za_name,
            zc_code_raw,
            zc_name_raw,
            ROW_NUMBER() OVER (
                PARTITION BY entity_type, entity_key, sales_year
                ORDER BY bill_date DESC, zc_code_raw DESC, zc_name_raw DESC, za_name DESC
            ) AS rn
        FROM sales_rows
    ) r
    WHERE rn = 1
),
customer_years AS (
    SELECT
        y.target_year,
        c.entity_type,
        c.entity_key
    FROM target_years y
    CROSS JOIN (
        SELECT DISTINCT entity_type, entity_key
        FROM yearly_sales
        WHERE sales_year BETWEEN {min(YEARS) - 1} AND {max(YEARS)}
    ) c
),
classified AS (
    SELECT
        cy.target_year AS `연도`,
        cy.entity_type AS `개인형/FC`,
        CASE
            WHEN cy.entity_type = 'FC'
                THEN COALESCE(curr_rep.zc_code_raw, prev_rep.zc_code_raw, '')
            ELSE '개인형'
        END AS `ZC코드`,
        CASE
            WHEN cy.entity_type = 'FC'
                THEN COALESCE(NULLIF(curr_rep.zc_name_raw, ''), NULLIF(prev_rep.zc_name_raw, ''), '')
            ELSE '개인형'
        END AS `ZC코드명`,
        CASE WHEN cy.entity_type = 'FC' THEN '' ELSE cy.entity_key END AS `ZA코드`,
        CASE WHEN cy.entity_type = 'FC' THEN '' ELSE COALESCE(curr_rep.za_name, prev_rep.za_name, '') END AS `ZA코드명`,
        CASE
            WHEN COALESCE(curr.year_sales_amt, 0) > 0
             AND fs.first_bill_date >= MAKE_DATE(cy.target_year - 1, 10, 1)
                THEN '신규'
            WHEN COALESCE(curr.year_sales_amt, 0) > 0
             AND fs.first_bill_date < MAKE_DATE(cy.target_year - 1, 10, 1)
                THEN '기존'
            WHEN COALESCE(prev.year_sales_amt, 0) > 0
             AND COALESCE(curr.year_sales_amt, 0) = 0
                THEN '중단'
            ELSE NULL
        END AS `신규기존중단여부`
    FROM customer_years cy
    LEFT JOIN yearly_sales curr
      ON curr.entity_type = cy.entity_type
     AND curr.entity_key = cy.entity_key
     AND curr.sales_year = cy.target_year
    LEFT JOIN yearly_sales prev
      ON prev.entity_type = cy.entity_type
     AND prev.entity_key = cy.entity_key
     AND prev.sales_year = cy.target_year - 1
    LEFT JOIN first_sales fs
      ON fs.entity_type = cy.entity_type
     AND fs.entity_key = cy.entity_key
    LEFT JOIN yearly_rep curr_rep
      ON curr_rep.entity_type = cy.entity_type
     AND curr_rep.entity_key = cy.entity_key
     AND curr_rep.sales_year = cy.target_year
    LEFT JOIN yearly_rep prev_rep
      ON prev_rep.entity_type = cy.entity_type
     AND prev_rep.entity_key = cy.entity_key
     AND prev_rep.sales_year = cy.target_year - 1
)
SELECT
    `연도`,
    `개인형/FC`,
    `ZC코드`,
    `ZC코드명`,
    `ZA코드`,
    `ZA코드명`,
    `신규기존중단여부`
FROM classified
WHERE `연도` BETWEEN {min(YEARS)} AND {max(YEARS)}
  AND `신규기존중단여부` IS NOT NULL
ORDER BY
    `연도`,
    CASE `신규기존중단여부`
        WHEN '신규' THEN 1
        WHEN '기존' THEN 2
        WHEN '중단' THEN 3
        ELSE 9
    END,
    `개인형/FC`,
    `ZC코드`,
    `ZA코드`
"""


def main_export() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sql = build_sql()
    rows = main.run_query(sql, raw=True)
    df = pd.DataFrame(rows)

    expected_columns = ["연도", "개인형/FC", "ZC코드", "ZC코드명", "ZA코드", "ZA코드명", "신규기존중단여부"]
    if df.empty:
        df = pd.DataFrame(columns=expected_columns)
    else:
        df = df[expected_columns]

    summary = (
        df.groupby(["연도", "신규기존중단여부"], dropna=False)
        .size()
        .unstack(fill_value=0)
        .reindex(index=list(YEARS), fill_value=0)
    )

    output_file = OUTPUT_FILE
    try:
        writer = pd.ExcelWriter(output_file, engine="openpyxl")
    except PermissionError:
        output_file = OUTPUT_DIR / f"{OUTPUT_FILE.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{OUTPUT_FILE.suffix}"
        writer = pd.ExcelWriter(output_file, engine="openpyxl")

    with writer:
        for year in YEARS:
            sheet_df = df[df["연도"] == year].drop(columns=["연도"])
            sheet_df.to_excel(writer, sheet_name=str(year), index=False)
        summary.to_excel(writer, sheet_name="summary")

    print(f"created={output_file}")
    print(f"rows={len(df)}")
    print(summary.to_string())
    print(f"generated_at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main_export()
