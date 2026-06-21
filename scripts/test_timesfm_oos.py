"""scripts/test_timesfm_oos.py — does the TimesFM forecast add cost-aware edge on the RECENT window?

Zero-shot TimesFM is inherently out-of-sample (never fit on our series). We walk the LAST `--win` bars,
make a forecast from PAST-ONLY closes at each non-overlapping step (stride = horizon → clean compounding),
and score the forecast's DIRECTION against what actually happened, net of round-trip cost.

Honest by construction: no lookahead (inputs = closes[:d+1]); the abstain "typical band" yardstick is
computed from history BEFORE the window. Small sample — report as-seen, no fabrication. Run:
    python scripts/test_timesfm_oos.py MNT BTC ETH --win 3000 --horizon 24
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from firm.timesfm_desk import _CTX, _closes, _typical_band_pct, forecast_batch  # noqa: E402

COST = 0.001 * 2  # round-trip fee+slippage (matches edge_lab: 0.00055 fee + 0.00045 slip per side)


def run(asset: str, win: int, horizon: int) -> None:
    closes = _closes(asset)
    n = len(closes)
    start = max(_CTX, n - win)
    ds = list(range(start, n - horizon, horizon))  # non-overlapping decision indices
    if len(ds) < 20:
        print(f"{asset}: too few eval points ({len(ds)})")
        return

    inputs = [closes[: d + 1] for d in ds]               # PAST-ONLY (no lookahead)
    pts, qs, backend = forecast_batch(inputs, horizon)
    dec = closes[ds]                                      # price at decision (closes[d])
    fut = closes[[d + horizon for d in ds]]              # price horizon bars later
    realized = fut / dec - 1.0
    pred = pts[:, -1] / dec - 1.0                         # forecast H-step return
    band_pct = (qs[:, -1, 9] - qs[:, -1, 1]) / dec * 100  # forecast uncertainty width

    typ = _typical_band_pct(closes[:start], horizon)     # yardstick from PRE-window history (no lookahead)

    pos = np.sign(pred)                                   # long if forecast up, short if down
    hit = np.mean(np.sign(pred) == np.sign(realized)) * 100
    traded = pos != 0

    def equity(mask: np.ndarray) -> tuple[float, int]:
        net = np.where(mask, pos * realized - COST, 0.0)
        return (float(np.prod(1.0 + net) - 1.0) * 100, int(mask.sum()))

    all_roi, all_n = equity(traded)
    conf = traded & (band_pct < typ)                     # abstain-on-uncertainty (#2): trade only when band tight
    conf_roi, conf_n = equity(conf)
    conf_hit = (np.mean(np.sign(pred[conf]) == np.sign(realized[conf])) * 100) if conf.any() else float("nan")
    bh = float(closes[ds[-1] + horizon] / closes[ds[0]] - 1.0) * 100
    gross = float(np.prod(1.0 + pos * realized) - 1.0) * 100

    print(f"\n=== {asset}  ({backend}) ===")
    print(f"  OOS window: last {n - start} bars · {len(ds)} non-overlapping {horizon}h trades")
    print(f"  directional hit-rate : {hit:.1f}%   (50% = coin-flip)")
    print(f"  ALL-TRADE  net ROI   : {all_roi:+.1f}%  (gross {gross:+.1f}%, cost drag {gross - all_roi:.1f}pp)  vs B&H {bh:+.1f}%")
    print(f"  CONFIDENT-ONLY (#2)  : {conf_roi:+.1f}%  over {conf_n}/{all_n} trades  · hit {conf_hit:.1f}%  (band<{typ:.1f}% typ)")
    avg_bps = (all_roi / max(all_n, 1)) * 100
    verdict = ("EDGE: beats B&H + survives cost" if all_roi > max(bh, 0) and all_roi > 0
               else "abstain-filter helps" if conf_roi > all_roi and conf_roi > 0
               else "NO cost-aware edge (forecast direction fee-eaten / coin-flip)")
    print(f"  avg net/trade ~{avg_bps:+.0f} bps  →  {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("assets", nargs="*", default=["MNT"])
    ap.add_argument("--win", type=int, default=3000)
    ap.add_argument("--horizon", type=int, default=24)
    a = ap.parse_args()
    for asset in (a.assets or ["MNT"]):
        try:
            run(asset.upper(), a.win, a.horizon)
        except Exception as e:  # noqa: BLE001
            print(f"{asset}: ERROR {e}")
