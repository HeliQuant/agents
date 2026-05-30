"""FastAPI server exposing one HTTP endpoint per agent role.

Paperclip's HTTP adapter POSTs to these endpoints; we synchronously return the
result for short-running work. For long-running work (e.g. waiting for Allora
freshness + tx confirmation), we accept the run, return 202, and callback to
Paperclip with the final result.

Run locally:
    uvicorn firm.app:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from firm.config import settings
from firm.schemas import (
    AgentResult,
    JobEnvelope,
    ResearchInput,
    SignalInput,
    RiskInput,
    ExecutionInput,
    ReputationInput,
)

log = structlog.get_logger()

app = FastAPI(
    title="Mantle AI Trading Firm — Agent Workers",
    version="0.1.0",
    description="Python workers orchestrated by Paperclip control plane.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3100", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "chain_id": str(settings.chain_id)}


# ─── Agent endpoints (stub implementations — filled in Task #9 / #17 / #18) ─


@app.post("/agent/research")
async def research(envelope: JobEnvelope) -> AgentResult:
    from firm.agents.research import run as run_research

    inputs = ResearchInput(**envelope.inputs)
    log.info("research.invoked", run_id=envelope.run_id, pair=inputs.pair)
    output = await run_research(inputs)
    return AgentResult(status="succeeded", result=output.model_dump())


@app.post("/agent/signal")
async def signal(envelope: JobEnvelope) -> AgentResult:
    from firm.agents.signal import run as run_signal

    inputs = SignalInput(**envelope.inputs)
    log.info("signal.invoked", run_id=envelope.run_id, pair=inputs.pair)
    output = await run_signal(inputs)
    return AgentResult(status="succeeded", result=output.model_dump())


@app.post("/agent/risk")
async def risk(envelope: JobEnvelope) -> AgentResult:
    from firm.agents.risk import run as run_risk

    inputs = RiskInput(**envelope.inputs)
    log.info("risk.invoked", run_id=envelope.run_id, job_id=inputs.job_id)
    output = await run_risk(inputs)
    return AgentResult(status="succeeded", result=output.model_dump())


@app.post("/agent/execution")
async def execution(envelope: JobEnvelope) -> AgentResult:
    from firm.agents.execution import run as run_execution

    if settings.executor_private_key is None:
        raise HTTPException(status_code=503, detail="Executor key not configured")

    inputs = ExecutionInput(**envelope.inputs)
    log.info("execution.invoked", run_id=envelope.run_id, job_id=inputs.job_id)
    output = await run_execution(inputs)
    return AgentResult(status="succeeded", result=output.model_dump())


@app.post("/agent/reputation")
async def reputation(envelope: JobEnvelope) -> AgentResult:
    from firm.agents.reputation import run as run_reputation

    inputs = ReputationInput(**envelope.inputs)
    log.info("reputation.invoked", run_id=envelope.run_id, firm=inputs.firm_token_id)
    output = await run_reputation(inputs)
    return AgentResult(status="succeeded", result=output.model_dump())
