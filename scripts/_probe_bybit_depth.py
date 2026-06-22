"""Research probe — how DEEP is the history for each perp-venue positioning endpoint? (decides backtestability)
Paginate backward via endTime cursor until exhausted. MNT perp, mainnet.
NOTE: dead research. The retail long/short ratio (account-ratio) and an OI history series have no clean
keyless equivalent on our current venue; this script is kept only as an exploratory artifact."""
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.bybit.com"  # legacy perp-venue host probed here — research only


def depth(label, path, params, ts_key, max_pages):
    rows, cursor, pages = {}, None, 0
    for _ in range(max_pages):
        p = dict(params)
        if cursor is not None:
            p["endTime"] = cursor
        try:
            j = requests.get(BASE + path, params=p, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"  {label}: FAIL {str(e)[:50]}"); return
        if j.get("retCode") != 0:
            print(f"  {label}: retCode {j.get('retCode')} {j.get('retMsg')}"); break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        pages += 1
        for x in lst:
            rows[int(x[ts_key])] = x
        oldest = min(int(x[ts_key]) for x in lst)
        if cursor is not None and oldest >= cursor:
            break
        cursor = oldest - 1
        time.sleep(0.15)
    if not rows:
        print(f"{label}: NO DATA"); return
    ts = sorted(rows)
    days = (ts[-1] - ts[0]) / 1000 / 86400
    print(f"{label}: {len(rows):>5} points / {pages} pages | "
          f"{datetime.fromtimestamp(ts[0]/1000, tz=timezone.utc):%Y-%m-%d} -> "
          f"{datetime.fromtimestamp(ts[-1]/1000, tz=timezone.utc):%Y-%m-%d} = {days:.0f} days "
          f"{'  <- BACKTESTABLE' if days > 180 else '  <- shallow' if days < 60 else ''}")


SYM = "MNTUSDT"
print(f"History depth for {SYM} (linear perp):\n")
depth("funding    ", "/v5/market/funding/history", {"category": "linear", "symbol": SYM, "limit": 200}, "fundingRateTimestamp", 30)
depth("open-int 1h", "/v5/market/open-interest", {"category": "linear", "symbol": SYM, "intervalTime": "1h", "limit": 200}, "timestamp", 30)
depth("long/short ", "/v5/market/account-ratio", {"category": "linear", "symbol": SYM, "period": "1h", "limit": 500}, "timestamp", 30)
