"""MANTIS performance validation — full 3-layer backtest with synthetic-but-honest multi-source.

We can NOT get true historical Allora inferences (no archive) or historical
sentiment (paid tier). Instead we model each source as a noisy directional
signal calibrated to PUBLISHED reliability levels:

  Source         Accuracy assumption (directional)   Justification
  Allora BTC     65%                                  Allora claims 60-70% historically
  Whale flow     60%                                  Empirical "smart money lags by ~1h"
  Sentiment      derived from past 24h/7d returns     SAME formula as live source

Layer 1 (regime classifier + strategy) is REAL — uses trained model.
Layer 2 (composite voter) is REAL — uses CompositeVoter logic.
Layer 3 (reconciliation) is REAL — same logic as live signal.py.

3 architectures compared (same costs, same risk model):

  A) OLD baseline      = ML direction predictor + ADX filter (binary XGBoost)
  B) Layer 1 only      = regime classifier + strategy router (no multi-source)
  C) FULL MANTIS 3L    = regime + strategies + multi-source vote + reconciliation

Outputs:
  data/backtest_3l_metrics.json
  data/backtest_3l_equity.png       (3 curves on one chart)
  data/backtest_3l_trades_{A,B,C}.csv
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

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
REGIME_MODEL = ROOT / "models" / "xgb_regime.pkl"
OUT_DIR = ROOT / "data"

# ─── Trading params ────────────────────────────────────────────────────────
INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
LOOKAHEAD = 8
SL_MULT = 1.0
REWARD_MULT = 1.46
ANNUALISATION_FACTOR = 24 * 365

# Arch A (OLD)
OLD_BUY_TH, OLD_SELL_TH, OLD_MIN_ADX = 0.65, 0.60, 20.0

# Arch B + C (regime forward gate)
NEW_FORWARD_CONF_TH = 0.65

# Arch C (multi-source synthetic accuracy)
ALLORA_ACC = 0.65       # directional accuracy on next 8h sign
WHALE_ACC = 0.60        # directional accuracy on next 4h sign
RNG_SEED = 42

# Composite gates
COMPOSITE_BULL_TH = 0.20
COMPOSITE_BEAR_TH = -0.20
MIN_AGREEING = 2

# Strategy router (same as production)
REGIME_TO_STRATEGY = {
    "Trending_Up": MomentumStrategy(),
    "Trending_Down": MomentumStrategy(),
    "Ranging": MeanReversionStrategy(),
    "High_Volatility": DefensiveStrategy(),
}


# ─── Shared trade simulator ────────────────────────────────────────────────


def simulate_trade(
    i: int, df: pd.DataFrame, direction: int, entry: float, atr: float, equity: float
) -> tuple[float, str, float, float]:
    sl_dist = atr * SL_MULT
    notional = (equity * RISK_PER_TRADE) * (entry / sl_dist)
    notional = min(notional, equity * 0.95)
    window = df.iloc[i + 1 : i + LOOKAHEAD + 1]
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
    gross = notional * pnl_pct
    fee = notional * SWAP_FEE * 2
    return gross - fee, outcome, exit_price, notional


# ─── Architecture A: OLD direction predictor ───────────────────────────────


def backtest_A(df: pd.DataFrame, proba_old: np.ndarray) -> tuple[list[dict], pd.Series]:
    sell_p, buy_p = proba_old[:, 0], proba_old[:, 1]
    equity, trades, curve = INITIAL_EQUITY, [], []
    i = 0
    while i < len(df) - LOOKAHEAD:
        curve.append(equity)
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
        pnl, outcome, exit_price, notional = simulate_trade(i, df, direction, entry, atr, equity)
        equity += pnl
        trades.append({
            "bar": i, "arch": "A", "direction": "BUY" if direction > 0 else "SELL",
            "entry": entry, "exit": exit_price, "outcome": outcome,
            "notional": notional, "net_pnl": pnl, "equity_after": equity,
        })
        i += LOOKAHEAD
    curve.append(equity)
    return trades, pd.Series(curve, name="A")


# ─── Architecture B: Layer 1 only (regime + strategy) ──────────────────────


def detect_current(row, adx_strong: float, vol_high: float) -> str:
    if float(row["volatility_10"]) > vol_high:
        return "High_Volatility"
    if float(row["adx"]) > adx_strong:
        return "Trending_Up" if float(row["ema_slope_5"]) > 0 else "Trending_Down"
    return "Ranging"


def backtest_B(
    df: pd.DataFrame, forward_proba: np.ndarray, labels: list[str],
    adx_strong: float, vol_high: float,
) -> tuple[list[dict], pd.Series]:
    equity, trades, curve = INITIAL_EQUITY, [], []
    i = 0
    while i < len(df) - LOOKAHEAD:
        curve.append(equity)
        row = df.iloc[i]
        current = detect_current(row, adx_strong, vol_high)
        idx = int(forward_proba[i].argmax())
        fwd = labels[idx]
        conf = float(forward_proba[i][idx])
        if conf < NEW_FORWARD_CONF_TH:
            i += 1
            continue
        chosen = current if current == fwd else fwd
        strategy = REGIME_TO_STRATEGY[chosen]
        window = df.iloc[max(0, i - 30):i + 1]
        decision: StrategyDecision = strategy.evaluate(window, Position())
        if decision.action not in (Action.BUY, Action.SELL):
            i += 1
            continue
        direction = 1 if decision.action == Action.BUY else -1
        entry, atr = float(row["close"]), float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl, outcome, exit_price, notional = simulate_trade(i, df, direction, entry, atr, equity)
        equity += pnl
        trades.append({
            "bar": i, "arch": "B", "regime": chosen, "direction": decision.action.value,
            "fwd_conf": conf, "entry": entry, "exit": exit_price, "outcome": outcome,
            "notional": notional, "net_pnl": pnl, "equity_after": equity,
        })
        i += LOOKAHEAD
    curve.append(equity)
    return trades, pd.Series(curve, name="B")


# ─── Architecture C: full MANTIS 3-layer with synthetic multi-source ───────


def synthetic_allora_vote(
    fwd_8h_return: float, rng: np.random.Generator
) -> tuple[float, float]:
    """Return (score in [-1,+1], confidence 0..1) emulating Allora w/ 65% acc."""
    true_dir = 1 if fwd_8h_return > 0 else (-1 if fwd_8h_return < 0 else 0)
    if rng.random() < ALLORA_ACC:
        signal_dir = true_dir
    else:
        signal_dir = -true_dir if true_dir != 0 else 0
    # Saturate magnitude proportionally to actual move (capped)
    magnitude = math.tanh(abs(fwd_8h_return) / 0.02)
    return float(signal_dir * magnitude), 0.75


def synthetic_whale_vote(
    fwd_4h_return: float, rng: np.random.Generator
) -> tuple[float, float]:
    """Whale flow with 60% directional accuracy on 4h ahead."""
    true_dir = 1 if fwd_4h_return > 0 else (-1 if fwd_4h_return < 0 else 0)
    if rng.random() < WHALE_ACC:
        signal_dir = true_dir
    else:
        signal_dir = -true_dir if true_dir != 0 else 0
    magnitude = math.tanh(abs(fwd_4h_return) / 0.015)
    confidence = 0.6 + 0.4 * rng.random()
    return float(signal_dir * magnitude), float(confidence)


def sentiment_vote(row: pd.Series) -> tuple[float, float]:
    """Replicates live sentiment source from real past returns at this bar."""
    r1 = float(row.get("return_1", 0.0)) * 100   # convert to pct
    r5 = float(row.get("return_5", 0.0)) * 100
    r10 = float(row.get("return_10", 0.0)) * 100
    score = (
        math.tanh(r1 / 5.0) * 0.40
        + math.tanh(r5 / 15.0) * 0.30
        + math.tanh(r10 / 25.0) * 0.10
    ) / 0.80
    return float(max(-1.0, min(1.0, score))), 0.85


def composite_vote(
    allora: tuple[float, float],
    whale: tuple[float, float],
    sentiment: tuple[float, float],
    weights: dict[str, float] = {"allora": 1.0, "whale": 1.0, "sentiment": 0.7},
) -> tuple[str, float, int]:
    """Apply same logic as live CompositeVoter. Return (direction, score, n_agree)."""
    scores = [
        ("allora", allora[0], allora[1]),
        ("whale", whale[0], whale[1]),
        ("sentiment", sentiment[0], sentiment[1]),
    ]
    num, denom = 0.0, 0.0
    for src, sc, conf in scores:
        w = weights.get(src, 1.0) * conf
        num += sc * w
        denom += w
    composite = num / denom if denom > 0 else 0.0

    signs = [1 if sc > 0.10 else (-1 if sc < -0.10 else 0) for _, sc, _ in scores]
    n_non_neutral = sum(1 for s in signs if s != 0)
    composite_sign = 1 if composite > 0 else (-1 if composite < 0 else 0)
    n_agree = sum(1 for s in signs if s == composite_sign and s != 0)

    if composite >= COMPOSITE_BULL_TH and n_agree >= MIN_AGREEING:
        return "bullish", composite, n_agree
    if composite <= COMPOSITE_BEAR_TH and n_agree >= MIN_AGREEING:
        return "bearish", composite, n_agree
    return "neutral", composite, n_agree


def backtest_C(
    df: pd.DataFrame, forward_proba: np.ndarray, labels: list[str],
    adx_strong: float, vol_high: float,
) -> tuple[list[dict], pd.Series]:
    rng = np.random.default_rng(RNG_SEED)
    equity, trades, curve = INITIAL_EQUITY, [], []
    i = 0
    while i < len(df) - LOOKAHEAD:
        curve.append(equity)
        row = df.iloc[i]

        # ─── Layer 1: regime + strategy ───
        current = detect_current(row, adx_strong, vol_high)
        idx = int(forward_proba[i].argmax())
        fwd = labels[idx]
        fwd_conf = float(forward_proba[i][idx])
        if fwd_conf < NEW_FORWARD_CONF_TH:
            i += 1
            continue
        chosen = current if current == fwd else fwd
        strategy = REGIME_TO_STRATEGY[chosen]
        window = df.iloc[max(0, i - 30):i + 1]
        decision: StrategyDecision = strategy.evaluate(window, Position())
        if decision.action not in (Action.BUY, Action.SELL):
            i += 1
            continue

        # ─── Layer 2: synthetic multi-source vote ───
        future = df.iloc[i + 1 : i + LOOKAHEAD + 1]
        if future.empty:
            i += 1
            continue
        entry = float(row["close"])
        fwd_8h_return = (float(future["close"].iloc[-1]) - entry) / entry
        fwd_4h_idx = min(4, len(future) - 1)
        fwd_4h_return = (float(future["close"].iloc[fwd_4h_idx]) - entry) / entry

        allora_v = synthetic_allora_vote(fwd_8h_return, rng)
        whale_v = synthetic_whale_vote(fwd_4h_return, rng)
        sentiment_v = sentiment_vote(row)
        composite_dir, composite_score, n_agree = composite_vote(allora_v, whale_v, sentiment_v)

        # ─── Layer 3: reconciliation ───
        if composite_dir == "neutral":
            i += 1
            continue
        if decision.action == Action.BUY and composite_dir == "bearish":
            i += 1
            continue
        if decision.action == Action.SELL and composite_dir == "bullish":
            i += 1
            continue

        direction = 1 if decision.action == Action.BUY else -1
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue

        pnl, outcome, exit_price, notional = simulate_trade(i, df, direction, entry, atr, equity)
        equity += pnl
        trades.append({
            "bar": i, "arch": "C", "regime": chosen, "direction": decision.action.value,
            "fwd_conf": fwd_conf, "composite_dir": composite_dir,
            "composite_score": composite_score, "agreeing_sources": n_agree,
            "entry": entry, "exit": exit_price, "outcome": outcome,
            "notional": notional, "net_pnl": pnl, "equity_after": equity,
        })
        i += LOOKAHEAD
    curve.append(equity)
    return trades, pd.Series(curve, name="C")


# ─── Metrics ───────────────────────────────────────────────────────────────


def summarize(trades: list[dict], equity_series: pd.Series, label: str) -> dict:
    if not trades:
        return {"label": label, "trades": 0}
    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["net_pnl"] > 0]
    losses = tdf[tdf["net_pnl"] <= 0]
    pf = (
        float(wins["net_pnl"].sum()) / float(-losses["net_pnl"].sum())
        if not losses.empty and losses["net_pnl"].sum() != 0
        else float("inf")
    )
    eq = equity_series.values
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([0.0])
    sharpe = float((rets.mean() / rets.std()) * np.sqrt(ANNUALISATION_FACTOR)) if rets.std() > 0 else 0.0
    running_peak = np.maximum.accumulate(eq)
    dd = (running_peak - eq) / running_peak
    max_dd = float(dd.max() * 100) if len(dd) > 0 else 0.0
    roi = (float(eq[-1]) / INITIAL_EQUITY - 1.0) * 100
    calmar = (roi / max_dd) if max_dd > 0 else float("inf")
    return {
        "label": label,
        "trades": len(tdf),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": float(len(wins) / len(tdf)),
        "roi_pct": roi,
        "profit_factor": pf,
        "max_drawdown_pct": max_dd,
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
    new_bundle = joblib.load(REGIME_MODEL)
    old_proba = old_bundle["model"].predict_proba(test_df[old_bundle["features"]])
    new_proba = new_bundle["model"].predict_proba(test_df[new_bundle["features"]])

    print("\nBacktest A: OLD direction predictor")
    tA, eqA = backtest_A(test_df, old_proba)
    sA = summarize(tA, eqA, "A) OLD direction predictor")

    print("Backtest B: Layer 1 only (regime + strategy)")
    tB, eqB = backtest_B(
        test_df, new_proba, new_bundle["labels"],
        new_bundle["adx_strong_threshold"], new_bundle["vol_high_threshold"],
    )
    sB = summarize(tB, eqB, "B) Layer 1 (regime + strategy)")

    print("Backtest C: FULL MANTIS 3-Layer with synthetic multi-source")
    tC, eqC = backtest_C(
        test_df, new_proba, new_bundle["labels"],
        new_bundle["adx_strong_threshold"], new_bundle["vol_high_threshold"],
    )
    sC = summarize(tC, eqC, f"C) MANTIS 3-Layer (Allora={ALLORA_ACC:.0%}, Whale={WHALE_ACC:.0%})")

    print()
    for s in (sA, sB, sC):
        print(f"=== {s['label']} ===")
        print(f"  trades       : {s.get('trades', 0)}")
        print(f"  wins / losses: {s.get('wins', 0)} / {s.get('losses', 0)}")
        print(f"  win rate     : {s.get('win_rate', 0) * 100:.2f}%")
        print(f"  ROI          : {s.get('roi_pct', 0):.2f}%")
        print(f"  profit factor: {s.get('profit_factor', 0):.2f}")
        print(f"  max drawdown : {s.get('max_drawdown_pct', 0):.2f}%")
        print(f"  Sharpe (ann.): {s.get('sharpe_annualized', 0):.2f}")
        print(f"  Calmar       : {s.get('calmar', 0):.2f}")
        print(f"  final equity : ${s.get('final_equity', INITIAL_EQUITY):.2f}")
        print()

    pd.DataFrame(tA).to_csv(OUT_DIR / "backtest_3l_trades_A.csv", index=False)
    pd.DataFrame(tB).to_csv(OUT_DIR / "backtest_3l_trades_B.csv", index=False)
    pd.DataFrame(tC).to_csv(OUT_DIR / "backtest_3l_trades_C.csv", index=False)
    (OUT_DIR / "backtest_3l_metrics.json").write_text(json.dumps({
        "A_old_baseline": sA,
        "B_layer1_only": sB,
        "C_full_3layer_mantis": sC,
        "comparison": {
            "C_vs_A_roi_delta_pct": sC.get("roi_pct", 0) - sA.get("roi_pct", 0),
            "C_vs_A_winrate_delta": sC.get("win_rate", 0) - sA.get("win_rate", 0),
            "C_vs_A_sharpe_delta": sC.get("sharpe_annualized", 0) - sA.get("sharpe_annualized", 0),
            "C_vs_B_roi_delta_pct": sC.get("roi_pct", 0) - sB.get("roi_pct", 0),
            "C_vs_B_winrate_delta": sC.get("win_rate", 0) - sB.get("win_rate", 0),
        },
        "synthetic_assumptions": {
            "allora_directional_accuracy": ALLORA_ACC,
            "whale_directional_accuracy": WHALE_ACC,
            "sentiment_method": "live source formula on real past returns",
            "rng_seed": RNG_SEED,
        },
    }, indent=2))

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(eqA.values, label=sA["label"], color="#1f77b4", linewidth=2)
    ax.plot(eqB.values, label=sB["label"], color="#ff7f0e", linewidth=2)
    ax.plot(eqC.values, label=sC["label"], color="#2ca02c", linewidth=2)
    ax.axhline(INITIAL_EQUITY, color="gray", linestyle="--", alpha=0.5,
               label=f"Initial ${INITIAL_EQUITY:.0f}")
    ax.set_title("MANTIS Performance Validation -- 3 Architectures on MNT/USD")
    ax.set_xlabel("Bars elapsed (1h cadence)")
    ax.set_ylabel("Equity ($)")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "backtest_3l_equity.png", dpi=120)
    print(f"Saved chart   -> {OUT_DIR / 'backtest_3l_equity.png'}")
    print(f"Saved metrics -> {OUT_DIR / 'backtest_3l_metrics.json'}")


if __name__ == "__main__":
    main()
