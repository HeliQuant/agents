"""Edge RESOLVER — when an asset's edge fails, search a BOUNDED variant space for a tradeable config,
validated by the same honest gate. This IS propose->validate->dispose (the self-refactor pattern):
HeliQuant proposes variants (threshold / horizon / fee-model), DATA accepts only the ones that clear
the fee bar AND survive out-of-sample. Honest both ways — it resolves the asset, or proves the edge
is structurally too thin to trade net of fees.

BTC's known problem: OI-contrarian is real (+4.59% OOS) but its per-trade edge (+8.3 bps) < taker fee
(~11 bps round-trip) -> fee-eaten. Variants that could rescue a thin-but-real edge:
  * stricter threshold (decile 10/90 vs quintile 20/80) -> fewer, higher-conviction trades, bigger edge
  * different hold/lookback horizon (12 / 24 / 48h)
  * maker execution (~4 bps round-trip) instead of taker (~11) -> a contrarian limit order often gets
    filled at the extreme it fades (CAVEAT: maker fills are not guaranteed; this is an upper bound)

Run: python scripts/69_edge_resolver.py [BTC] [--source oi_chg24|funding|flow_imbalance]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
from firm.edge_lab import SIGNAL_SOURCES, _COL  # noqa: E402

FEES = {"taker": 0.00055, "maker": 0.0001}   # per side; maker assumes a limit fill (caveat)
THRESHOLDS = {"quintile": (20, 80), "decile": (10, 90)}
HORIZONS = [12, 24, 48]


def validate(close, sig, H, lo_q, hi_q, fee):
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
    plo, phi = np.nanpercentile(tr_sig, lo_q), np.nanpercentile(tr_sig, hi_q)
    eq, trades, rets = 1.0, 0, []
    for i in te:
        s = sig[i]
        if s >= phi:
            pos = -1 if contrarian else 1
        elif s <= plo:
            pos = 1 if contrarian else -1
        else:
            continue
        net = pos * (close[i + H] / close[i] - 1) - 2 * fee
        eq *= 1 + net
        trades += 1
        rets.append(net)
    if trades < 20:
        return None
    bh = close[te[-1] + H] / close[te[0]] - 1
    avg_bps = float(np.mean(rets) * 1e4)
    return {"oos_roi": (eq - 1) * 100, "trades": trades, "avg_bps": round(avg_bps, 1),
            "bh": bh * 100, "dir": "contrarian" if contrarian else "momentum"}


def wf_param(close, sig, H, lo_q, hi_q, fee, k=5):
    """Anchored walk-forward for a (threshold, horizon, fee) variant — so a 'resolve' must be ROBUST,
    not a lucky grid cell. Returns consistency (majority folds +ve AND positive after dropping best)."""
    n = len(close)
    idx = [i for i in range(H, n - H, H) if not np.isnan(sig[i])]
    if len(idx) < 80:
        return {"folds": 0, "consistent": False}
    init = int(len(idx) * 0.4)
    folds = [list(f) for f in np.array_split(np.array(idx[init:]), k)]
    fr, te = [], init
    for f in folds:
        if not f:
            continue
        train = idx[:te]
        ts = np.array([sig[i] for i in train])
        trr = np.array([close[i + H] / close[i] - 1 for i in train])
        contr = pd.Series(ts).corr(pd.Series(trr), method="spearman") < 0
        plo, phi = np.nanpercentile(ts, lo_q), np.nanpercentile(ts, hi_q)
        eq, cnt = 1.0, 0
        for i in f:
            s = sig[i]
            if s >= phi:
                pos = -1 if contr else 1
            elif s <= plo:
                pos = 1 if contr else -1
            else:
                continue
            eq *= 1 + (pos * (close[i + H] / close[i] - 1) - 2 * fee)
            cnt += 1
        if cnt > 0:
            fr.append(round((eq - 1) * 100, 2))
        te += len(f)
    if len(fr) < 3:
        return {"folds": len(fr), "consistent": False}
    posn = sum(r > 0 for r in fr)
    trimmed = sorted(fr)[:-1] if len(fr) >= 4 else fr
    consistent = (posn >= int(np.ceil(len(fr) * 0.6)) and float(np.mean(fr)) > 0
                  and sum(r > 0 for r in trimmed) > len(trimmed) / 2 and float(np.mean(trimmed)) > 0)
    return {"folds": len(fr), "positive": posn, "fold_rois": fr,
            "ex_best": round(float(np.mean(trimmed)), 2), "consistent": bool(consistent)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("asset", nargs="?", default="BTC")
    ap.add_argument("--source", default="oi_chg24")
    args = ap.parse_args()
    asset, src = args.asset.upper(), args.source

    df = pd.read_csv(DATA / f"{asset.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    if _COL[src] not in df.columns:
        print(f"{asset} has no column for {src}")
        return
    c = df["close"].values

    print(f"EDGE RESOLVER — {asset} · source {src}  (propose bounded variants → DATA validates)\n")
    hdr = f"{'threshold':10}{'horizon':>8}{'fee':>7}{'feebar':>8}{'OOS ROI':>10}{'trades':>7}{'avg_bps':>9}{'clears?':>9}"
    print(hdr)
    print("-" * len(hdr))
    winners = []
    for tname, (lo, hi) in THRESHOLDS.items():
        for H in HORIZONS:
            sig = df[_COL[src]].pct_change(H).values if src in ("oi_chg24", "price_mom24") else df[_COL[src]].values
            for fname, fee in FEES.items():
                r = validate(c, sig, H, lo, hi, fee)
                if not r:
                    continue
                feebar = 2 * fee * 1e4
                clears = r["avg_bps"] > feebar and r["oos_roi"] > 0 and r["oos_roi"] > r["bh"]
                tag = "✅ CLEARS" if clears else "—"
                print(f"{tname:10}{H:>7}h{fname:>7}{feebar:>7.0f}b{r['oos_roi']:>+9.1f}%{r['trades']:>7}"
                      f"{r['avg_bps']:>+9.1f}{tag:>9}")
                if clears:
                    winners.append({"threshold": tname, "horizon": H, "fee": fname, **r})
    print("-" * len(hdr))
    if not winners:
        print(f"\n⚪ VERDICT: NO variant clears the bar — {asset}'s {src} edge is structurally too thin to")
        print("   trade net of realistic fees. Honest outcome: HeliQuant abstains on it (no forced trade).")
        return
    takers = [w for w in winners if w["fee"] == "taker"]
    if takers:
        bt = max(takers, key=lambda w: w["oos_roi"])
        H = bt["horizon"]
        lo, hi = THRESHOLDS[bt["threshold"]]
        sig = df[_COL[src]].pct_change(H).values if src in ("oi_chg24", "price_mom24") else df[_COL[src]].values
        wf = wf_param(c, sig, H, lo, hi, FEES["taker"])
        if wf.get("consistent"):
            print(f"\n🟢 RESOLVED + ROBUST (taker, no fill assumption): {bt['threshold']}/{bt['horizon']}h → "
                  f"{bt['oos_roi']:+.1f}% OOS, avg {bt['avg_bps']}bps; walk-forward {wf['positive']}/{wf['folds']} "
                  f"folds, ex-best {wf['ex_best']}% {wf['fold_rois']}.")
            print("   → earns CANDIDATE (scripts/60 confirms over NEW data → graduate → anchor on-chain). HONEST resolve.")
        else:
            print(f"\n🟡 CLEARS fee on 1-split ({bt['threshold']}/{bt['horizon']}h, {bt['oos_roi']:+.1f}%, avg {bt['avg_bps']}bps) "
                  f"but NOT walk-forward-robust (folds {wf.get('fold_rois')}, ex-best {wf.get('ex_best')}%).")
            print("   → likely a grid-search artifact. HELD, not registered. Honest: the fee-clearing config doesn't survive folds.")
    else:
        best = max(winners, key=lambda w: w["oos_roi"])
        print(f"\n🟡 Clears ONLY with MAKER fees: {best['threshold']}/{best['horizon']}h → {best['oos_roi']:+.1f}% "
              f"(avg {best['avg_bps']}bps). CAVEAT: assumes limit fills at the faded extreme — not guaranteed; needs live fill-rate proof.")


if __name__ == "__main__":
    main()
