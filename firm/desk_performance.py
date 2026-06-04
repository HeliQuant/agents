"""firm/desk_performance.py — ADDITIVE self-learning: the org learns which of its 7 desks have been
RELIABLE and surfaces that as an ADVISORY prior to the PM. A NEW skill, layered on — it does NOT
change how the org works today.

NON-DESTRUCTIVE BY DESIGN (the hard constraint):
  * Own files only: data/desk_outcomes.jsonl (ledger) + data/desk_weights.json (learned state).
  * No data -> load_weights() returns ALL-NEUTRAL (1.0) -> the org behaves EXACTLY as before.
  * Weights are BOUNDED to [0.6, 1.4] -> no desk is ever muted; every desk keeps its voice.
  * It NEVER touches the R:R gate, the validation gate, or the AGGRESSIVE-edge rules. It only adds
    one advisory line to the PM's context. The PM still judges; the deterministic gates still enforce.

A desk earns its weight by TRACK RECORD: did its directional stance align with the realized move?
Accumulated forward from live runs (log_outcome) + seeded from the one desk we can replay on real
history (OI-Contrarian). "Smarter with use" — honestly, bounded, reversible.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "desk_outcomes.jsonl"
WEIGHTS = ROOT / "data" / "desk_weights.json"

DESKS = ["Regime/Technical", "Macro (Allora)", "On-chain/Risk", "Research",
         "Smart-Money Flow", "Smart-Social", "OI-Contrarian"]
LO, HI = 0.6, 1.4      # weight bounds — never mute a desk
NEUTRAL = 1.0
MIN_SAMPLES = 15       # need >= 15 resolved samples before a desk's weight moves off neutral


def _stance_dir(stance: str) -> str | None:
    s = str(stance).lower()
    if any(k in s for k in ("bull", "long", "accumul")):
        return "LONG"
    if any(k in s for k in ("bear", "short", "distrib")):
        return "SHORT"
    return None  # neutral/avoid/unavailable -> no directional call -> not scored


def _append(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def log_outcome(ticker: str, analysts: dict, realized_direction: str, *, source: str = "live") -> int:
    """Forward hook: when a decision RESOLVES, score each desk's stance vs the realized direction.
    Call from the live loop once an outcome is known. Returns #rows logged."""
    rd = str(realized_direction).upper()
    if rd not in ("LONG", "SHORT"):
        return 0
    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for desk in DESKS:
        sd = _stance_dir((analysts.get(desk) or {}).get("stance", ""))
        if sd is None:
            continue
        rows.append({"ts": ts, "ticker": ticker.upper(), "desk": desk,
                     "stance_dir": sd, "realized_dir": rd, "aligned": sd == rd, "source": source})
    _append(rows)
    return len(rows)


def seed_oi_from_replay(ticker: str = "MNT") -> int:
    """Seed the ledger with the OI-Contrarian desk's REAL historical directional alignment (the one
    desk whose stance we can reconstruct on history). Idempotent: clears prior replay rows first."""
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
        sd = "LONG" if s <= p20 else "SHORT" if s >= p80 else None  # contrarian: fade the extreme
        if sd is None:
            continue
        rd = "LONG" if c[i + 24] >= c[i] else "SHORT"
        rows.append({"ts": f"replay:{ticker.upper()}:{i}", "ticker": ticker.upper(), "desk": "OI-Contrarian",
                     "stance_dir": sd, "realized_dir": rd, "aligned": sd == rd, "source": "replay:oi"})
    # idempotent: drop old replay rows, keep live ones, then add fresh
    keep = []
    if LEDGER.exists():
        keep = [ln for ln in LEDGER.read_text(encoding="utf-8").splitlines()
                if ln.strip() and json.loads(ln).get("source") != "replay:oi"]
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text("\n".join(keep + [json.dumps(r) for r in rows]) + "\n", encoding="utf-8")
    return len(rows)


def compute_weights() -> dict:
    """Per-desk reliability from the ledger -> bounded weight. >= MIN_SAMPLES needed to move off
    neutral. weight = clamp(2 * alignment_rate, 0.6, 1.4). Writes desk_weights.json. Returns detail."""
    tally: dict[str, list[int]] = {d: [0, 0] for d in DESKS}  # [aligned, total]
    if LEDGER.exists():
        for ln in LEDGER.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            d = r.get("desk")
            if d in tally:
                tally[d][1] += 1
                tally[d][0] += int(bool(r.get("aligned")))
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
    WEIGHTS.write_text(json.dumps({"weights": weights, "detail": detail,
                                   "bounds": [LO, HI], "min_samples": MIN_SAMPLES}, indent=2))
    return detail


def load_weights() -> dict:
    """Current desk weights — ALL-NEUTRAL if none learned yet (org then behaves identically)."""
    if not WEIGHTS.exists():
        return {d: NEUTRAL for d in DESKS}
    try:
        return json.loads(WEIGHTS.read_text()).get("weights", {d: NEUTRAL for d in DESKS})
    except (ValueError, OSError):
        return {d: NEUTRAL for d in DESKS}


def weights_brief(weights: dict) -> str:
    """One-line advisory for the PM. Empty string if everything is neutral (-> no context added,
    org behaves exactly as before). Lists only desks that have earned a non-neutral prior."""
    moved = {d: w for d, w in weights.items() if abs(w - NEUTRAL) > 1e-6}
    if not moved:
        return ""
    parts = [f"{d} {w:.2f}x ({'more' if w > NEUTRAL else 'less'} reliable)" for d, w in moved.items()]
    return ("; ".join(parts)
            + ". Use as a soft prior on whose read to trust — NOT a hard rule; gates still bind.")
