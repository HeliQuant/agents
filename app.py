"""app.py — HeliQuant always-on web service (Railway-deployable).

Runs the autonomous firm in a background loop (self-learning -> org analysis -> firm-governed execution)
and exposes endpoints to watch it live:
  GET /            dashboard (HTML) — loop status, validated edges, recent decisions
  GET /logs        RAW logs, plain text (sanitized — secret env values are redacted)
  GET /status      JSON status (loop alive, last cycle, edges)
  GET /decisions   JSON of recent firm decisions
  GET /health      "ok" (Railway healthcheck)

CONFIG (Railway env vars):
  ASSETS=BTC            comma-list to analyze each cycle (default BTC)
  INTERVAL_MIN=12       minutes between LLM org cycles (multi-key rotation lifts the old >=15 limit; floor is 5)
  EXECUTE=0             1 = place firm-sanctioned LIVE orders (Bitget); 0 = analyze-only (default, safe)
  REFRESH_DATA=1        1 = re-fetch fresh market data each cycle (needs Bitget public reachable)
  + all the firm's API keys (LLM/Groq, Nansen, Elfa, Mantlescan, Supabase, Bitget...) — copy from your .env.
  NEVER commit secrets; set them in Railway's Variables tab.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Load any locally-stored credentials (agents-localReady SQLite) into env BEFORE config reads them.
# NO-CUSTODY: reads only THIS machine's data/credentials.db (written via /register); never overwrites an
# env var already set, so Railway / .env always take precedence.
with contextlib.suppress(Exception):
    from firm import creds_store as _creds_store

    _creds_store.apply_to_env()

# ── config ──
ASSETS = [a.strip().upper() for a in os.environ.get("ASSETS", "MNT").split(",") if a.strip()]
INTERVAL_MIN = max(int(os.environ.get("INTERVAL_MIN", "30")), 5)  # org-cycle gap. 30min (not 12) keeps the
#   Groq FREE-tier daily quota alive: a 4–24h-horizon firm doesn't need to re-reason every 12min, and ~12min
#   cadence burned ~1800 calls/day across the 10 keys -> daily exhaustion (429 on every model). The no-LLM
#   floor is UNAFFECTED (its own 4min open / 60s resolve cadence). Override via INTERVAL_MIN env if needed.
EXECUTE = os.environ.get("EXECUTE", "0").strip() in {"1", "true", "yes"}
REFRESH_DATA = os.environ.get("REFRESH_DATA", "1").strip() in {"1", "true", "yes"}

# ── log buffer + secret sanitizer (the /logs endpoint is PUBLIC) ──
LOGS: deque[str] = deque(maxlen=4000)
_SECRETS = [v for k, v in os.environ.items()
            if any(s in k.upper() for s in ("KEY", "SECRET", "TOKEN", "PASSWORD", "PRIVATE", "MNEMONIC"))
            and isinstance(v, str) and len(v) >= 8]


def _sanitize(s: str) -> str:
    for sec in _SECRETS:
        s = s.replace(sec, "***REDACTED***")
    return s


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z {msg}"
    LOGS.append(_sanitize(line))


class _LogWriter(io.TextIOBase):
    """Captures stdout from the verbose org/self-learning into the log buffer (sanitized)."""
    def write(self, s):  # noqa: D401
        for part in str(s).splitlines():
            if part.strip():
                LOGS.append(_sanitize(part))
        return len(s)


STATE: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "cycles": 0,
               "last_cycle_utc": None, "last_error": None, "decisions": deque(maxlen=50),
               "assets": ASSETS, "interval_min": INTERVAL_MIN, "execute": EXECUTE, "last_ingest": None,
               "last_cycle_epoch": 0.0}

# Bulletproof external-trigger lock: an uptime pinger hits /run-cycle every few min — that keeps the
# container warm AND drives cycles independently of the internal sleep-loop (which a sleeping/suspended
# host can freeze). The gap guard means frequent pings are cheap keep-alive no-ops; a real cycle only
# fires every MIN_CYCLE_GAP_S. Lock prevents overlapping cycles.
_cycle_lock = threading.Lock()
MIN_CYCLE_GAP_S = max(INTERVAL_MIN * 60 - 120, 300)  # ~the interval, minus slack; >=5min floor


def _refresh(asset: str) -> None:
    if not REFRESH_DATA:
        return
    try:
        import scripts  # noqa: F401  (ensure path)
    except Exception:  # noqa: BLE001
        pass
    try:
        # reuse the generalized collector for fresh positioning data
        import importlib
        sys_path = str(ROOT)
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        coll = importlib.import_module("scripts.73_collect_alt") if (ROOT / "scripts" / "73_collect_alt.py").exists() else None
        if coll and hasattr(coll, "collect"):
            with contextlib.redirect_stdout(_LogWriter()):
                coll.collect(asset)
    except Exception as e:  # noqa: BLE001
        log(f"  [refresh {asset}] skipped: {str(e)[:80]}")


def cycle() -> None:
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    log("================ CYCLE START ================")
    # 1) self-learning: re-validate edges on current data (graduate/demote on evidence)
    try:
        from importlib import import_module
        sl = import_module("scripts.60_self_learn")
        log("-- self-learning (re-validate edges) --")
        with contextlib.redirect_stdout(_LogWriter()):
            old_argv = sys.argv
            sys.argv = ["60_self_learn.py"]  # dry (no --apply) so the loop only reports; promotion is deliberate
            try:
                sl.main()
            finally:
                sys.argv = old_argv
    except Exception as e:  # noqa: BLE001
        log(f"  [self-learn] error: {str(e)[:120]}")

    # 2) org analysis — ROTATE one asset per cycle (cuts LLM calls/cycle ~in half -> avoids Groq 429
    #    daily-quota burn; each asset still re-analyzed every len(ASSETS) cycles, fine for a 24h horizon).
    from firm.organization import run_organization
    rotated = [ASSETS[STATE["cycles"] % len(ASSETS)]] if ASSETS else []
    for asset in rotated:
        _refresh(asset)
        log(f"-- ORG analysis: {asset} --")
        try:
            with contextlib.redirect_stdout(_LogWriter()):
                res = run_organization(asset, verbose=True)
            dec = (res or {}).get("decision", {})
            d = str(dec.get("decision", "?")).upper()
            log(f"  >> PM: {d} {dec.get('direction','')} — {str(dec.get('reasoning') or dec.get('ticket_note') or '')[:160]}")
            STATE["decisions"].appendleft({"utc": datetime.now(timezone.utc).isoformat(), "asset": asset,
                                           "decision": d, "direction": dec.get("direction"),
                                           "reason": str(dec.get("reasoning") or dec.get("ticket_note") or "")[:240]})
            try:  # seal each desk's output on Mantle (ERC-8004 ValidationRegistry; gated by AGENT_ONCHAIN)
                from firm.agent_onchain import record_outputs
                recs = record_outputs(res.get("analysts", {}), asset, STATE["cycles"])
                if recs:
                    log(f"  [onchain] sealed {len(recs)} desk output(s) on Mantle · {recs[-1]['tx'][:14]}...")
            except Exception as e:  # noqa: BLE001
                log(f"  [onchain] seal skipped ({str(e)[:60]})")
            if EXECUTE and d == "ENTER" and str(dec.get("direction", "")).upper() == "LONG" \
                    and (dec.get("trade_ticket") or {}).get("valid"):
                log(f"  >> EXECUTE enabled + firm ENTER LONG -> (live order path)")
                # execution is intentionally conservative on cloud; wire scripts/83 here when venue reachable.
        except Exception as e:  # noqa: BLE001
            log(f"  [org {asset}] error: {str(e)[:140]}")
    log("================ CYCLE DONE ================")


def _do_cycle() -> bool:
    """Run ONE cycle under the lock (skips if one is already running). BOTH the internal loop and the
    external /run-cycle trigger go through here — so cycles never overlap. Returns True if it ran."""
    if not _cycle_lock.acquire(blocking=False):
        return False
    try:
        cycle()
        STATE["cycles"] += 1
        STATE["last_cycle_utc"] = datetime.now(timezone.utc).isoformat()
        STATE["last_cycle_epoch"] = time.time()
        STATE["last_error"] = None
    except Exception as e:  # noqa: BLE001
        STATE["last_error"] = str(e)[:200]
        log("CYCLE ERROR: " + _sanitize(traceback.format_exc()[-400:]))
    finally:
        _cycle_lock.release()
    return True


_campaign_last = 0.0   # last time NEW positions were opened
_resolve_last = 0.0    # last time matured positions were RESOLVED (closed)
_campaign_lock = threading.Lock()
CAMPAIGN_STEP_MIN = int(os.environ.get("CAMPAIGN_STEP_MIN", "4"))     # open NEW positions at most this often
RESOLVE_MIN_S = int(os.environ.get("CAMPAIGN_RESOLVE_S", "60"))        # but RESOLVE (close matured: near-TP/SL/trail) this often — snappy exits
CAMPAIGN_ON = os.environ.get("CAMPAIGN", "1") == "1"


def _campaign_step(open_new: bool = True) -> None:
    """One campaign step. open_new=True -> resolve matured + open new (the latter throttled to
    CAMPAIGN_STEP_MIN). open_new=False -> RESOLVE-ONLY (close matured positions only) so exits — the
    near-TP lock, trailing stop, SL/TP — fire within RESOLVE_MIN_S instead of waiting a whole open-cycle.
    Lock-guarded so concurrent callers (loop + FE-traffic kicks) never overlap. firm/campaign.py."""
    global _campaign_last, _resolve_last
    if not CAMPAIGN_ON or not _campaign_lock.acquire(blocking=False):
        return
    _resolve_last = time.time()
    if open_new:
        _campaign_last = time.time()
    try:
        from firm.campaign import step
        st = step(log=log, open_new=open_new)
        log(f"[campaign] {'step' if open_new else 'resolve'} · open {st.get('open_now')} · closed {st.get('closed')} "
            f"· net ${st.get('net_usd'):+.2f}")
    except Exception as e:  # noqa: BLE001
        log(f"[campaign] step error: {str(e)[:120]}")
    finally:
        _campaign_lock.release()


def _kick_campaign() -> None:
    """Opportunistic self-driver: any FE read of /campaign or /trades (and every keep-alive ping) advances
    the floor in the BACKGROUND. RESOLVE (close matured positions) fires every RESOLVE_MIN_S so exits stay
    snappy; OPENING new positions stays throttled to CAMPAIGN_STEP_MIN. Keeps advancing even when Railway
    suspends the internal loop. Non-blocking + lock-guarded -> cheap."""
    if not CAMPAIGN_ON:
        return
    now = time.time()
    do_open = now - _campaign_last >= CAMPAIGN_STEP_MIN * 60
    do_resolve = now - _resolve_last >= RESOLVE_MIN_S
    if do_open or do_resolve:
        threading.Thread(target=_campaign_step, args=(do_open,), daemon=True).start()


def _loop() -> None:
    log(f"HeliQuant loop starting: assets={ASSETS} interval={INTERVAL_MIN}min execute={EXECUTE} "
        f"campaign={'ON' if CAMPAIGN_ON else 'off'}")
    while True:
        if time.time() - STATE["last_cycle_epoch"] >= MIN_CYCLE_GAP_S:
            _do_cycle()
        _kick_campaign()   # resolve (close matured) every ~60s · open new every CAMPAIGN_STEP_MIN
        time.sleep(60)  # wake every minute; the gap guard + external /run-cycle decide when a cycle fires


app = FastAPI(title="HeliQuant")

# CORS: the dApp (heliquant.vercel.app) fetches these endpoints from the BROWSER (cross-origin). All
# routes are public, read-only GETs with no cookies/auth, so allow any origin — without this the
# browser blocks the fetch and the FE shows "floor unreachable" even though the API is up.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)


def _restore_positioning() -> None:
    """On boot, pull the latest positioning CSVs from Supabase (ephemeral FS loses the /ingest-fed ones on
    redeploy). Keeps the cloud on fresh data across redeploys — no manual re-feed needed."""
    try:
        from firm import state_store
        for a in ASSETS:
            csv = state_store.load(f"pos:{a.lower()}")
            if isinstance(csv, str) and len(csv) > 50:
                (DATA / f"{a.lower()}_positioning.csv").write_text(csv, encoding="utf-8")
                log(f"[restore] positioning {a} from Supabase ({csv.count(chr(10))} rows)")
    except Exception as e:  # noqa: BLE001
        log(f"[restore] skipped: {str(e)[:80]}")


@app.on_event("startup")
def _start():
    _restore_positioning()   # restore positioning from Supabase before the loop runs
    threading.Thread(target=_loop, daemon=True).start()


@app.get("/health")
def health():
    return PlainTextResponse("ok")


@app.api_route("/run-cycle", methods=["GET", "HEAD"])
def run_cycle():
    """Keep-alive + external cycle driver. Point an uptime pinger here (every few min). Accepts HEAD too —
    UptimeRobot & most monitors send HEAD by default (a GET-only route returns 405 and the ping is a no-op).
    Frequent pings are cheap no-ops (keep the host warm); a real cycle fires only when MIN_CYCLE_GAP_S has
    elapsed — robust even if the internal loop is frozen by a suspended host."""
    _kick_campaign()  # every keep-alive ping also advances the trading floor (guarded to <=1 step / CAMPAIGN_STEP_MIN)
    now = time.time()
    gap = now - STATE["last_cycle_epoch"]
    if gap < MIN_CYCLE_GAP_S:
        return JSONResponse({"status": "alive — next cycle not due yet", "cycles": STATE["cycles"],
                             "mins_to_next": round((MIN_CYCLE_GAP_S - gap) / 60, 1)})
    if _cycle_lock.locked():
        return JSONResponse({"status": "cycle already running", "cycles": STATE["cycles"]})
    threading.Thread(target=_do_cycle, daemon=True).start()
    return JSONResponse({"status": "cycle triggered", "cycles": STATE["cycles"]})


@app.get("/logs")
def logs(n: int = 400):
    return PlainTextResponse("\n".join(list(LOGS)[-n:]) or "(no logs yet — loop warming up)")


@app.get("/status")
def status():
    import json
    edges = {}
    try:
        edges = json.loads((DATA / "validated_edges.json").read_text())
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"started_utc": STATE["started_utc"], "cycles": STATE["cycles"],
                         "last_cycle_utc": STATE["last_cycle_utc"], "last_error": STATE["last_error"],
                         "assets": ASSETS, "interval_min": INTERVAL_MIN, "execute": EXECUTE,
                         "last_ingest": STATE.get("last_ingest"),
                         "validated_edges": edges, "log_lines": len(LOGS)})


@app.get("/decisions")
def decisions():
    return JSONResponse(list(STATE["decisions"]))


@app.get("/campaign")
def campaign_status():
    """THE LIVE CAMPAIGN progress — 100 desk-justified paper positions at live prices. Each open
    position carries the desk votes that justified it (auditable bravery; PM real-capital gate
    untouched). Watch it fill: opened/closed/win%/net + the live book."""
    _kick_campaign()  # FE traffic self-drives the floor — advances even if the keep-alive pinger lapses
    try:
        from firm.campaign import status
        return JSONResponse(status())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)


@app.get("/trades")
def trades(limit: int = 120):
    """THE TRADE LEDGER — every resolved campaign trade with its full data (entry/exit/PnL/exit-reason/
    desk-votes). Honest: these are paper trades at live prices (real fills on Bitget testnet when armed);
    the chain anchors the DECISIONS, not each paper fill."""
    _kick_campaign()  # FE traffic self-drives the floor — advances even if the keep-alive pinger lapses
    try:
        from firm.campaign import trade_log
        return JSONResponse(trade_log(limit))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120], "trades": []}, status_code=500)


@app.api_route("/campaign/rebaseline", methods=["GET", "POST"])
def campaign_rebaseline(token: str = ""):
    """OPS: reset the floor to a clean slate — the prior trades were under a superseded (broken-TP) config,
    so every asset is benched on losses that don't reflect the current logic. Discards that record and
    re-measures from zero. Token-gated. Do this ONCE after a config change, not to shop for a good run.
    Usage: /campaign/rebaseline?token=<INGEST_TOKEN>"""
    tok = os.environ.get("INGEST_TOKEN")
    if not tok or token != tok:
        return JSONResponse({"error": "bad or missing token — append ?token=<INGEST_TOKEN>"}, status_code=403)
    try:
        from firm.campaign import rebaseline
        r = rebaseline(log=log)
        _kick_campaign()
        return JSONResponse(r)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


@app.api_route("/campaign/unstick", methods=["GET", "POST"])
def campaign_unstick(token: str = ""):
    """OPS: clear leftover cooldowns / condition-bans (e.g. set under an older, longer COOLDOWN_H) so the
    floor re-evaluates all assets next step. Record-based bench + the discipline gates stay. Token-gated.
    Usage: /campaign/unstick?token=<INGEST_TOKEN>"""
    tok = os.environ.get("INGEST_TOKEN")
    if not tok or token != tok:
        return JSONResponse({"error": "bad or missing token — append ?token=<INGEST_TOKEN>"}, status_code=403)
    try:
        from firm.campaign import clear_bans
        r = clear_bans(log=log)
        _kick_campaign()  # advance a step now so freed assets get re-evaluated immediately
        return JSONResponse(r)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


@app.api_route("/campaign/test-open", methods=["GET", "POST"])
def campaign_test_open(asset: str = "SUI", side: str = "long", token: str = ""):
    """OPS/TEST: force-open ONE paper position now to verify the live mechanics (calibrated TP/SL, dynamic
    horizon, trailing) without waiting for an organic setup. Token-gated (INGEST_TOKEN). Counts in the
    ledger — use sparingly. Usage: /campaign/test-open?asset=SUI&token=<INGEST_TOKEN>"""
    tok = os.environ.get("INGEST_TOKEN")
    if not tok or token != tok:
        return JSONResponse({"error": "bad or missing token — append ?token=<INGEST_TOKEN>"}, status_code=403)
    try:
        from firm.campaign import test_open
        return JSONResponse(test_open(asset, side=side, log=log))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:160]}, status_code=500)


_AGENTS_CACHE: dict = {}


def _credential_counts(validation_addr: str) -> dict:
    """Outputs sealed per agent — count CredentialRecorded events (agentTokenId = indexed topic1) via
    Etherscan v2 getLogs. Reliable where the deployed credentialCountFor view under-reports."""
    out: dict = {}
    try:
        import collections

        import requests
        from web3 import Web3

        from firm.onchain_recorder import CHAIN_ID, _env
        topic0 = Web3.keccak(text="CredentialRecorded(uint256,uint256,bytes32,uint8,address)").hex()
        if not topic0.startswith("0x"):
            topic0 = "0x" + topic0
        r = requests.get("https://api.etherscan.io/v2/api", timeout=12, params={
            "chainid": CHAIN_ID, "module": "logs", "action": "getLogs", "address": validation_addr,
            "topic0": topic0, "fromBlock": 0, "toBlock": "latest", "apikey": _env("MANTLESCAN_API_KEY") or ""}).json()
        rows = r.get("result") if isinstance(r.get("result"), list) else []
        out = dict(collections.Counter(int(x["topics"][1], 16) for x in rows if len(x.get("topics", [])) > 1))
    except Exception:  # noqa: BLE001
        pass
    return out


def _agent_reps(reg: dict) -> dict:
    """Per-agent on-chain state read live from Mantle (cached ~120s): reputation (getReputation) +
    credentials sealed (credentialCountFor = how many desk outputs were recorded on-chain). Honest:
    reputation reads zero until outcomes resolve; credentials grow as the org seals each cycle's outputs."""
    hit = _AGENTS_CACHE.get("reps")
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    out: dict = {}
    try:
        from pathlib import Path

        from web3 import Web3

        from firm.onchain_recorder import MANTLE_SEPOLIA_RPC, _env
        abi_dir = Path(__file__).resolve().parent / "abi"  # bundled (contracts/out absent on Railway)
        rep_abi = json.loads((abi_dir / "ReputationRegistry.json").read_text())
        w3 = Web3(Web3.HTTPProvider(_env("MANTLE_SEPOLIA_RPC_URL") or MANTLE_SEPOLIA_RPC, request_kwargs={"timeout": 10}))
        rc = w3.eth.contract(address=Web3.to_checksum_address(reg["reputation"]), abi=rep_abi)
        ids = [reg.get("firm", {}).get("tokenId")] + [d.get("tokenId") for d in (reg.get("desks") or {}).values()]
        # credentials sealed per agent — tally CredentialRecorded events via Etherscan getLogs (the
        # deployed credentialCountFor view under-reports; the events are the source of truth).
        creds = _credential_counts(reg["validation"])
        for tid in [i for i in ids if i is not None]:
            rec: dict = {"credentials": creds.get(int(tid), 0)}
            try:
                r = rc.functions.getReputation(int(tid)).call()
                rec.update({"total_jobs": int(r[0]), "successful_jobs": int(r[1]), "cum_pnl": int(r[2])})
            except Exception:  # noqa: BLE001
                pass
            out[str(tid)] = rec
    except Exception:  # noqa: BLE001
        pass
    _AGENTS_CACHE["reps"] = (time.time(), out)
    return out


