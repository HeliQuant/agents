"""Composite Voter — aggregates intelligence sources into a single decision.

Sources voted:
  - Allora macro (BTC 8h price prediction)
  - Whale flow (Mantle on-chain DEX swaps > threshold)
  - Sentiment (price/volume/trending proxy via CoinGecko)
  - Rugpull screener (hard veto when token unsafe)

Voting rule (default):
  - Soft votes: Allora / Whale / Sentiment cast {-1, 0, +1}
  - Confidence-weighted sum normalised by total confidence
  - Require at least `min_agreeing_sources` non-neutral votes pointing the same way
  - Rugpull screener overrides everything when token is unsafe -> HOLD

This is a *transparent* aggregator — every component vote is exposed for the
on-chain reasoning trace. No secret weights, no hidden ML wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from firm.allora_client import MacroSignal
from firm.sources.rugpull import RiskAssessment
from firm.sources.sentiment import SentimentScore
from firm.sources.whale import WhaleFlowSignal


@dataclass(frozen=True)
class Vote:
    source: str
    direction: Literal["bullish", "bearish", "neutral"]
    score: float           # [-1, +1]
    confidence: float      # 0..1
    reasoning: str


@dataclass
class CompositeDecision:
    direction: Literal["bullish", "bearish", "neutral"]
    composite_score: float
    agreement_score: float     # fraction of voters agreeing with majority
    votes: list[Vote] = field(default_factory=list)
    hard_veto: str | None = None     # reason if rugpull or similar blocks
    reasoning: str = ""


class CompositeVoter:
    def __init__(
        self,
        *,
        bullish_threshold: float = 0.20,
        bearish_threshold: float = -0.20,
        min_agreeing_sources: int = 2,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.bullish_threshold = bullish_threshold
        self.bearish_threshold = bearish_threshold
        self.min_agreeing_sources = min_agreeing_sources
        self.weights = weights or {
            "allora": 1.0,
            "whale": 1.0,
            "sentiment": 0.7,
        }

    # ─── normalisers ───────────────────────────────────────────────────

    @staticmethod
    def _allora_vote(macro: MacroSignal | None) -> Vote | None:
        if macro is None:
            return None
        if macro.direction_bull:
            d: Literal["bullish", "bearish", "neutral"] = "bullish"
        elif macro.direction_bear:
            d = "bearish"
        else:
            d = "neutral"
        # Map predicted_return to [-1, +1] via 2% saturation
        import math
        score = max(-1.0, min(1.0, math.tanh(macro.predicted_return / 0.02)))
        return Vote(
            source="allora",
            direction=d,
            score=score,
            confidence=macro.confidence_bps / 10000.0,
            reasoning=(
                f"Allora BTC 8h: spot=${macro.spot_usd:.0f} "
                f"-> predicted=${macro.predicted_usd:.0f} "
                f"(ret={macro.predicted_return*100:+.2f}%) -> {d.upper()}"
            ),
        )

    @staticmethod
    def _whale_vote(w: WhaleFlowSignal | None) -> Vote | None:
        if w is None:
            return None
        return Vote(
            source="whale",
            direction=w.direction,
            score=w.score,
            confidence=w.confidence,
            reasoning=("Whale: " + w.reasoning),
        )

    @staticmethod
    def _sentiment_vote(s: SentimentScore | None) -> Vote | None:
        if s is None:
            return None
        return Vote(
            source="sentiment",
            direction=s.direction,
            score=s.score,
            confidence=s.confidence,
            reasoning=("Sentiment: " + s.reasoning),
        )

    # ─── main API ──────────────────────────────────────────────────────

    def decide(
        self,
        *,
        allora: MacroSignal | None,
        whale: WhaleFlowSignal | None,
        sentiment: SentimentScore | None,
        rugpull: RiskAssessment | None = None,
    ) -> CompositeDecision:
        votes: list[Vote] = []
        for v in (
            self._allora_vote(allora),
            self._whale_vote(whale),
            self._sentiment_vote(sentiment),
        ):
            if v is not None:
                votes.append(v)

        # Hard veto from rugpull screener
        hard_veto = None
        if rugpull is not None and rugpull.is_honeypot:
            hard_veto = f"Rugpull screener: HONEYPOT ({rugpull.token_address})"
        elif rugpull is not None and not rugpull.trade_safe:
            hard_veto = f"Rugpull screener: UNSAFE (risk={rugpull.risk_score:.2f})"

        # Weighted composite score
        num = 0.0
        denom = 0.0
        for v in votes:
            w = self.weights.get(v.source, 1.0) * v.confidence
            num += v.score * w
            denom += w
        composite = num / denom if denom > 0 else 0.0

        # Agreement: how many *non-neutral* votes pointed the same way as composite sign
        signs = [
            1 if v.score > 0.10 else (-1 if v.score < -0.10 else 0)
            for v in votes
        ]
        n_non_neutral = sum(1 for s in signs if s != 0)
        composite_sign = 1 if composite > 0 else (-1 if composite < 0 else 0)
        n_agree = sum(1 for s in signs if s == composite_sign and s != 0)
        agreement = (n_agree / n_non_neutral) if n_non_neutral > 0 else 0.0

        if hard_veto:
            direction: Literal["bullish", "bearish", "neutral"] = "neutral"
            reasoning_summary = f"HARD VETO -> NEUTRAL. {hard_veto}"
        elif composite >= self.bullish_threshold and n_agree >= self.min_agreeing_sources:
            direction = "bullish"
            reasoning_summary = (
                f"BULLISH (score={composite:+.3f}, {n_agree}/{n_non_neutral} agree)."
            )
        elif composite <= self.bearish_threshold and n_agree >= self.min_agreeing_sources:
            direction = "bearish"
            reasoning_summary = (
                f"BEARISH (score={composite:+.3f}, {n_agree}/{n_non_neutral} agree)."
            )
        else:
            direction = "neutral"
            reasoning_summary = (
                f"NEUTRAL: composite={composite:+.3f}, "
                f"non-neutral votes {n_non_neutral}, "
                f"agreement {n_agree}/{n_non_neutral or 1}, "
                f"below threshold {self.bullish_threshold}/{self.bearish_threshold} "
                f"or < {self.min_agreeing_sources} agreeing."
            )

        full_reasoning = reasoning_summary + " | " + " | ".join(v.reasoning for v in votes)
        return CompositeDecision(
            direction=direction,
            composite_score=composite,
            agreement_score=agreement,
            votes=votes,
            hard_veto=hard_veto,
            reasoning=full_reasoning,
        )
