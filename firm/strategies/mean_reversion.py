"""Mean Reversion strategy — fires when the regime is Ranging.

Entry rules:
  - BUY when RSI < oversold_threshold AND price below ema20 (price stretched below mean)
  - SELL when RSI > overbought_threshold AND price above ema20 (price stretched above mean)
Exit: handled by SL/TP. Target is the mid-band (ema20).

Why this fits Ranging regimes:
  In sideways markets, prices oscillate around a mean. Fading extremes
  has positive expectancy. Critically, we ONLY apply mean reversion in
  a ranging regime — applying it in a trend = ruin.
"""

from __future__ import annotations

import pandas as pd

from firm.strategies.base import Action, BaseStrategy, Position, StrategyDecision


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"
    suitable_regimes = ("Ranging",)

    def __init__(
        self,
        *,
        oversold_rsi: float = 25.0,        # MNT V5 production
        overbought_rsi: float = 75.0,
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 1.8,
    ) -> None:
        self.oversold_rsi = oversold_rsi
        self.overbought_rsi = overbought_rsi
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult

    def evaluate(self, window: pd.DataFrame, position: Position) -> StrategyDecision:
        bar = window.iloc[-1]
        close = float(bar["close"])
        atr = float(bar["atr"])
        rsi = float(bar["rsi"])
        ema20 = float(bar["ema20"])

        if position.in_market:
            return StrategyDecision(
                action=Action.HOLD,
                reasoning="Position already open; SL/TP handle exit.",
            )
        if atr <= 0:
            return StrategyDecision(action=Action.HOLD, reasoning="ATR unavailable.")

        if rsi <= self.oversold_rsi and close < ema20:
            sl, tp = self._atr_levels(
                close, atr, Action.BUY, self.sl_atr_mult, self.tp_atr_mult
            )
            return StrategyDecision(
                action=Action.BUY,
                confidence=min((self.oversold_rsi - rsi) / 20.0, 1.0),
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                reasoning=(
                    f"BUY mean-reversion: RSI {rsi:.1f} <= {self.oversold_rsi} "
                    f"and close {close:.4f} below EMA20 {ema20:.4f}."
                ),
                meta={"rsi": rsi, "ema20_distance": (ema20 - close) / ema20},
            )

        if rsi >= self.overbought_rsi and close > ema20:
            sl, tp = self._atr_levels(
                close, atr, Action.SELL, self.sl_atr_mult, self.tp_atr_mult
            )
            return StrategyDecision(
                action=Action.SELL,
                confidence=min((rsi - self.overbought_rsi) / 20.0, 1.0),
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                reasoning=(
                    f"SELL mean-reversion: RSI {rsi:.1f} >= {self.overbought_rsi} "
                    f"and close {close:.4f} above EMA20 {ema20:.4f}."
                ),
                meta={"rsi": rsi, "ema20_distance": (close - ema20) / ema20},
            )

        return StrategyDecision(
            action=Action.HOLD,
            reasoning=f"RSI {rsi:.1f} mid-range; no fade signal.",
        )
