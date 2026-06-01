"""Collect ~1 year of REAL hourly OHLC from Pyth Network (TradingView history shim).

This is the breakthrough source for MANTLE-ECOSYSTEM tokens (mETH, cmETH, MNT) that
are NOT on Binance/CryptoCompare. Pyth is a real oracle, live on Mantle on-chain, and
its benchmarks history endpoint returns OHLC bars. We paginate (720 bars/call) to
assemble ~1 year of hourly candles.

Pyth symbols are "Crypto.<BASE>/USD" (e.g. Crypto.METH/USD).

Usage:
    python scripts/17_collect_pyth.py METH
    python scripts/17_collect_pyth.py MNT
    python scripts/17_collect_pyth.py CMETH
Output: data/{ticker}_hourly.csv (schema matches scripts/01 so features/walk-forward
        run unchanged)
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://benchmarks.pyth.network/v1/shims/tradingview/history"
WINDOW = 720 * 3600     # 720 hourly bars per call
TARGET_DAYS = 365

# Pyth uses "Crypto.<BASE>/USD". Map our tickers to Pyth bases.
PYTH_SYMBOL = {
    "MNT": "Crypto.MNT/USD",
    "METH": "Crypto.METH/USD",
    "CMETH": "Crypto.CMETH/USD",
    "FBTC": "Crypto.FBTC/USD",
    "BTC": "Crypto.BTC/USD",
    "ETH": "Crypto.ETH/USD",
    "SOL": "Crypto.SOL/USD",
    "USDE": "Crypto.USDE/USD",
}


def fetch_history(symbol: str, target_days: int = TARGET_DAYS) -> pd.DataFrame:
    now = int(time.time())
    start_target = now - target_days * 86400
    rows: dict[int, dict] = {}
    to = now
    while to > start_target:
        frm = to - WINDOW
        try:
            r = requests.get(
                BASE,
                params={"symbol": symbol, "resolution": "60", "from": frm, "to": to},
                timeout=20,
                headers={"User-Agent": "heliquant/0.1"},
            )
            j = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"  error: {e}")
            break
        if j.get("s") != "ok" or not j.get("t"):
            break
        for t, o, h, l, c, v in zip(
            j["t"], j["o"], j["h"], j["l"], j["c"], j.get("v", [0] * len(j["t"]))
        ):
            rows[int(t)] = {
                "timestamp": int(t) * 1000,
                "datetime": datetime.fromtimestamp(int(t), tz=timezone.utc),
                "open": float(o), "high": float(h), "low": float(l),
                "close": float(c), "volume": float(v),
            }
        oldest = min(j["t"])
        if oldest >= to:  # no progress
            break
        to = oldest - 3600
        time.sleep(0.5)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(sorted(rows.values(), key=lambda r: r["timestamp"]))
    return df.reset_index(drop=True)


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "METH").upper()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else TARGET_DAYS
    symbol = PYTH_SYMBOL.get(ticker, f"Crypto.{ticker}/USD")
    print(f"Fetching up to {days}d REAL hourly OHLC for {ticker} ({symbol}) from Pyth...")
    df = fetch_history(symbol, days)
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
