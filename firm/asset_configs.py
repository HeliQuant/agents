"""Per-asset strategy configuration.

Each tradeable asset gets its OWN tuned parameters so calibration for one market
never collides with another. MNT was tuned first (the production hero); other
assets get their own configs found via scripts/12_tune_asset.py grid search.

Design: a frozen dataclass per asset, looked up by ticker. `DEFAULT` is the
conservative fallback for any asset without an explicit config.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    ticker: str
    coingecko_id: str

    # Mean reversion
    oversold_rsi: float = 25.0
    overbought_rsi: float = 75.0
    mr_sl_atr_mult: float = 1.0
    mr_tp_atr_mult: float = 1.8

    # Momentum
    momentum_adx_min: float = 60.0
    momentum_breakout_window: int = 20
    momentum_volume_min_ratio: float = 1.15
    mo_sl_atr_mult: float = 1.2
    mo_tp_atr_mult: float = 2.5

    # Lifecycle gate (mean reversion active when |macro return| < this %)
    mr_flat_threshold_pct: float = 3.0
    # Forward-regime confidence gate
    forward_conf_threshold: float = 0.65
    # Whether momentum is allowed to fire at all for this asset
    momentum_enabled: bool = False


# Production configs. MNT is the calibrated hero; others start from DEFAULT and
# get overwritten by the tuner (scripts/12_tune_asset.py) as configs are found.
ASSET_CONFIGS: dict[str, AssetConfig] = {
    # Each config found via scripts/12_tune_asset.py grid search (min 6 trades floor).
    "MNT": AssetConfig(
        ticker="MNT", coingecko_id="mantle",
        oversold_rsi=25.0, overbought_rsi=75.0, mr_tp_atr_mult=1.8,
        mr_flat_threshold_pct=3.0, momentum_enabled=False,
    ),  # +1.36% ROI, 69% win, 13 trades
    "BTC": AssetConfig(
        ticker="BTC", coingecko_id="bitcoin",
        oversold_rsi=20.0, overbought_rsi=80.0, mr_tp_atr_mult=2.2,
        mr_flat_threshold_pct=5.0, momentum_enabled=False,
    ),  # +0.94% ROI, 71.4% win, 7 trades
    "METH": AssetConfig(
        ticker="METH", coingecko_id="mantle-staked-ether",
        oversold_rsi=20.0, overbought_rsi=80.0, mr_tp_atr_mult=1.8,
        mr_flat_threshold_pct=5.0, momentum_enabled=False,
    ),  # +0.55% ROI, 62.5% win, 8 trades
    "CMETH": AssetConfig(
        ticker="CMETH", coingecko_id="mantle-restaked-eth",
        oversold_rsi=20.0, overbought_rsi=80.0, mr_tp_atr_mult=2.2,
        mr_flat_threshold_pct=5.0, momentum_enabled=False,
    ),  # +0.48% ROI, 62.5% win, 8 trades
    "FBTC": AssetConfig(
        ticker="FBTC", coingecko_id="ignition-fbtc",
        oversold_rsi=20.0, overbought_rsi=80.0, mr_tp_atr_mult=1.8,
        mr_flat_threshold_pct=5.0, momentum_enabled=False,
    ),  # +0.55% ROI, 62.5% win, 8 trades
}


_COINGECKO_IDS = {
    "MNT": "mantle",
    "BTC": "bitcoin",
    "METH": "mantle-staked-ether",
    "CMETH": "mantle-restaked-eth",
    "FBTC": "ignition-fbtc",
    "USDE": "ethena-usde",
}


def get_config(ticker: str) -> AssetConfig:
    """Return the tuned config for a ticker, or a sensible DEFAULT."""
    ticker = ticker.upper()
    if ticker in ASSET_CONFIGS:
        return ASSET_CONFIGS[ticker]
    return AssetConfig(
        ticker=ticker,
        coingecko_id=_COINGECKO_IDS.get(ticker, ticker.lower()),
    )
