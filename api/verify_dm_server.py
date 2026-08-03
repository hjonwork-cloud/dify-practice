#!/usr/bin/env python3
"""
Azure 서버에서 실행하는 검증 스크립트.
주요 체크:
  1) SQLite dm_send_logs에 SF송도갈비본점 로그 있는지
  2) T_MAIN에 DM 이후 해당 거래처 매출 있는지
  3) 추천 상품 100914 실제 구매 여부
  4) 수정 후 로직 dry-run

실행: python verify_dm_server.py
(Azure App Service > Kudu > /site/wwwroot/api/ 경로에서 실행)
"""
import os, sys, json, sqlite3
from pathlib import Path

DATA_DIR   = Path(os.getenv("CHATBOT_DATA_DIR", os.getenv("DATA_DIR", r"/home/data/chatbot")))
DB_PATH    = DATA_DIR / "portal_activity.db"
TARGET_CUST  = "0000193241"
TARGET_MATNR = "100914"
DM_YM        = "202607"

print("=" * 70)
print("STEP 1. SQLite dm_send_logs 조회")
print("=" * 70)

if not DB_PATH.exists():
    print(f"  [WARN] DB 파일 없음: {DB_PATH}")
else:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 거래처코드 0000193241 로그 조회
    rows = conn.execute("""
        SELECT id, emp_code, customer_code, customer_name, brand_code,
               action_type, product_names, price_items_json, status, created_at
        FROM dm_send_logs
        WHERE customer_code = ?
        ORDER BY created_at DESC LIMIT 10
    """, (TARGET_CUST,)).fetchall()

    if not rows:
        # 거래처명으로도 검색
        rows2 = conn.execute("""
            SELECT id, customer_code, customer_name, action_type, product_names,
                   price_items_json, status, created_at
            FROM dm_send_logs
            WHERE customer_name LIKE '%SF송도갈비%' OR customer_name LIKE '%SF송도%'
            ORDER BY created_at DESC LIMIT 10
        """).fetchall()
        if rows2:
            print("  [INFO] 거래처코드 0000193241 없음, 거래처명 검색 결과:")
            rows = rows2
        else:
            print(f"  [결과] SF송도갈비본점(0000193241) DM 로그 없음")
            print("  → 로그가 없으면 전환 집계 불가 (DM 발송 자체가 기록 안된 것)")
            print()
            # 가장 최근 10건 출력으로 상황 파악
            recent = conn.execute("""
                SELECT id, customer_code, customer_name, action_type, product_names,
                       status, created_at
                FROM dm_send_logs
                ORDER BY created_at DESC LIMIT 5
            """).fetchall()
            print("  [최근 DM 발송 5건]")
            for r in recent:
                r = dict(r)
                print(f"  {r['created_at']}  {r['customer_code']} {r['customer_name']}  "
                      f"type={r['action_type']}  status={r['status']}")

    for row in rows:
        r = dict(row)
        pj = r.get("price_items_json") or ""
        has_price = bool(pj and pj not in ("[]", "null", ""))
        print(f"\n  id={r['id']}  created_at={r['created_at']}")
        print(f"  거래처: {r['customer_code']} / {r['customer_name']}")
        print(f"  action_type : {r['action_type']}")
        print(f"  products    : {r.get('product_names','')}")
        print(f"  price_items : {'있음' if has_price else '없음 ← 집계 제외 원인'}")
        print(f"  status      : {r['status']}")

        # 현재 로직 시뮬
        action_ym_current = ""
        action_ym_fixed   = str(r["created_at"])[:7].replace("-", "")  # "202607"
        if has_price:
            try:
                items = json.loads(pj)
                dates = [str(it.get("date_from",""))[:6] for it in items if it.get("date_from")]
                action_ym_current = min((d for d in dates if len(d)==6), default="")
            except Exception:
                pass
        print(f"\n  [현재 로직] action_ym = {'\"' + action_ym_current + '\"' if action_ym_current else '추출 불가 → continue(집계 제외)'}")
        print(f"  [수정 후  ] action_ym = {action_ym_fixed}  (created_at 기반)")

    conn.close()

print()
print("=" * 70)
print("STEP 2. Databricks T_MAIN 쿼리 (main 모듈 사용)")
print("=" * 70)

