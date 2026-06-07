"""firm/edge_evolver.py — GEPA-style reflective edge self-improvement.

When an edge DECAYS (MNT oi-contrarian: +76% thru Apr -> -7% now), the old loop just retires it. This
asks the next question: can it be ADAPTED? It diagnoses the likely failure mode, then searches a small,
SAFE variant space around the base edge — different horizons, and regime FILTERS that gate out the
conditions where it breaks (e.g. "don't fade OI in a strong trend — that's catching a falling knife").

Every variant is judged by the SAME bar that killed the original: cost-aware OOS net of realistic cost,
AND cross-cutoff robustness (positive at 70% / 85% / 100% of history — the window-stability MNT failed).
This is the overfitting guard — a variant only counts if it survives windows it wasn't tuned on. Output:
the best ROBUST variant, or an honest "no adaptation survives" (the edge is genuinely dead — don't force it).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import firm.edge_lab as el  # noqa: E402

HORIZONS = [24, 48]
REGIMES = ["none", "calm_only", "trend_aligned"]


def _regime_mask(close: np.ndarray, kind: str, H: int) -> np.ndarray:
    """Boolean per-bar mask of when the variant is ALLOWED to trade."""
    n = len(close)
    if kind == "none":
        return np.ones(n, dtype=bool)
    look = 168  # trailing 7d trend strength
    trail = np.full(n, np.nan)
    for i in range(look, n):
        trail[i] = close[i] / close[i - look] - 1
    if kind == "calm_only":               # only range-bound regimes (|7d move| < ~8%) — avoid strong trends
        return np.abs(trail) < 0.08
    if kind == "trend_aligned":           # only when a trend exists (let direction logic ride/fade it knowingly)
        return np.abs(trail) >= 0.08
    return np.ones(n, dtype=bool)


def _oos(close: np.ndarray, sig: np.ndarray, H: int, mask: np.ndarray, end_frac: float = 1.0) -> dict | None:
    """edge_lab-style OOS on the first `end_frac` of data, restricted to allowed (masked) bars."""
    n = int(len(close) * end_frac)
    idx = [i for i in range(H, n - H, H) if not np.isnan(sig[i]) and mask[i]]
    if len(idx) < 40:
        return None
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    tr_sig = np.array([sig[i] for i in tr]); tr_ret = np.array([close[i + H] / close[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    if np.isnan(ic):
        return None
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
    eq, rets = 1.0, []
    for i in te:
        s = sig[i]
        pos = (-1 if contrarian else 1) if s >= p80 else (1 if contrarian else -1) if s <= p20 else 0
        if pos == 0:
            continue
        net = pos * (close[i + H] / close[i] - 1) - 2 * el.COST
        eq *= 1 + net; rets.append(net)
    if len(rets) < 8:
        return None
    return {"oos_roi_pct": round((eq - 1) * 100, 1), "trades": len(rets),
            "contrarian": bool(contrarian), "avg_bps": round(float(np.mean(rets)) * 1e4, 1), "rets": rets}


def evaluate_variant(close: np.ndarray, sig: np.ndarray, H: int, regime: str) -> dict | None:
    """Full robust eval: OOS at 70/85/100% cutoffs. Robust = all positive (the window-stability MNT failed)."""
    cuts = [0.7, 0.85, 1.0]
    rois = []
    for c in cuts:
        m = _oos(close, sig, H, _regime_mask(close, regime, H), end_frac=c)
        if m is None:
            return None
        rois.append(m["oos_roi_pct"])
    full = _oos(close, sig, H, _regime_mask(close, regime, H), end_frac=1.0)
    rets = full.pop("rets")
    # OUTLIER guard: recompute OOS after dropping the single best trade (the +186bps avg + 21 trades smelled
    # of a lucky outlier). A real edge survives without its luckiest trade.
    bi = int(np.argmax(rets))
    ex_best = float(np.prod([1 + r for j, r in enumerate(rets) if j != bi]) - 1) * 100
    robust = all(r > 0 for r in rois) and ex_best > 0
    return {**full, "regime": regime, "H": H, "cutoff_rois": [round(r, 1) for r in rois],
            "ex_best_roi_pct": round(ex_best, 1), "robust": robust}


def evolve(asset: str, base_signal: str = "oi_chg24") -> dict:
    """Search horizon × regime variants of the (decayed) base edge; return the best ROBUST adaptation or none."""
    fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return {"asset": asset, "error": "no data"}
    df = pd.read_csv(fp)
    close = df["close"].values
    sig = el.SIGNAL_SOURCES[base_signal](df).values
    base = evaluate_variant(close, sig, 24, "none")  # the original edge
    cand = []
    for H in HORIZONS:
        for reg in REGIMES:
            v = evaluate_variant(close, sig, H, reg)
            if v:
                cand.append(v)
    survivors = sorted([v for v in cand if v["robust"] and v["oos_roi_pct"] > 0],
                       key=lambda x: x["oos_roi_pct"], reverse=True)
    return {"asset": asset.upper(), "base_signal": base_signal, "base": base,
            "survivors": survivors, "best": survivors[0] if survivors else None, "n_tested": len(cand)}


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    for a in (sys.argv[1:] or ["MNT"]):
        r = evolve(a)
        b = r.get("base") or {}
        print(f"\n=== EVOLVE {r['asset']} (base {r['base_signal']}) ===")
        print(f"  ORIGINAL (H24, no filter): OOS {b.get('oos_roi_pct')}% robust={b.get('robust')}")
        print(f"  tested {r['n_tested']} variants -> {len(r['survivors'])} robust survivors")
        if r["best"]:
            v = r["best"]
            print(f"  ✅ BEST ADAPTATION: H{v['H']} regime='{v['regime']}' -> OOS {v['oos_roi_pct']}% "
                  f"(cutoffs {v['cutoff_rois']}), ex-best-trade {v['ex_best_roi_pct']}%, {v['trades']} trades, {v['avg_bps']:+} bps")
            print(f"     CANDIDATE only — best-of-{r['n_tested']} search + {v['trades']} trades; needs forward-confirm before trading.")
            for v in r["survivors"][1:4]:
                print(f"     also: H{v['H']} '{v['regime']}' OOS {v['oos_roi_pct']}% ex-best {v['ex_best_roi_pct']}%")
        else:
            print("  ❌ NO adaptation survives cross-window robustness — edge genuinely dead, don't force it.")
