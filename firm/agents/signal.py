"""Signal Agent — REGIME + MULTI-SOURCE INTELLIGENCE (post-multi-source pivot).

3-layer decision architecture:

  Layer 1 — REGIME (when to act)
    1a) Deterministic rule detects current regime from trailing indicators
    1b) XGBoost forecasts regime at t+4h (confidence gate >= 0.65)
    1c) Route to the rule-based strategy matching the regime

  Layer 2 — INTELLIGENCE (vote of multiple independent sources)
    2a) Allora Network decentralised AI macro signal (BTC 8h)
    2b) On-chain whale flow tracker (Mantle DEX swap events > $10K)
    2c) Price/volume/trending sentiment proxy (CoinGecko free API)
    2d) Rugpull screener (GoPlus Security API) — HARD VETO when unsafe

  Layer 3 — RECONCILIATION (regime strategy must agree with intelligence vote)
    - If strategy says BUY but composite is bearish -> downgrade to HOLD
    - If composite issues hard veto -> HOLD regardless
    - Position size scales by (forward_classifier_conf * composite_confidence)

Every gate exposes reasoning in the SignalOutput.reasoning string for full
on-chain transparency. This addresses the senior peer feedback that pure ML
trading models are "bullshit" — we now show multi-dimensional, auditable,
non-prediction-centric decisions.
"""

from __future__ import annotations

import os
import structlog

from firm.allora_client import MacroSignal, fetch_btc_macro_signal
from firm.config import settings
from firm.ml import fetch_recent_bars, build_features
from firm.regime import detect_current_regime, predict_forward_regime
from firm.schemas import SignalInput, SignalOutput, TradeDirection
from firm.sources import (
    CompositeDecision,
    CompositeVoter,
    RugpullScreener,
    SentimentSource,
    WhaleTracker,
)
from firm.strategies import (
    Action,
    BaseStrategy,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    StrategyDecision,
)
from firm.strategies.base import Position

log = structlog.get_logger()

REGIME_TO_STRATEGY: dict[str, BaseStrategy] = {
    "Trending_Up": MomentumStrategy(),
    "Trending_Down": MomentumStrategy(),
    "Ranging": MeanReversionStrategy(),
    "High_Volatility": DefensiveStrategy(),
}

FORWARD_CONFIDENCE_THRESHOLD = 0.65
TARGET_TOKEN_ADDRESS = os.environ.get(
    "TARGET_TOKEN_ADDRESS",
    "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9",  # USDC on Mantle as safe default
)


async def _safe_macro() -> MacroSignal | None:
    try:
        return await fetch_btc_macro_signal()
    except Exception as e:  # noqa: BLE001
        log.warning("allora.fetch_failed", error=str(e)[:160])
        return None


def _safe_sentiment() -> "object | None":
    try:
        return SentimentSource().fetch()
    except Exception as e:  # noqa: BLE001
        log.warning("sentiment.fetch_failed", error=str(e)[:160])
        return None


def _safe_whale() -> "object | None":
    try:
        return WhaleTracker().fetch()
    except Exception as e:  # noqa: BLE001
        log.warning("whale.fetch_failed", error=str(e)[:160])
        return None


def _safe_rugpull(token_address: str) -> "object | None":
    try:
        return RugpullScreener().assess(token_address)
    except Exception as e:  # noqa: BLE001
        log.warning("rugpull.fetch_failed", error=str(e)[:160])
        return None


def _action_to_direction(action: Action) -> TradeDirection:
    if action == Action.BUY:
        return TradeDirection.BUY
    if action == Action.SELL:
        return TradeDirection.SELL
    return TradeDirection.HOLD


def _composite_disagrees(action: Action, decision: CompositeDecision) -> bool:
    """True if multi-source vote contradicts the strategy's direction."""
    if action == Action.BUY and decision.direction == "bearish":
        return True
    if action == Action.SELL and decision.direction == "bullish":
        return True
    return False


