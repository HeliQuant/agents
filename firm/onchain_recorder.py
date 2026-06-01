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
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in (ROOT / ".env").read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
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
    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError:
        out["error"] = "pip install web3 eth-account to broadcast"
        return out
    pk = _env("EXECUTOR_PRIVATE_KEY")
    if not pk:
        out["error"] = "set EXECUTOR_PRIVATE_KEY in env/.env to broadcast (testnet wallet only)"
        return out
    rpc = _env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        out["error"] = f"cannot reach RPC {rpc}"
        return out
    acct = Account.from_key(pk)
    tx = {"chainId": CHAIN_ID, "from": acct.address, "to": acct.address, "value": 0,
          "nonce": w3.eth.get_transaction_count(acct.address),
          "gas": 30000, "gasPrice": w3.eth.gas_price, "data": h}
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    txh = w3.eth.send_raw_transaction(raw)
    out.update(sent=True, tx_hash=txh.hex(), from_address=acct.address,
               explorer=f"https://sepolia.mantlescan.xyz/tx/{txh.hex()}")
    return out


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sample = {"decision": "ENTER", "direction": "LONG", "confidence": "high",
              "trade_ticket": {"entry": 0.672, "stop_loss": 0.655,
                               "take_profit": [{"price": 0.70}], "risk_pct": 3.0, "mode": "AGGRESSIVE"}}
    send = "--send" in sys.argv
    print(json.dumps(anchor(sample, "MNT", "2026-06-02 12:00", send=send), indent=2))
