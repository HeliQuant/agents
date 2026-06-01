"""Holdings-based whale ranking via Etherscan v2 (Mantle) — separate real holders from churners.

The volume-ranked watchlist catches high-turnover bots/arb (big volume, tiny bag). A REAL whale
HOLDS a big position. This checks each candidate's actual on-chain holdings:
  native MNT (gas) + WMNT + mETH  ->  holdings_usd
and ranks by HOLDINGS, exposing who's a conviction holder vs a churner.

Prices from our own Pyth feature CSVs (last close). Run: python scripts/32_whale_holdings.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
KEY = env.get("MANTLESCAN_API_KEY") or env.get("ETHERSCAN_API_KEY") or ""
BASE = "https://api.etherscan.io/v2/api"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"
METH = "0xcDA86A272531e8640cD7F1a92c01839911B90bb0"


def _price(asset: str, default: float) -> float:
    try:
        return round(float(pd.read_csv(ROOT / "data" / f"{asset}_features.csv")["close"].iloc[-1]), 4)
    except Exception:  # noqa: BLE001
        return default


P_MNT, P_METH = _price("mnt", 0.65), _price("meth", 2000.0)


def _get(params: dict):
    params.update({"chainid": 5000, "apikey": KEY})
    try:
        return requests.get(BASE, params=params, timeout=25).json().get("result")
    except Exception:  # noqa: BLE001
        return None


def native_mnt(addr: str) -> float:
    r = _get({"module": "account", "action": "balance", "address": addr, "tag": "latest"})
    try:
        return int(r) / 1e18
    except Exception:  # noqa: BLE001
        return 0.0


def token_bal(contract: str, addr: str) -> float:
    r = _get({"module": "account", "action": "tokenbalance", "contractaddress": contract,
              "address": addr, "tag": "latest"})
    try:
        return int(r) / 1e18
    except Exception:  # noqa: BLE001
        return 0.0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    whales = json.loads((ROOT / "data" / "whale_watchlist.json").read_text(encoding="utf-8"))
    print(f"Holdings check via Etherscan v2 — prices MNT=${P_MNT}, mETH=${P_METH}\n")
    print(f"{'address':14} {'MNT':>9} {'WMNT':>9} {'mETH':>7} {'HOLDINGS$':>11} {'VOL$':>10}  verdict")
    rows = []
    for w in whales:
        a = w.get("address", "")
        mnt = native_mnt(a); time.sleep(0.22)
        wmnt = token_bal(WMNT, a); time.sleep(0.22)
        meth = token_bal(METH, a); time.sleep(0.22)
        hold = (mnt + wmnt) * P_MNT + meth * P_METH
        vol = float(w.get("total_volume_usd") or 0)
        verdict = ("WHALE (holder)" if hold >= 10_000
                   else "churner/bot" if vol > 40_000 and hold < 2_000
                   else "small")
        rows.append({"addr": a, "holdings_usd": round(hold), "vol": round(vol),
                     "bias": w.get("direction_bias"), "verdict": verdict})
        print(f"{a[:14]} {mnt:>9,.1f} {wmnt:>9,.1f} {meth:>7,.3f} ${hold:>10,.0f} ${vol:>9,.0f}  {verdict}")

    rows.sort(key=lambda r: r["holdings_usd"], reverse=True)
    real = [r for r in rows if r["verdict"].startswith("WHALE")]
    churn = [r for r in rows if r["verdict"] == "churner/bot"]
    print(f"\nsummary: {len(real)} real holder-whale(s) | {len(churn)} churner/bot | {len(rows)-len(real)-len(churn)} small")
    print("ranked by HOLDINGS (top 3):")
    for r in rows[:3]:
        print(f"  {r['addr'][:16]} ${r['holdings_usd']:,} held | {r['bias']} | {r['verdict']}")


if __name__ == "__main__":
    main()
