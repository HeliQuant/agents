"""Smoke tests for the 3 rule-based strategies.

Each strategy is tested with hand-crafted feature windows for its target regime
to confirm it produces the expected action.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.strategies import (  # noqa: E402
    Action,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    StrategyDecision,
)
from firm.strategies.base import Position  # noqa: E402


def _window(n: int = 30, **overrides) -> pd.DataFrame:
    """Build a default features window then patch the last row with overrides."""
    rng = np.random.default_rng(42)
    base_close = 1.0 + rng.standard_normal(n).cumsum() * 0.001
    df = pd.DataFrame({
        "close": base_close,
        "high": base_close * 1.002,
        "low": base_close * 0.998,
        "atr": np.full(n, 0.005),
        "rsi": np.full(n, 50.0),
        "adx": np.full(n, 30.0),
        "ema20": base_close * 0.999,
        "ema50": base_close * 0.997,
        "volatility_10": np.full(n, 0.002),
    })
    for k, v in overrides.items():
        df.iloc[-1, df.columns.get_loc(k)] = v
    return df


# ─── Momentum tests ────────────────────────────────────────────────────────


def test_momentum_buy_on_breakout_with_trend():
    s = MomentumStrategy(breakout_window=20)
    win = _window(30)
    # Last bar: above prior high, ADX strong, ema20 > ema50
    prior_high = float(win["high"].iloc[-21:-1].max())
    win.iloc[-1, win.columns.get_loc("close")] = prior_high * 1.005
    win.iloc[-1, win.columns.get_loc("high")] = prior_high * 1.006
    win.iloc[-1, win.columns.get_loc("adx")] = 45.0
    win.iloc[-1, win.columns.get_loc("ema20")] = prior_high * 1.001
    win.iloc[-1, win.columns.get_loc("ema50")] = prior_high * 0.998

    out = s.evaluate(win, Position())
    assert out.action == Action.BUY, out.reasoning
    assert out.entry_price is not None
    assert out.stop_loss is not None and out.stop_loss < out.entry_price
    assert out.take_profit is not None and out.take_profit > out.entry_price


def test_momentum_hold_when_adx_weak():
    s = MomentumStrategy(breakout_window=20)
    win = _window(30, adx=10.0)
    out = s.evaluate(win, Position())
    assert out.action == Action.HOLD
    assert "ADX" in out.reasoning


def test_momentum_hold_when_position_open():
    s = MomentumStrategy()
    out = s.evaluate(_window(), Position(in_market=True, side=Action.BUY))
    assert out.action == Action.HOLD


# ─── Mean reversion tests ──────────────────────────────────────────────────


def test_mean_reversion_buy_when_oversold():
    s = MeanReversionStrategy()
    win = _window(30, rsi=25.0)
    win.iloc[-1, win.columns.get_loc("close")] = 0.99
    win.iloc[-1, win.columns.get_loc("ema20")] = 1.00
    out = s.evaluate(win, Position())
    assert out.action == Action.BUY, out.reasoning


def test_mean_reversion_sell_when_overbought():
    s = MeanReversionStrategy()
    win = _window(30, rsi=78.0)
    win.iloc[-1, win.columns.get_loc("close")] = 1.05
    win.iloc[-1, win.columns.get_loc("ema20")] = 1.00
    out = s.evaluate(win, Position())
    assert out.action == Action.SELL, out.reasoning


def test_mean_reversion_hold_when_neutral():
    s = MeanReversionStrategy()
    win = _window(30, rsi=50.0)
    out = s.evaluate(win, Position())
    assert out.action == Action.HOLD


# ─── Defensive tests ───────────────────────────────────────────────────────


def test_defensive_exit_when_in_market():
    s = DefensiveStrategy()
    out = s.evaluate(_window(), Position(in_market=True, side=Action.BUY))
    assert out.action == Action.EXIT
    assert out.confidence == 1.0


def test_defensive_route_to_yield_when_flat():
    s = DefensiveStrategy()
    out = s.evaluate(_window(), Position())
    assert out.action == Action.STABLE_YIELD


# ─── Interface sanity ─────────────────────────────────────────────────────


@pytest.mark.parametrize("Strategy", [MomentumStrategy, MeanReversionStrategy, DefensiveStrategy])
def test_strategy_returns_decision(Strategy):
    """Every strategy returns a StrategyDecision dataclass."""
    out = Strategy().evaluate(_window(), Position())
    assert isinstance(out, StrategyDecision)
    assert out.action in Action
    assert out.reasoning  # never empty
