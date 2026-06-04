# agents

> Python ML + **autonomous multi-agent firm** for **[HeliQuant](https://github.com/HeliQuant)** — the all-seeing quant.
>
> Submitted to **Mantle Turing Test Hackathon 2026** — Track 1: AI Trading & Strategy.

## Architecture — the autonomous firm

The brain is `firm/organization.py`: a **seven-desk autonomous org → Portfolio Manager → gated trade-ticket → on-chain anchor.** Each desk reads a different **real** source in parallel, the desks debate bull-vs-bear, the PM synthesises one disciplined decision, and the decision is recorded (off-chain always; on-chain on ENTER).

```
RECALL → PLAN → DEBATE → PM DECISION → TICKET → EXECUTE → LEARN ↻
memory   7 desks  bull/bear   verdict      R:R≥2   record    loop
```

**The seven desks (each a real source):**

| Desk | Source | Module |
|---|---|---|
| Regime / Technical | XGBoost regime classifier (82.6% OOS) | `firm/ml.py`, `firm/regime.py` |
| Macro (Allora) | Allora decentralised-AI (BTC/ETH 8h) | `firm/allora_client.py` |
| On-chain / Risk | whale flow + GoPlus token-safety veto | `firm/sources/whale.py`, `rugpull.py` |
| Research | Fear & Greed + public market APIs | `firm/sources/sentiment.py` |
| Smart-Money Flow | dynamic whale/contract flow + Nansen | `firm/whale_manager.py`, `firm/nansen_client.py` |
| Smart-Social | Elfa narratives / mindshare | `firm/elfa_client.py` |
| OI-Contrarian | perp Open-Interest extremes — **the one validated edge** | `data/validated_edges.json` |

**Execution discipline** (`firm/trade_ticket.py`): entry zone · **structural stop** · separate invalidation · TP-ladder, behind a hard **R:R ≥ 2:1 gate**. **SAFE** by default; **AGGRESSIVE** = fractional-Kelly, **≤3% risk / ≤5× leverage**, **20% drawdown breaker**, min-edge-sample 20 — unlocked only when a validated edge's live signal fires *and* agrees. The LLM judges direction; deterministic code computes the numbers; the gate enforces safety.

**Memory + ledger** (`firm/memory_store.py`): decisions persist to Supabase `decisions_hq` (auto-falls back to SQLite). **On-chain anchor** (`firm/onchain_recorder.py`): on ENTER, the decision hash is written to a 0-value Mantle transaction (broadcast-on-ENTER).

## What's validated — and what isn't (honesty first)

| Claim | Status |
|---|---|
| **Regime classifier** | **82.6% out-of-sample accuracy** — verified. *But accuracy did NOT become simulated trading profit* — its job is capital allocation + risk, not price prediction. |
| **OI-Contrarian on MNT** | the **one** edge surviving cost-aware OOS: **58.8% win · 1.30 payoff · 34 trades · +28.9% OOS.** Hedge-like, **caveated**: bear-amplified, small-sample (n=34), inconsistent fold-to-fold → fractional-Kelly sized, never a guarantee. |
| Trend-following / mean-reversion | ❌ fail full-history walk-forward OOS; early single-window returns were artifacts (**retracted**). |
| Funding-capture | ❌ tested → lost 30–39% (fees eat the carry). |
| mETH/ETH convergence | ❌ **+96% OOS / 95.6% win** on paper → **−73 bps/trade** under realistic mETH slippage → thin-liquidity artifact → **rejected.** |

> Sessions frequently fire **0 trades** (sideways MNT / no validated edge) — a correct, disciplined **ABSTAIN**, not a bug. *We publish what doesn't work, too.*

## Quickstart

```bash
# Python 3.12, fresh venv
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -e .

# ── data → features → regime model ──
python scripts/01_collect_data.py             # CoinGecko hourly OHLCV
python scripts/02_feature_engineering.py      # 39 technical features
python scripts/05_train_regime_classifier.py  # XGBoost regime model

# ── validate the edge (cost-aware, out-of-sample) ──
python scripts/39_oi_backtest.py              # OI-contrarian → writes data/validated_edges.json
python scripts/13_walkforward.py              # walk-forward OOS (rejects overfit)

# ── the autonomous firm ──
python scripts/24_autonomous_org.py           # one pass: 7 desks → debate → PM → ticket → record
python scripts/52_autonomous_loop.py --once   # the autonomous loop (PLAN→EXEC→LEARN); --replay for no-LLM backtest
python scripts/26_paper_trade.py              # live forward paper-trade
```

## Module layout

```
firm/
  organization.py        ← THE BRAIN: 7-desk org → debate → PM → finalize_decision
  trade_ticket.py        ← entry/stop/invalidation/TP-ladder + SAFE/AGGRESSIVE Kelly + R:R≥2 gate
  memory_store.py        ← decisions_hq (Supabase + pgvector, SQLite fallback) + Obsidian vault mirror
  onchain_recorder.py    ← anchor decision hash → Mantle Sepolia (broadcast-on-ENTER)
  llm_client.py          ← BYO multi-provider / multi-key LLM abstraction
  allora_client.py · nansen_client.py · elfa_client.py · whale_manager.py   ← desk data sources
  ml.py · regime.py      ← XGBoost regime classifier (feature pipeline + serving)
  asset_configs.py       ← per-asset config (2-tier universe)
  agents/                ← signal.py (legacy 3-layer orchestrator) · research · risk · execution · reputation · strategy_lifecycle · adaptive_selector
  strategies/            ← base · momentum · mean_reversion · defensive
  sources/               ← whale · whale_indexer · rugpull (GoPlus) · sentiment · composite voter
  app.py · config.py · schemas.py   ← FastAPI worker · Pydantic settings · typed envelopes

scripts/                 ← data pipeline · training · backtests · walk-forward · org · autonomous loop
  37–43_*                ← positioning / OI-contrarian backtests (the validated edge)
  53_meth_eth_convergence.py  ← the REJECTED +96% strategy (kept as honest evidence)
  51_smart_money_engine.py    ← dynamic smart-money flow engine
data/                    ← validated_edges.json · whale watchlist · replay results
models/                  ← trained XGBoost regime classifier
tests/                   ← unit tests
```

## Honest disclosures

- **No all-weather alpha** here — the headline is **rigor**, not a return. Only the regime classifier (82.6% OOS) and the OI-contrarian MNT hedge survived rigorous testing.
- The OI-contrarian aggregate OOS was **bear-market-amplified** and inconsistent fold-to-fold → a risk-controlled hedge input, not a guarantee.
- We **retracted earlier headline numbers** when they failed full-history walk-forward, and **rejected a +96% backtest** (thin-liquidity artifact). The discipline that rejects a 96% backtest is worth more than the backtest.

## License

MIT
