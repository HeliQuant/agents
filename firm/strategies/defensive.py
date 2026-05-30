"""Defensive strategy — fires when regime is High_Volatility.

Logic: in high-volatility regimes, retail directional trades have terrible
expected value (slippage spikes, stops are hit far more often, regime
transitions are sharp). Best action is to close any open position and route
principal to a stablecoin yield venue.

Per the Gemini research, funding-rate harvesting is no longer viable
(APY < 4% mid-2025), so we route to stable yield (Aave/LI.FI style) instead.

This strategy is intentionally LOUD — it produces an EXIT/STABLE_YIELD signal
even when flat. The router treats this as "do not enter".
"""

from __future__ import annotations

import pandas as pd

from firm.strategies.base import Action, BaseStrategy, Position, StrategyDecision


class DefensiveStrategy(BaseStrategy):
    name = "defensive_kill_switch"
    suitable_regimes = ("High_Volatility",)

    def evaluate(self, window: pd.DataFrame, position: Position) -> StrategyDecision:
        bar = window.iloc[-1]
        vol = float(bar.get("volatility_10", 0.0))
        atr = float(bar.get("atr", 0.0))

        if position.in_market:
            return StrategyDecision(
                action=Action.EXIT,
                confidence=1.0,
                reasoning=(
                    f"High-volatility regime detected (vol_10={vol:.5f}, ATR={atr:.5f}). "
                    f"Closing position to protect capital."
                ),
                meta={"vol_10": vol, "atr": atr},
            )

        return StrategyDecision(
            action=Action.STABLE_YIELD,
            confidence=1.0,
            reasoning=(
                f"High-volatility regime + flat. Route principal to stablecoin "
                f"yield until regime normalises (vol_10={vol:.5f})."
            ),
            meta={"vol_10": vol, "atr": atr},
        )
