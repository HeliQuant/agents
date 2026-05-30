"""Regime detection helpers — deterministic rules + forward ML forecasting.

Two complementary roles:

1. detect_current_regime(): cheap, explainable, no model needed. Used for
   immediate strategy routing. The same rules the regime classifier was
   trained against, so test-time inputs match training-time labels.

2. predict_forward_regime(): loads xgb_regime.pkl and returns probability
   distribution over future regime (4 bars ahead by default). Used for
   confidence-gated decisions and proactive transition handling.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from firm.config import settings

REGIME_MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "xgb_regime.pkl"


@lru_cache(maxsize=1)
def load_regime_bundle() -> dict[str, Any]:
    if not REGIME_MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Regime model not found at {REGIME_MODEL_PATH}. "
            "Run `python scripts/05_train_regime_classifier.py` first."
        )
    return joblib.load(REGIME_MODEL_PATH)


def detect_current_regime(bar: pd.Series) -> str:
    """Apply the same trailing-indicator rules used to label training data."""
    bundle = load_regime_bundle()
    adx_strong = bundle["adx_strong_threshold"]
    vol_high = bundle["vol_high_threshold"]

    if bar["volatility_10"] > vol_high:
        return "High_Volatility"
    if bar["adx"] > adx_strong:
        return "Trending_Up" if bar["ema_slope_5"] > 0 else "Trending_Down"
    return "Ranging"


def predict_forward_regime(window: pd.DataFrame) -> dict[str, Any]:
    """Predict regime at t + forecast_horizon for the LAST row of `window`.

    Returns:
        {
            "regime": <best class>,
            "confidence": <max probability>,
            "probabilities": {class: prob, ...},
            "horizon_bars": <int>,
        }
    """
    bundle = load_regime_bundle()
    model = bundle["model"]
    features = bundle["features"]
    labels = bundle["labels"]

    row = window[features].iloc[-1:].copy()
    proba = model.predict_proba(row)[0]
    best_idx = int(proba.argmax())

    return {
        "regime": labels[best_idx],
        "confidence": float(proba[best_idx]),
        "probabilities": {labels[i]: float(p) for i, p in enumerate(proba)},
        "horizon_bars": int(bundle.get("forecast_horizon_bars", 4)),
    }
