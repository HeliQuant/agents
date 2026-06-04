"""Stress-test the HYPE buy_ratio (long-short account ratio) contrarian signal.

The single-split cost-aware backtest gave HYPE buy_ratio-contrarian OOS +92% (avg +72 bps, n=111),
beating buy&hold +51%. That is exactly the kind of eye-popping single number this project REJECTS
until it survives rigor (a prior +96% was a thin-liquidity artifact). So we apply the same bar MNT got:
  1. Expanding walk-forward (script-41 protocol): robust = most folds +ROI AND +drift-neutral spread,
     AND a STABLE direction sign across folds (sign flips = not a real signal).
  2. Alignment sensitivity: shift the 24h entry grid by a few hours; a real edge is stable, a binary
     artifact swings wildly (this is the test that exposed binary OI fragility before).

Run: python scripts/56_hype_buyratio_rigor.py
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
TICKER = "HYPE"


def compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return (eq - 1) * 100


def _load():
    df = pd.read_csv(ROOT / "data" / f"{TICKER.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    return df["close"].values, df["buy_ratio"].values, df["datetime"].astype(str).values


def walkforward():
    c, br, dt = _load()
    n = len(c)
    idx = [i for i in range(H, n - H, H) if not np.isnan(br[i])]
    N = len(idx)
    init = int(N * 0.4)
    fold = (N - init) // NF
    print("===== HYPE buy_ratio contrarian: expanding walk-forward (script-41 protocol) =====")
    print(f"{'fold period':25} {'tr_IC':>7} {'dir':>7} {'trd':>4} {'OOS ROI':>9} {'spread':>8} {'buy&hold':>9}")
    pos_roi = pos_spread = 0
    dirs = []
    for f in range(NF):
        ts = init + f * fold
        teEnd = (init + (f + 1) * fold) if f < NF - 1 else N
        train, test = idx[:ts], idx[ts:teEnd]
        if len(test) < 8:
            continue
        tr_sig = np.array([br[i] for i in train])
        ic = pd.Series(tr_sig).corr(pd.Series([c[i + H] / c[i] - 1 for i in train]), method="spearman")
        contrarian = ic < 0
        dirs.append("contra" if contrarian else "moment")
        p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
        net, top_raw, bot_raw = [], [], []
        for i in test:
            s, raw = br[i], c[i + H] / c[i] - 1
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
        print(f"{dt[test[0]][:10]}..{dt[test[-1]][:10]}{'':2} {ic:+7.3f} {dirs[-1]:>7} {len(net):4} "
              f"{roi:+8.2f}% {spread:+7.2f}% {bh:+8.2f}%")
    stable_dir = len(set(dirs)) == 1
    print(f"  -> {pos_roi}/{NF} folds +ROI | {pos_spread}/{NF} folds +spread | direction stable across folds: {stable_dir} ({dirs})")
    return pos_roi, pos_spread, stable_dir


def split_backtest(offset):
    c, br, _ = _load()
    n = len(c)
    idx = [i for i in range(H + offset, n - H, H) if not np.isnan(br[i])]
    if len(idx) < 60:
        return None
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    tr_sig = np.array([br[i] for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series([c[i + H] / c[i] - 1 for i in tr]), method="spearman")
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
    rets = []
    for i in te:
        s, raw = br[i], c[i + H] / c[i] - 1
        if s >= p80:
            pos = -1 if contrarian else 1
        elif s <= p20:
            pos = 1 if contrarian else -1
        else:
            continue
        rets.append(pos * raw - 2 * FEE)
    return compound(rets), len(rets), (np.mean(rets) * 1e4 if rets else 0), ("contra" if contrarian else "moment")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    pr, ps, stable = walkforward()
    print("\n===== Alignment sensitivity (single-split 60/40 OOS at different entry-hour offsets) =====")
    rois = []
    for off in [0, 3, 6, 9, 12, 18]:
        r = split_backtest(off)
        if r:
            rois.append(r[0])
            print(f"  offset {off:2}h: OOS {r[0]:+8.2f}%  ({r[1]} trades, avg {r[2]:+6.1f} bps, dir {r[3]})")
    if rois:
        print(f"  spread of OOS across offsets: {min(rois):+.1f}% .. {max(rois):+.1f}%  (range {max(rois)-min(rois):.1f} pp)")
    print("\n===== HONEST VERDICT (HYPE buy_ratio) =====")
    robust = pr >= 3 and ps >= 3 and stable
    if robust:
        print("  ROBUST candidate — survived folds + stable direction. Worth registering as a SEPARATE edge.")
    else:
        print(f"  NOT robust ({pr}/4 ROI, {ps}/4 spread, dir-stable={stable}). The +92% single-split is")
        print("  an alignment/window artifact, NOT a tradeable edge. Do NOT register. ABSTAIN.")


if __name__ == "__main__":
    main()
