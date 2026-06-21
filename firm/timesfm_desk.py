"""firm/timesfm_desk.py — TimesFM Volatility / Risk Desk.

Backend: Google Research **TimesFM 2.5 (200M)** zero-shot (else a clearly-labelled EWMA fallback).

VALIDATED ROLE (the reason this desk exists): **next-day realized-VOLATILITY forecasting.** OOS
(250 days, Diebold-Mariano test) TimesFM zero-shot **significantly beats HAR-RV and EWMA** on
BTC/ETH/SOL (DM p<0.05, 3/3 walk-forward folds; MNT competitive but not significant) — see
scripts/test_timesfm_vol_oos.py. Vol is autocorrelated/clustered → forecastable, UNLIKE price
DIRECTION (24h ≈ martingale: OOS coin-flip, NO edge — scripts/test_timesfm_oos.py; covariate-fusion
didn't help either — XReg needs known-future covariates, the firm's intel is unknown-future).

So the desk's PRIMARY output is `volatility` (next-day RV + rising/falling regime + a vol-targeting
sizing multiplier), for risk / sizing / abstain. The price forecast (quantile band, direction,
residual regime-break anomaly) is kept as CONTEXT only — direction carries no tradeable edge.

HONEST BOUNDARY: the vol-forecast value is OOS-validated (above); price-direction is NOT. `backend`
is reported on every call so a fallback read is never mistaken for TimesFM.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

_CTX = 1024          # context bars fed to the forecaster
_DECILE_Z = np.array([-1.2816, -0.8416, -0.5244, -0.2533, 0.0, 0.2533, 0.5244, 0.8416, 1.2816])

_MODEL = None
_BACKEND: str | None = None  # "timesfm-2.5-200m" | "fallback-ewma-lognormal"


# ── remote backend (HF Space) — set TIMESFM_URL to keep the 200M model off this host ─────────────
def _remote_url() -> str:
    return os.environ.get("TIMESFM_URL", "").strip()


def _remote_forecast(series_list: list, horizon: int) -> tuple[np.ndarray, np.ndarray, str]:
    """POST series to the HeliQuant TimesFM HF Space; returns (points[N,H], quantiles[N,H,10], backend)."""
    import requests  # noqa: PLC0415

    tok = os.environ.get("TIMESFM_TOKEN", "").strip()         # app-level SPACE_TOKEN → X-App-Token
    hf = os.environ.get("TIMESFM_HF_TOKEN", "").strip()       # HF access token → Authorization (PRIVATE Space)
    headers = {}
    if tok:
        headers["X-App-Token"] = tok
    if hf:
        headers["Authorization"] = f"Bearer {hf}"
    payload = {"series": [list(map(float, np.asarray(s, float)[-_CTX:])) for s in series_list], "horizon": horizon}
    r = requests.post(f"{_remote_url()}/forecast", json=payload, headers=headers, timeout=90)
    r.raise_for_status()
    j = r.json()
    return np.asarray(j["points"], float), np.asarray(j["quantiles"], float), "timesfm-remote"


# ── backend ──────────────────────────────────────────────────────────────────
def _get_model():
    """Lazily load real TimesFM 2.5 if installed; cache the decision. Returns model or None."""
    global _MODEL, _BACKEND
    if _BACKEND is not None:
        return _MODEL
    try:
        import timesfm  # noqa: PLC0415
        import torch  # noqa: PLC0415

        torch.set_float32_matmul_precision("high")
        m = timesfm.TimesFM_2p5_200M_torch.from_pretrained("google/timesfm-2.5-200m-pytorch")
        m.compile(
            timesfm.ForecastConfig(
                max_context=_CTX, max_horizon=256, normalize_inputs=True,
                use_continuous_quantile_head=True, force_flip_invariance=True,
                infer_is_positive=True, fix_quantile_crossing=True,
            )
        )
        _MODEL, _BACKEND = m, "timesfm-2.5-200m"
    except Exception:  # noqa: BLE001  (not installed / load failed → honest fallback)
        _MODEL, _BACKEND = None, "fallback-ewma-lognormal"
    return _MODEL


def forecast(closes: np.ndarray, horizon: int) -> tuple[np.ndarray, np.ndarray, str]:
    """Forecast `horizon` steps ahead from a 1-D close series.

    Returns (point[H], quantiles[H,10], backend). Quantile cols = [median, q10..q90] (TimesFM order).
    """
    closes = np.asarray(closes, dtype=float)
    if _remote_url():  # remote HF Space backend (no local 200M model)
        try:
            p, q, b = _remote_forecast([closes], horizon)
            return p[0], q[0], b
        except Exception:  # noqa: BLE001  (Space asleep / error → fall back to local or EWMA)
            pass
    model = _get_model()
    if model is not None:
        ctx = closes[-_CTX:]
        point, quant = model.forecast(horizon=horizon, inputs=[ctx])
        return np.asarray(point[0], float), np.asarray(quant[0], float), _BACKEND

    # fallback: EWMA log-return drift + log-normal bands (median == point)
    series = closes[-500:]
    logret = np.diff(np.log(series))
    drift = float(pd.Series(logret).ewm(span=72).mean().iloc[-1])      # ~3-day EWMA drift
    sigma = float(np.std(logret[-240:]) or np.std(logret) or 1e-4)     # recent hourly vol
    last = float(series[-1])
    point = np.empty(horizon)
    quant = np.empty((horizon, 10))
    for k in range(horizon):
        step = k + 1
        med = last * np.exp(drift * step)
        sig_h = sigma * np.sqrt(step)
        point[k] = med
        quant[k, 0] = med
        quant[k, 1:] = last * np.exp(drift * step + _DECILE_Z * sig_h)
    return point, quant, _BACKEND


def forecast_batch(series_list: list, horizon: int, chunk: int = 64) -> tuple[np.ndarray, np.ndarray, str]:
    """Batch many windows in one (chunked) model call — for OOS backtests. Each input sliced to last _CTX.

    Returns (points[N,H], quantiles[N,H,10], backend).
    """
    arrs = [np.asarray(s, float)[-_CTX:] for s in series_list]
    model = _get_model()
    if model is not None:
        P, Q = [], []
        for i in range(0, len(arrs), chunk):
            pts, qs = model.forecast(horizon=horizon, inputs=arrs[i:i + chunk])
            P.append(np.asarray(pts, float))
            Q.append(np.asarray(qs, float))
        return np.concatenate(P), np.concatenate(Q), _BACKEND
    P = np.empty((len(arrs), horizon))
    Q = np.empty((len(arrs), horizon, 10))
    for i, s in enumerate(arrs):
        p, q, _ = forecast(s, horizon)
        P[i], Q[i] = p, q
    return P, Q, _BACKEND


# ── data ─────────────────────────────────────────────────────────────────────
def _closes(ticker: str) -> np.ndarray:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv", usecols=["close"]).dropna()
    return df["close"].to_numpy(float)


def _rv_daily(closes: np.ndarray) -> np.ndarray:
    """Daily realized vol from hourly closes: RV_d = sqrt(Σ hourly log-return²) per 24h block.

    Vol is the FORECASTABLE target (autocorrelated/clustered) — TimesFM beats HAR-RV/EWMA on it OOS,
    unlike price direction (a near-martingale, no edge). This is the desk's validated role.
    """
    r = np.diff(np.log(np.asarray(closes, float)))
    nd = len(r) // 24
    return np.array([np.sqrt(np.sum(r[d * 24:(d + 1) * 24] ** 2)) for d in range(nd)]) if nd else np.array([])


def _typical_band_pct(closes: np.ndarray, horizon: int) -> float:
    """This asset's usual H-step 10–90 percentile move (%) — the yardstick for 'wide' uncertainty."""
    if len(closes) <= horizon + 50:
        return float("nan")
    fwd = closes[horizon:] / closes[:-horizon] - 1.0
    return float((np.nanpercentile(fwd, 90) - np.nanpercentile(fwd, 10)) * 100)


