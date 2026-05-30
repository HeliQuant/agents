"""Build feature matrix from raw MNT hourly bars.

This is an adaptation of the user's AI-for-Trading feature engineering (XAUUSD/MT5 →
crypto MNT/USD). Forex session features are dropped (crypto is 24/7); features
that strictly need true intra-bar high/low (Stoch %K, classic ATR, wick anatomy)
are replaced with close-only equivalents:
  - ATR → rolling std of true range using our synthetic OHLC
  - Stoch → percentile-rank over rolling window
  - Wick/body anatomy → dropped (no true intra-bar data)

We also label each bar with the forward SL/TP simulation methodology from the
user's original script — only labels that have a clear winner in the lookahead
window get a target; ambiguous bars are dropped.

Usage:
    python scripts/02_feature_engineering.py
Output:
    data/mnt_features.csv
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

IN_PATH = Path(__file__).resolve().parents[1] / "data" / "mnt_hourly.csv"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "mnt_features.csv"

LOOKAHEAD = 8           # bars (= 8 hours)
SL_MULTIPLIER = 1.0     # x rolling-std distance
REWARD_RATIO = 1.46     # same as user's original
MIN_ADX = 20.0


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # Trend
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema100"] = out["close"].ewm(span=100, adjust=False).mean()

    # RSI
    delta = out["close"].diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain_series = pd.Series(gain, index=out.index).rolling(14).mean()
    loss_series = pd.Series(loss, index=out.index).rolling(14).mean()
    rs = gain_series / (loss_series + 1e-12)
    out["rsi"] = 100 - (100 / (1 + rs))

    # ATR (using synthetic OHLC — high/low are 3-bar rolling extrema)
    high_low = out["high"] - out["low"]
    high_close = (out["high"] - out["close"].shift()).abs()
    low_close = (out["low"] - out["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    out["atr"] = true_range.rolling(14).mean()

    # ADX (close-derived approximation)
    plus_dm = (out["high"].diff()).clip(lower=0)
    minus_dm = (-out["low"].diff()).clip(lower=0)
    plus_dm = np.where(plus_dm > minus_dm, plus_dm, 0)
    minus_dm = np.where(minus_dm > plus_dm, minus_dm, 0)
    plus_di = 100 * pd.Series(plus_dm, index=out.index).rolling(14).mean() / (
        out["atr"] + 1e-12
    )
    minus_di = 100 * pd.Series(minus_dm, index=out.index).rolling(14).mean() / (
        out["atr"] + 1e-12
    )
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-12)
    out["adx"] = dx.rolling(14).mean()

    # Momentum (close)
    for n in (3, 5, 10):
        out[f"momentum_{n}"] = out["close"] - out["close"].shift(n)
        out[f"momentum_{n}_atr"] = out[f"momentum_{n}"] / (out["atr"] + 1e-12)

    # EMA derived
    out["ema_slope_5"] = out["ema20"] - out["ema20"].shift(5)
    out["ema_slope_10"] = out["ema20"] - out["ema20"].shift(10)
    out["ema_gap_20_50"] = (out["ema20"] - out["ema50"]) / (out["ema50"] + 1e-12)
    out["ema_gap_50_100"] = (out["ema50"] - out["ema100"]) / (out["ema100"] + 1e-12)
    out["distance_ema20"] = (out["close"] - out["ema20"]) / (out["ema20"] + 1e-12)
    out["distance_ema50"] = (out["close"] - out["ema50"]) / (out["ema50"] + 1e-12)
    out["distance_ema100"] = (out["close"] - out["ema100"]) / (out["ema100"] + 1e-12)

    # Volatility regime
    out["volatility_10"] = out["close"].rolling(10).std()
    out["volatility_20"] = out["close"].rolling(20).std()
    out["volatility_ratio"] = out["volatility_10"] / (out["volatility_20"] + 1e-12)

    # Returns
    for n in (1, 3, 5, 10):
        out[f"return_{n}"] = out["close"].pct_change(n)

    # Volume
    out["volume_change"] = out["volume"].pct_change()
    out["volume_ma20"] = out["volume"].rolling(20).mean()
    out["volume_ratio"] = out["volume"] / (out["volume_ma20"] + 1e-12)

    # Regime flags (binary)
    out["ema_cross"] = (out["ema20"] > out["ema50"]).astype(int)
    out["price_above_ema20"] = (out["close"] > out["ema20"]).astype(int)
    out["price_above_ema50"] = (out["close"] > out["ema50"]).astype(int)
    out["price_above_ema100"] = (out["close"] > out["ema100"]).astype(int)
    out["rsi_above_50"] = (out["rsi"] > 50).astype(int)
    out["rsi_overbought"] = (out["rsi"] > 70).astype(int)
    out["rsi_oversold"] = (out["rsi"] < 30).astype(int)

    # UTC hour of day — crypto is 24/7, but liquidity still varies
    out["datetime"] = pd.to_datetime(out["datetime"], format="ISO8601", utc=True)
    out["hour_utc"] = out["datetime"].dt.hour
    out["asia_hours"] = ((out["hour_utc"] >= 0) & (out["hour_utc"] < 8)).astype(int)
    out["europe_hours"] = ((out["hour_utc"] >= 8) & (out["hour_utc"] < 16)).astype(int)
    out["us_hours"] = ((out["hour_utc"] >= 16) & (out["hour_utc"] < 24)).astype(int)

    return out


def label_forward_outcome(df: pd.DataFrame) -> pd.DataFrame:
    """Forward SL/TP simulation labeling (user's original methodology).

    For each bar i:
      - distance = atr * SL_MULTIPLIER (we use atr as the distance proxy)
      - check forward LOOKAHEAD bars
      - BUY wins if high hits entry + reward_ratio*atr before low hits entry - atr
      - SELL wins if low hits entry - reward_ratio*atr before high hits entry + atr
    Only label bars where exactly one direction wins.
    """
    out = df.copy()
    targets: list[float] = []
    for i in range(len(out)):
        if i + LOOKAHEAD >= len(out):
            targets.append(np.nan)
            continue

        entry = out["close"].iloc[i]
        atr = out["atr"].iloc[i]
        if pd.isna(atr) or atr <= 0:
            targets.append(np.nan)
            continue

        window = out.iloc[i + 1 : i + LOOKAHEAD + 1]

        buy_sl = entry - atr * SL_MULTIPLIER
        buy_tp = entry + atr * REWARD_RATIO
        sell_sl = entry + atr * SL_MULTIPLIER
        sell_tp = entry - atr * REWARD_RATIO

        buy_hit_tp = window[window["high"] >= buy_tp]
        buy_hit_sl = window[window["low"] <= buy_sl]
        sell_hit_tp = window[window["low"] <= sell_tp]
        sell_hit_sl = window[window["high"] >= sell_sl]

        buy_win = (
            (not buy_hit_tp.empty and buy_hit_sl.empty)
            or (
                not buy_hit_tp.empty
                and not buy_hit_sl.empty
                and buy_hit_tp.index[0] < buy_hit_sl.index[0]
            )
        )
        sell_win = (
            (not sell_hit_tp.empty and sell_hit_sl.empty)
            or (
                not sell_hit_tp.empty
                and not sell_hit_sl.empty
                and sell_hit_tp.index[0] < sell_hit_sl.index[0]
            )
        )

        if buy_win and not sell_win:
            targets.append(1)
        elif sell_win and not buy_win:
            targets.append(0)
        else:
            targets.append(np.nan)

    out["target"] = targets
    return out


def main() -> None:
    print(f"Reading {IN_PATH}")
    df = pd.read_csv(IN_PATH)
    print(f"Loaded {len(df)} bars")

    df = add_indicators(df)
    print(f"After indicators: {len(df)} bars (NaNs in early rows expected)")

    df = label_forward_outcome(df)
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    df = df[df["target"].isin([0, 1])]
    print(f"After labeling + cleaning: {len(df)} usable training rows")
    print(f"  class balance: BUY={int((df['target'] == 1).sum())} "
          f"SELL={int((df['target'] == 0).sum())}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_PATH, index=False)
    print(f"Saved features to {OUT_PATH}")


if __name__ == "__main__":
    main()
