"""scripts/test_timesfm_covariates_oos.py — #5: does fusing the firm's intel (OI / funding / flow) as
TimesFM XReg covariates beat the RAW forecast on the recent OOS window?

Apples-to-apples vs raw: same data (positioning.csv close), same decision points, same cost-aware
protocol (stride = horizon, no lookahead, 20bps round-trip).

HONEST CAVEAT: XReg dynamic covariates need FUTURE values over the horizon; the firm's intel is
unknown-future → we **persist the last-known value** across the horizon (defensible for slow-moving
OI/funding — labelled). "xreg + timesfm" mode: TimesFM baseline → fit residual~covariate on the
context → apply to the (persisted) horizon covariate as an adjustment. Small sample, report as-seen.

Run: python scripts/test_timesfm_covariates_oos.py MNT --win 2500 --horizon 24
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import timesfm  # noqa: E402

from firm.timesfm_desk import _CTX, _get_model  # noqa: E402

COST = 0.001 * 2  # round-trip


def _model():
    """Real TimesFM, recompiled with return_backcast=True (required by XReg)."""
    m = _get_model()
    if m is None:
        return None
    m.compile(timesfm.ForecastConfig(
        max_context=_CTX, max_horizon=256, normalize_inputs=True,
        use_continuous_quantile_head=True, force_flip_invariance=True,
        infer_is_positive=True, fix_quantile_crossing=True, return_backcast=True,
    ))
    return m


def _load(asset: str) -> tuple[np.ndarray, dict]:
    df = pd.read_csv(ROOT / "data" / f"{asset.lower()}_positioning.csv")
    if "flow" not in df.columns and "buy_ratio" in df.columns:
        df["flow"] = df["buy_ratio"] - 0.5
    cols = [c for c in ("close", "oi", "funding", "flow") if c in df.columns]
    df = df[cols].dropna().reset_index(drop=True)
    close = df["close"].to_numpy(float)
    covs = {c: df[c].to_numpy(float) for c in ("oi", "funding", "flow") if c in df.columns}
    return close, covs


def _roi_hit(pred: np.ndarray, realized: np.ndarray) -> tuple[float, float]:
    pos = np.sign(pred)
    hit = float(np.mean(np.sign(pred) == np.sign(realized)) * 100)
    roi = float((np.prod(1.0 + (pos * realized - COST)) - 1.0) * 100)
    return hit, roi


def run(asset: str, win: int, horizon: int) -> None:
    model = _model()
    if model is None:
        print(f"{asset}: real TimesFM not loaded (fallback can't do covariates)")
        return
    close, covs = _load(asset)
    n = len(close)
    start = max(_CTX, n - win)
    ds = [d for d in range(start, n - horizon, horizon)]
    if len(ds) < 20 or not covs:
        print(f"{asset}: insufficient ({len(ds)} pts, covs={list(covs)})")
        return
    inputs = [close[d + 1 - _CTX: d + 1] for d in ds]
    dec = close[ds]
    fut = close[[d + horizon for d in ds]]
    realized = fut / dec - 1.0
    CH = 32

    raw_pt, cov_pt = [], []
    dyn_full = {name: [np.concatenate([v[d + 1 - _CTX: d + 1], np.full(horizon, v[d])]) for d in ds]
                for name, v in covs.items()}
    for i in range(0, len(ds), CH):
        sl = slice(i, i + CH)
        raw_out = model.forecast(horizon=horizon, inputs=inputs[sl])
        raw_pt.append(np.asarray(raw_out[0], float))
        cov_out = model.forecast_with_covariates(
            inputs=inputs[sl],
            dynamic_numerical_covariates={k: v[sl] for k, v in dyn_full.items()},
            xreg_mode="xreg + timesfm",
        )
        cov_pt.append(np.asarray(cov_out[0], float))

    raw_pred = np.concatenate(raw_pt)[:, -1] / dec - 1.0
    cov_pred = np.concatenate(cov_pt)[:, -1] / dec - 1.0

    raw_hit, raw_roi = _roi_hit(raw_pred, realized)
    cov_hit, cov_roi = _roi_hit(cov_pred, realized)
    bh = float(close[ds[-1] + horizon] / close[ds[0]] - 1.0) * 100
    agree = float(np.mean(np.sign(raw_pred) == np.sign(cov_pred)) * 100)

    print(f"\n=== {asset}  · {len(ds)} non-overlap {horizon}h trades · covs={list(covs)} (persisted fwd) ===")
    print(f"  RAW forecast       : hit {raw_hit:.1f}%   net ROI {raw_roi:+.1f}%")
    print(f"  COVARIATE (XReg)   : hit {cov_hit:.1f}%   net ROI {cov_roi:+.1f}%")
    print(f"  buy & hold         : {bh:+.1f}%   |  raw/cov direction agree {agree:.0f}%")
    d_hit, d_roi = cov_hit - raw_hit, cov_roi - raw_roi
    verdict = ("COVARIATES HELP (hit + ROI both up)" if d_hit > 1 and d_roi > 1
               else "covariates help ROI only" if d_roi > 2
               else "NO meaningful lift from intel covariates")
    print(f"  Δ from intel       : hit {d_hit:+.1f}pp · ROI {d_roi:+.1f}pp  →  {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("assets", nargs="*", default=["MNT"])
    ap.add_argument("--win", type=int, default=2500)
    ap.add_argument("--horizon", type=int, default=24)
    a = ap.parse_args()
    for asset in (a.assets or ["MNT"]):
        try:
            run(asset.upper(), a.win, a.horizon)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"{asset}: ERROR {e}")
            traceback.print_exc()
