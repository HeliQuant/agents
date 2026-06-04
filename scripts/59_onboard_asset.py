"""HeliQuant — asset onboarding: earn an edge or abstain (scripts/59).

Runs firm.edge_lab over every asset with perp data, prints the honest validation table
(every hypothesis — pass AND fail), and GROWS validated_edges.json ONLY with edges that
clear the cost-aware OOS bar. The registry is the firm's earned knowledge: it expands only
when an asset proves a tradeable edge, never on hope. Existing entries are preserved
(the canonical MNT edge is never regressed) — onboarding only ADDS newly-earned assets.

Run:
  python scripts/59_onboard_asset.py [ASSET|all]            # dry: print table + write report
  python scripts/59_onboard_asset.py [ASSET|all] --write    # also merge NEW earned edges into registry
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.edge_lab import FEE, MIN_TRADES, onboard  # noqa: E402

DATA = ROOT / "data"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_write = "--write" in sys.argv
    arg = (args[0] if args else "all").upper()
    assets = ([arg] if arg != "ALL"
              else sorted({p.name.replace("_positioning.csv", "").upper()
                           for p in DATA.glob("*_positioning.csv")}))

    print(f"HeliQuant asset-onboarding edge lab — gate: OOS ROI>0 & >buy&hold & "
          f"avg_bps>{2*FEE*1e4:.0f} & trades>={MIN_TRADES}")
    print("(non-overlap 24h, direction+thresholds from TRAIN only, net of fees)\n")
    hdr = (f"{'asset':8}{'hypothesis':16}{'dir':11}{'OOS ROI':>9}{'b&h':>9}"
           f"{'trades':>7}{'win%':>6}{'avg_bps':>8}{'verdict':>10}")
    print(hdr)
    print("-" * len(hdr))

    report, earned, robust = {}, {}, {}
    for a in assets:
        r = onboard(a, DATA)
        report[a] = r
        if r.get("error"):
            print(f"{a:8}{r['error']}")
            continue
        for m in sorted(r["results"], key=lambda x: -x["oos_roi_pct"]):
            dirn = "contrarian" if m["contrarian"] else "momentum"
            verdict = "EARNED ✅" if m["passed"] else "—"
            print(f"{a:8}{m['edge']:16}{dirn:11}{m['oos_roi_pct']:>+8.2f}%{m['buyhold_pct']:>+8.1f}%"
                  f"{m['trades']:>7}{m['win_pct']:>6.0f}{m['avg_bps']:>+8.1f}{verdict:>10}")
        if r["earned"]:
            b = r["earned"]
            wf = b.get("wf") or {}
            robust[a] = bool(b.get("robust"))
            tag = "ROBUST ✅" if robust[a] else "FRAGILE ⚠️  (one-fold-dependent — HELD, not registered)"
            print(f"   └─ walk-forward: {wf.get('positive','?')}/{wf.get('folds','?')} folds +ve, "
                  f"mean {wf.get('mean_fold_roi','?')}%, ex-best-fold mean {wf.get('ex_best_mean','?')}%  "
                  f"folds={wf.get('fold_rois','')}  ->  {tag}")
            earned[a] = {
                "edge": b["edge"], "asset": a, "validated": True,
                "p_win": b["p_win"], "payoff_b": b["payoff_b"], "sample_n": b["trades"],
                "oos_roi_pct": b["oos_roi_pct"],
                "note": (f"{b['edge']} via edge_lab onboarding (non-overlap 24h, cost-aware OOS, "
                         f"{wf.get('positive','?')}/{wf.get('folds','?')} walk-forward folds +ve). "
                         f"avg {b['avg_bps']}bps > {b['rt_fee_bps']}bps fee; sample {b['trades']}."),
            }
        print()

    (DATA / "onboarding_report.json").write_text(json.dumps(report, indent=2))
    reg_path = DATA / "validated_edges.json"
    reg = json.loads(reg_path.read_text()) if reg_path.exists() else {}
    # ADD only NEW assets that are ROBUST (passed 1-split AND walk-forward); preserve canonical existing.
    add_robust = [a for a in earned if a not in reg and robust.get(a)]
    held = [a for a in earned if a not in reg and not robust.get(a)]

    print("=" * len(hdr))
    print(f"ASSETS scanned: {sum(1 for a in report if not report[a].get('error'))}   "
          f"passed 1-split bar: {len(earned)} ({', '.join(earned) or 'none'})   "
          f"of which ROBUST (walk-forward): {', '.join(a for a in earned if robust.get(a)) or 'none'}")
    if held:
        print(f"⚠️  HELD as candidates (great single-split, FAILED walk-forward → NOT registered): {', '.join(held)}")
    if do_write:
        for a in add_robust:
            reg[a] = earned[a]
        reg_path.write_text(json.dumps(reg, indent=2))
        print(f"registry NOW holds: {', '.join(reg.keys())}"
              + (f"   (NEW robust: {', '.join(add_robust)})" if add_robust else "   (no new robust edges — registry unchanged)"))
    else:
        print(f"[dry] would ADD (robust only): {', '.join(add_robust) or 'nothing new'}   "
              f"(registry currently: {', '.join(reg.keys())}).  Pass --write to commit.")
    print("full report (incl. failures + walk-forward) -> data/onboarding_report.json")


if __name__ == "__main__":
    main()
