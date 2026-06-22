"""Re-test the trend-following edge on REAL exchange data (now WITH volume) — does volume help?

Pyth had no volume -> we disabled the momentum volume gate -> trend-following failed OOS. The exchange
feed gives real volume, so now we can test honestly: classifier features include real volume, AND the
momentum entry can require volume confirmation (volume_min_ratio). Walk-forward 60/40, grid-search
on train, lock, test once OOS — same honest protocol as scripts/19. Compares vol-gate OFF vs ON.

Run: python scripts/36_walkforward_bybit_volume.py
"""
from __future__ import annotations

import importlib.util
import sys
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.strategies import Action, MomentumStrategy  # noqa: E402
from firm.strategies.base import Position  # noqa: E402

wf = importlib.util.module_from_spec(importlib.util.spec_from_file_location("wf19", ROOT / "scripts" / "19_walkforward_trend.py"))
importlib.util.spec_from_file_location("wf19", ROOT / "scripts" / "19_walkforward_trend.py").loader.exec_module(wf)
ma = importlib.util.module_from_spec(importlib.util.spec_from_file_location("ma", ROOT / "scripts" / "multi_asset.py"))
importlib.util.spec_from_file_location("ma", ROOT / "scripts" / "multi_asset.py").loader.exec_module(ma)

TREND = ("Trending_Up", "Trending_Down")
GRID = list(product([30.0, 40.0], [10, 20], [2.0, 2.5], [0.65]))


def run(df, proba, labels, adx_th, vol_th, adx_min, bwin, tp, conf, vol_min):
    mo = MomentumStrategy(adx_min=adx_min, breakout_window=bwin, volume_min_ratio=vol_min, tp_atr_mult=tp)
    eq, wins, n, i, cd = wf.INITIAL, 0, 0, 30, -1
    win_lb = max(bwin + 2, 30)
    while i < len(df) - wf.LOOKAHEAD:
        if i <= cd:
            i += 1; continue
        if float(proba[i].max()) < conf:
            i += 1; continue
        row = df.iloc[i]
        cur = ma.detect_current(row, adx_th, vol_th)
        fwd = labels[int(proba[i].argmax())]
        chosen = "High_Volatility" if cur == "High_Volatility" else (cur if cur == fwd else fwd)
        if chosen not in TREND:
            i += 1; continue
        dec = mo.evaluate(df.iloc[max(0, i - win_lb): i + 1], Position())
        if dec.action not in (Action.BUY, Action.SELL):
            i += 1; continue
        atr = float(row["atr"])
        if atr <= 0:
            i += 1; continue
        pnl = wf.simulate(i, df, 1 if dec.action == Action.BUY else -1, float(row["close"]), atr, eq, tp)
        eq += pnl; wins += 1 if pnl > 0 else 0; n += 1; cd = i + wf.LOOKAHEAD; i += 1
    return {"roi": round((eq / wf.INITIAL - 1) * 100, 2), "win": round((wins / n * 100) if n else 0.0, 1), "trades": n}


def validate(ticker, vol_min):
    fp = ROOT / "data" / f"{ticker.lower()}_perp_hourly.csv"
    if not fp.exists():
        return None
    df = ma.add_features(pd.read_csv(fp)).dropna().reset_index(drop=True)
    split = int(len(df) * 0.6)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)
    bundle = ma.train_classifier(train)
    labels, feats = bundle["labels"], bundle["features"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    ptr, pte = bundle["model"].predict_proba(train[feats]), bundle["model"].predict_proba(test[feats])
    best, best_is = None, -1e9
    for adx_min, bwin, tp, conf in GRID:
        r = run(train, ptr, labels, adx_th, vol_th, adx_min, bwin, tp, conf, vol_min)
        if r["trades"] >= 5 and r["roi"] > best_is:
            best_is, best = r["roi"], (adx_min, bwin, tp, conf)
    if not best:
        return {"verdict": "THIN(train)"}
    oos = run(test, pte, labels, adx_th, vol_th, *best, vol_min)
    v = "INCONCLUSIVE" if oos["trades"] < 4 else ("PASS" if oos["roi"] > 0 else "OVERFIT")
    return {"verdict": v, **oos, "acc": round(bundle["test_accuracy"] * 100, 1)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("=== Trend-following on REAL exchange data (with volume) — vol-gate OFF vs ON ===\n")
    print(f"{'asset':6} {'vol_gate':>8} {'verdict':>13} {'OOS ROI':>9} {'win':>6} {'trades':>7} {'clf_acc':>8}")
    for t in ["MNT", "BTC", "ETH", "SOL"]:
        for vol_min in (0.0, 1.1):
            r = validate(t, vol_min)
            if not r:
                print(f"{t:6} NO DATA"); break
            if "roi" in r:
                print(f"{t:6} {('OFF' if vol_min == 0 else 'ON'):>8} {r['verdict']:>13} "
                      f"{r['oos_roi'] if 'oos_roi' in r else r['roi']:+9.2f} {r.get('win', 0):6.1f} {r.get('trades', 0):7} {r.get('acc', '?'):>7}%")
            else:
                print(f"{t:6} {('OFF' if vol_min == 0 else 'ON'):>8} {r.get('verdict'):>13}")


if __name__ == "__main__":
    main()
