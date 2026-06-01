"""Enrich the whale watchlist with Etherscan v2 (Mantle) — the L3->L2 labeling upgrade.

GeckoTerminal finds active DEX *traders*; Etherscan tells us if each is a real wallet worth
following or just infra/noise:
  - eth_getCode  -> EOA (real wallet) vs CONTRACT (router / pool / bot) -> filter noise
  - balance      -> native MNT holdings -> a real WHALE holds a big bag, not just churns volume

This directly addresses the honest gap: "our 'whales' are unlabeled, could be market-makers/bots."

Run: python scripts/31_enrich_whales.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
KEY = env.get("MANTLESCAN_API_KEY") or env.get("ETHERSCAN_API_KEY") or ""
BASE = "https://api.etherscan.io/v2/api"
MNT_PRICE = 0.65  # rough USD for holdings estimate (Pyth spot ~0.65)


def _get(params: dict) -> dict:
    params.update({"chainid": 5000, "apikey": KEY})
    try:
        return requests.get(BASE, params=params, timeout=25).json()
    except Exception as e:  # noqa: BLE001
        return {"status": "0", "message": str(e)[:60]}


def is_eoa(addr: str) -> bool | None:
    j = _get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"})
    code = j.get("result")
    if not isinstance(code, str):
        return None
    return code in ("0x", "0x0", "")


def native_mnt(addr: str) -> float | None:
    j = _get({"module": "account", "action": "balance", "address": addr, "tag": "latest"})
    r = j.get("result")
    try:
        return int(r) / 1e18
    except Exception:  # noqa: BLE001
        return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    whales = json.loads((ROOT / "data" / "whale_watchlist.json").read_text(encoding="utf-8"))
    print(f"Enriching {len(whales)} watchlist whales via Etherscan v2 (Mantle)...\n")
    print(f"{'address':14} {'type':8} {'MNT held':>14} {'~USD':>12}  {'gecko bias':<13} {'vol_usd':>10}")
    eoa_n, contract_n = 0, 0
    for w in whales:
        addr = w.get("address", "")
        eoa = is_eoa(addr)
        time.sleep(0.25)
        bal = native_mnt(addr)
        time.sleep(0.25)
        typ = "EOA" if eoa else ("CONTRACT" if eoa is False else "?")
        eoa_n += 1 if eoa else 0
        contract_n += 1 if eoa is False else 0
        usd = (bal * MNT_PRICE) if bal is not None else None
        print(f"{addr[:14]} {typ:8} {bal if bal is not None else 0:>14,.2f} "
              f"{('$'+format(usd, ',.0f')) if usd is not None else 'n/a':>12}  "
              f"{str(w.get('direction_bias','?')):<13} ${w.get('total_volume_usd', 0):>9,.0f}")
    print(f"\nsummary: {eoa_n} EOA (real wallets) | {contract_n} CONTRACT (router/pool/bot -> noise) "
          f"| {len(whales)-eoa_n-contract_n} unknown")
    print("note: native MNT holdings only (ERC-20 mETH/WMNT bags not counted here); MNT~$0.65 est.")


if __name__ == "__main__":
    main()
