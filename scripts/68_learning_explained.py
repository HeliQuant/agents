"""SHOW the self-learning mechanism — the actual UPDATE RULES, traced on real data (not hand-waving).

HeliQuant has THREE self-learning loops. This makes each one's GEARS visible:
  A) EDGE learning  — re-validate on new data; tier transitions candidate↔validated↔demoted, gated by
                      walk-forward robustness + cross-time stability + NEW-DATA confirmation.
  B) DESK learning  — each desk's weight = f(its stance-vs-outcome track record), bounded, evidence-driven.
  C) MEMORY recall  — resolved trades fed back to the PM (factual recency+regime, not invented lessons).

Run: python scripts/68_learning_explained.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
from firm.desk_performance import HI, LO, MIN_SAMPLES, NEUTRAL  # the real bounds/threshold  # noqa: E402


def _weight(rate: float) -> float:
    return round(min(HI, max(LO, 2.0 * rate)), 2)


def part_A_edge():
    print("═" * 76)
    print("A) EDGE LEARNING — the rule that moves an edge between tiers (what you saw with HYPE)")
    print("═" * 76)
    print("""  RULE (per re-validation on the latest data):
    1. cost-aware OOS pass?  (ROI>0 & >buy&hold & avg_bps>fee & trades>=20)   else-> fail/hold
    2. walk-forward robust?  (5 folds, majority +ve AND positive after dropping the single best fold)
    3. cross-time STABLE?    (robust across data cutoffs, not one lucky snapshot)
    4. NEW-DATA confirmed?   (held robust over >=2 cycles of genuinely new bars; re-runs don't count)
       pass all 4 -> GRADUATE candidate->validated (+ anchor on-chain).  decay -> DEMOTE validated->candidate.""")
    val = json.loads((DATA / "validated_edges.json").read_text()) if (DATA / "validated_edges.json").exists() else {}
    cand = json.loads((DATA / "candidate_edges.json").read_text()) if (DATA / "candidate_edges.json").exists() else {}
    print("\n  CURRENT STATE (this is the mechanism's live output):")
    for a, e in val.items():
        print(f"    🟢 {a:5} {e.get('edge'):14} VALIDATED — passed all 4 gates, live-eligible")
    for a, e in cand.items():
        print(f"    🕐 {a:5} {e.get('edge'):14} CANDIDATE — confirmations {e.get('confirmations',0)}/2 "
              f"(crossed gates 1-3 on fresh data; awaiting NEW-DATA confirmation #2 before live)")
    print("\n  ^ When you fetched fresh HYPE data, gate 1-3 flipped pass -> that WAS the gear turning.")
    print("    Gate 4 (confirmation) is what holds it: it must re-pass on the NEXT new bars too.\n")


def part_B_desk():
    print("═" * 76)
    print("B) DESK LEARNING — watch a desk's WEIGHT actually move as evidence accumulates")
    print("═" * 76)
    print(f"  RULE: weight = clamp(2 × align_rate, {LO}, {HI});  neutral 1.00 until >= {MIN_SAMPLES} samples.")
    print("        align_rate = (# times the desk's directional stance matched the realized move) / total\n")
    print("  The rule's response to evidence (align_rate -> weight):")
    for r in (0.30, 0.40, 0.50, 0.56, 0.65, 0.75):
        arrow = "more trusted" if _weight(r) > 1 else "less trusted" if _weight(r) < 1 else "neutral"
        print(f"     align {int(r*100)}%  ->  weight {_weight(r):.2f}x   ({arrow})")

    # REAL learning curve: replay the OI-Contrarian desk's outcomes, show weight CONVERGE as samples grow
    df = pd.read_csv(DATA / "mnt_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    oichg = df["oi"].pct_change(24).values
    n = len(df)
    idx = [i for i in range(24, n - 24, 24) if not np.isnan(oichg[i])]
    sig = np.array([oichg[i] for i in idx])
    p20, p80 = np.nanpercentile(sig, 20), np.nanpercentile(sig, 80)
    outcomes = []
    for i in idx:
        s = oichg[i]
        stance = "L" if s <= p20 else "S" if s >= p80 else None
        if stance is None:
            continue
        outcomes.append(stance == ("L" if c[i + 24] >= c[i] else "S"))
    print(f"\n  REAL learning curve — OI-Contrarian desk weight as its {len(outcomes)} outcomes accumulate:")
    for k in (5, 15, 30, 60, 100, len(outcomes)):
        pref = outcomes[:k]
        rate = sum(pref) / len(pref)
        w = _weight(rate) if len(pref) >= MIN_SAMPLES else NEUTRAL
        tag = "neutral (too few samples — won't trust yet)" if len(pref) < MIN_SAMPLES else f"LEARNED {w:.2f}x"
        print(f"     after {k:>3} outcomes:  align {rate*100:4.0f}%  ->  weight {w:.2f}x   {tag}")
    print("  ^ THAT is desk-learning: the weight is held at neutral until enough evidence, then it")
    print("    converges to what the track record earns — bounded so no desk is ever silenced.\n")


def part_C_memory():
    print("═" * 76)
    print("C) MEMORY RECALL — resolved trades fed back to the PM (the 'smarter with use' loop)")
    print("═" * 76)
    print("""  RULE: every PM decision is logged (decisions_hq); when a trade resolves (TP/SL/timeout) the
        outcome is recorded; on the NEXT decision the recent same-regime resolved trades are recalled
        into the PM's context — factual recency+regime recall (no invented 'lessons'). Forward-accumulating.

  THE THREE LOOPS TOGETHER = self-learning that is evidence-driven, bounded, and anti-overfit:
    • edges earn/lose their tier on NEW data (not on re-runs)   • desks earn/lose trust on track record
    • the PM remembers what actually happened   • nothing graduates on a single lucky snapshot""")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    part_A_edge()
    part_B_desk()
    part_C_memory()


if __name__ == "__main__":
    main()
