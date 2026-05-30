"""Train XGBoost binary classifier on MNT features with TimeSeriesSplit CV.

Methodology adapted from the user's AI-for-Trading XGBoost work:
  - TimeSeriesSplit (5 folds) — leakage-safe time-series validation
  - scale_pos_weight handles class imbalance
  - Final model trained on 80% earliest data, validated on 20% latest
  - Save model artifact + feature_importance + metadata for Signal Agent

Confidence thresholds are tuned in a downstream step (04_tune_confidence.py).

Usage:
    python scripts/03_train_model.py
Output:
    models/xgb_mnt.pkl
    data/feature_importance_xgb_mnt.csv
    models/metadata.json
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "mnt_features.csv"
MODEL_OUT = Path(__file__).resolve().parents[1] / "models" / "xgb_mnt.pkl"
IMPORTANCE_OUT = (
    Path(__file__).resolve().parents[1] / "data" / "feature_importance_xgb_mnt.csv"
)
META_OUT = Path(__file__).resolve().parents[1] / "models" / "metadata.json"

FEATURES = [
    # Trend
    "ema20", "ema50", "ema100",
    "ema_slope_5", "ema_slope_10",
    "ema_gap_20_50", "ema_gap_50_100",
    "distance_ema20", "distance_ema50", "distance_ema100",
    # Oscillators
    "rsi", "atr", "adx",
    # Momentum
    "momentum_3", "momentum_5", "momentum_10",
    "momentum_3_atr", "momentum_5_atr", "momentum_10_atr",
    # Returns
    "return_1", "return_3", "return_5", "return_10",
    # Volatility regime
    "volatility_10", "volatility_20", "volatility_ratio",
    # Volume
    "volume_change", "volume_ratio",
    # Binary regime
    "ema_cross", "price_above_ema20", "price_above_ema50", "price_above_ema100",
    "rsi_above_50", "rsi_overbought", "rsi_oversold",
    # Session-of-day
    "hour_utc", "asia_hours", "europe_hours", "us_hours",
]


def cross_validate(X: pd.DataFrame, y: pd.Series) -> list[float]:
    tscv = TimeSeriesSplit(n_splits=5)
    scores: list[float] = []
    for fold, (train_idx, test_idx) in enumerate(tscv.split(X), 1):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]

        n0 = int((y_tr == 0).sum())
        n1 = int((y_tr == 1).sum())
        spw = n0 / max(n1, 1)

        model = XGBClassifier(
            n_estimators=400,
            max_depth=4,
            learning_rate=0.025,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=5,
            gamma=0.2,
            reg_alpha=0.1,
            reg_lambda=1.0,
            objective="binary:logistic",
            eval_metric="logloss",
            scale_pos_weight=spw,
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_tr, y_tr)
        pred = model.predict(X_te)
        acc = accuracy_score(y_te, pred)
        scores.append(acc)
        print(f"  Fold {fold}: accuracy {acc * 100:.2f}%  (n_train={len(X_tr)}, n_test={len(X_te)})")
    return scores


def main() -> None:
    print(f"Loading {DATA_PATH}")
    df = pd.read_csv(DATA_PATH)
    print(f"  rows: {len(df)}")

    X = df[FEATURES]
    y = df["target"].astype(int)

    print("\n=== TimeSeriesSplit CV ===")
    cv_scores = cross_validate(X, y)
    print(f"  mean accuracy: {float(np.mean(cv_scores)) * 100:.2f}% "
          f"(±{float(np.std(cv_scores)) * 100:.2f}%)")

    # Final fit on 80% earliest, validate on 20% latest
    split = int(len(df) * 0.8)
    X_tr, X_te = X.iloc[:split], X.iloc[split:]
    y_tr, y_te = y.iloc[:split], y.iloc[split:]
    n0 = int((y_tr == 0).sum())
    n1 = int((y_tr == 1).sum())
    spw = n0 / max(n1, 1)

    print(f"\n=== Final fit: train={len(X_tr)} test={len(X_te)} ===")
    final = XGBClassifier(
        n_estimators=400,
        max_depth=4,
        learning_rate=0.025,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        gamma=0.2,
        reg_alpha=0.1,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="logloss",
        scale_pos_weight=spw,
        random_state=42,
        n_jobs=-1,
    )
    final.fit(X_tr, y_tr)

    pred = final.predict(X_te)
    proba = final.predict_proba(X_te)
    acc = accuracy_score(y_te, pred)
    print(f"  Test accuracy: {acc * 100:.2f}%")
    print("  Classification report:")
    print(classification_report(y_te, pred, digits=3))
    print("  Confusion matrix:")
    print(confusion_matrix(y_te, pred))

    importance_df = (
        pd.DataFrame({"feature": FEATURES, "importance": final.feature_importances_})
        .sort_values("importance", ascending=False)
        .reset_index(drop=True)
    )
    print("\n=== Top 15 features ===")
    print(importance_df.head(15).to_string(index=False))

    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": final, "features": FEATURES}, MODEL_OUT)
    importance_df.to_csv(IMPORTANCE_OUT, index=False)

    meta = {
        "asset": "MNT/USD",
        "data_source": "CoinGecko market_chart days=90 (~hourly cadence)",
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "cv_mean_accuracy": float(np.mean(cv_scores)),
        "cv_std_accuracy": float(np.std(cv_scores)),
        "test_accuracy": float(acc),
        "n_features": len(FEATURES),
        "model_class": "XGBClassifier",
        "scale_pos_weight": float(spw),
    }
    META_OUT.write_text(json.dumps(meta, indent=2))
    print(f"\nSaved model -> {MODEL_OUT}")
    print(f"Saved importance -> {IMPORTANCE_OUT}")
    print(f"Saved metadata -> {META_OUT}")


if __name__ == "__main__":
    main()
