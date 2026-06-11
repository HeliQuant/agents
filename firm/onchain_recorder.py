"""firm/onchain_recorder.py — Line-2 execution: anchor a decision on Mantle Sepolia (verifiable).

We deliberately do NOT auto-swap on a thin Mantle DEX (high slippage / liquidity risk). Instead we
ANCHOR an immutable, auditable record of each decision/ticket on-chain: a canonical SHA-256 hash of
the decision payload, written into a 0-value self-transaction's calldata on Mantle Sepolia. Anyone
can later verify the decision existed at that block — the honest, low-risk verifiability primitive
that fits the "firm of AI agents, recorded on-chain" thesis without gambling capital on a swap.

Modes:
  DRY (default)      build the canonical record + hash + the unsigned tx. No key/gas/network needed.
  SEND (send=True)   broadcast the anchor tx. Needs `pip install web3 eth-account`, EXECUTOR_PRIVATE_KEY
                     in env/.env, and a little testnet MNT for gas. Sandbox-first: Mantle Sepolia only.

The deployed TradingVault (0x3BbD…6424) exposes trade()/openJob() (real DEX execution) — wire those
as a separate, explicitly-opted-in execution path later; anchoring is the safe default.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANTLE_SEPOLIA_RPC = "https://rpc.sepolia.mantle.xyz"
CHAIN_ID = 5003  # Mantle Sepolia (sandbox)
TRADING_VAULT = "0x3BbD1f5e8733e901A8FdFf5cFA7E18e575896424"


def _env(key: str) -> str | None:
    v = os.environ.get(key)
    if v:
        return v
    for envfile in (ROOT / ".env", ROOT.parent / ".env"):  # agents/.env, then project-root .env (on-chain config)
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                for line in envfile.read_text(encoding=enc).splitlines():
                    line = line.lstrip("﻿").strip()
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
            except (UnicodeError, ValueError, FileNotFoundError):
                continue
    return None


def canonical_record(decision: dict, ticker: str, ts: str) -> dict:
    """Compact, deterministic snapshot of the decision/ticket to anchor on-chain."""
    tk = decision.get("trade_ticket") or {}
    return {
        "ts": ts, "ticker": ticker.upper(), "decision": decision.get("decision"),
        "direction": decision.get("direction"), "confidence": decision.get("confidence"),
        "entry": tk.get("entry"), "stop_loss": tk.get("stop_loss"),
        "tp1": (tk.get("take_profit") or [{}])[0].get("price"),
        "risk_pct": tk.get("risk_pct"), "mode": tk.get("mode"),
    }


def record_hash(rec: dict) -> str:
    """Canonical SHA-256 of the record (sorted keys, compact) -> 0x-prefixed 32-byte hex."""
    blob = json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
    return "0x" + hashlib.sha256(blob).hexdigest()


def anchor(decision: dict, ticker: str, ts: str, *, send: bool = False) -> dict:
    """Build (and optionally broadcast) an on-chain anchor of the decision. Always safe in DRY mode."""
    rec = canonical_record(decision, ticker, ts)
    h = record_hash(rec)
    out = {"chain": "mantle-sepolia", "chain_id": CHAIN_ID, "record": rec, "record_hash": h,
           "trading_vault": TRADING_VAULT, "sent": False}
    if not send:
        out["note"] = "DRY — record + hash built; pass send=True (+ EXECUTOR_PRIVATE_KEY + gas) to broadcast"
        return out
    out.update(_send_hash_tx(h))
    return out


def canonical_trade(trade: dict) -> dict:
    """Compact, deterministic snapshot of a RESOLVED campaign trade to seal on-chain (the outcome,
    not the paper fill mechanics)."""
    return {"id": trade.get("id"), "asset": trade.get("asset"), "dir": trade.get("dir"),
            "tier": trade.get("tier"), "entry": trade.get("entry"), "exit": trade.get("exit"),
            "exit_reason": trade.get("exit_reason"), "net_pct": trade.get("net_pct"),
            "pnl_usd": trade.get("pnl_usd"), "ts": trade.get("utc_close")}


def batch_anchor(records: list[dict]) -> list[dict]:
    """Seal a batch of records on-chain in ONE nonce-managed sweep (no wait for receipts — fast). Each
    record → a 0-value self-send carrying its SHA-256 in calldata. Returns [{id, tx_hash, explorer}] for
    those sent. Best-effort: returns [] on any setup failure (web3 missing / no key / RPC down), and
    stops at the first send error to avoid a nonce gap — callers must treat anchoring as non-critical."""
    if not records:
        return []
    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError:
        return []
    pk = _env("EXECUTOR_PRIVATE_KEY") or _env("DEPLOYER_PRIVATE_KEY")
    if not pk:
        return []
    rpc = _env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
        if not w3.is_connected():
            return []
        acct = Account.from_key(pk)
        nonce = w3.eth.get_transaction_count(acct.address)
        gp = w3.eth.gas_price
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for rec in records:
        try:
            h = record_hash(rec)
            tx = {"chainId": CHAIN_ID, "from": acct.address, "to": acct.address, "value": 0,
                  "nonce": nonce, "gas": 30000, "gasPrice": gp, "data": h}
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
            txh = w3.eth.send_raw_transaction(raw).hex()
            txh = txh if txh.startswith("0x") else "0x" + txh
            out.append({"id": rec.get("id"), "tx_hash": txh,
                        "explorer": f"https://sepolia.mantlescan.xyz/tx/{txh}"})
            nonce += 1
        except Exception:  # noqa: BLE001
            break  # stop on first failure — earlier sends stand, no nonce gap
    return out


def _send_hash_tx(h: str) -> dict:
    """Broadcast a 0-value self-tx carrying hash `h` as calldata on Mantle Sepolia (the proven, safe
    anchoring primitive). Returns {sent, tx_hash, from_address, explorer} or {error}. Testnet only."""
    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError:
        return {"error": "pip install web3 eth-account to broadcast"}
    pk = _env("EXECUTOR_PRIVATE_KEY") or _env("DEPLOYER_PRIVATE_KEY")  # testnet: deployer doubles as executor
    if not pk:
        return {"error": "set EXECUTOR_PRIVATE_KEY (or DEPLOYER_PRIVATE_KEY) in env/.env (testnet only)"}
    rpc = _env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        return {"error": f"cannot reach RPC {rpc}"}
    acct = Account.from_key(pk)
    tx = {"chainId": CHAIN_ID, "from": acct.address, "to": acct.address, "value": 0,
          "nonce": w3.eth.get_transaction_count(acct.address),
          "gas": 30000, "gasPrice": w3.eth.gas_price, "data": h}
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    txh = w3.eth.send_raw_transaction(raw)
    return {"sent": True, "tx_hash": txh.hex(), "from_address": acct.address,
            "explorer": f"https://sepolia.mantlescan.xyz/tx/{txh.hex()}"}


def canonical_validation_record(edge: dict, ts: str) -> dict:
    """Deterministic snapshot of a VALIDATED/GRADUATED edge — auditable proof it cleared HeliQuant's
    cost-aware OOS gate on real data at this time. References the firm's ERC-8004 identity (tokenId)."""
    return {
        "type": "edge_validation", "ts": ts, "asset": str(edge.get("asset", "")).upper(),
        "edge": edge.get("edge"), "validated": bool(edge.get("validated")),
        "p_win": edge.get("p_win"), "payoff_b": edge.get("payoff_b"),
        "sample_n": edge.get("sample_n"), "oos_roi_pct": edge.get("oos_roi_pct"),
        "firm_token_id": _env("FIRM_TOKEN_ID"),
    }


def anchor_validation(edge: dict, ts: str, *, send: bool = False) -> dict:
    """Build (and optionally broadcast) an on-chain anchor of an edge-validation event. DRY-safe.
    The hash is anchored in a Mantle Sepolia tx's calldata — auditable on Mantlescan; not a claim."""
    rec = canonical_validation_record(edge, ts)
    h = record_hash(rec)
    out = {"chain": "mantle-sepolia", "chain_id": CHAIN_ID, "kind": "edge_validation",
           "record": rec, "record_hash": h, "sent": False}
    if not send:
        out["note"] = "DRY — validation record + hash built; pass send=True (+ key + gas) to broadcast"
        return out
    out.update(_send_hash_tx(h))
    return out


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    # Honest demo record: MNT's REAL disciplined stance (ranging, no validated edge -> ABSTAIN).
    sample = {"decision": "ABSTAIN", "direction": "NONE", "confidence": "low",
              "reasoning": "MNT ranging; no OOS-validated edge + R:R<2 gate -> disciplined abstain",
              "trade_ticket": None}
    print(json.dumps(anchor(sample, "MNT", "2026-06-02 manual-demo", send="--send" in sys.argv), indent=2))
