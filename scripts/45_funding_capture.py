"""S2 — Delta-neutral FUNDING CAPTURE (market-neutral carry; complements the OI-contrarian hedge).

Mechanic: when perp funding f>0 (longs pay shorts) → SHORT perp + LONG spot (delta~0) → RECEIVE funding
each 8h. When f<0 → LONG perp + SHORT spot → receive again. Price PnL cancels (delta-neutral), so
strategy PnL = funding collected − trading fees. Fee = 2 legs × 0.055% taker = 0.11% per open or close;
a direction FLIP = close+open = 0.22%. Honest walk-forward on the entry hurdle (60/40), vs always-collect.

Funding sampled every 8h from {ticker}_positioning.csv. Annualize at 1095 (=3×365) periods/yr.
Run: python scripts/45_funding_capture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ["MNT", "BTC", "ETH", "SOL"]
FEE_PAIR = 0.00055 * 2     # open OR close the 2-leg delta-neutral position
PERIODS_YR = 3 * 365


def funding_8h(ticker):
    fp = ROOT / "data" / f"{ticker.lower()}_positioning.csv"
    if not fp.exists():
        return None
    df = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    f = df["funding"].dropna().values
    return f[::8]  # sample every 8h (funding settlement cadence)


def run(f8, hurdle):
    """Walk funding; hold the funding-RECEIVING side when |f|>hurdle. Returns (net%, gross%, fees%, flips, pct_in)."""
    d = 0
    gross = fees = 0.0
    flips = in_pos = 0
    for f in f8:
        target = (1 if f > 0 else -1) if abs(f) > hurdle else 0
        if target != d:
            if d != 0:
                fees += FEE_PAIR        # close existing pair
            if target != 0:
                fees += FEE_PAIR        # open new pair
            flips += 1
            d = target
        if d != 0:
            gross += abs(f)             # collect funding this 8h
            in_pos += 1
    net = gross - fees
    return net * 100, gross * 100, fees * 100, flips, in_pos / len(f8) * 100


def validate(ticker):
    f8 = funding_8h(ticker)
    if f8 is None or len(f8) < 100:
        return None
    split = int(len(f8) * 0.6)
    tr, te = f8[:split], f8[split:]
    grid = [0.0, 0.00005, 0.0001, 0.0002, 0.0005, 0.001]
    best_h, best_net = 0.0, -1e9
    for h in grid:
        net = run(tr, h)[0]
        if net > best_net:
            best_net, best_h = net, h
    oos = run(te, best_h)
    base = run(te, 0.0)  # always-collect baseline OOS
    yrs = len(te) / PERIODS_YR
    return {"hurdle": best_h, "oos_net": oos[0], "oos_ann": oos[0] / yrs,
            "gross": oos[1], "fees": oos[2], "flips": oos[3], "pct_in": oos[4],
            "base_net": base[0], "yrs": yrs}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(f"S2 funding capture — delta-neutral carry (fee {FEE_PAIR*100:.2f}%/side-pair, OOS 40%)\n")
    print(f"{'asset':6} {'hurdle/8h':>10} {'OOS net':>9} {'annualized':>11} {'gross':>8} {'fees':>8} {'flips':>6} {'%in':>6} {'always-collect':>14}")
    for t in ASSETS:
        r = validate(t)
        if not r:
            print(f"{t:6} insufficient"); continue
        print(f"{t:6} {r['hurdle']*100:>9.3f}% {r['oos_net']:+8.2f}% {r['oos_ann']:+10.2f}% "
              f"{r['gross']:>7.2f}% {r['fees']:>7.2f}% {r['flips']:>6} {r['pct_in']:>5.0f}% {r['base_net']:+13.2f}%")
    print("\nRead: OOS net>0 AND annualized meaningfully>0 = real market-neutral carry. If fees(flips) eat gross → funding too choppy to harvest.")
    print("Caveat: ignores basis-risk/slippage on the 2 legs + capital for both sides; this is the funding-only upper bound.")


if __name__ == "__main__":
    main()
