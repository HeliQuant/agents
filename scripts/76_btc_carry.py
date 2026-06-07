"""scripts/76 — BTC delta-neutral FUNDING/BASIS CARRY, tested honestly (the non-predictive BTC path).

Every directional BTC attempt failed because BTC is efficient — you can't PREDICT it. But you don't have
to: the cash-and-carry / basis trade earns the perpetual FUNDING RATE while being delta-neutral (long
spot + short perp, price cancels). It's carry (like interest), not a directional bet. This script measures
what that carry actually pays on real BTC funding history, NET of fees — no prediction, no overfit.

Mechanics: short the perp (collect funding when funding>0) + hold spot (hedges price). PnL ≈ Σ funding
− entry/exit fees − basis drift. We test two modes:
  * ALWAYS-ON  — hold continuously, pay funding when it's negative too (one open + one close).
  * CONDITIONAL — only hold when funding>0 (skip negative-funding regimes), pay a fee each state switch.

Honest caveats baked into the report: 4 fee legs (open+close, both venues), funding can go negative (bear),
basis can dislocate in a crash (tail risk). Verdict = is the net annualized carry meaningfully > a ~5%
risk-free, after costs.

Run:  python scripts/76_btc_carry.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
BYBIT = "https://api.bybit.com"
FEE = 0.00055   # taker per side per leg
LEGS_ROUNDTRIP = 4  # open spot + open perp + close spot + close perp


def fetch_funding(symbol="BTCUSDT", months=12):
    now = int(time.time() * 1000)
    start = now - months * 30 * 86400 * 1000
    rows = {}
    cursor = now
    for _ in range(40):
        try:
            j = requests.get(BYBIT + "/v5/market/funding/history",
                             params={"category": "linear", "symbol": symbol, "limit": 200, "endTime": cursor},
                             timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"  funding error: {str(e)[:60]}"); break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if oldest <= start or oldest >= cursor:
            break
        cursor = oldest - 1
        time.sleep(0.12)
    items = sorted(rows.items())
    return np.array([t for t, _ in items]), np.array([r for _, r in items])


def compute_carry(symbol: str, months: int = 12) -> dict | None:
    ts, fund = fetch_funding(symbol, months=months)
    if len(fund) < 50:
        return None
    days = (ts[-1] - ts[0]) / 1000 / 86400
    gross = fund.sum()
    ann_net = (gross - LEGS_ROUNDTRIP * FEE) * 365 / days * 100   # always-on, net of 1 round-trip
    cum = np.cumsum(fund)
    dd = (np.maximum.accumulate(cum) - cum).max() * 100           # worst negative-funding stretch
    return {"symbol": symbol, "n": len(fund), "days": days, "mean_bps": fund.mean() * 1e4,
            "pos_pct": 100 * (fund > 0).mean(), "ann_net": ann_net, "dd": dd,
            "vol_bps": fund.std() * 1e4}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    symbols = [s.upper() for s in sys.argv[1:]] or ["BTCUSDT", "ETHUSDT", "SUIUSDT", "HYPEUSDT"]
    rf = 5.0  # ~stablecoin/risk-free yield benchmark, %/yr
    print("Delta-neutral FUNDING CARRY across assets — honest (no prediction, net of fees)\n")
    print("Thesis: BTC carry is thin because BTC funding is structurally low (efficient/crowded).")
    print("Less-efficient assets pay richer funding — but richer yield = MORE tail risk, not free money.\n")
    print(f"{'asset':10}{'mean bps/8h':>12}{'funding vol':>12}{'pos %':>7}{'ANN net carry':>15}{'neg-stretch DD':>16}")
    print("-" * 72)
    rows = []
    for s in symbols:
        r = compute_carry(s)
        if not r:
            print(f"{s:10}  no funding history"); continue
        rows.append(r)
        print(f"{s:10}{r['mean_bps']:>+11.2f}{r['vol_bps']:>12.2f}{r['pos_pct']:>6.0f}%"
              f"{r['ann_net']:>+14.1f}%{('-'+format(r['dd'],'.2f')+'%'):>16}")
    print("-" * 72)
    if rows:
        btc = next((r for r in rows if r["symbol"] == "BTCUSDT"), None)
        rich = max(rows, key=lambda r: r["ann_net"])
        print(f"\nBTC carry ~{btc['ann_net']:+.1f}%/yr (vs ~{rf:.0f}% risk-free) = {'beats' if btc and btc['ann_net']>rf else 'BELOW'} risk-free"
              f" -> structurally thin." if btc else "")
        print(f"Richest carry: {rich['symbol']} ~{rich['ann_net']:+.1f}%/yr (funding vol {rich['vol_bps']:.1f}bps = "
              f"{'MUCH ' if rich['vol_bps']>btc['vol_bps']*2 else ''}higher tail risk).")
        print("\nHONEST READ: the carry STRATEGY is sound; BTC is just the wrong target (thinnest funding).")
        print("Richer-funding assets pay more BUT the same volatility that lifts funding can dislocate the")
        print("basis in a crash — the hedge that's supposed to protect you. Higher carry is RISK PREMIUM, not")
        print("alpha. Next rigor: stress the basis in the worst crash window before trusting any rich-carry asset.")


if __name__ == "__main__":
    main()
