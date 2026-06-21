# HeliQuant 🦅 — Autonomous AI Trading Firm

> Python ML + **autonomous multi-agent firm** for **[HeliQuant](https://github.com/HeliQuant)** — the all-seeing quant.
> **Bitget AI Base Camp Hackathon S1 · Track: Trading Agent.**

An autonomous *firm* (not a bot): **10 specialist desks** (+ flow-intel & whale = **12 analyst voices**) debate
bull-vs-bear → one **Portfolio-Manager** decision → **risk-gated** (R:R ≥ 2:1, fractional-Kelly, vol-targeted,
drawdown-breaker) → **executed on Bitget demo futures** (long **and** short) → **anchored on-chain**. It
**abstains when no validated edge fires** — discipline over activity. We publish what doesn't work, too.

## Architecture — the autonomous firm
The brain is `firm/organization.py`. Each desk reads a different **real** source in parallel; desks debate
bull-vs-bear; the PM synthesises one disciplined decision; it's risk-gated, executed, and recorded.

```
RECALL → PLAN → DEBATE → PM DECISION → TICKET → EXECUTE → LEARN ↻
memory  10 desks  bull/bear  verdict     R:R≥2   Bitget    loop
```

**The 10 desks** (`build_desks`) — each a real source:

| Desk | Source | Module |
|---|---|---|
| Regime / Technical | XGBoost regime classifier (**82.6% OOS**) | `firm/ml.py`, `firm/regime.py` |
| Macro (Allora) | Allora decentralised-AI inference (BTC/ETH 8h) | `firm/allora_client.py` |
| On-chain / Risk | whale flow + GoPlus token-safety veto | `firm/whale_manager.py` |
| Research | Fear & Greed + CoinGecko market APIs | `firm/sentiment_feeds.py` |
| Smart-Money Flow | dynamic whale/contract flow + Nansen netflow | `firm/nansen_client.py` |
| Smart-Social | Elfa narratives / mindshare | `firm/elfa_client.py` |
| OI-Contrarian | perp Open-Interest extremes | `firm/edge_lab.py` |
| Carry | delta-neutral funding-carry (HYPE/SUI) | `firm/carry_signal.py` |
| Mantle Fundamentals | DeFiLlama chain TVL / fees / staking | `firm/defillama_client.py` |
| **TimesFM Vol/Risk** | **TimesFM 2.5 (200M) realized-vol forecast** | `firm/timesfm_desk.py` |

(+ `flow-intel` self-learning + Hyperliquid `whale` desk → 12 analyst voices to the PM.)

## What's validated — and what isn't (honesty first)
| Claim | Status |
|---|---|
| ⭐ **TimesFM volatility forecast** | **Significantly beats HAR-RV + EWMA** on BTC/ETH/SOL realized-vol — **Diebold-Mariano p<0.05, 3/3 walk-forward folds, two OOS windows** (`scripts/test_timesfm_vol_oos.py`). Drives vol-targeting sizing. *(MNT: a tie with HAR — stated honestly.)* |
| **Regime classifier** | **82.6% out-of-sample accuracy** — verified. But accuracy ≠ trading profit; its job is capital allocation + risk, not price prediction. |
| Directional edge scan | `firm/edge_lab.py` (cost-aware OOS + walk-forward + **Benjamini-Hochberg FDR**): **no directional edge currently earned** — the FDR gate rejected a +18.3% momentum result as a false positive → the firm **ABSTAINS**. |
| TimesFM *direction* + covariate-fusion | ❌ ~coin-flip, fee-eaten → **no edge** → published as honest findings (`scripts/test_timesfm_oos.py`, `test_timesfm_covariates_oos.py`). |
| Trend / funding / mETH-ETH convergence | ❌ failed cost-aware OOS / thin-liquidity artifacts → **retired** (we publish negatives). |

> Sessions frequently fire **0 trades** (no validated edge) — a correct, disciplined **ABSTAIN**, not a bug.

## 🟦 Bitget integration
- `firm/bitget_adapter.py` — **Bitget v2 REST API** (HMAC-SHA256), demo **SUSDT-FUTURES**: place/close market
  orders, positions, balances, one-way mode. **Verified long + short + flatten** (no real funds).
- `scripts/bitget_paper_trade.py` — desk-driven decisions **executed on Bitget demo** → Bitget-format log
  (`data/bitget_paper_log.json`). Demo pairs: BTC / ETH / XRP.

## Self-learning — assets EARN their edge
A new asset must **earn** its edge; the firm sharpens by evidence, never by tweaking the gate.
`edge_lab` (cost-aware OOS + walk-forward + FDR) discovers/validates/**retires** edges → `validated_edges.json`
(live-eligible) vs `candidate_edges.json` (paper-until-graduated); desk-reliability weights are learned
(bounded, advisory); decision hashes are anchored on-chain (auditable proof, not a claim).

## Risk engine (`firm/trade_ticket.py`)
Entry zone · structural stop · separate invalidation · TP-ladder, behind a hard **R:R ≥ 2:1 gate**. SAFE by
default; AGGRESSIVE = fractional-Kelly (**≤3% risk / ≤5× leverage**), **vol-targeting** (TimesFM realized-vol),
**20% drawdown breaker** — unlocked only when a validated edge fires. The LLM judges direction; deterministic
code computes the numbers; the gate enforces safety.

## Quickstart
```bash
python -m venv .venv
.venv/Scripts/activate            # Windows  (*nix: source .venv/bin/activate)
pip install -e .
cp .env.example .env              # add keys: BITGET_* , GROQ_API_KEY , (optional) TIMESFM_URL/TOKEN

python scripts/24_autonomous_org.py BTC            # one pass: 10 desks → debate → PM → ticket
python scripts/bitget_paper_trade.py               # execute on Bitget demo + write Bitget-format log
python scripts/test_timesfm_vol_oos.py BTC ETH SOL # reproduce the TimesFM vol validation (DM test)
python scripts/59_onboard_asset.py all --write     # edge-lab: earn an edge or abstain (OOS+WF+FDR)
```
📊 Backtest report: [`BACKTEST.md`](BACKTEST.md).

## On-chain
Decisions are anchored on-chain (Mantle Sepolia; ERC-8004 agent identity + ERC-8183 jobs) — auditable proof.
*(Chain-agnostic by design — the on-chain layer can target any EVM chain.)*

## Honest disclosures
- The headline is **rigor**, not a return. Validated: the TimesFM vol-forecaster (beats HAR-RV, DM p<0.05) +
  the regime classifier (82.6% OOS). Most directional "edges" decay — and we **retire** them.
- We retracted earlier headline numbers that failed full-history walk-forward, and rejected a +96% backtest
  (thin-liquidity artifact). The discipline that rejects a 96% backtest is worth more than the backtest.

## License
MIT
