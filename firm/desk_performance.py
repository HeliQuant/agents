"""firm/desk_performance.py — ADDITIVE self-learning: the org learns which of its 7 desks have been
RELIABLE and surfaces that as an ADVISORY prior to the PM. A NEW skill, layered on — it does NOT
change how the org works today.

STORAGE (compact, table-backed — not raw blobs):
  * Local: a compact SQLite table `desk_outcomes` in data/heliquant_memory.db (the shared learning DB).
  * Cloud: Supabase table `desk_outcomes_hq` (same project as decisions_hq) — used automatically when
    SUPABASE_URL + SUPABASE_KEY (service_role) are set; the frontend can read it (RLS public-read).
  Each row is TINY: ticker · desk · stance('L'/'S') · realized('L'/'S') · source. `aligned` is
  derived on read (stance == realized) — nothing redundant stored.

NON-DESTRUCTIVE BY DESIGN:
  * Own table + own file (desk_weights.json) — no schema of the org is touched.
  * No data -> load_weights() returns ALL-NEUTRAL (1.0) -> the org behaves EXACTLY as before.
  * Weights are BOUNDED to [0.6, 1.4] -> no desk is ever muted; every desk keeps its voice.
  * It NEVER touches the R:R gate, validation gate, or AGGRESSIVE-edge rules — only ONE advisory line.

A desk earns its weight by TRACK RECORD (stance vs realized direction), accumulated forward from live
runs (log_outcome) + seeded from the one desk we can replay on history (OI-Contrarian).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "heliquant_memory.db"          # shared local learning DB (also used by memory_store)
WEIGHTS = ROOT / "data" / "desk_weights.json"        # computed current state (small) — the org reads this
SB_TABLE = "desk_outcomes_hq"                         # Supabase table (cloud mirror, FE-readable)

DESKS = ["Regime/Technical", "Macro (Allora)", "On-chain/Risk", "Research",
         "Smart-Money Flow", "Smart-Social", "OI-Contrarian"]
LO, HI = 0.6, 1.4      # weight bounds — never mute a desk
NEUTRAL = 1.0
MIN_SAMPLES = 15       # need >= 15 resolved samples before a desk's weight moves off neutral

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_outcomes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT, desk TEXT,
    stance     TEXT,   -- 'L' | 'S'
    realized   TEXT,   -- 'L' | 'S'  (aligned = stance==realized, computed on read)
    source     TEXT DEFAULT 'live',
    created_at TEXT DEFAULT (datetime('now'))
);"""

