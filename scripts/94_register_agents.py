"""scripts/94_register_agents.py — register HeliQuant's agents on-chain (ERC-8004 IdentityRegistry).

Registers the FIRM (kind=Firm) then each of the 9 desks as child agents (parent=firm), and authorises
the firm wallet to write reputation + validation credentials. Idempotent: if data/agent_registry.json
already has token IDs it skips registration (so re-runs only top up authorisation). Real Mantle Sepolia
txs — verifiable on Mantlescan.

The Regime/Technical desk IS the ML (XGBoost regime classifier, 82.6% OOS) — tagged ml:true. The
disproven directional xgb_mnt is deliberately NOT registered (it produces no live output).

Run:  .venv/Scripts/python.exe scripts/94_register_agents.py            # register + authorise
      .venv/Scripts/python.exe scripts/94_register_agents.py --dry      # print plan only
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT.parent / "contracts" / "out"
REGISTRY_JSON = ROOT / "data" / "agent_registry.json"
ENDPOINT = "https://agents-production-5a3d.up.railway.app"

# deployed (Mantle Sepolia, chainid 5003)
IDENTITY = "0x0fae6342195fdC0007b94fb3293bF56463C55ff3"
REPUTATION = "0x5a18F8D33d551666233701025754274DCA9b2929"
VALIDATION = "0x8e55E41dC9a93E30AAf580dba0b3Ee6b34e14A1b"

KIND = {"Firm": 0, "Research": 1, "Signal": 2, "Risk": 3, "Execution": 4, "Reputation": 5, "Custom": 6}

# 9 desks → (kind, role, is_ml). metadataURI carries the real name/role; the kind is the ERC-8004 category.
DESKS = [
    ("Regime/Technical", "Signal", "XGBoost regime classifier (82.6% OOS) + trend strength + dynamic R:R", True),
    ("Macro (Allora)", "Signal", "Allora decentralised-AI BTC/ETH 8h price prediction", False),
    ("On-chain/Risk", "Risk", "whale flow + watchlist health + GoPlus token safety", False),
    ("Research", "Research", "external macro: Fear&Greed + global market via public APIs", False),
    ("Smart-Money Flow", "Signal", "Mantle DEX flow + mETH staking / majors perp + Nansen netflow", False),
    ("Smart-Social", "Research", "Elfa narrative/mindshare — smart accounts, not retail", False),
    ("OI-Contrarian", "Signal", "the OOS-validated edge — fade perp open-interest extremes", False),
    ("flow-intel", "Signal", "self-learning flow-signal validation (OI/funding/order-flow/momentum)", False),
    ("whale", "Signal", "Hyperliquid per-asset whale stance tracker", False),
]


def _abi(name: str) -> list:
    return json.loads((OUT / f"{name}.sol" / f"{name}.json").read_text())["abi"]


def main() -> None:
    dry = "--dry" in sys.argv
    from eth_account import Account
    from web3 import Web3

    from firm.onchain_recorder import CHAIN_ID, MANTLE_SEPOLIA_RPC, _env

    pk = _env("EXECUTOR_PRIVATE_KEY") or _env("DEPLOYER_PRIVATE_KEY")
    assert pk, "set EXECUTOR_PRIVATE_KEY / DEPLOYER_PRIVATE_KEY"
    w3 = Web3(Web3.HTTPProvider(_env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC, request_kwargs={"timeout": 30}))
    assert w3.is_connected(), "RPC unreachable"
    acct = Account.from_key(pk)
    ident = w3.eth.contract(address=Web3.to_checksum_address(IDENTITY), abi=_abi("IdentityRegistry"))
    rep = w3.eth.contract(address=Web3.to_checksum_address(REPUTATION), abi=_abi("ReputationRegistry"))
    val = w3.eth.contract(address=Web3.to_checksum_address(VALIDATION), abi=_abi("ValidationRegistry"))

    existing = json.loads(REGISTRY_JSON.read_text()) if REGISTRY_JSON.exists() else {}
    print(f"wallet {acct.address} · bal {w3.from_wei(w3.eth.get_balance(acct.address), 'ether'):.2f} MNT · "
          f"already-registered: {bool(existing.get('firm'))}")
    if dry:
        print("PLAN: register firm + 9 desks + authorise wallet (submitter+reporter). agents:")
        for n, k, role, ml in DESKS:
            print(f"  - {n:18} kind={k:8} ml={ml}  {role[:50]}")
        return

    nonce = w3.eth.get_transaction_count(acct.address)
    gp = w3.eth.gas_price

    def send(fn, gas: int) -> dict:
        nonlocal nonce
        tx = fn.build_transaction({"chainId": CHAIN_ID, "from": acct.address, "nonce": nonce,
                                   "gas": gas, "gasPrice": gp})
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        txh = w3.eth.send_raw_transaction(raw)
        nonce += 1
        return w3.eth.wait_for_transaction_receipt(txh, timeout=120)

    def register(kind: str, parent: int, meta: dict, gas: int = 380000) -> int:
        rc = send(ident.functions.register(KIND[kind], parent, json.dumps(meta, separators=(",", ":")), ENDPOINT), gas)
        ev = ident.events.AgentRegistered().process_receipt(rc)
        return int(ev[0]["args"]["tokenId"])

    reg = existing or {}
    if not reg.get("firm"):
        fid = register("Firm", 0, {"name": "HeliQuant", "role": "autonomous multi-desk AI trading firm", "kind": "Firm"})
        print(f"  FIRM registered · tokenId {fid}")
        desks = {}
        for name, kind, role, ml in DESKS:
            tid = register(kind, fid, {"name": name, "role": role, "ml": ml, "firm": "HeliQuant"})
            desks[name] = {"tokenId": tid, "kind": kind, "ml": ml}
            print(f"  desk {name:18} tokenId {tid}")
        reg = {"firm": {"tokenId": fid}, "desks": desks, "identity": IDENTITY,
               "reputation": REPUTATION, "validation": VALIDATION, "explorer": "https://sepolia.mantlescan.xyz"}
        REGISTRY_JSON.write_text(json.dumps(reg, indent=2))
        print(f"  saved {REGISTRY_JSON}")
    else:
        print("  identities already registered — skipping (idempotent)")

    # authorise the firm wallet to write reputation + credentials (owner-only; wallet is the deployer/owner)
    for label, fn in (("reputation reporter", rep.functions.setAuthorizedReporter(acct.address, True)),
                      ("validation submitter", val.functions.setAuthorizedSubmitter(acct.address, True))):
        try:
            send(fn, 80000)
            print(f"  authorised: {label}")
        except Exception as e:  # noqa: BLE001
            print(f"  authorise {label} skipped ({str(e)[:60]})")

    print("DONE. Verify on Mantlescan:", f"https://sepolia.mantlescan.xyz/address/{IDENTITY}")


if __name__ == "__main__":
    main()
