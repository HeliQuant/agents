"""Collect exchange PUBLIC HYPE perp data (candles + open-interest + funding) -> positioning CSV.

HYPE is a promising new lead with NO local data. The exchange lists HYPEUSDT as a perp with a long
hourly history on the public endpoint — enough for the same cost-aware OI-contrarian validation we
ran on MNT. PUBLIC market endpoints need NO api key.

This mirrors scripts/35 (candles) + scripts/37 (positioning):
  * candles (1h)              -> close bars (the price series the backtest trades)
  * funding/history           -> funding column (for the IC/funding scans)
  * open-interest (current)   -> oi column (the contrarian signal source)
Aligned by merge_asof (backward) into data/hype_positioning.csv with the SAME schema as the other
positioning files (timestamp,datetime,close,funding,oi,buy_ratio) so scripts/38/39/41 run unchanged.
We ALSO write data/hype_perp_hourly.csv (OHLCV) so the regime pipeline can use it later.

HONESTY NOTE: the public perp venue serves a full funding HISTORY but only a CURRENT snapshot of open
interest (no keyless OI time-series) and no retail long/short ratio. We attach the live OI reading to
the most recent bar and leave buy_ratio NaN — we collect what is served and never fabricate the rest
(the IC test already tolerates missing columns).

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
BASE = "https://api.bitget.com"
PRODUCT_TYPE = "usdt-futures"
SYMBOL = "HYPEUSDT"
TICKER = "HYPE"


def fetch_kline(symbol: str, target_days: int = 540) -> pd.DataFrame:
    """Perp 1h candles, paginated backward via `endTime` cursor (same idea as scripts/35)."""
    now_ms = int(time.time() * 1000)
    start_target = now_ms - target_days * 86400 * 1000
    rows: dict[int, dict] = {}
    end = now_ms
    while end > start_target:
        try:
            j = requests.get(
                BASE + "/api/v2/mix/market/candles",
                params={"symbol": symbol, "productType": PRODUCT_TYPE, "granularity": "1H",
                        "limit": 1000, "endTime": end},
                timeout=20,
            ).json()
        except Exception as e:  # noqa: BLE001
            print(f"    candle error: {str(e)[:60]}")
            break
        if str(j.get("code")) not in ("0", "00000"):
            print(f"    candle code {j.get('code')}: {str(j.get('msg'))[:60]}")
            break
        lst = j.get("data", [])  # oldest-first [ts, open, high, low, close, baseVol, quoteVol]
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


def funding_history(symbol: str, max_pages: int = 30) -> dict[int, float]:
    """Full funding-rate history (newest-first), paginated by incrementing pageNo -> {ts_ms: rate}."""
    rows: dict[int, float] = {}
    for pg in range(1, max_pages + 1):
        try:
            j = requests.get(BASE + "/api/v2/mix/market/history-fund-rate",
                             params={"symbol": symbol, "productType": PRODUCT_TYPE,
                                     "pageSize": 100, "pageNo": pg}, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"      fail {str(e)[:40]}")
            break
        if str(j.get("code")) not in ("0", "00000"):
            print(f"      code {j.get('code')} {str(j.get('msg'))[:40]}")
            break
        lst = j.get("data", []) or []
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingTime"])] = float(x["fundingRate"])
        time.sleep(0.12)
    return rows


def current_oi(symbol: str):
    """Current open interest (= holdingAmount) from the ticker, or None."""
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


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print(f"Collecting exchange PUBLIC {SYMBOL} (perp) — no API key\n")

    print("[1/3] candles (1h) ...", end=" ", flush=True)
    h = fetch_kline(SYMBOL)
    if h.empty:
        print("NO DATA — abort")
        return
    days = (h["timestamp"].iloc[-1] - h["timestamp"].iloc[0]) / 1000 / 86400
    print(f"{len(h)} bars, {days:.0f}d, {str(h['datetime'].iloc[0])[:10]}->{str(h['datetime'].iloc[-1])[:10]}")
    h.to_csv(ROOT / "data" / f"{TICKER.lower()}_perp_hourly.csv", index=False)

    print("[2/3] funding/history ...", end=" ", flush=True)
    fund = funding_history(SYMBOL)
    print(f"{len(fund)} points")

    print("[3/3] open-interest (current snapshot) ...", end=" ", flush=True)
    oi_now = current_oi(SYMBOL)
    print("ok" if oi_now is not None else "n/a")

    # Align everything to the candle close bars (backward merge_asof, same as scripts/37).
    merged = h[["timestamp", "datetime", "close"]].sort_values("timestamp").reset_index(drop=True)
    if fund:
        fdf = pd.DataFrame([{"timestamp": int(k), "funding": float(v)} for k, v in fund.items()]).sort_values("timestamp")
        merged = pd.merge_asof(merged, fdf, on="timestamp", direction="backward")
    # current OI snapshot only — attached to the latest bar (no fabricated history)
    merged["oi"] = pd.NA
    if oi_now is not None and not merged.empty:
        merged.loc[merged.index[-1], "oi"] = oi_now
    merged["buy_ratio"] = float("nan")  # retail long/short ratio not served keyless — keep schema identical

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
