"""Adaptive Strategy Selector — multi-source consensus picks the right strategy.

Replaces the static "regime label -> strategy" mapping with a dynamic
decision that fuses every available source:

  - ML regime probability distribution (P_trending vs P_ranging vs P_highvol)
  - ADX  (trend strength)
  - Volatility_10  (vol regime)
  - Sentiment proxy  (recent price momentum)
  - Allora BTC macro (when available; skipped in replay)
  - Whale flow (when available; skipped in replay)

Decision tree (priority order):

  1. If volatility > vol_high → DEFENSIVE (stay out of chop)
  2. If "strong-bull" consensus (ADX strong + ML trending_up + bullish sentiment) → MOMENTUM
  3. If "strong-bear" consensus (ADX strong + ML trending_down + bearish sentiment) → MOMENTUM
  4. If "ranging" consensus (low ADX + ML ranging + neutral sentiment) → MEAN REVERSION
  5. Otherwise → DEFENSIVE (no consensus = no trade)

This is more selective by design — only fires when multiple independent
indicators agree, mirroring how multi-strategy quant funds actually
allocate capital across regimes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from firm.strategies import (
    BaseStrategy,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
)


@dataclass(frozen=True)
class SelectionDecision:
    strategy: BaseStrategy
    reason: str
    score: float                   # composite conviction 0..1
    market_state: str              # "strong_bull" | "strong_bear" | "ranging" | "volatile" | "mixed"


def _sentiment_from_returns(bar: pd.Series) -> float:
    """Compose a [-1, +1] sentiment score from recent returns (same formula as
    the live SentimentSource so backtests reflect production behavior)."""
    r1 = float(bar.get("return_1", 0.0)) * 100
    r5 = float(bar.get("return_5", 0.0)) * 100
    r10 = float(bar.get("return_10", 0.0)) * 100
    score = (
        math.tanh(r1 / 5.0) * 0.40
        + math.tanh(r5 / 15.0) * 0.30
        + math.tanh(r10 / 25.0) * 0.10
    ) / 0.80
    return float(max(-1.0, min(1.0, score)))


def select_strategy(
    bar: pd.Series,
    ml_probabilities: dict[str, float],
    *,
    vol_high_threshold: float,
    adx_strong_threshold: float = 30.0,
    sentiment_strong_threshold: float = 0.25,
    sentiment_neutral_threshold: float = 0.10,
    allora_bullish: bool | None = None,
    allora_bearish: bool | None = None,
    whale_bullish: bool | None = None,
    whale_bearish: bool | None = None,
) -> SelectionDecision:
    """Pick a strategy by fusing all available signals."""
    vol = float(bar.get("volatility_10", 0.0))
    adx = float(bar.get("adx", 0.0))
    ema20 = float(bar.get("ema20", 0.0))
    ema50 = float(bar.get("ema50", 0.0))

    sentiment = _sentiment_from_returns(bar)
    p_trending = ml_probabilities.get("Trending_Up", 0.0) + ml_probabilities.get("Trending_Down", 0.0)
    p_ranging = ml_probabilities.get("Ranging", 0.0)
    p_highvol = ml_probabilities.get("High_Volatility", 0.0)
    p_up = ml_probabilities.get("Trending_Up", 0.0)
    p_down = ml_probabilities.get("Trending_Down", 0.0)

    # ─── 1) Volatility veto ─────────────────────────────────────────────
    if vol > vol_high_threshold or p_highvol >= 0.45:
        return SelectionDecision(
            strategy=DefensiveStrategy(),
            reason=f"vol_10={vol:.5f} > {vol_high_threshold:.5f} OR P(HighVol)={p_highvol:.2f} -> DEFENSIVE",
            score=1.0,
            market_state="volatile",
        )

    # Build directional consensus score [-1, +1]: positive = bull, negative = bear
    bull_votes: list[float] = []
    bear_votes: list[float] = []
    if sentiment >= sentiment_strong_threshold:
        bull_votes.append(sentiment)
    elif sentiment <= -sentiment_strong_threshold:
        bear_votes.append(-sentiment)
    if p_up > p_down + 0.10:
        bull_votes.append(p_up - p_down)
    elif p_down > p_up + 0.10:
        bear_votes.append(p_down - p_up)
    if ema20 > ema50 * 1.001:
        bull_votes.append(0.5)
    elif ema20 < ema50 * 0.999:
        bear_votes.append(0.5)
    if allora_bullish:
        bull_votes.append(0.7)
    if allora_bearish:
        bear_votes.append(0.7)
    if whale_bullish:
        bull_votes.append(0.6)
    if whale_bearish:
        bear_votes.append(0.6)

    bull_score = sum(bull_votes) / max(len(bull_votes), 1) if bull_votes else 0.0
    bear_score = sum(bear_votes) / max(len(bear_votes), 1) if bear_votes else 0.0

    strong_trend = adx >= adx_strong_threshold and p_trending >= 0.35

    # ─── 2) Strong directional consensus + trend → Momentum ─────────────
    if strong_trend and bull_score > 0.30 and len(bull_votes) >= 2 and bear_score == 0:
        return SelectionDecision(
            strategy=MomentumStrategy(),
            reason=(
                f"STRONG BULL: ADX={adx:.1f}, P(trend)={p_trending:.2f}, "
                f"{len(bull_votes)} bullish votes (score {bull_score:.2f}) -> MOMENTUM"
            ),
            score=bull_score,
            market_state="strong_bull",
        )
    if strong_trend and bear_score > 0.30 and len(bear_votes) >= 2 and bull_score == 0:
        return SelectionDecision(
            strategy=MomentumStrategy(),
            reason=(
                f"STRONG BEAR: ADX={adx:.1f}, P(trend)={p_trending:.2f}, "
                f"{len(bear_votes)} bearish votes (score {bear_score:.2f}) -> MOMENTUM"
            ),
            score=bear_score,
            market_state="strong_bear",
        )

    # ─── 3) Ranging consensus → Mean Reversion ──────────────────────────
    # Relaxed: trust regime classifier; allow sentiment up to 0.20
    is_ranging = (
        p_ranging >= 0.40
        and abs(sentiment) < 0.20
        and adx < adx_strong_threshold
    )
    if is_ranging:
        return SelectionDecision(
            strategy=MeanReversionStrategy(),
            reason=(
                f"RANGING: P(Ranging)={p_ranging:.2f}, ADX={adx:.1f}, "
                f"sentiment={sentiment:+.2f} -> MEAN REVERSION"
            ),
            score=p_ranging,
            market_state="ranging",
        )

    # ─── 4) Mixed signals -> defensive (no consensus = no trade) ────────
    return SelectionDecision(
        strategy=DefensiveStrategy(),
        reason=(
            f"MIXED: bull={bull_score:.2f}({len(bull_votes)}), "
            f"bear={bear_score:.2f}({len(bear_votes)}), "
            f"P(ranging)={p_ranging:.2f}, ADX={adx:.1f}, sentiment={sentiment:+.2f} -> no consensus"
        ),
        score=0.0,
        market_state="mixed",
    )
