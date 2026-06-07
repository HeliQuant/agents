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
   - **Only if `EXECUTE=1`:** `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET=true`
   - **Only if on-chain anchoring:** `DEPLOYER_PRIVATE_KEY`
   - Config: `ASSETS=MNT` · `INTERVAL_MIN=30` · `EXECUTE=0` · `REFRESH_DATA=1`
3. Deploy. Open the URL → `/` and `/logs`.

## ⚠️ Honest notes
- **Keep `INTERVAL_MIN ≥ 15`** — each cycle makes ~10 LLM calls (7 desks + debate + PM). Too frequent
  hits Groq rate limits. 30 min is comfortable; analysis isn't time-critical (HeliQuant trades rarely).
- **`EXECUTE=0` by default.** Live Bybit orders from a cloud IP may be **geo-restricted** (derivatives are
  region-banned; spot uncertain). Leave execution off until you've confirmed the venue is reachable from
  Railway. The analysis + self-learning + decisions are the always-on value.
- **The `/logs` endpoint redacts secret env values**, but treat the URL as semi-public; don't paste real
  keys into logs anywhere.
- **Cost**: an always-on service + continuous LLM calls accrue Railway + LLM usage. Tune `INTERVAL_MIN`.

## After deploy
Share the **`/logs`** URL with me — I'll fetch it to watch HeliQuant analyze + (rarely) trade, live.

---

## 🏗️ Modular architecture (exchanges are geo-blocked from the cloud)

Verified via `/probe`: from Railway's IP, **Bybit = 403 (CloudFront), Binance = 451** (blocked); **Hyperliquid + Groq work**. So the data/execution EDGE must run locally (where exchanges are reachable via WARP); the BRAIN runs in the cloud. They connect over HTTP:

```
  LOCAL (your machine, WARP on)                CLOUD (Railway, always-on)
  ├─ scripts/85 local data engine  ──POST /ingest──►  app.py brain
  │    fetch Bybit positioning → push          │    self-learning + 7-desk org + PM
  └─ scripts/82,83 local executor  ◄──/decisions──┘   /logs /status /decisions (monitor)
       execute firm ENTERs on Bybit
```

### Cloud setup (one extra var)
In Railway Variables add: **`INGEST_TOKEN=<any-long-random-string>`** (enables `/ingest`) and set
**`REFRESH_DATA=0`** (the cloud can't reach Bybit; it consumes the local engine's feed instead).

### Run the local engine (judges: this is your half)
On a machine that can reach Bybit (WARP on):
```bash
python scripts/85_local_engine.py MNT --cloud https://<your-app>.up.railway.app \
       --token <same-INGEST_TOKEN> --loop --interval 30
```
It fetches fresh data and POSTs it to the cloud every 30 min → the cloud brain self-learns on live data.
Verify on the cloud `/status` → `last_ingest` shows your latest push. Execution stays local
(`scripts/82`/`83`) since orders must hit Bybit from a reachable IP.