@app.get("/agents")
def agents():
    """THE AGENT ROSTER — the firm + 9 desks as ERC-8004 on-chain identities (IdentityRegistry). Each
    agent's tokenId, kind, ml flag, role + its live ON-CHAIN reputation, plus the off-chain
    desk_performance reliability weight for cross-reference. Honest: reputation accrues forward."""
    try:
        from pathlib import Path
        reg_fp = Path(__file__).resolve().parent / "data" / "agent_registry.json"
        if not reg_fp.exists():
            return JSONResponse({"agents": [], "error": "agents not registered yet"})
        reg = json.loads(reg_fp.read_text())
        try:
            from firm.desk_performance import load_weights
            weights = load_weights()
        except Exception:  # noqa: BLE001
            weights = {}
        reps = _agent_reps(reg)
        firm = reg.get("firm", {})
        out = [{"name": "HeliQuant", "kind": "Firm", "tokenId": firm.get("tokenId"), "ml": False,
                "role": "autonomous multi-desk AI trading firm", "reputation": reps.get(str(firm.get("tokenId")))}]
        for name, d in (reg.get("desks") or {}).items():
            out.append({"name": name, "kind": d.get("kind"), "tokenId": d.get("tokenId"), "ml": d.get("ml", False),
                        "weight": weights.get(name), "reputation": reps.get(str(d.get("tokenId")))})
        return JSONResponse({"agents": out, "identity": reg.get("identity"),
                             "explorer": reg.get("explorer", "https://sepolia.mantlescan.xyz")})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"agents": [], "error": str(e)[:120]}, status_code=200)


