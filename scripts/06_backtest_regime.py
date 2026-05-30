"""Pivot Step 4 — backtest the regime-aware architecture end-to-end.

Compares two architectures on the same held-out test data:

  A) OLD: ML directional predictor (XGBoost binary)
     - Same as scripts/04_backtest.py logic
     - Trade on ML buy/sell prob with ADX filter

  B) NEW: Regime-aware router
     - Detect current regime via deterministic rules
     - Forecast regime at t+4h via the new ML classifier
     - Only act if forward-confidence >= 0.60
     - Route to MomentumStrategy / MeanReversionStrategy / DefensiveStrategy

Both architectures get:
  - Same realistic costs: 0.10% per swap (Mantle DEX competitive: Merchant Moe charges ~0.05-0.30%)
  - Same risk model: 1% capital per trade
  - Same execution semantics: enter at close, SL/TP via subsequent high/low

Outputs:
  data/backtest_v2_metrics.json    metrics for both architectures + comparison
  data/backtest_v2_equity.png      equity curves (old vs new)
  data/backtest_v2_trades_old.csv  / _new.csv
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Make `firm.*` importable
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

# ─── Paths ─────────────────────────────────────────────────────────────────
DATA_PATH = ROOT / "data" / "mnt_features.csv"
OLD_MODEL = ROOT / "models" / "xgb_mnt.pkl"
NEW_MODEL = ROOT / "models" / "xgb_regime.pkl"
OUT_DIR = ROOT / "data"

# ─── Trading params ────────────────────────────────────────────────────────
INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010                  # 0.10% per leg (Mantle DEX realistic)
LOOKAHEAD = 8                      # bars (max holding for SL/TP check)
SL_MULT = 1.0
REWARD_MULT = 1.46

# Architecture A (old ML direction)
OLD_BUY_TH, OLD_SELL_TH = 0.65, 0.60
OLD_MIN_ADX = 20.0

# Architecture B (new regime-aware)
NEW_FORWARD_CONF_TH = 0.65          # raised from 0.60 for higher conviction
ANNUALISATION_FACTOR = 24 * 365      # hourly bars -> annual for Sharpe


# ─── Strategy router for arch B ────────────────────────────────────────────
REGIME_TO_STRATEGY = {
    "Trending_Up": MomentumStrategy(),
    "Trending_Down": MomentumStrategy(),
    "Ranging": MeanReversionStrategy(),
    "High_Volatility": DefensiveStrategy(),
}


# ─── Shared trade simulator ────────────────────────────────────────────────


def simulate_trade(
    bar_idx: int,
    df: pd.DataFrame,
    direction: int,            # +1 BUY, -1 SELL
    entry: float,
    atr: float,
    equity: float,
) -> tuple[float, str, float, float]:
    """Return (net_pnl, outcome, exit_price, position_notional)."""
    sl_dist = atr * SL_MULT
    position_notional = (equity * RISK_PER_TRADE) * (entry / sl_dist)
    position_notional = min(position_notional, equity * 0.95)

    window = df.iloc[bar_idx + 1 : bar_idx + LOOKAHEAD + 1]
    if window.empty:
        return 0.0, "TIMEOUT", entry, 0.0

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
        outcome = "TP" if hit_tp.index[0] < hit_sl.index[0] else "SL"
        exit_price = tp if outcome == "TP" else sl
    elif not hit_tp.empty:
        outcome, exit_price = "TP", tp
    elif not hit_sl.empty:
        outcome, exit_price = "SL", sl
    else:
        outcome, exit_price = "TIMEOUT", float(window["close"].iloc[-1])

    pnl_pct = (exit_price - entry) / entry if direction > 0 else (entry - exit_price) / entry
    gross = position_notional * pnl_pct
    fee = position_notional * SWAP_FEE * 2
    return gross - fee, outcome, exit_price, position_notional


# ─── Architecture A: old ML direction predictor ────────────────────────────


def backtest_old(df: pd.DataFrame, proba: np.ndarray) -> tuple[list[dict], pd.Series]:
    sell_p, buy_p = proba[:, 0], proba[:, 1]
    equity = INITIAL_EQUITY
    equity_curve, trades = [], []
    i = 0
    while i < len(df) - LOOKAHEAD:
        equity_curve.append(equity)
        row = df.iloc[i]
        if float(row["adx"]) < OLD_MIN_ADX:
            i += 1
            continue
        if buy_p[i] >= OLD_BUY_TH:
            direction = 1
        elif sell_p[i] >= OLD_SELL_TH:
            direction = -1
        else:
            i += 1
            continue
        entry, atr = float(row["close"]), float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        net_pnl, outcome, exit_price, notional = simulate_trade(
            i, df, direction, entry, atr, equity
        )
        equity += net_pnl
        trades.append({
            "bar_index": i, "arch": "old",
            "direction": "BUY" if direction > 0 else "SELL",
            "entry": entry, "exit": exit_price, "outcome": outcome,
            "notional": notional, "net_pnl": net_pnl, "equity_after": equity,
        })
        i += LOOKAHEAD
    equity_curve.append(equity)
    return trades, pd.Series(equity_curve, name="equity_old")


# ─── Architecture B: regime-aware router ───────────────────────────────────


def detect_current(row, adx_strong: float, vol_high: float) -> str:
    if float(row["volatility_10"]) > vol_high:
        return "High_Volatility"
    if float(row["adx"]) > adx_strong:
        return "Trending_Up" if float(row["ema_slope_5"]) > 0 else "Trending_Down"
    return "Ranging"


def backtest_new(
    df: pd.DataFrame,
    forward_proba: np.ndarray,
    forward_labels: list[str],
    adx_strong: float,
    vol_high: float,
) -> tuple[list[dict], pd.Series, dict[str, int]]:
    equity = INITIAL_EQUITY
    equity_curve, trades = [], []
    regime_counts = {k: 0 for k in ["Ranging", "Trending_Up", "Trending_Down", "High_Volatility"]}

    i = 0
    while i < len(df) - LOOKAHEAD:
        equity_curve.append(equity)
        row = df.iloc[i]

        # Current regime
        current = detect_current(row, adx_strong, vol_high)

        # Forward forecast
        proba_row = forward_proba[i]
        forward_idx = int(proba_row.argmax())
        forward_label = forward_labels[forward_idx]
        forward_conf = float(proba_row[forward_idx])

        if forward_conf < NEW_FORWARD_CONF_TH:
            i += 1
            continue

        chosen = current if current == forward_label else forward_label
        regime_counts[chosen] += 1
        strategy = REGIME_TO_STRATEGY[chosen]

        # Use trailing window for the strategy
        window = df.iloc[max(0, i - 30) : i + 1]
        decision: StrategyDecision = strategy.evaluate(window, Position())
        if decision.action not in (Action.BUY, Action.SELL):
            i += 1
            continue

        direction = 1 if decision.action == Action.BUY else -1
        entry, atr = float(row["close"]), float(row["atr"])
        if atr <= 0:
            i += 1
            continue

        net_pnl, outcome, exit_price, notional = simulate_trade(
            i, df, direction, entry, atr, equity
        )
        equity += net_pnl
        trades.append({
            "bar_index": i, "arch": "new",
            "regime_current": current, "regime_forward": forward_label,
            "forward_conf": forward_conf, "strategy": strategy.name,
            "direction": decision.action.value,
            "entry": entry, "exit": exit_price, "outcome": outcome,
            "notional": notional, "net_pnl": net_pnl, "equity_after": equity,
            "reasoning": decision.reasoning,
        })
        i += LOOKAHEAD
    equity_curve.append(equity)
    return trades, pd.Series(equity_curve, name="equity_new"), regime_counts


# ─── Metrics ───────────────────────────────────────────────────────────────


def summarize(trades: list[dict], equity_series: pd.Series, label: str) -> dict:
    if not trades:
        return {"label": label, "trades": 0}
    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["net_pnl"] > 0]
    losses = tdf[tdf["net_pnl"] <= 0]
    total_pnl = float(tdf["net_pnl"].sum())

    pf = (
        float(wins["net_pnl"].sum()) / float(-losses["net_pnl"].sum())
        if not losses.empty and losses["net_pnl"].sum() != 0
        else float("inf")
    )

    eq = equity_series.values
    if len(eq) > 1:
        rets = np.diff(eq) / eq[:-1]
        if rets.std() > 0:
            sharpe = float((rets.mean() / rets.std()) * np.sqrt(ANNUALISATION_FACTOR))
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    running_peak = np.maximum.accumulate(eq)
    dd_series = (running_peak - eq) / running_peak
    max_dd_pct = float(dd_series.max() * 100) if len(dd_series) > 0 else 0.0
    roi_pct = (float(eq[-1]) / INITIAL_EQUITY - 1.0) * 100
    calmar = (roi_pct / max_dd_pct) if max_dd_pct > 0 else float("inf")

    return {
        "label": label,
        "trades": len(tdf),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": float(len(wins) / len(tdf)),
        "total_pnl": total_pnl,
        "roi_pct": roi_pct,
        "profit_factor": pf,
        "max_drawdown_pct": max_dd_pct,
        "sharpe_annualized": sharpe,
        "calmar": calmar,
        "final_equity": float(eq[-1]),
    }


def main() -> None:
    print("Loading data + models")
    df = pd.read_csv(DATA_PATH).reset_index(drop=True)
    split = int(len(df) * 0.8)
    test_df = df.iloc[split:].reset_index(drop=True)
    print(f"  test_df: {len(test_df)} bars (~{len(test_df)} hours)")

    old_bundle = joblib.load(OLD_MODEL)
    new_bundle = joblib.load(NEW_MODEL)
    old_proba = old_bundle["model"].predict_proba(test_df[old_bundle["features"]])
    new_proba = new_bundle["model"].predict_proba(test_df[new_bundle["features"]])

    print()
    print("Backtest A: OLD direction predictor")
    trades_old, eq_old = backtest_old(test_df, old_proba)
    s_old = summarize(trades_old, eq_old, "OLD: ML direction + ADX")

    print("Backtest B: NEW regime-aware router")
    trades_new, eq_new, regime_counts = backtest_new(
        test_df,
        new_proba,
        new_bundle["labels"],
        new_bundle["adx_strong_threshold"],
        new_bundle["vol_high_threshold"],
    )
    s_new = summarize(trades_new, eq_new, "NEW: regime router (conf>=0.60)")
    s_new["regime_distribution"] = regime_counts

    print()
    for s in (s_old, s_new):
        print(f"=== {s['label']} ===")
        print(f"  trades       : {s.get('trades', 0)}")
        print(f"  wins / losses: {s.get('wins', 0)} / {s.get('losses', 0)}")
        print(f"  win rate     : {s.get('win_rate', 0) * 100:.2f}%")
        print(f"  total PnL    : ${s.get('total_pnl', 0):.2f}")
        print(f"  ROI          : {s.get('roi_pct', 0):.2f}%")
        print(f"  profit factor: {s.get('profit_factor', 0):.2f}")
        print(f"  max drawdown : {s.get('max_drawdown_pct', 0):.2f}%")
        print(f"  Sharpe (ann.): {s.get('sharpe_annualized', 0):.2f}")
        print(f"  Calmar       : {s.get('calmar', 0):.2f}")
        print(f"  final equity : ${s.get('final_equity', INITIAL_EQUITY):.2f}")
        if "regime_distribution" in s:
            print(f"  regime selection counts: {s['regime_distribution']}")
        print()

    pd.DataFrame(trades_old).to_csv(OUT_DIR / "backtest_v2_trades_old.csv", index=False)
    pd.DataFrame(trades_new).to_csv(OUT_DIR / "backtest_v2_trades_new.csv", index=False)
    (OUT_DIR / "backtest_v2_metrics.json").write_text(json.dumps({
        "old_architecture": s_old,
        "new_architecture": s_new,
        "comparison": {
            "roi_delta_pct": s_new.get("roi_pct", 0) - s_old.get("roi_pct", 0),
            "win_rate_delta": s_new.get("win_rate", 0) - s_old.get("win_rate", 0),
            "sharpe_delta": s_new.get("sharpe_annualized", 0) - s_old.get("sharpe_annualized", 0),
            "drawdown_delta_pct": s_old.get("max_drawdown_pct", 0) - s_new.get("max_drawdown_pct", 0),
        },
        "params": {
            "initial_equity": INITIAL_EQUITY,
            "risk_per_trade": RISK_PER_TRADE,
            "swap_fee_per_side": SWAP_FEE,
            "lookahead_bars": LOOKAHEAD,
            "sl_atr_mult": SL_MULT,
            "tp_atr_mult": REWARD_MULT,
            "old_buy_threshold": OLD_BUY_TH,
            "old_sell_threshold": OLD_SELL_TH,
            "old_min_adx": OLD_MIN_ADX,
            "new_forward_conf_threshold": NEW_FORWARD_CONF_TH,
            "annualisation_factor": ANNUALISATION_FACTOR,
        },
    }, indent=2))

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(eq_old.values, label=s_old["label"], color="#1f77b4", linewidth=2)
    ax.plot(eq_new.values, label=s_new["label"], color="#2ca02c", linewidth=2)
    ax.axhline(INITIAL_EQUITY, color="gray", linestyle="--", alpha=0.6,
               label=f"Initial ${INITIAL_EQUITY:.0f}")
    ax.set_title("MNT/USD Backtest -- OLD ML direction vs NEW regime-aware router")
    ax.set_xlabel("Bars elapsed (1h cadence)")
    ax.set_ylabel("Equity ($)")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "backtest_v2_equity.png", dpi=120)

    print(f"Saved chart   -> {OUT_DIR / 'backtest_v2_equity.png'}")
    print(f"Saved metrics -> {OUT_DIR / 'backtest_v2_metrics.json'}")


if __name__ == "__main__":
    main()
