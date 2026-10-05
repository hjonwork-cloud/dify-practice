"""
Phase 0+1: AI 매핑 알고리즘 개선 — 학습 데이터셋 구축 + 피처 엔지니어링

목적:
  - 운영 DB(price_map_product_link, pm_feedback.json)의 매핑 이력을 학습 데이터로 변환
  - Positive: 활성 매핑(플랫폼상품 ↔ 우리상품) — "정답" 쌍
  - Hard Negative:
      (a) 피드백 캐시에 기록된 ai_suggested_code (AI가 1순위로 틀리게 제안한 상품)
      (b) 토큰 역인덱스로 뽑은 "텍스트는 비슷하지만 정답이 아닌" 후보 상품들
  - 각 쌍에 대해 _score_mapping과 동일 계열의 세부 피처(원점수가 아닌 분해된 피처)를 계산해
    CSV로 저장 → Phase 2(GBDT 학습)에서 바로 사용

실행:
  python ml_mapping/phase0_build_dataset.py
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import sqlite3
import tempfile
import datetime
from collections import defaultdict, Counter

import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_API_DIR = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _API_DIR)

TOKEN_FILE = os.path.join(_API_DIR, ".token_cache")
HOST = "adb-707807361397497.17.azuredatabricks.net"
HTTP_PATH = "/sql/1.0/warehouses/acc2ec933ffef2d0"

T_ZSDR = "h_hmfo_fsi.gd_fsi_ent.sap_zsdr0017_order_linkage_status_d"
T_ZMM60 = "h_hmfo_fsi.gd_fsi_ent.sap_zmm60_material_master_d"
T_MAIN = "h_hmfo_fsi_dm.gd_rst_ing.sales_custmasters_compat_v"  # main.py의 T_MAIN과 동일

PLANTS_REAL = ["4120", "4123"]

PROD_DB_PATH = os.path.join(tempfile.gettempdir(), "prod_portal_activity.db")
PROD_FEEDBACK_PATH = os.path.join(tempfile.gettempdir(), "prod_pm_feedback.json")

OUT_DIR = os.path.join(_THIS_DIR, "data")
os.makedirs(OUT_DIR, exist_ok=True)


# ── Databricks 연결 (crawl_platform_prices.py와 동일 패턴, 앱 모듈 import 없이 독립 실행) ──

def _get_token() -> str:
    env_pat = os.getenv("DATABRICKS_TOKEN", "").strip()
    if env_pat:
        return env_pat
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    raise RuntimeError(f"Databricks 토큰을 찾을 수 없습니다 ({TOKEN_FILE})")


def _get_conn():
    import databricks.sql as dbsql
    token = os.getenv("DATABRICKS_TOKEN", "").strip()
    if not token and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()

    if token:
        try:
            conn = dbsql.connect(server_hostname=HOST, http_path=HTTP_PATH, access_token=token)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            print("  ✓ PAT 토큰으로 연결 성공")
            return conn
        except Exception as e:
            print(f"  ⚠ PAT 토큰 연결 실패 ({e}), 브라우저 인증 시도...")

    print("  브라우저 로그인 팝업이 열립니다...")
    from databricks.sdk import WorkspaceClient
    wc = WorkspaceClient(host=f"https://{HOST}", auth_type="external-browser")
    me = wc.current_user.me()
    print(f"  ✓ 로그인: {me.user_name}")
    new_token = None
    try:
        creds = wc.config.authenticate()
        new_token = creds.get("Authorization", "").replace("Bearer ", "")
    except Exception:
        pass
    if not new_token:
        raise RuntimeError("브라우저 인증 후 토큰 추출 실패.")
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(new_token)
    return dbsql.connect(server_hostname=HOST, http_path=HTTP_PATH, access_token=new_token)


def _q(conn, sql: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── our_products 조회 (price_monitor_router._get_our_products와 동일 쿼리, plant='ALL') ──

def fetch_our_products(conn) -> list[dict]:
    plant_cond_op = f"z.`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
    sql = f"""
        SELECT
            z.`상품코드`                                    AS product_code,
            COALESCE(MAX(m.`상품명`), z.`상품코드`)        AS product_name,
            MAX(m.`자재유형명`)                            AS brand,
            MAX(m.`단위`)                                  AS unit,
            MAX(m.`자재그룹명`)                            AS product_group,
            MAX(m.`자재그룹`)                              AS material_group,
            MAX(m.`대분류`)                                AS category,
            MAX(m.`중분류`)                                AS mid_category,
            MAX(m.`소분류`)                                AS sub_category,
            MAX(m.`총중량`)                                AS total_weight,
            MAX(m.`순중량`)                                AS net_weight,
            MAX(m.`온도조건`)                               AS temp_cond,
            MAX(COALESCE(m.`세금분류명`, '과세'))            AS tax_class
        FROM {T_ZSDR} z
        LEFT JOIN {T_ZMM60} m ON z.`상품코드` = m.`상품코드`
        WHERE {plant_cond_op}
          AND z.`배치` IN ('01','03')
          AND COALESCE(m.`자재그룹`, '') != '5140'
        GROUP BY z.`상품코드`
        ORDER BY COALESCE(MAX(m.`상품명`), z.`상품코드`)
        LIMIT 100000
    """
    print("[1/5] our_products 조회 중...")
    rows = _q(conn, sql)
    print(f"  ✓ {len(rows):,}개")
    return rows


def fetch_base_prices(conn) -> list[dict]:
    plant_cond_bp = f"`플랜트` IN ({', '.join(repr(p) for p in PLANTS_REAL)})"
    sql = f"""
        SELECT
            `자재`                                                        AS product_code,
            ROUND(SUM(CAST(`매출액` AS DOUBLE)) / NULLIF(SUM(CAST(`매출수량` AS DOUBLE)), 0) * 100, 2) AS avg_sale_price,
            ROUND(SUM(CAST(`매출원가` AS DOUBLE)) / NULLIF(SUM(CAST(`매출수량` AS DOUBLE)), 0) * 100, 2) AS avg_buy_price
        FROM {T_MAIN}
        WHERE {plant_cond_bp}
          AND CAST(`년월` AS INT) >= YEAR(DATE_SUB(CURRENT_DATE(), 60)) * 100 + MONTH(DATE_SUB(CURRENT_DATE(), 60))
          AND `자재` IS NOT NULL
          AND `매출수량` > 0
          AND `매출원가` IS NOT NULL
        GROUP BY `자재`
    """
    print("[2/5] base_prices 조회 중...")
    try:
        rows = _q(conn, sql)
        print(f"  ✓ {len(rows):,}개")
        return rows
    except Exception as e:
        print(f"  ⚠ base_prices 조회 실패(스킵): {e}")
        return []


# ── 운영 DB(매핑/피드백) 로드 ──────────────────────────────────────────────

def load_prod_mappings() -> list[dict]:
    print("[3/5] 운영 매핑 테이블 로드 중...")
    conn = sqlite3.connect(PROD_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM price_map_product_link WHERE is_active=1"
    ).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    print(f"  ✓ 활성 매핑 {len(rows):,}건")
    return rows


def load_prod_feedback() -> dict:
    if not os.path.exists(PROD_FEEDBACK_PATH):
        return {}
    with open(PROD_FEEDBACK_PATH, encoding="utf-8") as f:
        fb = json.load(f)
    print(f"  ✓ 피드백(하드네거티브 소스) {len(fb):,}건")
    return fb


# ── 순수 텍스트 유틸 (price_monitor_router.py의 로직을 부작용 없이 복제) ──────
# ※ price_monitor_router를 직접 import하면 모듈 로드시 백그라운드 Databricks 프리로드
#   스레드/다른 라우터 의존성이 함께 실행되므로, 배치 스크립트에서는 순수 함수만 복제한다.

def strip_seller(name: str, seller_name: str) -> str:
    cleaned = (name or "").strip()
    cleaned = re.sub(r'\[[^\]]*\]', ' ', cleaned)
    if seller_name:
        seller_words = [w.strip() for w in re.split(r'[\s\-_/·]+', seller_name) if len(w.strip()) >= 2]
        for word in seller_words:
            cleaned = re.sub(
                r'(?<![가-힣a-zA-Z0-9])' + re.escape(word) + r'(?![가-힣a-zA-Z0-9])',
                ' ', cleaned, flags=re.IGNORECASE
            )
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if len(cleaned) >= 2 else (name or "").strip()


def parse_volume(name: str):
    text = (name or "").lower()
    patterns = [
        (re.compile(r'(\d[\d.]+)\s*l\b'), lambda m: float(m.group(1)) * 1000, 'ml'),
        (re.compile(r'(\d[\d.]*)\s*ml\b'), lambda m: float(m.group(1)), 'ml'),
        (re.compile(r'(\d[\d.]+)\s*kg\b'), lambda m: float(m.group(1)) * 1000, 'g'),
        (re.compile(r'(\d[\d.]*)\s*g\b'), lambda m: float(m.group(1)), 'g'),
        (re.compile(r'(\d[\d.]*)\s*키로\b'), lambda m: float(m.group(1)) * 1000, 'g'),
    ]
    for pat, fn, unit in patterns:
        m = pat.search(text)
        if m:
            try:
                val = fn(m)
                if val > 0:
                    return val, unit
            except Exception:
                pass
    return None, None


def tokenize(name: str) -> set:
    name = (name or "").strip()
    name = re.sub(r'[/·•\-_,\(\)\[\]{}]', ' ', name)
    tokens = set()
    for t in re.split(r'\s+', name):
        t = t.strip()
        if len(t) >= 2:
            tokens.add(t.lower())
            korean = re.sub(r'[^가-힣]', '', t)
            if len(korean) >= 5:
                for i in range(len(korean) - 1):
                    tokens.add(korean[i:i + 2])
                for i in range(len(korean) - 2):
                    tokens.add(korean[i:i + 3])
    return tokens


_STOP = {
    '냉동', '냉장', '자숙',
    '슬라이스', '신선', '건조', '원물', '국산', '수입',
    '일반', '특대', '대용량', '소포장', '개별', '낱개', '원터치', '직배송',
    '무료배송', '당일배송', '묶음', '세트', '팩', '개입', '입점',
    '필리핀산', '국내산', '수입산', '베트남산', '미국산', '호주산',
    'new', 'ea',
}


# ── 피처 추출: _score_mapping의 하위 구성요소를 "분해된 피처"로 산출 ────────
# (최종 스코어 1개가 아니라, GBDT가 스스로 가중치를 학습하도록 원재료 피처를 제공)

def extract_features(platform_name: str, our_name: str,
                      platform_price, our_sale_price, our_buy_price,
                      our_prod: dict | None = None) -> dict:
    import rapidfuzz.fuzz as fuzz

    pt = tokenize(platform_name)
    ot = tokenize(our_name)
    common = pt & ot
    common_text = {t for t in common if t not in _STOP}

    jaccard = len(common_text) / max(len(pt | ot), 1)
    overlap_coef = len(common_text) / max(min(len(pt), len(ot)), 1)

    plat_meaningful = [t for t in pt if len(t) >= 3 and t not in _STOP and not re.match(r'^[\d.]+', t)]
    our_meaningful = [t for t in ot if len(t) >= 3 and t not in _STOP and not re.match(r'^[\d.]+', t)]
    our_lower = (our_name or "").lower()
    plat_lower = (platform_name or "").lower()
    n_kw_matched = sum(1 for w in plat_meaningful if w in our_lower)
    n_kw_matched += sum(1 for w in our_meaningful if w in plat_lower and w not in plat_meaningful)

    # 퍼지 문자열 유사도 (임베딩 모델 없이도 "의미적으로 가까움"을 어느 정도 대체)
    fuzz_ratio = fuzz.ratio(platform_name or "", our_name or "") / 100.0
    fuzz_token_sort = fuzz.token_sort_ratio(platform_name or "", our_name or "") / 100.0
    fuzz_token_set = fuzz.token_set_ratio(platform_name or "", our_name or "") / 100.0
    fuzz_partial = fuzz.partial_ratio(platform_name or "", our_name or "") / 100.0

    # 용량/중량 비교
    pv, pu = parse_volume(platform_name)
    ov, ou = parse_volume(our_name)
    vol_ratio = None
    vol_same_unit = 0
    if pv and ov and pu == ou:
        vol_same_unit = 1
        vol_ratio = min(pv, ov) / max(pv, ov)

    # 가격 비율
    price_ratio = None
    if platform_price and our_sale_price and our_sale_price > 0:
        try:
            price_ratio = float(platform_price) / float(our_sale_price)
        except Exception:
            price_ratio = None

    # 온도조건 일치
    temp_match = -1  # -1: 판단불가
    if our_prod:
        our_temp = (our_prod.get("temp_cond") or "").strip()
        if our_temp:
            plat_frozen = "냉동" in (platform_name or "")
            plat_chilled = "냉장" in (platform_name or "")
            if plat_frozen:
                temp_match = 1 if our_temp in {"30", "40"} else 0
            elif plat_chilled:
                temp_match = 1 if our_temp == "20" else 0
            else:
                temp_match = 1  # 플랫폼명에 온도 표기 없으면 중립(일치로 간주)

    name_len_diff = abs(len(platform_name or "") - len(our_name or ""))

    return {
        "jaccard": round(jaccard, 4),
        "overlap_coef": round(overlap_coef, 4),
        "n_common_tokens": len(common_text),
        "n_kw_matched": n_kw_matched,
        "fuzz_ratio": round(fuzz_ratio, 4),
        "fuzz_token_sort": round(fuzz_token_sort, 4),
        "fuzz_token_set": round(fuzz_token_set, 4),
        "fuzz_partial": round(fuzz_partial, 4),
        "vol_same_unit": vol_same_unit,
        "vol_ratio": vol_ratio if vol_ratio is not None else -1.0,
        "price_ratio": price_ratio if price_ratio is not None else -1.0,
        "has_price_data": 1 if price_ratio is not None else 0,
        "temp_match": temp_match,
        "name_len_diff": name_len_diff,
        "plat_name_len": len(platform_name or ""),
        "our_name_len": len(our_name or ""),
    }


def main():
    t0 = time.time()
    conn = _get_conn()
    our_products = fetch_our_products(conn)
    base_price_rows = fetch_base_prices(conn)
    conn.close()

    our_by_code = {p["product_code"]: p for p in our_products}
    base_by_code = {r["product_code"]: r for r in base_price_rows}

    mappings = load_prod_mappings()
    feedback = load_prod_feedback()

    # ── 토큰 역인덱스 구축 (하드네거티브 후보 생성용) ──
    print("[4/5] 역인덱스 구축 + 후보(하드네거티브) 생성 중...")
    tok_inv: dict[str, set] = defaultdict(set)
    for code, p in our_by_code.items():
        for t in tokenize(p.get("product_name") or ""):
            tok_inv[t].add(code)

    records = []
    seen_pairs = set()  # (product_key, our_product_code) 중복 방지

    skipped_no_our_product = 0
    for m in mappings:
        code = m.get("our_product_code")
        if code not in our_by_code:
            skipped_no_our_product += 1
            continue
        our_prod = our_by_code[code]
        bp = base_by_code.get(code, {})
        plat_name = m.get("product_name") or ""
        seller = m.get("seller_name") or ""
        clean_plat_name = strip_seller(plat_name, seller)

        pair_key = (m.get("product_key"), code)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        feats = extract_features(
            clean_plat_name, our_prod.get("product_name") or "",
            None, bp.get("avg_sale_price"), bp.get("avg_buy_price"),
            our_prod,
        )
        feats.update({
            "label": 1,
            "source": "mapping",
            "platform": m.get("platform"),
            "product_key": m.get("product_key"),
            "seller_name": seller,
            "our_product_code": code,
            "plat_name_raw": plat_name,
            "our_name_raw": our_prod.get("product_name"),
        })
        records.append(feats)

        # ── 하드네거티브: 같은 플랫폼상품 텍스트로 역인덱스 후보 상품 중
        #    정답이 아닌 것들을 최대 3개까지 negative로 추가 ──
        plat_toks = tokenize(clean_plat_name)
        cand_codes: Counter = Counter()
        for t in plat_toks:
            for c in tok_inv.get(t, ()):
                cand_codes[c] += 1
        # 자기 자신(정답) 제외, 토큰 공통 개수 많은 순
        cands = [c for c, _ in cand_codes.most_common(8) if c != code]
        neg_added = 0
        for ncode in cands:
            if neg_added >= 3:
                break
            npair = (m.get("product_key"), ncode)
            if npair in seen_pairs:
                continue
            seen_pairs.add(npair)
            nprod = our_by_code[ncode]
            nbp = base_by_code.get(ncode, {})
            nfeats = extract_features(
                clean_plat_name, nprod.get("product_name") or "",
                None, nbp.get("avg_sale_price"), nbp.get("avg_buy_price"),
                nprod,
            )
            nfeats.update({
                "label": 0,
                "source": "hard_neg_candidate",
                "platform": m.get("platform"),
                "product_key": m.get("product_key"),
                "seller_name": seller,
                "our_product_code": ncode,
                "plat_name_raw": plat_name,
                "our_name_raw": nprod.get("product_name"),
            })
            records.append(nfeats)
            neg_added += 1

    print(f"  ✓ 매핑 기반 positive+hard_neg {len(records):,}건 (our_product 누락으로 스킵 {skipped_no_our_product}건)")

    # ── 피드백 기반 hard negative: AI가 틀리게 제안(ai_suggested_code)한 건 ──
    fb_added = 0
    for pkey, fb in feedback.items():
        true_code = fb.get("our_product_code")
        wrong_code = fb.get("ai_suggested_code")
        plat_name = fb.get("platform_name") or ""
        if wrong_code and wrong_code != true_code and wrong_code in our_by_code:
            npair = (pkey, wrong_code)
            if npair in seen_pairs:
                continue
            seen_pairs.add(npair)
            nprod = our_by_code[wrong_code]
            nbp = base_by_code.get(wrong_code, {})
            nfeats = extract_features(
                plat_name, nprod.get("product_name") or "",
                None, nbp.get("avg_sale_price"), nbp.get("avg_buy_price"),
                nprod,
            )
            nfeats.update({
                "label": 0,
                "source": "feedback_wrong_suggestion",
                "platform": "",
                "product_key": pkey,
                "seller_name": "",
                "our_product_code": wrong_code,
                "plat_name_raw": plat_name,
                "our_name_raw": nprod.get("product_name"),
            })
            records.append(nfeats)
            fb_added += 1

        # 피드백의 "정답"도 positive로 추가 (이미 매핑 테이블에 있을 수 있으나 중복은 seen_pairs로 방지)
        if true_code and true_code in our_by_code:
            ppair = (pkey, true_code)
            if ppair not in seen_pairs:
                seen_pairs.add(ppair)
                pprod = our_by_code[true_code]
                pbp = base_by_code.get(true_code, {})
                pfeats = extract_features(
                    plat_name, pprod.get("product_name") or "",
                    None, pbp.get("avg_sale_price"), pbp.get("avg_buy_price"),
                    pprod,
                )
                pfeats.update({
                    "label": 1,
                    "source": "feedback_correct",
                    "platform": "",
                    "product_key": pkey,
                    "seller_name": "",
                    "our_product_code": true_code,
                    "plat_name_raw": plat_name,
                    "our_name_raw": pprod.get("product_name"),
                })
                records.append(pfeats)

    print(f"  ✓ 피드백 기반 레코드 추가 {fb_added}건(negative) + positive 보강")

    df = pd.DataFrame(records)
    out_path = os.path.join(OUT_DIR, "train_dataset.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"\n[5/5] 저장 완료: {out_path}")
    print(f"  총 레코드: {len(df):,} | positive: {(df['label']==1).sum():,} | negative: {(df['label']==0).sum():,}")
    print(f"  platform 분포: {df['platform'].value_counts().to_dict()}")
    print(f"  source 분포: {df['source'].value_counts().to_dict()}")
    print(f"  소요시간: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
