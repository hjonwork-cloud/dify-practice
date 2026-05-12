"""
_diag_compare4.py
────────────────────────────────────────────────────────────────────────────
4방향 × 상위40 / 하위20 브랜드 × 3개월 × day3/day7 종합 비교

방향별 예측 로직:
  v6      : 현행  rr_w=(day/30)^1.5  + 통계 앙상블
  v6b     : 보수  rr_w=(day/30)^2.0  + 통계 앙상블 (통계 의존 강화)
  v7      : DOW채움(직전28일) + cap(1.5/0.4) + 적응형 블렌딩
  v_stat  : 통계 앙상블 단독 (rr 완전 배제), 이상치 감지 태그만

브랜드 분류:
  상위40 : 직근 3개월(202502~202504) 평균 월매출 기준 상위 40개
  하위20 : 하위 20개
  (나머지는 분석에서 제외)

테스트 셀:
  (2026-02 day3/day7), (2026-03 day3/day7), (2026-04 day3/day7)
────────────────────────────────────────────────────────────────────────────
"""
import sys, os, statistics, calendar
from datetime import date, datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()
from main import run_query

# ── v7 엔진 함수 로드 ─────────────────────────────────────────────────
_v7_path = os.path.join(os.path.dirname(__file__), "forecast_engine_v7.py")
_src = open(_v7_path, encoding="utf-8").read()
_cut = _src.find("# 메인 루프")
exec(compile(_src[:_cut], _v7_path, "exec"), globals())

FILL_CAP_R   = 1.50
FILL_FLOOR_R = 0.40
TEST_CELLS   = [(2026,2,3),(2026,2,7),(2026,3,3),(2026,3,7),(2026,4,3),(2026,4,7)]

# ──────────────────────────────────────────────────────────────────────
# Step1. 브랜드 매출 규모 조회 → 상위40 / 하위20 분류
# ──────────────────────────────────────────────────────────────────────
print("브랜드 매출 규모 조회 중 (202502~202504 평균)...", flush=True)

brand_avg_sales = {}
for ZC_CODE, BRAND_NAME in BRANDS:
    sql = f"""
SELECT AVG(monthly_sales) AS avg_sales FROM (
  SELECT `년월`, SUM(`매출액`)/1000000 AS monthly_sales
  FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
  WHERE `사업부명`='외식식재사업부' AND `ZC본부`='{ZC_CODE}'
    AND `년월` IN ('202502','202503','202504')
  GROUP BY `년월`
) t
"""
    try:
        r = run_query(sql)
        avg = float(r[0]["avg_sales"]) if r and r[0]["avg_sales"] else 0.0
    except Exception:
        avg = 0.0
    brand_avg_sales[(ZC_CODE, BRAND_NAME)] = avg

# 매출 기준 정렬
sorted_brands = sorted(BRANDS, key=lambda b: brand_avg_sales[b], reverse=True)
TOP40  = sorted_brands[:40]
BTM20  = sorted_brands[-20:]

print(f"상위40 기준 최소: {brand_avg_sales[TOP40[-1]]:.1f}백만  "
      f"하위20 기준 최대: {brand_avg_sales[BTM20[0]]:.1f}백만")
print(f"상위40: {[b[1] for b in TOP40[:5]]}... ~ {[b[1] for b in TOP40[-3:]]}")
print(f"하위20: {[b[1] for b in BTM20[:3]]}... ~ {[b[1] for b in BTM20[-3:]]}")
print()

