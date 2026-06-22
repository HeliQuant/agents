"""Validate HYPE OI-contrarian with the IDENTICAL bar as MNT — zero reimplementation.

We import and call the EXACT functions from the existing validation scripts so HYPE is judged on
the same methodology that gave MNT its registered edge (no weakened gate, no peeking):
  * scripts/38_positioning_ic.build/ic_split/quantile  -> predictive power + half-split stability
  * scripts/39_oi_backtest.backtest                    -> cost-aware single-split OOS + the tradeable gate
  * scripts/41_oi_walkforward.walkforward              -> rolling-fold robustness

This script ONLY reports + (optionally) registers HYPE into data/validated_edges.json using the SAME
gate as script 39's main() (contrarian, OOS>0, OOS>buy&hold, avg_bps>round-trip-fee, payoff>0, n>=10).
It NEVER removes or alters MNT's entry — it reads the existing file and ADDS HYPE only if it passes.

Run: python scripts/55_validate_hype.py            # report only
     python scripts/55_validate_hype.py --register # also write HYPE into validated_edges.json if it passes
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s38 = _load("s38", "38_positioning_ic.py")
s39 = _load("s39", "39_oi_backtest.py")
s41 = _load("s41", "41_oi_walkforward.py")

TICKER = "HYPE"
FEE = s39.FEE  # 0.00055 taker/side — identical constant


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    register = "--register" in sys.argv

    print(f"===== HYPE validation (identical bar to MNT; fee {FEE*100:.3f}%/side) =====\n")

    # ---- 1. Predictive power + stability (script 38 functions) ----
    sig, fwd = s38.build(TICKER)
    print("[IC] signal predictive power (Spearman IC; half-split stability)")
    print(f"  {'signal':16} {'IC@4h':>8} {'IC@8h':>8} {'IC@24h':>8}   {'half@24h(1st/2nd)':>20}  flag")
    for name, sv in sig.items():
        ics = {h: s38.ic_split(sv, fwd[h])[0] for h in s38.HORIZONS}
        full24, i1, i2 = s38.ic_split(sv, fwd[24])
        stable = (i1 is not None and i2 is not None and (i1 > 0) == (i2 > 0) and min(abs(i1), abs(i2)) >= 0.02)
        strong = stable and full24 is not None and abs(full24) >= 0.04
        flag = "** STRONG" if strong else ("* stable" if stable else "")

        def fmt(x):
            return f"{x:+.3f}" if x is not None else "   n/a"
        half = f"{fmt(i1)}/{fmt(i2)}" if i1 is not None else "n/a"
        print(f"  {name:16} {fmt(ics[4]):>8} {fmt(ics[8]):>8} {fmt(ics[24]):>8}   {half:>20}  {flag}")

    # ---- 2. Cost-aware single-split OOS backtest (script 39 — the registry bar) ----
    r = s39.backtest(TICKER)
    print("\n[BACKTEST] cost-aware OI-contrarian, non-overlap 24h, train-derived dir+thresholds")
    if not r:
        print("  insufficient data — abort")
        return
    print(f"  train_IC {r['train_ic']:+.3f} | dir {r['dir']} | OOS net ROI {r['oos_roi']:+.2f}% | "
          f"trades {r['trades']} | win {r['win']:.1f}% | payoff {r['payoff_b']:.2f}")
    print(f"  avg_bps {r['avg_bps']:+.1f} (round-trip fee {2*FEE*1e4:.1f} bps) | buy&hold {r['bh']:+.2f}%")

    # ---- 3. Rolling-fold robustness (script 41) ----
    print("\n[WALK-FORWARD] expanding folds (robustness; a single window can fool us)")
    pos_roi, pos_spread = s41.walkforward(TICKER)

    # ---- 4. Apply the EXACT script-39 gate ----
    rt_fee_bps = 2 * FEE * 1e4
    passes = (r["dir"] == "contrarian" and r["oos_roi"] > 0 and r["oos_roi"] > r["bh"]
              and r["avg_bps"] > rt_fee_bps and r["payoff_b"] > 0 and r["trades"] >= 10)
    print("\n===== GATE (identical to script 39 main) =====")
    print(f"  contrarian={r['dir']=='contrarian'} | OOS>0={r['oos_roi']>0} | OOS>b&h={r['oos_roi']>r['bh']} | "
          f"avg_bps>{rt_fee_bps:.0f}={r['avg_bps']>rt_fee_bps} | payoff>0={r['payoff_b']>0} | n>=10={r['trades']>=10}")
    print(f"  -> {'PASSES — tradeable edge' if passes else 'FAILS — NOT a validated edge (abstain)'}")
    print(f"  (robustness context: {pos_roi}/4 folds +ROI, {pos_spread}/4 folds +drift-neutral spread)")

    if register and passes:
        edges_path = ROOT / "data" / "validated_edges.json"
        edges = json.loads(edges_path.read_text()) if edges_path.exists() else {}
        edges[TICKER] = {
            "edge": "oi_contrarian", "asset": TICKER, "validated": True,
            "p_win": round(r["p_win"], 4), "payoff_b": r["payoff_b"], "sample_n": r["trades"],
            "oos_roi_pct": round(r["oos_roi"], 2),
            "note": ("OI-contrarian, OOS (non-overlap 24h, cost-aware, exchange HYPEUSDT perp). "
                     f"Rolling robustness {pos_roi}/4 folds +ROI -> caveated; sizing gates on sample_n>=20."),
        }
        edges_path.write_text(json.dumps(edges, indent=2))
        print(f"\nREGISTERED HYPE -> {edges_path.name} (MNT entry preserved; total {len(edges)} edges)")
    elif register and not passes:
        print("\nNOT registered — HYPE did not clear the honest gate. Registry unchanged (MNT only).")


if __name__ == "__main__":
    main()