@app.get("/desks")
def desks():
    """DESK RELIABILITY (strategic self-learning) — each of the 9 desks earns a weight 0.6-1.4 by
    track record (its stance vs realized direction, >=15 samples). Neutral 1.0 until a desk proves
    itself; OI-Contrarian is seeded from its OOS replay. Surfaced for the /learning Tuning Bay."""
    try:
        from firm import desk_performance as dp
        out = {"desks": dp.DESKS, "bounds": [dp.LO, dp.HI], "min_samples": dp.MIN_SAMPLES,
               "weights": dp.load_weights(), "detail": {}}
        if dp.WEIGHTS.exists():
            out["detail"] = json.loads(dp.WEIGHTS.read_text()).get("detail", {})
        return JSONResponse(out)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)


@app.get("/edges")
def edges():
    """THE EDGE REGISTRY (edge-discovery self-learning) — validated_edges GATE real aggression;
    candidate_edges are on PROBATION (passed cost-aware OOS + walk-forward but awaiting fresh-data
    confirmation before graduating). Honest: 0 validated right now — the firm hunts, never fakes one."""
    try:
        from firm.desk_performance import ROOT

        def _rd(name: str) -> dict:
            fp = ROOT / "data" / name
            try:
                return json.loads(fp.read_text()) if fp.exists() else {}
            except (ValueError, OSError):
                return {}

        validated, candidate = _rd("validated_edges.json"), _rd("candidate_edges.json")
        # last_run = the latest REAL probation-confirmation bar stored on a candidate (scripts/60 writes
        # last_confirm_bar on a genuine new-data run). The registry stores no run-wide timestamp, so if no
        # entry carries one, last_run is null — honest, never a fabricated/mtime-derived time.
        bars = [e["last_confirm_bar"] for e in {**validated, **candidate}.values()
                if isinstance(e, dict) and e.get("last_confirm_bar")]
        return JSONResponse({"validated": validated, "candidate": candidate,
                             "last_run": max(bars) if bars else None})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)


