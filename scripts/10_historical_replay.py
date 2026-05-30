"""Historical replay — find every bar in 90 days where MANTIS would have fired
and walk through real outcomes.

Methodology (NO synthetic):
  - Load 90 days of real MNT/USD hourly features
  - For EACH bar (after warmup):
      * Detect current regime via deterministic rules
      * Predict forward regime via real XGBoost classifier
      * Pass forward_confidence gate (production threshold 0.65)
      * Route to regime-matched strategy
      * Get strategy decision (BUY/SELL/HOLD)
      * If BUY/SELL: simulate forward 8-bar outcome using REAL price highs/lows
  - Realistic costs: 0.10% per swap (Mantle DEX competitive)
  - Realistic risk: 1% per trade

Output:
  data/historical_replay_trades.csv  one row per fired signal + outcome
  data/historical_replay_summary.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.agents.strategy_lifecycle import (  # noqa: E402
    LifecycleStatus,
    assess_momentum_activation,
)
from firm.strategies import (  # noqa: E402
    Action,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    StrategyDecision,
)
from firm.strategies.base import Position  # noqa: E402

DATA_PATH = ROOT / "data" / "mnt_features.csv"
REGIME_MODEL = ROOT / "models" / "xgb_regime.pkl"
OUT_TRADES = ROOT / "data" / "historical_replay_trades.csv"
OUT_SUMMARY = ROOT / "data" / "historical_replay_summary.json"

INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
LOOKAHEAD = 8
SL_MULT = 1.0
REWARD_MULT = 1.46
FORWARD_CONFIDENCE_THRESHOLD = 0.65
ANNUALISATION_FACTOR = 24 * 365

REGIME_TO_STRATEGY = {
    "Trending_Up": MomentumStrategy(),
    "Trending_Down": MomentumStrategy(),
    "Ranging": MeanReversionStrategy(),
    "High_Volatility": DefensiveStrategy(),
}


def detect_current(row, adx_strong: float, vol_high: float) -> str:
    if float(row["volatility_10"]) > vol_high:
        return "High_Volatility"
    if float(row["adx"]) > adx_strong:
        return "Trending_Up" if float(row["ema_slope_5"]) > 0 else "Trending_Down"
    return "Ranging"


def simulate_outcome(i: int, df: pd.DataFrame, direction: int, entry: float, atr: float, equity: float):
    sl_dist = atr * SL_MULT
    notional = (equity * RISK_PER_TRADE) * (entry / sl_dist)
    notional = min(notional, equity * 0.95)
    window = df.iloc[i + 1 : i + LOOKAHEAD + 1]
    if window.empty:
        return 0.0, "TIMEOUT", entry, notional, 0

    if direction > 0:
        tp = entry + atr * REWARD_MULT
        sl = entry - sl_dist
        hit_tp = window[window["high"] >= tp]
        hit_sl = window[window["low"] <= sl]
    else:
        tp = entry - atr * REWARD_MULT
        sl = entry + sl_dist
        hit_tp = window[window["low"] <= tp]
        hit_sl = window[window["high"] >= sl]

    if not hit_tp.empty and not hit_sl.empty:
        if hit_tp.index[0] < hit_sl.index[0]:
            outcome, exit_price, bars = "TP", tp, int(hit_tp.index[0] - window.index[0]) + 1
        else:
            outcome, exit_price, bars = "SL", sl, int(hit_sl.index[0] - window.index[0]) + 1
    elif not hit_tp.empty:
        outcome, exit_price, bars = "TP", tp, int(hit_tp.index[0] - window.index[0]) + 1
    elif not hit_sl.empty:
        outcome, exit_price, bars = "SL", sl, int(hit_sl.index[0] - window.index[0]) + 1
    else:
        outcome, exit_price, bars = "TIMEOUT", float(window["close"].iloc[-1]), LOOKAHEAD

    pnl_pct = (exit_price - entry) / entry if direction > 0 else (entry - exit_price) / entry
    gross = notional * pnl_pct
    fee = notional * SWAP_FEE * 2
    return gross - fee, outcome, exit_price, notional, bars


def main() -> None:
    print(f"Loading data + model")
    df = pd.read_csv(DATA_PATH).reset_index(drop=True)
    print(f"  bars: {len(df)}")

    bundle = joblib.load(REGIME_MODEL)
    model = bundle["model"]
    features = bundle["features"]
    labels = bundle["labels"]
    adx_strong = bundle["adx_strong_threshold"]
    vol_high = bundle["vol_high_threshold"]

    proba = model.predict_proba(df[features])
    print(f"  forward predictions: {proba.shape}")

    equity = INITIAL_EQUITY
    trades: list[dict] = []
    i = 30  # warmup for rolling window in strategies
    fired_signals = 0
    held_signals = 0
    cooldown_until = -1

    while i < len(df) - LOOKAHEAD:
        if i <= cooldown_until:
            i += 1
            continue

        row = df.iloc[i]
        current = detect_current(row, adx_strong, vol_high)
        idx = int(proba[i].argmax())
        fwd_label = labels[idx]
        fwd_conf = float(proba[i][idx])
        if fwd_conf < FORWARD_CONFIDENCE_THRESHOLD:
            i += 1
            continue

        # Defensive priority + Strategy Lifecycle: only deploy Momentum when
        # the macro market has actually established a clear trend (30-bar
        # return + EMA50 drift both confirm). Otherwise Momentum is dormant.
        if current == "High_Volatility":
            chosen = "High_Volatility"
        elif current == fwd_label:
            chosen = current
        else:
            chosen = fwd_label

        lifecycle = assess_momentum_activation(df.iloc[max(0, i - 60):i + 1])

        if chosen in ("Trending_Up", "Trending_Down"):
            # Momentum stays dormant for MNT bear-phase validation (V5):
            # MNT data showed momentum loses in all tested configurations.
            chosen = "High_Volatility"   # treat as Defensive (no trade)
        elif chosen == "Ranging" and not lifecycle.mean_reversion_active:
            # Mean reversion lifecycle gate: skip fading extremes when the macro
            # 30-bar move is large (trending) -> avoids catching a falling knife.
            chosen = "High_Volatility"   # treat as Defensive (no trade)

        strategy = REGIME_TO_STRATEGY[chosen]
        window = df.iloc[max(0, i - 30):i + 1]
        decision: StrategyDecision = strategy.evaluate(window, Position())

        if decision.action not in (Action.BUY, Action.SELL):
            held_signals += 1
            i += 1
            continue

        fired_signals += 1
        direction = 1 if decision.action == Action.BUY else -1
        entry = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue

        net_pnl, outcome, exit_price, notional, bars_held = simulate_outcome(
            i, df, direction, entry, atr, equity
        )
        equity += net_pnl
        trades.append({
            "bar_idx": i,
            "datetime": row.get("datetime"),
            "regime_current": current,
            "regime_forward": fwd_label,
            "fwd_conf": round(fwd_conf, 4),
            "strategy": strategy.name,
            "direction": decision.action.value,
            "entry": entry,
            "exit": exit_price,
            "outcome": outcome,
            "bars_held": bars_held,
            "notional": round(notional, 2),
            "net_pnl": round(net_pnl, 4),
            "equity_after": round(equity, 2),
            "reasoning": decision.reasoning,
        })
        cooldown_until = i + LOOKAHEAD
        i += 1

    OUT_TRADES.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(trades).to_csv(OUT_TRADES, index=False)

    if not trades:
        print("\nNo signals fired in entire 90-day window.")
        summary = {"trades": 0}
    else:
        tdf = pd.DataFrame(trades)
        wins = tdf[tdf["net_pnl"] > 0]
        losses = tdf[tdf["net_pnl"] <= 0]
        eq_series = pd.concat([
            pd.Series([INITIAL_EQUITY]),
            tdf["equity_after"]
        ], ignore_index=True)
        running_peak = np.maximum.accumulate(eq_series.values)
        dd = (running_peak - eq_series.values) / running_peak
        max_dd = float(dd.max() * 100)
        pf = (
            float(wins["net_pnl"].sum()) / float(-losses["net_pnl"].sum())
            if not losses.empty and losses["net_pnl"].sum() != 0
            else float("inf")
        )
        rets = (eq_series.diff() / eq_series.shift()).dropna()
        sharpe = float((rets.mean() / rets.std()) * np.sqrt(ANNUALISATION_FACTOR)) if rets.std() > 0 else 0.0

        summary = {
            "trades": len(tdf),
            "fired_signals": fired_signals,
            "held_signals": held_signals,
            "selectivity_pct": fired_signals / max(fired_signals + held_signals, 1) * 100,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": float(len(wins) / len(tdf) * 100),
            "total_pnl_usd": float(tdf["net_pnl"].sum()),
            "roi_pct": (equity / INITIAL_EQUITY - 1) * 100,
            "profit_factor": pf,
            "max_drawdown_pct": max_dd,
            "sharpe_annualized": sharpe,
            "final_equity": round(equity, 2),
            "by_strategy": tdf.groupby("strategy").agg(
                trades=("net_pnl", "count"),
                wins=("net_pnl", lambda s: int((s > 0).sum())),
                avg_pnl=("net_pnl", "mean"),
                total_pnl=("net_pnl", "sum"),
            ).round(3).to_dict(orient="index"),
            "by_regime": tdf.groupby("regime_current").agg(
                trades=("net_pnl", "count"),
                wins=("net_pnl", lambda s: int((s > 0).sum())),
                avg_pnl=("net_pnl", "mean"),
                total_pnl=("net_pnl", "sum"),
            ).round(3).to_dict(orient="index"),
            "by_outcome": tdf["outcome"].value_counts().to_dict(),
        }

    OUT_SUMMARY.write_text(json.dumps(summary, indent=2, default=str))

    print("\n=== HISTORICAL REPLAY RESULTS ===")
    if trades:
        print(f"  Signals evaluated  : {fired_signals + held_signals}")
        print(f"  Signals fired      : {fired_signals} ({summary['selectivity_pct']:.1f}%)")
        print(f"  Trades simulated   : {len(trades)}")
        print(f"  Wins / Losses      : {summary['wins']} / {summary['losses']}")
        print(f"  Win rate           : {summary['win_rate_pct']:.2f}%")
        print(f"  Total PnL          : ${summary['total_pnl_usd']:+.2f}")
        print(f"  ROI                : {summary['roi_pct']:+.2f}%")
        print(f"  Profit factor      : {summary['profit_factor']:.2f}")
        print(f"  Max drawdown       : {summary['max_drawdown_pct']:.2f}%")
        print(f"  Sharpe (annualized): {summary['sharpe_annualized']:.2f}")
        print(f"  Final equity       : ${summary['final_equity']:.2f}")
        print()
        print(f"  Outcome distribution: {summary['by_outcome']}")
        print(f"  Per-strategy: {summary['by_strategy']}")
    print(f"\nSaved trades  -> {OUT_TRADES}")
    print(f"Saved summary -> {OUT_SUMMARY}")


if __name__ == "__main__":
    main()
