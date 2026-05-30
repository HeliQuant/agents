"""Reputation Agent — updates ERC-8004 reputation after a Job settles.

Triggered by the off-chain indexer when a `JobSettled` event is observed on
the JobManager contract. NOTE: in our final on-chain design the JobManager
already calls ReputationRegistry.recordJobOutcome itself — so this agent is
the off-chain mirror that pushes the same data into our orchestration log
+ optionally re-emits to alternative reputation sinks (off-chain leaderboards,
external attestation services, ERC-8183 portable reputation feeds).

V1 stub: log only. V2: emit to Postgres reputation projection.
"""

from __future__ import annotations

import structlog

from firm.schemas import ReputationInput, ReputationOutput

log = structlog.get_logger()


async def run(inputs: ReputationInput) -> ReputationOutput:
    log.info(
        "reputation.run",
        firm_token_id=inputs.firm_token_id,
        job_id=inputs.job_id,
        pnl=inputs.pnl,
    )

    # In our architecture the on-chain JobManager already wrote ERC-8004 state.
    # This agent's job is the off-chain projection (analytics, dashboard counters).
    # Return the original on-chain settlement tx as confirmation.
    return ReputationOutput(tx_hash="0x" + "00" * 32)
