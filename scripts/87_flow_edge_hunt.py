"""scripts/87 — FLOW-EDGE HUNT on 'dead' assets: route prediction-failures (BTC) to a RICH flow-signal
search. The thesis (user's): if you can't predict price, maybe the edge lives in the FLOW/positioning data
itself. So expand far beyond the basic 4 signals — multi-horizon OI, funding momentum/extreme, taker-flow,
OI-price divergence, crowding combos — and test EACH under the same brutal gate edge_evolver uses:
cross-window robust (70/85/100% cutoffs all +) AND ex-best-trade (outlier guard). Honest: most will fail
(these assets are efficient); the point is to find one that DOESN'T, if it exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.edge_evolver import evaluate_variant  # noqa: E402  (cross-window + ex-best gate)


def rich_signals(df: pd.DataFrame) -> dict:
    """Derive a rich flow-signal suite from {close, funding, oi, buy_ratio}."""
    close, oi, fund, buy = df["close"], df["oi"], df["funding"], df["buy_ratio"]
    pchg = close.pct_change
    oichg = oi.pct_change
    s = {
        "oi_chg6": oichg(6), "oi_chg12": oichg(12), "oi_chg48": oichg(48), "oi_chg72": oichg(72),
        "oi_accel": oichg(24) - oichg(24).shift(24),                       # OI accelerating
        "funding_lvl": fund, "funding_mom24": fund - fund.shift(24),       # funding level / momentum
        "funding_z": (fund - fund.rolling(168).mean()) / (fund.rolling(168).std() + 1e-12),
        "taker_flow": buy - 0.5, "taker_mom24": buy - buy.shift(24),       # taker buy/sell imbalance / momentum
        "oi_price_div": oichg(24) - pchg(24),                              # OI rising vs price (accumulation/squeeze)
        "fund_oi_crowd": fund * np.sign(oichg(24)),                        # crowded positioning (high funding + OI build)
        "oi_x_taker": oichg(24) * (buy - 0.5),                            # OI build aligned with taker side
    }
    return {k: v.values for k, v in s.items()}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    assets = [a.upper() for a in sys.argv[1:]] or ["BTC"]
    for asset in assets:
        fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
        if not fp.exists():
            print(f"\n{asset}: no data"); continue
        df = pd.read_csv(fp)
        close = df["close"].values
        sigs = rich_signals(df)
        print(f"\n{'='*64}\n  FLOW-EDGE HUNT — {asset}  ({len(sigs)} signals × 2 regimes, gate: cross-window + ex-best)\n{'='*64}")
        survivors = []
        for name, sig in sigs.items():
            for regime in ("none", "trend_aligned"):
                for H in (24, 48):
                    try:
                        v = evaluate_variant(close, sig, H, regime)
                    except Exception:  # noqa: BLE001
                        v = None
                    if v and v["robust"] and v["oos_roi_pct"] > 0:
                        survivors.append({**v, "signal": name})
        survivors.sort(key=lambda x: x["ex_best_roi_pct"], reverse=True)
        if survivors:
            print(f"  {len(survivors)} ROBUST survivor(s) (out of {len(sigs)*4} tested):")
            for v in survivors[:8]:
                print(f"   ✅ {v['signal']:14} H{v['H']} '{v['regime']:13}' OOS {v['oos_roi_pct']:+.1f}% "
                      f"ex-best {v['ex_best_roi_pct']:+.1f}% cutoffs {v['cutoff_rois']} n={v['trades']}")
            print(f"  ⚠️ CANDIDATES only — best-of-{len(sigs)*4} search = multiple-testing; forward-confirm before trading.")
        else:
            print(f"  ❌ ZERO robust flow edge across {len(sigs)*4} variants — no flow edge here either. Honest abstain.")


if __name__ == "__main__":
    main()
