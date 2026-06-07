"""scripts/79 — RIGOROUS carry PnL: full per-trade model (funding + basis drift − 4-leg fees) + walk-forward.

scripts/76 measured carry as Σ funding only. This models the TRUE per-trade economics of a delta-neutral
hold: over each holding window you (a) collect funding, (b) gain/lose the BASIS DRIFT (basis_entry −
basis_exit — the residual the hedge doesn't cancel), and (c) pay 4 fee legs (open spot+perp, close
spot+perp). Then it WALK-FORWARDS the net across the year — is the carry consistently positive out of
sample, or lumpy? And it sweeps the HOLDING PERIOD to answer "how often can I rebalance before fees eat it?"

Honest verdict: net annualized carry must be > ~5% risk-free, with a MAJORITY of walk-forward buckets
positive, at a realistic holding period. Decomposes funding vs basis so we see if the yield is real
carry or basis luck.

Run:  python scripts/79_carry_rigorous.py            # HYPEUSDT SUIUSDT BTCUSDT
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import requests

BYBIT = "https://api.bybit.com"
FEE = 0.00055
LEGS = 4
RF = 5.0


def _kline(path, symbol, days=365, interval="60"):
    now = int(time.time() * 1000); start = now - days * 86400 * 1000
    rows = {}; end = now
    for _ in range(30):
        try:
            j = requests.get(BYBIT + path, params={"category": "linear", "symbol": symbol,
                                                   "interval": interval, "limit": 1000, "end": end}, timeout=20).json()
        except Exception:  # noqa: BLE001
            break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        for k in lst:
            rows[int(k[0])] = float(k[4])
        oldest = min(int(k[0]) for k in lst)
        if oldest <= start or oldest >= end:
            break
        end = oldest - 1; time.sleep(0.08)
    ts = np.array(sorted(rows)); return ts, np.array([rows[t] for t in ts])


def _funding(symbol, days=365):
    now = int(time.time() * 1000); start = now - days * 86400 * 1000
    rows = {}; cur = now
    for _ in range(30):
        try:
            j = requests.get(BYBIT + "/v5/market/funding/history",
                             params={"category": "linear", "symbol": symbol, "limit": 200, "endTime": cur}, timeout=20).json()
        except Exception:  # noqa: BLE001
            break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if oldest <= start or oldest >= cur:
            break
        cur = oldest - 1; time.sleep(0.08)
    return np.array(sorted(rows)), np.array([rows[t] for t in sorted(rows)])


def analyze(symbol):
    pt, pp = _kline("/v5/market/kline", symbol)
    it, ip = _kline("/v5/market/index-price-kline", symbol)
    ft, fr = _funding(symbol)
    if len(pt) < 500 or len(it) < 500 or len(fr) < 50:
        print(f"{symbol}: insufficient data"); return
    # align perp & index on common timestamps
    common = np.intersect1d(pt, it)
    pmap = dict(zip(pt, pp)); imap = dict(zip(it, ip))
    ts = common
    perp = np.array([pmap[t] for t in ts]); idx = np.array([imap[t] for t in ts])
    basis = (perp - idx) / idx  # fraction
    days_total = (ts[-1] - ts[0]) / 1000 / 86400
    print(f"\n=== {symbol} === ({len(ts)} hourly bars, {days_total:.0f}d)")
    print(f"{'hold':>6}{'net ann':>9}{'  = funding':>12}{'+ basis':>9}{'- fees':>8}{'%pos':>6}{'n':>4}  walk-forward buckets")
    for H in (7, 30, 90):
        step = H * 24
        nets, funds, bases = [], [], []
        for i in range(0, len(ts) - step, step):
            t0, t1 = ts[i], ts[i + step]
            fsum = fr[(ft >= t0) & (ft < t1)].sum()
            bpnl = basis[i] - basis[i + step]          # -(Δbasis): short perp gains if perp cheapens
            net = fsum + bpnl - LEGS * FEE
            nets.append(net); funds.append(fsum); bases.append(bpnl)
        nets, funds, bases = np.array(nets), np.array(funds), np.array(bases)
        if len(nets) < 4:
            continue
        ann = nets.mean() * 365 / H * 100
        f_ann = funds.mean() * 365 / H * 100
        b_ann = bases.mean() * 365 / H * 100
        fee_ann = LEGS * FEE * 365 / H * 100
        pos = 100 * (nets > 0).mean()
        # walk-forward: split holds into 4 time buckets, mean net annualized each
        buckets = np.array_split(nets, 4)
        wf = [f"{b.mean()*365/H*100:+.0f}" for b in buckets if len(b)]
        print(f"{H:>5}d{ann:>+8.1f}%{f_ann:>+11.1f}{b_ann:>+9.1f}{-fee_ann:>+8.1f}{pos:>5.0f}%{len(nets):>4}  [{' '.join(wf)}]%")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    syms = [s.upper() for s in sys.argv[1:]] or ["HYPEUSDT", "SUIUSDT", "BTCUSDT"]
    print("RIGOROUS carry — full PnL (funding + basis drift − 4-leg fees) + walk-forward + hold sweep")
    print(f"(net annualized must clear ~{RF:.0f}% risk-free with a majority of WF buckets +ve; longer hold = less fee drag)")
    for s in syms:
        analyze(s)
    print("\nRead: fee drag scales with rebalancing — short holds (7d) bleed ~11%/yr in fees, long holds")
    print("(90d) ~1%/yr. If net stays >risk-free at 90d AND walk-forward buckets are mostly +ve, the carry")
    print("is real + durable. Basis column shows how much is funding (durable) vs basis drift (luckier).")


if __name__ == "__main__":
    main()
