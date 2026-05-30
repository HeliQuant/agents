"""Collect ~1 year of REAL hourly OHLCV from Binance's public data mirror.

`data-api.binance.vision` is Binance's open market-data host — no auth, no
geo-block (the main api.binance.com is TLS/geo-blocked from our region; this
mirror is not). Klines return real OHLCV, up to 1000 bars/call, paginated via
startTime. We stack calls to assemble ~1 year of hourly candles.

This replaces the CoinGecko 90-day/close-only path with real high/low/volume +
4x more history, which is what walk-forward validation actually needs.

Usage:
    python scripts/14_collect_klines.py BTC
    python scripts/14_collect_klines.py ETH
    python scripts/14_collect_klines.py MNT
Output: data/{ticker}_hourly.csv (schema matches scripts/01)
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data-api.binance.vision/api/v3/klines"
LIMIT = 1000
HOURS_TARGET = 8760  # ~365 days


def fetch_year_hourly(symbol: str) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - HOURS_TARGET * 3600 * 1000
    rows: list[dict] = []
    cursor = start
    while cursor < end:
        r = requests.get(
            BASE,
            params={"symbol": symbol, "interval": "1h", "startTime": cursor, "limit": LIMIT},
            timeout=20,
            headers={"User-Agent": "heliquant/0.1"},
        )
        if r.status_code != 200:
            print(f"  status {r.status_code}: {r.text[:120]}")
            break
        batch = r.json()
        if not batch:
            break
        for k in batch:
            # kline: [openTime, open, high, low, close, volume, closeTime, quoteVol, ...]
            ot = int(k[0])
            rows.append({
                "timestamp": ot,
                "datetime": datetime.fromtimestamp(ot / 1000, tz=timezone.utc),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[7]),  # quote-asset (USDT) volume
            })
        cursor = int(batch[-1][0]) + 3600 * 1000
        if len(batch) < LIMIT:
            break
        time.sleep(0.3)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset="timestamp").sort_values("timestamp")
    return df.reset_index(drop=True)


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "BTC").upper()
    symbol = f"{ticker}USDT"
    print(f"Fetching ~1yr REAL hourly OHLC for {symbol} from Binance data mirror...")
    df = fetch_year_hourly(symbol)
    if df.empty:
        print(f"  No data for {symbol}.")
        return
    out = ROOT / "data" / f"{ticker.lower()}_hourly.csv"
    df.to_csv(out, index=False)
    span = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 1000 / 86400
    print(f"  {len(df)} bars, {span:.0f} days "
          f"({df['datetime'].iloc[0]} -> {df['datetime'].iloc[-1]})")
    print(f"  saved -> {out}")


if __name__ == "__main__":
    main()