# ── desk ─────────────────────────────────────────────────────────────────────
def vol_sizing_factor(ticker: str, lo: float = 0.5, hi: float = 1.25) -> tuple[float, str]:
    """Vol-targeting position factor for the sizer: forecast next-day RV vs the asset's typical
    (median) RV → size DOWN when vol is forecast ABOVE typical, modest UP when below. Conservative
    clip [lo, hi]. Returns (1.0, reason) if unavailable. The vol-forecast is OOS-validated vs
    HAR-RV/EWMA (the desk's proven role) — this is the validated payoff wired into sizing.
    """
    try:
        rv = _rv_daily(_closes(ticker))
        if len(rv) < 60:
            return 1.0, "vol-sizing: insufficient RV history"
        rvf, _q, backend = forecast(rv[-512:], 1)
        vol_fc, tgt = float(rvf[0]), float(np.median(rv[-120:]))
        if vol_fc <= 0 or tgt <= 0:
            return 1.0, "vol-sizing: bad RV"
        factor = float(np.clip(tgt / vol_fc, lo, hi))
        reg = "high→down" if factor < 0.95 else "low→up" if factor > 1.05 else "normal"
        return round(factor, 2), f"vol-target({backend}): RV {vol_fc * 100:.2f}% vs typ {tgt * 100:.2f}% → x{factor:.2f} ({reg})"
    except Exception as e:  # noqa: BLE001
        return 1.0, f"vol-sizing unavailable: {str(e)[:60]}"


