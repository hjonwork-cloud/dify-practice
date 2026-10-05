"""
Phase 3/4: 학습된 매핑 랭킹 모델(LightGBM) 추론 래퍼.

- 섀도우 모드(Phase 3): 기존 휴리스틱 점수와 나란히 계산해 로그만 남김 (사용자 노출 없음)
- 운영 전환(Phase 4): PM_AI_USE_ML_SCORE=1 환경변수로 실제 랭킹에 모델 점수 사용

프로덕션에 lightgbm/rapidfuzz가 설치되지 않았거나 모델 아티팩트가 없는 경우에도
앱이 깨지지 않도록 전부 optional-import + lazy-load로 처리한다.
"""
from __future__ import annotations

import os
import json
import threading
import logging

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(_THIS_DIR, "model", "mapping_ranker.txt")
META_PATH = os.path.join(_THIS_DIR, "model", "model_meta.json")

_lock = threading.Lock()
_model = None          # lgb.Booster | None
_meta: dict = {}
_load_attempted = False
_available = False     # 모델 + 의존성 모두 로드 성공 시 True


def _try_load():
    global _model, _meta, _load_attempted, _available
    if _load_attempted:
        return
    with _lock:
        if _load_attempted:
            return
        _load_attempted = True
        try:
            import lightgbm as lgb  # optional dependency
        except ImportError:
            logger.info("[ml_mapping] lightgbm 미설치 — ML 스코어링 비활성(휴리스틱만 사용)")
            return
        if not os.path.exists(MODEL_PATH):
            logger.info(f"[ml_mapping] 모델 아티팩트 없음({MODEL_PATH}) — ML 스코어링 비활성")
            return
        try:
            _model = lgb.Booster(model_file=MODEL_PATH)
            if os.path.exists(META_PATH):
                with open(META_PATH, encoding="utf-8") as f:
                    _meta = json.load(f)
            _available = True
            logger.info(f"[ml_mapping] 모델 로드 성공 (test_auc={_meta.get('test_auc')}, "
                        f"top1_acc={_meta.get('ml_top1_acc')})")
        except Exception as e:
            logger.warning(f"[ml_mapping] 모델 로드 실패: {e}")


def is_available() -> bool:
    _try_load()
    return _available


def get_meta() -> dict:
    _try_load()
    return _meta


def score_pair(platform_name: str, our_name: str,
               platform_price=None, our_sale_price=None, our_buy_price=None,
               our_prod: dict | None = None) -> float | None:
    """(플랫폼상품명, 우리상품명) 쌍의 ML 매핑 확률(0~100 스케일) 반환.
    모델/의존성 미가용 시 None (호출부에서 휴리스틱 폴백 처리)."""
    _try_load()
    if not _available or _model is None:
        return None
    try:
        from .features import extract_features, FEATURE_COLS
    except ImportError:
        from features import extract_features, FEATURE_COLS  # 스크립트 직접 실행 시
    try:
        feats = extract_features(platform_name, our_name, platform_price,
                                  our_sale_price, our_buy_price, our_prod)
        row = [[feats.get(c, -1) for c in FEATURE_COLS]]
        proba = _model.predict(row)[0]
        return round(float(proba) * 100.0, 1)
    except Exception as e:
        logger.warning(f"[ml_mapping] score_pair 실패: {e}")
        return None
