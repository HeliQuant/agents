"""Generalized exchange PUBLIC perp collector — fetch ANY alt's positioning (candles + open-interest +
funding) into {ticker}_positioning.csv, ready for edge_lab onboarding. Generalizes scripts/54 (which
worked for HYPE). Mid-cap alts are LESS efficient than BTC -> the OI-contrarian edge may live there
(like MNT/HYPE). Public market endpoints need NO API key.

HONESTY NOTE: the public perp venue serves a full funding HISTORY but only a CURRENT open-interest
snapshot (no keyless OI time-series) and no retail long/short ratio — those columns are sparse/NaN by
design, never fabricated.

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
BASE = "https://api.bitget.com"
PRODUCT_TYPE = "usdt-futures"


def fetch_kline(symbol: str, target_days: int = 540) -> pd.DataFrame:
    now_ms = int(time.time() * 1000)
    start_target = now_ms - target_days * 86400 * 1000
    rows: dict[int, dict] = {}
    end = now_ms
    while end > start_target:
        try:
            j = requests.get(BASE + "/api/v2/mix/market/candles",
                             params={"symbol": symbol, "productType": PRODUCT_TYPE, "granularity": "1H",
                                     "limit": 1000, "endTime": end},
                             timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"    candle error: {str(e)[:60]}")
            break
        if str(j.get("code")) not in ("0", "00000"):
            print(f"    candle code {j.get('code')}: {str(j.get('msg'))[:50]}")
            break
        lst = j.get("data", [])  # oldest-first [ts, open, high, low, close, baseVol, quoteVol]
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


def funding_history(symbol: str, max_pages: int = 30) -> dict[int, float]:
    rows: dict[int, float] = {}
    for pg in range(1, max_pages + 1):
        try:
            j = requests.get(BASE + "/api/v2/mix/market/history-fund-rate",
                             params={"symbol": symbol, "productType": PRODUCT_TYPE,
                                     "pageSize": 100, "pageNo": pg}, timeout=20).json()
        except Exception:  # noqa: BLE001
            break
        if str(j.get("code")) not in ("0", "00000"):
            break
        lst = j.get("data", []) or []
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingTime"])] = float(x["fundingRate"])
        time.sleep(0.12)
    return rows


def current_oi(symbol: str):
    try:
        j = requests.get(BASE + "/api/v2/mix/market/ticker",
                         params={"symbol": symbol, "productType": PRODUCT_TYPE}, timeout=20).json()
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


def collect(ticker: str) -> None:
    symbol = f"{ticker.upper()}USDT"
    print(f"\n=== {symbol} ===")
    h = fetch_kline(symbol)
    if h.empty:
        print("  NO candle data — skip (not listed?)")
        return
    days = (h["timestamp"].iloc[-1] - h["timestamp"].iloc[0]) / 1000 / 86400
    print(f"  candles {len(h)} bars ({days:.0f}d)")
    h.to_csv(ROOT / "data" / f"{ticker.lower()}_perp_hourly.csv", index=False)
    fund = funding_history(symbol)
    oi_now = current_oi(symbol)
    print(f"  funding {len(fund)} · oi-snapshot {'ok' if oi_now is not None else 'n/a'}")
    merged = h[["timestamp", "datetime", "close"]].sort_values("timestamp").reset_index(drop=True)
    if fund:
        fdf = pd.DataFrame([{"timestamp": int(k), "funding": float(v)} for k, v in fund.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, fdf, on="timestamp", direction="backward")
    # current OI snapshot only — attached to the latest bar (no fabricated history)
    merged["oi"] = pd.NA
    if oi_now is not None and not merged.empty:
        merged.loc[merged.index[-1], "oi"] = oi_now
    merged["buy_ratio"] = float("nan")  # retail long/short ratio not served keyless
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
    print(f"Collecting exchange PUBLIC perp positioning for: {', '.join(tickers)}")
    for t in tickers:
        try:
            collect(t)
        except Exception as e:  # noqa: BLE001
            print(f"  {t} failed: {str(e)[:60]}")


if __name__ == "__main__":
    main()
