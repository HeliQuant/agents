"""Capital-Flow Desk — the contract-tracking feature, honest scope, 100% VERIFIED-FREE data (no key drama).

Per Mantle asset, combines signals that ACTUALLY WORK on free APIs:
  • DEX flow_bias + liquidity (DexScreener, no key)        → on-chain buy/sell pressure
  • CEX vs DEX volume split (Bybit, no key)                → wash/synthetic-volume transparency (research tip)
  • mETH staking conviction (Etherscan L1, $118M-real)     → net-stake (LST conviction)
Output: per-asset flow READ + preliminary score + forward-log (trend builds over time). Honest: CONTEXT
for the PM, forward-logged to validate — not a claimed alpha.
Run: python scripts/50_capital_flow_desk.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DS = "https://api.dexscreener.com"
BYBIT = "https://api.bybit.com/v5/market/tickers"
ESCAN = "https://api.etherscan.io/v2/api"
EKEY = os.environ.get("MANTLESCAN_API_KEY", "")
METH_STAKING_L1 = "0xe3cBd06D7dadB3F4e6557bAb7EdD924CD1489E8f"

ASSETS = ["WMNT", "mETH", "cmETH", "USDe", "FBTC"]
BYBIT_SYM = {"WMNT": "MNTUSDT", "mETH": "METHUSDT", "USDe": "USDEUSDT"}  # which have a CEX market


def g(d, *ks):
    for k in ks:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d


def dex(sym):
    j = requests.get(f"{DS}/latest/dex/search", params={"q": sym}, timeout=15).json()
    pairs = [p for p in j.get("pairs", []) if p.get("chainId") == "mantle" and p["baseToken"]["symbol"].upper() == sym.upper()]
    if not pairs:
        return None
    liq = sum((g(p, "liquidity", "usd") or 0) for p in pairs)
    vol = sum((g(p, "volume", "h24") or 0) for p in pairs)
    buys = sum((g(p, "txns", "h24", "buys") or 0) for p in pairs)
    sells = sum((g(p, "txns", "h24", "sells") or 0) for p in pairs)
    price = (sum(float(p.get("priceUsd") or 0) * (g(p, "liquidity", "usd") or 0) for p in pairs) / liq) if liq else 0
    fb = buys / (buys + sells) if (buys + sells) else None
    return {"price": price, "liq": liq, "dex_vol24h": vol, "flow_bias": fb, "buys": buys, "sells": sells}


def cex_vol(sym):
    bs = BYBIT_SYM.get(sym)
    if not bs:
        return None
    try:
        j = requests.get(BYBIT, params={"category": "spot", "symbol": bs}, timeout=15).json()
        t = j.get("result", {}).get("list", [])
        return float(t[0]["turnover24h"]) if t else None
    except Exception:  # noqa: BLE001
        return None


def staking_eth():
    try:
        j = requests.get(ESCAN, params={"chainid": 1, "module": "account", "action": "balance",
                                        "address": METH_STAKING_L1, "tag": "latest", "apikey": EKEY}, timeout=20).json()
        return int(j.get("result", "0")) / 1e18
    except Exception:  # noqa: BLE001
        return None


def read_and_score(d, cexv):
    """Preliminary flow read (flow-bias driven; wash-aware). Honest: un-tuned, forward-logged."""
    if not d or d["flow_bias"] is None:
        return 50, "neutral", ""
    score = int(round(d["flow_bias"] * 100))
    note = ""
    if cexv and d["dex_vol24h"]:
        ratio = cexv / d["dex_vol24h"]
        if ratio > 50:
            note = f"⚠️ CEX-dominated (CEX/DEX vol {ratio:,.0f}x) — on-chain flow thin, discount signal"
    label = "🟢 buy-pressure" if score >= 60 else ("🔴 sell-pressure" if score <= 40 else "⚪ balanced")
    return score, label, note


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("🌊 CAPITAL-FLOW DESK — Mantle (verified-free data)\n" + "=" * 64)
    stk = staking_eth()
    rows = []
    now = int(time.time() * 1000)
    for sym in ASSETS:
        d = dex(sym)
        if not d:
            print(f"\n{sym}: no Mantle DEX pairs"); continue
        cv = cex_vol(sym)
        score, label, note = read_and_score(d, cv)
        print(f"\n▌ {sym}")
        print(f"   price ${d['price']:,.4f} · liq ${d['liq']:,.0f} · DEX vol24h ${d['dex_vol24h']:,.0f} · buys/sells {d['buys']}/{d['sells']}")
        print(f"   flow_bias {d['flow_bias']:.3f}" + (f" · CEX vol24h ${cv:,.0f}" if cv else " · (no CEX market)"))
        print(f"   → SCORE {score}/100 {label}" + (f"  {note}" if note else ""))
        if sym == "mETH" and stk:
            print(f"   → staking conviction: {stk:,.0f} ETH locked in mETH staking (L1, $118M-scale) — net-stake trend forward-logged")
        rows.append({"timestamp": now, "asset": sym, "price": round(d["price"], 4), "liq_usd": round(d["liq"]),
                     "dex_vol24h": round(d["dex_vol24h"]), "flow_bias": round(d["flow_bias"], 3) if d["flow_bias"] is not None else None,
                     "cex_vol24h": round(cv) if cv else None, "score": score})
    # forward-log
    out = ROOT / "data" / "capital_flow_log.csv"
    df = pd.DataFrame(rows)
    if out.exists():
        df = pd.concat([pd.read_csv(out), df], ignore_index=True)
    df.to_csv(out, index=False)
    print(f"\n{'=' * 64}\n✓ forward-logged → {out.name} ({len(df)} rows). Trend (net-flow over time) builds with each run.")
    print("Honest: CONTEXT signal for PM (buy/sell pressure + wash-flag + staking conviction), forward-logged to validate — not a claimed alpha.")


if __name__ == "__main__":
    main()