@app.get("/performance")
def performance():
    """RIGOROUS analytics on the real trade ledger — Sharpe/Sortino/Calmar/maxDD/profit-factor, computed
    from resolved trades (return = pnl/size). quantstats-grade metrics, numpy-only (no deploy bloat).
    Honest: annualized figures are suppressed until the sample is meaningful (>=30d & >=30 trades) so a
    3-day high-frequency window can't extrapolate to a fantasy CAGR; per-trade stats always show."""
    _kick_campaign()
    try:
        from firm.campaign import trade_log
        from firm import perf_stats
        return JSONResponse(perf_stats.compute(trade_log(500).get("trades", [])))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)


@app.get("/sentiment")
def sentiment():
    """FREE market-context feeds (keyless): crypto Fear & Greed (alternative.me) + BTC network backdrop
    (blockchain.com). CONTEXT, not edge — a slow contrarian mood gauge surfaced for the dashboard, never
    a hard trade gate. Fails soft (fields go null on a feed outage)."""
    try:
        from firm import sentiment_feeds
        return JSONResponse(sentiment_feeds.read())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)


_ONCHAIN_CACHE: dict = {}   # wallet -> (epoch, payload). Etherscan v2 is rate-limited; cache ~60s.
FIRM_WALLET = "0x48379F4d1427209311E9FF0bcC4a354953ea631B"  # public anchor wallet (Mantle Sepolia)


