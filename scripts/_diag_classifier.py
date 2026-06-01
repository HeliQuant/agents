"""Honest diagnostic: how good is the regime classifier REALLY, vs naive baselines?

Tests the hypothesis "XGBoost accuracy is bad". Trains on first 60% (via multi_asset.
train_classifier), evaluates forward-regime prediction on the unseen last 40%, and
compares against:
  - majority baseline  (always predict the most common regime)
  - persistence baseline (predict 'forward regime = current regime')
Also shows per-class recall — the Trending classes are what actually trigger trades.

Run: python scripts/_diag_classifier.py MNT
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

ticker = (sys.argv[1] if len(sys.argv) > 1 else "MNT").upper()
df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
split = int(len(df) * 0.60)
train_df, test_df = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)

bundle = ma.train_classifier(train_df)
adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]

test_df = test_df.copy()
test_df["regime_now"] = ma.label_regimes(test_df, adx_th, vol_th)
test_df["regime_future"] = test_df["regime_now"].shift(-ma.FORECAST_HORIZON)
test_df = test_df.dropna(subset=["regime_future"]).reset_index(drop=True)

y_true = test_df["regime_future"].map(ma.LABEL_TO_INT).astype(int)
y_pred = bundle["model"].predict(test_df[ma.FEATURES])

acc = accuracy_score(y_true, y_pred)
majority = test_df["regime_future"].value_counts(normalize=True).max()
persistence = (test_df["regime_now"] == test_df["regime_future"]).mean()

print(f"=== {ticker} regime classifier — honest OOS diagnostic ===")
print(f"  test bars (OOS): {len(test_df)}")
print(f"  XGBoost OOS accuracy : {acc*100:5.1f}%")
print(f"  majority baseline    : {majority*100:5.1f}%   (always predict most-common regime)")
print(f"  persistence baseline : {persistence*100:5.1f}%   (predict 'future = now')")
print(f"  --> XGBoost beats persistence by {(acc-persistence)*100:+.1f} pts\n")

print("  class distribution (forward regime, OOS):")
dist = test_df["regime_future"].value_counts(normalize=True)
for name in ma.REGIME_LABELS:
    print(f"    {name:16} {dist.get(name, 0.0)*100:5.1f}%")

print("\n  per-class report (recall = how often it catches that regime):")
present = sorted(set(y_true) | set(y_pred))
print(classification_report(
    y_true, y_pred, labels=present,
    target_names=[ma.REGIME_LABELS[i] for i in present], digits=2, zero_division=0))

print("  confusion matrix (rows=true, cols=pred):", [ma.REGIME_LABELS[i] for i in present])
print(confusion_matrix(y_true, y_pred, labels=present))
