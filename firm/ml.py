"""ML inference helpers — load the trained MNT XGBoost and serve predictions.

The Signal Agent imports this module, fetches fresh hourly MNT bars from CoinGecko,
builds the same features used during training, and calls `predict`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import requests

from firm.config import settings

MODEL_PATH = Path(__file__).resolve().parents[1] / settings.ml_model_path
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/mantle/market_chart"


@lru_cache(maxsize=1)
def load_model() -> dict[str, Any]:
    """Load the trained model + feature list. Cached process-wide."""
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Model file not found at {MODEL_PATH}. "
            "Run `python scripts/03_train_model.py` first."
        )
    return joblib.load(MODEL_PATH)


def fetch_recent_bars(days: int = 7) -> pd.DataFrame:
    """Fetch recent MNT hourly close+volume from CoinGecko."""
    resp = requests.get(
        COINGECKO_URL,
        params={"vs_currency": "usd", "days": str(days)},
        timeout=15,
        headers={"User-Agent": "mantle-trading-firm/0.1"},
    )
    resp.raise_for_status()
    payload = resp.json()
    prices = payload["prices"]
    vols = {ts: v for ts, v in payload["total_volumes"]}

    df = pd.DataFrame(
        [
            {
                "timestamp": int(ts),
                "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                "close": float(c),
                "volume": float(vols.get(ts, 0.0)),
            }
            for ts, c in prices
        ]
    )
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = df["close"].rolling(3, min_periods=1).max()
    df["low"] = df["close"].rolling(3, min_periods=1).min()
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Same indicator pipeline as scripts/02_feature_engineering.py — kept in sync."""
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

    out["hour_utc"] = out["datetime"].dt.hour
    out["asia_hours"] = ((out["hour_utc"] >= 0) & (out["hour_utc"] < 8)).astype(int)
    out["europe_hours"] = ((out["hour_utc"] >= 8) & (out["hour_utc"] < 16)).astype(int)
    out["us_hours"] = ((out["hour_utc"] >= 16) & (out["hour_utc"] < 24)).astype(int)

    return out


def predict_latest() -> dict[str, float]:
    """Return the latest-bar BUY/SELL probability, ADX, and current price."""
    bundle = load_model()
    model = bundle["model"]
    features = bundle["features"]

    df = fetch_recent_bars(days=7)
    df = build_features(df)
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if df.empty:
        raise RuntimeError("Empty feature frame after dropping NaNs")

    latest_row = df[features].iloc[-1:].copy()
    proba = model.predict_proba(latest_row)[0]
    sell_prob = float(proba[0])
    buy_prob = float(proba[1])

    return {
        "buy_prob": buy_prob,
        "sell_prob": sell_prob,
        "adx": float(df["adx"].iloc[-1]),
        "close": float(df["close"].iloc[-1]),
        "rsi": float(df["rsi"].iloc[-1]),
    }