sys.path.insert(0, str(Path(__file__).parent))
try:
    import main as _m
    T_MAIN = _m.T_MAIN

    def q(sql):
        return _m._safe_query(sql.strip(), raw=True)

    # 거래처 기본 정보
    r1 = q(f"SELECT MAX(`거래처명`) AS nm, MAX(`ZC본부명`) AS brand FROM {T_MAIN} WHERE `거래처`='{TARGET_CUST}'")
    if r1 and r1[0].get("nm"):
        print(f"  거래처명: {r1[0]['nm']}  브랜드: {r1[0]['brand']}")
    else:
        print(f"  [WARN] T_MAIN에서 거래처 {TARGET_CUST} 없음 → 거래처코드 불일치 가능성")
        # 거래처명으로 재검색
        r1b = q(f"SELECT DISTINCT `거래처`, `거래처명`, `ZC본부명` FROM {T_MAIN} WHERE `거래처명` LIKE '%SF송도갈비%' LIMIT 5")
        if r1b:
            print(f"  → 거래처명 검색 결과:")
            for r in r1b:
                print(f"     거래처={r['거래처']}  명={r['거래처명']}  브랜드={r['ZC본부명']}")
        else:
            print("  → '거래처명 LIKE SF송도갈비' 결과도 없음")

    print()

    # DM 이후 월별 매출
    r2 = q(f"""
        SELECT `년월`, ROUND(SUM(`매출액`)) AS 매출액, COUNT(DISTINCT `상품코드`) AS 품목수
        FROM {T_MAIN}
        WHERE `거래처`='{TARGET_CUST}' AND `년월` >= '{DM_YM}'
        GROUP BY `년월` ORDER BY `년월`
    """)
    if r2:
        print(f"  [DM 이후 월별 매출 ({DM_YM}~)]")
        for r in r2:
            print(f"    {r['년월']}: {float(r['매출액'] or 0):,.0f}원 / {r['품목수']}품목")
    else:
        print(f"  [WARN] {DM_YM} 이후 매출 없음 → 8월 데이터 미반영 또는 코드 불일치")

    print()

    # 추천 상품 구매 이력
    r3 = q(f"""
        SELECT `년월`, `상품명`, ROUND(SUM(`매출액`)) AS 매출액
        FROM {T_MAIN}
        WHERE `거래처`='{TARGET_CUST}' AND `상품코드`='{TARGET_MATNR}' AND `년월`>='202601'
        GROUP BY `년월`, `상품명` ORDER BY `년월`
    """)
    print(f"  [추천 상품 {TARGET_MATNR} 구매 이력]")
    if r3:
        for r in r3:
            flag = " ← DM 이후 구매!" if str(r['년월']) >= DM_YM else ""
            print(f"    {r['년월']}  {r['상품명']}: {float(r['매출액'] or 0):,.0f}원{flag}")
    else:
        print(f"    없음 (2026년 전체)")

    print()

    # 수정 후 dry-run
    r4 = q(f"""
        SELECT
          ROUND(SUM(COALESCE(`매출액`,0))/10000) AS total_after_만원,
          ROUND(SUM(CASE WHEN `상품코드`='{TARGET_MATNR}' THEN COALESCE(`매출액`,0) ELSE 0 END)/10000) AS dm품목_만원
        FROM {T_MAIN}
        WHERE `거래처`='{TARGET_CUST}' AND `년월`>='{DM_YM}'
    """)
    print(f"  [수정 후 dry-run: action_ym={DM_YM}]")
    if r4:
        r = r4[0]
        tot = float(r.get("total_after_만원") or 0)
        dm  = float(r.get("dm품목_만원") or 0)
        print(f"    DM 이후 전체 매출  : {tot:,.0f}만원")
        print(f"    추천 상품 매출     : {dm:,.0f}만원")
        print()
        if dm > 0:
            print("  ✅ 추천 상품 구매 확인 → 수정 후 집계 가능")
        elif tot > 0:
            print("  ⚠️  DM 이후 매출은 있으나 추천 상품 구매 미확인 (다른 상품 구매만 있음)")
        else:
            print("  ❌ DM 이후 매출 데이터 없음 → 8월 데이터 미반영일 가능성 높음")
            print("     8월 데이터 반영 후 재확인 권장")

except Exception as e:
    print(f"  [ERROR] {e}")
    import traceback; traceback.print_exc()

print()
print("=" * 70)
print("검증 완료")
print("=" * 70)
