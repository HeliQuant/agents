"""Dynamic Smart-Money Flow Engine — auto-selects mode per asset → unified score for the PM.

  📜 CONTRACT mode (Mantle assets): DEX flow_bias + liquidity + mETH staking conviction (on-chain).
  📈 POSITIONING mode (majors BTC/ETH/SOL): Bybit OI 24h-change + funding + long-short (derivatives
     positioning = where perp whales sit; contrarian-to-crowding, aligned with our OI-contrarian strategy).

Honest: CONTEXT signal for the PM, forward-logged to validate (not a claimed alpha). All FREE data.
Run: python scripts/51_smart_money_engine.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DS = "https://api.dexscreener.com"
BYBIT = "https://api.bybit.com"
ESCAN = "https://api.etherscan.io/v2/api"
EKEY = os.environ.get("MANTLESCAN_API_KEY", "")
METH_STAKING_L1 = "0xe3cBd06D7dadB3F4e6557bAb7EdD924CD1489E8f"

MODE = {  # dynamic: Mantle eco = contract-heavy; majors = deep-perp positioning
    "WMNT": "CONTRACT", "mETH": "CONTRACT", "cmETH": "CONTRACT", "USDe": "CONTRACT", "FBTC": "CONTRACT",
    "BTC": "POSITIONING", "ETH": "POSITIONING", "SOL": "POSITIONING",
}
PERP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


def g(d, *ks):
    for k in ks:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d


# ---------- CONTRACT mode (Mantle on-chain) ----------
def contract_signal(sym):
    j = requests.get(f"{DS}/latest/dex/search", params={"q": sym}, timeout=15).json()
    pairs = [p for p in j.get("pairs", []) if p.get("chainId") == "mantle" and p["baseToken"]["symbol"].upper() == sym.upper()]
    if not pairs:
        return None
    liq = sum((g(p, "liquidity", "usd") or 0) for p in pairs)
    vol = sum((g(p, "volume", "h24") or 0) for p in pairs)
    buys = sum((g(p, "txns", "h24", "buys") or 0) for p in pairs)
    sells = sum((g(p, "txns", "h24", "sells") or 0) for p in pairs)
    price = (sum(float(p.get("priceUsd") or 0) * (g(p, "liquidity", "usd") or 0) for p in pairs) / liq) if liq else 0
    fb = buys / (buys + sells) if (buys + sells) else 0.5
    score = int(round(fb * 100))
    extra = ""
    if sym == "mETH":
        try:
            jb = requests.get(ESCAN, params={"chainid": 1, "module": "account", "action": "balance",
                                             "address": METH_STAKING_L1, "tag": "latest", "apikey": EKEY}, timeout=20).json()
            eth = int(jb.get("result", "0")) / 1e18
            extra = f" · staking conviction {eth:,.0f} ETH locked (L1)"
        except Exception:  # noqa: BLE001
            pass
    lbl = "🟢 buy-pressure" if score >= 60 else ("🔴 sell-pressure" if score <= 40 else "⚪ balanced")
    return {"score": score, "label": lbl,
            "detail": f"DEX flow_bias {fb:.3f} · liq ${liq:,.0f} · vol24h ${vol:,.0f} · buys/sells {buys}/{sells}{extra}"}


# ---------- POSITIONING mode (majors perp) ----------
def positioning_signal(sym):
    s = PERP[sym]
    src = "live"
    try:
        oi = requests.get(f"{BYBIT}/v5/market/open-interest", params={"category": "linear", "symbol": s, "intervalTime": "1h", "limit": 24}, timeout=12).json()
        oil = oi.get("result", {}).get("list", [])
        oi_chg = ((float(oil[0]["openInterest"]) / float(oil[-1]["openInterest"]) - 1) * 100) if len(oil) > 1 else 0
        fr = requests.get(f"{BYBIT}/v5/market/funding/history", params={"category": "linear", "symbol": s, "limit": 1}, timeout=12).json()
        funding = float(fr["result"]["list"][0]["fundingRate"]) * 100
        ar = requests.get(f"{BYBIT}/v5/market/account-ratio", params={"category": "linear", "symbol": s, "period": "1h", "limit": 1}, timeout=12).json()
        buy_ratio = float(ar["result"]["list"][0]["buyRatio"])
    except Exception:  # noqa: BLE001 — Bybit intermittently geo-blocked; fall back to cached scripts/37 data
        fp = ROOT / "data" / f"{sym.lower()}_positioning.csv"
        if not fp.exists():
            return {"score": 50, "label": "n/a", "detail": "no live Bybit + no cache"}
        df = pd.read_csv(fp)
        buy_ratio = float(df["buy_ratio"].iloc[-1])
        funding = float(df["funding"].iloc[-1]) * 100
        oi_chg = ((float(df["oi"].iloc[-1]) / float(df["oi"].iloc[-25]) - 1) * 100) if len(df) > 25 else 0.0
        src = "cached"
    # contrarian-to-crowding: heavy long-crowd → contrarian bearish (low score); balanced → 50
    score = int(round(100 - buy_ratio * 100))  # buy_ratio 0.78→22 (crowded long=caution); 0.30→70 (crowded short=bullish)
    crowd = "long-crowded" if buy_ratio > 0.6 else ("short-crowded" if buy_ratio < 0.4 else "balanced")
    lbl = "🟢 contrarian-bull" if score >= 60 else ("🔴 contrarian-bear / crowded-long" if score <= 40 else "⚪ neutral")
    return {"score": score, "label": lbl,
            "detail": f"[{src}] OI 24h {oi_chg:+.1f}% · funding {funding:+.4f}% · long/short {buy_ratio:.2f} ({crowd})"}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("🧠 DYNAMIC SMART-MONEY FLOW ENGINE — auto mode per asset\n" + "=" * 70)
    rows, now = [], int(time.time() * 1000)
    for sym, mode in MODE.items():
        sig = contract_signal(sym) if mode == "CONTRACT" else positioning_signal(sym)
        if not sig:
            print(f"\n{sym} [{mode}]: no data"); continue
        icon = "📜" if mode == "CONTRACT" else "📈"
        print(f"\n{icon} {sym:5} [{mode}]  SCORE {sig['score']:>3}/100  {sig['label']}")
        print(f"        {sig['detail']}")
        rows.append({"timestamp": now, "asset": sym, "mode": mode, "score": sig["score"], "read": sig["label"]})
    out = ROOT / "data" / "smart_money_log.csv"
    df = pd.DataFrame(rows)
    if out.exists():
        df = pd.concat([pd.read_csv(out), df], ignore_index=True)
    df.to_csv(out, index=False)
    print(f"\n{'=' * 70}\n✓ {len(rows)} assets scored (CONTRACT={sum(1 for r in rows if r['mode']=='CONTRACT')}, "
          f"POSITIONING={sum(1 for r in rows if r['mode']=='POSITIONING')}) → forward-logged to {out.name}")
    print("Honest: CONTEXT for PM (dynamic per-asset), forward-logged to validate. Mantle=on-chain flow · majors=perp positioning (OI-contrarian-aligned).")


if __name__ == "__main__":
    main()
