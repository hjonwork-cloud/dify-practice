"""
_diag_apr7_v7.py
────────────────────────────────────────────────────────────────
v7 DOW채움 + 공휴일계수 + 이상치cap 효과 검증
SIM_DATE = 2026-04-07 (7일차)

실제 데이터: 수(4/1)/목(4/2)/금(4/3)/토(4/4)/일(4/5, 배송없음)/월(4/6)/화(4/7)
→ 실매출 6일치 확보 (일요일 제외)

비교:
  v6: 균일/DOW가중 런레이트 — rr_weight=(7/30)^1.5 ≈ 6.4% → 통계예측 93.6%
  v7: DOW채움 + 공휴일계수 — 실제 7일 + 전월 동요일 채움, 블렌딩 w=40%
"""
import sys, os, statistics, calendar
from datetime import date, datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()
from main import run_query

# ── v7 함수 로드 ──────────────────────────────────────────────────────
_v7_path = os.path.join(os.path.dirname(__file__), "forecast_engine_v7.py")
_src = open(_v7_path, encoding="utf-8").read()
_cut = _src.find("# 메인 루프")
exec(compile(_src[:_cut], _v7_path, "exec"), globals())

# ── Apr-7 파라미터 ────────────────────────────────────────────────────
SIM_DATE_7    = date(2026, 4, 7)
DAYS_SO_FAR_7 = 7
DAY_RATIO_7   = DAYS_SO_FAR_7 / 30
RR_WEIGHT_V6  = DAY_RATIO_7 ** 1.5       # 0.0641 (6.4%)
ST_WEIGHT_V6  = 1 - RR_WEIGHT_V6         # 0.9359
FILL_CAP_R    = 1.50   # 채움/통계 비율 상한
FILL_FLOOR_R  = 0.40   # 채움/통계 비율 하한

FILL_W_MAX_V7 = _get_fill_blend_w(DAYS_SO_FAR_7)  # 일수별 최대 가중치 (7일차→ 0.50)

print("=" * 110)
print(f"v7 DOW채움+공휴일계수 효과 검증 — SIM_DATE=2026-04-07 (7일차)")
print(f"  v6 rr_weight={(RR_WEIGHT_V6*100):.1f}%  (균일/DOW가중 런레이트, 통계 비중 {ST_WEIGHT_V6*100:.0f}%)")
print(f"  v7 블렌딩: 통계(적응형), DOW채움 최대가중 {FILL_W_MAX_V7*100:.0f}%(일수별)  cap={FILL_CAP_R}/{FILL_FLOOR_R}")
print(f"  4/1~7 요일: 수/목/금/토/[일:배송없음]/월/화  |  직전 28일: 2026-03-04~03-31")
print("=" * 110)

results = []
errors  = []

