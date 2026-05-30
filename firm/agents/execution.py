"""Execution Agent — signs and submits a DEX trade tx via TradingVault on Mantle.

This is the ONLY agent that holds a private key. Its scope is intentionally narrow:
  - Receive {job_id, direction, size_token_in, min_amount_out}
  - Call vault.trade(...) with the executor signing key
  - Return tx hash + amount out

V1 stub returns a fake tx hash. V2 will:
  - Construct web3.py contract instance
  - Sign with eth_account
  - Submit + wait for receipt
  - Return real tx data
"""

from __future__ import annotations

import structlog

from firm.config import settings
from firm.schemas import ExecutionInput, ExecutionOutput, TradeDirection

log = structlog.get_logger()


async def run(inputs: ExecutionInput) -> ExecutionOutput:
    log.info(
        "execution.run",
        job_id=inputs.job_id,
        direction=inputs.direction.value,
        size=inputs.size_token_in,
    )

    if settings.trading_vault_address is None:
        raise RuntimeError(
            "TRADING_VAULT_ADDRESS not configured — set after Task #7 deployment"
        )

    # ─── V1 stub ──────────────────────────────────────────────────────
    # V2: web3.eth_account local signing + contract.functions.trade(...).build_transaction()
    return ExecutionOutput(
        tx_hash="0x" + "00" * 32,
        amount_out=inputs.min_amount_out,
        block_number=0,
    )
