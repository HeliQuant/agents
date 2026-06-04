"""BTC deep-research — hunt a tradeable edge in NEW signal hypotheses, validated under the SAME honest
gate (cost-aware OOS + walk-forward + drop-best-fold). Single signals (oi/funding/flow/price) already
FAILED on BTC (fee-eaten). Here we try transforms + CONFLUENCE (multiple extremes agreeing = higher
conviction → maybe a bigger per-trade edge that clears the ~11 bps fee robustly).

Honest both ways: find a robust fee-clearing BTC edge, or confirm the available signals are exhausted
(BTC efficient → would need richer data: CVD/liquidations/cross-exchange/on-chain, which we don't have).

Run: python scripts/70_btc_deep_research.py [BTC]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FEE = 0.00055          # taker per side
RT_FEE_BPS = 2 * FEE * 1e4  # 11 bps round-trip (the bar avg must clear, as a slippage buffer)
H = 24


def _net_rets(close, entries, pos):
    """pos: dict i-> -1/0/1. Returns list of net returns for non-flat entries."""
    rets = []
    for i in entries:
        p = pos.get(i, 0)
        if p == 0:
            continue
        rets.append(p * (close[i + H] / close[i] - 1) - 2 * FEE)
    return rets


def _contrarian(sig, ret_idx, close):
    tr_sig = np.array([sig[i] for i in ret_idx])
    tr_ret = np.array([close[i + H] / close[i] - 1 for i in ret_idx])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    return (ic < 0) if not np.isnan(ic) else True


def positions_single(sig, idx, train, close, lo=20, hi=80):
    """Quintile-extreme contrarian/momentum positions (train-derived)."""
    contr = _contrarian(sig, train, close)
    plo, phi = np.nanpercentile([sig[i] for i in train], lo), np.nanpercentile([sig[i] for i in train], hi)
    pos = {}
    for i in idx:
        s = sig[i]
        if np.isnan(s):
            continue
        if s >= phi:
            pos[i] = -1 if contr else 1
        elif s <= plo:
            pos[i] = 1 if contr else -1
    return pos


def positions_confluence(sig_a, sig_b, idx, train, close):
    """Trade ONLY when BOTH signals are at the SAME contrarian extreme (confluence = high conviction)."""
    ca, cb = _contrarian(sig_a, train, close), _contrarian(sig_b, train, close)
    a_lo, a_hi = np.nanpercentile([sig_a[i] for i in train], 20), np.nanpercentile([sig_a[i] for i in train], 80)
    b_lo, b_hi = np.nanpercentile([sig_b[i] for i in train], 20), np.nanpercentile([sig_b[i] for i in train], 80)
    pos = {}
    for i in idx:
        a, b = sig_a[i], sig_b[i]
        if np.isnan(a) or np.isnan(b):
            continue
        da = (-1 if ca else 1) if a >= a_hi else (1 if ca else -1) if a <= a_lo else 0
        db = (-1 if cb else 1) if b >= b_hi else (1 if cb else -1) if b <= b_lo else 0
        if da != 0 and da == db:        # both fire the SAME direction
            pos[i] = da
    return pos


def positions_volcond(sig, vol, idx, train, close, high_vol=True):
    """OI-contrarian, but ONLY when realized vol is in the upper (or lower) half (regime filter)."""
    base = positions_single(sig, idx, train, close)
    vmed = np.nanmedian([vol[i] for i in train])
    return {i: p for i, p in base.items() if (vol[i] >= vmed) == high_vol}


def evaluate(close, idx, posfn, k=5):
    """1-split OOS + anchored walk-forward (drop-best-fold) for a position-builder posfn(idx_subset, train)."""
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    rets = _net_rets(close, te, posfn(te, tr))
    if len(rets) < 20:
        return None
    eq = float(np.prod([1 + r for r in rets]))
    bh = close[te[-1] + H] / close[te[0]] - 1
    avg_bps = float(np.mean(rets) * 1e4)
    oos_roi = (eq - 1) * 100
    clears = avg_bps > RT_FEE_BPS and oos_roi > 0 and oos_roi > bh * 100
    # walk-forward
    init = int(len(idx) * 0.4)
    folds = [list(f) for f in np.array_split(np.array(idx[init:]), k)]
    fr, end = [], init
    for f in folds:
        if not f:
            continue
        rr = _net_rets(close, f, posfn(f, idx[:end]))
        if rr:
            fr.append(round(float(np.prod([1 + r for r in rr]) - 1) * 100, 2))
        end += len(f)
    robust = False
    if len(fr) >= 3:
        trimmed = sorted(fr)[:-1] if len(fr) >= 4 else fr
        robust = (sum(r > 0 for r in fr) >= int(np.ceil(len(fr) * 0.6)) and np.mean(fr) > 0
                  and sum(r > 0 for r in trimmed) > len(trimmed) / 2 and np.mean(trimmed) > 0)
    return {"oos_roi": round(oos_roi, 2), "trades": len(rets), "avg_bps": round(avg_bps, 1),
            "bh": round(bh * 100, 2), "clears": clears, "robust": bool(robust), "ex_best": (round(float(np.mean(sorted(fr)[:-1])), 2) if len(fr) >= 4 else None)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    asset = (sys.argv[1] if len(sys.argv) > 1 else "BTC").upper()
    df = pd.read_csv(DATA / f"{asset.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    close = df["close"].values
    oi_chg = df["oi"].pct_change(H).values
    funding = df["funding"].values
    funding_chg = pd.Series(funding).diff(H).values
    flow = (df["buy_ratio"] - 0.5).values
    oi_accel = pd.Series(oi_chg).diff(H).values
    vol = df["close"].pct_change().rolling(H).std().values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oi_chg[i])]

    print(f"BTC DEEP-RESEARCH — new hypotheses under the gate (avg_bps must clear {RT_FEE_BPS:.0f} + walk-forward)\n")
    hyps = {
        "oi_chg (baseline)":   lambda i, t: positions_single(oi_chg, i, t, close),
        "funding_change":      lambda i, t: positions_single(funding_chg, i, t, close),
        "oi_acceleration":     lambda i, t: positions_single(oi_accel, i, t, close),
        "CONFLUENCE oi+funding": lambda i, t: positions_confluence(oi_chg, funding, i, t, close),
        "CONFLUENCE oi+flow":  lambda i, t: positions_confluence(oi_chg, flow, i, t, close),
        "oi @ HIGH-vol regime": lambda i, t: positions_volcond(oi_chg, vol, i, t, close, True),
        "oi @ LOW-vol regime":  lambda i, t: positions_volcond(oi_chg, vol, i, t, close, False),
    }
    hdr = f"{'hypothesis':24}{'OOS ROI':>9}{'b&h':>8}{'trades':>7}{'avg_bps':>9}{'clears':>8}{'robust':>8}"
    print(hdr)
    print("-" * len(hdr))
    winners = []
    for name, fn in hyps.items():
        r = evaluate(close, idx, fn)
        if not r:
            print(f"{name:24}{'<20 trades / n/a':>41}")
            continue
        cl = "✅" if r["clears"] else "—"
        rb = "✅" if r["robust"] else "—"
        print(f"{name:24}{r['oos_roi']:>+8.1f}%{r['bh']:>+7.1f}%{r['trades']:>7}{r['avg_bps']:>+9.1f}{cl:>8}{rb:>8}")
        if r["clears"] and r["robust"]:
            winners.append((name, r))
    print("-" * len(hdr))
    if winners:
        for name, r in winners:
            print(f"\n🟢 EDGE FOUND: {name} → {r['oos_roi']:+.1f}% OOS, avg {r['avg_bps']}bps, clears fee AND "
                  f"walk-forward-robust (ex-best {r['ex_best']}%). → CANDIDATE (scripts/60 confirms → on-chain).")
    else:
        print("\n⚪ NO robust fee-clearing edge in these hypotheses either. Honest: BTC's edge isn't in")
        print("   oi/funding/flow/vol transforms of the data we have. Next would need RICHER data BTC is")
        print("   efficient on — CVD/order-flow, liquidation feeds, cross-exchange basis, on-chain netflows —")
        print("   which we don't currently collect. We do NOT force a trade. (Honest deep-research result.)")


if __name__ == "__main__":
    main()
