"""Pull historical OHLCV-like data for MNT/USD from CoinGecko.

Exchange APIs (Binance, Bybit, OKX, KuCoin, MEXC) are TLS-blocked from the
deployer's region — CoinGecko free tier is the working source. CoinGecko's
market_chart endpoint with days=90 returns ~2160 hourly close+volume points,
which is enough training data. We synthesize an OHLC view by:
  - close = the per-hour close from CoinGecko
  - open  = previous-hour close (shift 1)
  - high  = rolling 3-bar max close
  - low   = rolling 3-bar min close
This is an honest approximation; features that strictly need intra-bar high/low
(ATR, Stoch %K, wick anatomy) are replaced with close-only equivalents downstream.

Usage:
    python scripts/01_collect_data.py
Output:
    data/mnt_hourly.csv
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/mantle/market_chart"
DAYS = 90
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "mnt_hourly.csv"


def fetch_market_chart() -> pd.DataFrame:
    resp = requests.get(
        COINGECKO_URL,
        params={"vs_currency": "usd", "days": str(DAYS)},
        timeout=20,
        headers={"User-Agent": "mantle-trading-firm/0.1"},
    )
    resp.raise_for_status()
    payload = resp.json()
    prices = payload["prices"]
    volumes = {ts: v for ts, v in payload["total_volumes"]}

    rows: list[dict[str, float | int]] = []
    for ts, close in prices:
        rows.append(
            {
                "timestamp": int(ts),
                "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                "close": float(close),
                "volume": float(volumes.get(ts, 0.0)),
            }
        )
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    # Synthesize OHLC. close is the truth; open is prev close; high/low are
    # 3-bar rolling extrema of close. This is an *honest approximation* for
    # a free data source — model features handle this assumption downstream.
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = df["close"].rolling(3, min_periods=1).max()
    df["low"] = df["close"].rolling(3, min_periods=1).min()

    return df


def main() -> None:
    print(f"Fetching {DAYS}d hourly MNT data from CoinGecko...")
    df = fetch_market_chart()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_PATH, index=False)
    print(f"Saved {len(df)} bars to {OUT_PATH}")
    print(
        f"Range: {df['datetime'].iloc[0]} -> {df['datetime'].iloc[-1]} "
        f"({(df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]) / 1000 / 3600:.0f}h span)"
    )
    print(df.head().to_string(index=False))


if __name__ == "__main__":
    main()
