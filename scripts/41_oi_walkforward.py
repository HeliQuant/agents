"""DECISIVE robustness test: does MNT oi_chg24 contrarian hold across MULTIPLE OOS windows?
(A single OOS window is what fooled us before with +5.58%. Real edge survives rolling walk-forward.)

Expanding-train walk-forward: initial 40% train, then 4 sequential OOS folds (train grows each fold).
Per fold derive direction+quintile thresholds from TRAIN ONLY, test OOS. A robust edge = most folds
positive net ROI AND positive drift-neutral spread, across different market periods.

Run: python scripts/41_oi_walkforward.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.00055
H = 24
N_FOLDS = 4


def compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return (eq - 1) * 100


def walkforward(ticker):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    dt = df["datetime"].astype(str).values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i])]
    N = len(idx)
    init = int(N * 0.4)
    fold_size = (N - init) // N_FOLDS

    print(f"\n===== {ticker}: expanding walk-forward, {N} windows, {N_FOLDS} OOS folds =====")
    print(f"{'fold period':25} {'tr_IC':>7} {'dir':>11} {'trades':>7} {'OOS ROI':>9} {'spread':>8} {'buy&hold':>9}")
    pos_roi = pos_spread = 0
    for f in range(N_FOLDS):
        ts = init + f * fold_size
        teEnd = (init + (f + 1) * fold_size) if f < N_FOLDS - 1 else N
        train, test = idx[:ts], idx[ts:teEnd]
        if len(test) < 8:
            continue
        tr_sig = np.array([oichg[i] for i in train])
        ic = pd.Series(tr_sig).corr(pd.Series([c[i + H] / c[i] - 1 for i in train]), method="spearman")
        contrarian = ic < 0
        p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
        net, top_raw, bot_raw = [], [], []
        for i in test:
            s, raw = oichg[i], c[i + H] / c[i] - 1
            if s >= p80:
                top_raw.append(raw); pos = -1 if contrarian else 1
            elif s <= p20:
                bot_raw.append(raw); pos = 1 if contrarian else -1
            else:
                continue
            net.append(pos * raw - 2 * FEE)
        roi = compound(net)
        spread = ((np.mean(bot_raw) if bot_raw else 0) - (np.mean(top_raw) if top_raw else 0)) * 100
        bh = (c[test[-1] + H] / c[test[0]] - 1) * 100
        pos_roi += int(roi > 0); pos_spread += int(spread > 0)
        period = f"{dt[test[0]][:10]}..{dt[test[-1]][:10]}"
        print(f"{period:25} {ic:+7.3f} {('contrarian' if contrarian else 'momentum'):>11} "
              f"{len(net):7} {roi:+8.2f}% {spread:+7.2f}% {bh:+8.2f}%")
    print(f"  -> {pos_roi}/{N_FOLDS} folds positive ROI | {pos_spread}/{N_FOLDS} folds positive drift-neutral spread")
    return pos_roi, pos_spread


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    summary = {}
    for t in ["MNT", "BTC", "ETH", "SOL"]:
        summary[t] = walkforward(t)
    print("\n===== HONEST VERDICT =====")
    for t, (pr, ps) in summary.items():
        if pr >= 3 and ps >= 3:
            print(f"  {t}: ROBUST across folds ({pr}/{N_FOLDS} ROI, {ps}/{N_FOLDS} spread) — genuine edge candidate")
        elif pr >= 3:
            print(f"  {t}: ROI ok but spread inconsistent — partial; verify before trusting")
        else:
            print(f"  {t}: NOT robust ({pr}/{N_FOLDS} folds) — single-window artifact, do not claim edge")


if __name__ == "__main__":
    main()
