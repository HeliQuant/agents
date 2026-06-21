"""scripts/test_timesfm_vol_oos.py — the RIGHT test: does TimesFM forecast REALIZED VOLATILITY
(a forecastable target) better than RW / EWMA / HAR-RV baselines?

Why this and not direction: daily return DIRECTION is ~unpredictable (martingale) — proven dead in
scripts/test_timesfm_oos.py. But realized VOL is autocorrelated/clustered → forecastable, and the
literature (arXiv 2505.11163) shows fine-tuned TimesFM beats HAR/GARCH on it. This is zero-shot (a
floor); if even zero-shot is competitive, the desk's real role = a VOLATILITY/RISK engine.

RV target: daily realized vol from hourly log-returns, RV_d = sqrt(Σ r_t²) over the 24h block.
Baselines: RW (RV_{d-1}), EWMA(span), HAR-RV (RV ~ daily + weekly + monthly), fit on pre-OOS only.
Metrics (lower better except corr): QLIKE (vol loss), RMSE, MAE, corr & R² vs realized, vol-direction hit.

Run: python scripts/test_timesfm_vol_oos.py MNT BTC ETH --oos 250
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

from firm.timesfm_desk import forecast_batch  # noqa: E402


def _rv(asset: str) -> np.ndarray:
    """Daily realized vol from hourly closes: RV_d = sqrt(sum of squared hourly log-returns)."""
    close = pd.read_csv(ROOT / "data" / f"{asset.lower()}_features.csv", usecols=["close"])["close"].to_numpy(float)
    r = np.diff(np.log(close))
    nd = len(r) // 24
    return np.array([np.sqrt(np.sum(r[d * 24:(d + 1) * 24] ** 2)) for d in range(nd)])


def _har_fit(rv: np.ndarray, upto: int) -> np.ndarray:
    """Fit HAR-RV coefficients on rv[:upto] (daily, weekly=5, monthly=22 averages)."""
    X, y = [], []
    for t in range(22, upto):
        X.append([1.0, rv[t - 1], rv[t - 5:t].mean(), rv[t - 22:t].mean()])
        y.append(rv[t])
    beta, *_ = np.linalg.lstsq(np.array(X), np.array(y), rcond=None)
    return beta


def _har_pred(rv: np.ndarray, t: int, beta: np.ndarray) -> float:
    return float(beta @ np.array([1.0, rv[t - 1], rv[t - 5:t].mean(), rv[t - 22:t].mean()]))


def _metrics(pred: np.ndarray, act: np.ndarray, prev: np.ndarray) -> dict:
    pred = np.clip(pred, 1e-9, None)
    err = pred - act
    qlike = float(np.mean(act / pred - np.log(act / pred) - 1.0))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    mae = float(np.mean(np.abs(err)))
    corr = float(np.corrcoef(pred, act)[0, 1])
    dir_hit = float(np.mean(np.sign(pred - prev) == np.sign(act - prev)) * 100)
    return {"QLIKE": qlike, "RMSE": rmse * 1e4, "corr": corr, "dirHit": dir_hit}


def run(asset: str, oos: int) -> None:
    rv = _rv(asset)
    n = len(rv)
    start = max(60, n - oos)
    idx = list(range(start, n))
    act = rv[idx]
    prev = rv[[i - 1 for i in idx]]

    # TimesFM: forecast next-day RV from RV history (capped context)
    inputs = [rv[max(0, i - 512):i] for i in idx]
    pts, _q, backend = forecast_batch(inputs, 1)
    tfm = np.clip(pts[:, 0], 1e-9, None)

    # baselines
    rw = prev
    ew = np.array([pd.Series(rv[:i]).ewm(span=10).mean().iloc[-1] for i in idx])
    beta = _har_fit(rv, start)
    har = np.array([_har_pred(rv, i, beta) for i in idx])

    print(f"\n=== {asset}  ({backend}) · RV next-day forecast · {len(idx)} OOS days ===")
    print(f"  {'method':10} {'QLIKE↓':>9} {'RMSE↓(bp)':>10} {'corr↑':>7} {'dirHit↑':>8}")
    rows = {"TimesFM": tfm, "RW(last)": rw, "EWMA": ew, "HAR-RV": har}
    best_q = min(_metrics(p, act, prev)["QLIKE"] for p in rows.values())
    for name, p in rows.items():
        m = _metrics(p, act, prev)
        star = " *" if abs(m["QLIKE"] - best_q) < 1e-9 else ""
        print(f"  {name:10} {m['QLIKE']:>9.4f} {m['RMSE']:>10.2f} {m['corr']:>7.3f} {m['dirHit']:>7.1f}%{star}")

    # rigor: Diebold-Mariano on QLIKE loss + 3-fold walk-forward robustness
    from math import erf, sqrt

    def _ql(p):
        p = np.clip(p, 1e-9, None)
        return act / p - np.log(act / p) - 1.0

    def _dm(a, b):  # loss(b) - loss(a); stat>0 & low p => a (TimesFM) significantly better
        d = _ql(b) - _ql(a)
        stat = float(d.mean() / (d.std(ddof=1) / sqrt(len(d)) + 1e-12))
        p = float(2 * (1 - 0.5 * (1 + erf(abs(stat) / sqrt(2)))))
        return stat, p
    for base in ("HAR-RV", "EWMA"):
        s, p = _dm(tfm, rows[base])
        sig = "  ← TimesFM SIGNIFICANTLY better" if s > 0 and p < 0.05 else ""
        print(f"  DM vs {base:7}: stat {s:+.2f}  p={p:.4f}{sig}")
    folds = np.array_split(np.arange(len(idx)), 3)
    wins = sum(1 for f in folds if _metrics(tfm[f], act[f], prev[f])["QLIKE"]
               < _metrics(har[f], act[f], prev[f])["QLIKE"])
    print(f"  walk-forward: TimesFM beats HAR-RV (QLIKE) in {wins}/3 folds")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("assets", nargs="*", default=["MNT"])
    ap.add_argument("--oos", type=int, default=250)
    a = ap.parse_args()
    for asset in (a.assets or ["MNT"]):
        try:
            run(asset.upper(), a.oos)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"{asset}: ERROR {e}")
            traceback.print_exc()
