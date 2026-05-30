"""Pivot Step 1 — Retarget XGBoost: directional predictor -> FORWARD regime classifier.

WHY: Pure ML directional prediction is industry-known failure mode. Pivot validated
by 2 independent deep research outputs (Gemini + Qwen). Reframe ML from
"what WILL price do" (untractable) to "what WILL regime be" (tractable, durable).

CRITICAL: Earlier naive approach (label = current regime from current features)
produced 100% accuracy — a leakage artifact, NOT real ML. The model trivially
inverted our labeling function.

Honest approach: forward-looking regime prediction.
  - Regime label rules (deterministic, trailing-only):
      High_Volatility  : volatility_10 > p85
      Trending_Up      : adx > p60 AND ema_slope_5 > 0 AND not high vol
      Trending_Down    : adx > p60 AND ema_slope_5 < 0 AND not high vol
      Ranging          : otherwise
  - Training target: regime AT t + FORECAST_HORIZON (default 4 bars / 4 hours)
  - Features: snapshot AT t (NO peek at the future)
  - Result: real forecasting task. Expected accuracy 55-75% (above 25% chance baseline
    for 4 balanced classes, lower than perfect — reflects difficulty of forecasting
    market regime).

Production use:
  - At inference: compute current regime via deterministic rules (cheap, explainable).
  - Also use this model to anticipate next regime — useful for proactive position
    adjustment before transitions hit.

Usage:
    python scripts/05_train_regime_classifier.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
)
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "mnt_features.csv"
MODEL_OUT = Path(__file__).resolve().parents[1] / "models" / "xgb_regime.pkl"
IMPORTANCE_OUT = (
    Path(__file__).resolve().parents[1] / "data" / "feature_importance_regime.csv"
)
CONFUSION_OUT = (
    Path(__file__).resolve().parents[1] / "data" / "regime_confusion_matrix.csv"
)
META_OUT = Path(__file__).resolve().parents[1] / "models" / "regime_metadata.json"

# Forecast horizon: how many bars ahead to predict regime.
# 4 bars at hourly cadence = 4 hours = matches Allora's 8h topic timeframe / 2.
FORECAST_HORIZON = 4

FEATURES = [
    # Trend structure
    "ema20", "ema50", "ema100",
    "ema_gap_20_50", "ema_gap_50_100",
    "distance_ema20", "distance_ema50", "distance_ema100",
    # Oscillators (rsi+atr+adx kept — they help forecast, even though adx defines current regime)
    "rsi", "atr", "adx",
    # Trend momentum
    "ema_slope_5", "ema_slope_10",
    "momentum_3", "momentum_5", "momentum_10",
    "momentum_3_atr", "momentum_5_atr", "momentum_10_atr",
    # Multi-period returns
    "return_1", "return_3", "return_5", "return_10",
    # Volatility regime
    "volatility_10", "volatility_20", "volatility_ratio",
    # Volume regime
    "volume_change", "volume_ratio",
    # Binary regime
    "ema_cross", "price_above_ema20", "price_above_ema50", "price_above_ema100",
    "rsi_above_50", "rsi_overbought", "rsi_oversold",
    # Session
    "hour_utc", "asia_hours", "europe_hours", "us_hours",
]

REGIME_LABELS = ["Ranging", "Trending_Up", "Trending_Down", "High_Volatility"]
LABEL_TO_INT = {name: i for i, name in enumerate(REGIME_LABELS)}


def label_regimes(
    df: pd.DataFrame,
    adx_strong_threshold: float,
    vol_high_threshold: float,
) -> pd.Series:
    """Assign one regime per bar. Priority: High_Vol > Trending_(Up|Down) > Ranging.

    Uses ONLY trailing indicators (adx, volatility_10, ema_slope_5).
    Same rules used to label both training and test sets, with thresholds calibrated
    from training-set distribution to avoid look-ahead.
    """
    labels = pd.Series(index=df.index, dtype="object")

    high_vol_mask = df["volatility_10"] > vol_high_threshold
    strong_trend_mask = (df["adx"] > adx_strong_threshold) & ~high_vol_mask
    up_mask = df["ema_slope_5"] > 0
    down_mask = df["ema_slope_5"] < 0

    labels[high_vol_mask] = "High_Volatility"
    labels[strong_trend_mask & up_mask] = "Trending_Up"
    labels[strong_trend_mask & down_mask] = "Trending_Down"
    labels[labels.isna()] = "Ranging"

    return labels


def main() -> None:
    print(f"Loading {DATA_PATH}")
    df = pd.read_csv(DATA_PATH).reset_index(drop=True)
    print(f"  total bars: {len(df)}")

    # ===== Train / test split first (chronological) =====
    split = int(len(df) * 0.8)
    train_df = df.iloc[:split].reset_index(drop=True)
    test_df = df.iloc[split:].reset_index(drop=True)

    # Calibrate thresholds from TRAIN only
    adx_strong_threshold = float(train_df["adx"].quantile(0.60))
    vol_high_threshold = float(train_df["volatility_10"].quantile(0.85))
    print()
    print("=== Calibrated thresholds (from training set only) ===")
    print(f"  ADX strong-trend threshold (p60):  {adx_strong_threshold:.4f}")
    print(f"  Volatility high threshold (p85):   {vol_high_threshold:.6f}")

    # ===== Compute *current* regime per bar (for diagnostics) =====
    train_df["regime_now"] = label_regimes(train_df, adx_strong_threshold, vol_high_threshold)
    test_df["regime_now"] = label_regimes(test_df, adx_strong_threshold, vol_high_threshold)

    # ===== TARGET: regime at t + FORECAST_HORIZON =====
    train_df["regime_future"] = train_df["regime_now"].shift(-FORECAST_HORIZON)
    test_df["regime_future"] = test_df["regime_now"].shift(-FORECAST_HORIZON)
    train_df = train_df.dropna(subset=["regime_future"]).reset_index(drop=True)
    test_df = test_df.dropna(subset=["regime_future"]).reset_index(drop=True)

    print()
    print(f"=== Class distribution (target = regime at t+{FORECAST_HORIZON}h) ===")
    print(f"  Train ({len(train_df)}): {Counter(train_df['regime_future'])}")
    print(f"  Test  ({len(test_df)}):  {Counter(test_df['regime_future'])}")

    # ===== Persistence baseline (predict "regime stays the same") =====
    persistence_train = (train_df["regime_now"] == train_df["regime_future"]).mean()
    persistence_test = (test_df["regime_now"] == test_df["regime_future"]).mean()
    print()
    print(f"=== Persistence baseline (current regime == future regime) ===")
    print(f"  Train: {persistence_train * 100:.2f}%")
    print(f"  Test:  {persistence_test * 100:.2f}%")
    print(f"  (Beat this to demonstrate the model captures regime transitions, not just inertia.)")

    train_df["y"] = train_df["regime_future"].map(LABEL_TO_INT).astype(int)
    test_df["y"] = test_df["regime_future"].map(LABEL_TO_INT).astype(int)

    X_train = train_df[FEATURES]
    y_train = train_df["y"]
    X_test = test_df[FEATURES]
    y_test = test_df["y"]

    # ===== TimeSeriesSplit CV =====
    print()
    print("=== TimeSeriesSplit CV (5 folds) ===")
    tscv = TimeSeriesSplit(n_splits=5)
    cv_scores: list[float] = []
    for fold, (tr_idx, te_idx) in enumerate(tscv.split(X_train), 1):
        model_cv = XGBClassifier(
            n_estimators=400,
            max_depth=5,
            learning_rate=0.025,
            subsample=0.85,
            colsample_bytree=0.85,
            min_child_weight=4,
            gamma=0.1,
            reg_alpha=0.1,
            reg_lambda=1.0,
            objective="multi:softprob",
            num_class=len(REGIME_LABELS),
            eval_metric="mlogloss",
            random_state=42,
            n_jobs=-1,
        )
        model_cv.fit(X_train.iloc[tr_idx], y_train.iloc[tr_idx])
        acc_cv = accuracy_score(y_train.iloc[te_idx], model_cv.predict(X_train.iloc[te_idx]))
        cv_scores.append(acc_cv)
        print(f"  Fold {fold}: accuracy {acc_cv * 100:.2f}%")
    cv_mean, cv_std = float(np.mean(cv_scores)), float(np.std(cv_scores))
    print(f"  Mean: {cv_mean * 100:.2f}% (+/- {cv_std * 100:.2f}%)")

    # ===== Final fit + evaluate =====
    print()
    print(f"=== Final fit: train={len(X_train)}, test={len(X_test)} ===")
    final_model = XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.025,
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=4,
        gamma=0.1,
        reg_alpha=0.1,
        reg_lambda=1.0,
        objective="multi:softprob",
        num_class=len(REGIME_LABELS),
        eval_metric="mlogloss",
        random_state=42,
        n_jobs=-1,
    )
    final_model.fit(X_train, y_train)

    pred_test = final_model.predict(X_test)
    proba_test = final_model.predict_proba(X_test)
    test_acc = accuracy_score(y_test, pred_test)
    print(f"  Test accuracy: {test_acc * 100:.2f}%   (persistence baseline {persistence_test * 100:.2f}%)")
    print()
    print(classification_report(y_test, pred_test, target_names=REGIME_LABELS, digits=3, zero_division=0))

    cm = confusion_matrix(y_test, pred_test, labels=list(range(len(REGIME_LABELS))))
    cm_df = pd.DataFrame(
        cm,
        index=[f"true_{n}" for n in REGIME_LABELS],
        columns=[f"pred_{n}" for n in REGIME_LABELS],
    )
    print("=== Confusion matrix (test) ===")
    print(cm_df.to_string())

    # Confidence threshold analysis
    max_proba = proba_test.max(axis=1)
    print()
    print("=== Coverage vs accuracy by confidence threshold ===")
    for th in (0.40, 0.50, 0.60, 0.70, 0.75, 0.80):
        mask = max_proba >= th
        n = int(mask.sum())
        if n > 0:
            acc_at_th = accuracy_score(y_test[mask], pred_test[mask])
            print(f"  conf >= {th:.2f}: {mask.mean() * 100:5.1f}% coverage ({n:3d} bars), accuracy {acc_at_th * 100:.2f}%")
        else:
            print(f"  conf >= {th:.2f}: 0% coverage")

    # ===== Save artifacts =====
    importance_df = (
        pd.DataFrame({"feature": FEATURES, "importance": final_model.feature_importances_})
        .sort_values("importance", ascending=False)
        .reset_index(drop=True)
    )
    print()
    print("=== Top 12 features ===")
    print(importance_df.head(12).to_string(index=False))

    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": final_model,
            "features": FEATURES,
            "labels": REGIME_LABELS,
            "label_to_int": LABEL_TO_INT,
            "adx_strong_threshold": adx_strong_threshold,
            "vol_high_threshold": vol_high_threshold,
            "forecast_horizon_bars": FORECAST_HORIZON,
        },
        MODEL_OUT,
    )
    importance_df.to_csv(IMPORTANCE_OUT, index=False)
    cm_df.to_csv(CONFUSION_OUT)
    META_OUT.write_text(json.dumps({
        "model_type": "forward_regime_classifier",
        "asset": "MNT/USD",
        "data_source": "CoinGecko market_chart days=90 (~hourly cadence)",
        "forecast_horizon_bars": FORECAST_HORIZON,
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "n_features": len(FEATURES),
        "n_classes": len(REGIME_LABELS),
        "classes": REGIME_LABELS,
        "cv_mean_accuracy": cv_mean,
        "cv_std_accuracy": cv_std,
        "test_accuracy": float(test_acc),
        "persistence_baseline_test": float(persistence_test),
        "thresholds": {
            "adx_strong": adx_strong_threshold,
            "vol_high": vol_high_threshold,
        },
        "model_class": "XGBClassifier (multi:softprob)",
    }, indent=2))

    print()
    print(f"Saved model artifact     -> {MODEL_OUT}")
    print(f"Saved feature importance -> {IMPORTANCE_OUT}")
    print(f"Saved confusion matrix   -> {CONFUSION_OUT}")
    print(f"Saved metadata           -> {META_OUT}")


if __name__ == "__main__":
    main()
