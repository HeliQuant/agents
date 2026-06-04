"""Verify the deep-research Mantle addresses on-chain (chainid 5000) before trusting them.
Resolve mETH (DexScreener 0xcDA86A vs research 0x5fc6fe) + confirm bridge/CEX hold Mantle assets."""
import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

KEY = os.environ.get("MANTLESCAN_API_KEY") or os.environ.get("MANTLESCAN_API_KEY", "")
BASE = "https://api.etherscan.io/v2/api"

TOKENS = {  # token contracts to confirm (tokensupply + getcode)
    "mETH (DexScreener-traded)": "0xcDA86A272531e8640cD7F1a92c01839911B90bb0",
    "mETH (research-claimed)": "0x5fc6fe32871642Ea04E0651F54BE80546cF2173b",
    "cmETH": "0xE6829d9a7eE3040e1276Fa75293Bde931859e8fA",
}
ACTORS = {  # bridge / CEX — confirm contract + Mantle-asset holdings
    "L2StandardBridge 0x4200..10": "0x4200000000000000000000000000000000000010",
    "Binance HW20": "0xF977814e90dA44bFA03b6295A0616a897441aceC",
    "Bybit HW4": "0xee5b5b923ffce93a870b3104b7ca09c3db80047a",
}
HOLD_CHECK = {"mETH": "0xcDA86A272531e8640cD7F1a92c01839911B90bb0",
              "WMNT": "0x78c1b0C915c4FAA5FffA6CAbf0219DA63d7f4cb8",
              "USDe": "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34"}


def get(params):
    params.update({"chainid": 5000, "apikey": KEY})
    try:
        return requests.get(BASE, params=params, timeout=25).json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:60]}


def is_contract(addr):
    code = get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"}).get("result", "0x")
    return code not in ("0x", "", None)


def nonce(addr):
    try:
        return int(get({"module": "proxy", "action": "eth_getTransactionCount", "address": addr, "tag": "latest"}).get("result", "0x0"), 16)
    except (ValueError, TypeError):
        return -1


print("=== TOKEN CONTRACTS (tokensupply + type) ===")
for name, addr in TOKENS.items():
    j = get({"module": "stats", "action": "tokensupply", "contractaddress": addr})
    res = j.get("result", "")
    try:
        sup = int(res) / 1e18
        supct = f"{sup:,.0f}"
    except (ValueError, TypeError):
        supct = f"INVALID ({str(res)[:40]})"
    print(f"  {name:28} supply={supct:>18}  type={'contract' if is_contract(addr) else 'EOA/none'}")
    time.sleep(0.3)

print("\n=== ACTORS (bridge/CEX: type, nonce, Mantle-asset holdings) ===")
for name, addr in ACTORS.items():
    ctype = "contract" if is_contract(addr) else "EOA"
    n = nonce(addr)
    holds = []
    for sym, taddr in HOLD_CHECK.items():
        jb = get({"module": "account", "action": "tokenbalance", "contractaddress": taddr, "address": addr, "tag": "latest"})
        try:
            bal = int(jb.get("result", "0")) / 1e18
        except (ValueError, TypeError):
            bal = 0.0
        if bal > 0.01:
            holds.append(f"{sym} {bal:,.1f}")
        time.sleep(0.25)
    print(f"  {name:28} {ctype:9} nonce={n:>10}  holds: {', '.join(holds) if holds else '(none of mETH/WMNT/USDe)'}")
