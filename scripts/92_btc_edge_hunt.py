"""
scripts/92_btc_edge_hunt.py — the HONEST "make BTC tradeable" attempt.

BTC directional trading has failed every prior test (Findings #24/#25, the cost-aware OOS gate,
the carry study). Before accepting "BTC = no edge" we widen the self-learning hunt: every signal
source × MULTIPLE horizons (not just 24h), under the SAME rigorous gate (cost-aware OOS + walk-
forward + FDR). A wider hunt means MORE tests, so FDR is STRICTER (it must be — crowning the best
horizon is the multiple-testing trap). If anything clears it, BTC earns a real edge and can trade.
If nothing does, that's the honest answer — reported, not papered over.

MNT is included as a positive control: the hunt MUST still find MNT's known OI edge, else the
harness is broken and a BTC null would be meaningless.
"""
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import edge_lab as el  # noqa: E402

HORIZONS = [4, 6, 8, 12, 24, 48]
DATA = ROOT / "data"
_orig_H = el.H


def hunt(asset: str) -> None:
    fp = DATA / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        print(f"\n{asset}: no positioning data")
        return
    df = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    close = df["close"].values
    rows = []
    for h in HORIZONS:
        el.H = h  # the signal lambdas + the OOS hold-window both read el.H at call time
        for src, fn in el.SIGNAL_SOURCES.items():
            if not el._usable(df, src):
                continue
            m = el.validate_signal(close, fn(df).values)
            if m:
                rows.append({"h": h, "src": src, "edge": el.edge_name(src, m["contrarian"]),
                             "oos": m["oos_roi_pct"], "pval": m["pval"], "passed": m["passed"]})
    # FDR across EVERY (horizon × source) test — wider hunt, stricter correction (anti cherry-pick)
    fdr = el.benjamini_hochberg({f"{r['h']}:{r['src']}": r["pval"] for r in rows}, alpha=0.10)
    for r in rows:
        r["fdr"] = bool(fdr.get(f"{r['h']}:{r['src']}", False))

    print(f"\n================  {asset}  ·  {len(rows)} tests (×{len(HORIZONS)} horizons)  ================")
    print(f"  {'H':>3} {'edge':<16}{'OOS%':>9}{'pval':>8}{'OOSok':>6}{'FDRok':>6}")
    for r in sorted(rows, key=lambda x: -x["oos"]):
        flag = "  <<" if (r["passed"] and r["fdr"]) else ""
        print(f"  {r['h']:>3} {r['edge']:<16}{r['oos']:>9.1f}{r['pval']:>8.3f}"
              f"{('Y' if r['passed'] else '-'):>6}{('Y' if r['fdr'] else '-'):>6}{flag}")

    winners = [r for r in rows if r["passed"] and r["fdr"]]
    if not winners:
        print(f"  VERDICT: no {asset} edge clears the gate at any horizon — abstain (honest).")
        return
    print(f"  {len(winners)} candidate(s) clear OOS+FDR — checking walk-forward stability:")
    for r in winners:
        el.H = r["h"]
        wf = el.walk_forward(close, el.SIGNAL_SOURCES[r["src"]](df).values)
        ok = bool(wf and wf.get("consistent"))
        print(f"    H{r['h']} {r['edge']}: OOS {r['oos']:+.1f}% · walk-forward {'ROBUST' if ok else 'NOT robust'}"
              f"{' — EARNS a slot' if ok else ' — hold'}")


if __name__ == "__main__":
    for a in (sys.argv[1:] or ["BTC", "MNT"]):
        hunt(a)
    el.H = _orig_H
    print("\n(gate = cost-aware OOS + Benjamini-Hochberg FDR + walk-forward; same standard as the registry)")
