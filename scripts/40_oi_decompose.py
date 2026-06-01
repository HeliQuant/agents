"""Is MNT oi_chg24 a REAL signal, or just short-bias during a crash? Decompose + drift-neutral test.

The OOS period had MNT -31% (a downtrend). A net-short strategy profits in a downtrend regardless
of signal quality. So we check:
  1. LONG-leg vs SHORT-leg separately: if shorts carry everything and longs LOSE -> it's short-bias.
  2. DRIFT-NEUTRAL spread: in the SAME OOS window, mean fwd return of bottom-OI-quintile (we go long)
     MINUS top-OI-quintile (we go short). Same period = same market drift -> the SPREAD removes drift.
     Positive spread = the signal genuinely discriminates up vs down. ~0 = no real timing edge.
  3. Beat the dumb benchmark? Strategy vs ALWAYS-SHORT over the same OOS (the real bar in a crash).

Run: python scripts/40_oi_decompose.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.00055
H = 24


def compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return (eq - 1) * 100


def analyze(ticker):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i])]
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    tr_sig = np.array([oichg[i] for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series([c[i + H] / c[i] - 1 for i in tr]), method="spearman")
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)

    long_net, short_net, top_raw, bot_raw = [], [], [], []
    for i in te:
        s, raw = oichg[i], c[i + H] / c[i] - 1
        if s >= p80:
            top_raw.append(raw)
            pos = -1 if contrarian else 1
        elif s <= p20:
            bot_raw.append(raw)
            pos = 1 if contrarian else -1
        else:
            continue
        net = pos * raw - 2 * FEE
        (long_net if pos > 0 else short_net).append(net)

    all_raw = [c[i + H] / c[i] - 1 for i in te]
    spread = (np.mean(bot_raw) - np.mean(top_raw)) * 100  # drift-neutral
    print(f"\n===== {ticker} (train IC {ic:+.3f}, {'contrarian' if contrarian else 'momentum'}) =====")
    print(f"  LONG  leg: {len(long_net):3} trades, win {np.mean([x>0 for x in long_net])*100 if long_net else 0:4.0f}%, "
          f"compounded {compound(long_net):+7.2f}%, avg {np.mean(long_net)*1e4 if long_net else 0:+6.1f} bps")
    print(f"  SHORT leg: {len(short_net):3} trades, win {np.mean([x>0 for x in short_net])*100 if short_net else 0:4.0f}%, "
          f"compounded {compound(short_net):+7.2f}%, avg {np.mean(short_net)*1e4 if short_net else 0:+6.1f} bps")
    print(f"  >> DRIFT-NEUTRAL spread (bottomQ long-windows minus topQ short-windows, raw): {spread:+.2f}%  "
          f"({'real timing signal' if spread > 0.5 else 'NO real edge (drift artifact)' if spread < 0.2 else 'marginal'})")
    print(f"  benchmarks over OOS: buy&hold {compound(all_raw):+.2f}% | "
          f"always-SHORT {compound([-r - 2*FEE for r in all_raw]):+.2f}% | "
          f"always-LONG {compound([r - 2*FEE for r in all_raw]):+.2f}%")
    strat = compound(long_net + short_net)
    always_short = compound([-r - 2 * FEE for r in all_raw])
    print(f"  strategy total {strat:+.2f}% vs always-short {always_short:+.2f}% -> "
          f"{'BEATS dumb short' if strat > always_short else 'LOSES to dumb short (no added value)'}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    for t in ["MNT", "BTC"]:
        analyze(t)


if __name__ == "__main__":
    main()
