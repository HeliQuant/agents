"""Shared types + interface for all rule-based strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"           # close any open position
    HOLD = "HOLD"           # explicitly do nothing
    STABLE_YIELD = "STABLE" # rotate principal to yield (defensive)


@dataclass
class StrategyDecision:
    """Output of strategy.evaluate(). Reasoning is exposed in on-chain log."""

    action: Action
    confidence: float = 0.0            # 0..1, strategy-internal conviction
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    size_fraction: float = 1.0         # fraction of allowed risk budget
    reasoning: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """Current open position, passed into strategies for exit / sizing decisions."""

    in_market: bool = False
    side: Action | None = None   # BUY or SELL
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


class BaseStrategy(ABC):
    """All strategies implement this minimal interface."""

    #: human-readable strategy name (used in logs & pitch)
    name: str = "base"

    #: regime labels (from regime classifier) where this strategy is the
    #: PRIMARY choice. The router uses this to map regime -> strategy.
    suitable_regimes: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(self, window: pd.DataFrame, position: Position) -> StrategyDecision:
        """Inspect the latest market window + current position, return a decision.

        Args:
            window: tail of feature dataframe. The LAST row is the current bar.
                    Must contain at minimum: close, atr, rsi, adx, ema20, ema50.
            position: current open position (or empty Position if flat).
        Returns:
            StrategyDecision describing what to do.
        """
        ...

    # ─── shared helpers ────────────────────────────────────────────────

    @staticmethod
    def _atr_levels(
        entry: float,
        atr: float,
        side: Action,
        sl_multiplier: float = 1.0,
        tp_multiplier: float = 1.46,
    ) -> tuple[float, float]:
        """Return (stop_loss, take_profit) using ATR-based distances.

        sl_multiplier/tp_multiplier defaults match the user's AI for Trading
        methodology (1.46 reward ratio).
        """
        if side == Action.BUY:
            return entry - atr * sl_multiplier, entry + atr * tp_multiplier
        return entry + atr * sl_multiplier, entry - atr * tp_multiplier
