"""Is MNT-L1 exchange netflow trackable? (the one usable nugget from the DIY-Whale-Alert idea)
Check whether known Binance hot wallets on ETHEREUM (chainid 1) actually hold/move MNT (L1 token).
If yes → MNT exchange-netflow (multi-day aggregate, NOT naive single-tx) is a real free signal for MNT."""
import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

KEY = os.environ.get("MANTLESCAN_API_KEY") or "ARKZEP422CW6ASTDKJ2AESEGUVKPHI16RF"
BASE = "https://api.etherscan.io/v2/api"
MNT_L1 = "0x3c3a81e81dc49A522A592e7622A7E711c06bf354"
CEX = {
    "Binance 14 (0x28C6)": "0x28C6c06298d514Db089934071355E5743bf21d60",
    "Binance 20 (0xF977)": "0xF977814e90dA44bFA03b6295A0616a897441aceC",
    "Binance 15 (0x21a31)": "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549",
}


def get(params):
    params.update({"chainid": 1, "apikey": KEY})
    try:
        return requests.get(BASE, params=params, timeout=25).json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:60]}


print("=== MNT-L1 exchange-flow trackability (Ethereum chainid 1) ===\n")
for name, addr in CEX.items():
    jb = get({"module": "account", "action": "tokenbalance", "contractaddress": MNT_L1, "address": addr, "tag": "latest"})
    try:
        bal = int(jb.get("result", "0")) / 1e18
    except (ValueError, TypeError):
        bal = None
    jn = get({"module": "proxy", "action": "eth_getTransactionCount", "address": addr, "tag": "latest"})
    try:
        nonce = int(jn.get("result", "0x0"), 16)
    except (ValueError, TypeError):
        nonce = -1
    print(f"{name:24} MNT balance: {bal:,.0f}" if bal is not None else f"{name}: n/a", f"| nonce {nonce:,}")
    time.sleep(0.3)

# recent MNT transfers in/out of Binance 14 → is there real exchange flow?
print("\n=== recent MNT transfers in/out of Binance 14 (last 10) ===")
jt = get({"module": "account", "action": "tokentx", "contractaddress": MNT_L1,
          "address": CEX["Binance 14 (0x28C6)"], "page": 1, "offset": 10, "sort": "desc"})
res = jt.get("result", [])
if isinstance(res, list) and res:
    for t in res[:10]:
        val = int(t["value"]) / 1e18
        direction = "IN <-" if t["to"].lower() == CEX["Binance 14 (0x28C6)"].lower() else "OUT ->"
        print(f"  {direction} {val:>14,.0f} MNT  block {t['blockNumber']}")
else:
    print(f"  {str(res)[:120]}")
