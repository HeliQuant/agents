"""Walk-forward validation — the ANTI-overfit multi-asset tuner.

scripts/12 grid-searched the best config on ONE window and reported it. That
overfits — gains vanish on fresh data.

This does it honestly:
  1. Split each asset chronologically: TRAIN (first 60%) / TEST (last 40%).
  2. Grid-search the best config on TRAIN only.
  3. Lock it. Evaluate ONCE on TEST (out-of-sample, never seen during tuning).
  4. Report IS vs OOS side by side. Asset PASSES only if OOS ROI > 0 with
     >= MIN_TEST_TRADES trades.

Usage:
    python scripts/13_walkforward.py BTC
Reads: data/{ticker}_features.csv  (run multi_asset.py first)
"""

from __future__ import annotations

import importlib.util
import sys
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.strategies import Action, MeanReversionStrategy  # noqa: E402
from firm.strategies.base import Position  # noqa: E402
from firm.agents.strategy_lifecycle import assess_momentum_activation  # noqa: E402

spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
multi_asset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi_asset)

INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
LOOKAHEAD = 8
SL_MULT = 1.0
REWARD_MULT = 1.46
TRAIN_FRAC = 0.60
MIN_TRAIN_TRADES = 5
MIN_TEST_TRADES = 4


def simulate(i, df, direction, entry, atr, equity):
    sl_dist = atr * SL_MULT
    notional = min((equity * RISK_PER_TRADE) * (entry / sl_dist), equity * 0.95)
    win = df.iloc[i + 1 : i + LOOKAHEAD + 1]
    if win.empty:
        return 0.0
    if direction > 0:
        tp, sl = entry + atr * REWARD_MULT, entry - sl_dist
        hit_tp, hit_sl = win[win["high"] >= tp], win[win["low"] <= sl]
    else:
        tp, sl = entry - atr * REWARD_MULT, entry + sl_dist
        hit_tp, hit_sl = win[win["low"] <= tp], win[win["high"] >= sl]
    if not hit_tp.empty and not hit_sl.empty:
        exit_p = tp if hit_tp.index[0] < hit_sl.index[0] else sl
    elif not hit_tp.empty:
        exit_p = tp
    elif not hit_sl.empty:
        exit_p = sl
    else:
        exit_p = float(win["close"].iloc[-1])
    pnl_pct = (exit_p - entry) / entry if direction > 0 else (entry - exit_p) / entry
    return notional * pnl_pct - notional * SWAP_FEE * 2


def run(df, proba, labels, adx_th, vol_th, *, oversold, overbought, mr_tp, flat, conf):
    mr = MeanReversionStrategy(oversold_rsi=oversold, overbought_rsi=overbought, tp_atr_mult=mr_tp)
    equity, wins, n = INITIAL_EQUITY, 0, 0
    i, cooldown = 30, -1
    while i < len(df) - LOOKAHEAD:
        if i <= cooldown:
            i += 1
            continue
        if float(proba[i].max()) < conf:
            i += 1
            continue
        row = df.iloc[i]
        cur = multi_asset.detect_current(row, adx_th, vol_th)
        fwd = labels[int(proba[i].argmax())]
        chosen = "High_Volatility" if cur == "High_Volatility" else (cur if cur == fwd else fwd)
        if chosen != "Ranging":
            i += 1
            continue
        life = assess_momentum_activation(df.iloc[max(0, i - 60):i + 1], return_strong_pct=flat)
        if not life.mean_reversion_active:
            i += 1
            continue
        dec = mr.evaluate(df.iloc[max(0, i - 30):i + 1], Position())
        if dec.action not in (Action.BUY, Action.SELL):
            i += 1
            continue
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl = simulate(i, df, 1 if dec.action == Action.BUY else -1, float(row["close"]), atr, equity)
        equity += pnl
        wins += pnl > 0
        n += 1
        cooldown = i + LOOKAHEAD
        i += 1
    roi = (equity / INITIAL_EQUITY - 1) * 100
    return {"roi": roi, "win": (wins / n * 100) if n else 0.0, "trades": n}


def main():
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "BTC").upper()
    fp = ROOT / "data" / f"{ticker.lower()}_features.csv"
    if not fp.exists():
        print(f"No {fp}. Run: python scripts/multi_asset.py <coingecko_id> {ticker}")
        return
    df = pd.read_csv(fp).dropna().reset_index(drop=True)
    split = int(len(df) * TRAIN_FRAC)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)

    bundle = multi_asset.train_classifier(train)
    feats, labels = bundle["features"], bundle["labels"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    proba_tr = bundle["model"].predict_proba(train[feats])
    proba_te = bundle["model"].predict_proba(test[feats])

    print(f"=== {ticker} walk-forward ===  train={len(train)} bars / test={len(test)} bars")

    grid = product(
        [20.0, 25.0, 30.0], [70.0, 75.0, 80.0], [1.5, 1.8, 2.2], [2.0, 3.0, 5.0], [0.60, 0.65],
    )
    best, best_is = None, -1e9
    for ov, ob, tp, flat, conf in grid:
        r = run(train, proba_tr, labels, adx_th, vol_th,
                oversold=ov, overbought=ob, mr_tp=tp, flat=flat, conf=conf)
        if r["trades"] >= MIN_TRAIN_TRADES and r["roi"] > best_is:
            best_is, best = r["roi"], dict(ov=ov, ob=ob, tp=tp, flat=flat, conf=conf, **r)

    if best is None:
        print("No config cleared the in-sample trade floor. Asset too thin for this strategy.")
        return

    oos = run(test, proba_te, labels, adx_th, vol_th,
              oversold=best["ov"], overbought=best["ob"], mr_tp=best["tp"],
              flat=best["flat"], conf=best["conf"])

    print(f"Best IS config: RSI {best['ov']:.0f}/{best['ob']:.0f}  tp {best['tp']:.1f}  "
          f"flat {best['flat']:.0f}%  conf {best['conf']:.2f}")
    print(f"  IN-SAMPLE : ROI {best['roi']:+.2f}%  win {best['win']:.1f}%  ({best['trades']} trades)")
    print(f"  OUT-SAMPLE: ROI {oos['roi']:+.2f}%  win {oos['win']:.1f}%  ({oos['trades']} trades)")
    if oos["trades"] >= MIN_TEST_TRADES and oos["roi"] > 0:
        print(f"  PASS — {ticker} shows REAL out-of-sample edge.")
    elif oos["trades"] < MIN_TEST_TRADES:
        print(f"  INCONCLUSIVE — only {oos['trades']} OOS trades (need {MIN_TEST_TRADES}).")
    else:
        print(f"  OVERFIT — IS good but OOS {oos['roi']:+.2f}%. Do NOT claim {ticker} profitable.")


if __name__ == "__main__":
    main()
