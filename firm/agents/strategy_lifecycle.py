"""Strategy Lifecycle Manager — keep Momentum dormant until market warrants it.

Concept: real quant funds don't have every strategy running all the time.
Strategies get *activated* and *deactivated* based on macro regime. Momentum
should be sleeping during chop/bear-and-sideways markets, and only deployed
when a clear trend has visibly established over a longer horizon.

Status states per strategy:
  - ACTIVE_LONG  : can fire BUYs
  - ACTIVE_SHORT : can fire SELLs
  - ACTIVE_BOTH  : can fire either side
  - PENDING      : dormant, do not deploy this strategy
  - DISABLED     : permanently off (e.g., found to be broken)

For Momentum specifically:
  Look at past N-bar (default 30) close + EMA50 movement to determine if a
  trend has actually established. If yes -> ACTIVE in that direction.
  Otherwise -> PENDING.

Mean Reversion is ALWAYS active (it works in any condition where RSI hits
extremes; the strategy's own gate handles selectivity).

Defensive is always available (activated by High_Volatility regime label).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class LifecycleStatus(str, Enum):
    ACTIVE_LONG = "active_long"
    ACTIVE_SHORT = "active_short"
    ACTIVE_BOTH = "active_both"
    PENDING = "pending"
    DISABLED = "disabled"


@dataclass(frozen=True)
class StrategyLifecycle:
    momentum_status: LifecycleStatus
    macro_return_pct: float           # last N-bar % return
    macro_ema_change_pct: float       # last N-bar % EMA50 change
    macro_window_bars: int
    reasoning: str


def assess_momentum_activation(
    window: pd.DataFrame,
    *,
    macro_window_bars: int = 30,
    return_strong_pct: float = 3.0,
    ema_strong_pct: float = 1.5,
) -> StrategyLifecycle:
    """Determine if Momentum strategy should be ACTIVE for the current bar.

    Args:
        window: feature dataframe; uses `close` and `ema50` columns. Must have
                at least `macro_window_bars + 1` rows.
        macro_window_bars: how far back to look for macro trend establishment.
        return_strong_pct: threshold for "clear trend" via raw return.
        ema_strong_pct: threshold for "clear trend" via EMA50 drift.
    """
    if len(window) < macro_window_bars + 1:
        return StrategyLifecycle(
            momentum_status=LifecycleStatus.PENDING,
            macro_return_pct=0.0,
            macro_ema_change_pct=0.0,
            macro_window_bars=macro_window_bars,
            reasoning="insufficient history",
        )

    past_close = float(window["close"].iloc[-(macro_window_bars + 1)])
    current_close = float(window["close"].iloc[-1])
    macro_return_pct = (current_close - past_close) / past_close * 100

    past_ema = float(window["ema50"].iloc[-(macro_window_bars + 1)])
    current_ema = float(window["ema50"].iloc[-1])
    macro_ema_change_pct = (current_ema - past_ema) / past_ema * 100

    long_bias = (
        macro_return_pct >= return_strong_pct
        and macro_ema_change_pct >= ema_strong_pct
    )
    short_bias = (
        macro_return_pct <= -return_strong_pct
        and macro_ema_change_pct <= -ema_strong_pct
    )

    if long_bias and not short_bias:
        status = LifecycleStatus.ACTIVE_LONG
        why = (
            f"Macro long: ret_{macro_window_bars}={macro_return_pct:+.2f}% >= {return_strong_pct}% "
            f"AND EMA50_{macro_window_bars}={macro_ema_change_pct:+.2f}% >= {ema_strong_pct}% "
            f"-> momentum activated long-only"
        )
    elif short_bias and not long_bias:
        status = LifecycleStatus.ACTIVE_SHORT
        why = (
            f"Macro short: ret_{macro_window_bars}={macro_return_pct:+.2f}% <= -{return_strong_pct}% "
            f"AND EMA50_{macro_window_bars}={macro_ema_change_pct:+.2f}% <= -{ema_strong_pct}% "
            f"-> momentum activated short-only"
        )
    else:
        status = LifecycleStatus.PENDING
        why = (
            f"No clear macro trend: ret_{macro_window_bars}={macro_return_pct:+.2f}%, "
            f"EMA50_{macro_window_bars}={macro_ema_change_pct:+.2f}% -> momentum PENDING"
        )

    return StrategyLifecycle(
        momentum_status=status,
        macro_return_pct=macro_return_pct,
        macro_ema_change_pct=macro_ema_change_pct,
        macro_window_bars=macro_window_bars,
        reasoning=why,
    )