@app.get("/onchain")
def onchain(wallet: str = "", limit: int = 40):
    """THE ON-CHAIN LEDGER — the firm's decision-anchor transactions, fetched LIVE from Etherscan v2
    (Mantle Sepolia, chainid 5003). Each anchor seals a decision's SHA-256 in a self-send tx's
    calldata — verifiable on Mantlescan, not a claim. Wallet is public; the Mantlescan API key stays
    server-side. Pass ?wallet=0x.. to inspect any address (BYO/modular). Cached ~60s."""
    import re

    import requests
    from firm.onchain_recorder import CHAIN_ID, _env
    addr = wallet.strip() or FIRM_WALLET
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return JSONResponse({"error": "bad address", "txs": []}, status_code=400)
    hit = _ONCHAIN_CACHE.get(addr.lower())
    if hit and time.time() - hit[0] < 60:
        return JSONResponse(hit[1])
    key = _env("MANTLESCAN_API_KEY") or ""
    try:
        r = requests.get("https://api.etherscan.io/v2/api", timeout=15, params={
            "chainid": CHAIN_ID, "module": "account", "action": "txlist", "address": addr,
            "startblock": 0, "endblock": 99999999, "sort": "desc", "apikey": key}).json()
        rows = r.get("result") if isinstance(r.get("result"), list) else []
        txs = [{"hash": t["hash"], "block": int(t["blockNumber"]), "ts": int(t["timeStamp"]),
                "to": t.get("to", ""), "from": t.get("from", ""), "value": t.get("value", "0"),
                "method": t.get("methodId", "0x"), "is_error": t.get("isError", "0") == "1",
                "self_anchor": t.get("to", "").lower() == addr.lower() and t.get("from", "").lower() == addr.lower()}
               for t in rows[:max(1, min(limit, 100))]]
        payload = {"wallet": addr, "chain": "Mantle Sepolia", "chain_id": CHAIN_ID,
                   "explorer": "https://sepolia.mantlescan.xyz", "total": len(rows), "txs": txs,
                   "configured": bool(key)}
        _ONCHAIN_CACHE[addr.lower()] = (time.time(), payload)
        return JSONResponse(payload)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"wallet": addr, "txs": [], "error": str(e)[:120],
                             "configured": bool(key)}, status_code=200)