# Transient working state: desk stances awaiting their 24h outcome (local only — scored -> desk_outcomes).
_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_pending (
    ticker TEXT, made_epoch REAL, entry REAL, desk TEXT, stance TEXT
);"""


def _env(key: str) -> str | None:
    v = os.environ.get(key)
    if v:
        return v
    for envfile in (ROOT / ".env", ROOT.parent / ".env"):  # agents/.env, then project-root .env
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                for line in envfile.read_text(encoding=enc).splitlines():
                    line = line.lstrip("﻿").strip()
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
            except (UnicodeError, ValueError, FileNotFoundError):
                continue
    return None


def _supabase():
    """Supabase client if creds exist (service_role for writes), else None -> SQLite fallback."""
    url, key = _env("SUPABASE_URL"), _env("SUPABASE_KEY")
    if not (url and key):
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception:  # noqa: BLE001
        return None


def _sqlite():
    c = sqlite3.connect(str(DB))
    c.execute(_SQLITE_SCHEMA)
    c.commit()
    return c


def _write(rows: list[dict]) -> str:
    """Persist compact outcome rows to the active backend (Supabase if keyed, else SQLite)."""
    if not rows:
        return "noop"
    sb = _supabase()
    if sb:
        sb.table(SB_TABLE).insert(rows).execute()
        return "supabase"
    c = _sqlite()
    c.executemany("INSERT INTO desk_outcomes (ticker,desk,stance,realized,source) VALUES (?,?,?,?,?)",
                  [(r["ticker"], r["desk"], r["stance"], r["realized"], r["source"]) for r in rows])
    c.commit()
    c.close()
    return "sqlite"


def _read() -> list[dict]:
    sb = _supabase()
    if sb:
        return sb.table(SB_TABLE).select("ticker,desk,stance,realized").execute().data or []
    c = _sqlite()
    rows = [{"ticker": t, "desk": d, "stance": s, "realized": r}
            for t, d, s, r in c.execute("SELECT ticker,desk,stance,realized FROM desk_outcomes").fetchall()]
    c.close()
    return rows


def _clear_source(src: str) -> None:
    """Idempotent re-seed: drop prior rows from a given source before re-writing them."""
    sb = _supabase()
    if sb:
        sb.table(SB_TABLE).delete().eq("source", src).execute()
        return
    c = _sqlite()
    c.execute("DELETE FROM desk_outcomes WHERE source=?", (src,))
    c.commit()
    c.close()


def _ls(direction: str) -> str | None:
    d = str(direction).upper()
    return "L" if d in ("LONG", "L") else "S" if d in ("SHORT", "S") else None


def _stance_dir(stance: str) -> str | None:
    s = str(stance).lower()
    if any(k in s for k in ("bull", "long", "accumul")):
        return "L"
    if any(k in s for k in ("bear", "short", "distrib")):
        return "S"
    return None  # neutral/avoid/unavailable -> no directional call -> not scored


def log_outcome(ticker: str, analysts: dict, realized_direction: str, *, source: str = "live") -> int:
    """Forward hook: when a decision RESOLVES, score each desk's stance vs realized direction and
    write compact rows. Call from the live loop once an outcome is known. Returns #rows written."""
    rd = _ls(realized_direction)
    if rd is None:
        return 0
    rows = [{"ticker": ticker.upper(), "desk": d, "stance": _stance_dir((analysts.get(d) or {}).get("stance", "")),
             "realized": rd, "source": source} for d in DESKS]
    rows = [r for r in rows if r["stance"] is not None]
    _write(rows)
    return len(rows)


def seed_oi_from_replay(ticker: str = "MNT") -> int:
    """Seed with the OI-Contrarian desk's REAL historical alignment (the one desk we can replay).
    Idempotent: clears prior replay rows first."""
    import numpy as np
    import pandas as pd
    fp = ROOT / "data" / f"{ticker.lower()}_positioning.csv"
    if not fp.exists():
        return 0
    df = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    oichg = df["oi"].pct_change(24).values
    n = len(df)
    idx = [i for i in range(24, n - 24, 24) if not np.isnan(oichg[i])]
    if len(idx) < 30:
        return 0
    sig = np.array([oichg[i] for i in idx])
    p20, p80 = np.nanpercentile(sig, 20), np.nanpercentile(sig, 80)  # OI edge is contrarian (known)
    rows = []
    for i in idx:
        s = oichg[i]
        stance = "L" if s <= p20 else "S" if s >= p80 else None  # contrarian: fade the extreme
        if stance is None:
            continue
        rows.append({"ticker": ticker.upper(), "desk": "OI-Contrarian", "stance": stance,
                     "realized": "L" if c[i + 24] >= c[i] else "S", "source": "replay:oi"})
    _clear_source("replay:oi")
    _write(rows)
    return len(rows)


def _weights_from(rows: list[dict]) -> tuple[dict, dict]:
    """Compute (weights, detail) for one subset of outcome rows. weight = clamp(2*align_rate) once
    >= MIN_SAMPLES, else neutral. align derived as stance==realized."""
    tally: dict[str, list[int]] = {d: [0, 0] for d in DESKS}  # [aligned, total]
    for r in rows:
        d = r.get("desk")
        if d in tally:
            tally[d][1] += 1
            tally[d][0] += int(r.get("stance") == r.get("realized"))
    weights, detail = {}, {}
    for d in DESKS:
        aligned, total = tally[d]
        if total >= MIN_SAMPLES:
            rate = aligned / total
            w = round(min(HI, max(LO, 2.0 * rate)), 2)
        else:
            rate, w = (aligned / total if total else None), NEUTRAL
        weights[d] = w
        detail[d] = {"weight": w, "samples": total, "align_rate": round(rate, 3) if rate is not None else None}
    return weights, detail


