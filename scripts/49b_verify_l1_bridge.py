"""Is the Mantle L1 bridge (escrow) + mETH staking trackable on Ethereum (chainid 1)?
The L1 bridge escrows tokens bridged TO Mantle → its MNT balance = capital committed to Mantle.
mETH staking holds staked ETH = conviction. This confirms the MASTER contract-flow source (L1)."""
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
MNT_L1 = "0x3c3a81e81dc49A522A592e7622A7E711c06bf354"
BRIDGE_L1 = "0x95fc37a27a2f68e3a647cdc081f0a89bb47c3012"
METH_STAKING_L1 = "0xe3cBd06D7dadB3F4e6557bAb7EdD924CD1489E8f"
METH_L1 = "0xd5F7838F5C461fefF7FE49ea5ebaF7728bB0ADfa"


def get(params):
    params.update({"chainid": 1, "apikey": KEY})
    try:
        return requests.get(BASE, params=params, timeout=25).json()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:60]}


def code_type(addr):
    return "contract" if get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"}).get("result", "0x") not in ("0x", "", None) else "EOA"


def tok_bal(token, addr):
    j = get({"module": "account", "action": "tokenbalance", "contractaddress": token, "address": addr, "tag": "latest"})
    try:
        return int(j.get("result", "0")) / 1e18
    except (ValueError, TypeError):
        return None


def native_bal(addr):
    j = get({"module": "account", "action": "balance", "address": addr, "tag": "latest"})
    try:
        return int(j.get("result", "0")) / 1e18
    except (ValueError, TypeError):
        return None


print("=== Mantle L1 bridge + staking on Ethereum (chainid 1) ===\n")
print(f"MNT L1 token: supply check ...")
sj = get({"module": "stats", "action": "tokensupply", "contractaddress": MNT_L1})
try:
    print(f"  MNT supply: {int(sj.get('result','0'))/1e18:,.0f}")
except (ValueError, TypeError):
    print(f"  MNT supply: invalid ({str(sj.get('result'))[:40]})")
time.sleep(0.3)

print(f"\nL1 Bridge {BRIDGE_L1[:10]}.. ({code_type(BRIDGE_L1)}):")
time.sleep(0.3)
mnt_escrow = tok_bal(MNT_L1, BRIDGE_L1)
print(f"  MNT escrowed: {mnt_escrow:,.0f}" if mnt_escrow is not None else "  MNT escrow: n/a")
print(f"  (this = capital bridged to Mantle; track MNT Transfers in/out of here = net bridge flow)")
time.sleep(0.3)

print(f"\nmETH Staking {METH_STAKING_L1[:10]}.. ({code_type(METH_STAKING_L1)}):")
time.sleep(0.3)
eth_staked = native_bal(METH_STAKING_L1)
print(f"  native ETH held: {eth_staked:,.2f}" if eth_staked is not None else "  ETH: n/a")
sj2 = get({"module": "stats", "action": "tokensupply", "contractaddress": METH_L1})
try:
    print(f"  mETH L1 supply: {int(sj2.get('result','0'))/1e18:,.0f} (net stake = supply trend / Transfers)")
except (ValueError, TypeError):
    print(f"  mETH L1 supply: invalid")
