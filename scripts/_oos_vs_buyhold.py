"""Honest check: did the strategy preserve capital vs buy-and-hold over the
SAME out-of-sample window used in walk-forward (last 40% of MNT features)?"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

STRAT_OOS_ROI = -10.32  # MNT, from data/mantle_walkforward.json (verified)

d = pd.read_csv(ROOT / "data" / "mnt_features.csv").dropna().reset_index(drop=True)
split = int(len(d) * 0.60)
test = d.iloc[split:].reset_index(drop=True)

c0 = float(test["close"].iloc[0])
c1 = float(test["close"].iloc[-1])
bh = (c1 / c0 - 1) * 100

eq = test["close"].values
peak = np.maximum.accumulate(eq)
bh_mdd = float(((peak - eq) / peak).max() * 100)

d0 = str(test["datetime"].iloc[0])[:10]
d1 = str(test["datetime"].iloc[-1])[:10]

print(f"MNT OOS window : {len(test)} bars (~{len(test)/24:.0f} days) | {d0} -> {d1}")
print(f"MNT price      : {c0:.4f} -> {c1:.4f}")
print(f"Buy-and-hold MNT over OOS : {bh:+.2f}%   (max drawdown {bh_mdd:.1f}%)")
print(f"HeliQuant strategy OOS    : {STRAT_OOS_ROI:+.2f}%")
verdict = "BEAT" if STRAT_OOS_ROI > bh else "LOST TO"
print(f"=> strategy {verdict} buy-and-hold by {abs(STRAT_OOS_ROI - bh):.2f}pp")
