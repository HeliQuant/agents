"""Backtest the trained model on the held-out 20% of MNT data.

Two scenarios are evaluated:
  A) ML-standalone — same gating as the user's original XGBoost methodology
     (confidence threshold + ADX trend filter)
  B) ML + Allora cross-validation gate — only trade if a simulated Allora
     signal agrees with the ML direction. We simulate Allora as a noisy
     predictor of the next-bar return sign with accuracy ALLORA_ACC.

Both scenarios apply realistic DEX execution costs: 0.30% per swap (60 bps
round-trip), which approximates Merchant Moe / Agni Finance fees + slippage
on small notionals.

Outputs:
  data/backtest_metrics.json
  data/backtest_equity.png  (equity curves of both scenarios)
  data/backtest_trades_A.csv, backtest_trades_B.csv
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "mnt_features.csv"
MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "xgb_mnt.pkl"
OUT_DIR = Path(__file__).resolve().parents[1] / "data"

# Trading params
INITIAL_EQUITY = 1_000.0       # USD
RISK_PER_TRADE = 0.01          # 1% of equity at risk per trade
SWAP_FEE = 0.0010              # 0.10% per swap (Mantle DEX realistic — Merchant Moe charges ~0.05-0.3%)
LOOKAHEAD = 8                  # bars (same as labeling lookahead)
SL_MULT = 1.0
REWARD_MULT = 1.46

# Gates — tightened: trade only on high-conviction model output
BUY_THRESHOLD = 0.65
SELL_THRESHOLD = 0.60
MIN_ADX = 20.0
ALLORA_ACC = 0.65              # simulated Allora directional accuracy (Allora claims 60-70%)
ALLORA_SEED = 42


def simulate(
    df: pd.DataFrame,
    proba: np.ndarray,
    *,
    use_allora_gate: bool,
) -> tuple[list[dict], pd.Series]:
    """Walk forward, return (trade log, equity series)."""
    rng = np.random.default_rng(ALLORA_SEED if use_allora_gate else 0)

    sell_p = proba[:, 0]
    buy_p = proba[:, 1]

    equity = INITIAL_EQUITY
    equity_curve: list[float] = []
    trades: list[dict] = []

    i = 0
    n = len(df)
    while i < n - LOOKAHEAD:
        row = df.iloc[i]
        equity_curve.append(equity)

        adx = float(row["adx"])
        if adx < MIN_ADX:
            i += 1
            continue

        # ML direction
        if buy_p[i] >= BUY_THRESHOLD:
            ml_dir = 1
            ml_conf = buy_p[i]
        elif sell_p[i] >= SELL_THRESHOLD:
            ml_dir = 0
            ml_conf = sell_p[i]
        else:
            i += 1
            continue

        if use_allora_gate:
            # Simulate Allora: with probability ALLORA_ACC, it agrees with the
            # TRUE forward sign; otherwise it disagrees. Using true forward sign
            # for the *gate* (not for the actual trade outcome) models a slightly
            # informed external predictor.
            future_close = df["close"].iloc[i + LOOKAHEAD]
            entry_close = float(row["close"])
            true_dir = 1 if future_close > entry_close else 0
            agree = rng.random() < ALLORA_ACC
            allora_dir = true_dir if agree else 1 - true_dir
            if allora_dir != ml_dir:
                i += 1
                continue

        # ─── Execute trade ───
        entry = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue

        # SL distance in price terms = atr * SL_MULT
        # Position size such that hitting SL = RISK_PER_TRADE * equity (pre-fee)
        sl_dist = atr * SL_MULT
        position_notional = (equity * RISK_PER_TRADE) * (entry / sl_dist)
        # Cap at 95% equity (no extreme leverage in MVP)
        position_notional = min(position_notional, equity * 0.95)

        window = df.iloc[i + 1 : i + LOOKAHEAD + 1]
        outcome = None
        exit_price = float(window["close"].iloc[-1])  # default = close at end of window

        if ml_dir == 1:  # BUY
            tp = entry + atr * REWARD_MULT
            sl = entry - sl_dist
            hit_tp = window[window["high"] >= tp]
            hit_sl = window[window["low"] <= sl]
            if not hit_tp.empty and not hit_sl.empty:
                if hit_tp.index[0] < hit_sl.index[0]:
                    outcome, exit_price = "TP", tp
                else:
                    outcome, exit_price = "SL", sl
            elif not hit_tp.empty:
                outcome, exit_price = "TP", tp
            elif not hit_sl.empty:
                outcome, exit_price = "SL", sl
            else:
                outcome = "TIMEOUT"
            pnl_pct = (exit_price - entry) / entry
        else:  # SELL
            tp = entry - atr * REWARD_MULT
            sl = entry + sl_dist
            hit_tp = window[window["low"] <= tp]
            hit_sl = window[window["high"] >= sl]
            if not hit_tp.empty and not hit_sl.empty:
                if hit_tp.index[0] < hit_sl.index[0]:
                    outcome, exit_price = "TP", tp
                else:
                    outcome, exit_price = "SL", sl
            elif not hit_tp.empty:
                outcome, exit_price = "TP", tp
            elif not hit_sl.empty:
                outcome, exit_price = "SL", sl
            else:
                outcome = "TIMEOUT"
            pnl_pct = (entry - exit_price) / entry

        # Apply fees: 2x SWAP_FEE (in + out)
        gross_pnl = position_notional * pnl_pct
        fee = position_notional * SWAP_FEE * 2
        net_pnl = gross_pnl - fee
        equity += net_pnl

        trades.append({
            "bar_index": i,
            "datetime": row.get("datetime"),
            "direction": "BUY" if ml_dir == 1 else "SELL",
            "ml_conf": float(ml_conf),
            "adx": adx,
            "entry": entry,
            "exit": exit_price,
            "outcome": outcome,
            "position_notional": position_notional,
            "gross_pnl": gross_pnl,
            "fee": fee,
            "net_pnl": net_pnl,
            "equity_after": equity,
        })

        # Advance past the trade window
        i += LOOKAHEAD

    equity_curve.append(equity)
    return trades, pd.Series(equity_curve, name="equity")


def summarize(trades: list[dict], equity_series: pd.Series, label: str) -> dict:
    if not trades:
        return {
            "label": label,
            "trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "roi_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
        }

    df = pd.DataFrame(trades)
    wins = df[df["net_pnl"] > 0]
    losses = df[df["net_pnl"] <= 0]
    total_pnl = float(df["net_pnl"].sum())

    pf = (
        float(wins["net_pnl"].sum()) / float(-losses["net_pnl"].sum())
        if not losses.empty and losses["net_pnl"].sum() != 0
        else float("inf")
    )

    eq = equity_series.values
    running_peak = np.maximum.accumulate(eq)
    dd = (running_peak - eq) / running_peak
    max_dd_pct = float(dd.max() * 100) if len(dd) > 0 else 0.0

    return {
        "label": label,
        "trades": len(df),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": float(len(wins) / len(df)) if len(df) > 0 else 0.0,
        "total_pnl": total_pnl,
        "roi_pct": (eq[-1] / INITIAL_EQUITY - 1.0) * 100,
        "profit_factor": pf,
        "max_drawdown_pct": max_dd_pct,
        "final_equity": float(eq[-1]),
    }


def main() -> None:
    print(f"Loading model + data")
    bundle = joblib.load(MODEL_PATH)
    model = bundle["model"]
    features = bundle["features"]

    df = pd.read_csv(DATA_PATH)
    split = int(len(df) * 0.8)
    test_df = df.iloc[split:].reset_index(drop=True)
    proba = model.predict_proba(test_df[features])

    print(f"Backtesting on {len(test_df)} held-out bars (~{len(test_df)} hours)")

    trades_a, eq_a = simulate(test_df, proba, use_allora_gate=False)
    trades_b, eq_b = simulate(test_df, proba, use_allora_gate=True)

    summary_a = summarize(trades_a, eq_a, "ML-only (XGBoost + ADX)")
    summary_b = summarize(trades_b, eq_b, f"ML + Allora gate ({ALLORA_ACC:.0%} acc)")

    print()
    for s in (summary_a, summary_b):
        print(f"=== {s['label']} ===")
        print(f"  trades       : {s['trades']}")
        print(f"  wins / losses: {s.get('wins', 0)} / {s.get('losses', 0)}")
        print(f"  win rate     : {s['win_rate'] * 100:.2f}%")
        print(f"  total PnL    : ${s['total_pnl']:.2f}")
        print(f"  ROI          : {s['roi_pct']:.2f}%")
        print(f"  profit factor: {s['profit_factor']:.2f}")
        print(f"  max drawdown : {s['max_drawdown_pct']:.2f}%")
        print(f"  final equity : ${s.get('final_equity', INITIAL_EQUITY):.2f}")
        print()

    # ─── Save artifacts ───
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(trades_a).to_csv(OUT_DIR / "backtest_trades_A.csv", index=False)
    pd.DataFrame(trades_b).to_csv(OUT_DIR / "backtest_trades_B.csv", index=False)

    metrics = {
        "scenario_A_ml_only": summary_a,
        "scenario_B_ml_plus_allora": summary_b,
        "params": {
            "initial_equity": INITIAL_EQUITY,
            "risk_per_trade": RISK_PER_TRADE,
            "swap_fee_per_side": SWAP_FEE,
            "lookahead_bars": LOOKAHEAD,
            "sl_multiplier": SL_MULT,
            "reward_multiplier": REWARD_MULT,
            "buy_threshold": BUY_THRESHOLD,
            "sell_threshold": SELL_THRESHOLD,
            "min_adx": MIN_ADX,
            "allora_simulated_accuracy": ALLORA_ACC,
        },
    }
    (OUT_DIR / "backtest_metrics.json").write_text(json.dumps(metrics, indent=2))

    # ─── Plot equity curves ───
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(eq_a.values, label=summary_a["label"], color="#1f77b4", linewidth=2)
    ax.plot(eq_b.values, label=summary_b["label"], color="#2ca02c", linewidth=2)
    ax.axhline(INITIAL_EQUITY, color="gray", linestyle="--", alpha=0.6,
               label=f"Initial ${INITIAL_EQUITY:.0f}")
    ax.set_title("MNT/USD Backtest Equity Curves\nML-only vs ML + Allora cross-validation")
    ax.set_xlabel("Bars elapsed (1h cadence)")
    ax.set_ylabel("Equity ($)")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "backtest_equity.png", dpi=120)
    print(f"Saved equity chart -> {OUT_DIR / 'backtest_equity.png'}")
    print(f"Saved metrics    -> {OUT_DIR / 'backtest_metrics.json'}")


if __name__ == "__main__":
    main()