# ──────────────────────────────────────────────────────────────────────
# Step2. 4방향 예측 함수
# ──────────────────────────────────────────────────────────────────────
def run_four_ways(brand_list: list, year: int, month: int, sim_day: int) -> list:
    """brand_list 전체에 대해 4방향 예측 결과 리스트를 반환."""
    TARGET_YM   = f"{year}{month:02d}"
    SIM_DATE    = date(year, month, sim_day)
    DAYS_IN_MON = calendar.monthrange(year, month)[1]
    rr_w_v6     = (sim_day / 30) ** 1.5
    rr_w_v6b    = (sim_day / 30) ** 2.0
    st_w_v6     = 1 - rr_w_v6
    st_w_v6b    = 1 - rr_w_v6b
    fill_w_max  = _get_fill_blend_w(sim_day)
    last_6m     = add_months(TARGET_YM, -6)
    sim_from    = f"{year}{month:02d}01"
    sim_to      = f"{year}{month:02d}{sim_day:02d}"

    results = []
    for ZC_CODE, BRAND_NAME in brand_list:
        try:
            # SQL1: 월별 매출
            sql1 = f"""
SELECT `년월` AS ym, SUM(`매출액`)/1000000 AS sales,
       COUNT(DISTINCT `ZB본지점`) AS stores
FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
WHERE `사업부명`='외식식재사업부' AND `ZC본부`='{ZC_CODE}'
GROUP BY `년월` ORDER BY `년월`
"""
            rows1 = run_query(sql1)
            if not rows1: continue
            actual_raw = {r["ym"]: {"sales": float(r["sales"]),
                                    "stores": int(r["stores"]),
                                    "per_store": float(r["sales"])/int(r["stores"])
                                                 if int(r["stores"])>0 else 0}
                          for r in rows1}
            actual, _ = preprocess_actual(actual_raw)
            brand_type, metrics = classify_brand(actual)

            actual_full = actual.get(TARGET_YM, {}).get("sales", 0)
            if actual_full <= 0: continue

            # SQL3: 일별
            sql3 = f"""
SELECT `대금청구일` AS date, SUM(`매출액`)/1000000 AS sales
FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
WHERE `사업부명`='외식식재사업부' AND `ZC본부`='{ZC_CODE}'
  AND `년월`>='{last_6m}' AND `년월`<'{TARGET_YM}'
GROUP BY `대금청구일` ORDER BY `대금청구일`
"""
            rows3 = run_query(sql3)

            # SQL5: 시뮬 누계
            sql5 = f"""
SELECT SUM(`매출액`)/1000000 AS sp
FROM h_hmfo.gd_dcube.`01_sap_sales_custmasters`
WHERE `사업부명`='외식식재사업부' AND `ZC본부`='{ZC_CODE}'
  AND `대금청구일`>='{sim_from}' AND `대금청구일`<='{sim_to}'
"""
            r5 = run_query(sql5)
            so_far = float(r5[0]["sp"]) if r5 and r5[0]["sp"] else actual_full*(sim_day/DAYS_IN_MON)

            # 통계 앙상블
            stat = pred_ensemble(actual, TARGET_YM, brand_type)
            if not stat or stat <= 0: continue

            # DOW 가중 런레이트
            dow_weights = compute_dow_weights(rows3) if rows3 else None
            dow_rr = pred_run_rate_dow(so_far, SIM_DATE, dow_weights) if dow_weights else None
            USE_DOW = (dow_rr is not None and (
                brand_type=="NASCENT" or metrics["n_months"]<12 or metrics["store_last"]<20))
            daily_avg = so_far / sim_day if sim_day else 0
            rr = dow_rr if USE_DOW else daily_avg * 30

            # 이상치 감지 (v_stat용)
            prev_ym = add_months(TARGET_YM, -1)
            prev_partial_est = 0.0
            prev_full = actual.get(prev_ym, {}).get("sales", 0)
            if prev_full > 0:
                prev_partial_est = prev_full * (sim_day / DAYS_IN_MON)
            anomaly_tag = ""
            if prev_partial_est > 0:
                ratio_to_prev = so_far / prev_partial_est
                if ratio_to_prev > 2.0:
                    anomaly_tag = f"급증({ratio_to_prev:.1f}×)"
                elif ratio_to_prev < 0.3:
                    anomaly_tag = f"급감({ratio_to_prev:.1f}×)"

            # ── 4방향 예측 ──────────────────────────────────────────
            # v6
            p_v6   = stat * st_w_v6  + rr * rr_w_v6
            # v6b
            p_v6b  = stat * st_w_v6b + rr * rr_w_v6b
            # v7
            fill_rr, fill_meta = (None, {})
            if sim_day <= FILL_THRESHOLD and rows3:
                fill_rr, fill_meta = pred_dow_fill(so_far, rows3, SIM_DATE, brand_type)
            fill_capped = ""
            if fill_rr is not None and stat > 0:
                ratio = fill_rr / stat
                if ratio > FILL_CAP_R:
                    fill_capped = f"cap↑({ratio:.2f})"; fill_rr = None
                elif ratio < FILL_FLOOR_R:
                    fill_capped = f"cap↓({ratio:.2f})"; fill_rr = None
            eff_w = 0.0
            if fill_rr is not None:
                div   = abs(fill_rr - stat) / stat
                eff_w = fill_w_max * max(0.0, 1.0 - div)
            p_v7  = (stat*(1-eff_w) + fill_rr*eff_w) if (fill_rr and eff_w>0) else p_v6
            # v_stat
            p_vs   = stat

            e_v6   = (p_v6  - actual_full) / actual_full * 100
            e_v6b  = (p_v6b - actual_full) / actual_full * 100
            e_v7   = (p_v7  - actual_full) / actual_full * 100
            e_vs   = (p_vs  - actual_full) / actual_full * 100

            results.append({
                "brand": BRAND_NAME, "zc": ZC_CODE,
                "actual": actual_full, "so_far": so_far,
                "stat": stat, "rr": rr, "eff_w": eff_w,
                "fill_capped": fill_capped, "anomaly": anomaly_tag,
                "p_v6": p_v6, "e_v6": e_v6,
                "p_v6b": p_v6b, "e_v6b": e_v6b,
                "p_v7": p_v7, "e_v7": e_v7,
                "p_vs": p_vs, "e_vs": e_vs,
            })
        except Exception:
            pass
    return results

