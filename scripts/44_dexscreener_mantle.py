"""Mantle on-chain DEX snapshot via DexScreener (FREE, no key, 60 rpm).

Honest scope: DexScreener public API = PAIR-LEVEL aggregate (price/liquidity/volume/buy-sell COUNTS),
NOT per-wallet whale txs. So this is a real Mantle ON-CHAIN data source (satisfies the track's
"Mantle on-chain data core" requirement) + a DEX order-flow proxy (buy/sell tx bias) — but it does
NOT identify individual whales (that needs Cielo). Pulls our Mantle-eco core assets, aggregates
across all Mantle DEXs (merchantmoe/agni/fusionx/oku), appends a timestamped snapshot for forward-logging.

Run: python scripts/44_dexscreener_mantle.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.dexscreener.com"
CORE = ["WMNT", "mETH", "cmETH", "USDe", "FBTC"]


def g(d, *keys):
    for k in keys:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d


def snapshot(now_ms: int):
    rows = []
    for sym in CORE:
        try:
            j = requests.get(f"{BASE}/latest/dex/search", params={"q": sym}, timeout=15).json()
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: FAIL {str(e)[:40]}"); continue
        pairs = [p for p in j.get("pairs", [])
                 if p.get("chainId") == "mantle" and p["baseToken"]["symbol"].upper() == sym.upper()]
        if not pairs:
            print(f"  {sym}: no Mantle pairs"); continue
        liq = sum((g(p, "liquidity", "usd") or 0) for p in pairs)
        vol = sum((g(p, "volume", "h24") or 0) for p in pairs)
        buys = sum((g(p, "txns", "h24", "buys") or 0) for p in pairs)
        sells = sum((g(p, "txns", "h24", "sells") or 0) for p in pairs)
        price = (sum(float(p.get("priceUsd") or 0) * (g(p, "liquidity", "usd") or 0) for p in pairs) / liq) if liq else 0
        flow = (buys / (buys + sells)) if (buys + sells) else None
        rows.append({"timestamp": now_ms, "symbol": sym, "pairs": len(pairs),
                     "price_usd": round(price, 4), "liquidity_usd": round(liq),
                     "vol24h_usd": round(vol), "buys24h": buys, "sells24h": sells,
                     "flow_bias": round(flow, 3) if flow is not None else None})
    return rows


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    now_ms = int(time.time() * 1000)
    rows = snapshot(now_ms)
    print("\nMantle DEX snapshot (DexScreener, aggregated across DEXs):\n")
    print(f"{'asset':6} {'pairs':>5} {'price':>11} {'liquidity':>13} {'vol24h':>12} {'buys/sells':>12} {'flow_bias':>9}")
    for r in rows:
        print(f"{r['symbol']:6} {r['pairs']:5} {r['price_usd']:>11} ${r['liquidity_usd']:>11,} ${r['vol24h_usd']:>10,} "
              f"{str(r['buys24h'])+'/'+str(r['sells24h']):>12} {str(r['flow_bias']):>9}")
    out = ROOT / "data" / "mantle_dex_snapshot.csv"
    df = pd.DataFrame(rows)
    if out.exists():
        df = pd.concat([pd.read_csv(out), df], ignore_index=True)
    df.to_csv(out, index=False)
    print(f"\nflow_bias = buys/(buys+sells): >0.5 net-accumulation, <0.5 net-distribution (24h, pair-level proxy)")
    print(f"appended snapshot -> {out.name} ({len(df)} total rows)")


if __name__ == "__main__":
    main()