def compute_weights() -> dict:
    """PER-ASSET desk reliability (a desk can be great for MNT, useless for HYPE) + a global pool.
    Writes desk_weights.json: top-level `weights`/`detail` = GLOBAL (back-compat) + `by_asset`.
    Returns {by_asset, global}."""
    rows = _read()
    assets = sorted({r.get("ticker") for r in rows if r.get("ticker")})
    by_asset = {}
    for a in assets:
        w, d = _weights_from([r for r in rows if r.get("ticker") == a])
        by_asset[a] = {"weights": w, "detail": d}
    gw, gd = _weights_from(rows)  # global pool (all assets)
    WEIGHTS.write_text(json.dumps({"weights": gw, "detail": gd, "by_asset": by_asset,
                                   "bounds": [LO, HI], "min_samples": MIN_SAMPLES,
                                   "backend": "supabase" if _supabase() else "sqlite"}, indent=2))
    return {"by_asset": by_asset, "global": gd}


def stash_pending(ticker: str, entry: float, analysts: dict, made_epoch: float) -> int:
    """At decision time, stash each desk's directional stance so it can be SCORED when the 24h
    window matures. Local SQLite (transient working state). Returns #desks stashed."""
    rows = [(ticker.upper(), float(made_epoch), float(entry), d,
             _stance_dir((analysts.get(d) or {}).get("stance", ""))) for d in DESKS]
    rows = [r for r in rows if r[4]]  # only desks that took a directional view
    if not rows:
        return 0
    c = _sqlite()
    c.execute(_PENDING_SCHEMA)
    c.executemany("INSERT INTO desk_pending (ticker,made_epoch,entry,desk,stance) VALUES (?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return len(rows)


def resolve_matured(price_by_ticker: dict, now_epoch: float, hold_h: float = 24.0) -> int:
    """Score every pending stance whose 24h window has elapsed: realized = current price vs entry,
    aligned = stance==realized. Writes results to desk_outcomes (Supabase/SQLite), clears pending.
    This is how the 6 LLM/API desks earn real weights FORWARD as live decisions mature. Returns #scored."""
    c = _sqlite()
    c.execute(_PENDING_SCHEMA)
    pend = c.execute("SELECT rowid,ticker,made_epoch,entry,desk,stance FROM desk_pending").fetchall()
    c.close()
    scored, done = [], []
    for rowid, tk, made, entry, desk, stance in pend:
        cur = price_by_ticker.get(tk)
        if cur is None or (now_epoch - made) < hold_h * 3600:
            continue
        scored.append({"ticker": tk, "desk": desk, "stance": stance,
                       "realized": "L" if float(cur) >= entry else "S", "source": "live"})
        done.append(rowid)
    if scored:
        _write(scored)
    if done:
        c = _sqlite()
        c.executemany("DELETE FROM desk_pending WHERE rowid=?", [(r,) for r in done])
        c.commit()
        c.close()
    return len(scored)


def load_weights(asset: str | None = None) -> dict:
    """Desk weights for an asset (its own learned weights if available, else the global pool, else
    ALL-NEUTRAL). No data anywhere -> neutral -> the org behaves identically (backward-compatible)."""
    neutral = {d: NEUTRAL for d in DESKS}
    if not WEIGHTS.exists():
        return neutral
    try:
        data = json.loads(WEIGHTS.read_text())
    except (ValueError, OSError):
        return neutral
    if asset:  # per-asset: an asset's OWN learned weights, else NEUTRAL — never borrow another asset's
        return data.get("by_asset", {}).get(asset.upper(), {}).get("weights", neutral)
    return data.get("weights", neutral)  # no asset specified -> global pool (back-compat)


def weights_brief(weights: dict) -> str:
    """One-line advisory for the PM. Empty string if everything is neutral (-> no context added,
    org behaves exactly as before). Lists only desks that earned a non-neutral prior."""
    moved = {d: w for d, w in weights.items() if abs(w - NEUTRAL) > 1e-6}
    if not moved:
        return ""
    parts = [f"{d} {w:.2f}x ({'more' if w > NEUTRAL else 'less'} reliable)" for d, w in moved.items()]
    return ("; ".join(parts)
            + ". Use as a soft prior on whose read to trust — NOT a hard rule; gates still bind.")
