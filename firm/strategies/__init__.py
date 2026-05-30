"""Rule-based trading strategies, one per market regime.

The Strategy Router (firm/agents/signal.py) maps the predicted regime to one
of these and asks it to .evaluate() the current market window. Each strategy
returns a StrategyDecision or None.

Adding a new strategy:
  1. Subclass BaseStrategy
  2. Implement evaluate(df, position) -> StrategyDecision | None
  3. Add to STRATEGY_MAP in router
"""

from firm.strategies.base import (
    Action,
    BaseStrategy,
    StrategyDecision,
)
from firm.strategies.defensive import DefensiveStrategy
from firm.strategies.mean_reversion import MeanReversionStrategy
from firm.strategies.momentum import MomentumStrategy

__all__ = [
    "Action",
    "BaseStrategy",
    "StrategyDecision",
    "MomentumStrategy",
    "MeanReversionStrategy",
    "DefensiveStrategy",
]
