"""Multi-asset pipeline: BTC + MNT data, features, regime classifier, replay.

This consolidates what was 5 scripts into one parameterized runner so we can
add new assets (BTC, ETH, etc.) quickly.

Steps per asset:
  1. Fetch 90-day hourly OHLCV from CoinGecko
  2. Build features (same pipeline as scripts/02 — kept consistent across assets)
  3. Label regimes + train XGBoost regime classifier
  4. Run replay using V5 production architecture (momentum disabled during MNT-like
     bearish phase; lifecycle activates when 30d macro trend confirms)
  5. Save summary

Usage:
    python scripts/multi_asset.py bitcoin BTC
    python scripts/multi_asset.py mantle  MNT
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import requests
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

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

DATA_DIR = ROOT / "data"
MODELS_DIR = ROOT / "models"
INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
LOOKAHEAD = 8
SL_MULT = 1.0
REWARD_MULT = 1.46
FORWARD_CONFIDENCE_THRESHOLD = 0.65
ANNUALISATION_FACTOR = 24 * 365

FEATURES = [
    "ema20", "ema50", "ema100", "ema_slope_5", "ema_slope_10",
    "ema_gap_20_50", "ema_gap_50_100",
    "distance_ema20", "distance_ema50", "distance_ema100",
    "rsi", "atr", "adx",
    "momentum_3", "momentum_5", "momentum_10",
    "momentum_3_atr", "momentum_5_atr", "momentum_10_atr",
    "return_1", "return_3", "return_5", "return_10",
    "volatility_10", "volatility_20", "volatility_ratio",
    "volume_change", "volume_ratio",
    "ema_cross", "price_above_ema20", "price_above_ema50", "price_above_ema100",
    "rsi_above_50", "rsi_overbought", "rsi_oversold",
    "hour_utc", "asia_hours", "europe_hours", "us_hours",
]
REGIME_LABELS = ["Ranging", "Trending_Up", "Trending_Down", "High_Volatility"]
LABEL_TO_INT = {n: i for i, n in enumerate(REGIME_LABELS)}
FORECAST_HORIZON = 4

def build_strategy_map(adx_strong_threshold: float) -> dict:
    """Per-asset strategy params: momentum.adx_min = asset's own p60 ADX threshold.
    Hard-coded adx_min=60 was tuned for MNT and too strict for BTC/mETH/etc."""
    momentum = MomentumStrategy(adx_min=max(adx_strong_threshold, 25.0))
    return {
        "Trending_Up": momentum,
        "Trending_Down": momentum,
        "Ranging": MeanReversionStrategy(),
        "High_Volatility": DefensiveStrategy(),
    }


REGIME_TO_STRATEGY = build_strategy_map(60.0)  # default for MNT


# ─── 1. Data ───────────────────────────────────────────────────────────────


def fetch_hourly(coin_id: str, days: int = 90) -> pd.DataFrame:
    r = requests.get(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": str(days)},
        timeout=20,
        headers={"User-Agent": "mantle-mantis/0.1"},
    )
    r.raise_for_status()
    payload = r.json()
    vols = {ts: v for ts, v in payload["total_volumes"]}
    rows = [
        {
            "timestamp": int(ts),
            "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
            "close": float(c),
            "volume": float(vols.get(ts, 0.0)),
        }
        for ts, c in payload["prices"]
    ]
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = df["close"].rolling(3, min_periods=1).max()
    df["low"] = df["close"].rolling(3, min_periods=1).min()
    return df


# ─── 2. Features (same pipeline as scripts/02 — kept identical) ────────────


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema100"] = out["close"].ewm(span=100, adjust=False).mean()

    delta = out["close"].diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain_s = pd.Series(gain, index=out.index).rolling(14).mean()
    loss_s = pd.Series(loss, index=out.index).rolling(14).mean()
    out["rsi"] = 100 - (100 / (1 + gain_s / (loss_s + 1e-12)))

    high_low = out["high"] - out["low"]
    high_close = (out["high"] - out["close"].shift()).abs()
    low_close = (out["low"] - out["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    out["atr"] = tr.rolling(14).mean()

    plus_dm = (out["high"].diff()).clip(lower=0)
    minus_dm = (-out["low"].diff()).clip(lower=0)
    plus_dm = np.where(plus_dm > minus_dm, plus_dm, 0)
    minus_dm = np.where(minus_dm > plus_dm, minus_dm, 0)
    plus_di = 100 * pd.Series(plus_dm, index=out.index).rolling(14).mean() / (out["atr"] + 1e-12)
    minus_di = 100 * pd.Series(minus_dm, index=out.index).rolling(14).mean() / (out["atr"] + 1e-12)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-12)
    out["adx"] = dx.rolling(14).mean()

    for n in (3, 5, 10):
        out[f"momentum_{n}"] = out["close"] - out["close"].shift(n)
        out[f"momentum_{n}_atr"] = out[f"momentum_{n}"] / (out["atr"] + 1e-12)

    out["ema_slope_5"] = out["ema20"] - out["ema20"].shift(5)
    out["ema_slope_10"] = out["ema20"] - out["ema20"].shift(10)
    out["ema_gap_20_50"] = (out["ema20"] - out["ema50"]) / (out["ema50"] + 1e-12)
    out["ema_gap_50_100"] = (out["ema50"] - out["ema100"]) / (out["ema100"] + 1e-12)
    out["distance_ema20"] = (out["close"] - out["ema20"]) / (out["ema20"] + 1e-12)
    out["distance_ema50"] = (out["close"] - out["ema50"]) / (out["ema50"] + 1e-12)
    out["distance_ema100"] = (out["close"] - out["ema100"]) / (out["ema100"] + 1e-12)

    out["volatility_10"] = out["close"].rolling(10).std()
    out["volatility_20"] = out["close"].rolling(20).std()
    out["volatility_ratio"] = out["volatility_10"] / (out["volatility_20"] + 1e-12)

    for n in (1, 3, 5, 10):
        out[f"return_{n}"] = out["close"].pct_change(n)

    out["volume_change"] = out["volume"].pct_change()
    out["volume_ma20"] = out["volume"].rolling(20).mean()
    out["volume_ratio"] = out["volume"] / (out["volume_ma20"] + 1e-12)

    out["ema_cross"] = (out["ema20"] > out["ema50"]).astype(int)
    out["price_above_ema20"] = (out["close"] > out["ema20"]).astype(int)
    out["price_above_ema50"] = (out["close"] > out["ema50"]).astype(int)
    out["price_above_ema100"] = (out["close"] > out["ema100"]).astype(int)
    out["rsi_above_50"] = (out["rsi"] > 50).astype(int)
    out["rsi_overbought"] = (out["rsi"] > 70).astype(int)
    out["rsi_oversold"] = (out["rsi"] < 30).astype(int)

    out["datetime"] = pd.to_datetime(out["datetime"], format="ISO8601", utc=True, errors="coerce")
    out["hour_utc"] = out["datetime"].dt.hour
    out["asia_hours"] = ((out["hour_utc"] >= 0) & (out["hour_utc"] < 8)).astype(int)
    out["europe_hours"] = ((out["hour_utc"] >= 8) & (out["hour_utc"] < 16)).astype(int)
    out["us_hours"] = ((out["hour_utc"] >= 16) & (out["hour_utc"] < 24)).astype(int)
    return out


# ─── 3. Regime label + classifier ──────────────────────────────────────────


def label_regimes(df: pd.DataFrame, adx_th: float, vol_th: float) -> pd.Series:
    labels = pd.Series(index=df.index, dtype="object")
    high_vol = df["volatility_10"] > vol_th
    strong_trend = (df["adx"] > adx_th) & ~high_vol
    up = df["ema_slope_5"] > 0
    down = df["ema_slope_5"] < 0
    labels[high_vol] = "High_Volatility"
    labels[strong_trend & up] = "Trending_Up"
    labels[strong_trend & down] = "Trending_Down"
    labels[labels.isna()] = "Ranging"
    return labels


def train_classifier(df: pd.DataFrame) -> dict:
    df = df.dropna().reset_index(drop=True)
    split = int(len(df) * 0.8)
    train_df = df.iloc[:split].reset_index(drop=True)
    test_df = df.iloc[split:].reset_index(drop=True)

    adx_th = float(train_df["adx"].quantile(0.60))
    vol_th = float(train_df["volatility_10"].quantile(0.85))

    train_df["regime_now"] = label_regimes(train_df, adx_th, vol_th)
    test_df["regime_now"] = label_regimes(test_df, adx_th, vol_th)
    train_df["regime_future"] = train_df["regime_now"].shift(-FORECAST_HORIZON)
    test_df["regime_future"] = test_df["regime_now"].shift(-FORECAST_HORIZON)
    train_df = train_df.dropna(subset=["regime_future"]).reset_index(drop=True)
    test_df = test_df.dropna(subset=["regime_future"]).reset_index(drop=True)

    X_train = train_df[FEATURES]
    y_train = train_df["regime_future"].map(LABEL_TO_INT).astype(int)
    X_test = test_df[FEATURES]
    y_test = test_df["regime_future"].map(LABEL_TO_INT).astype(int)

    model = XGBClassifier(
        n_estimators=400, max_depth=5, learning_rate=0.025,
        subsample=0.85, colsample_bytree=0.85, min_child_weight=4,
        gamma=0.1, reg_alpha=0.1, reg_lambda=1.0,
        objective="multi:softprob", num_class=len(REGIME_LABELS),
        eval_metric="mlogloss", random_state=42, n_jobs=-1,
    )
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    acc = accuracy_score(y_test, pred)

    return {
        "model": model, "features": FEATURES, "labels": REGIME_LABELS,
        "adx_strong_threshold": adx_th, "vol_high_threshold": vol_th,
        "forecast_horizon_bars": FORECAST_HORIZON,
        "test_accuracy": float(acc),
    }


# ─── 4. Replay (V5 production: momentum disabled in pending macro) ─────────


def simulate_outcome(i, df, direction, entry, atr, equity):
    sl_dist = atr * SL_MULT
    notional = (equity * RISK_PER_TRADE) * (entry / sl_dist)
    notional = min(notional, equity * 0.95)
    window = df.iloc[i + 1 : i + LOOKAHEAD + 1]
    if window.empty:
        return 0.0, "TIMEOUT", entry, notional
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
        outcome, exit_price = ("TP", tp) if hit_tp.index[0] < hit_sl.index[0] else ("SL", sl)
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


def detect_current(row, adx_th, vol_th):
    if float(row["volatility_10"]) > vol_th:
        return "High_Volatility"
    if float(row["adx"]) > adx_th:
        return "Trending_Up" if float(row["ema_slope_5"]) > 0 else "Trending_Down"
    return "Ranging"


def replay(df: pd.DataFrame, bundle: dict) -> dict:
    df = df.dropna().reset_index(drop=True)
    model = bundle["model"]
    proba = model.predict_proba(df[bundle["features"]])
    labels = bundle["labels"]
    adx_th = bundle["adx_strong_threshold"]
    vol_th = bundle["vol_high_threshold"]

    # Per-asset momentum calibration
    strategy_map = build_strategy_map(adx_th)

    equity = INITIAL_EQUITY
    trades = []
    momentum_activations = {"long": 0, "short": 0, "pending": 0}
    i = 30
    cooldown = -1

    while i < len(df) - LOOKAHEAD:
        if i <= cooldown:
            i += 1
            continue
        row = df.iloc[i]
        idx = int(proba[i].argmax())
        conf = float(proba[i][idx])
        if conf < FORWARD_CONFIDENCE_THRESHOLD:
            i += 1
            continue

        current = detect_current(row, adx_th, vol_th)
        fwd = labels[idx]
        if current == "High_Volatility":
            chosen = "High_Volatility"
        elif current == fwd:
            chosen = current
        else:
            chosen = fwd

        # Strategy Lifecycle for Momentum
        lifecycle = assess_momentum_activation(df.iloc[max(0, i - 60):i + 1])
        if chosen in ("Trending_Up", "Trending_Down"):
            if lifecycle.momentum_status == LifecycleStatus.PENDING:
                chosen = "High_Volatility"  # defensive fallback
                momentum_activations["pending"] += 1
            elif chosen == "Trending_Up" and lifecycle.momentum_status == LifecycleStatus.ACTIVE_SHORT:
                chosen = "High_Volatility"
            elif chosen == "Trending_Down" and lifecycle.momentum_status == LifecycleStatus.ACTIVE_LONG:
                chosen = "High_Volatility"
            else:
                momentum_activations["long" if "_Up" in chosen else "short"] += 1
        elif chosen == "Ranging" and not lifecycle.mean_reversion_active:
            # Mean reversion lifecycle gate: don't fade extremes during a strong
            # macro trend (avoids catching a falling knife on trending assets).
            chosen = "High_Volatility"

        strategy = strategy_map[chosen]
        window = df.iloc[max(0, i - 30):i + 1]
        decision: StrategyDecision = strategy.evaluate(window, Position())
        if decision.action not in (Action.BUY, Action.SELL):
            i += 1
            continue

        direction = 1 if decision.action == Action.BUY else -1
        entry = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl, outcome, exit_price, notional = simulate_outcome(i, df, direction, entry, atr, equity)
        equity += pnl
        trades.append({
            "bar_idx": i, "regime_current": current, "regime_forward": fwd,
            "strategy": strategy.name, "lifecycle": lifecycle.momentum_status.value,
            "direction": decision.action.value, "entry": entry, "exit": exit_price,
            "outcome": outcome, "notional": round(notional, 2),
            "net_pnl": round(pnl, 4), "equity_after": round(equity, 2),
        })
        cooldown = i + LOOKAHEAD
        i += 1

    if not trades:
        return {"trades": 0}

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["net_pnl"] > 0]
    losses = tdf[tdf["net_pnl"] <= 0]
    eq = np.concatenate([[INITIAL_EQUITY], tdf["equity_after"].values])
    running_peak = np.maximum.accumulate(eq)
    dd = (running_peak - eq) / running_peak
    max_dd_pct = float(dd.max() * 100)
    pf = (
        float(wins["net_pnl"].sum()) / float(-losses["net_pnl"].sum())
        if not losses.empty and losses["net_pnl"].sum() != 0 else float("inf")
    )
    rets = np.diff(eq) / eq[:-1]
    sharpe = float((rets.mean() / rets.std()) * np.sqrt(ANNUALISATION_FACTOR)) if rets.std() > 0 else 0.0

    return {
        "trades": len(tdf), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": float(len(wins) / len(tdf) * 100),
        "roi_pct": (float(eq[-1]) / INITIAL_EQUITY - 1) * 100,
        "profit_factor": pf, "max_drawdown_pct": max_dd_pct,
        "sharpe_annualized": sharpe, "final_equity": float(eq[-1]),
        "momentum_activations": momentum_activations,
        "by_strategy": tdf.groupby("strategy").agg(
            trades=("net_pnl", "count"), wins=("net_pnl", lambda s: int((s > 0).sum())),
            total_pnl=("net_pnl", "sum"),
        ).round(3).to_dict(orient="index"),
    }


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("coin_id", help="CoinGecko coin id e.g. 'bitcoin', 'mantle'")
    parser.add_argument("ticker", help="Short label e.g. 'BTC', 'MNT'")
    args = parser.parse_args()

    print(f"[1/4] Fetching {args.ticker} hourly data...")
    raw = fetch_hourly(args.coin_id)
    print(f"  rows: {len(raw)}")
    raw_path = DATA_DIR / f"{args.ticker.lower()}_hourly.csv"
    raw.to_csv(raw_path, index=False)
    print(f"  saved -> {raw_path}")

    print(f"[2/4] Building features for {args.ticker}...")
    feat = add_features(raw)
    print(f"  rows after features (pre-dropna): {len(feat)}")
    feat_path = DATA_DIR / f"{args.ticker.lower()}_features.csv"
    feat.to_csv(feat_path, index=False)

    print(f"[3/4] Training regime classifier for {args.ticker}...")
    bundle = train_classifier(feat)
    print(f"  test accuracy: {bundle['test_accuracy']*100:.2f}%")
    print(f"  adx_strong_threshold p60: {bundle['adx_strong_threshold']:.4f}")
    print(f"  vol_high_threshold p85:   {bundle['vol_high_threshold']:.6f}")
    model_path = MODELS_DIR / f"xgb_regime_{args.ticker.lower()}.pkl"
    joblib.dump(bundle, model_path)
    print(f"  saved -> {model_path}")

    print(f"[4/4] Running replay for {args.ticker} (V5: momentum lifecycle)...")
    summary = replay(feat, bundle)
    print()
    if summary.get("trades", 0) == 0:
        print(f"  No trades fired.")
        return
    print(f"=== {args.ticker} V5 REPLAY RESULTS ===")
    print(f"  Trades       : {summary['trades']}")
    print(f"  Wins/Losses  : {summary['wins']} / {summary['losses']}")
    print(f"  Win rate     : {summary['win_rate_pct']:.2f}%")
    print(f"  ROI          : {summary['roi_pct']:+.2f}%")
    print(f"  Profit factor: {summary['profit_factor']:.2f}")
    print(f"  Max drawdown : {summary['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe (ann.): {summary['sharpe_annualized']:.2f}")
    print(f"  Final equity : ${summary['final_equity']:.2f}")
    print(f"  Momentum activations: {summary['momentum_activations']}")
    print(f"  By strategy: {summary['by_strategy']}")

    summary_path = DATA_DIR / f"replay_{args.ticker.lower()}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nsaved -> {summary_path}")


if __name__ == "__main__":
    main()
