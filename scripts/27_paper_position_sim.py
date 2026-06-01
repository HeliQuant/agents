"""$100 hypothetical paper-sim — trade the regime prediction, P&L from REAL prices (no money).

Honest forward-style paper-trade on the recent OOS window (model trained on all-but-LIVE_DAYS,
never saw it): when the regime engine predicts a TRENDING regime with confidence >= GATE, open a
position in that direction (Trending_Up=LONG, Trending_Down=SHORT); exit at TP (ATR*mult) / SL
(ATR) / timeout using REAL forward prices. Track a $100 account.

HONEST: backtests show NO validated trend-following edge -> expect ~flat/noise over a short
window. This is transparency (what $100 would have done), NOT a profit claim. Prediction
accuracy != trading profit.

Run: python scripts/27_paper_position_sim.py MNT [DAYS]
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

CAP, RISK, FEE = 100.0, 0.01, 0.0010
LOOKAHEAD, SL_MULT, TP_MULT, GATE = 8, 1.0, 2.5, 0.65
TREND = ("Trending_Up", "Trending_Down")


def simulate(i, df, direction, entry, atr, equity):
    sl = atr * SL_MULT
    notional = min((equity * RISK) * (entry / sl), equity * 0.95)
    win = df.iloc[i + 1: i + LOOKAHEAD + 1]
    if win.empty:
        return 0.0, "TIMEOUT"
    if direction > 0:
        tp, sld = entry + atr * TP_MULT, entry - sl
        ht, hs = win[win["high"] >= tp], win[win["low"] <= sld]
    else:
        tp, sld = entry - atr * TP_MULT, entry + sl
        ht, hs = win[win["low"] <= tp], win[win["high"] >= sld]
    if not ht.empty and not hs.empty:
        ex, oc = (tp, "TP") if ht.index[0] < hs.index[0] else (sld, "SL")
    elif not ht.empty:
        ex, oc = tp, "TP"
    elif not hs.empty:
        ex, oc = sld, "SL"
    else:
        ex, oc = float(win["close"].iloc[-1]), "TIMEOUT"
    pct = (ex - entry) / entry if direction > 0 else (entry - ex) / entry
    return notional * pct - notional * FEE * 2, oc


def run(ticker: str, live_days: int) -> dict:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    live = live_days * 24
    train_df, recent = df.iloc[:-live].reset_index(drop=True), df.iloc[-live:].reset_index(drop=True)
    bundle = ma.train_classifier(train_df)
    labels = bundle["labels"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    proba = bundle["model"].predict_proba(recent[ma.FEATURES])

    eq, wins, n, i, cd = CAP, 0, 0, 30, -1
    eq_curve = [CAP]
    while i < len(recent) - LOOKAHEAD:
        if i <= cd:
            i += 1
            continue
        if float(proba[i].max()) < GATE:
            i += 1
            continue
        row = recent.iloc[i]
        cur = ma.detect_current(row, adx_th, vol_th)
        fwd = labels[int(proba[i].argmax())]
        chosen = "High_Volatility" if cur == "High_Volatility" else (cur if cur == fwd else fwd)
        if chosen not in TREND:
            i += 1
            continue
        atr = float(row["atr"])
        if atr <= 0:
            i += 1
            continue
        pnl, _ = simulate(i, recent, 1 if chosen == "Trending_Up" else -1, float(row["close"]), atr, eq)
        eq += pnl
        eq_curve.append(eq)
        wins += 1 if pnl > 0 else 0
        n += 1
        cd = i + LOOKAHEAD
        i += 1

    arr = np.array(eq_curve)
    peak = np.maximum.accumulate(arr)
    max_dd = float(((peak - arr) / peak).max() * 100) if len(arr) > 1 else 0.0
    buyhold = (float(recent["close"].iloc[-1]) / float(recent["close"].iloc[0]) - 1) * 100
    return {
        "ticker": ticker, "days": live_days, "trades": n,
        "win_rate_pct": round((wins / n * 100) if n else 0.0, 1),
        "final_equity": round(eq, 2), "pnl_pct": round((eq / CAP - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2), "buy_hold_pct": round(buyhold, 2),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tickers = [sys.argv[1].upper()] if len(sys.argv) > 1 else ["MNT", "BTC", "ETH"]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    print(f"=== $100 paper-sim (trade the regime prediction, real-price P&L) — last {days}d OOS ===")
    print("HONEST: backtests show no validated edge; expect ~flat/noise. Not a profit claim.\n")
    print(f"{'asset':6} {'trades':>7} {'win%':>6} {'$100→':>9} {'P&L%':>7} {'maxDD%':>7} {'buy-hold%':>10}")
    for t in tickers:
        r = run(t, days)
        print(f"{t:6} {r['trades']:7} {r['win_rate_pct']:6.1f} ${r['final_equity']:>7.2f} "
              f"{r['pnl_pct']:+7.2f} {r['max_drawdown_pct']:7.2f} {r['buy_hold_pct']:+10.2f}")


if __name__ == "__main__":
    main()
