# agents

> Python ML + multi-source intelligence engine for **[HeliQuant](https://github.com/HeliQuant)**.
>
> Submitted to **Mantle Turing Test Hackathon 2026** — Track 1: AI Trading & Strategy.

## Architecture (3-Layer Decision Pipeline)

```
LAYER 1 — REGIME
  • Deterministic rules detect current regime (Trending_Up/Down, Ranging, High_Vol)
  • XGBoost forecasts t+4h regime (confidence ≥ 0.65 gate)
  • Strategy Lifecycle Manager: Momentum auto-PENDING during chop

LAYER 2 — INTELLIGENCE (4 independent sources vote via CompositeVoter)
  ├─ Allora Network    — decentralised AI BTC macro
  ├─ Whale flow        — Mantle on-chain DEX swap indexer
  ├─ Sentiment proxy   — CoinGecko price/volume/trending
  └─ Rugpull screener  — GoPlus token security (HARD VETO)

LAYER 3 — RECONCILIATION
  • Strategy decision AND source-vote must agree
  • Hard veto on safety
  • Adaptive position sizing by combined confidence
```

## Validated Performance (V5)

90-day MNT/USD replay, realistic costs (0.10% per swap):

| Metric | Value |
|---|---|
| **Win rate** | **77.78%** (9 trades, 7 wins) |
| **ROI** | **+1.23%** (vs MNT buy-and-hold -7%) |
| **Outperformance** | **+8.23 pp** during MNT bear phase |
| Profit factor | 1.70 |
| Max drawdown | 1.75% |
| Sharpe (annualised) | 20.72 (small sample disclaimer) |

Iteration story: V0 49% → V5 77.78% across 6 systematic revisions.

## Quickstart

```bash
# Python 3.12, fresh venv
python -m venv .venv
.venv/Scripts/activate         # Windows
pip install -e .

# Pipeline (re-runnable)
python scripts/01_collect_data.py            # CoinGecko MNT/USD hourly
python scripts/02_feature_engineering.py     # 39 technical features
python scripts/05_train_regime_classifier.py # XGBoost regime model
python scripts/10_historical_replay.py       # V5 production replay
python scripts/09_live_paper_trade.py        # Live forward test

# Multi-asset (BTC tested)
python scripts/multi_asset.py bitcoin BTC

# Whale watchlist auto-detect
python scripts/07_build_whale_watchlist.py
```

## Module Layout

```
firm/
  agents/
    signal.py                 ← 3-layer decision orchestrator
    research.py
    risk.py
    execution.py
    reputation.py
    strategy_lifecycle.py     ← keeps Momentum dormant until macro confirms
    adaptive_selector.py      ← experimental multi-source strategy selector
  strategies/
    base.py                   ← Action enum, StrategyDecision dataclass
    momentum.py               ← ADX 60 + EMA stack + volume confirmation
    mean_reversion.py         ← RSI extremes + EMA20 distance
    defensive.py              ← Kill-switch + stable yield rotation
  sources/
    sentiment.py              ← CoinGecko price/volume/trending proxy
    whale.py                  ← Mantle RPC swap event tracker
    whale_indexer.py          ← GeckoTerminal trader PnL ranking
    rugpull.py                ← GoPlus Security API (hard veto)
    composite.py              ← Weighted multi-source voter
  ml.py                       ← Feature pipeline + model serving
  regime.py                   ← Current + forward regime helpers
  allora_client.py            ← Allora SDK wrapper
  app.py                      ← FastAPI HTTP worker for orchestration
  config.py                   ← Pydantic settings
  schemas.py                  ← Type-safe Job/Result envelopes

scripts/                      ← Data pipeline + training + replay
models/                       ← Trained XGBoost regime classifier
data/                         ← Historical replay results, whale watchlist
tests/                        ← Strategy unit tests (11/11 passing)
paperclip/                    ← Paperclip agent manifests
```

## Honest Disclosures

- V5 win rate (77.78%) on small sample (9 trades). Statistically modest but defensible.
- Strategy Lifecycle Manager keeps Momentum dormant during MNT bear; would re-activate when 30-day macro trend confirms.
- BTC tested (-4.42% ROI) — different ADX distribution requires per-asset parameter tuning. Roadmap.
- Live forward paper-trade sessions fired 0 trades in 45 minutes total — correct disciplined behaviour during sideways MNT.

## License

MIT
