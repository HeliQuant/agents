"""Multi-source intelligence inputs for the composite Signal Agent.

Each source returns a typed signal with a directional flag (bullish/bearish/neutral)
and confidence/magnitude. The Composite aggregator then votes across sources.

Sources:
  - sentiment.py — price/volume/trending sentiment proxy via CoinGecko free API
  - whale.py     — on-chain MNT/USDC large-swap flow via Mantle RPC
  - rugpull.py   — GoPlus token security screener (safety / kill-switch)
  - composite.py — voting aggregator that combines all sources + Allora macro
"""

from firm.sources.sentiment import SentimentScore, SentimentSource
from firm.sources.whale import WhaleFlowSignal, WhaleTracker
from firm.sources.rugpull import RiskAssessment, RugpullScreener
from firm.sources.composite import CompositeDecision, CompositeVoter, Vote

__all__ = [
    "SentimentScore",
    "SentimentSource",
    "WhaleFlowSignal",
    "WhaleTracker",
    "RiskAssessment",
    "RugpullScreener",
    "CompositeDecision",
    "CompositeVoter",
    "Vote",
]
