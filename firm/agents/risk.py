"""Risk Agent — gates each trade signal. Veto if any limit would be breached.

Checks:
  - Signal direction is not HOLD
  - Position size respects risk_per_trade_bps of current vault principal balance
  - Max drawdown not exceeded
  - Stop-loss and take-profit prices respect ATR-based distances

V1 stub returns approved=true with mock sizes. V2 reads TradingVault state.
"""

from __future__ import annotations

import structlog

from firm.config import settings
from firm.schemas import RiskInput, RiskOutput, TradeDirection

log = structlog.get_logger()


async def run(inputs: RiskInput) -> RiskOutput:
    log.info("risk.run", job_id=inputs.job_id, direction=inputs.direction.value)

    if inputs.direction == TradeDirection.HOLD:
        return RiskOutput(
            approved=False,
            size_token_in="0",
            sl_token_out="0",
            tp_token_out="0",
            veto_reason="No trade signal (HOLD)",
        )

    # ─── V1 stub: derive size from a synthetic balance + ATR ──────────
    # V2 will: read vault.getJobBalance(job_id), fetch ATR from price feed.
    mock_balance_atomic = 1_000_000_000  # 1,000 USDC at 6 decimals
    size_atomic = (mock_balance_atomic * settings.risk_per_trade_bps) // 10_000
    sl_atomic = int(size_atomic * settings.atr_sl_multiplier)
    tp_atomic = int(size_atomic * settings.atr_tp_multiplier)

    return RiskOutput(
        approved=True,
        size_token_in=str(size_atomic),
        sl_token_out=str(sl_atomic),
        tp_token_out=str(tp_atomic),
        veto_reason=None,
    )
