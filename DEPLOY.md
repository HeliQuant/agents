# 🛰️ Deploy HeliQuant to Railway (always-on)

`app.py` runs the autonomous firm in a background loop and exposes live endpoints. Railway-ready
(`Procfile`, `requirements.txt`, `runtime.txt`, `railway.json`).

## What it does each cycle (default every 30 min)
1. **Self-learning** — re-validates edges on current data (graduate/demote on evidence; report-only).
2. **Org analysis** — runs the 7-desk + PM firm on each asset → a disciplined decision (mostly ABSTAIN).
3. **(optional) Execution** — only firm-sanctioned ENTERs, and only if `EXECUTE=1` (default OFF, safe).

## Endpoints (once deployed, Railway gives a public URL)
| path | what |
|---|---|
| `/` | dashboard: cycles, last decision, recent firm decisions |
| `/logs` | **RAW logs, plain text** (secret env values auto-redacted) — share this with me to monitor |
| `/status` | JSON: loop alive, last cycle, validated edges |
| `/decisions` | JSON: recent firm decisions |
| `/health` | `ok` (Railway healthcheck) |

## Deploy steps
1. **railway.com → New Project → Deploy from GitHub repo → `HeliQuant/agents`** (this repo).
   (CLI alt: `railway login` → `railway init` → `railway up`.)
2. **Variables tab → add your env vars** (copy values from your local `agents/.env` — NEVER commit them):
   - LLM: `GROQ_API_KEY` (+ any additional Groq keys your `firm/llm_client.py` rotates)
   - Desks: `NANSEN_API_KEY`, `ELFA_API_KEY`, `MANTLESCAN_API_KEY`
   - Memory: `SUPABASE_URL`, `SUPABASE_KEY` (service_role)
   - **Execution (Bitget DEMO):** `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE`, `BITGET_DEMO=1`, `BITGET_EXECUTE=1` (BTC/ETH/XRP fill on the demo; SOL/HYPE/SUI paper)
   - **Only if on-chain anchoring:** `DEPLOYER_PRIVATE_KEY`
   - Config: `ASSETS=BTC` · `INTERVAL_MIN=30` · `EXECUTE=0` · `REFRESH_DATA=1`
3. Deploy. Open the URL → `/` and `/logs`.

## ⚠️ Honest notes
- **Keep `INTERVAL_MIN ≥ 15`** — each cycle makes ~10 LLM calls (7 desks + debate + PM). Too frequent
  hits Groq rate limits. 30 min is comfortable; analysis isn't time-critical (HeliQuant trades rarely).
- **Bitget DEMO execution runs from the cloud.** Bitget's keyless public data AND its SUSDT-FUTURES demo
  are reachable from Railway, so the firm fetches data and places demo fills (BTC/ETH/XRP) directly in the
  cloud — no local relay needed. `EXECUTE` gates the LLM-org's own orders (default OFF); the campaign
  floor's demo fills are gated by `BITGET_EXECUTE`. Real money still requires a validated edge.
- **The `/logs` endpoint redacts secret env values**, but treat the URL as semi-public; don't paste real
  keys into logs anywhere.
- **Cost**: an always-on service + continuous LLM calls accrue Railway + LLM usage. Tune `INTERVAL_MIN`.

## After deploy
Share the **`/logs`** URL with me — I'll fetch it to watch HeliQuant analyze + (rarely) trade, live.

---

## 🏗️ Architecture — cloud-native on Bitget

Verified via `/probe`: from Railway's IP, **Bitget public data + demo are reachable**, and **Hyperliquid + Groq work**. So the firm runs **end-to-end in the cloud** — no local half required:

```
  CLOUD (Railway, always-on)
  ├─ app.py brain   self-learning + multi-desk org + PM + the live campaign floor
  ├─ data           keyless Bitget mainnet — price / kline / funding / OI  (REFRESH_DATA=1)
  └─ execution      Bitget DEMO fills (BTC/ETH/XRP) when BITGET_EXECUTE=1; SOL/HYPE/SUI paper
                    monitor live via  /logs  /status  /decisions  /campaign
```

### Optional: local data push
The `/ingest` connector still exists (set **`INGEST_TOKEN`**) so a local engine can push *supplementary*
data (e.g. a precomputed carry result) — but it is **optional**. The cloud reads Bitget directly and is
self-sufficient, so keep **`REFRESH_DATA=1`** and let it fetch its own live data.
