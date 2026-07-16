from pathlib import Path

import pandas as pd


def main() -> None:
    src = Path(r"e:\Downloads\무회전재고_60일이상.xlsx")
    out = Path(r"e:\Downloads\무회전재고_60일이상_5월_팀_담당자_분석.xlsx")

    df = pd.read_excel(src, dtype=str).fillna("")

    text_cols = ["조회월", "신규 / 이월", "팀", "영업사원명", "물류센터명상품코드"]
    for column in text_cols:
        if column in df.columns:
            df[column] = df[column].astype(str).str.strip()

    numeric_cols = ["(당월)재고금액", "(당월)재고", "(익월)재고금액", "(익월)재고"]
    for column in numeric_cols:
        if column in df.columns:
            converted = (
                df[column]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.replace(" ", "", regex=False)
            )
            df[column] = pd.to_numeric(converted, errors="coerce").fillna(0)

    may = df[df["조회월"] == "5월"].copy()

    team_summary = (
        may.groupby("팀", dropna=False)
        .agg(
            무회전SKU수=("물류센터명상품코드", "nunique"),
            당월재고금액=("(당월)재고금액", "sum"),
            영업사원수=("영업사원명", "nunique"),
        )
        .reset_index()
        .sort_values(["당월재고금액", "무회전SKU수"], ascending=[False, False])
    )

    team_salesperson = (
        may.groupby(["팀", "영업사원명"], dropna=False)
        .agg(
            무회전SKU수=("물류센터명상품코드", "nunique"),
            당월재고금액=("(당월)재고금액", "sum"),
        )
        .reset_index()
    )

    carry_map = (
        may.groupby(["팀", "영업사원명"], dropna=False)
        .apply(
            lambda group: pd.Series(
                {
                    "이월SKU개수": group.loc[
                        group["신규 / 이월"] == "이월", "물류센터명상품코드"
                    ].nunique(),
                    "신규SKU개수": group.loc[
                        group["신규 / 이월"] == "신규", "물류센터명상품코드"
                    ].nunique(),
                }
            )
        )
        .reset_index()
    )

    team_salesperson = team_salesperson.merge(
        carry_map,
        on=["팀", "영업사원명"],
        how="left",
    ).fillna(0)

    team_salesperson["이월비중"] = (
        team_salesperson["이월SKU개수"] / team_salesperson["무회전SKU수"]
    ).replace([float("inf")], 0).fillna(0)

    team_salesperson = team_salesperson.sort_values(
        ["팀", "당월재고금액"],
        ascending=[True, False],
    )

    owner_carryover = team_salesperson[
        ["팀", "영업사원명", "무회전SKU수", "이월SKU개수", "이월비중", "당월재고금액"]
    ].copy()

    owner_carryover["이월비중순위"] = owner_carryover["이월비중"].rank(
        method="min",
        ascending=False,
    )
    owner_carryover["이월SKU개수순위"] = owner_carryover["이월SKU개수"].rank(
        method="min",
        ascending=False,
    )
    owner_carryover = owner_carryover.sort_values(
        ["이월비중", "이월SKU개수", "당월재고금액"],
        ascending=[False, False, False],
    )

    pivot_amount = pd.pivot_table(
        may,
        index="팀",
        columns="영업사원명",
        values="(당월)재고금액",
        aggfunc="sum",
        fill_value=0,
    )

    pivot_sku = pd.pivot_table(
        may,
        index="팀",
        columns="영업사원명",
        values="물류센터명상품코드",
        aggfunc=lambda series: series.nunique(),
        fill_value=0,
    )

    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        team_summary.to_excel(writer, index=False, sheet_name="5월_팀요약")
        team_salesperson.to_excel(writer, index=False, sheet_name="5월_팀_영업사원")
        owner_carryover.to_excel(writer, index=False, sheet_name="5월_담당자_이월비중")
        pivot_amount.to_excel(writer, sheet_name="5월_팀별영업사원_금액")
        pivot_sku.to_excel(writer, sheet_name="5월_팀별영업사원_SKU")

    print(f"created: {out}")
    print(f"may_rows: {len(may)}")
    print(f"team_summary_rows: {len(team_summary)}")
    print(f"team_salesperson_rows: {len(team_salesperson)}")
    print("owner_carryover_top5:")
    print(owner_carryover.head(5).to_string(index=False))


if __name__ == "__main__":
    main()