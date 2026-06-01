"""Probe real on-chain volume sources BEFORE building a collector (verify-first).

Checks:
  1. Mantlescan API key works (block explorer — raw logs/balances, NOT candles).
  2. GeckoTerminal OHLCV for a Mantle DEX pool — real on-chain DEX candles WITH
     volume. How much hourly history + is volume populated.

Run: python scripts/_probe_onchain_volume.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]

# Load .env (project root is one level above agents/)
env = {}
env_path = ROOT.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
KEY = env.get("MANTLESCAN_API_KEY", "")


def ts(x: int) -> str:
    return datetime.fromtimestamp(int(x), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


print("=== 1) Mantlescan API (does the key work?) ===")
try:
    r = requests.get(
        "https://api.mantlescan.xyz/api",
        params={"module": "proxy", "action": "eth_blockNumber", "apikey": KEY},
        timeout=25,
    )
    j = r.json()
    res = j.get("result")
    bn = int(res, 16) if isinstance(res, str) and res.startswith("0x") else res
    print(f"  HTTP {r.status_code} | latest block: {bn} | raw: {str(j)[:120]}")
except Exception as e:  # noqa: BLE001
    print(f"  Mantlescan ERR: {repr(e)[:200]}")

# Candidate Mantle MNT pools (from prior session's GeckoTerminal scan)
POOLS = {
    "USDe/WMNT": "0xeafc4d6d4c3391cd4fc10c85d2f5f972d58c0dd5",
    "USDC/WMNT (iZiSwap)": "0x8605c9d608a3f773b87fe1db5582ad35fe212144",
}

for label, pool in POOLS.items():
    print(f"=== 2) GeckoTerminal OHLCV hour — {label} ({pool[:10]}...) ===")
    try:
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/mantle/pools/{pool}/ohlcv/hour",
            params={"aggregate": "1", "limit": "1000"},
            timeout=25,
            headers={"Accept": "application/json"},
        )
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            ohlcv = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            print(f"  candles: {len(ohlcv)}")
            if ohlcv:
                nz_vol = sum(1 for row in ohlcv if float(row[5]) > 0)
                print(f"  range: {ts(ohlcv[-1][0])} -> {ts(ohlcv[0][0])}")
                print(f"  rows with volume>0: {nz_vol}/{len(ohlcv)}")
                print(f"  newest [ts,o,h,l,c,vol]: {ohlcv[0]}")
        else:
            print(f"  body: {r.text[:160]}")
    except Exception as e:  # noqa: BLE001
        print(f"  GeckoTerminal ERR: {repr(e)[:200]}")
