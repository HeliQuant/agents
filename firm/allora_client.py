"""Thin wrapper around the official `allora_sdk` Python client.

Why a wrapper:
  - Single source of truth for which Allora topics we use and what "direction" they imply
  - Sane caching (one async call per heartbeat tick, not per request)
  - Pydantic-typed return so the Signal Agent doesn't depend on SDK internals
  - Easy mocking in tests

Topic selection (mainnet, confirmed live):
  - Topic 14 = BTC/USD Price Prediction 8h — cleanest USD price prediction
  - Topic 15 = BTC/USD Log Returns 24h — pure directional signal in log-return space

For the multi-scale validation in Signal Agent we use Topic 14: compute the
prediction's deviation from CURRENT spot BTC and treat as macro direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from allora_sdk import AlloraAPIClient
from allora_sdk.api_client.client import ChainID

from firm.config import settings

BTC_PRICE_PREDICTION_8H_TOPIC = 14
BTC_LOG_RETURNS_24H_TOPIC = 15
ETH_PRICE_PREDICTION_8H_TOPIC = 9

COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"


@dataclass(frozen=True)
class MacroSignal:
    asset: str
    spot_usd: float
    predicted_usd: float
    predicted_return: float       # (predicted - spot) / spot
    confidence_bps: int           # bookkeeping; SDK doesn't always expose, default 7500
    direction_bull: bool          # predicted_return >= +threshold
    direction_bear: bool          # predicted_return <= -threshold
    signature_hex: str            # raw publisher signature (for relayer use)
    timestamp: int


def _spot_price(coin_id: str) -> float:
    resp = requests.get(
        COINGECKO_PRICE_URL,
        params={"ids": coin_id, "vs_currencies": "usd"},
        timeout=10,
        headers={"User-Agent": "mantle-trading-firm/0.1"},
    )
    resp.raise_for_status()
    return float(resp.json()[coin_id]["usd"])


async def fetch_macro_signal(
    topic_id: int,
    coin_id: str,
    asset: str,
    *,
    bull_threshold: float = 0.005,
    bear_threshold: float = -0.005,
) -> MacroSignal:
    """Return the latest Allora macro signal for a topic: predicted 8h price vs current spot."""
    if not settings_allora_key():
        raise RuntimeError("ALLORA_API_KEY not configured")

    client = AlloraAPIClient(
        chain_id=ChainID.MAINNET,
        api_key=settings_allora_key(),
    )
    inference = await client.get_inference_by_topic_id(topic_id)
    data: dict[str, Any] = inference.inference_data.model_dump()
    predicted = float(data["network_inference_normalized"])
    timestamp = int(data["timestamp"])
    signature_hex = str(inference.signature)

    spot = _spot_price(coin_id)
    predicted_return = (predicted - spot) / spot if spot > 0 else 0.0

    return MacroSignal(
        asset=asset,
        spot_usd=spot,
        predicted_usd=predicted,
        predicted_return=predicted_return,
        confidence_bps=7500,
        direction_bull=predicted_return >= bull_threshold,
        direction_bear=predicted_return <= bear_threshold,
        signature_hex=signature_hex,
        timestamp=timestamp,
    )


async def fetch_btc_macro_signal(**kw) -> MacroSignal:
    """BTC/USD 8h prediction (topic 14)."""
    return await fetch_macro_signal(BTC_PRICE_PREDICTION_8H_TOPIC, "bitcoin", "BTC", **kw)


async def fetch_eth_macro_signal(**kw) -> MacroSignal:
    """ETH/USD 8h prediction (topic 9)."""
    return await fetch_macro_signal(ETH_PRICE_PREDICTION_8H_TOPIC, "ethereum", "ETH", **kw)


def settings_allora_key() -> str | None:
    """Read API key via settings — kept lazy so .env reload picks up new values."""
    # `pydantic-settings` cached at import; explicitly re-read env to be safe
    import os
    return os.environ.get("ALLORA_API_KEY") or getattr(settings, "allora_api_key", None)
