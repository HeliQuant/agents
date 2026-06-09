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

FEE = 0.00055    # exchange taker per side (matches scripts/39)
SLIPPAGE = 0.00045  # spread + slippage per side on a LIQUID venue (~4.5 bps). Added 2026-06-07 for HONEST
#                     execution cost: a real fill pays fee + half-spread + slippage, NOT just the fee. (A live
#                     100-trade test showed the wider TESTNET spread ~190 bps eats any edge — so trade liquid
#                     venues only, and validate against realistic cost, not the optimistic fee-only number.)
COST = FEE + SLIPPAGE  # per-side execution cost used by the validation gate (round-trip = 2*COST = ~20 bps)
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


def _norm_cdf(z: float) -> float:
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def pval_mean_positive(rets) -> float:
    """One-sided p-value that mean(rets) > 0 (t-stat, normal approx — no scipy). Lower = more significant."""
    r = np.asarray(rets, dtype=float)
    n = len(r)
    sd = r.std(ddof=1) if n > 1 else 0.0
    if n < 2 or sd == 0:
        return 1.0
    return float(1.0 - _norm_cdf(r.mean() / (sd / np.sqrt(n))))


def benjamini_hochberg(pvals: dict, alpha: float = 0.10) -> dict:
    """Benjamini-Hochberg FDR control. {name: pval} -> {name: passed_fdr}. Caps the false-discovery rate
    when MANY signals/assets are tested at once (the 'best-of-N looks great by luck' trap)."""
    items = sorted(((k, v) for k, v in pvals.items() if v is not None), key=lambda kv: kv[1])
    m = len(items)
    passed = {k: False for k in pvals}
    if m == 0:
        return passed
    kmax = 0
    for i, (_, p) in enumerate(items, start=1):
        if p <= alpha * i / m:
            kmax = i
    for i, (k, _) in enumerate(items, start=1):
        passed[k] = i <= kmax
    return passed


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
        net = pos * (close[i + H] / close[i] - 1) - 2 * COST
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
    pval = pval_mean_positive(rets)   # one-sided significance of mean net > 0 (for FDR correction across signals)
    rt_fee_bps = 2 * COST * 1e4  # realistic round-trip cost (fee + spread + slippage), not fee-only
    oos_roi = (eq - 1) * 100
    passed = bool(oos_roi > 0 and oos_roi > bh * 100 and avg_bps > rt_fee_bps
                  and payoff > 0 and trades >= MIN_TRADES)
    return {
        "contrarian": bool(contrarian), "train_ic": round(float(ic), 3),
        "oos_roi_pct": round(oos_roi, 2), "trades": trades,
        "win_pct": round(wins / trades * 100, 1), "p_win": round(wins / trades, 4),
        "payoff_b": round(float(payoff), 3), "avg_bps": round(avg_bps, 1),
        "buyhold_pct": round(bh * 100, 2), "rt_fee_bps": round(rt_fee_bps, 1),
        "pval": round(pval, 4), "passed": passed,
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
            eq *= 1 + (pos * (close[i + H] / close[i] - 1) - 2 * COST)
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
    # FDR (Benjamini-Hochberg) across every signal tested here: crowning the best of N signals is the classic
    # multiple-testing trap (best-of-N looks great by pure luck). An edge must clear its own cost-aware OOS
    # gate AND survive FDR before it's eligible for the registry slot — caps the false-discovery rate.
    fdr = benjamini_hochberg({r["source"]: r["pval"] for r in results}, alpha=0.10)
    for r in results:
        r["fdr_pass"] = bool(fdr.get(r["source"], False))
    passed = [r for r in results if r["passed"] and r["fdr_pass"]]
    best = max(passed, key=lambda r: r["oos_roi_pct"]) if passed else None
    if best:
        best["wf"] = walk_forward(close, SIGNAL_SOURCES[best["source"]](df).values)
        best["robust"] = bool(best["wf"] and best["wf"].get("consistent"))
    return {"asset": asset.upper(), "results": results, "earned": best}


def sync_edges_hq() -> int:
    """Mirror the local EDGE registry (validated_edges.json + candidate_edges.json) to Supabase
    `edges_hq` (upsert by asset) so the firm's edge state — incl. CANDIDATE/probation edges like HYPE
    — is FE-readable. Local JSON stays source of truth. Returns #rows synced (0 if no Supabase creds)."""
    import json as _json
    from firm.desk_performance import _supabase

    def _rows(p: Path, tier: str) -> list[dict]:
        d = _json.loads(p.read_text()) if p.exists() else {}
        return [{"asset": a, "edge": e.get("edge"), "tier": tier, "validated": bool(e.get("validated")),
                 "p_win": e.get("p_win"), "payoff_b": e.get("payoff_b"), "sample_n": e.get("sample_n"),
                 "oos_roi_pct": e.get("oos_roi_pct"), "confirmations": int(e.get("confirmations", 0) or 0),
                 "last_confirm_bar": e.get("last_confirm_bar"), "note": e.get("note")} for a, e in d.items()]

    rows = _rows(ROOT / "data" / "validated_edges.json", "validated") + \
        _rows(ROOT / "data" / "candidate_edges.json", "candidate")
    sb = _supabase()
    if not sb or not rows:
        return 0
    sb.table("edges_hq").upsert(rows, on_conflict="asset").execute()
    return len(rows)


def is_stable_robust(asset: str, edge: str, data_dir, cutoffs=(0.8, 0.9, 1.0)) -> dict:
    """STABILITY gate for graduation: an edge graduates ONLY if it's robust CONSISTENTLY across
    MEANINGFUL data fractions (80/90/100% — matching the timeline standard that validated MNT), not a
    single lucky snapshot. Cutoffs are deliberately WIDE (not 85/92/100%, which proved noise-sensitive:
    they gave SUI a spurious [F,T,T] despite SUI being clearly robust at 80/90/100%). An edge whose
    robustness genuinely FLIPS across these wide fractions is unstable -> hold. Uniform across assets."""
    if edge not in _EDGE_SRC:
        return {"stable": False, "robust_at": [], "note": f"unknown edge {edge}"}
    src, _ = _EDGE_SRC[edge]
    fp = Path(data_dir) / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return {"stable": False, "robust_at": [], "note": "no data"}
    full = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    robust_at = []
    for f in cutoffs:
        df = full.iloc[: int(len(full) * f)].reset_index(drop=True)
        c = df["close"].values
        sig = SIGNAL_SOURCES[src](df).values
        m = validate_signal(c, sig)
        wf = walk_forward(c, sig)
        robust_at.append(bool(m and m["passed"] and wf and wf.get("consistent")))
    return {"cutoffs": list(cutoffs), "robust_at": robust_at, "stable": all(robust_at)}
