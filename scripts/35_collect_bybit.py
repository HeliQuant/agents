"""Collect REAL OHLCV (with VOLUME) from a public exchange market API — the trading venue.

The trading track connects to liquidity via the exchange API. The exchange's PUBLIC market endpoint
needs NO api key. This pulls hourly candles (open/high/low/close/VOLUME/turnover) for the liquid
universe and saves {ticker}_perp_hourly.csv — schema matches our pipeline so add_features / regime /
walk-forward run unchanged, now WITH real volume (fixes the Pyth no-volume gap).

Run: python scripts/35_collect_bybit.py            # default universe
     python scripts/35_collect_bybit.py BTC ETH    # subset
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.bitget.com/api/v2/mix/market/candles"
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "MNT": "MNTUSDT"}
GRANULARITY = "1H"     # 1 hour
PRODUCT_TYPE = "usdt-futures"
TARGET_DAYS = 365
LIMIT = 1000


def fetch(symbol: str) -> pd.DataFrame:
    now_ms = int(time.time() * 1000)
    start_target = now_ms - TARGET_DAYS * 86400 * 1000
    rows: dict[int, dict] = {}
    end = now_ms
    while end > start_target:
        try:
            r = requests.get(BASE, params={"symbol": symbol, "productType": PRODUCT_TYPE,
                                           "granularity": GRANULARITY, "limit": LIMIT, "endTime": end}, timeout=20)
            j = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"    error: {str(e)[:60]}")
            break
        if str(j.get("code")) not in ("0", "00000"):
            print(f"    code {j.get('code')}: {str(j.get('msg'))[:60]}")
            break
        lst = j.get("data", [])  # oldest-first list of [ts, open, high, low, close, baseVol, quoteVol]
        if not lst:
            break
        for k in lst:
            t = int(k[0])
            rows[t] = {"timestamp": t,
                       "datetime": datetime.fromtimestamp(t / 1000, tz=timezone.utc),
                       "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                       "close": float(k[4]), "volume": float(k[5])}
        oldest = min(int(k[0]) for k in lst)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(sorted(rows.values(), key=lambda r: r["timestamp"])).reset_index(drop=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [t.upper() for t in sys.argv[1:]] or list(SYMBOLS)
    print(f"Exchange public candles (1h, ~{TARGET_DAYS}d) — no API key needed\n")
    for t in tickers:
        sym = SYMBOLS.get(t, f"{t}USDT")
        print(f"[{t}] {sym} ...", end=" ", flush=True)
        df = fetch(sym)
        if df.empty:
            print("NO DATA")
            continue
        out = ROOT / "data" / f"{t.lower()}_perp_hourly.csv"
        df.to_csv(out, index=False)
        days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 1000 / 86400
        vol_ok = (df["volume"] > 0).mean() * 100
        print(f"{len(df)} bars, {days:.0f}d, {str(df['datetime'].iloc[0])[:10]}->{str(df['datetime'].iloc[-1])[:10]} "
              f"| volume>0: {vol_ok:.0f}% | last ${df['close'].iloc[-1]:,.4f}")
    print("\nsaved -> data/{ticker}_perp_hourly.csv (real OHLCV + volume)")


if __name__ == "__main__":
    main()
