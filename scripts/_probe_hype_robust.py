"""Honesty robustness check on the HYPE OI-contrarian non-validation.
Full-sample IC + a couple of split points — is the 'momentum, not contrarian' read stable
or just a thin-sample artifact? Read-only (no files written)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
H, FEE = 24, 0.00055

df = pd.read_csv(ROOT / "data" / "hype_hl_positioning.csv").sort_values("timestamp").reset_index(drop=True)
c = df["close"].values
oichg = df["oi"].pct_change(H).values
n = len(df)
idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i])]

sig = np.array([oichg[i] for i in idx])
ret = np.array([c[i + H] / c[i] - 1 for i in idx])
full_ic = pd.Series(sig).corr(pd.Series(ret), method="spearman")
print(f"FULL-sample (all {len(idx)} non-overlap 24h windows):")
print(f"  spearman IC(oi_chg24, fwd_ret) = {full_ic:+.3f}  -> "
      f"{'CONTRARIAN lean' if full_ic < 0 else 'MOMENTUM lean'}")
print(f"  oi_chg24 range: {np.nanmin(sig)*100:+.1f}% .. {np.nanmax(sig)*100:+.1f}% | "
      f"fwd 24h ret range: {ret.min()*100:+.1f}% .. {ret.max()*100:+.1f}%")

print("\nSplit sensitivity (train IC sign drives direction):")
for frac in (0.5, 0.6, 0.7):
    s = int(len(idx) * frac)
    tr = idx[:s]
    tr_sig = np.array([oichg[i] for i in tr])
    tr_ret = np.array([c[i + H] / c[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    print(f"  split {frac:.0%}: train n={len(tr)}  train_IC={ic:+.3f} -> "
          f"{'contrarian' if ic < 0 else 'momentum'}")

print("\nNote: HYPE rallied hard this window (buy&hold +18.9% over the OOS slice);")
print("a positive OI/return relationship in a strong uptrend = OI rising WITH price (momentum),")
print("the opposite of the mean-reverting OI-contrarian edge that validated on MNT.")
