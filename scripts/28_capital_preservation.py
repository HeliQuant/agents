"""Capital-preservation overlay — give the 85-91% regime engine a job it might win.

Hypothesis: can't profitably TIME entries (proven: paper-sim loses), but maybe the regime
prediction can AVOID drawdowns. Rule (long-or-flat, no shorting):
  FLAT (cash) when the engine CONFIDENTLY predicts a defensive regime (Trending_Down /
  High_Volatility); otherwise HOLD long.

A naive bar-by-bar version churns (the 4h regime flips often) and dies to transaction costs.
So we add HYSTERESIS (min-hold after each switch) and report BOTH:
  - NO-COST  : does the timing itself preserve capital (lower maxDD / better return)?
  - WITH-COST: is it tradeable after realistic switch costs?
plus the switch count (turnover). OOS only (train 60% / test 40%), real prices. HONEST: this is
risk management, not alpha — success = lower drawdown, even at lower return.

Run: python scripts/28_capital_preservation.py [MNT BTC ETH SOL ...]
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

GATE = 0.65
DEFENSIVE = ("Trending_Down", "High_Volatility")
SWITCH_COST = 0.0010
MIN_HOLD = 72          # bars (~3 days) to hold an exposure before another switch (cuts churn)
ANN = 24 * 365


def _maxdd(curve) -> float:
    arr = np.array(curve)
    peak = np.maximum.accumulate(arr)
    return float(((peak - arr) / peak).max() * 100)


def run(ticker: str) -> dict:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    split = int(len(df) * 0.60)
    train_df, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)
    bundle = ma.train_classifier(train_df)
    labels = bundle["labels"]
    proba = bundle["model"].predict_proba(test[ma.FEATURES])
    pred = [labels[int(proba[i].argmax())] for i in range(len(test))]
    conf = proba.max(axis=1)
    ret = test["close"].pct_change().fillna(0.0).values

    eq_nc, eq_c, eq_b = 1.0, 1.0, 1.0
    cnc, cb = [1.0], [1.0]
    state, last_switch, switches, in_mkt = 1, -MIN_HOLD, 0, 0
    for i in range(len(test) - 1):
        want = 0 if (conf[i] >= GATE and pred[i] in DEFENSIVE) else 1
        if want != state and (i - last_switch) >= MIN_HOLD:
            state = want
            last_switch = i
            switches += 1
            eq_c *= (1 - SWITCH_COST)
        r = ret[i + 1]
        eq_nc *= (1 + state * r)
        eq_c *= (1 + state * r)
        eq_b *= (1 + r)
        in_mkt += state
        cnc.append(eq_nc)
        cb.append(eq_b)
    return {
        "ticker": ticker, "switches": switches, "pct_in_market": round(in_mkt / (len(test) - 1) * 100, 1),
        "ret_nocost_pct": round((eq_nc - 1) * 100, 2), "ret_cost_pct": round((eq_c - 1) * 100, 2),
        "ret_buyhold_pct": round((eq_b - 1) * 100, 2),
        "maxdd_strat_pct": round(_maxdd(cnc), 2), "maxdd_buyhold_pct": round(_maxdd(cb), 2),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [t.upper() for t in sys.argv[1:]] or ["MNT", "BTC", "ETH", "SOL"]
    print(f"=== Regime-aware capital-preservation overlay vs buy-and-hold (OOS 40%, min-hold {MIN_HOLD}b) ===")
    print("FLAT when engine confidently predicts Trending_Down/High_Vol; else HOLD long.\n")
    print(f"{'asset':6} {'switch':>6} {'in-mkt%':>7} | {'RET no-cost':>11} {'RET +cost':>9} {'RET b/h':>8} | {'maxDD strat':>11} {'maxDD b/h':>10}")
    for t in tickers:
        try:
            r = run(t)
        except Exception as e:  # noqa: BLE001
            print(f"{t:6} ERROR {str(e)[:60]}")
            continue
        print(f"{t:6} {r['switches']:6} {r['pct_in_market']:7.1f} | {r['ret_nocost_pct']:+11.2f} "
              f"{r['ret_cost_pct']:+9.2f} {r['ret_buyhold_pct']:+8.2f} | {r['maxdd_strat_pct']:11.2f} {r['maxdd_buyhold_pct']:10.2f}")


if __name__ == "__main__":
    main()
