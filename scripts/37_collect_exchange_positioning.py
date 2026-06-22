"""Collect exchange positioning signals (funding / open-interest) from the public perp venue
and align to our hourly close bars. Output: data/{ticker}_positioning.csv ready for IC backtest.

Signals only exist for PERPS. Our tradeable core MNT has a perp; we also pull BTC/ETH/SOL
to cross-check whether any relationship is asset-specific or general. mETH perp may not exist (skip
gracefully). Run: python scripts/37_collect_exchange_positioning.py

Honesty note: the public perp venue exposes a full HISTORY of funding rates, but only a CURRENT
snapshot of open interest (holdingAmount on the ticker). We therefore backfill funding over the
window and attach the live OI reading to the most recent bar only — we do NOT fabricate a historical
OI series. A retail long/short ratio feed is not available keyless here, so that column is omitted.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.bitget.com"
PRODUCT_TYPE = "usdt-futures"
ASSETS = {"MNT": "MNTUSDT", "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "METH": "METHUSDT"}


def funding_history(sym, max_pages=25):
    """Full funding-rate history (newest-first), paginated by incrementing pageNo -> {ts_ms: rate}."""
    rows: dict[int, float] = {}
    for pg in range(1, max_pages + 1):
        try:
            j = requests.get(f"{BASE}/api/v2/mix/market/history-fund-rate",
                             params={"symbol": sym, "productType": PRODUCT_TYPE,
                                     "pageSize": 100, "pageNo": pg}, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"      fail {str(e)[:40]}"); break
        if str(j.get("code")) not in ("0", "00000"):
            print(f"      code {j.get('code')} {str(j.get('msg'))[:40]}"); break
        lst = j.get("data", []) or []
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingTime"])] = float(x["fundingRate"])
        time.sleep(0.12)
    return rows


def current_oi(sym):
    """Current open interest (= holdingAmount) from the ticker, or None."""
    try:
        j = requests.get(f"{BASE}/api/v2/mix/market/ticker",
                         params={"symbol": sym, "productType": PRODUCT_TYPE}, timeout=20).json()
    except Exception:  # noqa: BLE001
        return None
    if str(j.get("code")) not in ("0", "00000"):
        return None
    data = j.get("data")
    row = data[0] if isinstance(data, list) and data else (data or {})
    try:
        return float(row.get("holdingAmount"))
    except (TypeError, ValueError):
        return None


def collect(ticker, sym):
    hp = ROOT / "data" / f"{ticker.lower()}_perp_hourly.csv"
    if not hp.exists():
        print(f"   {ticker}: no hourly file, skip"); return
    h = pd.read_csv(hp)[["timestamp", "datetime", "close"]].sort_values("timestamp").reset_index(drop=True)

    fund = funding_history(sym)
    oi_now = current_oi(sym)

    if not (fund or oi_now is not None):
        print(f"   {ticker} ({sym}): NO perp data (no perp listed?) — skip"); return

    merged = h.copy()
    if fund:
        fdf = pd.DataFrame([{"timestamp": int(k), "funding": float(v)} for k, v in fund.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, fdf, on="timestamp", direction="backward")  # ffill last funding
    if oi_now is not None and not merged.empty:
        # current snapshot only — attached to the latest bar (no fabricated history)
        merged["oi"] = pd.NA
        merged.loc[merged.index[-1], "oi"] = oi_now

    out = ROOT / "data" / f"{ticker.lower()}_positioning.csv"
    merged.to_csv(out, index=False)
    cov = {c: f"{merged[c].notna().mean()*100:.0f}%" for c in ("funding", "oi") if c in merged}
    print(f"   {ticker} ({sym}): {len(merged)} bars | coverage {cov} -> {out.name}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [t.upper() for t in sys.argv[1:]] or list(ASSETS)
    print("Collecting exchange positioning (funding/OI) -> aligned hourly:\n")
    for t in tickers:
        collect(t, ASSETS.get(t, f"{t}USDT"))


if __name__ == "__main__":
    main()
