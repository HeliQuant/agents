"""scripts/96_register_new_desks.py — register the 3 newest desks on-chain as firm children.

Carry, Mantle Fundamentals, and TimesFM Vol/Risk were wired into the PM synthesis 2026-06-20
(analytical-only). This registers them as ERC-8004 child agents under the existing firm (tokenId 2),
the same way scripts/94 registered the original 9 — appending tokenIds 12/13/14 to
data/agent_registry.json. Idempotent: skips any desk already in the registry. Real Mantle Sepolia txs.

Run:  .venv/Scripts/python.exe scripts/96_register_new_desks.py --dry   # verify, no tx
      .venv/Scripts/python.exe scripts/96_register_new_desks.py         # send the register txs
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ABI_DIR = ROOT / "abi"  # bundled ABIs (present locally + on Railway)
REGISTRY_JSON = ROOT / "data" / "agent_registry.json"
ENDPOINT = "https://agents-production-5a3d.up.railway.app"
KIND = {"Firm": 0, "Research": 1, "Signal": 2, "Risk": 3, "Execution": 4, "Reputation": 5, "Custom": 6}

# the 3 desks added to build_desks 2026-06-20 → (name, kind, role, is_ml)
NEW_DESKS = [
    ("Carry", "Signal", "delta-neutral funding-carry yield (market-neutral; HYPE/SUI OOS-validated)", False),
    ("Mantle Fundamentals", "Research", "DeFiLlama Mantle chain TVL / fees / staking — ecosystem risk-on/off", False),
    ("TimesFM Vol/Risk", "Risk", "TimesFM 2.5 next-day realized-vol forecast (beats HAR-RV, DM p<0.05) -> vol-targeting", False),
]


def _abi(name: str) -> list:
    return json.loads((ABI_DIR / f"{name}.json").read_text())


def main() -> None:
    dry = "--dry" in sys.argv
    from eth_account import Account
    from web3 import Web3

    from firm.onchain_recorder import CHAIN_ID, MANTLE_SEPOLIA_RPC, _env

    reg = json.loads(REGISTRY_JSON.read_text())
    firm_id = int(reg.get("firm", {}).get("tokenId") or 0)
    assert firm_id, "firm not registered — run scripts/94 first"
    desks = reg.setdefault("desks", {})
    todo = [d for d in NEW_DESKS if d[0] not in desks]

    pk = _env("EXECUTOR_PRIVATE_KEY") or _env("DEPLOYER_PRIVATE_KEY")
    assert pk, "set DEPLOYER_PRIVATE_KEY in the root .env"
    w3 = Web3(Web3.HTTPProvider(_env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC, request_kwargs={"timeout": 30}))
    assert w3.is_connected(), "RPC unreachable"
    acct = Account.from_key(pk)
    ident = w3.eth.contract(address=Web3.to_checksum_address(reg["identity"]), abi=_abi("IdentityRegistry"))
    bal = w3.from_wei(w3.eth.get_balance(acct.address), "ether")
    print(f"wallet {acct.address} · bal {bal:.3f} MNT · firm tok {firm_id} · "
          f"existing desks {len(desks)} · to register: {[d[0] for d in todo]}")

    if not todo:
        print("all 3 already registered — nothing to do")
        return
    if dry:
        for n, k, role, ml in todo:
            print(f"  PLAN register {n:20} kind={k:8} parent={firm_id}")
        print("(dry run — no tx sent)")
        return

    nonce = w3.eth.get_transaction_count(acct.address)
    gp = w3.eth.gas_price
    for name, kind, role, ml in todo:
        meta = {"name": name, "role": role, "ml": ml, "firm": "HeliQuant"}
        fn = ident.functions.register(KIND[kind], firm_id, json.dumps(meta, separators=(",", ":")), ENDPOINT)
        tx = fn.build_transaction({"chainId": CHAIN_ID, "from": acct.address, "nonce": nonce,
                                   "gas": 380000, "gasPrice": gp})
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        txh = w3.eth.send_raw_transaction(raw)
        rc = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
        ev = ident.events.AgentRegistered().process_receipt(rc)
        tid = int(ev[0]["args"]["tokenId"])
        desks[name] = {"tokenId": tid, "kind": kind, "ml": ml}
        nonce += 1
        thx = txh.hex() if not isinstance(txh, str) else txh
        print(f"  registered {name:20} tokenId {tid}  tx {thx if str(thx).startswith('0x') else '0x' + str(thx)}")

    REGISTRY_JSON.write_text(json.dumps(reg, indent=2))
    print(f"saved {REGISTRY_JSON} — firm + {len(desks)} desks now on-chain")
    print("verify:", f"https://sepolia.mantlescan.xyz/address/{reg['identity']}")


if __name__ == "__main__":
    main()
