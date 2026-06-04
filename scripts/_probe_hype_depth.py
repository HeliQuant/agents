"""Probe the real DEPTH/window of the two usable HYPE sources before full fetch."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


print("BINANCE openInterestHist depth test (limit=500, period=1h):")
r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                 params={"symbol": "HYPEUSDT", "period": "1h", "limit": 500}, timeout=25)
j = r.json()
print("  HTTP", r.status_code, "| n pts:", len(j))
if j:
    print("  oldest:", ts(j[0]["timestamp"]), "| newest:", ts(j[-1]["timestamp"]))
    span_h = (j[-1]["timestamp"] - j[0]["timestamp"]) / 3600000
    print(f"  span: {span_h:.0f} h  (~{span_h/24:.1f} days)  -> ~{span_h/24:.0f} non-overlap 24h trades max")

# can we page BEFORE the oldest with endTime?
if j:
    older_end = j[0]["timestamp"] - 3600000
    r2 = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                      params={"symbol": "HYPEUSDT", "period": "1h",
                              "limit": 500, "endTime": older_end}, timeout=25)
    j2 = r2.json()
    print("\n  page-back attempt (endTime before oldest):")
    print("   HTTP", r2.status_code, "| n:", len(j2) if isinstance(j2, list) else j2)
    if isinstance(j2, list) and j2:
        print("   that page oldest:", ts(j2[0]["timestamp"]), "newest:", ts(j2[-1]["timestamp"]))
        if j2[0]["timestamp"] >= j[0]["timestamp"]:
            print("   -> NO older data (Binance hard-caps ~30d OI history)")

print("\nBINANCE klines depth (limit=1500, 1h):")
r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                 params={"symbol": "HYPEUSDT", "interval": "1h", "limit": 1500}, timeout=25)
k = r.json()
print("  HTTP", r.status_code, "| n:", len(k))
if k:
    print("  oldest:", ts(k[0][0]), "| newest:", ts(k[-1][0]),
          f"  (~{(k[-1][0]-k[0][0])/3600000/24:.0f} days)")

print("\nHYPERLIQUID candleSnapshot single-call cap (ask 12 months):")
now = int(time.time() * 1000)
start = now - 365 * 24 * 3600 * 1000
r = requests.post("https://api.hyperliquid.xyz/info",
                  json={"type": "candleSnapshot",
                        "req": {"coin": "HYPE", "interval": "1h",
                                "startTime": start, "endTime": now}}, timeout=30)
h = r.json()
print("  HTTP", r.status_code, "| n candles:", len(h))
if h:
    print("  oldest:", ts(h[0]["t"]), "| newest:", ts(h[-1]["t"]),
          f"  (~{(h[-1]['t']-h[0]['t'])/3600000/24:.0f} days returned in one call)")
