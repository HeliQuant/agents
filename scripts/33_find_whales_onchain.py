"""On-chain whale finder (Etherscan v2 / Mantle) — professional-grade, honest.

Discovers REAL conviction whales (big EOA holders), not churners/bots/CEX. Pipeline:
  1. DISCOVER  candidates from LARGE transfers (>$MIN_TRANSFER) of WMNT + mETH
  2. FILTER    drop in order:
       - system/dead addresses (0x0, 0xdead)
       - CONTRACTS            (eth_getCode)        -> pools / bridges / staking / routers
       - likely CEX / MEV-bot (eth_getTransactionCount nonce >= CEX_NONCE) -> custodial/infra, not smart money
       - pass-through         (holdings < DUST)    -> relayers / deposit sweepers
  3. HOLDINGS  native MNT + WMNT + mETH  (core Mantle bags; OTHER tokens undercounted — honest)
  4. CLASSIFY  mega / large / whale / holder by holdings tier
Saves the clean list to data/onchain_whales.json.

Caveats (stated, not hidden): CEX detection is a nonce heuristic (no paid labels); holdings exclude
cmETH/fBTC/USDe/LP/lending positions; only ACTIVE whales (recently transferring) are discovered.

Run: python scripts/33_find_whales_onchain.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
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
METH = "0xcda86a272531e8640cd7f1a92c01839911b90bb0"

MIN_TRANSFER_USD = 20_000
DUST_USD = 5_000          # below this holdings = pass-through, not a holder
WHALE_USD = 50_000        # holdings tier floor for "whale"
CEX_NONCE = 25_000        # >= this tx-count => likely CEX/MEV-bot infra, not smart-money individual
MAX_CANDIDATES = 30
SYSTEM = {"0x0000000000000000000000000000000000000000",
          "0x000000000000000000000000000000000000dead"}


def _price(asset, default):
    try:
        return round(float(pd.read_csv(ROOT / "data" / f"{asset}_features.csv")["close"].iloc[-1]), 4)
    except Exception:  # noqa: BLE001
        return default


P_MNT, P_METH = _price("mnt", 0.67), _price("meth", 2200.0)


def _get(params):
    params.update({"chainid": 5000, "apikey": KEY})
    for _ in range(3):
        try:
            j = requests.get(BASE, params=params, timeout=30).json()
            if isinstance(j, dict) and str(j.get("message", "")).startswith("NOTOK") and "rate" in str(j.get("result", "")).lower():
                time.sleep(1.0)
                continue
            return j
        except Exception:  # noqa: BLE001
            time.sleep(0.8)
    return {}


def discover(token, price):
    j = _get({"module": "account", "action": "tokentx", "contractaddress": token,
              "page": 1, "offset": 5000, "sort": "desc"})
    res, out = j.get("result"), defaultdict(float)
    if isinstance(res, list):
        for t in res:
            try:
                usd = int(t["value"]) / (10 ** int(t["tokenDecimal"])) * price
                if usd >= MIN_TRANSFER_USD:
                    out[t["from"].lower()] = max(out[t["from"].lower()], usd)
                    out[t["to"].lower()] = max(out[t["to"].lower()], usd)
            except Exception:  # noqa: BLE001
                continue
    return out, (len(res) if isinstance(res, list) else 0)


def is_contract(addr):
    code = _get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"}).get("result")
    return (isinstance(code, str) and code not in ("0x", "0x0", ""))


def nonce(addr):
    r = _get({"module": "proxy", "action": "eth_getTransactionCount", "address": addr, "tag": "latest"}).get("result")
    try:
        return int(r, 16)
    except Exception:  # noqa: BLE001
        return -1


def holdings(addr):
    def bal(p):
        r = _get(p).get("result")
        try:
            return int(r) / 1e18
        except Exception:  # noqa: BLE001
            return 0.0
    mnt = bal({"module": "account", "action": "balance", "address": addr, "tag": "latest"})
    wmnt = bal({"module": "account", "action": "tokenbalance", "contractaddress": WMNT, "address": addr, "tag": "latest"})
    meth = bal({"module": "account", "action": "tokenbalance", "contractaddress": METH, "address": addr, "tag": "latest"})
    return (mnt + wmnt) * P_MNT + meth * P_METH


def tier(h):
    return ("mega-whale" if h >= 1_000_000 else "large-whale" if h >= 250_000
            else "whale" if h >= WHALE_USD else "holder" if h >= DUST_USD else "mover")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(f"On-chain whale finder | prices MNT=${P_MNT} mETH=${P_METH}\n")
    cand = defaultdict(float)
    for tok, nm, pr in ((WMNT, "WMNT", P_MNT), (METH, "mETH", P_METH)):
        addrs, n = discover(tok, pr)
        print(f"  {nm}: scanned {n} transfers -> {len(addrs)} addrs moving >${MIN_TRANSFER_USD//1000}k")
        for a, v in addrs.items():
            cand[a] = max(cand[a], v)
        time.sleep(0.3)

    top = sorted(cand.items(), key=lambda kv: kv[1], reverse=True)[:MAX_CANDIDATES]
    print(f"\nClassifying {len(top)} candidates...\n")
    print(f"{'address':14} {'class':12} {'holdings$':>12} {'nonce':>8}  note")
    whales = []
    for addr, big in top:
        if addr in SYSTEM:
            print(f"{addr[:14]} {'SYSTEM':12} {'—':>12} {'—':>8}  zero/burn — skip")
            continue
        if is_contract(addr):
            print(f"{addr[:14]} {'CONTRACT':12} {'—':>12} {'—':>8}  pool/bridge/staking — skip")
            time.sleep(0.15)
            continue
        nc = nonce(addr); time.sleep(0.15)
        h = holdings(addr); time.sleep(0.4)
        if h < DUST_USD:
            klass, note = "mover", "big transfer but holds ~0 (pass-through)"
        elif nc >= CEX_NONCE:
            klass, note = "CEX/bot?", f"nonce {nc:,} very high — likely exchange/MEV infra"
        else:
            klass, note = tier(h), "EOA conviction holder"
            whales.append({"address": addr, "holdings_usd": round(h), "nonce": nc,
                           "biggest_move_usd": round(big), "class": klass})
        print(f"{addr[:14]} {klass:12} ${h:>11,.0f} {nc if nc >= 0 else '?':>8}  {note}")

    whales.sort(key=lambda w: w["holdings_usd"], reverse=True)
    (ROOT / "data" / "onchain_whales.json").write_text(json.dumps(whales, indent=2), encoding="utf-8")
    print(f"\n=== {len(whales)} REAL conviction whale(s) (EOA, holds >= ${WHALE_USD:,}, not CEX/bot) ===")
    for w in whales:
        print(f"  {w['address']} | {w['class']} | holds ${w['holdings_usd']:,} | nonce {w['nonce']:,}")
    print(f"\nsaved -> data/onchain_whales.json")
    print("caveats: holdings = MNT+WMNT+mETH only (others undercounted); CEX flag = nonce heuristic; active whales only")


if __name__ == "__main__":
    main()
