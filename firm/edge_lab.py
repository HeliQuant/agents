"""firm/edge_lab.py — the ASSET-ONBOARDING edge lab.

The honest core of "HeliQuant onboards new assets by EARNING each edge." It tests a LIBRARY of
edge hypotheses (signal sources) against an asset's real perp data under ONE rigorous,
overfit-resistant gate — the exact methodology of scripts/39_oi_backtest.py, generalised:

  * NON-OVERLAPPING 24h windows -> independent samples (no overlap inflation).
  * Signal DIRECTION (contrarian vs momentum) + quintile thresholds derived ONLY from TRAIN.
  * OOS net return = pos*raw - 2*taker_fee. An edge is EARNED only if, out-of-sample, it clears:
    ROI > 0  AND  ROI > buy&hold  AND  avg_bps > round-trip fee  AND  trades >= MIN_TRADES.

Register ONLY what clears the bar. validated_edges.json grows when an edge is earned — never on
hope, a live win-streak, or an in-sample mirage. Failures are reported in full (honesty-by-design).
This is the self-learning loop's deterministic spine; new hypotheses (incl. ML) plug in as sources.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

FEE = 0.00055    # Bybit perp taker per side (matches scripts/39)
H = 24           # hold / non-overlap window (hours)
MIN_TRADES = 20  # >= 20 OOS samples before trusting an edge (also the Kelly-trust threshold)

# Signal SOURCES: name -> fn(df) -> Series aligned to rows. The harness derives the direction.
SIGNAL_SOURCES = {
    "oi_chg24":       lambda df: df["oi"].pct_change(H),
    "price_mom24":    lambda df: df["close"].pct_change(H),
    "funding":        lambda df: df["funding"],
    "flow_imbalance": lambda df: df["buy_ratio"] - 0.5,
}
_COL = {"oi_chg24": "oi", "price_mom24": "close", "funding": "funding", "flow_imbalance": "buy_ratio"}


def edge_name(source: str, contrarian: bool) -> str:
    """Human edge name. oi_chg24+contrarian reproduces the canonical 'oi_contrarian'."""
    return {
        ("oi_chg24", True): "oi_contrarian", ("oi_chg24", False): "oi_momentum",
        ("price_mom24", True): "price_meanrev", ("price_mom24", False): "price_momentum",
        ("funding", True): "funding_fade", ("funding", False): "funding_follow",
        ("flow_imbalance", True): "flow_contrarian", ("flow_imbalance", False): "flow_momentum",
    }.get((source, contrarian), f"{source}_{'contrarian' if contrarian else 'momentum'}")


def validate_signal(close: np.ndarray, sig: np.ndarray) -> dict | None:
    """Honest OOS/cost-aware gate on one signal source. Direction + thresholds from TRAIN only.
    Returns metrics + ``passed``, or None if too little data."""
    n = len(close)
    idx = [i for i in range(H, n - H, H) if not np.isnan(sig[i])]
    if len(idx) < 50:
        return None
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    tr_sig = np.array([sig[i] for i in tr])
    tr_ret = np.array([close[i + H] / close[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    if np.isnan(ic):
        return None
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)

    eq, trades, wins, rets = 1.0, 0, 0, []
    for i in te:
        s = sig[i]
        if s >= p80:
            pos = -1 if contrarian else 1
        elif s <= p20:
            pos = 1 if contrarian else -1
        else:
            continue
        net = pos * (close[i + H] / close[i] - 1) - 2 * FEE
        eq *= 1 + net
        trades += 1
        wins += int(net > 0)
        rets.append(net)
    if trades == 0:
        return None
    bh = close[te[-1] + H] / close[te[0]] - 1
    w = [r for r in rets if r > 0]
    ls = [r for r in rets if r <= 0]
    payoff = (np.mean(w) / abs(np.mean(ls))) if (w and ls) else 0.0
    avg_bps = float(np.mean(rets) * 1e4)
    rt_fee_bps = 2 * FEE * 1e4
    oos_roi = (eq - 1) * 100
    passed = bool(oos_roi > 0 and oos_roi > bh * 100 and avg_bps > rt_fee_bps
                  and payoff > 0 and trades >= MIN_TRADES)
    return {
        "contrarian": bool(contrarian), "train_ic": round(float(ic), 3),
        "oos_roi_pct": round(oos_roi, 2), "trades": trades,
        "win_pct": round(wins / trades * 100, 1), "p_win": round(wins / trades, 4),
        "payoff_b": round(float(payoff), 3), "avg_bps": round(avg_bps, 1),
        "buyhold_pct": round(bh * 100, 2), "rt_fee_bps": round(rt_fee_bps, 1), "passed": passed,
    }


def walk_forward(close: np.ndarray, sig: np.ndarray, k: int = 5) -> dict | None:
    """Anchored (expanding-window) walk-forward: re-derive direction+thresholds from the growing
    TRAIN, test on K sequential OOS folds. A real edge is CONSISTENT (positive in most folds), not
    one lucky split. This is the extra bar a NEW edge must clear before it joins the live registry —
    the discipline that rejected our own spectacular-but-fragile backtests."""
    n = len(close)
    idx = [i for i in range(H, n - H, H) if not np.isnan(sig[i])]
    if len(idx) < 80:
        return None
    init = int(len(idx) * 0.4)
    folds = [list(f) for f in np.array_split(np.array(idx[init:]), k)]
    fold_rois, train_end = [], init
    for f in folds:
        if not f:
            continue
        train = idx[:train_end]
        tr_sig = np.array([sig[i] for i in train])
        tr_ret = np.array([close[i + H] / close[i] - 1 for i in train])
        ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
        contrarian = ic < 0
        p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
        eq, tr = 1.0, 0
        for i in f:
            s = sig[i]
            if s >= p80:
                pos = -1 if contrarian else 1
            elif s <= p20:
                pos = 1 if contrarian else -1
            else:
                continue
            eq *= 1 + (pos * (close[i + H] / close[i] - 1) - 2 * FEE)
            tr += 1
        if tr > 0:
            fold_rois.append(round((eq - 1) * 100, 2))
        train_end += len(f)
    if len(fold_rois) < 3:
        return {"folds": len(fold_rois), "positive": sum(r > 0 for r in fold_rois),
                "fold_rois": fold_rois, "consistent": False, "note": "too few populated folds"}
    positive = sum(1 for r in fold_rois if r > 0)
    maj = positive >= int(np.ceil(len(fold_rois) * 0.6)) and float(np.mean(fold_rois)) > 0
    # OUTLIER-ROBUSTNESS: a real edge must not bank its return in ONE lucky fold. Drop the single
    # best fold; it must still be majority-positive with a positive mean. This is what separates a
    # distributed edge (MNT) from a one-window mirage (the trap we keep refusing to fall for).
    trimmed = sorted(fold_rois)[:-1] if len(fold_rois) >= 4 else fold_rois
    trim_ok = (sum(1 for r in trimmed if r > 0) > len(trimmed) / 2) and float(np.mean(trimmed)) > 0
    return {"folds": len(fold_rois), "positive": positive,
            "mean_fold_roi": round(float(np.mean(fold_rois)), 2),
            "ex_best_mean": round(float(np.mean(trimmed)), 2),
            "fold_rois": [round(float(r), 2) for r in fold_rois],
            "consistent": bool(maj and trim_ok)}


def _usable(df: pd.DataFrame, src: str) -> bool:
    col = _COL[src]
    return col in df.columns and df[col].notna().sum() > 60 and df[col].nunique() > 5


# edge_type -> (signal source, is-contrarian) — inverse of edge_name; lets the org/loop act on ANY edge.
_EDGE_SRC = {
    "oi_contrarian": ("oi_chg24", True), "oi_momentum": ("oi_chg24", False),
    "price_meanrev": ("price_mom24", True), "price_momentum": ("price_mom24", False),
    "funding_fade": ("funding", True), "funding_follow": ("funding", False),
    "flow_contrarian": ("flow_imbalance", True), "flow_momentum": ("flow_imbalance", False),
}


def live_signal(asset: str, edge_type: str, data_dir) -> dict:
    """Current actionable signal for a given edge TYPE — generalised beyond OI so the org/loop can act
    on ANY earned-or-candidate edge. Direction is encoded in edge_type; thresholds are the asset's own
    historical quintiles (same construction as organization._oi_contrarian_signal). signal in {LONG,SHORT,None}."""
    if edge_type not in _EDGE_SRC:
        return {"signal": None, "note": f"unknown edge_type {edge_type}"}
    src, contrarian = _EDGE_SRC[edge_type]
    fp = Path(data_dir) / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return {"signal": None, "note": "no positioning data"}
    df = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    if not _usable(df, src):
        return {"signal": None, "note": f"source {src} not usable"}
    sig = SIGNAL_SOURCES[src](df).dropna()
    if sig.empty:
        return {"signal": None, "note": "no signal values"}
    latest, p20, p80 = float(sig.iloc[-1]), float(sig.quantile(0.20)), float(sig.quantile(0.80))
    s = ("SHORT" if contrarian else "LONG") if latest >= p80 else \
        ("LONG" if contrarian else "SHORT") if latest <= p20 else None
    return {"signal": s, "edge_type": edge_type, "source": src, "contrarian": contrarian,
            "latest": round(latest, 6), "p20": round(p20, 6), "p80": round(p80, 6),
            "actionable": s is not None}


def onboard(asset: str, data_dir) -> dict:
    """Test ALL signal sources against an asset's real perp data. Returns the full report
    (every hypothesis, pass or fail) + the best passing edge (if any)."""
    fp = Path(data_dir) / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return {"asset": asset.upper(), "error": "no positioning data", "results": [], "earned": None}
    df = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    close = df["close"].values
    results = []
    for src, fn in SIGNAL_SOURCES.items():
        if not _usable(df, src):
            continue
        m = validate_signal(close, fn(df).values)
        if m is None:
            continue
        m["source"] = src
        m["edge"] = edge_name(src, m["contrarian"])
        results.append(m)
    passed = [r for r in results if r["passed"]]
    best = max(passed, key=lambda r: r["oos_roi_pct"]) if passed else None
    if best:
        best["wf"] = walk_forward(close, SIGNAL_SOURCES[best["source"]](df).values)
        best["robust"] = bool(best["wf"] and best["wf"].get("consistent"))
    return {"asset": asset.upper(), "results": results, "earned": best}
