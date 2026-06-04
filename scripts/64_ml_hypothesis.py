"""ML as a HYPOTHESIS — tested under the exact same honest gate as every other edge (no special pass).

Trains a model (RandomForest / XGBoost) on TRAIN features to predict the 24h-forward direction, then
trades its high-confidence calls OUT-OF-SAMPLE, net of fees, on NON-OVERLAPPING 24h windows. Reports
BOTH classification accuracy AND cost-aware OOS profit — because the project's hardest lesson is that
accuracy != money-weighted profit (Finding #5: regime classifier 82-88% OOS yet didn't profit).

Leak-free: time-ordered 60/40 split (no shuffle); model trained ONLY on train rows; quintile
thresholds derived ONLY from train predictions. An ML edge must clear the SAME bar — earn it or abstain.

Run: python scripts/64_ml_hypothesis.py [--asset MNT] [--model rf|xgb]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.00055
H = 24
# stationary, backward-looking features only (exclude raw price/volume LEVELS — non-stationary/leaky scale)
FEATURES = [
    "rsi", "atr", "adx", "momentum_3", "momentum_3_atr", "momentum_5", "momentum_5_atr",
    "momentum_10", "momentum_10_atr", "ema_slope_5", "ema_slope_10", "ema_gap_20_50", "ema_gap_50_100",
    "distance_ema20", "distance_ema50", "distance_ema100", "volatility_10", "volatility_20",
    "volatility_ratio", "return_1", "return_3", "return_5", "return_10", "volume_change", "volume_ratio",
    "ema_cross", "price_above_ema20", "price_above_ema50", "price_above_ema100",
    "rsi_above_50", "rsi_overbought", "rsi_oversold", "hour_utc",
]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="MNT")
    ap.add_argument("--model", default="rf", choices=["rf", "xgb"])
    args = ap.parse_args()

    fp = ROOT / "data" / f"{args.asset.lower()}_features.csv"
    if not fp.exists():
        print(f"no features file for {args.asset} ({fp.name}) — ML hypothesis needs engineered features.")
        return
    df = pd.read_csv(fp).reset_index(drop=True)
    feats = [f for f in FEATURES if f in df.columns]
    c = df["close"].values
    n = len(df)
    fwd = np.array([(c[i + H] / c[i] - 1) if i + H < n else np.nan for i in range(n)])
    y = (fwd > 0).astype(int)
    mask = ~np.isnan(fwd) & df[feats].notna().all(axis=1).values
    idx_all = np.where(mask)[0]
    split_row = idx_all[int(len(idx_all) * 0.6)]
    tr = idx_all[idx_all < split_row]
    te = idx_all[idx_all >= split_row]

    X = df[feats].values
    if args.model == "xgb":
        from xgboost import XGBClassifier
        clf = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.05, subsample=0.8,
                            eval_metric="logloss", n_jobs=2)
    else:
        from sklearn.ensemble import RandomForestClassifier
        clf = RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=50,
                                     n_jobs=2, random_state=0)
    clf.fit(X[tr], y[tr])
    proba = clf.predict_proba(X)[:, 1]  # P(up)
    acc = float(((proba[te] > 0.5).astype(int) == y[te]).mean())

    # trade high-confidence calls on NON-OVERLAP 24h test windows; thresholds from TRAIN proba
    p20, p80 = np.percentile(proba[tr], 20), np.percentile(proba[tr], 80)
    te_entries = [i for i in te if (i - te[0]) % H == 0]
    eq, trades, wins, rets = 1.0, 0, 0, []
    for i in te_entries:
        p = proba[i]
        if p >= p80:
            pos = 1            # model very bullish -> LONG
        elif p <= p20:
            pos = -1           # model very bearish -> SHORT
        else:
            continue
        net = pos * (c[i + H] / c[i] - 1) - 2 * FEE
        eq *= 1 + net; trades += 1; wins += int(net > 0); rets.append(net)
    bh = c[te_entries[-1] + H] / c[te_entries[0]] - 1 if te_entries else 0.0
    avg_bps = float(np.mean(rets) * 1e4) if rets else 0.0
    oos_roi = (eq - 1) * 100
    rt_fee = 2 * FEE * 1e4
    passed = (oos_roi > 0 and oos_roi > bh * 100 and avg_bps > rt_fee and trades >= 20)

    print(f"ML hypothesis ({args.model.upper()}) on {args.asset} — same cost-aware OOS gate as every edge\n")
    print(f"  features: {len(feats)}   train rows: {len(tr)}   test rows: {len(te)}")
    print(f"  CLASSIFICATION accuracy (OOS): {acc*100:.1f}%   (direction up/down)")
    print(f"  TRADING (high-confidence calls, non-overlap {H}h, net fee):")
    print(f"     OOS ROI {oos_roi:+.2f}%   buy&hold {bh*100:+.2f}%   trades {trades}   "
          f"win {wins/trades*100 if trades else 0:.0f}%   avg {avg_bps:+.1f}bps (fee {rt_fee:.0f})")
    print(f"\n  VERDICT: {'EARNED (clears the bar)' if passed else 'DOES NOT clear the bar -> abstain'}")
    if acc > 0.52 and not passed:
        print("  ^ classic accuracy != profit: the model predicts direction OK but it isn't money-weighted")
        print("    profit net of costs. Honest result — ML must EARN like everything else, not get a pass.")
    print("\n  (Leak-free: time-ordered 60/40, model trained on train only, thresholds from train.")
    print("   To register, an ML edge would also need walk-forward robustness — same as scripts/59/60.)")


if __name__ == "__main__":
    main()
