"""HeliQuant — self-learning loop: edges EARN their way up (and can be demoted) on evidence.

Run this every cycle (as data refreshes). It re-validates every edge on the LATEST data and moves
edges between two tiers — NEVER by tweaking the gate, only by accumulating evidence:

  VALIDATED (live-eligible, AGGRESSIVE-capable)  <—graduate—  CANDIDATE (paper-only, learning)
        │                                                          ▲
        └── decays below the robust bar ──> demote ───────────────┘

  * GRADUATE: a candidate that now clears the FULL bar (1-split + walk-forward + outlier-robust).
  * DEMOTE:   a validated edge that no longer clears it (markets change — honesty cuts both ways).
  * HOLD:     candidate still not robust -> keep paper-trading; log a forward observation so the
              evidence pile grows (resolved when data extends past the entry bar).

This is how HeliQuant "gets better the more it trades": not by curve-fitting until a backtest
dazzles, but by letting real out-of-sample evidence promote (or retire) edges over time.

Run:  python scripts/60_self_learn.py            # dry: show the cycle's verdicts
      python scripts/60_self_learn.py --apply    # actually graduate/demote between registries
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.edge_lab import is_stable_robust, live_signal, onboard  # noqa: E402

DATA = ROOT / "data"
VAL = DATA / "validated_edges.json"
CAND = DATA / "candidate_edges.json"
LEDGER = DATA / "paper_ledger.jsonl"


CONFIRM_NEEDED = 2  # a JUST-CROSSED edge must confirm on this many NEW-DATA cycles before live graduation


def _load(p):
    return json.loads(p.read_text()) if p.exists() else {}


def _latest_bar(asset: str) -> str:
    """Latest data bar's datetime (lightweight CSV-tail read) — the 'new evidence' clock for confirmation."""
    try:
        return (DATA / f"{asset.lower()}_positioning.csv").read_text().strip().splitlines()[-1].split(",")[1]
    except Exception:  # noqa: BLE001
        return ""


def _revalidate(asset):
    """Re-run the full gate on current data. Returns (robust, edge_name, oos_roi, wf)."""
    r = onboard(asset, DATA)
    b = r.get("earned")
    if not b:
        return False, None, None, None
    return bool(b.get("robust")), b["edge"], b["oos_roi_pct"], (b.get("wf") or {})


def _paper_tick(asset, edge):
    """Log a forward paper observation for a candidate's live signal (evidence accumulator).
    Uses the data's last bar (reproducible), not wall-clock, for the entry mark."""
    sig = live_signal(asset, edge, DATA)
    obs = {"asset": asset, "edge": edge, "signal": sig.get("signal"),
           "actionable": sig.get("actionable", False), "logged_utc": datetime.now(timezone.utc).isoformat()}
    if sig.get("actionable"):
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obs) + "\n")
    return sig


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    apply = "--apply" in sys.argv
    val, cand = _load(VAL), _load(CAND)
    promote, demote = {}, []

    print("HeliQuant self-learning cycle — edges move on EVIDENCE, never by tweaking the gate.\n")
    print("── VALIDATED (live-eligible) — re-checked on latest data ──")
    for a, e in sorted(val.items()):
        robust, edge, roi, wf = _revalidate(a)
        sig = live_signal(a, e.get("edge", edge or ""), DATA)
        live = sig.get("signal") or "wait"
        if robust:
            print(f"  ✅ {a:5} {e.get('edge'):14} stays VALIDATED  (re-val {roi:+.1f}% OOS, "
                  f"WF {wf.get('positive')}/{wf.get('folds')})  live signal: {live}")
        else:
            demote.append(a)
            print(f"  ⚠️ {a:5} {e.get('edge'):14} DECAYED -> demote to candidate  (no longer robust)")

    print("\n── CANDIDATE (paper-only, accumulating evidence) ──")
    for a, e in sorted(cand.items()):
        robust, edge, roi, wf = _revalidate(a)
        sig = _paper_tick(a, e.get("edge", edge or ""))
        live = sig.get("signal") or "wait (no extreme)"
        if robust:
            stab = is_stable_robust(a, e.get("edge", edge or ""), DATA)  # cross-snapshot STABILITY gate
            if stab.get("stable"):
                cur_bar = _latest_bar(a)
                advanced = bool(cur_bar) and cur_bar != e.get("last_confirm_bar", "")  # NEW-DATA passes only
                conf = int(e.get("confirmations", 0)) + (1 if advanced else 0)
                if conf >= CONFIRM_NEEDED:
                    promote[a] = {k: v for k, v in e.items() if k not in ("tier", "confirmations", "last_confirm_bar")}
                    promote[a].update({"validated": True})
                    print(f"  🎓 {a:5} {e.get('edge'):14} GRADUATED -> VALIDATED!  robust {roi:+.1f}%, stable, "
                          f"AND confirmed {conf}x over new data → earned live size.")
                else:
                    e["confirmations"], e["last_confirm_bar"] = conf, cur_bar  # persisted on --apply
                    print(f"  🕐 {a:5} {e.get('edge'):14} PROBATION {conf}/{CONFIRM_NEEDED}: passes gate+stable NOW "
                          f"({roi:+.1f}%) but JUST crossed — must hold {CONFIRM_NEEDED-conf} more NEW-DATA cycle(s) "
                          f"before live capital (regime-dependent; confirm before we trust it).")
            else:
                print(f"  ⏸️ {a:5} {e.get('edge'):14} robust NOW ({roi:+.1f}%) but UNSTABLE across snapshots "
                      f"(robust_at={stab.get('robust_at')}) -> HELD (anti-flicker).")
        else:
            xb = wf.get("ex_best_mean") if wf else "?"
            print(f"  📝 {a:5} {e.get('edge'):14} still candidate (paper)  re-val 1-split {roi if roi is not None else '—'}% "
                  f"but ex-best-fold {xb}% < bar.  live signal: {live}  -> evidence logged")

    print("\n" + "─" * 60)
    print(f"GRADUATE: {', '.join(promote) or 'none'}   |   DEMOTE: {', '.join(demote) or 'none'}")
    if apply:
        for a in promote:
            val[a] = promote[a]
            cand.pop(a, None)
        for a in demote:
            cand[a] = {**val.pop(a), "validated": False, "tier": "candidate"}
        VAL.write_text(json.dumps(val, indent=2))
        CAND.write_text(json.dumps(cand, indent=2))
        print(f"APPLIED. validated now: {', '.join(val) or 'none'}  |  candidates: {', '.join(cand) or 'none'}")
    else:
        print("[dry] no files changed. Pass --apply to graduate/demote. (Paper observations are logged either way.)")
    print("the loop: re-run as data grows — earned edges rise, decayed edges retire. No gate-tweaking.")


if __name__ == "__main__":
    main()
