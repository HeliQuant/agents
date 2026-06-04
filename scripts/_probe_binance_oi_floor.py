"""Page Binance HYPEUSDT openInterestHist all the way back to find the true floor."""
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


URL = "https://fapi.binance.com/futures/data/openInterestHist"
all_pts = {}
end = int(time.time() * 1000)
pages = 0
while True:
    r = requests.get(URL, params={"symbol": "HYPEUSDT", "period": "1h",
                                  "limit": 500, "endTime": end}, timeout=25)
    if r.status_code != 200:
        print("stop: HTTP", r.status_code, r.text[:100])
        break
    j = r.json()
    if not j:
        print("stop: empty page")
        break
    before = len(all_pts)
    for p in j:
        all_pts[p["timestamp"]] = float(p["sumOpenInterest"])
    pages += 1
    oldest = min(x["timestamp"] for x in j)
    new = len(all_pts) - before
    print(f"page {pages}: {len(j)} pts, oldest={ts(oldest)}, new unique={new}")
    if new == 0:
        print("-> no new data, floor reached")
        break
    end = oldest - 3600000
    time.sleep(0.25)
    if pages > 12:
        print("-> page cap hit")
        break

if all_pts:
    keys = sorted(all_pts)
    span_h = (keys[-1] - keys[0]) / 3600000
    print(f"\nTOTAL unique OI pts: {len(all_pts)}")
    print(f"range: {ts(keys[0])} .. {ts(keys[-1])}  (~{span_h/24:.1f} days)")
    print(f"max non-overlap 24h trades: ~{int(span_h/24)}")