for ZC_CODE, BRAND_NAME in BRANDS:
    try:
        # SQL1: 월별 매출
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
        actual_raw = {r["ym"]: {"sales": float(r["sales"]), "stores": int(r["stores"]),
                                "per_store": float(r["sales"]) / int(r["stores"]) if int(r["stores"]) > 0 else 0}
                      for r in rows1}
        actual, _ = preprocess_actual(actual_raw)
        brand_type, metrics = classify_brand(actual)

        # SQL3: 최근 6개월 일별 매출 (전월 포함)
        last_6m = add_months("202604", -6)
        sql3 = f"""
SELECT `대금청구일` AS date, SUM(`매출액`)/1000000 AS sales
FROM h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `년월` >= '{last_6m}' AND `년월` < '202604'
GROUP BY `대금청구일` ORDER BY `대금청구일`
"""
        rows3 = run_query(sql3)

        # SQL5: 실제 4월 1~7일 누계
        sql5 = f"""
SELECT SUM(`매출액`)/1000000 AS sales_7d
FROM h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v
WHERE `사업부명` = '외식식재사업부' AND `ZC본부` = '{ZC_CODE}'
  AND `대금청구일` >= '20260401' AND `대금청구일` <= '20260407'
"""
        r5 = run_query(sql5)
        actual_7d = float(r5[0]["sales_7d"]) if r5 and r5[0]["sales_7d"] else 0.0

        # 4월 실제 월합계
        actual_apr = actual.get("202604", {}).get("sales", 0)
        if actual_apr <= 0:
            continue

        # ── A) v6 방식 ─────────────────────────────────────────────────
        # 누계 시뮬 (실제 7일치 사용)
        so_far_v6 = actual_7d if actual_7d > 0 else actual_apr * (DAYS_SO_FAR_7 / 30)

        dow_weights_v6 = compute_dow_weights(rows3) if rows3 else None
        dow_rr_v6 = pred_run_rate_dow(so_far_v6, SIM_DATE_7, dow_weights_v6) if dow_weights_v6 else None
        USE_DOW_v6 = (dow_rr_v6 is not None and (
            brand_type == "NASCENT" or metrics["n_months"] < 12 or metrics["store_last"] < 20
        ))
        daily_avg_v6 = so_far_v6 / DAYS_SO_FAR_7
        rr_v6 = dow_rr_v6 if USE_DOW_v6 else daily_avg_v6 * 30
        method_v6 = "DOW가중" if USE_DOW_v6 else "균일"

        stat_v6 = pred_ensemble(actual, "202604", brand_type)
        if stat_v6 is None:
            continue
        final_v6 = stat_v6 * ST_WEIGHT_V6 + rr_v6 * RR_WEIGHT_V6
        err_v6 = (final_v6 - actual_apr) / actual_apr * 100

        # ── B) v7 DOW채움 방식 ──────────────────────────────────────────
        so_far_v7 = actual_7d if actual_7d > 0 else actual_apr * (DAYS_SO_FAR_7 / 30)
        fill_rr_v7, fill_meta = pred_dow_fill(so_far_v7, rows3, SIM_DATE_7, brand_type) \
            if rows3 else (None, {})

        # ── cap 체크 ──
        fill_capped = ""
        if fill_rr_v7 is not None and stat_v6 is not None and stat_v6 > 0:
            ratio = fill_rr_v7 / stat_v6
            if ratio > FILL_CAP_R:
                fill_capped = f"↑({ratio:.2f})"
                fill_rr_v7, fill_meta = None, {}
            elif ratio < FILL_FLOOR_R:
                fill_capped = f"↓({ratio:.2f})"
                fill_rr_v7, fill_meta = None, {}

        # ── 적응형 블렌딩 ──
        effective_w = 0.0
        if fill_rr_v7 is not None and stat_v6 is not None and stat_v6 > 0:
            divergence  = abs(fill_rr_v7 - stat_v6) / stat_v6
            fill_conf   = max(0.0, 1.0 - divergence)
            effective_w = FILL_W_MAX_V7 * fill_conf

        if fill_rr_v7 is not None and effective_w > 0:
            final_v7  = stat_v6 * (1 - effective_w) + fill_rr_v7 * effective_w
            method_v7 = f"DOW채움(w={effective_w:.2f})"
        else:
            final_v7  = final_v6
            method_v7 = f"fallback{''+fill_capped if fill_capped else '=v6'}"
        err_v7 = (final_v7 - actual_apr) / actual_apr * 100

        improvement = abs(err_v6) - abs(err_v7)

        results.append({
            "brand": BRAND_NAME, "actual": actual_apr,
            "actual_7d": actual_7d,
            "fill_rr": fill_rr_v7,
            "fill_meta": fill_meta,
            "fill_capped": fill_capped,
            "eff_w": effective_w,
            "stat": stat_v6,
            "final_v6": final_v6, "err_v6": err_v6, "method_v6": method_v6,
            "final_v7": final_v7, "err_v7": err_v7, "method_v7": method_v7,
            "improvement": improvement,
        })

    except Exception as e:
        errors.append((BRAND_NAME, ZC_CODE, str(e)))

# ── 결과 출력 ────────────────────────────────────────────────────────────
print(f"\n{'브랜드':<18} {'실제':>6} {'실7일':>6} {'채움추정':>8} {'w':>5} {'cap':>8} │ "
      f"{'v6예측':>7} {'v6오차':>7} │ {'v7예측':>7} {'v7오차':>7} │ {'개선':>7}")
print("─" * 130)

improved_count = worse_count = same_count = 0

for r in sorted(results, key=lambda x: abs(x["err_v7"])):
    b   = r["brand"][:17]
    imp = r["improvement"]
    if imp > 1:    improved_count += 1
    elif imp < -1: worse_count    += 1
    else:          same_count     += 1

    fill_str = f"{r['fill_rr']:.2f}" if r["fill_rr"] is not None else "   N/A"
    w_str    = f"{r['eff_w']:.2f}"   if r["fill_rr"] is not None else "  -  "
    cap_str  = r["fill_capped"]       if r["fill_capped"] else " "

    print(f"  {b:<17} {r['actual']:>6.2f} {r['actual_7d']:>6.2f} {fill_str:>8} "
          f"{w_str:>5} {cap_str:>8} │ "
          f"{r['final_v6']:>7.2f} {r['err_v6']:>+7.1f}% │ "
          f"{r['final_v7']:>7.2f} {r['err_v7']:>+7.1f}% │ "
          f"{imp:>+6.1f}%p")

