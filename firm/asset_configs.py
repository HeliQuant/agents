"""Per-asset strategy configuration — MODULAR, one config per market.

Each tradeable asset gets its OWN config block so calibration for one market never
collides with another. Grouped by family: Mantle native (MNT) and its ecosystem
children (mETH, cmETH, fBTC) each kept separate.

⚠️ HONEST STATE (verified 2026-05-30):
  - Only MNT has a positive, reproducible result, and ONLY as a 90-day full-period
    replay (scripts/10_historical_replay.py): +1.36% ROI, 69% win, PF 1.81. That is
    NOT out-of-sample — it is the asset the strategy was tuned on.
  - Walk-forward out-of-sample testing did NOT validate any asset:
      * Majors (BTC/ETH/SOL/... on Binance 1yr real OHLC): 0/12 pass (data/universe_results.json)
      * cmETH, fBTC (CoinGecko 90d): OVERFIT (OOS -2.57%, -3.42%)
      * MNT, mETH on Pyth: data too sparse (Pyth benchmark history ~64 valid hourly
        bars/yr for these tokens) → could not walk-forward test.
  - Therefore ONLY the MNT config is kept here, clearly labeled as replay-validated
    (not OOS). No fabricated multi-asset numbers. Adding an asset requires real
    out-of-sample proof first.

Data-source reality for Mantle-ecosystem tokens (see get_data_source()):
  - MNT, mETH, cmETH, fBTC have CoinGecko spot + Pyth on-chain oracle, but neither
    gives deep, clean hourly OHLC history. Best on-chain history = GeckoTerminal /
    DexScreener (Mantle DEX pools). That is the path for genuine validation (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    ticker: str
    coingecko_id: str

    oversold_rsi: float = 25.0
    overbought_rsi: float = 75.0
    mr_sl_atr_mult: float = 1.0
    mr_tp_atr_mult: float = 1.8

    momentum_adx_min: float = 60.0
    momentum_breakout_window: int = 20
    momentum_volume_min_ratio: float = 1.10
    mo_sl_atr_mult: float = 1.2
    mo_tp_atr_mult: float = 2.5

    mr_flat_threshold_pct: float = 3.0
    forward_conf_threshold: float = 0.65
    momentum_enabled: bool = False

    validation: str = "default"


# The ONLY config with a real (replay) result. Not out-of-sample validated.
_MNT = AssetConfig(
    ticker="MNT", coingecko_id="mantle",
    oversold_rsi=25.0, overbought_rsi=75.0, mr_tp_atr_mult=1.8,
    mr_flat_threshold_pct=3.0, forward_conf_threshold=0.65, momentum_enabled=False,
    validation="90d full-period replay +1.36% ROI / 69% win / PF 1.81 (NOT out-of-sample)",
)

ASSET_CONFIGS: dict[str, AssetConfig] = {
    "MNT": _MNT,
}


# Where to source data for each ecosystem token (honest: history is thin for children).
_DATA_SOURCE = {
    "MNT": "coingecko:mantle (90d hourly) | pyth:Crypto.MNT/USD (sparse history)",
    "METH": "coingecko:mantle-staked-ether | pyth:Crypto.METH/USD (sparse)",
    "CMETH": "coingecko:mantle-restaked-eth (90d only)",
    "FBTC": "coingecko:ignition-fbtc (90d only)",
}

_COINGECKO_IDS = {
    "MNT": "mantle",
    "METH": "mantle-staked-ether",
    "CMETH": "mantle-restaked-eth",
    "FBTC": "ignition-fbtc",
}


def get_config(ticker: str) -> AssetConfig:
    """Return the validated config for a ticker, or a conservative DEFAULT.

    DEFAULT is returned for any asset without a real validated config. Trading on a
    DEFAULT config is "explore only" — it has NOT been proven out-of-sample.
    """
    ticker = ticker.upper()
    if ticker in ASSET_CONFIGS:
        return ASSET_CONFIGS[ticker]
    return AssetConfig(
        ticker=ticker,
        coingecko_id=_COINGECKO_IDS.get(ticker, ticker.lower()),
        validation="UNVALIDATED (default config — not proven out-of-sample)",
    )


def is_validated(ticker: str) -> bool:
    """True only if the asset has a config with a real (non-default) result."""
    return ticker.upper() in ASSET_CONFIGS


def get_data_source(ticker: str) -> str:
    return _DATA_SOURCE.get(ticker.upper(), "unknown")