_CANDLE_CACHE: dict = {}   # (asset,interval) -> (epoch, payload). Bitget mainnet kline (keyless).
_BG_GRAN = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1H", "120": "2H",
            "240": "4H", "360": "6H", "720": "12H", "D": "1D", "1D": "1D", "W": "1W"}


@app.get("/candles")
def candles(asset: str = "BTC", interval: str = "60", limit: int = 120):
    """Recent OHLC candles for an asset (Bitget mainnet perp, keyless — served from the cloud so any
    viewer gets them regardless of their own geo). Cached ~45s. Feeds the live-floor candlestick that
    shows each open campaign position against its entry / SL / TP."""
    import requests
    key = (asset.upper(), interval)
    hit = _CANDLE_CACHE.get(key)
    if hit and time.time() - hit[0] < 45:
        return JSONResponse(hit[1])
    try:
        gran = _BG_GRAN.get(interval, "1H")
        r = requests.get("https://api.bitget.com/api/v2/mix/market/candles",
                         params={"symbol": f"{asset.upper()}USDT", "productType": "usdt-futures",
                                 "granularity": gran, "limit": str(min(max(limit, 10), 200))}, timeout=12).json()
        rows = r.get("data") or []  # Bitget returns oldest-first
        out = [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
        payload = {"asset": asset.upper(), "interval": interval, "candles": out}
        _CANDLE_CACHE[key] = (time.time(), payload)
        return JSONResponse(payload)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"asset": asset.upper(), "interval": interval, "candles": [], "error": str(e)[:120]})


@app.get("/carry")
def carry():
    """Current carry desk reads — delta-neutral funding carry per symbol, computed live from keyless
    Bitget mainnet funding (cloud-reachable; falls back to the scripts/88 cache on a feed hiccup)."""
    try:
        from firm.carry_signal import carry_brief, live_carry
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)
    out = {}
    asofs = []
    for s in ("HYPEUSDT", "SUIUSDT", "BTCUSDT", "ETHUSDT", "XRPUSDT"):
        c = live_carry(s)
        if c and c.get("asof"):
            asofs.append(c["asof"])
        out[s] = ({"carry_ann_pct": c.get("carry_ann_pct"), "crash_class": c.get("crash_class"),
                   "verdict": c.get("verdict"), "source": c.get("source", "live")} if c else None)
    # asof = real compute time (funding fetched live from Bitget this request), or the cache's stamp on
    # fallback. Most-recent across symbols, or null if none carry a timestamp yet — a null is honest; a
    # fabricated freshness time is not.
    return JSONResponse({"asof": max(asofs) if asofs else None, "carry": out,
                         "best_harvestable": carry_brief() or "none harvestable now (all thin/lumpy — honest skip)"})


