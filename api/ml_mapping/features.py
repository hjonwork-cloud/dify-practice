"""
공용 피처 추출 모듈 (부작용 없음 — 오프라인 학습 스크립트와 온라인 섀도우 스코어링 양쪽에서 재사용).

price_monitor_router.py의 _tokenize/_strip_seller/_parse_volume 로직을 그대로 복제하되,
이 모듈은 순수 함수만 포함하므로 (Databricks 연결/백그라운드 스레드 등 부작용 없음)
온라인 FastAPI 프로세스에서 안전하게 import할 수 있다.
"""
from __future__ import annotations

import re

try:
    import rapidfuzz.fuzz as _fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False


_STOP = {
    '냉동', '냉장', '자숙',
    '슬라이스', '신선', '건조', '원물', '국산', '수입',
    '일반', '특대', '대용량', '소포장', '개별', '낱개', '원터치', '직배송',
    '무료배송', '당일배송', '묶음', '세트', '팩', '개입', '입점',
    '필리핀산', '국내산', '수입산', '베트남산', '미국산', '호주산',
    'new', 'ea',
}

FEATURE_COLS = [
    "jaccard", "overlap_coef", "n_common_tokens", "n_kw_matched",
    "fuzz_ratio", "fuzz_token_sort", "fuzz_token_set", "fuzz_partial",
    "vol_same_unit", "vol_ratio",
    "price_ratio", "has_price_data",
    "temp_match",
    "name_len_diff", "plat_name_len", "our_name_len",
]


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


def extract_features(platform_name: str, our_name: str,
                      platform_price, our_sale_price, our_buy_price,
                      our_prod: dict | None = None) -> dict:
    """_score_mapping과 동일 계열의 세부 피처를 '분해된' 형태로 산출.
    rapidfuzz 미설치 환경에서는 fuzz_* 피처를 0.0으로 채워 동작은 유지(섀도우 모드 비활성 권장)."""
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

    if _HAS_RAPIDFUZZ:
        fuzz_ratio = _fuzz.ratio(platform_name or "", our_name or "") / 100.0
        fuzz_token_sort = _fuzz.token_sort_ratio(platform_name or "", our_name or "") / 100.0
        fuzz_token_set = _fuzz.token_set_ratio(platform_name or "", our_name or "") / 100.0
        fuzz_partial = _fuzz.partial_ratio(platform_name or "", our_name or "") / 100.0
    else:
        fuzz_ratio = fuzz_token_sort = fuzz_token_set = fuzz_partial = 0.0

    pv, pu = parse_volume(platform_name)
    ov, ou = parse_volume(our_name)
    vol_ratio = -1.0
    vol_same_unit = 0
    if pv and ov and pu == ou:
        vol_same_unit = 1
        vol_ratio = min(pv, ov) / max(pv, ov)

    price_ratio = -1.0
    has_price_data = 0
    if platform_price and our_sale_price and our_sale_price > 0:
        try:
            price_ratio = float(platform_price) / float(our_sale_price)
            has_price_data = 1
        except Exception:
            pass

    temp_match = -1
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
                temp_match = 1

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
        "vol_ratio": vol_ratio,
        "price_ratio": price_ratio,
        "has_price_data": has_price_data,
        "temp_match": temp_match,
        "name_len_diff": name_len_diff,
        "plat_name_len": len(platform_name or ""),
        "our_name_len": len(our_name or ""),
    }
