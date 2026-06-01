"""Collect FULL-history Bybit positioning signals (funding / open-interest / long-short ratio)
and align to our hourly close bars. Output: data/{ticker}_positioning.csv ready for IC backtest.

Signals only exist for PERPS (linear). Our tradeable core MNT has a perp; we also pull BTC/ETH/SOL
to cross-check whether any relationship is asset-specific or general. mETH perp may not exist (skip
gracefully). Run: python scripts/37_collect_bybit_positioning.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.bybit.com"
ASSETS = {"MNT": "MNTUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "METH": "METHUSDT"}


def paginate(path, params, ts_key, max_pages):
    rows: dict[int, dict] = {}
    cursor = None
    for _ in range(max_pages):
        p = dict(params)
        if cursor is not None:
            p["endTime"] = cursor
        try:
            j = requests.get(BASE + path, params=p, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"      fail {str(e)[:40]}"); break
        if j.get("retCode") != 0:
            print(f"      retCode {j.get('retCode')} {str(j.get('retMsg'))[:40]}"); break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        for x in lst:
            rows[int(x[ts_key])] = x
        oldest = min(int(x[ts_key]) for x in lst)
        if cursor is not None and oldest >= cursor:
            break
        cursor = oldest - 1
        time.sleep(0.12)
    return rows


def collect(ticker, sym):
    hp = ROOT / "data" / f"{ticker.lower()}_bybit_hourly.csv"
    if not hp.exists():
        print(f"   {ticker}: no hourly file, skip"); return
    h = pd.read_csv(hp)[["timestamp", "datetime", "close"]].sort_values("timestamp").reset_index(drop=True)

    fund = paginate("/v5/market/funding/history", {"category": "linear", "symbol": sym, "limit": 200}, "fundingRateTimestamp", 25)
    oi = paginate("/v5/market/open-interest", {"category": "linear", "symbol": sym, "intervalTime": "1h", "limit": 200}, "timestamp", 45)
    lsr = paginate("/v5/market/account-ratio", {"category": "linear", "symbol": sym, "period": "1h", "limit": 500}, "timestamp", 45)

    if not (fund or oi or lsr):
        print(f"   {ticker} ({sym}): NO perp data (no perp listed?) — skip"); return

    merged = h.copy()
    if fund:
        fdf = pd.DataFrame([{"timestamp": int(k), "funding": float(v["fundingRate"])} for k, v in fund.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, fdf, on="timestamp", direction="backward")  # ffill last funding
    if oi:
        odf = pd.DataFrame([{"timestamp": int(k), "oi": float(v["openInterest"])} for k, v in oi.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, odf, on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)
    if lsr:
        ldf = pd.DataFrame([{"timestamp": int(k), "buy_ratio": float(v["buyRatio"])} for k, v in lsr.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, ldf, on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)

    out = ROOT / "data" / f"{ticker.lower()}_positioning.csv"
    merged.to_csv(out, index=False)
    cov = {c: f"{merged[c].notna().mean()*100:.0f}%" for c in ("funding", "oi", "buy_ratio") if c in merged}
    print(f"   {ticker} ({sym}): {len(merged)} bars | coverage {cov} -> {out.name}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [t.upper() for t in sys.argv[1:]] or list(ASSETS)
    print("Collecting Bybit positioning (funding/OI/long-short) -> aligned hourly:\n")
    for t in tickers:
        collect(t, ASSETS.get(t, f"{t}USDT"))


if __name__ == "__main__":
    main()
