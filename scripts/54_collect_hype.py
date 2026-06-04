"""Collect Bybit PUBLIC HYPE perp data (kline + open-interest + funding) -> positioning CSV.

HYPE is a promising new lead with NO local data. Bybit lists HYPEUSDT as a LinearPerpetual
(launched 2024-12-04) with ~500d of hourly OI retained on the public endpoint — enough for the
same cost-aware OI-contrarian validation we ran on MNT. PUBLIC market endpoints need NO api key.

This mirrors scripts/35 (klines) + scripts/37 (positioning):
  * kline (linear, 1h)        -> close bars (the price series the backtest trades)
  * open-interest (1h)        -> oi column (the contrarian signal source)
  * funding/history           -> funding column (for the IC/funding scans)
Aligned by merge_asof (backward) into data/hype_positioning.csv with the SAME schema as the other
positioning files (timestamp,datetime,close,funding,oi,buy_ratio) so scripts/38/39/41 run unchanged.
We ALSO write data/hype_bybit_hourly.csv (OHLCV) so the regime pipeline can use it later.

NOTE: long-short account-ratio (buy_ratio) retention on Bybit is short (~last few days) for newer
listings; we collect what is served and leave buy_ratio NaN where unavailable (honest — the IC test
already tolerates missing columns).

Run: python scripts/54_collect_hype.py
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
SYMBOL = "HYPEUSDT"
TICKER = "HYPE"


def fetch_kline(symbol: str, target_days: int = 540) -> pd.DataFrame:
    """Linear-perp 1h klines, paginated backward via `end` cursor (same as scripts/35)."""
    now_ms = int(time.time() * 1000)
    start_target = now_ms - target_days * 86400 * 1000
    rows: dict[int, dict] = {}
    end = now_ms
    while end > start_target:
        try:
            j = requests.get(
                BASE + "/v5/market/kline",
                params={"category": "linear", "symbol": symbol, "interval": "60", "limit": 1000, "end": end},
                timeout=20,
            ).json()
        except Exception as e:  # noqa: BLE001
            print(f"    kline error: {str(e)[:60]}")
            break
        if j.get("retCode") != 0:
            print(f"    kline retCode {j.get('retCode')}: {str(j.get('retMsg'))[:60]}")
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


def paginate(path: str, params: dict, ts_key: str, max_pages: int) -> dict[int, dict]:
    """Generic backward pagination via endTime cursor (same as scripts/37)."""
    rows: dict[int, dict] = {}
    cursor = None
    for _ in range(max_pages):
        p = dict(params)
        if cursor is not None:
            p["endTime"] = cursor
        try:
            j = requests.get(BASE + path, params=p, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"      fail {str(e)[:40]}")
            break
        if j.get("retCode") != 0:
            print(f"      retCode {j.get('retCode')} {str(j.get('retMsg'))[:40]}")
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


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print(f"Collecting Bybit PUBLIC {SYMBOL} (linear perp) — no API key\n")

    print("[1/4] kline (linear, 1h) ...", end=" ", flush=True)
    h = fetch_kline(SYMBOL)
    if h.empty:
        print("NO DATA — abort")
        return
    days = (h["timestamp"].iloc[-1] - h["timestamp"].iloc[0]) / 1000 / 86400
    print(f"{len(h)} bars, {days:.0f}d, {str(h['datetime'].iloc[0])[:10]}->{str(h['datetime'].iloc[-1])[:10]}")
    h.to_csv(ROOT / "data" / f"{TICKER.lower()}_bybit_hourly.csv", index=False)

    print("[2/4] open-interest (1h) ...", end=" ", flush=True)
    oi = paginate("/v5/market/open-interest",
                  {"category": "linear", "symbol": SYMBOL, "intervalTime": "1h", "limit": 200}, "timestamp", 70)
    print(f"{len(oi)} points")

    print("[3/4] funding/history ...", end=" ", flush=True)
    fund = paginate("/v5/market/funding/history",
                    {"category": "linear", "symbol": SYMBOL, "limit": 200}, "fundingRateTimestamp", 30)
    print(f"{len(fund)} points")

    print("[4/4] account-ratio (long-short) ...", end=" ", flush=True)
    lsr = paginate("/v5/market/account-ratio",
                   {"category": "linear", "symbol": SYMBOL, "period": "1h", "limit": 500}, "timestamp", 45)
    print(f"{len(lsr)} points")

    # Align everything to the kline close bars (backward merge_asof, same as scripts/37).
    merged = h[["timestamp", "datetime", "close"]].sort_values("timestamp").reset_index(drop=True)
    if fund:
        fdf = pd.DataFrame([{"timestamp": int(k), "funding": float(v["fundingRate"])} for k, v in fund.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, fdf, on="timestamp", direction="backward")
    if oi:
        odf = pd.DataFrame([{"timestamp": int(k), "oi": float(v["openInterest"])} for k, v in oi.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, odf, on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)
    if lsr:
        ldf = pd.DataFrame([{"timestamp": int(k), "buy_ratio": float(v["buyRatio"])} for k, v in lsr.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, ldf, on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)
    else:
        merged["buy_ratio"] = float("nan")  # keep schema identical to other positioning files

    out = ROOT / "data" / f"{TICKER.lower()}_positioning.csv"
    merged.to_csv(out, index=False)
    cov = {c: f"{merged[c].notna().mean() * 100:.0f}%" for c in ("funding", "oi", "buy_ratio") if c in merged}
    print(f"\n{TICKER} ({SYMBOL}): {len(merged)} bars | coverage {cov} -> {out.name}")
    oin = merged["oi"].notna()
    if oin.any():
        first = merged.loc[oin, "datetime"].iloc[0]
        last = merged.loc[oin, "datetime"].iloc[-1]
        print(f"   OI-covered span: {str(first)[:10]} -> {str(last)[:10]} ({oin.sum()} bars with OI)")


if __name__ == "__main__":
    main()
