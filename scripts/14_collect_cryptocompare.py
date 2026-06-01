"""Collect ~1 year of REAL hourly OHLCV from CryptoCompare (paginated histohour).

CoinGecko free tier caps hourly data at ~90 days and returns close-only (we had to
synthesize high/low). CryptoCompare histohour paginates via toTs — stack 2000-bar
calls to pull ~1 year of REAL OHLC with true high/low/volume. Better data ->
accurate ATR/Stoch/wick features -> meaningful walk-forward out-of-sample window.

Usage:
    python scripts/14_collect_cryptocompare.py BTC
    python scripts/14_collect_cryptocompare.py BTC --tsym USD   # for thin tokens
Output: data/{ticker}_hourly.csv  (schema matches scripts/01 so the rest of the
        pipeline — features/train/replay/walkforward — works unchanged)
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://min-api.cryptocompare.com/data/v2/histohour"
CALLS = 5       # 5 x 2000 = ~10000 hours ~= 416 days
LIMIT = 2000


def fetch_year_hourly(fsym: str, tsym: str) -> pd.DataFrame:
    all_bars: list[dict] = []
    to_ts = int(time.time())
    for call in range(CALLS):
        r = requests.get(
            BASE,
            params={"fsym": fsym, "tsym": tsym, "limit": LIMIT, "toTs": to_ts},
            timeout=20,
            headers={"User-Agent": "heliquant/0.1"},
        )
        j = r.json()
        if j.get("Response") != "Success":
            print(f"  call {call}: {j.get('Message')}")
            break
        data = j["Data"]["Data"]
        if not data:
            break
        all_bars = data + all_bars
        to_ts = data[0]["time"] - 3600
        time.sleep(1.2)

    seen, rows = set(), []
    for b in all_bars:
        t = b["time"]
        if t in seen or b.get("close", 0) <= 0:
            continue
        seen.add(t)
        rows.append({
            "timestamp": int(t) * 1000,
            "datetime": datetime.fromtimestamp(t, tz=timezone.utc),
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
            "volume": float(b.get("volumeto", 0.0)),  # quote-ccy (USD) volume
        })
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ticker = (args[0] if args else "BTC").upper()
    tsym = "USDT"
    if "--tsym" in sys.argv:
        tsym = sys.argv[sys.argv.index("--tsym") + 1].upper()

    print(f"Fetching ~1yr REAL hourly OHLC for {ticker}/{tsym} from CryptoCompare...")
    df = fetch_year_hourly(ticker, tsym)
    if df.empty or df["high"].max() <= 0:
        print(f"  No usable data for {ticker}/{tsym}.")
        return
    out = ROOT / "data" / f"{ticker.lower()}_hourly.csv"
    df.to_csv(out, index=False)
    span = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 1000 / 86400
    print(f"  {len(df)} bars, {span:.0f} days "
          f"({df['datetime'].iloc[0]} -> {df['datetime'].iloc[-1]})")
    print(f"  saved -> {out}")


if __name__ == "__main__":
    main()
