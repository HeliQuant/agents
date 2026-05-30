"""Momentum / Breakout strategy — fires when the regime is Trending_Up/Down.

Entry rules (BUY side; SELL is the mirror):
  - ADX above threshold (trend strength confirmed)
  - close >= rolling N-bar high (breakout)
  - ema20 > ema50 (short-term above mid-term — directional alignment)

Position is opened at current close. SL/TP are ATR-based using the user's
AI-for-Trading defaults (1x ATR stop, 1.46x ATR target). Exit-while-open
is handled by SL/TP hits, not this strategy — keep evaluate() pure.

Why this fits Trending regimes:
  Trends extend more than mean-reversion. Entering on a breakout with
  trend confirmation gives positive expectancy when trend persists,
  and is naturally vetoed (no entry) when conditions weaken.
"""

from __future__ import annotations

import pandas as pd

from firm.strategies.base import Action, BaseStrategy, Position, StrategyDecision


class MomentumStrategy(BaseStrategy):
    name = "momentum_breakout"
    suitable_regimes = ("Trending_Up", "Trending_Down")

    def __init__(
        self,
        *,
        breakout_window: int = 20,
        adx_min: float = 60.0,            # tuned: p75 of MNT distribution = only very strong trends
        volume_min_ratio: float = 1.15,   # tighter volume confirmation
        sl_atr_mult: float = 1.2,
        tp_atr_mult: float = 2.5,         # wider TP — only trade big movers
    ) -> None:
        self.breakout_window = breakout_window
        self.adx_min = adx_min
        self.volume_min_ratio = volume_min_ratio
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult

    def evaluate(self, window: pd.DataFrame, position: Position) -> StrategyDecision:
        bar = window.iloc[-1]
        prior = window.iloc[-(self.breakout_window + 1):-1]

        close = float(bar["close"])
        atr = float(bar["atr"])
        adx = float(bar["adx"])
        ema20 = float(bar["ema20"])
        ema50 = float(bar["ema50"])
        ema100 = float(bar.get("ema100", ema50))
        rsi = float(bar.get("rsi", 50.0))
        volume_ratio = float(bar.get("volume_ratio", 1.0))

        if position.in_market:
            return StrategyDecision(
                action=Action.HOLD,
                reasoning="Position already open; momentum strategy lets SL/TP exit.",
            )

        if adx < self.adx_min:
            return StrategyDecision(
                action=Action.HOLD,
                reasoning=f"ADX {adx:.1f} < {self.adx_min} — trend not strong enough.",
            )
        if volume_ratio < self.volume_min_ratio:
            return StrategyDecision(
                action=Action.HOLD,
                reasoning=f"Volume ratio {volume_ratio:.2f} < {self.volume_min_ratio} — "
                          f"breakout not volume-confirmed (likely false move).",
            )
        if atr <= 0 or len(prior) < self.breakout_window:
            return StrategyDecision(
                action=Action.HOLD,
                reasoning="Insufficient window or ATR data.",
            )

        prior_high = float(prior["high"].max())
        prior_low = float(prior["low"].min())

        # BUY: requires full uptrend stack (20>50>100), breakout, AND non-extreme RSI
        uptrend_stack = ema20 > ema50 > ema100
        downtrend_stack = ema20 < ema50 < ema100

        if uptrend_stack and close >= prior_high and rsi < 70:
            sl, tp = self._atr_levels(
                close, atr, Action.BUY, self.sl_atr_mult, self.tp_atr_mult
            )
            return StrategyDecision(
                action=Action.BUY,
                confidence=min((adx - self.adx_min) / 25.0, 1.0),
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                reasoning=(
                    f"BUY breakout: close {close:.4f} >= prior_high {prior_high:.4f}, "
                    f"ADX {adx:.1f}, EMA stack 20>50>100, RSI {rsi:.1f}<70 (not extreme)."
                ),
                meta={"prior_high": prior_high, "adx": adx, "rsi": rsi},
            )

        if downtrend_stack and close <= prior_low and rsi > 30:
            sl, tp = self._atr_levels(
                close, atr, Action.SELL, self.sl_atr_mult, self.tp_atr_mult
            )
            return StrategyDecision(
                action=Action.SELL,
                confidence=min((adx - self.adx_min) / 25.0, 1.0),
                entry_price=close,
                stop_loss=sl,
                take_profit=tp,
                reasoning=(
                    f"SELL breakdown: close {close:.4f} <= prior_low {prior_low:.4f}, "
                    f"ADX {adx:.1f}, EMA stack 20<50<100, RSI {rsi:.1f}>30 (not extreme)."
                ),
                meta={"prior_low": prior_low, "adx": adx, "rsi": rsi},
            )

        return StrategyDecision(
            action=Action.HOLD,
            reasoning=(
                f"No breakout: close {close:.4f}, range [{prior_low:.4f}, {prior_high:.4f}]."
            ),
        )
