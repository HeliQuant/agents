"""Does gating OI-contrarian by REGIME / volatility rescue it to robust? (pattern #3 hypothesis)

Hypothesis from rolling-WF folds: contrarian-OI works in MODERATE vol (mean-reverting), fails in
violent crash (High_Volatility) and dead-flat. So gate it. To avoid overfitting (reverse-engineering
the 4 losing folds), we PRE-COMMIT 4 principled gates and report ALL — no cherry-picking:
  * ungated   : baseline (sanity-checks vs script 41)
  * ranging   : trade only when regime == Ranging (contrarian belongs in ranging markets — theory)
  * not_HV    : trade only when regime != High_Volatility (skip violent crashes)
  * vol_band  : trade only when trailing 7d realized vol in train's [p30,p70] (literal "moderate vol")
All thresholds (regime adx/vol, OI quintiles, direction, vol band) derived from TRAIN ONLY per fold.
A gate is promising ONLY if it lifts folds-positive across BOTH MNT and BTC. Else: gating doesn't save it.

Run: python scripts/42_oi_regime_gated.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("ma", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)

FEE = 0.00055
H = 24
NF = 4
GATES = ["ungated", "ranging", "not_HV", "vol_band"]


def compound(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return (eq - 1) * 100


def prep(ticker):
    h = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_perp_hourly.csv")
    feat = ma.add_features(h)
    pos = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv")[["timestamp", "oi"]]
    df = feat.merge(pos, on="timestamp", how="left").sort_values("timestamp").reset_index(drop=True)
    df["oi_chg24"] = df["oi"].pct_change(H)
    df["rvol"] = df["return_1"].rolling(168).std()
    return df


def regime_vec(df, adx_th, vol_th):
    hv = df["volatility_10"].values > vol_th
    tr = df["adx"].values > adx_th
    up = df["ema_slope_5"].values > 0
    out = np.where(hv, "HV", np.where(tr, np.where(up, "TU", "TD"), "RG"))
    return out


def take(gate, rg, rvol, vlo, vhi):
    if gate == "ranging":
        return rg == "RG"
    if gate == "not_HV":
        return rg != "HV"
    if gate == "vol_band":
        return vlo <= rvol <= vhi
    return True  # ungated


def walkforward(ticker):
    df = prep(ticker)
    c = df["close"].values
    oichg = df["oi_chg24"].values
    rvolv = df["rvol"].values
    n = len(df)
    idx = [i for i in range(168, n - H, H) if not np.isnan(oichg[i]) and not np.isnan(rvolv[i])]
    N = len(idx)
    init = int(N * 0.4)
    fold = (N - init) // NF
    res = {g: [] for g in GATES}
    for f in range(NF):
        ts = init + f * fold
        teEnd = (init + (f + 1) * fold) if f < NF - 1 else N
        train, test = idx[:ts], idx[ts:teEnd]
        if len(test) < 8:
            continue
        tbar = test[0]
        trbars = df.iloc[30:tbar]
        adx_th = float(trbars["adx"].quantile(0.60))
        vol_th = float(trbars["volatility_10"].quantile(0.85))
        rg = regime_vec(df, adx_th, vol_th)
        tr_sig = np.array([oichg[i] for i in train])
        ic = pd.Series(tr_sig).corr(pd.Series([c[i + H] / c[i] - 1 for i in train]), method="spearman")
        contrarian = ic < 0
        p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
        tr_rvol = np.array([rvolv[i] for i in train])
        vlo, vhi = np.nanpercentile(tr_rvol, 30), np.nanpercentile(tr_rvol, 70)
        for g in GATES:
            net = []
            for i in test:
                s = oichg[i]
                if s >= p80:
                    pos = -1 if contrarian else 1
                elif s <= p20:
                    pos = 1 if contrarian else -1
                else:
                    continue
                if not take(g, rg[i], rvolv[i], vlo, vhi):
                    continue
                net.append(pos * (c[i + H] / c[i] - 1) - 2 * FEE)
            res[g].append((compound(net), len(net)))
    return res


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    for t in ["MNT", "BTC"]:
        res = walkforward(t)
        print(f"\n================= {t} =================")
        print(f"{'gate':10} {'fold1':>9} {'fold2':>9} {'fold3':>9} {'fold4':>9}   {'total':>9} {'folds+':>7} {'trades':>7}")
        for g in GATES:
            folds = res[g]
            rois = [r for r, _ in folds]
            trades = sum(nt for _, nt in folds)
            total = compound([r / 100 for r in rois])
            pos = sum(1 for r in rois if r > 0)
            cells = " ".join(f"{r:+8.2f}%" for r in rois)
            print(f"{g:10} {cells}   {total:+8.2f}% {pos:5}/{len(rois)} {trades:7}")
    print("\nRead: a gate is promising ONLY if folds+ improves on BOTH MNT & BTC vs ungated, with enough trades.")
    print("If ranging/not_HV/vol_band don't consistently lift folds+ -> gating does NOT rescue it (honest).")


if __name__ == "__main__":
    main()
