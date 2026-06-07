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
import os
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
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
               "assets": ASSETS, "interval_min": INTERVAL_MIN, "execute": EXECUTE}


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


def _loop() -> None:
    log(f"HeliQuant loop starting: assets={ASSETS} interval={INTERVAL_MIN}min execute={EXECUTE}")
    while True:
        try:
            cycle()
            STATE["cycles"] += 1
            STATE["last_cycle_utc"] = datetime.now(timezone.utc).isoformat()
            STATE["last_error"] = None
        except Exception as e:  # noqa: BLE001
            STATE["last_error"] = str(e)[:200]
            log("LOOP ERROR: " + _sanitize(traceback.format_exc()[-400:]))
        time.sleep(INTERVAL_MIN * 60)


app = FastAPI(title="HeliQuant")


@app.on_event("startup")
def _start():
    threading.Thread(target=_loop, daemon=True).start()


@app.get("/health")
def health():
    return PlainTextResponse("ok")


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
                         "validated_edges": edges, "log_lines": len(LOGS)})


@app.get("/decisions")
def decisions():
    return JSONResponse(list(STATE["decisions"]))


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
