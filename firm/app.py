"""FastAPI server exposing one HTTP endpoint per agent role.

Paperclip's HTTP adapter POSTs to these endpoints; we synchronously return the
result for short-running work. For long-running work (e.g. waiting for Allora
freshness + tx confirmation), we accept the run, return 202, and callback to
Paperclip with the final result.

Run locally:
    uvicorn firm.app:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

from typing import Any

import anyio
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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

# How long a single live org decision pass may run before we give up (LLM calls
# across 7 desks + a 2-round debate + PM are slow; on free Groq this is ~30-90s).
ORG_RUN_TIMEOUT_SECONDS = 180.0

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


# ─── Live "run the firm" endpoint (frontend triggers a real org decision) ───


class RunRequest(BaseModel):
    """Trigger one full autonomous org decision pass for a single asset."""

    ticker: str = Field(default="MNT", description="Asset to analyze (e.g. MNT, mETH, BTC)")


@app.post("/run")
async def run(req: RunRequest) -> dict[str, Any]:
    """Run HeliQuant's full autonomous org loop for `ticker` and return the decision.

    Mirrors `scripts/24_autonomous_org.py`: calls `organization.run_organization(ticker)`
    exactly (OPERATIONS -> 7 ANALYSIS desks -> bull/bear DEBATE -> PM DECISION -> gated
    trade ticket). The org makes many blocking LLM calls, so we offload it to a worker
    thread and bound it with a timeout — never blocking the event loop, never 500-crashing
    the frontend on an LLM/network hiccup.
    """
    ticker = (req.ticker or "MNT").strip().upper()
    log.info("run.invoked", ticker=ticker)

    try:
        with anyio.fail_after(ORG_RUN_TIMEOUT_SECONDS):
            result = await anyio.to_thread.run_sync(_run_org_blocking, ticker)
    except TimeoutError:
        log.warning("run.timeout", ticker=ticker, timeout_s=ORG_RUN_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=503,
            detail=f"org run for {ticker} exceeded {int(ORG_RUN_TIMEOUT_SECONDS)}s (LLM/provider slow or unavailable)",
        )
    except Exception as exc:  # noqa: BLE001 — surface as graceful JSON, not a 500 crash
        log.error("run.failed", ticker=ticker, error=str(exc))
        return {"ok": False, "ticker": ticker, "error": str(exc)[:300]}

    decision = result.get("decision", {}) if isinstance(result, dict) else {}
    log.info("run.completed", ticker=ticker, decision=decision.get("decision"),
             direction=decision.get("direction"))

    # Surface the headline decision fields the frontend cares about, then include the
    # FULL org result (desks/analysts/debate/operations/provider) verbatim — we serialize
    # exactly what the org produced and never fabricate fields.
    return {
        "ok": True,
        "ticker": ticker,
        "decision": decision.get("decision"),
        "direction": decision.get("direction"),
        "confidence": decision.get("confidence"),
        "reasoning": decision.get("reasoning"),
        "desk_consensus": decision.get("desk_consensus"),
        "ticket": decision.get("trade_ticket"),
        "ticket_note": decision.get("ticket_note"),
        "desks": result.get("desks") if isinstance(result, dict) else None,
        "analysts": result.get("analysts") if isinstance(result, dict) else None,
        "debate": result.get("debate") if isinstance(result, dict) else None,
        "operations": result.get("operations") if isinstance(result, dict) else None,
        "provider": result.get("provider") if isinstance(result, dict) else None,
        "result": result,  # full verbatim org output (same shape scripts/24 writes to disk)
    }


def _run_org_blocking(ticker: str) -> dict[str, Any]:
    """Blocking org call, executed in a worker thread. Imported lazily so the heavy
    org/ML/data stack only loads when /run is actually hit (matches the per-agent
    lazy-import pattern used by the handlers below)."""
    from firm.organization import run_organization

    return run_organization(ticker, verbose=False)


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