@app.get("/whales")
def whales():
    """THE WHALE DESK — what Hyperliquid's top-30 traders (by 30d PnL) are DOING right now in each
    HL-covered asset: net long vs short, notional, avg ROE, stance. Live positions (clearinghouse +
    leaderboard, cached internally), reachable from Railway. MNT is NOT on Hyperliquid -> excluded.
    Honest: HL is ONE venue; on a read error an asset returns a neutral empty shape, never a guess."""
    from firm.hl_whales import whale_read, whale_detail
    assets = []
    for a in ("BTC", "ETH", "SOL", "HYPE", "SUI"):
        try:
            r = whale_read(a, n=30)
            r["whales"] = whale_detail(a, n=30)  # the individual wallets, clickable on the page
            assets.append(r)
        except Exception:  # noqa: BLE001  -> honest empty shape, never fabricated data
            assets.append({"asset": a, "whales_in_position": 0, "long": 0, "short": 0, "long_usd": 0,
                           "short_usd": 0, "net_usd": 0, "avg_roe_pct": None, "stance": "NEUTRAL",
                           "n_tracked": 0, "brief": f"{a}: whale read unavailable", "whales": []})
    return JSONResponse({"asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "source": "Hyperliquid top-30 by 30d PnL — live positions", "assets": assets})


@app.get("/probe")
def probe():
    """Test which exchange/data hosts are reachable FROM RAILWAY's IP (answers: can HeliQuant fetch Bitget here?)."""
    import requests
    targets = {
        "bitget_mainnet": ("GET", "https://api.bitget.com/api/v2/public/time", None),
        "bitget_mix": ("GET", "https://api.bitget.com/api/v2/mix/market/ticker?symbol=BTCUSDT&productType=usdt-futures", None),
        "hyperliquid": ("POST", "https://api.hyperliquid.xyz/info", {"type": "meta"}),
        "groq": ("GET", "https://api.groq.com/openai/v1/models", None),
    }
    out = {}
    for name, (method, url, body) in targets.items():
        try:
            t = time.time()
            r = requests.post(url, json=body, timeout=12) if method == "POST" else requests.get(url, timeout=12)
            try:
                r.json(); json_ok = True
            except Exception:  # noqa: BLE001
                json_ok = False
            # ANY HTTP response = the host was REACHED. A 401/403 here just means this probe sent no API
            # key (it tests reachability, not auth) — it does NOT mean the firm's real keys are bad.
            out[name] = {"status": r.status_code, "reachable": True,
                         "note": "reachable (auth not tested — 401/403 here is expected, probe sends no key)"
                                 if r.status_code in (401, 403) else "reachable",
                         "ms": int((time.time() - t) * 1000), "json_ok": json_ok, "snippet": _sanitize(r.text[:70])}
        except Exception as e:  # noqa: BLE001
            out[name] = {"reachable": False, "error": f"{type(e).__name__}: {str(e)[:90]}"}
    return JSONResponse(out)


@app.get("/bitget-probe")
def bitget_probe(order: int = 0):
    """Can HeliQuant place Bitget DEMO orders FROM THIS REGION? Runs a SIGNED private call (balance) —
    unlike /probe (reachability only), this answers whether orders are geo-ALLOWED here. Deploy to a
    Railway region, hit this, and read `bitget.connected`: true = orders allowed; false = blocked here."""
    import os
    import requests
    region = (os.environ.get("RAILWAY_REGION") or os.environ.get("RAILWAY_REPLICA_REGION")
              or os.environ.get("FLY_REGION") or os.environ.get("REGION") or "unknown")
    out = {"region": region}
    try:
        out["egress_ip"] = requests.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception:  # noqa: BLE001
        out["egress_ip"] = None
    try:
        from firm import bitget_adapter as bg
        out["bitget"] = bg.status()  # signed private get-balance → connected:true iff account reachable here
        if order:  # ?order=1 → DECISIVE test: open+close a tiny demo LONG then SHORT from THIS region
            res: dict = {}
            try:
                bg.set_position_mode(True)
                for label, side in (("long", "buy"), ("short", "sell")):
                    bg.place_market_order("BTC", side, 0.001)
                    time.sleep(1.0)
                    pos = bg.get_positions()
                    res[label] = {"opened": (pos[0].get("holdSide") if pos else None) == label,
                                  "holdSide": pos[0].get("holdSide") if pos else None}
                    bg.flatten()
                    time.sleep(0.8)
                both = res.get("long", {}).get("opened") and res.get("short", {}).get("opened")
                out["order_test"] = {**res, "verdict": "LONG + SHORT WORK FROM THIS REGION" if both else "partial — check holdSide"}
            except Exception as e:  # noqa: BLE001
                out["order_test"] = {**res, "verdict": "ORDER BLOCKED/FAILED HERE", "error": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        out["bitget"] = {"error": str(e)[:200]}
    return JSONResponse(out)


@app.get("/bitget-data")
def bitget_data(asset: str = "BTC", candles: int = 0):
    """Public Bitget MAINNET market data (keyless) — funding, open-interest, last, 24h vol; optional 1H
    candles (?asset=BTC&candles=50). A keyless read of the firm's own execution venue."""
    try:
        from firm import bitget_adapter as bg
        out: dict = {"snapshot": bg.market_snapshot(asset)}
        if candles:
            out["candles"] = bg.public_candles(asset, "1H", min(int(candles), 200))
        return JSONResponse(out)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:160]}, status_code=200)


@app.get("/setup-status")
def setup_status():
    """Onboarding status — which credential KEY NAMES this engine has on file (never values) + whether
    /register is enabled (HQ_SETUP_TOKEN set). The onboarding form reads this to show progress."""
    from firm import creds_store
    return JSONResponse({"register_enabled": bool(os.environ.get("HQ_SETUP_TOKEN", "").strip()),
                         "stored_keys": creds_store.stored_keys()})


