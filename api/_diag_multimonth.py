"""
_diag_multimonth.py
────────────────────────────────────────────────────────────────────────────
v6 vs v7 다중 월·일자 비교 진단 (월초 소표본 정확도 종합 검증)

테스트 셀:
  (2026-02, day=3)  (2026-02, day=7)
  (2026-03, day=3)  (2026-03, day=7)
  (2026-04, day=3)  (2026-04, day=7)

각 셀에서:
  v6: 균일/DOW가중 런레이트  (rr_w = (day/30)^1.5)
  v7: DOW채움(직전28일) + cap(1.5/0.4) + 적응형 블렌딩 + 일수별 blend_w

출력: 셀별 MAPE 표 + 브랜드별 오차 분포
────────────────────────────────────────────────────────────────────────────
"""
import sys, os, statistics, calendar
from datetime import date, datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()
from main import run_query

# ── v7 함수 로드 ──────────────────────────────────────────────────────
_v7_path = os.path.join(os.path.dirname(__file__), "forecast_engine_v7.py")
_src = open(_v7_path, encoding="utf-8").read()
_cut = _src.find("# 메인 루프")
exec(compile(_src[:_cut], _v7_path, "exec"), globals())

# ── 테스트 셀 정의 ────────────────────────────────────────────────────
#  (year, month, sim_day)
TEST_CELLS = [
    (2026, 2, 3),
    (2026, 2, 7),
    (2026, 3, 3),
    (2026, 3, 7),
    (2026, 4, 3),
    (2026, 4, 7),
]

FILL_CAP_R   = 1.50
FILL_FLOOR_R = 0.40

# ── 브랜드 목록은 엔진에서 불러온 BRANDS 사용 ──────────────────────
# (forecast_engine_v7.py exec 시 BRANDS 변수 포함됨)

# ──────────────────────────────────────────────────────────────────────
# 셀별 실행 함수
# ──────────────────────────────────────────────────────────────────────
def run_cell(year: int, month: int, sim_day: int) -> dict:
    """한 (월, 일수) 셀에 대해 모든 브랜드의 v6/v7 예측을 수행하고 결과 반환."""
    TARGET_YM   = f"{year}{month:02d}"
    SIM_DATE    = date(year, month, sim_day)
    DAYS_IN_MON = calendar.monthrange(year, month)[1]
    RR_WEIGHT   = (sim_day / 30) ** 1.5
    ST_WEIGHT   = 1 - RR_WEIGHT
    FILL_W_MAX  = _get_fill_blend_w(sim_day)

    # 시뮬 기준 일별 범위 문자열
    sim_from = f"{year}{month:02d}01"
    sim_to   = f"{year}{month:02d}{sim_day:02d}"

    # 6개월 lookback
    last_6m = add_months(TARGET_YM, -6)

    results = []
    errors  = []

    for ZC_CODE, BRAND_NAME in BRANDS:
        try:
            # ── SQL1: 월별 매출 ──────────────────────────────────────
            sql1 = f"""
SELECT `년월` AS ym,
       SUM(`매출액`)/1000000 AS sales,
       COUNT(DISTINCT `ZB본지점`) AS stores
FROM h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
GROUP BY `년월` ORDER BY `년월`
"""
            rows1 = run_query(sql1)
            if not rows1:
                continue
            actual_raw = {r["ym"]: {"sales": float(r["sales"]),
                                    "stores": int(r["stores"]),
                                    "per_store": float(r["sales"]) / int(r["stores"])
                                                 if int(r["stores"]) > 0 else 0}
                          for r in rows1}
            actual, _ = preprocess_actual(actual_raw)
            brand_type, metrics = classify_brand(actual)

            # 해당 월 실제 데이터가 없으면 skip
            actual_full = actual.get(TARGET_YM, {}).get("sales", 0)
            if actual_full <= 0:
                continue

            # ── SQL3: 직전 6개월 일별 매출 ───────────────────────────
            sql3 = f"""
SELECT `대금청구일` AS date, SUM(`매출액`)/1000000 AS sales
FROM h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `년월` >= '{last_6m}' AND `년월` < '{TARGET_YM}'
GROUP BY `대금청구일` ORDER BY `대금청구일`
"""
            rows3 = run_query(sql3)

            # ── SQL5: 해당 월 1~sim_day 실제 누계 ───────────────────
            sql5 = f"""
SELECT SUM(`매출액`)/1000000 AS sales_partial
FROM h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `대금청구일` >= '{sim_from}' AND `대금청구일` <= '{sim_to}'
"""
            r5 = run_query(sql5)
            so_far = float(r5[0]["sales_partial"]) if r5 and r5[0]["sales_partial"] else 0.0
            if so_far <= 0:
                so_far = actual_full * (sim_day / DAYS_IN_MON)

            # ── A) v6 예측 ──────────────────────────────────────────
            dow_weights = compute_dow_weights(rows3) if rows3 else None
            dow_rr = pred_run_rate_dow(so_far, SIM_DATE, dow_weights) if dow_weights else None
            USE_DOW = (dow_rr is not None and (
                brand_type == "NASCENT" or
                metrics["n_months"] < 12 or
                metrics["store_last"] < 20
            ))
            daily_avg = so_far / sim_day if sim_day else 0
            rr_v6     = dow_rr if USE_DOW else daily_avg * 30
            stat_v6   = pred_ensemble(actual, TARGET_YM, brand_type)
            if stat_v6 is None or stat_v6 <= 0:
                continue
            final_v6 = stat_v6 * ST_WEIGHT + rr_v6 * RR_WEIGHT
            err_v6   = (final_v6 - actual_full) / actual_full * 100

            # ── B) v7 예측 ──────────────────────────────────────────
            fill_rr, fill_meta = (None, {})
            if sim_day <= FILL_THRESHOLD and rows3:
                fill_rr, fill_meta = pred_dow_fill(so_far, rows3, SIM_DATE, brand_type)

            fill_capped = ""
            if fill_rr is not None and stat_v6 > 0:
                ratio = fill_rr / stat_v6
                if ratio > FILL_CAP_R:
                    fill_capped = f"↑({ratio:.2f})"
                    fill_rr, fill_meta = None, {}
                elif ratio < FILL_FLOOR_R:
                    fill_capped = f"↓({ratio:.2f})"
                    fill_rr, fill_meta = None, {}

            effective_w = 0.0
            if fill_rr is not None and stat_v6 > 0:
                divergence  = abs(fill_rr - stat_v6) / stat_v6
                fill_conf   = max(0.0, 1.0 - divergence)
                effective_w = FILL_W_MAX * fill_conf

            if fill_rr is not None and effective_w > 0:
                final_v7 = stat_v6 * (1 - effective_w) + fill_rr * effective_w
            else:
                final_v7 = final_v6
            err_v7 = (final_v7 - actual_full) / actual_full * 100

            results.append({
                "brand":      BRAND_NAME,
                "actual":     actual_full,
                "so_far":     so_far,
                "fill_rr":    fill_rr,
                "fill_capped": fill_capped,
                "eff_w":      effective_w,
                "stat":       stat_v6,
                "final_v6":   final_v6, "err_v6": err_v6,
                "final_v7":   final_v7, "err_v7": err_v7,
                "improve":    abs(err_v6) - abs(err_v7),
            })

        except Exception as e:
            errors.append((BRAND_NAME, ZC_CODE, str(e)))

    return {
        "label":   f"{year}-{month:02d} day{sim_day:02d}",
        "target_ym": TARGET_YM,
        "sim_day": sim_day,
        "rr_w":    RR_WEIGHT,
        "fill_w_max": FILL_W_MAX,
        "results": results,
        "errors":  errors,
    }