def tool_timesfm(ticker: str, horizon: int = 24) -> dict:
    """Forecast Desk output (firm desk pattern). `horizon` in hourly bars (default 24h)."""
    closes = _closes(ticker)
    if len(closes) < _CTX // 4:
        return {"asset": ticker.upper(), "error": "insufficient history", "backend": _BACKEND or "n/a"}

    last = float(closes[-1])
    point, quant, backend = forecast(closes, horizon)
    ph, lo, hi = float(point[-1]), float(quant[-1, 1]), float(quant[-1, 9])  # point, q10, q90 at H

    ret_pct = (ph - last) / last * 100
    band_pct = (hi - lo) / last * 100
    q10_pct, q90_pct = (lo - last) / last * 100, (hi - last) / last * 100

    typ = _typical_band_pct(closes, horizon)
    ratio = band_pct / typ if typ and typ == typ and typ > 0 else float("nan")
    confidence = "high" if ratio < 0.8 else "medium" if ratio < 1.2 else "low"
    abstain = bool(ratio == ratio and ratio > 1.5)  # band >1.5x usual → too uncertain to act

    # direction from the BAND, not just the point (worst/best case vs spot)
    if lo > last:
        direction = "bullish"
    elif hi < last:
        direction = "bearish"
    else:
        direction = "lean_bullish" if ret_pct > 0 else "lean_bearish" if ret_pct < 0 else "neutral"

    # residual-anomaly (#2): forecast made H bars ago vs what actually happened
    anomaly = {}
    if len(closes) > _CTX + horizon:
        pp, qq, _ = forecast(closes[:-horizon], horizon)
        prior_last = float(closes[-horizon - 1])
        realized = last
        z = (realized - float(pp[-1])) / (abs(float(qq[-1, 9]) - float(qq[-1, 1])) / 2.563 + 1e-9)
        outside = realized > float(qq[-1, 9]) or realized < float(qq[-1, 1])
        anomaly = {
            "realized_move_pct": round((realized - prior_last) / prior_last * 100, 2),
            "forecast_band_pct": round((float(qq[-1, 9]) - float(qq[-1, 1])) / prior_last * 100, 2),
            "z": round(float(z), 2),
            "regime_break": bool(outside),
        }

    # ── VALIDATED ROLE: next-day realized-vol forecast (TimesFM beats HAR-RV/EWMA OOS, zero-shot) ──
    volatility: dict = {}
    rv = _rv_daily(closes)
    if len(rv) > 30:
        rvf, _, _ = forecast(rv[-512:], 1)
        vol_fc, vol_now = float(rvf[0]), float(rv[-1])
        chg = (vol_fc / vol_now - 1.0) * 100 if vol_now else 0.0
        tgt = float(np.median(rv[-120:]))
        volatility = {
            "next_day_rv_pct": round(vol_fc * 100, 2),
            "now_rv_pct": round(vol_now * 100, 2),
            "change_pct": round(chg, 1),
            "regime": "rising" if chg > 10 else "falling" if chg < -10 else "stable",
            "percentile": round(float((rv < vol_fc).mean() * 100)),
            "sizing_multiplier": round(float(np.clip(tgt / max(vol_fc, 1e-9), 0.25, 2.0)), 2),
            "validated": "beats HAR-RV/EWMA OOS on QLIKE+corr (zero-shot; DM p<0.05 majors) — the desk's PROVEN role",
        }

    vlead = (f"VOL next-day {volatility['now_rv_pct']}%→{volatility['next_day_rv_pct']}% "
             f"({volatility['regime']}, pctile {volatility['percentile']}, size×{volatility['sizing_multiplier']}) · "
             if volatility else "")
    read = (
        vlead
        + f"price {horizon}h {ret_pct:+.2f}% ({direction}, {confidence}-conf — context-only, no directional edge)"
        + (" — ABSTAIN: band wide" if abstain else "")
        + (" — ⚠ REGIME-BREAK" if anomaly.get("regime_break") else "")
    )

    return {
        "asset": ticker.upper(),
        "backend": backend,
        "horizon_hours": horizon,
        "forecast_return_pct": round(ret_pct, 2),
        "band_pct": round(band_pct, 2),
        "q10_pct": round(q10_pct, 2),
        "q90_pct": round(q90_pct, 2),
        "typical_band_pct": round(typ, 2) if typ == typ else None,
        "band_vs_typical": round(ratio, 2) if ratio == ratio else None,
        "direction": direction,
        "confidence": confidence,
        "abstain_on_uncertainty": abstain,
        "anomaly": anomaly,
        "volatility": volatility,
        "directional_edge_oos": "none — recent-window OOS ~coin-flip & fee-eaten (test_timesfm_oos.py 2026-06-20); use the BAND (abstain) + ANOMALY (regime-break) + VOLATILITY (validated) + context, NOT the point direction, to trade",
        "stance_guidance": "This is a VOLATILITY/RISK desk — map the VOL read to your stance, NOT the price direction (which has no edge): rising / high-percentile vol → 'avoid' (risk-off, de-risk/size-down); stable or falling, low-percentile vol → 'neutral' (calm). Never vote 'bullish'/'bearish' on this desk.",
        "read": read,
        "caveat": "VOLATILITY forecast = VALIDATED role (TimesFM beats HAR-RV/EWMA OOS, DM p<0.05 on BTC/ETH/SOL → use for sizing/risk/regime/abstain). Price DIRECTION = context-only, NO edge (24h ≈ martingale).",
        "methodology": (
            "TimesFM 2.5 200M zero-shot: next-day realized-vol forecast (validated) + price quantile forecast (context/anomaly)"
            if backend == "timesfm-2.5-200m"
            else "FALLBACK baseline (EWMA log-drift + log-normal bands) — NOT TimesFM; install timesfm[torch] for the real model"
        ),
    }


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [a.upper() for a in sys.argv[1:]] or ["MNT", "BTC"]
    print(f"backend: {_get_model() and _BACKEND or _BACKEND}")
    for t in tickers:
        try:
            print(f"\n=== {t} ===")
            print(json.dumps(tool_timesfm(t), indent=2))
        except Exception as e:  # noqa: BLE001
            print(f"{t}: ERROR {e}")
