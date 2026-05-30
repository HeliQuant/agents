"""Historical replay using the ADAPTIVE strategy selector.

Compares against scripts/10 (regime-based static routing). Same dataset,
same execution simulator, same costs, same risk model.

Architecture:
  Static (scripts/10) : regime classifier label -> hard-coded strategy mapping
  Adaptive (this)     : multi-source consensus selects strategy per bar
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

from firm.agents.adaptive_selector import select_strategy  # noqa: E402
from firm.strategies import Action, StrategyDecision  # noqa: E402
from firm.strategies.base import Position  # noqa: E402

DATA_PATH = ROOT / "data" / "mnt_features.csv"
REGIME_MODEL = ROOT / "models" / "xgb_regime.pkl"
OUT_TRADES = ROOT / "data" / "replay_adaptive_trades.csv"
OUT_SUMMARY = ROOT / "data" / "replay_adaptive_summary.json"

INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
LOOKAHEAD = 8
SL_MULT = 1.0
REWARD_MULT = 1.46
FORWARD_CONFIDENCE_THRESHOLD = 0.65
ANNUALISATION_FACTOR = 24 * 365


def simulate_outcome(i, df, direction, entry, atr, equity):
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
    df = pd.read_csv(DATA_PATH).reset_index(drop=True)
    bundle = joblib.load(REGIME_MODEL)
    model = bundle["model"]
    features = bundle["features"]
    labels = bundle["labels"]
    vol_high = bundle["vol_high_threshold"]
    adx_strong = bundle["adx_strong_threshold"]
    proba = model.predict_proba(df[features])

    print(f"Replay (ADAPTIVE selector) on {len(df)} bars")

    equity = INITIAL_EQUITY
    trades: list[dict] = []
    state_counter: dict[str, int] = {}
    i = 30
    fired = 0
    held = 0
    cooldown_until = -1

    while i < len(df) - LOOKAHEAD:
        if i <= cooldown_until:
            i += 1
            continue
        row = df.iloc[i]

        # Forward gate (same as production signal.py)
        best_idx = int(proba[i].argmax())
        best_conf = float(proba[i][best_idx])
        if best_conf < FORWARD_CONFIDENCE_THRESHOLD:
            i += 1
            continue

        prob_dict = {labels[k]: float(proba[i][k]) for k in range(len(labels))}
        sel = select_strategy(
            row, prob_dict,
            vol_high_threshold=vol_high,
            adx_strong_threshold=adx_strong,
        )
        state_counter[sel.market_state] = state_counter.get(sel.market_state, 0) + 1

        window = df.iloc[max(0, i - 30):i + 1]
        decision: StrategyDecision = sel.strategy.evaluate(window, Position())

        if decision.action not in (Action.BUY, Action.SELL):
            held += 1
            i += 1
            continue

        fired += 1
        direction = 1 if decision.action == Action.BUY else -1
        entry = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl, outcome, exit_price, notional, bars_held = simulate_outcome(
            i, df, direction, entry, atr, equity
        )
        equity += pnl
        trades.append({
            "bar_idx": i,
            "datetime": row.get("datetime"),
            "market_state": sel.market_state,
            "selection_reason": sel.reason,
            "strategy": sel.strategy.name,
            "fwd_conf": round(best_conf, 4),
            "direction": decision.action.value,
            "entry": entry, "exit": exit_price, "outcome": outcome,
            "bars_held": bars_held, "notional": round(notional, 2),
            "net_pnl": round(pnl, 4), "equity_after": round(equity, 2),
        })
        cooldown_until = i + LOOKAHEAD
        i += 1

    pd.DataFrame(trades).to_csv(OUT_TRADES, index=False)
    if not trades:
        OUT_SUMMARY.write_text(json.dumps({"trades": 0}, indent=2))
        print("No trades fired.")
        return

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["net_pnl"] > 0]
    losses = tdf[tdf["net_pnl"] <= 0]
    eq_series = pd.concat([pd.Series([INITIAL_EQUITY]), tdf["equity_after"]], ignore_index=True)
    running_peak = np.maximum.accumulate(eq_series.values)
    dd = (running_peak - eq_series.values) / running_peak
    max_dd = float(dd.max() * 100)
    pf = (
        float(wins["net_pnl"].sum()) / float(-losses["net_pnl"].sum())
        if not losses.empty and losses["net_pnl"].sum() != 0 else float("inf")
    )
    rets = (eq_series.diff() / eq_series.shift()).dropna()
    sharpe = float((rets.mean() / rets.std()) * np.sqrt(ANNUALISATION_FACTOR)) if rets.std() > 0 else 0.0
    roi = (equity / INITIAL_EQUITY - 1) * 100

    summary = {
        "trades": len(tdf), "fired_signals": fired, "held_signals": held,
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": float(len(wins) / len(tdf) * 100),
        "roi_pct": roi, "profit_factor": pf, "max_drawdown_pct": max_dd,
        "sharpe_annualized": sharpe, "final_equity": round(equity, 2),
        "market_state_distribution": state_counter,
        "by_strategy": tdf.groupby("strategy").agg(
            trades=("net_pnl", "count"), wins=("net_pnl", lambda s: int((s > 0).sum())),
            avg_pnl=("net_pnl", "mean"), total_pnl=("net_pnl", "sum"),
        ).round(3).to_dict(orient="index"),
        "by_market_state": tdf.groupby("market_state").agg(
            trades=("net_pnl", "count"), wins=("net_pnl", lambda s: int((s > 0).sum())),
            total_pnl=("net_pnl", "sum"),
        ).round(3).to_dict(orient="index"),
        "by_outcome": tdf["outcome"].value_counts().to_dict(),
    }
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2, default=str))

    print()
    print("=== ADAPTIVE REPLAY RESULTS ===")
    print(f"  Trades            : {len(tdf)}")
    print(f"  Wins / Losses     : {summary['wins']} / {summary['losses']}")
    print(f"  Win rate          : {summary['win_rate_pct']:.2f}%")
    print(f"  ROI               : {roi:+.2f}%")
    print(f"  Profit factor     : {pf:.2f}")
    print(f"  Max drawdown      : {max_dd:.2f}%")
    print(f"  Sharpe (annual)   : {sharpe:.2f}")
    print(f"  Final equity      : ${equity:.2f}")
    print(f"  Outcome           : {summary['by_outcome']}")
    print(f"  Market states     : {state_counter}")
    print(f"  By strategy       : {summary['by_strategy']}")
    print()
    print(f"Saved -> {OUT_TRADES}")
    print(f"Saved -> {OUT_SUMMARY}")


if __name__ == "__main__":
    main()