# ── 요약 통계 ────────────────────────────────────────────────────────────
print("\n" + "=" * 110)
n = len(results)
if n:
    mape_v6 = sum(abs(r["err_v6"]) for r in results) / n
    mape_v7 = sum(abs(r["err_v7"]) for r in results) / n
    ok6  = sum(1 for r in results if abs(r["err_v6"]) < 5)
    ok7  = sum(1 for r in results if abs(r["err_v7"]) < 5)
    tri6 = sum(1 for r in results if 5 <= abs(r["err_v6"]) < 10)
    tri7 = sum(1 for r in results if 5 <= abs(r["err_v7"]) < 10)
    bad6 = sum(1 for r in results if abs(r["err_v6"]) >= 10)
    bad7 = sum(1 for r in results if abs(r["err_v7"]) >= 10)
    fill_applied = sum(1 for r in results if r["fill_rr"] is not None)
    cap_applied  = sum(1 for r in results if r["fill_capped"])
    capped_cnt   = sum(1 for r in results if r["fill_meta"].get("capped"))
    avg_eff_w    = (sum(r["eff_w"] for r in results if r["fill_rr"] is not None)
                   / fill_applied) if fill_applied else 0.0

    print(f"  브랜드 수: {n}개  |  DOW채움 적용(cap 통과): {fill_applied}개  |  cap 차단(fallback): {cap_applied}개  |  누적 이상치cap: {capped_cnt}개")
    print(f"  적응형 블렌딩 평균 effective_w: {avg_eff_w:.3f}  (일수별 최대 {FILL_W_MAX_V7:.2f}, FILL_WINDOW=28일)")
    print()
    print(f"  {'구분':<12} {'v6 (7일균일/DOW가중)':>22}  →  {'v7 (DOW채움 적응형)':>20}  {'변화':>8}")
    print(f"  {'─'*72}")
    print(f"  {'MAPE':<12} {mape_v6:>22.1f}%  →  {mape_v7:>17.1f}%  {mape_v7-mape_v6:>+7.1f}%p")
    print(f"  {'✓ ≤5%':<12} {ok6:>20}개({ok6/n*100:.0f}%)  →  {ok7:>15}개({ok7/n*100:.0f}%)")
    print(f"  {'△ 5~10%':<12} {tri6:>20}개  →  {tri7:>15}개")
    print(f"  {'✗ >10%':<12} {bad6:>20}개  →  {bad7:>15}개")
    print(f"  개선: {improved_count}개  악화: {worse_count}개  동일: {same_count}개")
    print()

    if mape_v7 < mape_v6:
        print(f"  ✅ v7 DOW채움(7일)이 v6 대비 MAPE {mape_v6-mape_v7:.1f}%p 개선")
    else:
        delta = mape_v7 - mape_v6
        print(f"  ⚠ v7 DOW채움(7일)이 v6 대비 MAPE {delta:.1f}%p 악화")

    # 이상치 cap 효과
    if capped_cnt:
        print(f"\n  [이상치 cap 적용 브랜드 {capped_cnt}개]")
        for r in results:
            if r["fill_meta"].get("capped"):
                m = r["fill_meta"]
                print(f"    {r['brand']:<18}  실7일={r['actual_7d']:.2f}억  "
                      f"기대7일={m.get('expected_so_far', 0):.2f}억  "
                      f"cap 후={m.get('used_so_far', 0):.2f}억  "
                      f"v7오차={r['err_v7']:+.1f}%  (v6오차={r['err_v6']:+.1f}%)")

    # 악화 원인 상위
    worse_brands = sorted([r for r in results if r["improvement"] < -1], key=lambda x: x["improvement"])
    if worse_brands:
        print(f"\n  ▼ 악화 브랜드 ({len(worse_brands)}개)  ← 전월 패턴이 당월과 다른 경우:")
        for r in worse_brands[:8]:
            m = r["fill_meta"]
            print(f"    {r['brand']:<18}  채움={r['fill_rr']:.2f}억  실제={r['actual']:.2f}억  "
                  f"(채움/실제={r['fill_rr']/r['actual']*100:.0f}%)  "
                  f"v6={r['err_v6']:+.1f}%→v7={r['err_v7']:+.1f}%  악화={r['improvement']:+.1f}%p")

    print()
    print(f"  [비교: Apr-3 vs Apr-7 vs Apr-14]")
    print(f"  Apr-3  (3일차,  rr_w=3.2%): MAPE≈8.9%  ✓44%")
    mape_v7_str = f"{mape_v7:.1f}%"
    ok7_str = f"{ok7/n*100:.0f}%"
    print(f"  Apr-7  (7일차,  DOW채움40%): MAPE={mape_v7_str}  ✓{ok7_str}")
    print(f"  Apr-14 (14일차, rr_w=31.9%): MAPE≈6.1%  ✓62%")

if errors:
    print(f"\n  오류 브랜드: {len(errors)}개")
    for nm, zc, e in errors[:5]:
        print(f"    {nm}({zc}): {e[:80]}")
