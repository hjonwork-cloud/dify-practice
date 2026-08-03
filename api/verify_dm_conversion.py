"""
검증 스크립트: SF송도갈비본점 DM 전환 집계 누락 원인 분석
- 대상 거래처: 0000193241 (SF송도갈비본점)
- DM 발송일: 2026-07-31
- 추천 상품: 100914

실행: python verify_dm_conversion.py
"""

import sys
import os
import json
import sqlite3
from pathlib import Path

# ── 경로 설정 ──────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.getenv("CHATBOT_DATA_DIR", os.getenv("DATA_DIR", r"E:\data\chatbot")))
DB_PATH  = DATA_DIR / "portal_activity.db"
TARGET_CUST = "0000193241"
TARGET_MATNR = "100914"
DM_DATE  = "2026-07-31"
DM_YM    = "202607"   # DM 발송월

T_MAIN = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"

print("=" * 70)
print("STEP 1. SQLite dm_send_logs 조회")
print("=" * 70)

# ── Step 1: SQLite 조회 ────────────────────────────────────────────────────────
if not DB_PATH.exists():
    print(f"  [WARN] DB 파일 없음: {DB_PATH}")
    print("  → Azure 서버에서만 접근 가능. 아래 수동 확인 쿼리 사용:")
    print(f"""
  sqlite3 "{DB_PATH}" \\
    "SELECT id, customer_code, customer_name, action_type, product_names,
            price_items_json, status, created_at
     FROM dm_send_logs
     WHERE customer_code = '{TARGET_CUST}'
     ORDER BY created_at DESC LIMIT 10;"
""")
else:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT id, emp_code, customer_code, customer_name, brand_code,
                   action_type, product_names, price_items_json, status, created_at
            FROM dm_send_logs
            WHERE customer_code = ?
            ORDER BY created_at DESC LIMIT 10""",
        (TARGET_CUST,)
    ).fetchall()

    if not rows:
        print(f"  [결과] 거래처 {TARGET_CUST} DM 로그 없음")
    else:
        for r in rows:
            r = dict(r)
            pj = r.get("price_items_json") or ""
            has_price = bool(pj and pj != "[]" and pj != "null")
            print(f"  id={r['id']}  action_type={r['action_type']}")
            print(f"    customer : {r['customer_code']} {r['customer_name']}")
            print(f"    products : {r['product_names']}")
            print(f"    price_items_json: {'있음 (' + str(len(pj)) + 'bytes)' if has_price else '없음 (빈값)'}")
            print(f"    status   : {r['status']}")
            print(f"    created_at: {r['created_at']}")
            print()

            # ── 현재 로직 시뮬레이션 ────────────────────────────────────────
            print(f"  [현재 로직 시뮬레이션]")
            if not has_price:
                print(f"  → price_items_json 없음 → continue (집계 제외) ← 누락 원인")
                action_ym_fallback = str(r["created_at"])[:7].replace("-", "")
                print(f"  → 수정 후: created_at({r['created_at'][:7]}) → action_ym = {action_ym_fallback}")
            else:
                try:
                    items = json.loads(pj)
                    dates = [str(it.get("date_from",""))[:6] for it in items if it.get("date_from")]
                    action_ym = min((d for d in dates if len(d)==6), default="")
                    print(f"  → action_ym = {action_ym}  (price_items 기반)")
                except Exception as e:
                    print(f"  → price_items_json 파싱 오류: {e}")
            print()
    conn.close()

# ── Step 2: Databricks 조회 ────────────────────────────────────────────────────
print("=" * 70)
print("STEP 2. Databricks T_MAIN: 거래처 DM 발송 이후 매출 존재 여부")
print("=" * 70)

try:
    sys.path.insert(0, str(Path(__file__).parent))
    import main as _main

    # ① 거래처 기본 정보 확인
    info_rows = _main._safe_query(f"""
        SELECT MAX(`거래처명`) AS cust_name,
               MAX(`ZC본부`) AS brand_code,
               MAX(`ZC본부명`) AS brand_name,
               MAX(`지점명`) AS team
        FROM {T_MAIN}
        WHERE `거래처` = '{TARGET_CUST}'
    """, raw=True)
    if info_rows:
        info = info_rows[0]
        print(f"  거래처명 : {info.get('cust_name')}")
        print(f"  브랜드   : {info.get('brand_code')} {info.get('brand_name')}")
        print(f"  팀       : {info.get('team')}")
    else:
        print(f"  [WARN] T_MAIN에서 거래처 {TARGET_CUST} 기본 정보 없음")

    print()

    # ② DM 발송 이후 매출 집계
    sales_rows = _main._safe_query(f"""
        SELECT `년월`,
               SUM(`매출액`) AS 매출액,
               COUNT(DISTINCT `상품코드`) AS 품목수,
               MAX(CASE WHEN `상품코드` = '{TARGET_MATNR}' THEN `매출액` ELSE 0 END) AS 추천품목매출
        FROM {T_MAIN}
        WHERE `거래처` = '{TARGET_CUST}'
          AND `년월` >= '{DM_YM}'
        GROUP BY `년월`
        ORDER BY `년월`
    """, raw=True)

    if not sales_rows:
        print(f"  [결과] {DM_YM} 이후 매출 데이터 없음 → T_MAIN에 아직 미반영 또는 거래처 불일치")
    else:
        print(f"  {'년월':<8} {'매출액':>14} {'품목수':>6} {'추천품목('+TARGET_MATNR+')':>20}")
        print(f"  {'-'*55}")
        for r in sales_rows:
            flag = " ← 추천품목 구매!" if float(r.get("추천품목매출") or 0) > 0 else ""
            print(f"  {r['년월']:<8} {float(r['매출액'] or 0):>14,.0f}원  {int(r['품목수'] or 0):>6}개  "
                  f"{float(r['추천품목매출'] or 0):>14,.0f}원{flag}")
        print()

    # ③ 추천 상품(100914) 직접 검색
    matnr_rows = _main._safe_query(f"""
        SELECT `년월`, `상품코드`, `상품명`, SUM(`매출액`) AS 매출액, SUM(`수량`) AS 수량
        FROM {T_MAIN}
        WHERE `거래처` = '{TARGET_CUST}'
          AND `상품코드` = '{TARGET_MATNR}'
          AND `년월` >= '202601'
        GROUP BY `년월`, `상품코드`, `상품명`
        ORDER BY `년월`
    """, raw=True)

    print(f"  [추천 상품 {TARGET_MATNR} 구매 이력]")
    if not matnr_rows:
        print(f"  → {TARGET_CUST}의 상품코드 {TARGET_MATNR} 구매 이력 없음 (2026년 전체)")
    else:
        for r in matnr_rows:
            dm_flag = " ← DM 발송 이후" if str(r['년월']) >= DM_YM else ""
            print(f"  {r['년월']} {r['상품명']} : {float(r['매출액'] or 0):,.0f}원 / {r['수량']}개{dm_flag}")

    print()

    # ④ 수정 후 로직으로 T_ACTION_RESULTS 시뮬레이션
    print("=" * 70)
    print("STEP 3. 수정 후 로직 dry-run (dm_only_sent → created_at 기반 action_ym)")
    print("=" * 70)

    # DM 발송일 기준 action_ym
    sim_action_ym = DM_YM  # "202607"

    sim_rows = _main._safe_query(f"""
        SELECT
          '{TARGET_CUST}' AS customer_code,
          '{sim_action_ym}' AS action_ym,
          ROUND(SUM(COALESCE(m.`매출액`, 0)) / 10000) AS sales_after_m,
          ROUND(SUM(CASE WHEN m.`상품코드` = '{TARGET_MATNR}'
                         THEN COALESCE(m.`매출액`, 0) ELSE 0 END) / 10000) AS dm_product_sales_m
        FROM {T_MAIN} m
        WHERE m.`거래처` = '{TARGET_CUST}'
          AND m.`년월` >= '{sim_action_ym}'
    """, raw=True)

    if sim_rows:
        r = sim_rows[0]
        print(f"  action_ym(DM 발송월) : {sim_action_ym}")
        print(f"  DM 이후 총매출       : {float(r.get('sales_after_m') or 0):,.0f}만원")
        print(f"  추천상품({TARGET_MATNR}) 매출 : {float(r.get('dm_product_sales_m') or 0):,.0f}만원")
        if float(r.get('dm_product_sales_m') or 0) > 0:
            print(f"\n  ✅ 수정 후 로직으로 전환 집계 가능 → 코드 수정 진행 권장")
        elif float(r.get('sales_after_m') or 0) > 0:
            print(f"\n  ⚠️  DM 이후 전체 매출은 있으나 추천상품 구매 미확인")
            print(f"     → 전체 매출 기준 전환 집계는 가능, 추천상품 추적은 별도 구현 필요")
        else:
            print(f"\n  ❌ DM 이후 매출 데이터 없음 → T_MAIN 미반영 or 거래처코드 불일치 확인 필요")

except ImportError as e:
    print(f"  [WARN] main 모듈 import 불가 (로컬 환경): {e}")
    print()
    print("  아래 쿼리를 Databricks에서 직접 실행하세요:")
    print(f"""
  -- ① DM 이후 매출 확인
  SELECT `년월`, SUM(`매출액`) AS 매출액, COUNT(DISTINCT `상품코드`) AS 품목수
  FROM {T_MAIN}
  WHERE `거래처` = '{TARGET_CUST}' AND `년월` >= '{DM_YM}'
  GROUP BY `년월` ORDER BY `년월`;

  -- ② 추천상품 구매 이력
  SELECT `년월`, `상품코드`, `상품명`, SUM(`매출액`) AS 매출액
  FROM {T_MAIN}
  WHERE `거래처` = '{TARGET_CUST}' AND `상품코드` = '{TARGET_MATNR}'
  GROUP BY `년월`, `상품코드`, `상품명` ORDER BY `년월`;

  -- ③ 수정 후 dry-run
  SELECT ROUND(SUM(COALESCE(`매출액`, 0)) / 10000) AS sales_after_m,
         ROUND(SUM(CASE WHEN `상품코드` = '{TARGET_MATNR}'
                        THEN COALESCE(`매출액`, 0) ELSE 0 END) / 10000) AS dm_product_sales_m
  FROM {T_MAIN}
  WHERE `거래처` = '{TARGET_CUST}' AND `년월` >= '{DM_YM}';
""")
except Exception as e:
    print(f"  [ERROR] Databricks 조회 실패: {e}")

print("=" * 70)
print("검증 완료")
print("=" * 70)
