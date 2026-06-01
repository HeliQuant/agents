"""Walk-forward for the TREND-FOLLOWING (momentum) hypothesis.

scripts/18 tested mean-reversion on Ranging regimes -> 0/4 OOS. But the data shows
MNT TRENDED HARD DOWN (-37% over the OOS window), so mean-reversion was the wrong
tool (it fights trends). A trend-follower that SHORTS the downtrend is the
well-motivated next hypothesis the data itself points to.

Same honest protocol as scripts/18: grid-search the momentum config on the first
60% (train), LOCK it, evaluate ONCE on the unseen last 40% (out-of-sample). No
tuning on the OOS window.

Pyth-sourced tokens (MNT, mETH) carry no volume, so the momentum volume gate is
disabled (volume_min_ratio=0) — honest: there is no volume to confirm with.

Usage: python scripts/19_walkforward_trend.py MNT METH CMETH FBTC
Writes: data/mantle_trend_walkforward.json  (single source of truth)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.strategies import Action, MomentumStrategy  # noqa: E402
from firm.strategies.base import Position  # noqa: E402

spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
multi_asset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi_asset)

INITIAL, RISK, FEE = 1_000.0, 0.01, 0.0010
LOOKAHEAD, SL_MULT = 8, 1.0
TRAIN_FRAC = 0.60
MIN_TRAIN, MIN_TEST = 5, 4
TREND = ("Trending_Up", "Trending_Down")


def simulate(i, df, direction, entry, atr, equity, reward):
    sl = atr * SL_MULT
    notional = min((equity * RISK) * (entry / sl), equity * 0.95)
    win = df.iloc[i + 1: i + LOOKAHEAD + 1]
    if win.empty:
        return 0.0
    if direction > 0:
        tp, sld = entry + atr * reward, entry - sl
        ht, hs = win[win["high"] >= tp], win[win["low"] <= sld]
    else:
        tp, sld = entry - atr * reward, entry + sl
        ht, hs = win[win["low"] <= tp], win[win["high"] >= sld]
    if not ht.empty and not hs.empty:
        ex = tp if ht.index[0] < hs.index[0] else sld
    elif not ht.empty:
        ex = tp
    elif not hs.empty:
        ex = sld
    else:
        ex = float(win["close"].iloc[-1])
    pct = (ex - entry) / entry if direction > 0 else (entry - ex) / entry
    return notional * pct - notional * FEE * 2


def run(df, proba, labels, adx_th, vol_th, *, adx_min, bwin, tp, conf):
    mo = MomentumStrategy(
        adx_min=adx_min, breakout_window=bwin, volume_min_ratio=0.0, tp_atr_mult=tp
    )
    eq, wins, n, i, cd = INITIAL, 0, 0, 30, -1
    win_lookback = max(bwin + 2, 30)
    while i < len(df) - LOOKAHEAD:
        if i <= cd:
            i += 1
            continue
        if float(proba[i].max()) < conf:
            i += 1
            continue
        row = df.iloc[i]
        cur = multi_asset.detect_current(row, adx_th, vol_th)
        fwd = labels[int(proba[i].argmax())]
        chosen = "High_Volatility" if cur == "High_Volatility" else (cur if cur == fwd else fwd)
        if chosen not in TREND:
            i += 1
            continue
        dec = mo.evaluate(df.iloc[max(0, i - win_lookback): i + 1], Position())
        if dec.action not in (Action.BUY, Action.SELL):
            i += 1
            continue
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl = simulate(i, df, 1 if dec.action == Action.BUY else -1, float(row["close"]), atr, eq, tp)
        eq += pnl
        wins += 1 if pnl > 0 else 0
        n += 1
        cd = i + LOOKAHEAD
        i += 1
    return {"roi": round((eq / INITIAL - 1) * 100, 2), "win": round((wins / n * 100) if n else 0.0, 1), "trades": n}


def validate(ticker: str) -> dict:
    fp = ROOT / "data" / f"{ticker.lower()}_features.csv"
    if not fp.exists():
        return {"ticker": ticker, "verdict": "NO_DATA"}
    df = pd.read_csv(fp).dropna().reset_index(drop=True)
    split = int(len(df) * TRAIN_FRAC)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)
    bundle = multi_asset.train_classifier(train)
    labels = bundle["labels"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    ptr = bundle["model"].predict_proba(train[bundle["features"]])
    pte = bundle["model"].predict_proba(test[bundle["features"]])

    best, best_is = None, -1e9
    for adx_min, bwin, tp, conf in product([20.0, 30.0, 40.0], [10, 15, 20], [1.5, 2.0, 2.5], [0.60, 0.65]):
        r = run(train, ptr, labels, adx_th, vol_th, adx_min=adx_min, bwin=bwin, tp=tp, conf=conf)
        if r["trades"] >= MIN_TRAIN and r["roi"] > best_is:
            best_is, best = r["roi"], dict(adx_min=adx_min, bwin=bwin, tp=tp, conf=conf, **r)
    if best is None:
        return {"ticker": ticker, "verdict": "THIN"}
    oos = run(test, pte, labels, adx_th, vol_th, adx_min=best["adx_min"], bwin=best["bwin"], tp=best["tp"], conf=best["conf"])
    verdict = "INCONCLUSIVE" if oos["trades"] < MIN_TEST else ("PASS" if oos["roi"] > 0 else "OVERFIT")
    return {
        "ticker": ticker, "verdict": verdict,
        "is_roi": best["roi"], "is_win": best["win"], "is_trades": best["trades"],
        "oos_roi": oos["roi"], "oos_win": oos["win"], "oos_trades": oos["trades"],
        "config": {"adx_min": best["adx_min"], "breakout_window": best["bwin"], "tp_atr": best["tp"], "conf": best["conf"]},
        "strategy": "momentum_trend_following (volume gate off — Pyth no-volume)",
    }


def main():
    tickers = [t.upper() for t in sys.argv[1:]] or ["MNT", "METH", "CMETH", "FBTC"]
    out = {}
    for t in tickers:
        print(f"validating {t} (trend-following) ...", end=" ", flush=True)
        res = validate(t)
        out[t] = res
        v = res["verdict"]
        if v == "PASS":
            print(f"PASS  OOS {res['oos_roi']:+.2f}%  win {res['oos_win']:.0f}%  ({res['oos_trades']} trd)")
        elif v in ("OVERFIT", "INCONCLUSIVE"):
            print(f"{v}  OOS {res.get('oos_roi')}%  ({res.get('oos_trades')} trd)")
        else:
            print(v)
    path = ROOT / "data" / "mantle_trend_walkforward.json"
    path.write_text(json.dumps(out, indent=2))
    passed = [t for t, r in out.items() if r["verdict"] == "PASS"]
    print(f"\nPASSED ({len(passed)}/{len(tickers)}): {', '.join(passed)}")
    print(f"saved -> {path}")


if __name__ == "__main__":
    main()
