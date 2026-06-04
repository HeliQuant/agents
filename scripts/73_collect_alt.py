"""Generalized Bybit PUBLIC perp collector — fetch ANY alt's positioning (kline + open-interest +
funding + long-short ratio) into {ticker}_positioning.csv, ready for edge_lab onboarding. Generalizes
scripts/54 (which worked for HYPE). Mid-cap alts are LESS efficient than BTC -> the OI-contrarian edge
may live there (like MNT/HYPE). Public market endpoints need NO API key.

Run: python scripts/73_collect_alt.py SUI APT ARB SEI TIA
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.bybit.com"


def fetch_kline(symbol: str, target_days: int = 540) -> pd.DataFrame:
    now_ms = int(time.time() * 1000)
    start_target = now_ms - target_days * 86400 * 1000
    rows: dict[int, dict] = {}
    end = now_ms
    while end > start_target:
        try:
            j = requests.get(BASE + "/v5/market/kline",
                             params={"category": "linear", "symbol": symbol, "interval": "60", "limit": 1000, "end": end},
                             timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"    kline error: {str(e)[:60]}")
            break
        if j.get("retCode") != 0:
            print(f"    kline retCode {j.get('retCode')}: {str(j.get('retMsg'))[:50]}")
            break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        for k in lst:
            t = int(k[0])
            rows[t] = {"timestamp": t, "datetime": datetime.fromtimestamp(t / 1000, tz=timezone.utc),
                       "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                       "close": float(k[4]), "volume": float(k[5])}
        oldest = min(int(k[0]) for k in lst)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.15)
    return pd.DataFrame(sorted(rows.values(), key=lambda r: r["timestamp"])).reset_index(drop=True) if rows else pd.DataFrame()


def paginate(path: str, params: dict, ts_key: str, max_pages: int) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    cursor = None
    for _ in range(max_pages):
        p = dict(params)
        if cursor is not None:
            p["endTime"] = cursor
        try:
            j = requests.get(BASE + path, params=p, timeout=20).json()
        except Exception:  # noqa: BLE001
            break
        if j.get("retCode") != 0:
            break
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


def collect(ticker: str) -> None:
    symbol = f"{ticker.upper()}USDT"
    print(f"\n=== {symbol} ===")
    h = fetch_kline(symbol)
    if h.empty:
        print("  NO kline data — skip (not listed?)")
        return
    days = (h["timestamp"].iloc[-1] - h["timestamp"].iloc[0]) / 1000 / 86400
    print(f"  kline {len(h)} bars ({days:.0f}d)")
    h.to_csv(ROOT / "data" / f"{ticker.lower()}_bybit_hourly.csv", index=False)
    oi = paginate("/v5/market/open-interest", {"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 200}, "timestamp", 70)
    fund = paginate("/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": 200}, "fundingRateTimestamp", 30)
    lsr = paginate("/v5/market/account-ratio", {"category": "linear", "symbol": symbol, "period": "1h", "limit": 500}, "timestamp", 45)
    print(f"  oi {len(oi)} · funding {len(fund)} · lsr {len(lsr)}")
    merged = h[["timestamp", "datetime", "close"]].sort_values("timestamp").reset_index(drop=True)
    if fund:
        fdf = pd.DataFrame([{"timestamp": int(k), "funding": float(v["fundingRate"])} for k, v in fund.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, fdf, on="timestamp", direction="backward")
    if oi:
        odf = pd.DataFrame([{"timestamp": int(k), "oi": float(v["openInterest"])} for k, v in oi.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, odf, on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)
    else:
        merged["oi"] = float("nan")
    if lsr:
        ldf = pd.DataFrame([{"timestamp": int(k), "buy_ratio": float(v["buyRatio"])} for k, v in lsr.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, ldf, on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)
    else:
        merged["buy_ratio"] = float("nan")
    out = ROOT / "data" / f"{ticker.lower()}_positioning.csv"
    merged.to_csv(out, index=False)
    cov = {c: f"{merged[c].notna().mean() * 100:.0f}%" for c in ("funding", "oi", "buy_ratio") if c in merged}
    oin = int(merged["oi"].notna().sum()) if "oi" in merged else 0
    print(f"  -> {out.name} | {len(merged)} bars · coverage {cov} · OI-bars {oin}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [t.upper() for t in sys.argv[1:]] or ["SUI", "APT", "ARB", "SEI", "TIA"]
    print(f"Collecting Bybit PUBLIC perp positioning for: {', '.join(tickers)}")
    for t in tickers:
        try:
            collect(t)
        except Exception as e:  # noqa: BLE001
            print(f"  {t} failed: {str(e)[:60]}")


if __name__ == "__main__":
    main()