# ─────────────────────────────────────────────────────────────────────
print("=" * 100)
print("v6 vs v7 다중월·일자 종합 진단  (Feb/Mar/Apr × day3/day7)")
print(f"브랜드 샘플: {len(BRANDS)}개 | cap={FILL_CAP_R}/{FILL_FLOOR_R} | WINDOW=28일")
print("=" * 100)

cell_summaries = []

for (yr, mo, dy) in TEST_CELLS:
    print(f"\n>>> 실행 중: {yr}-{mo:02d} day{dy:02d} ...", flush=True)
    cell = run_cell(yr, mo, dy)
    cell_summaries.append(cell)
    rs = cell["results"]
    n  = len(rs)
    if n == 0:
        print(f"    결과 없음 (errors={len(cell['errors'])})")
        continue

    mape_v6 = sum(abs(r["err_v6"]) for r in rs) / n
    mape_v7 = sum(abs(r["err_v7"]) for r in rs) / n
    ok6  = sum(1 for r in rs if abs(r["err_v6"]) < 5)
    ok7  = sum(1 for r in rs if abs(r["err_v7"]) < 5)
    bad6 = sum(1 for r in rs if abs(r["err_v6"]) >= 10)
    bad7 = sum(1 for r in rs if abs(r["err_v7"]) >= 10)
    imp  = sum(1 for r in rs if r["improve"] > 1)
    wrs  = sum(1 for r in rs if r["improve"] < -1)
    cap_cnt  = sum(1 for r in rs if r["fill_capped"])
    fill_cnt = sum(1 for r in rs if r["fill_rr"] is not None)
    avg_ew   = (sum(r["eff_w"] for r in rs if r["fill_rr"] is not None) / fill_cnt
                if fill_cnt else 0.0)

    cell["mape_v6"] = mape_v6
    cell["mape_v7"] = mape_v7
    cell["ok6"] = ok6; cell["ok7"] = ok7
    cell["bad6"] = bad6; cell["bad7"] = bad7
    cell["n"] = n

    delta = mape_v7 - mape_v6
    sign  = "▼" if delta < -0.05 else ("▲" if delta > 0.05 else "─")
    print(f"    n={n}  v6={mape_v6:.1f}%  v7={mape_v7:.1f}%  {sign}{abs(delta):.1f}%p  "
          f"DOW채움={fill_cnt}개 cap={cap_cnt}개 avg_ew={avg_ew:.2f}  "
          f"개선={imp}:악화={wrs}")

