"""Dynamic Smart-Money Flow Engine (SKELETON) — whale-mode vs contract-mode, auto-selected per token.

The novel core: classify a token's holder composition (%balance held by CONTRACTS vs EOAs) → pick mode.
  • contract-heavy (Mantle assets) → CONTRACT mode (bridge/staking/CEX/LP flows)
  • EOA-rich (BTC/ETH majors)      → WHALE mode (individual whale accumulation/distribution)
Then compute aggregate metrics (robust to wallet-splitting, per [[WhaleAnalysis research]]) → 1-100 score → PM.

STATUS: skeleton. FREE metrics wired now (DexScreener flow + Etherscan supply + verified-whale watchlist).
Research-dependent hooks STUBBED (CEX/bridge netflow addresses + validated metric weights ← deep-research running).
Run: python scripts/48_smart_money_engine.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
ESCAN = "https://api.etherscan.io/v2/api"
DS = "https://api.dexscreener.com"
KEY = os.environ.get("MANTLESCAN_API_KEY") or "ARKZEP422CW6ASTDKJ2AESEGUVKPHI16RF"

# Holder composition MEASURED this session (scripts 46/47). contract_balance_pct = share of top-holder
# balance held by contracts. >0.5 → CONTRACT mode.
COMPOSITION = {
    "mETH": {"contract_pct": 0.78, "eoa_whales": 2, "note": "top holders = staking/LP/bridge contracts; only 2 conviction EOA whales"},
    # others measured later; BTC/ETH (if reachable) expected EOA-rich → whale mode
}
TOKENS = {
    "mETH": "0xcDA86A272531e8640cD7F1a92c01839911B90bb0",
    "cmETH": "0xE6829d9a7eE3040e1276Fa75293Bde931859e8fA",
    "WMNT": "0x78c1b0C915c4FAA5FffA6CAbf0219DA63d7f4cb8",
    "USDe": "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34",
    "FBTC": "0xC96dE26018A54D51c097160568752c4E3BD6C364",
}


def classify_mode(sym: str) -> tuple[str, str]:
    comp = COMPOSITION.get(sym)
    if comp:
        mode = "CONTRACT" if comp["contract_pct"] >= 0.5 else "WHALE"
        return mode, f"measured: {comp['contract_pct']*100:.0f}% contract-held — {comp['note']}"
    return "UNKNOWN", "composition not yet measured (run scripts/46 holder reconstruction)"


def dex_flow(sym: str) -> dict:
    j = requests.get(f"{DS}/latest/dex/search", params={"q": sym}, timeout=15).json()
    pairs = [p for p in j.get("pairs", []) if p.get("chainId") == "mantle" and p["baseToken"]["symbol"].upper() == sym.upper()]
    if not pairs:
        return {}
    liq = sum((p.get("liquidity") or {}).get("usd") or 0 for p in pairs)
    buys = sum(((p.get("txns") or {}).get("h24") or {}).get("buys") or 0 for p in pairs)
    sells = sum(((p.get("txns") or {}).get("h24") or {}).get("sells") or 0 for p in pairs)
    flow_bias = buys / (buys + sells) if (buys + sells) else None
    price = (sum(float(p.get("priceUsd") or 0) * ((p.get("liquidity") or {}).get("usd") or 0) for p in pairs) / liq) if liq else 0
    return {"price": round(price, 4), "liq_usd": round(liq), "buys24h": buys, "sells24h": sells,
            "flow_bias": round(flow_bias, 3) if flow_bias is not None else None}


def supply(addr: str) -> float | None:
    j = requests.get(ESCAN, params={"chainid": 5000, "module": "stats", "action": "tokensupply",
                                     "contractaddress": addr, "apikey": KEY}, timeout=20).json()
    try:
        return int(j.get("result", "0")) / 1e18
    except (ValueError, TypeError):
        return None


def score(metrics: dict) -> tuple[int, str]:
    """Preliminary 1-100 (weights are PLACEHOLDER → deep-research will validate). Currently flow-bias driven."""
    fb = metrics.get("flow_bias")
    if fb is None:
        return 50, "neutral (no flow data)"
    s = int(round(fb * 100))  # 0.5 → 50 neutral; >0.5 buy pressure
    label = "Bullish" if s >= 60 else ("Bearish" if s <= 40 else "Neutral")
    return s, label


def desk(sym: str):
    addr = TOKENS[sym]
    mode, why = classify_mode(sym)
    dx = dex_flow(sym)
    sup = supply(addr)
    s, lbl = score(dx)
    print(f"\n🧠 SMART-MONEY DESK — {sym} (Mantle)")
    print(f"  MODE: {mode}  [{why}]")
    print(f"  DEX (free): price ${dx.get('price')} · liq ${dx.get('liq_usd'):,} · buys/sells24h {dx.get('buys24h')}/{dx.get('sells24h')} · flow_bias {dx.get('flow_bias')}" if dx else "  DEX: no Mantle pairs")
    print(f"  Supply (TVL proxy): {sup:,.0f} {sym}" if sup else "  Supply: n/a")
    print(f"  PRELIM SCORE: {s}/100 → {lbl}   ⚠️ flow-only placeholder")
    print("  STUBS (← deep-research): CEX-netflow, bridge-netflow, staking-TVL-trend, large-swap>$10k ratio, validated weights")
    if mode == "CONTRACT":
        print("  → CONTRACT mode active: will weight bridge/staking/CEX-LP flows once addresses known.")


def main():
    for sym in ["mETH", "cmETH", "WMNT", "USDe", "FBTC"]:
        try:
            desk(sym)
        except Exception as e:  # noqa: BLE001
            print(f"\n{sym}: ERROR {str(e)[:80]}")
    print("\n(skeleton runs on free data + measured mETH composition; full metrics+weights pending deep-research)")


if __name__ == "__main__":
    main()
