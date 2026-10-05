"""
Phase 2: GBDT 매핑 랭킹 모델 학습 + 평가

- 입력: ml_mapping/data/train_dataset.csv (Phase 0에서 생성)
- 그룹 기준(product_key) train/test 분할 → 같은 플랫폼상품의 후보들이
  train/test에 걸쳐 섞이는 데이터 누출(leakage) 방지
- 모델: LightGBM (binary classification, 각 (플랫폼상품, 우리상품) 쌍의 매핑 확률)
- 평가: 그룹별로 모델 점수가 가장 높은 후보가 실제 정답(label=1)인 비율 (Top-1 정확도)
        + 상위 3개 후보 중 정답 포함 비율 (Top-3 재현율)
        + 기존 휴리스틱 스코어 간이 재현(overlap/fuzz 기반) 대비 비교
"""
from __future__ import annotations

import os
import json
import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import roc_auc_score, average_precision_score
import lightgbm as lgb

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(_THIS_DIR, "data", "train_dataset.csv")
MODEL_DIR = os.path.join(_THIS_DIR, "model")
os.makedirs(MODEL_DIR, exist_ok=True)

FEATURE_COLS = [
    "jaccard", "overlap_coef", "n_common_tokens", "n_kw_matched",
    "fuzz_ratio", "fuzz_token_sort", "fuzz_token_set", "fuzz_partial",
    "vol_same_unit", "vol_ratio",
    "price_ratio", "has_price_data",
    "temp_match",
    "name_len_diff", "plat_name_len", "our_name_len",
]


def _topk_metrics(df: pd.DataFrame, score_col: str) -> dict:
    """product_key 그룹별로 score_col 내림차순 정렬 후 Top-1/Top-3 정답포함률 계산.
    label=1 후보가 그룹에 아예 없는 경우(= 정답이 네거티브 샘플링 단계에서 빠진 경우)는 평가 제외."""
    n_top1_hit = 0
    n_top3_hit = 0
    n_groups = 0
    for pkey, g in df.groupby("product_key"):
        if g["label"].sum() == 0:
            continue  # 정답 후보가 그룹에 없음 → 평가 불가 그룹 제외
        n_groups += 1
        g_sorted = g.sort_values(score_col, ascending=False)
        top1 = g_sorted.iloc[0]
        if top1["label"] == 1:
            n_top1_hit += 1
        top3 = g_sorted.iloc[:3]
        if (top3["label"] == 1).any():
            n_top3_hit += 1
    return {
        "n_groups": n_groups,
        "top1_acc": round(n_top1_hit / max(n_groups, 1), 4),
        "top3_recall": round(n_top3_hit / max(n_groups, 1), 4),
    }


def main():
    df = pd.read_csv(DATA_PATH)
    print(f"전체 레코드: {len(df):,} | positive: {(df.label==1).sum():,} | negative: {(df.label==0).sum():,}")

    # 그룹(product_key) 단위 분할 — 동일 플랫폼상품의 후보 쌍들이 train/test에 걸쳐 분산되지 않도록
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, test_idx = next(gss.split(df, groups=df["product_key"]))
    train_df = df.iloc[train_idx].reset_index(drop=True)
    test_df = df.iloc[test_idx].reset_index(drop=True)
    print(f"Train: {len(train_df):,}건 ({train_df['product_key'].nunique():,} 그룹) | "
          f"Test: {len(test_df):,}건 ({test_df['product_key'].nunique():,} 그룹)")

    X_train = train_df[FEATURE_COLS].fillna(-1)
    y_train = train_df["label"]
    X_test = test_df[FEATURE_COLS].fillna(-1)
    y_test = test_df["label"]

    # 클래스 불균형(1:3 negative 비중) 보정
    pos_weight = (y_train == 0).sum() / max((y_train == 1).sum(), 1)

    model = lgb.LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        max_depth=5,
        num_leaves=31,
        min_child_samples=15,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=pos_weight,
        random_state=42,
        verbose=-1,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        eval_metric="auc",
        callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
    )

    # ── 전체 성능 지표 ──
    test_proba = model.predict_proba(X_test)[:, 1]
    auc = roc_auc_score(y_test, test_proba)
    ap = average_precision_score(y_test, test_proba)
    print(f"\n[전체 Test 지표] AUC={auc:.4f} | AveragePrecision={ap:.4f}")

    # ── 그룹별 Top-1/Top-3 평가 (모델 vs 휴리스틱 베이스라인) ──
    test_df = test_df.copy()
    test_df["ml_score"] = test_proba
    # 베이스라인: 기존 _score_mapping과 유사하게 텍스트 피처들을 단순 가중합한 근사치
    test_df["heuristic_score"] = (
        test_df["overlap_coef"] * 0.5 + test_df["fuzz_token_set"] * 0.3 + test_df["n_kw_matched"] * 0.05
    )

    ml_metrics = _topk_metrics(test_df, "ml_score")
    heuristic_metrics = _topk_metrics(test_df, "heuristic_score")
    print(f"\n[그룹별 랭킹 평가 — 테스트셋 {ml_metrics['n_groups']}개 product_key 그룹]")
    print(f"  ML 모델     : Top-1 정확도={ml_metrics['top1_acc']*100:.1f}%  Top-3 재현율={ml_metrics['top3_recall']*100:.1f}%")
    print(f"  휴리스틱근사: Top-1 정확도={heuristic_metrics['top1_acc']*100:.1f}%  Top-3 재현율={heuristic_metrics['top3_recall']*100:.1f}%")

    # ── Feature Importance ──
    importances = sorted(
        zip(FEATURE_COLS, model.feature_importances_),
        key=lambda x: -x[1],
    )
    print("\n[Feature Importance (gain 기준 아님, split count)]")
    for feat, imp in importances:
        print(f"  {feat:20s} {imp}")

    # ── 모델 + 메타데이터 저장 ──
    model_path = os.path.join(MODEL_DIR, "mapping_ranker.txt")
    model.booster_.save_model(model_path)
    meta = {
        "feature_cols": FEATURE_COLS,
        "trained_at": pd.Timestamp.now().isoformat(),
        "n_train": len(train_df),
        "n_test": len(test_df),
        "test_auc": round(float(auc), 4),
        "test_ap": round(float(ap), 4),
        "ml_top1_acc": ml_metrics["top1_acc"],
        "ml_top3_recall": ml_metrics["top3_recall"],
        "heuristic_top1_acc": heuristic_metrics["top1_acc"],
        "heuristic_top3_recall": heuristic_metrics["top3_recall"],
    }
    with open(os.path.join(MODEL_DIR, "model_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 모델 저장: {model_path}")
    print(f"✓ 메타 저장: {os.path.join(MODEL_DIR, 'model_meta.json')}")


if __name__ == "__main__":
    main()
