"""Pydantic schemas for the Paperclip ↔ Python worker HTTP contract.

Paperclip's HTTP adapter POSTs to each agent's endpoint with a job envelope. We
respond with either:
  - 2xx + final result    (synchronous completion)
  - 202 + run_id          (async; we callback to Paperclip later)

Schemas here are minimal but typed strictly so a contract mismatch surfaces as a
422, not a silent miss.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TradeDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class JobEnvelope(BaseModel):
    """Common Paperclip job envelope."""

    model_config = ConfigDict(extra="allow")

    run_id: str = Field(description="Paperclip run id (use to callback)")
    issue_id: str | None = None
    agent: str = Field(description="Logical agent name (research, signal, ...)")
    inputs: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Synchronous result returned in the 2xx response body."""

    status: Literal["succeeded", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)
    token_usage: dict[str, int] | None = None
    error: str | None = None


# ─── Per-agent typed inputs/outputs (used inside the worker handlers) ──────


class ResearchInput(BaseModel):
    pair: str = "MNT/USDC"
    lookback_hours: int = 6


class ResearchOutput(BaseModel):
    pair: str
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    summary: str
    notable_events: list[str] = Field(default_factory=list)


class SignalInput(BaseModel):
    pair: str = "MNT/USDC"
    sentiment_score: float | None = None


class SignalOutput(BaseModel):
    pair: str
    direction: TradeDirection
    confidence: float = Field(ge=0.0, le=1.0)
    allora_value: float | None = None
    allora_confidence_bps: int | None = None
    ml_buy_prob: float | None = None
    ml_sell_prob: float | None = None
    ml_adx: float | None = None
    reasoning: str


class RiskInput(BaseModel):
    job_id: int
    direction: TradeDirection
    confidence: float
    pair: str = "MNT/USDC"


class RiskOutput(BaseModel):
    approved: bool
    size_token_in: str  # decimal string to preserve precision
    sl_token_out: str
    tp_token_out: str
    veto_reason: str | None = None


class ExecutionInput(BaseModel):
    job_id: int
    direction: TradeDirection
    size_token_in: str  # decimal string
    min_amount_out: str


class ExecutionOutput(BaseModel):
    tx_hash: str
    amount_out: str
    block_number: int


class ReputationInput(BaseModel):
    firm_token_id: int
    job_id: int
    pnl: int  # signed integer in token decimals
    volume: int
    current_balance: int


class ReputationOutput(BaseModel):
    tx_hash: str
