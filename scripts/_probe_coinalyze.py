"""CoinAlyze: detect correct auth method, then check MNT coverage + liquidations."""
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]


def load_key():
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in (ROOT / ".env").read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith("COINALYZE_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError, FileNotFoundError):
            continue
    return None


KEY = sys.argv[1].strip() if len(sys.argv) > 1 else load_key()
BASE = "https://api.coinalyze.net/v1"
print(f"key: len={len(KEY) if KEY else 0} val={KEY[:6]}..{KEY[-4:] if KEY else ''}\n")

# detect auth method
method = None
for desc, kwargs in [("header api_key", {"headers": {"api_key": KEY}}),
                     ("query api_key", {"params": {"api_key": KEY}}),
                     ("header Authorization", {"headers": {"Authorization": KEY}})]:
    try:
        r = requests.get(f"{BASE}/exchanges", timeout=15, **kwargs)
        print(f"  {desc}: HTTP {r.status_code} {r.text[:50]}")
        if r.status_code == 200 and method is None:
            method = kwargs
    except Exception as e:  # noqa: BLE001
        print(f"  {desc}: FAIL {str(e)[:40]}")

if not method:
    print("\nNo auth method worked — key may be inactive or auth differs."); raise SystemExit
print(f"\n✓ auth works. Finding MNT perps...")

markets = requests.get(f"{BASE}/future-markets", timeout=20, **method).json()
mnt = [m for m in markets if isinstance(m, dict) and m.get("base_asset") == "MNT" and m.get("is_perpetual")]
print(f"  total markets: {len(markets)} | MNT perps: {len(mnt)}")
for m in mnt[:8]:
    print(f"    {m.get('symbol'):26} exch={m.get('exchange')}")
if not mnt:
    raise SystemExit

sym = mnt[0]["symbol"]
now, day_ago = int(time.time()), int(time.time()) - 86400
print(f"\nusing {sym}:")
for label, path, extra in [
    ("OI(USD)", "/open-interest", {"convert_to_usd": "true"}),
    ("funding", "/funding-rate", {}),
    ("LIQUIDATIONS", "/liquidation-history", {"interval": "1hour", "from": day_ago, "to": now, "convert_to_usd": "true"}),
    ("long-short", "/long-short-ratio-history", {"interval": "1hour", "from": day_ago, "to": now}),
]:
    p = dict(method.get("params", {})); p["symbols"] = sym; p.update(extra)
    kw = {"headers": method["headers"]} if "headers" in method else {}
    r = requests.get(f"{BASE}{path}", params=p, timeout=20, **kw)
    j = r.json() if r.status_code == 200 else r.text[:120]
    if isinstance(j, list) and j and isinstance(j[0], dict) and "history" in j[0]:
        h = j[0]["history"]
        print(f"  {label}: HTTP {r.status_code} | {len(h)} pts, last: {h[-1] if h else 'none'}")
    else:
        print(f"  {label}: HTTP {r.status_code} | {str(j)[:160]}")
