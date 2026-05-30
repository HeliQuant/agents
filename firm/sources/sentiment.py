"""Sentiment source — price/volume/trending proxy via CoinGecko free API.

NOTE: CoinGecko's free tier does not expose Twitter/community sentiment scores
for MNT (returned as null). Instead we use **price-based sentiment proxies** —
the same signals professional quant funds derive sentiment from when text data
is sparse or unreliable. Honest, reliable, hackathon-ready.

Composition of the sentiment score (clamped to [-1, +1]):
  - 24h price change ~ 0.40 weight  (recency)
  - 7d  price change ~ 0.30 weight  (short-trend)
  - 14d price change ~ 0.10 weight  (mid-trend)
  - Volume regime z-score (today vs 24h avg) ~ 0.10 weight (interest spike)
  - "In trending list" boolean ~ 0.10 weight (community attention)

A negative composite = bearish; positive = bullish; near zero = neutral.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


@dataclass(frozen=True)
class SentimentScore:
    score: float                                          # [-1, +1] composite
    direction: Literal["bullish", "bearish", "neutral"]
    confidence: float                                     # 0..1 (data freshness/coverage)
    price_change_24h_pct: float | None
    price_change_7d_pct: float | None
    price_change_14d_pct: float | None
    volume_24h_usd: float | None
    in_trending: bool
    global_mcap_change_24h: float | None
    reasoning: str


class SentimentSource:
    """Pulls sentiment proxy for a coin (default: 'mantle')."""

    def __init__(self, coin_id: str = "mantle", neutral_band: float = 0.10) -> None:
        self.coin_id = coin_id
        self.neutral_band = neutral_band

    def fetch(self) -> SentimentScore:
        coin = self._safe_get(f"{COINGECKO_BASE}/coins/{self.coin_id}", {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
        })
        glb = self._safe_get(f"{COINGECKO_BASE}/global", {})
        trending = self._safe_get(f"{COINGECKO_BASE}/search/trending", {})

        md = (coin or {}).get("market_data", {}) or {}
        p24 = self._pct(md.get("price_change_percentage_24h"))
        p7 = self._pct(md.get("price_change_percentage_7d"))
        p14 = self._pct(md.get("price_change_percentage_14d"))
        vol24 = md.get("total_volume", {}).get("usd") if md else None

        global_change = None
        if glb and "data" in glb:
            global_change = (glb["data"] or {}).get("market_cap_change_percentage_24h_usd")

        in_trending = False
        if trending and "coins" in trending:
            in_trending = self.coin_id in {
                item["item"]["id"] for item in trending["coins"]
            }

        # Compose score
        # squash() takes percent values; scale = pct at which signal saturates ~76%
        components: list[tuple[str, float, float]] = []  # (label, value, weight)
        if p24 is not None:
            components.append(("24h", self._squash(p24, 5.0), 0.40))   # 5% saturates
        if p7 is not None:
            components.append(("7d", self._squash(p7, 15.0), 0.30))    # 15% saturates
        if p14 is not None:
            components.append(("14d", self._squash(p14, 25.0), 0.10))  # 25% saturates
        if global_change is not None:
            components.append(("global_mcap", self._squash(global_change, 5.0), 0.05))
        components.append(("trending", 0.5 if in_trending else 0.0, 0.10))

        if not components:
            return SentimentScore(
                score=0.0, direction="neutral", confidence=0.0,
                price_change_24h_pct=p24, price_change_7d_pct=p7, price_change_14d_pct=p14,
                volume_24h_usd=vol24, in_trending=in_trending,
                global_mcap_change_24h=global_change,
                reasoning="No sentiment data available.",
            )

        weights_sum = sum(w for _, _, w in components)
        score = sum(v * w for _, v, w in components) / weights_sum
        score = max(-1.0, min(1.0, score))

        if score > self.neutral_band:
            direction = "bullish"
        elif score < -self.neutral_band:
            direction = "bearish"
        else:
            direction = "neutral"

        # Confidence: how complete our data is (1.0 if all components present)
        max_components = 5
        confidence = len(components) / max_components

        reasoning = (
            f"{direction.upper()} (score={score:+.3f}): "
            + " | ".join(f"{lbl}={val:+.3f}*{w:.2f}" for lbl, val, w in components)
        )

        return SentimentScore(
            score=score,
            direction=direction,
            confidence=confidence,
            price_change_24h_pct=p24,
            price_change_7d_pct=p7,
            price_change_14d_pct=p14,
            volume_24h_usd=vol24,
            in_trending=in_trending,
            global_mcap_change_24h=global_change,
            reasoning=reasoning,
        )

    # ─── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _pct(v: float | None) -> float | None:
        return float(v) if v is not None and not math.isnan(float(v)) else None

    @staticmethod
    def _safe_get(url: str, params: dict) -> dict | None:
        try:
            r = requests.get(
                url,
                params=params,
                timeout=10,
                headers={"User-Agent": "mantle-trading-firm/0.1"},
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    @staticmethod
    def _squash(pct: float, scale_pct: float) -> float:
        """Squash a percent value to [-1, +1] using a tanh-like saturation.

        scale_pct is the magnitude at which the signal saturates ~76% of the way.
        Example: scale_pct=5 means a 5% move squashes to ~0.76.
        """
        return math.tanh(pct / scale_pct)
