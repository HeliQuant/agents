"""HeliQuant — desk self-learning: the org learns which of its 7 desks to TRUST (an ADDITIVE skill).

Seeds the OI-Contrarian desk's reliability from real MNT history (the one desk whose stance we can
replay), computes BOUNDED desk weights from the track-record ledger, and writes desk_weights.json —
an ADVISORY prior the PM reads. The other desks start neutral and earn their weight forward, as live
runs resolve (firm.desk_performance.log_outcome). Gates are NEVER touched; if the weights file is
absent the org behaves identically. "Smarter with use", honestly + reversibly.

Run: python scripts/62_desk_learning.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import desk_performance as dp  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print("HeliQuant — desk self-learning (ADDITIVE: own files, bounded [0.6,1.4], gates untouched)\n")
    n = dp.seed_oi_from_replay("MNT")
    print(f"seeded {n} OI-Contrarian samples from real MNT replay (other desks accumulate forward via live runs)\n")
    detail = dp.compute_weights()

    hdr = f"{'desk':20}{'weight':>8}{'samples':>9}{'align_rate':>12}{'status':>14}"
    print(hdr)
    print("-" * len(hdr))
    for desk, d in detail.items():
        ar = f"{d['align_rate']:.1%}" if d["align_rate"] is not None else "—"
        status = "learned" if d["samples"] >= dp.MIN_SAMPLES else f"neutral (<{dp.MIN_SAMPLES})"
        print(f"{desk:20}{d['weight']:>7.2f}x{d['samples']:>9}{ar:>12}{status:>14}")
    print("-" * len(hdr))

    brief = dp.weights_brief(dp.load_weights())
    print(f"\nPM advisory line: {brief or '(all neutral — PM gets NO extra context, org identical)'}")
    print("\nHonest: only desks with >=15 resolved samples move off neutral. Today that's the replayable")
    print("OI-Contrarian desk; the LLM/API desks earn their weight as live decisions resolve over time.")
    print("This is a SOFT PRIOR for the PM — the R:R/validation/edge gates are unchanged.")


if __name__ == "__main__":
    main()
