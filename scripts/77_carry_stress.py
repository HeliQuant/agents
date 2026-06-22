"""scripts/77 — CRASH STRESS-TEST for the delta-neutral funding carry (HYPE/SUI, BTC as control).

The carry (long spot + short perp, collect funding) is delta-neutral ONLY if perp tracks spot. Its real
killer is a BASIS DISLOCATION in a crash: when the perp gaps away from spot (index), the hedge breaks and
the position takes a mark-to-market hit — exactly when you need protection. Rich-funding alts (HYPE/SUI)
are the most suspect. This measures the basis = (perp_last - index)/index over a year and, critically,
WHAT THE BASIS DID during the worst price crashes + whether funding flipped against the carry then.

A carry is "safe enough" only if: (a) basis stays small + mean-reverting even in the worst crashes (no
big MTM gap), and (b) funding doesn't go deeply negative for long during selloffs. Honest, data-driven.

Run:  python scripts/77_carry_stress.py            # SUIUSDT HYPEUSDT BTCUSDT
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
EXCHANGE_API = "https://api.bitget.com"
PRODUCT_TYPE = "usdt-futures"
GRAN = "1H"  # interval=="60" -> hourly granularity
# map the two candle series we need to the exchange's v2 mix endpoints
_CANDLE_EP = {"market": "/api/v2/mix/market/candles", "index": "/api/v2/mix/market/history-index-candles"}


def _kline(kind: str, symbol: str, days: int = 365, interval: str = "60") -> pd.DataFrame:
    ep = _CANDLE_EP.get(kind, _CANDLE_EP["market"])
    now = int(time.time() * 1000); start = now - days * 86400 * 1000
    rows: dict[int, float] = {}; end = now
    for _ in range(30):
        try:
            j = requests.get(EXCHANGE_API + ep, params={"symbol": symbol, "productType": PRODUCT_TYPE,
                                                         "granularity": GRAN, "limit": 1000, "endTime": end}, timeout=20).json()
        except Exception:  # noqa: BLE001
            break
        lst = j.get("data", []) or []  # oldest-first [ts, o, h, l, c, ...]; close at index 4
        if not lst:
            break
        for k in lst:
            rows[int(k[0])] = float(k[4])
        oldest = min(int(k[0]) for k in lst)
        if oldest <= start or oldest >= end:
            break
        end = oldest - 1; time.sleep(0.1)
    s = pd.Series(rows).sort_index()
    return pd.DataFrame({"t": s.index, "close": s.values})


def _funding(symbol: str, days: int = 365):
    now = int(time.time() * 1000); start = now - days * 86400 * 1000
    rows = {}
    for pg in range(1, 31):  # funding history paginated by incrementing pageNo (newest-first)
        try:
            j = requests.get(EXCHANGE_API + "/api/v2/mix/market/history-fund-rate",
                             params={"symbol": symbol, "productType": PRODUCT_TYPE,
                                     "pageSize": 100, "pageNo": pg}, timeout=20).json()
        except Exception:  # noqa: BLE001
            break
        lst = j.get("data", []) or []
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingTime"])] = float(x["fundingRate"])
        oldest = min(int(x["fundingTime"]) for x in lst)
        if oldest <= start:
            break
        time.sleep(0.1)
    it = sorted((t, r) for t, r in rows.items() if t >= start)
    return np.array([t for t, _ in it]), np.array([r for _, r in it])


def stress(symbol: str):
    perp = _kline("market", symbol)
    idx = _kline("index", symbol)
    if perp.empty or idx.empty:
        print(f"{symbol:10} no perp/index data"); return
    df = pd.merge(perp, idx, on="t", suffixes=("_perp", "_idx")).sort_values("t").reset_index(drop=True)
    df["basis_bps"] = (df["close_perp"] - df["close_idx"]) / df["close_idx"] * 1e4
    df["ret24"] = df["close_perp"].pct_change(24)              # 24h forward-ish move
    ft, fr = _funding(symbol)
    # worst 24h price crashes
    worst = df.nsmallest(5, "ret24")
    # basis at those crash bars
    crash_basis = worst["basis_bps"].values
    # funding sign during the crash days (within +-12h of each crash)
    crash_fund = []
    for tt in worst["t"].values:
        m = (ft >= tt - 12 * 3600 * 1000) & (ft <= tt + 12 * 3600 * 1000)
        if m.any():
            crash_fund.append(fr[m].mean() * 1e4)
    b = df["basis_bps"]
    print(f"{symbol:10} basis(bps): median {b.median():+.1f} | p1 {b.quantile(.01):+.1f} | p99 {b.quantile(.99):+.1f} "
          f"| max|{b.abs().max():.0f}|  ||  worst 24h drop {worst['ret24'].min()*100:+.1f}%")
    print(f"{'':10}  -> basis AT the 5 worst crashes: {np.round(crash_basis,1).tolist()} bps "
          f"| funding then: {np.round(crash_fund,2).tolist()} bps/8h")
    # honest per-asset read
    big = b.abs().quantile(0.99)
    safe = big < 30 and (np.mean(crash_fund) > -1.0 if crash_fund else True)
    print(f"{'':10}  -> {'BASIS WELL-BEHAVED (carry survivable)' if safe else 'BASIS DISLOCATES / funding turns against you (carry RISKY in crashes)'}"
          f"  [99%ile |basis| {big:.0f}bps]\n")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    syms = [s.upper() for s in sys.argv[1:]] or ["SUIUSDT", "HYPEUSDT", "BTCUSDT"]
    print("CARRY CRASH STRESS-TEST — does the perp-vs-spot basis stay tame when price crashes?\n")
    print("(carry is delta-neutral only if basis stays small; a basis blowout = MTM hit on the hedge)\n")
    for s in syms:
        stress(s)
    print("Read: a carry is trustworthy only on assets whose basis stays small + mean-reverting THROUGH")
    print("crashes. A big p99 |basis| or funding flipping hard-negative in selloffs = the rich yield is")
    print("paying you for real crash risk. Size accordingly; never treat high-carry as risk-free.")


if __name__ == "__main__":
    main()