async def run(inputs: SignalInput) -> SignalOutput:
    log.info("signal.run", pair=inputs.pair)
    reasoning: list[str] = []

    # ─── Build local feature window ────────────────────────────────────
    bars = fetch_recent_bars(days=7)
    window = build_features(bars)
    window = window.replace([float("inf"), float("-inf")], None).dropna()
    if window.empty:
        return SignalOutput(
            pair=inputs.pair,
            direction=TradeDirection.HOLD,
            confidence=0.0,
            allora_value=None,
            allora_confidence_bps=None,
            ml_buy_prob=None,
            ml_sell_prob=None,
            ml_adx=None,
            reasoning="Empty feature window after dropping NaNs.",
        )
    last = window.iloc[-1]

    # ─── LAYER 1: regime detection + forward forecast ──────────────────
    current_regime = detect_current_regime(last)
    forward = predict_forward_regime(window)
    reasoning.append(
        f"[L1] current={current_regime}, forward(t+{forward['horizon_bars']}h)="
        f"{forward['regime']} conf={forward['confidence']:.2f}"
    )

    if forward["confidence"] < FORWARD_CONFIDENCE_THRESHOLD:
        reasoning.append(
            f"[L1] forward confidence < {FORWARD_CONFIDENCE_THRESHOLD} -> HOLD"
        )
        return _hold_output(inputs, last, forward, None, reasoning)

    # Defensive priority: if CURRENT is High_Volatility, never delegate to forward
    # prediction. Whipsaw cuts our momentum strategy to ribbons. Stay defensive.
    if current_regime == "High_Volatility":
        chosen_regime = "High_Volatility"
        reasoning.append("[L1] current=High_Volatility -> DEFENSIVE priority (ignore forward)")
    elif current_regime == forward["regime"]:
        chosen_regime = current_regime
    else:
        chosen_regime = forward["regime"]
        reasoning.append(f"[L1] anticipated transition {current_regime}->{chosen_regime}")
    strategy = REGIME_TO_STRATEGY[chosen_regime]
    decision: StrategyDecision = strategy.evaluate(window, Position())
    reasoning.append(f"[L1] strategy={strategy.name} -> {decision.action.value}: {decision.reasoning}")

    # If strategy is defensive or no-trade, surface it and stop.
    if decision.action in (Action.HOLD, Action.EXIT, Action.STABLE_YIELD):
        return _hold_output(
            inputs, last, forward, None, reasoning, override_action=decision.action
        )

    # ─── LAYER 2: multi-source intelligence vote ───────────────────────
    macro = await _safe_macro()
    whale = _safe_whale()
    sentiment = _safe_sentiment()
    rug = _safe_rugpull(TARGET_TOKEN_ADDRESS)

    composite = CompositeVoter().decide(
        allora=macro, whale=whale, sentiment=sentiment, rugpull=rug,
    )
    reasoning.append(
        f"[L2] composite={composite.direction.upper()} "
        f"score={composite.composite_score:+.3f} "
        f"agreement={composite.agreement_score:.2f}"
    )
    for v in composite.votes:
        reasoning.append(f"   - {v.reasoning}")

    # ─── LAYER 3: reconciliation ───────────────────────────────────────
    if composite.hard_veto:
        reasoning.append(f"[L3] HARD VETO -> HOLD ({composite.hard_veto})")
        return _hold_output(inputs, last, forward, macro, reasoning)

    if _composite_disagrees(decision.action, composite):
        reasoning.append(
            f"[L3] strategy {decision.action.value} but composite {composite.direction} "
            f"-> downgrade to HOLD"
        )
        return _hold_output(inputs, last, forward, macro, reasoning)

    # Composite must be at least directional (not pure neutral) to back a trade
    if composite.direction == "neutral":
        reasoning.append("[L3] composite NEUTRAL -> downgrade to HOLD (no consensus).")
        return _hold_output(inputs, last, forward, macro, reasoning)

    # Adaptive sizing: combine all three confidences
    combined = (
        forward["confidence"]
        * max(decision.confidence, 0.5)
        * max(composite.agreement_score, 0.5)
    )
    reasoning.append(
        f"[L3] EXECUTE {decision.action.value}, combined_conf={combined:.2f}"
    )

    return SignalOutput(
        pair=inputs.pair,
        direction=_action_to_direction(decision.action),
        confidence=combined,
        allora_value=macro.predicted_usd if macro else None,
        allora_confidence_bps=macro.confidence_bps if macro else None,
        ml_buy_prob=forward["probabilities"].get("Trending_Up"),
        ml_sell_prob=forward["probabilities"].get("Trending_Down"),
        ml_adx=float(last["adx"]),
        reasoning=" | ".join(reasoning),
    )


def _hold_output(
    inputs: SignalInput,
    last,
    forward: dict,
    macro: MacroSignal | None,
    reasoning: list[str],
    override_action: Action | None = None,
) -> SignalOutput:
    if override_action == Action.EXIT:
        reasoning.append("[STRATEGY] DEFENSIVE: exit existing position.")
    elif override_action == Action.STABLE_YIELD:
        reasoning.append("[STRATEGY] DEFENSIVE: route principal to stable yield.")
    return SignalOutput(
        pair=inputs.pair,
        direction=TradeDirection.HOLD,
        confidence=0.0,
        allora_value=macro.predicted_usd if macro else None,
        allora_confidence_bps=macro.confidence_bps if macro else None,
        ml_buy_prob=forward["probabilities"].get("Trending_Up"),
        ml_sell_prob=forward["probabilities"].get("Trending_Down"),
        ml_adx=float(last["adx"]),
        reasoning=" | ".join(reasoning),
    )
