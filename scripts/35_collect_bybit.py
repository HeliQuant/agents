"""Collect REAL OHLCV (with VOLUME) from Bybit v5 public API — the hackathon's trading venue.

The AI Trading track connects to liquidity via Bybit API. Bybit's PUBLIC market endpoint needs
NO api key. This pulls hourly klines (open/high/low/close/VOLUME/turnover) for the liquid universe
and saves {ticker}_bybit_hourly.csv — schema matches our pipeline so add_features / regime /
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
BASE = "https://api.bybit.com/v5/market/kline"
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "MNT": "MNTUSDT"}
INTERVAL = "60"        # 1 hour
CATEGORY = "spot"
TARGET_DAYS = 365
LIMIT = 1000


def fetch(symbol: str) -> pd.DataFrame:
    now_ms = int(time.time() * 1000)
    start_target = now_ms - TARGET_DAYS * 86400 * 1000
    rows: dict[int, dict] = {}
    end = now_ms
    while end > start_target:
        try:
            r = requests.get(BASE, params={"category": CATEGORY, "symbol": symbol,
                                           "interval": INTERVAL, "limit": LIMIT, "end": end}, timeout=20)
            j = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"    error: {str(e)[:60]}")
            break
        if j.get("retCode") != 0:
            print(f"    retCode {j.get('retCode')}: {str(j.get('retMsg'))[:60]}")
            break
        lst = j.get("result", {}).get("list", [])
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
    print(f"Bybit v5 public klines (spot, 1h, ~{TARGET_DAYS}d) — no API key needed\n")
    for t in tickers:
        sym = SYMBOLS.get(t, f"{t}USDT")
        print(f"[{t}] {sym} ...", end=" ", flush=True)
        df = fetch(sym)
        if df.empty:
            print("NO DATA")
            continue
        out = ROOT / "data" / f"{t.lower()}_bybit_hourly.csv"
        df.to_csv(out, index=False)
        days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 1000 / 86400
        vol_ok = (df["volume"] > 0).mean() * 100
        print(f"{len(df)} bars, {days:.0f}d, {str(df['datetime'].iloc[0])[:10]}->{str(df['datetime'].iloc[-1])[:10]} "
              f"| volume>0: {vol_ok:.0f}% | last ${df['close'].iloc[-1]:,.4f}")
    print("\nsaved -> data/{ticker}_bybit_hourly.csv (real OHLCV + volume)")


if __name__ == "__main__":
    main()
