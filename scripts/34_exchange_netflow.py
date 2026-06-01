"""Exchange Net-Flow proxy (Etherscan v2 / Mantle) — a real, professional on-chain signal.

Thesis (CryptoQuant/Glassnode-style): tokens flowing INTO exchanges = users positioning to SELL
(bearish); flowing OUT = withdrawal to self-custody / accumulation (bullish).

Pipeline:
  1. fetch recent WMNT + mETH transfers
  2. identify HUB wallets = top-involved EOAs with very high nonce (>= CEX_NONCE) = CEX/MM hot wallets
     (e.g. the 2.47M-nonce wallet found earlier); contracts/pools excluded
  3. tally USD flow:  inflow  = transfers INTO a hub (from a non-hub)   -> sell pressure
                      outflow = transfers OUT of a hub (to a non-hub)   -> accumulation
  4. net = inflow - outflow ;  net inflow => bearish, net outflow => bullish
  5. append snapshot to data/exchange_netflow_log.jsonl (forward-log to VALIDATE over time)

HONEST CAVEATS (a pro states these):
  - Hubs are HEURISTIC (high-nonce EOAs = CEX *or* market-maker), NOT verified exchange labels.
    Rigorous netflow needs curated CEX address lists (paid/Nansen) -> feature-dev.
  - Window is the recent transfer batch (snapshot); the forward-log builds the real series.
  - Mantle ecosystem is CEX-light vs ETH mainnet, so signal may be thin.

Run: python scripts/34_exchange_netflow.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
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
CEX_NONCE = 25_000
TOP_HUB_CHECK = 50
KNOWN_HUBS = {"0x588846213a30fd36244e0ae0ebb2374516da836c"}  # confirmed 2.47M-nonce CEX/MM hot wallet
LOG = ROOT / "data" / "exchange_netflow_log.jsonl"


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
            return requests.get(BASE, params=params, timeout=30).json()
        except Exception:  # noqa: BLE001
            time.sleep(0.7)
    return {}


def fetch_transfers(token, price):
    j = _get({"module": "account", "action": "tokentx", "contractaddress": token,
              "page": 1, "offset": 5000, "sort": "desc"})
    res, out = j.get("result"), []
    if isinstance(res, list):
        for t in res:
            try:
                out.append((t["from"].lower(), t["to"].lower(),
                            int(t["value"]) / (10 ** int(t["tokenDecimal"])) * price))
            except Exception:  # noqa: BLE001
                continue
    return out


def is_contract(addr):
    code = _get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"}).get("result")
    return isinstance(code, str) and code not in ("0x", "0x0", "")


def nonce(addr):
    r = _get({"module": "proxy", "action": "eth_getTransactionCount", "address": addr, "tag": "latest"}).get("result")
    try:
        return int(r, 16)
    except Exception:  # noqa: BLE001
        return -1


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    transfers = fetch_transfers(WMNT, P_MNT) + fetch_transfers(METH, P_METH)
    print(f"fetched {len(transfers)} transfers (WMNT+mETH) | prices MNT=${P_MNT} mETH=${P_METH}")
    if not transfers:
        print("no transfers — abort.")
        return

    involve = defaultdict(float)
    for f, t, usd in transfers:
        involve[f] += usd
        involve[t] += usd
    top = sorted(involve.items(), key=lambda kv: kv[1], reverse=True)[:TOP_HUB_CHECK]

    print(f"\nidentifying hubs among top {TOP_HUB_CHECK} most-involved addresses (+ {len(KNOWN_HUBS)} seeded)...")
    hubs = {a: -1 for a in KNOWN_HUBS}
    for a in KNOWN_HUBS:
        print(f"  HUB {a[:16]} (seeded, confirmed CEX/MM)")
    for addr, vol in top:
        if addr in hubs:
            continue
        if is_contract(addr):
            time.sleep(0.15)
            continue
        nc = nonce(addr)
        time.sleep(0.2)
        if nc >= CEX_NONCE:
            hubs[addr] = nc
            print(f"  HUB {addr[:16]} nonce={nc:,} (CEX/MM hot wallet)")
    if not hubs:
        print("  no high-nonce EOA hubs found in this window -> no exchange-flow signal.")

    inflow = outflow = 0.0
    for f, t, usd in transfers:
        f_h, t_h = f in hubs, t in hubs
        if t_h and not f_h:
            inflow += usd      # into exchange  -> sell pressure
        elif f_h and not t_h:
            outflow += usd     # out of exchange -> accumulation
    net = inflow - outflow
    gross = inflow + outflow
    net_score = (net / gross) if gross > 0 else 0.0
    signal = ("BEARISH (net inflow to exchanges = sell pressure)" if net_score > 0.15
              else "BULLISH (net outflow from exchanges = accumulation)" if net_score < -0.15
              else "neutral")

    print(f"\n=== EXCHANGE NET-FLOW (proxy) ===")
    print(f"  hubs: {len(hubs)} | inflow ${inflow:,.0f} | outflow ${outflow:,.0f} | NET ${net:+,.0f} "
          f"(score {net_score:+.2f})")
    print(f"  signal: {signal}")

    snap = {"logged_utc": datetime.now(timezone.utc).isoformat(), "hubs": len(hubs),
            "inflow_usd": round(inflow), "outflow_usd": round(outflow), "net_usd": round(net),
            "net_score": round(net_score, 3), "mnt_price": P_MNT}
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap) + "\n")
    n_log = sum(1 for _ in LOG.open(encoding="utf-8"))
    print(f"\nsaved snapshot -> data/exchange_netflow_log.jsonl ({n_log} total)")
    print("caveat: hubs heuristic (high-nonce EOA = CEX/MM, not verified labels); validate via forward-log")


if __name__ == "__main__":
    main()