@app.post("/register")
async def register(req: Request):
    """NO-CUSTODY onboarding (agents-localReady). The user's OWN onboarding form POSTs their keys to THEIR
    OWN engine (this process) over their tunnel — the secrets never touch the shared FE server. Stored in
    this engine's local SQLite + applied to the running process. Gated by HQ_SETUP_TOKEN (set it when you
    run the engine; enter it in onboarding). Body: {"token": "...", "creds": {"GROQ_API_KEY": "...", ...}}."""
    from firm import creds_store
    setup_token = os.environ.get("HQ_SETUP_TOKEN", "").strip()
    if not setup_token:
        return JSONResponse({"error": "register disabled — set HQ_SETUP_TOKEN on the engine to enable onboarding"}, status_code=503)
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "bad json"}, status_code=400)
    if str(body.get("token", "")) != setup_token:
        return JSONResponse({"error": "bad or missing setup token"}, status_code=403)
    creds = body.get("creds")
    if not isinstance(creds, dict) or not creds:
        return JSONResponse({"error": "creds must be a non-empty object"}, status_code=400)
    n = creds_store.save_creds(creds)
    creds_store.apply_to_env()
    return JSONResponse({"ok": True, "saved": n, "keys": creds_store.stored_keys(),
                         "note": "stored in this engine's local SQLite + applied to the running process"})


@app.post("/ingest")
async def ingest(req: Request):
    """CONNECTOR: the local engine POSTs supplementary data here (carry, positioning) so the cloud brain
    has it even when a particular feed is thin. Auth: Bearer INGEST_TOKEN (set in Railway).
    Body: {"asset": "BTC", "csv": "<full positioning csv text>"} or a carry payload."""
    token = os.environ.get("INGEST_TOKEN")
    if not token:
        return JSONResponse({"error": "ingest disabled — set INGEST_TOKEN in Railway to enable"}, status_code=503)
    if req.headers.get("authorization", "") != f"Bearer {token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "bad json"}, status_code=400)
    asset = str(body.get("asset", "")).upper()
    if not asset:
        return JSONResponse({"error": "need asset"}, status_code=400)
    # CARRY payload: a fallback path — the cloud now computes carry live from Bitget directly, but the
    # local engine can still push a precomputed result here in case the live fetch is thin.
    carry = body.get("carry")
    if isinstance(carry, dict):
        try:
            (DATA / f"{asset.lower()}_carry.json").write_text(json.dumps(carry), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"carry write failed: {str(e)[:80]}"}, status_code=500)
        log(f"[ingest] received {asset} carry: {carry.get('carry_ann_pct')}%/yr ({str(carry.get('verdict',''))[:40]})")
        return JSONResponse({"ok": True, "asset": asset, "carry_ann_pct": carry.get("carry_ann_pct")})
    # POSITIONING csv payload (default)
    csv_text = body.get("csv")
    if not isinstance(csv_text, str) or len(csv_text) < 50:
        return JSONResponse({"error": "need asset + csv (positioning) OR carry (dict)"}, status_code=400)
    try:
        (DATA / f"{asset.lower()}_positioning.csv").write_text(csv_text, encoding="utf-8")
        from firm import state_store
        state_store.save(f"pos:{asset.lower()}", csv_text)   # persist -> survives redeploys (restored on boot)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"write failed: {str(e)[:80]}"}, status_code=500)
    rows = csv_text.count("\n")
    last = csv_text.strip().splitlines()[-1].split(",")[1] if "\n" in csv_text else "?"
    STATE["last_ingest"] = {"asset": asset, "rows": rows, "last_bar": last,
                            "utc": datetime.now(timezone.utc).isoformat()}
    log(f"[ingest] received {asset}: {rows} rows, last bar {last} (from local engine)")
    return JSONResponse({"ok": True, "asset": asset, "rows": rows, "last_bar": last})


@app.get("/", response_class=HTMLResponse)
def home():
    last = STATE["last_cycle_utc"] or "warming up"
    decs = "".join(f"<tr><td>{d['utc'][11:19]}</td><td>{d['asset']}</td><td><b>{d['decision']}</b> {d.get('direction') or ''}</td>"
                   f"<td>{(d.get('reason') or '')[:120]}</td></tr>" for d in list(STATE['decisions'])[:15])
    return f"""<html><head><title>HeliQuant — live</title>
<style>body{{font:14px monospace;background:#0b0e11;color:#cdd;padding:24px}}a{{color:#9f9}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #234;padding:4px 8px;text-align:left}}</style></head>
<body><h2>🛰️ HeliQuant — autonomous firm (live)</h2>
<p>cycles: <b>{STATE['cycles']}</b> · last: {last} · assets: {','.join(ASSETS)} · interval: {INTERVAL_MIN}min ·
execute: {EXECUTE} · error: {STATE['last_error'] or 'none'}</p>
<p>📜 <a href="/logs">/logs</a> (raw, sanitized) · <a href="/status">/status</a> · <a href="/decisions">/decisions</a></p>
<h3>recent firm decisions</h3><table><tr><th>utc</th><th>asset</th><th>decision</th><th>why</th></tr>{decs or '<tr><td colspan=4>warming up…</td></tr>'}</table>
<p style="color:#789">Disciplined by design — it ABSTAINS far more than it trades. Few trades = the gates working.</p>
</body></html>"""
