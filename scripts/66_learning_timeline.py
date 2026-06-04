"""Does the self-learning loop actually WORK over time? — replay the LEARNING, not just the trading.

The honest test of "HeliQuant gets better as evidence grows": feed the data incrementally (expanding
window — a proxy for time passing / more market history), and at each cutoff re-run the FULL gate
(1-split cost-aware OOS + walk-forward + drop-best-fold robustness). Watch each edge's verdict EVOLVE.

This separates real self-learning from the gambler's fallacy:
  * Real: a verdict CHANGES only when new EVIDENCE changes — graduate iff it becomes robust.
  * Fallacy: "run it more on the same data and it gets better" — it does NOT (deterministic).

Honest expectation: MNT's OI edge should stay robust-ish; HYPE's order-flow edge should keep failing
robustness (one-fold-dependent) — i.e. the loop correctly HOLDS it, and would only graduate it if the
real market ever made the edge genuinely robust. Run: python scripts/66_learning_timeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
from firm.edge_lab import SIGNAL_SOURCES, _EDGE_SRC, validate_signal, walk_forward  # noqa: E402

BOOK = [("MNT", "oi_contrarian"), ("HYPE", "flow_contrarian")]
CUTOFFS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def evaluate_at(asset: str, edge: str, frac: float) -> dict:
    src, _ = _EDGE_SRC[edge]
    df = pd.read_csv(DATA / f"{asset.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    cut = int(len(df) * frac)
    df = df.iloc[:cut].reset_index(drop=True)
    c = df["close"].values
    sig = SIGNAL_SOURCES[src](df).values
    m = validate_signal(c, sig)
    if m is None:
        return {"status": "insufficient", "roi": None, "wf": {}}
    wf = walk_forward(c, sig) or {}
    robust = bool(wf.get("consistent"))
    status = "VALIDATED ✅" if (m["passed"] and robust) else "candidate" if m["passed"] else "fail"
    return {"status": status, "roi": m["oos_roi_pct"], "passed": m["passed"],
            "wf": wf, "robust": robust, "n": m["trades"], "bars": cut}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("SELF-LEARNING TIMELINE — verdict evolution as evidence (data) grows\n")
    for asset, edge in BOOK:
        print(f"════ {asset} · {edge} ════")
        hdr = f"  {'data used':>10} {'bars':>6} {'1-split OOS':>12} {'trades':>7} {'walk-fwd':>10} {'ex-best':>8} {'VERDICT':>12}"
        print(hdr)
        graduated_at = None
        final_status = "insufficient"
        for frac in CUTOFFS:
            r = evaluate_at(asset, edge, frac)
            if r["status"] == "insufficient":
                print(f"  {int(frac*100):>9}% {'':>6} insufficient data")
                continue
            wf = r["wf"]
            wfs = f"{wf.get('positive')}/{wf.get('folds')}"
            roi = f"{r['roi']:+.1f}%"
            print(f"  {int(frac*100):>9}% {r['bars']:>6} {roi:>12} {r['n']:>7} {wfs:>10} "
                  f"{str(wf.get('ex_best_mean'))+'%':>8} {r['status']:>12}")
            is_val = r["status"].startswith("VALIDATED")  # full bar: passed 1-split AND robust
            if is_val and graduated_at is None:
                graduated_at = frac
            final_status = r["status"]
        if final_status.startswith("VALIDATED"):
            verdict = (f"VALIDATED — earned at {int(graduated_at*100)}% data and HELD through latest "
                       "→ a STABLE edge the loop promoted on evidence.")
        elif graduated_at:
            verdict = (f"briefly VALIDATED at {int(graduated_at*100)}% data but FELL BACK to candidate by 100% "
                       "→ UNSTABLE; loop correctly HOLDS it now (we don't trust transient graduations).")
        else:
            verdict = "never cleared robust+sample across the timeline → correctly HELD (no overfit pass)."
        print(f"  => {verdict}\n")

    print("HONEST READ:")
    print("  • The loop re-evaluates on growing evidence and changes a verdict ONLY when robustness changes.")
    print("  • Running it MORE on the SAME data does nothing (deterministic) — that's the anti-overfit point.")
    print("  • HYPE 'improves' only if real future market data makes its edge genuinely robust — then it")
    print("    graduates AUTOMATICALLY (and anchors on-chain). We don't fake that; we wait for the evidence.")


if __name__ == "__main__":
    main()
