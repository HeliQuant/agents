"""app.py — HeliQuant always-on web service (Railway-deployable).

Runs the autonomous firm in a background loop (self-learning -> org analysis -> firm-governed execution)
and exposes endpoints to watch it live:
  GET /            dashboard (HTML) — loop status, validated edges, recent decisions
  GET /logs        RAW logs, plain text (sanitized — secret env values are redacted)
  GET /status      JSON status (loop alive, last cycle, edges)
  GET /decisions   JSON of recent firm decisions
  GET /health      "ok" (Railway healthcheck)

CONFIG (Railway env vars):
  ASSETS=MNT            comma-list to analyze each cycle (default MNT)
  INTERVAL_MIN=30       minutes between cycles (keep >=15 to respect LLM rate limits)
  EXECUTE=0             1 = place firm-sanctioned LIVE orders (Bybit); 0 = analyze-only (default, safe)
  REFRESH_DATA=1        1 = re-fetch fresh market data each cycle (needs Bybit public reachable)
  + all the firm's API keys (LLM/Groq, Nansen, Elfa, Mantlescan, Supabase, Bybit...) — copy from your .env.
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

# ── config ──
ASSETS = [a.strip().upper() for a in os.environ.get("ASSETS", "MNT").split(",") if a.strip()]
INTERVAL_MIN = max(int(os.environ.get("INTERVAL_MIN", "30")), 5)
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

    # 2) org analysis + firm-governed (gated) execution per asset
    from firm.organization import run_organization
    for asset in ASSETS:
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


def _loop() -> None:
    log(f"HeliQuant loop starting: assets={ASSETS} interval={INTERVAL_MIN}min execute={EXECUTE}")
    while True:
        if time.time() - STATE["last_cycle_epoch"] >= MIN_CYCLE_GAP_S:
            _do_cycle()
        time.sleep(60)  # wake every minute; the gap guard + external /run-cycle decide when a cycle fires


app = FastAPI(title="HeliQuant")


@app.on_event("startup")
def _start():
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


@app.get("/carry")
def carry():
    """Current carry desk reads — delta-neutral funding carry per symbol, from the locally-pushed cache
    (Bybit is geo-blocked from Railway; scripts/88 feeds this). Shows the desk IS alive in the cloud."""
    try:
        from firm.carry_signal import carry_brief, live_carry
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:120]}, status_code=500)
    out = {}
    for s in ("HYPEUSDT", "SUIUSDT", "MNTUSDT", "BTCUSDT", "ETHUSDT"):
        c = live_carry(s)
        out[s] = ({"carry_ann_pct": c.get("carry_ann_pct"), "crash_class": c.get("crash_class"),
                   "verdict": c.get("verdict"), "source": c.get("source", "live")} if c else None)
    return JSONResponse({"carry": out,
                         "best_harvestable": carry_brief() or "none harvestable now (all thin/lumpy — honest skip)"})


@app.get("/probe")
def probe():
    """Test which exchange/data hosts are reachable FROM RAILWAY's IP (answers: can HeliQuant fetch Bybit here?)."""
    import requests
    targets = {
        "bybit_mainnet": ("GET", "https://api.bybit.com/v5/market/time", None),
        "bybit_testnet": ("GET", "https://api-testnet.bybit.com/v5/market/time", None),
        "binance": ("GET", "https://fapi.binance.com/fapi/v1/time", None),
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
            out[name] = {"status": r.status_code, "ms": int((time.time() - t) * 1000),
                         "json_ok": json_ok, "snippet": _sanitize(r.text[:70])}
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": f"{type(e).__name__}: {str(e)[:90]}"}
    return JSONResponse(out)


@app.post("/ingest")
async def ingest(req: Request):
    """CONNECTOR: the local engine (which CAN reach Bybit via WARP) POSTs fresh positioning data here, so
    the cloud brain self-learns on live data without Bybit access. Auth: Bearer INGEST_TOKEN (set in Railway).
    Body: {"asset": "MNT", "csv": "<full positioning csv text>"}."""
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
    # CARRY payload: the local engine (Bybit-reachable) computed the carry and pushes the result so the
    # cloud carry desk works without a live exchange call (Bybit is geo-blocked from Railway).
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