# ──────────────────────────────────────────────────────────────────────
# Step3. 전체 셀 실행
# ──────────────────────────────────────────────────────────────────────
def mape(rs, key): return sum(abs(r[key]) for r in rs)/len(rs) if rs else 0
def ok5(rs, key):  return sum(1 for r in rs if abs(r[key]) < 5)
def bad10(rs,key): return sum(1 for r in rs if abs(r[key]) >= 10)

def summarize(label, rs):
    n = len(rs)
    if n == 0: return f"  {label:<22}  n=0"
    stats = {}
    for k in ("e_v6","e_v6b","e_v7","e_vs"):
        stats[k] = (mape(rs,k), ok5(rs,k), bad10(rs,k))
    best_key = min(stats, key=lambda k: stats[k][0])
    line = (f"  {label:<22}  n={n:>3}  "
            f"v6={stats['e_v6'][0]:>5.1f}%(ok={stats['e_v6'][1]:>2},bad={stats['e_v6'][2]:>2})  "
            f"v6b={stats['e_v6b'][0]:>5.1f}%(ok={stats['e_v6b'][1]:>2},bad={stats['e_v6b'][2]:>2})  "
            f"v7={stats['e_v7'][0]:>5.1f}%(ok={stats['e_v7'][1]:>2},bad={stats['e_v7'][2]:>2})  "
            f"vS={stats['e_vs'][0]:>5.1f}%(ok={stats['e_vs'][1]:>2},bad={stats['e_vs'][2]:>2})"
            f"  ★{best_key}")
    return line

all_cells = {}   # key=(yr,mo,dy,group_name) → results list

print("=" * 130)
print("4방향 비교: v6 / v6b(보수) / v7(DOW채움) / vS(통계단독)")
print(f"  상위40({len(TOP40)}개) / 하위20({len(BTM20)}개)  |  셀: {len(TEST_CELLS)}개")
print("=" * 130)
print(f"  {'구 간':<22}  {'n':>3}  "
      f"{'v6 MAPE':>13}  {'v6b MAPE':>14}  {'v7 MAPE':>13}  {'vS MAPE':>13}  최우수")
print("  " + "─"*126)

for (yr, mo, dy) in TEST_CELLS:
    lbl = f"{yr}-{mo:02d} day{dy:02d}"
    for grp_name, grp_brands in [("상위40", TOP40), ("하위20", BTM20)]:
        print(f">>> {lbl} [{grp_name}] 실행 중...", flush=True)
        rs = run_four_ways(grp_brands, yr, mo, dy)
        key = (yr, mo, dy, grp_name)
        all_cells[key] = rs
        line = summarize(f"{lbl} [{grp_name}]", rs)
        print(line)

# ──────────────────────────────────────────────────────────────────────
# Step4. 그룹별 전체 평균
# ──────────────────────────────────────────────────────────────────────
print("\n" + "=" * 130)
print("  그룹별 전체 평균 (6셀 통합)")
print("=" * 130)

