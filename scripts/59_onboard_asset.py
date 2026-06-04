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
from firm.edge_lab import FEE, MIN_TRADES, onboard, sync_edges_hq  # noqa: E402

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
    # Onboarding DISCOVERS edges -> ALL new ones enter the CANDIDATE tier. Promotion to VALIDATED is
    # scripts/60's job ONLY (gated by stability + NEW-DATA confirmation) so onboarding can NEVER bypass
    # probation. validated_edges.json is left untouched here (canonical preserved).
    new_edges = [a for a in earned if a not in reg]
    robust_new = [a for a in new_edges if robust.get(a)]
    fragile_new = [a for a in new_edges if not robust.get(a)]

    print("=" * len(hdr))
    print(f"ASSETS scanned: {sum(1 for a in report if not report[a].get('error'))}   "
          f"passed 1-split bar: {len(earned)} ({', '.join(earned) or 'none'})   "
          f"of which ROBUST (walk-forward): {', '.join(a for a in earned if robust.get(a)) or 'none'}")
    if fragile_new:
        print(f"⚠️  FRAGILE (1-split only, walk-forward artifact) → candidate/paper, won't graduate until robust: {', '.join(fragile_new)}")

    cand_path = DATA / "candidate_edges.json"
    candidates = {
        a: {**earned[a], "validated": False, "tier": "candidate",
            "note": earned[a]["note"] + (" CANDIDATE (ROBUST) — awaiting scripts/60 NEW-DATA confirmation before live."
                    if robust.get(a) else " CANDIDATE — passed 1-split, not walk-forward-robust; paper-only.")}
        for a in new_edges
    }
    # preserve probation progress (confirmations/last_confirm_bar) that scripts/60 maintains.
    _prev = json.loads(cand_path.read_text()) if cand_path.exists() else {}
    for a in candidates:
        for k in ("confirmations", "last_confirm_bar"):
            if a in _prev and k in _prev[a]:
                candidates[a][k] = _prev[a][k]
    if do_write:
        reg_path.write_text(json.dumps(reg, indent=2))   # validated UNCHANGED — only scripts/60 promotes
        cand_path.write_text(json.dumps(candidates, indent=2))
        try:  # mirror edge registry -> Supabase edges_hq (guarded; no-op without creds)
            _n = sync_edges_hq()
            if _n:
                print(f"[sync] {_n} edge(s) → Supabase edges_hq (FE-readable)")
        except Exception:  # noqa: BLE001
            pass
        print(f"VALIDATED (unchanged — promotion is scripts/60's job, confirmation-gated): {', '.join(reg.keys())}")
        print(f"CANDIDATE (paper): {', '.join(candidates.keys()) or 'none'}"
              + (f"  · robust → pending scripts/60 confirmation: {', '.join(robust_new)}" if robust_new else ""))
    else:
        print(f"[dry] new candidates: {', '.join(new_edges) or 'none'}"
              + (f"  (robust, will graduate via scripts/60 confirmation: {', '.join(robust_new)})" if robust_new else "")
              + ".  Pass --write to commit.")
    print("full report (incl. failures + walk-forward) -> data/onboarding_report.json")


if __name__ == "__main__":
    main()
