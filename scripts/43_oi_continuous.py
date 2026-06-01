"""Continuous-sizing OI-contrarian vs binary-quintile — does smooth sizing beat the fragility?

The binary version (top/bottom quintile, fixed 24h non-overlap) is alignment-fragile (a 6-day grid
shift flipped a fold's sign). Continuous sizing should be inherently less fragile: it rebalances
every bar to a position proportional to the signal strength, using ALL the info, not just extremes.

  pos[t] = clip(dir * zscore(oi_chg24)[t] * SCALE, -1, +1)   (dir from TRAIN IC sign; z over trailing 168h)
  pnl[t->t+1] = pos[t]*return[t+1] - |pos[t]-pos[t-1]|*FEE    (turnover-based cost, realistic)

Pre-committed params (no fit): ZWIN=168h, SCALE=0.5 (|z|=2 -> full position). Expanding 4-fold WF,
direction from train only. Reports continuous vs binary per fold + turnover (cost realism).

Run: python scripts/43_oi_continuous.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.00055
H = 24
NF = 4
ZWIN = 168
SCALE = 0.5


def compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return (eq - 1) * 100


def prep(ticker):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    df["oichg"] = df["oi"].pct_change(H)
    m = df["oichg"].rolling(ZWIN).mean()
    s = df["oichg"].rolling(ZWIN).std()
    df["z"] = (df["oichg"] - m) / (s + 1e-12)
    return df


def run(ticker):
    df = prep(ticker)
    c = df["close"].values
    oichg = df["oichg"].values
    z = df["z"].values
    dt = df["datetime"].astype(str).values
    n = len(df)
    first = ZWIN + H + 1
    bars = [i for i in range(first, n - 1) if not np.isnan(z[i]) and not np.isnan(oichg[i])]
    N = len(bars)
    init = int(N * 0.4)
    fold = (N - init) // NF

    print(f"\n================= {ticker} =================")
    print(f"{'fold period':25} {'dir':>11} {'CONT roi':>9} {'turn/yr':>8} | {'BIN roi':>9}")
    cont_rois, bin_rois = [], []
    for f in range(NF):
        ts = init + f * fold
        teEnd = (init + (f + 1) * fold) if f < NF - 1 else N
        train_bars, test_bars = bars[:ts], bars[ts:teEnd]
        if len(test_bars) < 24:
            continue
        tr_sig = np.array([oichg[i] for i in train_bars])
        tr_ret = np.array([c[i + H] / c[i] - 1 if i + H < n else 0.0 for i in train_bars])
        ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
        contrarian = ic < 0
        dsign = -1.0 if contrarian else 1.0
        p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)

        # CONTINUOUS — hourly rebalance, turnover fees
        eq, prev, turn, rets = 1.0, 0.0, 0.0, []
        for i in test_bars:
            if i + 1 >= n:
                break
            pos = float(np.clip(dsign * z[i] * SCALE, -1.0, 1.0))
            fee = abs(pos - prev) * FEE
            pnl = pos * (c[i + 1] / c[i] - 1) - fee
            eq *= (1 + pnl); rets.append(pnl); turn += abs(pos - prev); prev = pos
        cont_roi = (eq - 1) * 100
        turn_yr = (turn / len(test_bars)) * 24 * 365 if test_bars else 0

        # BINARY — non-overlap 24h quintile (the prior method, same fold range)
        bnet, j = [], test_bars[0]
        last = test_bars[-1]
        while j <= last:
            if j + H >= n:
                break
            s = oichg[j]
            if s >= p80:
                pos = -1 if contrarian else 1
            elif s <= p20:
                pos = 1 if contrarian else -1
            else:
                pos = 0
            if pos != 0:
                bnet.append(pos * (c[j + H] / c[j] - 1) - 2 * FEE)
            j += H
        bin_roi = compound(bnet)

        cont_rois.append(cont_roi); bin_rois.append(bin_roi)
        period = f"{dt[test_bars[0]][:10]}..{dt[test_bars[-1]][:10]}"
        print(f"{period:25} {('contra' if contrarian else 'momentum'):>11} {cont_roi:+8.2f}% {turn_yr:7.0f}x | {bin_roi:+8.2f}%")

    print(f"  CONTINUOUS: total {compound([r/100 for r in cont_rois]):+.2f}% | {sum(1 for r in cont_rois if r>0)}/{len(cont_rois)} folds+")
    print(f"  BINARY    : total {compound([r/100 for r in bin_rois]):+.2f}% | {sum(1 for r in bin_rois if r>0)}/{len(bin_rois)} folds+")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(f"Continuous (smooth) vs Binary (quintile) OI-contrarian. ZWIN={ZWIN}h SCALE={SCALE} fee {FEE*100:.3f}%/side")
    for t in ["MNT", "BTC"]:
        run(t)
    print("\nRead: if CONTINUOUS gives more folds+ / less wild swings than BINARY at sane turnover -> smoother & more robust.")
    print("If turnover/yr is huge or continuous underperforms -> signal lives only in the extremes (binary), confirmed fragile.")


if __name__ == "__main__":
    main()
