"""Probe reachability of HYPE perp data sources. Read-only, no files written."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]


def load_key(name: str):
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in (ROOT / ".env").read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError, FileNotFoundError):
            continue
    return None


print("=" * 70)
print("1) HYPERLIQUID /info  metaAndAssetCtxs")
try:
    r = requests.post("https://api.hyperliquid.xyz/info",
                      json={"type": "metaAndAssetCtxs"}, timeout=25)
    print("   HTTP", r.status_code)
    d = r.json()
    names = [u["name"] for u in d[0]["universe"]]
    print("   HYPE listed:", "HYPE" in names, "| total coins:", len(names))
    if "HYPE" in names:
        i = names.index("HYPE")
        ctx = d[1][i]
        print("   ctx keys:", list(ctx.keys()))
        print("   HYPE openInterest:", ctx.get("openInterest"),
              "| markPx:", ctx.get("markPx"),
              "| funding:", ctx.get("funding"),
              "| oraclePx:", ctx.get("oraclePx"))
except Exception as e:  # noqa: BLE001
    print("   FAIL:", repr(e)[:200])

print("\n2) HYPERLIQUID /info  candleSnapshot (1h, last 7d sample)")
try:
    now = int(time.time() * 1000)
    start = now - 7 * 24 * 3600 * 1000
    r = requests.post("https://api.hyperliquid.xyz/info",
                      json={"type": "candleSnapshot",
                            "req": {"coin": "HYPE", "interval": "1h",
                                    "startTime": start, "endTime": now}},
                      timeout=25)
    print("   HTTP", r.status_code)
    j = r.json()
    print("   n candles:", len(j))
    if j:
        print("   first:", j[0])
        print("   last :", j[-1])
except Exception as e:  # noqa: BLE001
    print("   FAIL:", repr(e)[:200])

print("\n3) HYPERLIQUID /info  fundingHistory (last 7d sample)")
try:
    now = int(time.time() * 1000)
    start = now - 7 * 24 * 3600 * 1000
    r = requests.post("https://api.hyperliquid.xyz/info",
                      json={"type": "fundingHistory", "coin": "HYPE",
                            "startTime": start, "endTime": now}, timeout=25)
    print("   HTTP", r.status_code)
    j = r.json()
    print("   n funding pts:", len(j))
    if j:
        print("   first:", j[0])
        print("   last :", j[-1])
except Exception as e:  # noqa: BLE001
    print("   FAIL:", repr(e)[:200])

print("\n4) BINANCE futures  openInterestHist (THE OI signal)")
try:
    r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                     params={"symbol": "HYPEUSDT", "period": "1h", "limit": 5}, timeout=25)
    print("   HTTP", r.status_code, "|", r.text[:300])
except Exception as e:  # noqa: BLE001
    print("   FAIL:", repr(e)[:200])

print("\n5) BINANCE futures  klines + fundingRate")
try:
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": "HYPEUSDT", "interval": "1h", "limit": 3}, timeout=25)
    print("   klines HTTP", r.status_code, "| n=",
          len(r.json()) if r.status_code == 200 else r.text[:200])
    r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                     params={"symbol": "HYPEUSDT", "limit": 3}, timeout=25)
    print("   fundingRate HTTP", r.status_code, "|", r.text[:200])
except Exception as e:  # noqa: BLE001
    print("   FAIL:", repr(e)[:200])

print("\n6) COINALYZE  open-interest-history (key from .env)")
key = load_key("COINALYZE_API_KEY")
print("   key:", f"len={len(key)} {key[:6]}..{key[-4:]}" if key else "MISSING")
if key:
    base = "https://api.coinalyze.net/v1"
    auth = None
    for desc, kw in [("header api_key", {"headers": {"api_key": key}}),
                     ("query api_key", {"params": {"api_key": key}})]:
        try:
            r = requests.get(f"{base}/future-markets", timeout=20, **kw)
            print(f"   {desc}: HTTP {r.status_code} {r.text[:60]}")
            if r.status_code == 200 and auth is None:
                auth = kw
        except Exception as e:  # noqa: BLE001
            print(f"   {desc}: FAIL {str(e)[:50]}")
    if auth:
        try:
            mkts = requests.get(f"{base}/future-markets", timeout=20, **auth).json()
            hype = [m for m in mkts if isinstance(m, dict)
                    and m.get("base_asset") == "HYPE" and m.get("is_perpetual")]
            print(f"   total markets: {len(mkts)} | HYPE perps: {len(hype)}")
            for m in hype[:6]:
                print(f"     {m.get('symbol'):28} exch={m.get('exchange')}")
        except Exception as e:  # noqa: BLE001
            print("   markets FAIL:", repr(e)[:150])

print("\n" + "=" * 70)
print("PROBE DONE")