# ─────────────────────────────────────────────────────────────────────
# 최종 요약 테이블
# ─────────────────────────────────────────────────────────────────────
print("\n\n" + "=" * 100)
print("  종합 비교 테이블")
print("=" * 100)
print(f"  {'구간':<20} {'n':>4}  "
      f"{'v6 MAPE':>8}  {'v7 MAPE':>8}  {'차이':>7}  "
      f"{'v6 ≤5%':>7}  {'v7 ≤5%':>7}  "
      f"{'v6 >10%':>8}  {'v7 >10%':>8}  {'rr_w':>7}")
print("  " + "─" * 95)

for c in cell_summaries:
    if "mape_v6" not in c:
        print(f"  {c['label']:<20} —")
        continue
    d  = c["mape_v7"] - c["mape_v6"]
    sg = "▼" if d < -0.05 else ("▲" if d > 0.05 else "─")
    n  = c["n"]
    print(f"  {c['label']:<20} {n:>4}  "
          f"{c['mape_v6']:>7.1f}%  {c['mape_v7']:>7.1f}%  "
          f"{sg}{abs(d):>5.1f}%p  "
          f"{c['ok6']:>4}개({c['ok6']/n*100:.0f}%)  "
          f"{c['ok7']:>4}개({c['ok7']/n*100:.0f}%)  "
          f"{c['bad6']:>5}개  {c['bad7']:>5}개  "
          f"{c['rr_w']*100:>6.1f}%")

# v6/v7 MAPE 전체 평균
cells_valid = [c for c in cell_summaries if "mape_v6" in c]
if cells_valid:
    all_mape6 = sum(c["mape_v6"] for c in cells_valid) / len(cells_valid)
    all_mape7 = sum(c["mape_v7"] for c in cells_valid) / len(cells_valid)
    print("  " + "─" * 95)
    print(f"  {'전체 평균':<20} {'':>4}  "
          f"{all_mape6:>7.1f}%  {all_mape7:>7.1f}%  "
          f"{'▼' if all_mape7 < all_mape6 else '▲'}{abs(all_mape7-all_mape6):>5.1f}%p")

print("\n")

# ─────────────────────────────────────────────────────────────────────
# day3 vs day7 일수 효과 분리 분석
# ─────────────────────────────────────────────────────────────────────
print("=" * 100)
print("  일수별 v6 MAPE 추이 (당월 데이터가 늘수록 정확해지는지)")
print("=" * 100)
day3_cells = [c for c in cells_valid if c["sim_day"] == 3]
day7_cells = [c for c in cells_valid if c["sim_day"] == 7]

def _mape_avg(lst, key): return sum(c[key] for c in lst)/len(lst) if lst else 0
print(f"  day3 평균: v6={_mape_avg(day3_cells,'mape_v6'):.1f}%  v7={_mape_avg(day3_cells,'mape_v7'):.1f}%")
print(f"  day7 평균: v6={_mape_avg(day7_cells,'mape_v6'):.1f}%  v7={_mape_avg(day7_cells,'mape_v7'):.1f}%")

# ─────────────────────────────────────────────────────────────────────
# 월별 상위 오차 브랜드 (v6 기준, >15%)
# ─────────────────────────────────────────────────────────────────────
print("\n" + "=" * 100)
print("  v6 >15% 오차 브랜드 목록 (월별 7일차)")
print("=" * 100)
for c in [x for x in cells_valid if x["sim_day"] == 7]:
    bad_brands = [(r["brand"][:16], r["actual"], r["err_v6"], r["err_v7"])
                  for r in c["results"] if abs(r["err_v6"]) >= 15]
    bad_brands.sort(key=lambda x: abs(x[2]), reverse=True)
    if bad_brands:
        print(f"\n  [{c['label']}]")
        for b, a, e6, e7 in bad_brands:
            arrow = "→▼" if abs(e7) < abs(e6) - 1 else ("→▲" if abs(e7) > abs(e6) + 1 else "→─")
            print(f"    {b:<18} 실제={a:>6.1f}백만  v6={e6:>+7.1f}%  v7={e7:>+7.1f}% {arrow}")

print("\n완료")
