"""firm/agent_onchain.py — seal each desk's OUTPUT on-chain (ERC-8004 ValidationRegistry).

Every org cycle, each desk that produced an analysis gets its output hashed and written as a
ValidationCredential against its registered agent tokenId — "this agent produced this output", sealed
on Mantle. Best-effort, nonce-managed, no wait for receipts (fast). Gated by AGENT_ONCHAIN (default
ON; set 0 to disable). Reputation accrues separately as outcomes resolve.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data" / "agent_registry.json"
ABI_DIR = ROOT / "abi"  # bundled into the agents deploy (contracts/out is a sibling, absent on Railway)
PROOF_ATTESTED = 1  # ProofKind.AttestedByValidator


def _registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except (ValueError, OSError):
        return {}


def _proof_hash(obj: dict) -> bytes:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).digest()


def record_outputs(analysts: dict, asset: str, job_id: int) -> list[dict]:
    """recordCredential per desk in `analysts` that maps to a registered agent — seals its output hash
    on-chain. Returns [{desk, tokenId, tx}] for those sealed. Best-effort: [] on any setup failure."""
    if os.environ.get("AGENT_ONCHAIN", "1") == "0" or not analysts:
        return []
    reg = _registry()
    desks = reg.get("desks") or {}
    if not desks or not reg.get("validation"):
        return []
    try:
        from eth_account import Account
        from web3 import Web3

        from firm.onchain_recorder import CHAIN_ID, MANTLE_SEPOLIA_RPC, _env
    except ImportError:
        return []
    pk = _env("EXECUTOR_PRIVATE_KEY") or _env("DEPLOYER_PRIVATE_KEY")
    if not pk:
        return []
    try:
        w3 = Web3(Web3.HTTPProvider(_env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC, request_kwargs={"timeout": 12}))
        if not w3.is_connected():
            return []
        abi = json.loads((ABI_DIR / "ValidationRegistry.json").read_text())
        c = w3.eth.contract(address=Web3.to_checksum_address(reg["validation"]), abi=abi)
        acct = Account.from_key(pk)
        nonce = w3.eth.get_transaction_count(acct.address)
        gp = w3.eth.gas_price
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for name, output in analysts.items():
        d = desks.get(name)
        if not d:
            continue
        try:
            ph = _proof_hash({"asset": asset, "desk": name, "output": output})
            fn = c.functions.recordCredential(int(d["tokenId"]), int(job_id), PROOF_ATTESTED, ph, acct.address)
            tx = fn.build_transaction({"chainId": CHAIN_ID, "from": acct.address, "nonce": nonce,
                                       "gas": 220000, "gasPrice": gp})
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            txh = w3.eth.send_raw_transaction(raw).hex()
            out.append({"desk": name, "tokenId": d["tokenId"], "tx": txh if txh.startswith("0x") else "0x" + txh})
            nonce += 1
        except Exception:  # noqa: BLE001
            break  # stop on first failure — no nonce gap
    return out
