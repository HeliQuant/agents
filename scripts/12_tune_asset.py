"""Per-asset hyperparameter tuner.

For one asset: load cached features, train its regime classifier, then grid-search
a small parameter space. Reports the best config by ROI subject to a minimum
trade-count floor (so we don't "win" with 1 lucky trade).

Usage:
    python scripts/12_tune_asset.py BTC
    python scripts/12_tune_asset.py METH

Reads:  data/{ticker}_features.csv  (created by multi_asset.py)
Prints: best AssetConfig params to paste into firm/asset_configs.py
"""

from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.strategies import (  # noqa: E402
    Action,
    DefensiveStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
)
from firm.strategies.base import Position  # noqa: E402
from firm.agents.strategy_lifecycle import assess_momentum_activation, LifecycleStatus  # noqa: E402

# Reuse the multi_asset pipeline's feature + training helpers
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "multi_asset", ROOT / "scripts" / "multi_asset.py"
)
multi_asset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi_asset)

INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
LOOKAHEAD = 8
SL_MULT = 1.0
REWARD_MULT = 1.46
MIN_TRADES = 6  # floor: ignore configs that fire fewer than this


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


def run_config(df, bundle, *, oversold, overbought, mr_tp, flat_pct, mom_enabled, mom_adx):
    proba = bundle["model"].predict_proba(df[bundle["features"]])
    labels = bundle["labels"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    mr = MeanReversionStrategy(oversold_rsi=oversold, overbought_rsi=overbought, tp_atr_mult=mr_tp)
    mo = MomentumStrategy(adx_min=max(mom_adx, 25.0))
    df_ = df
    equity, wins, losses, n = INITIAL_EQUITY, 0, 0, 0
    i, cooldown = 30, -1
    while i < len(df_) - LOOKAHEAD:
        if i <= cooldown:
            i += 1
            continue
        row = df_.iloc[i]
        idx = int(proba[i].argmax())
        if float(proba[i][idx]) < bundle.get("forward_conf_threshold", 0.65):
            i += 1
            continue
        cur = multi_asset.detect_current(row, adx_th, vol_th)
        fwd = labels[idx]
        chosen = "High_Volatility" if cur == "High_Volatility" else (cur if cur == fwd else fwd)
        life = assess_momentum_activation(df_.iloc[max(0, i - 60):i + 1], return_strong_pct=flat_pct)
        strat = None
        if chosen in ("Trending_Up", "Trending_Down"):
            strat = mo if (mom_enabled and life.momentum_status != LifecycleStatus.PENDING) else None
        elif chosen == "Ranging":
            strat = mr if life.mean_reversion_active else None
        if strat is None:
            i += 1
            continue
        dec = strat.evaluate(df_.iloc[max(0, i - 30):i + 1], Position())
        if dec.action not in (Action.BUY, Action.SELL):
            i += 1
            continue
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl = simulate(i, df_, 1 if dec.action == Action.BUY else -1, float(row["close"]), atr, equity)
        equity += pnl
        wins += pnl > 0
        losses += pnl <= 0
        n += 1
        cooldown = i + LOOKAHEAD
        i += 1
    roi = (equity / INITIAL_EQUITY - 1) * 100
    wr = (wins / n * 100) if n else 0.0
    return {"roi": roi, "win_rate": wr, "trades": n}


def main():
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC"
    feat_path = ROOT / "data" / f"{ticker.lower()}_features.csv"
    if not feat_path.exists():
        print(f"No features at {feat_path}. Run: python scripts/multi_asset.py <id> {ticker}")
        return
    df = pd.read_csv(feat_path).dropna().reset_index(drop=True)
    bundle = multi_asset.train_classifier(df)
    print(f"Tuning {ticker}: {len(df)} bars, classifier test acc {bundle['test_accuracy']*100:.1f}%\n")

    grid = product(
        [20.0, 25.0, 30.0],          # oversold
        [70.0, 75.0, 80.0],          # overbought
        [1.5, 1.8, 2.2],             # mr tp atr mult
        [2.0, 3.0, 5.0],             # flat threshold pct
        [False, True],               # momentum enabled
        [35.0, 45.0],                # momentum adx min
    )
    results = []
    for ov, ob, mrtp, flat, mom, madx in grid:
        r = run_config(df, bundle, oversold=ov, overbought=ob, mr_tp=mrtp,
                       flat_pct=flat, mom_enabled=mom, mom_adx=madx)
        if r["trades"] >= MIN_TRADES:
            r.update(oversold=ov, overbought=ob, mr_tp=mrtp, flat=flat, mom=mom, madx=madx)
            results.append(r)

    if not results:
        print(f"No config fired >= {MIN_TRADES} trades. {ticker} likely too trending for this strategy family.")
        return
    results.sort(key=lambda x: (x["roi"], x["win_rate"]), reverse=True)
    print(f"Top 5 configs for {ticker} (min {MIN_TRADES} trades):\n")
    print(f"{'ROI%':>7} {'Win%':>6} {'Trd':>4}  oversold/overbought tp_mult flat% mom adx")
    for r in results[:5]:
        print(f"{r['roi']:>7.2f} {r['win_rate']:>6.1f} {r['trades']:>4}  "
              f"{r['oversold']:.0f}/{r['overbought']:.0f}  {r['mr_tp']:.1f}   "
              f"{r['flat']:.0f}   {str(r['mom']):5s} {r['madx']:.0f}")
    best = results[0]
    print(f"\nBEST {ticker}: ROI {best['roi']:+.2f}%, win {best['win_rate']:.1f}%, {best['trades']} trades")


if __name__ == "__main__":
    main()
