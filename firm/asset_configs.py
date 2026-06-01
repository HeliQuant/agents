"""Per-asset strategy configuration — MODULAR, one config per market.

VERIFIED STATE (2026-05-30). Every number below is copied from a walk-forward
JSON source-of-truth, never hand-typed from memory:

  Mean-reversion (scripts/18 -> data/mantle_walkforward.json): 0/4 Mantle pass OOS.
    MNT -10.32%, mETH -0.79%, cmETH -2.57%, fBTC -3.42%. Wrong tool for trending crypto.
    (Majors via scripts/15 -> data/universe_results.json: 0/12 pass. Total 0/16 OOS.)

  Trend-following — earlier "2/4 PASS (MNT +5.58%, mETH +1.70%)" on ~1y data was a
  SHORT-WINDOW ARTIFACT. ⚠️ SUPERSEDED 2026-05-31: on full ~2.9y history the single-split
  walk-forward = OVERFIT (MNT -0.97%) and a rolling/adaptive re-tune (scripts/25) = flat
  (MNT +1% over 2.5y, mETH/ETH negative). There is NO robust out-of-sample TRADING edge.
  The momentum params below are kept for RESEARCH ONLY — not a validated live edge.

  What IS honestly strong: the regime classifier (scripts/_diag_classifier.py) — MNT
  forward-4h regime accuracy 82.6% OOS (91.6% when confident), beating persistence 77.5%.
  Accurate regime-reading != trading profit; the org's job is to read regime + stay
  disciplined (abstain) when no validated edge applies.

  Canonical proof: scripts/19 + scripts/25 + data/mantle_trend_walkforward.json +
  data/rolling_walkforward.json. No fabricated numbers — every figure traces to a JSON.
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


# Validated OOS configs. Params copied from data/mantle_trend_walkforward.json
# (produced by scripts/19_walkforward_trend.py). Trend-following is the edge;
# mean-reversion (mr_*) params are left at defaults but are NOT the validated path.
_MNT = AssetConfig(
    ticker="MNT",
    coingecko_id="mantle",
    momentum_adx_min=40.0,
    momentum_breakout_window=10,
    momentum_volume_min_ratio=0.0,  # Pyth data has no volume
    mo_tp_atr_mult=2.5,
    forward_conf_threshold=0.65,
    momentum_enabled=True,
    validation=(
        "NOT validated for live trading — the +5.58% (scripts/19, ~1y) was a short-window "
        "artifact; full 2.9y WF = OVERFIT (-0.97%) and rolling re-tune = flat (+1%/2.5y). "
        "Params kept for research only. (Regime classifier IS strong: 82.6% OOS.)"
    ),
)
_METH = AssetConfig(
    ticker="METH",
    coingecko_id="mantle-staked-ether",
    momentum_adx_min=40.0,
    momentum_breakout_window=20,
    momentum_volume_min_ratio=0.0,  # Pyth data has no volume
    mo_tp_atr_mult=2.0,
    forward_conf_threshold=0.60,
    momentum_enabled=True,
    validation=(
        "NOT validated — the +1.70% (scripts/19, ~1y, 4 trades) did not survive: full 2.2y "
        "WF + rolling re-tune both negative OOS. Params kept for research only."
    ),
)

# Only assets with a REAL out-of-sample pass live here. cmETH/fBTC were INCONCLUSIVE
# (too few OOS trades on 90-day data) — they get the conservative DEFAULT until they
# have genuine OOS proof. No fabricated configs.
ASSET_CONFIGS: dict[str, AssetConfig] = {
    "MNT": _MNT,
    "METH": _METH,
}


# Honest data-source map for Mantle-ecosystem tokens.
_DATA_SOURCE = {
    "MNT": "pyth:Crypto.MNT/USD (~390d real hourly OHLC, no volume) | coingecko:mantle (90d, has volume)",
    "METH": "pyth:Crypto.METH/USD (~390d real hourly OHLC, no volume)",
    "CMETH": "coingecko:mantle-restaked-eth (90d, has volume)",
    "FBTC": "coingecko:ignition-fbtc (90d, has volume)",
}

_COINGECKO_IDS = {
    "MNT": "mantle",
    "METH": "mantle-staked-ether",
    "CMETH": "mantle-restaked-eth",
    "FBTC": "ignition-fbtc",
}


def get_config(ticker: str) -> AssetConfig:
    """Return the OOS-validated config for a ticker, or a conservative DEFAULT.

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


# No asset has a validated out-of-sample TRADING edge: full-history walk-forward + rolling
# re-tune both showed trend-following is flat/negative OOS (the earlier "+5.58% MNT" was a
# short-window artifact, 2026-05-31). ASSET_CONFIGS above are kept for params/research only.
_VALIDATED_TRADING_EDGE: set[str] = set()


def is_validated(ticker: str) -> bool:
    """True only if the asset has a validated out-of-sample TRADING edge. Currently NONE —
    trend-following did not survive full-history / rolling walk-forward (2026-05-31)."""
    return ticker.upper() in _VALIDATED_TRADING_EDGE


def get_data_source(ticker: str) -> str:
    return _DATA_SOURCE.get(ticker.upper(), "unknown")
