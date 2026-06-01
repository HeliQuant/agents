"""Dynamic R:R experiment — does scaling take-profit by trend strength (ADX) beat
the FIXED R:R validated in scripts/19?

Controlled comparison: SAME entry logic + the validated per-asset entry params
(from scripts/19), ONLY the exit reward multiple changes:
  - FIXED   : reward = validated fixed tp (MNT 2.5, mETH 2.0)
  - DYNAMIC : reward = tp_lo + (tp_hi - tp_lo) * clamp((ADX - adx_lo)/(adx_hi - adx_lo), 0, 1)
              adx_lo = trending threshold (train ADX p60), adx_hi = train ADX p90.
              => just-trending -> tp_lo ; very strong trend -> tp_hi (let winners run).

Honest protocol: the dynamic rule's (tp_lo, tp_hi) is tuned on TRAIN only, then
evaluated ONCE on the unseen OOS window, head-to-head vs FIXED on the same OOS.
The FIXED OOS should reproduce scripts/19 (~MNT +5.58%) = harness sanity check.

Usage: python scripts/20_walkforward_dynamic_rr.py
Writes: data/dynamic_rr_results.json
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
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

INITIAL, RISK, FEE = 1_000.0, 0.01, 0.0010
LOOKAHEAD, SL_MULT = 8, 1.0
TRAIN_FRAC = 0.60
TREND = ("Trending_Up", "Trending_Down")

# Validated entry configs (scripts/19), held FIXED to isolate the R:R effect.
ENTRY = {
    "MNT": {"adx_min": 40.0, "bwin": 10, "conf": 0.65, "fixed_tp": 2.5},
    "METH": {"adx_min": 40.0, "bwin": 20, "conf": 0.60, "fixed_tp": 2.0},
}


def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


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


def run(df, proba, labels, adx_th, vol_th, *, adx_min, bwin, conf, rr):
    """rr = ('fixed', tp)  OR  ('dyn', tp_lo, tp_hi, adx_lo, adx_hi)."""
    mo = MomentumStrategy(adx_min=adx_min, breakout_window=bwin, volume_min_ratio=0.0, tp_atr_mult=2.5)
    eq, wins, n, i, cd = INITIAL, 0, 0, 30, -1
    wl = max(bwin + 2, 30)
    rewards = []
    while i < len(df) - LOOKAHEAD:
        if i <= cd:
            i += 1
            continue
        if float(proba[i].max()) < conf:
            i += 1
            continue
        row = df.iloc[i]
        cur = ma.detect_current(row, adx_th, vol_th)
        fwd = labels[int(proba[i].argmax())]
        chosen = "High_Volatility" if cur == "High_Volatility" else (cur if cur == fwd else fwd)
        if chosen not in TREND:
            i += 1
            continue
        dec = mo.evaluate(df.iloc[max(0, i - wl): i + 1], Position())
        if dec.action not in (Action.BUY, Action.SELL):
            i += 1
            continue
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        if rr[0] == "fixed":
            reward = rr[1]
        else:
            _, tp_lo, tp_hi, adx_lo, adx_hi = rr
            reward = tp_lo + (tp_hi - tp_lo) * clamp((float(row["adx"]) - adx_lo) / (adx_hi - adx_lo + 1e-9), 0, 1)
        rewards.append(reward)
        pnl = simulate(i, df, 1 if dec.action == Action.BUY else -1, float(row["close"]), atr, eq, reward)
        eq += pnl
        wins += 1 if pnl > 0 else 0
        n += 1
        cd = i + LOOKAHEAD
        i += 1
    return {
        "roi": round((eq / INITIAL - 1) * 100, 2),
        "win": round((wins / n * 100) if n else 0.0, 1),
        "trades": n,
        "avg_rr": round(sum(rewards) / len(rewards), 2) if rewards else 0.0,
    }


def evaluate(ticker: str) -> dict:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    split = int(len(df) * TRAIN_FRAC)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)
    bundle = ma.train_classifier(train)
    labels = bundle["labels"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    ptr = bundle["model"].predict_proba(train[bundle["features"]])
    pte = bundle["model"].predict_proba(test[bundle["features"]])
    e = ENTRY[ticker]
    adx_lo = float(adx_th)
    adx_hi = float(train["adx"].quantile(0.90))

    fixed_oos = run(test, pte, labels, adx_th, vol_th, adx_min=e["adx_min"], bwin=e["bwin"], conf=e["conf"], rr=("fixed", e["fixed_tp"]))

    best, best_is = None, -1e9
    for tp_lo, tp_hi in product([1.5, 2.0], [3.0, 3.5, 4.0]):
        r = run(train, ptr, labels, adx_th, vol_th, adx_min=e["adx_min"], bwin=e["bwin"], conf=e["conf"],
                rr=("dyn", tp_lo, tp_hi, adx_lo, adx_hi))
        if r["trades"] >= 5 and r["roi"] > best_is:
            best_is, best = r["roi"], (tp_lo, tp_hi)
    dyn_oos = (
        run(test, pte, labels, adx_th, vol_th, adx_min=e["adx_min"], bwin=e["bwin"], conf=e["conf"],
            rr=("dyn", best[0], best[1], adx_lo, adx_hi))
        if best else None
    )

    return {
        "ticker": ticker,
        "adx_scale": [round(adx_lo, 1), round(adx_hi, 1)],
        "fixed": {"tp": e["fixed_tp"], "oos": fixed_oos},
        "dynamic": {"tp_lo": best[0] if best else None, "tp_hi": best[1] if best else None, "oos": dyn_oos},
    }


def main():
    out = {}
    for t in ["MNT", "METH"]:
        r = evaluate(t)
        out[t] = r
        f = r["fixed"]["oos"]
        d = r["dynamic"]["oos"]
        print(f"=== {t}  (ADX scale {r['adx_scale'][0]}..{r['adx_scale'][1]}) ===")
        print(f"  FIXED   tp={r['fixed']['tp']:<4}        : OOS {f['roi']:+.2f}%  win {f['win']}%  ({f['trades']} trd)")
        if d:
            print(f"  DYNAMIC tp={r['dynamic']['tp_lo']}-{r['dynamic']['tp_hi']} (avg {d['avg_rr']}): OOS {d['roi']:+.2f}%  win {d['win']}%  ({d['trades']} trd)")
            delta = d["roi"] - f["roi"]
            print(f"  -> dynamic {'BEAT' if delta > 0 else 'did NOT beat'} fixed by {delta:+.2f}pp OOS")
        else:
            print("  DYNAMIC: no config cleared trade floor")
    (ROOT / "data" / "dynamic_rr_results.json").write_text(json.dumps(out, indent=2))
    print("\nsaved -> data/dynamic_rr_results.json")


if __name__ == "__main__":
    main()