for grp_name in ("상위40", "하위20"):
    all_rs = []
    for (yr,mo,dy) in TEST_CELLS:
        all_rs.extend(all_cells.get((yr,mo,dy,grp_name), []))
    line = summarize(f"[{grp_name}] 전체", all_rs)
    print(line)

all_rs_total = []
for rs in all_cells.values():
    all_rs_total.extend(rs)
line = summarize("[전체] 합산", all_rs_total)
print(line)

# ──────────────────────────────────────────────────────────────────────
# Step5. 일수(day3 vs day7) 효과 분리
# ──────────────────────────────────────────────────────────────────────
print("\n" + "=" * 130)
print("  일수별 비교 (day3 vs day7)")
print("=" * 130)
for grp_name in ("상위40", "하위20"):
    for dy in (3, 7):
        rs = []
        for (yr,mo,dy2) in TEST_CELLS:
            if dy2 == dy:
                rs.extend(all_cells.get((yr,mo,dy2,grp_name),[]))
        line = summarize(f"[{grp_name}] day{dy} 평균", rs)
        print(line)

# ──────────────────────────────────────────────────────────────────────
# Step6. 브랜드별 누적 오차 순위 (하위20, v6 기준 worst 10)
# ──────────────────────────────────────────────────────────────────────
print("\n" + "=" * 130)
print("  하위20 브랜드별 누적 MAPE (v6, 6셀 합산)")
print("=" * 130)
brand_agg = defaultdict(lambda: defaultdict(list))
for (yr,mo,dy) in TEST_CELLS:
    for grp in ("상위40","하위20"):
        for r in all_cells.get((yr,mo,dy,grp),[]):
            for k in ("e_v6","e_v6b","e_v7","e_vs"):
                brand_agg[r["brand"]][k].append(abs(r[k]))

btm_brands = {b[1] for b in BTM20}
btm_agg = [(b, v) for b,v in brand_agg.items() if b in btm_brands]
btm_agg.sort(key=lambda x: statistics.mean(x[1]["e_v6"]) if x[1]["e_v6"] else 0, reverse=True)

print(f"  {'브랜드':<20} {'v6 MAPE':>9} {'v6b MAPE':>10} {'v7 MAPE':>9} {'vS MAPE':>9}  최우수")
print("  " + "─"*80)
for bname, agg in btm_agg[:20]:
    def avg(lst): return statistics.mean(lst) if lst else 0
    vals = {k: avg(agg[k]) for k in ("e_v6","e_v6b","e_v7","e_vs")}
    best = min(vals, key=vals.get)
    print(f"  {bname:<20} {vals['e_v6']:>8.1f}% {vals['e_v6b']:>9.1f}% "
          f"{vals['e_v7']:>8.1f}% {vals['e_vs']:>8.1f}%  {best}")

# ──────────────────────────────────────────────────────────────────────
# Step7. 각 방향별 최다 이상치 악화 브랜드 (v6→vS 이상치 감소)
# ──────────────────────────────────────────────────────────────────────
print("\n" + "=" * 130)
print("  상위40 브랜드별 누적 MAPE (v6 vs vS — 통계단독 효과)")
print("=" * 130)
top_brands = {b[1] for b in TOP40}
top_agg = [(b,v) for b,v in brand_agg.items() if b in top_brands]
top_agg.sort(key=lambda x: abs(statistics.mean(x[1]["e_v6"]) - statistics.mean(x[1]["e_vs"]))
             if x[1]["e_v6"] else 0, reverse=True)

print(f"  {'브랜드':<20} {'v6 MAPE':>9} {'vS MAPE':>9}  {'차이':>7}  방향")
print("  " + "─"*60)
for bname, agg in top_agg[:20]:
    def avg(lst): return statistics.mean(lst) if lst else 0
    m6 = avg(agg["e_v6"]); ms = avg(agg["e_vs"])
    diff = ms - m6
    direction = "vS유리▼" if diff < -1 else ("v6유리▲" if diff > 1 else "동일─")
    print(f"  {bname:<20} {m6:>8.1f}% {ms:>8.1f}%  {diff:>+7.1f}%p  {direction}")

print("\n완료")
