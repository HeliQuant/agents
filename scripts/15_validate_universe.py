"""Validate a whole universe of assets through the walk-forward gate, end to end.

For each ticker: pull ~1yr hourly OHLC (Binance mirror) -> build features ->
walk-forward (tune on 60%, evaluate on unseen 40%) -> record PASS/OVERFIT/THIN.

Writes data/universe_results.json with every asset's IS + OOS numbers so nothing
is fabricated — the JSON is the single source of truth for docs/frontend.

Usage:
    python scripts/15_validate_universe.py BTC ETH SOL BNB ARB AVAX LINK MNT ENA DOGE
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.strategies import Action, MeanReversionStrategy  # noqa: E402
from firm.strategies.base import Position  # noqa: E402
from firm.agents.strategy_lifecycle import assess_momentum_activation  # noqa: E402

spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
multi_asset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi_asset)

BINANCE = "https://data-api.binance.vision/api/v3/klines"
INITIAL = 1_000.0
RISK = 0.01
FEE = 0.0010
LOOKAHEAD = 8
SL_MULT, REWARD = 1.0, 1.46
TRAIN_FRAC = 0.60
MIN_TRAIN_TRADES, MIN_TEST_TRADES = 5, 4
HOURS = 8760


def pull(ticker: str) -> pd.DataFrame:
    end = int(time.time() * 1000)
    cursor = end - HOURS * 3600 * 1000
    rows = []
    while cursor < end:
        r = requests.get(BINANCE, params={"symbol": f"{ticker}USDT", "interval": "1h",
                                          "startTime": cursor, "limit": 1000}, timeout=20)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        for k in batch:
            ot = int(k[0])
            rows.append({"timestamp": ot,
                         "datetime": datetime.fromtimestamp(ot / 1000, tz=timezone.utc),
                         "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                         "close": float(k[4]), "volume": float(k[7])})
        cursor = int(batch[-1][0]) + 3600 * 1000
        if len(batch) < 1000:
            break
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)


def simulate(i, df, direction, entry, atr, equity):
    sl_dist = atr * SL_MULT
    notional = min((equity * RISK) * (entry / sl_dist), equity * 0.95)
    win = df.iloc[i + 1: i + LOOKAHEAD + 1]
    if win.empty:
        return 0.0
    if direction > 0:
        tp, sl = entry + atr * REWARD, entry - sl_dist
        hit_tp, hit_sl = win[win["high"] >= tp], win[win["low"] <= sl]
    else:
        tp, sl = entry - atr * REWARD, entry + sl_dist
        hit_tp, hit_sl = win[win["low"] <= tp], win[win["high"] >= sl]
    if not hit_tp.empty and not hit_sl.empty:
        exit_p = tp if hit_tp.index[0] < hit_sl.index[0] else sl
    elif not hit_tp.empty:
        exit_p = tp
    elif not hit_sl.empty:
        exit_p = sl
    else:
        exit_p = float(win["close"].iloc[-1])
    pct = (exit_p - entry) / entry if direction > 0 else (entry - exit_p) / entry
    return notional * pct - notional * FEE * 2


def replay(df, proba, labels, adx_th, vol_th, *, ov, ob, tp, flat, conf):
    mr = MeanReversionStrategy(oversold_rsi=ov, overbought_rsi=ob, tp_atr_mult=tp)
    equity, wins, n = INITIAL, 0, 0
    i, cd = 30, -1
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
        if chosen != "Ranging":
            i += 1
            continue
        if not assess_momentum_activation(df.iloc[max(0, i - 60):i + 1], return_strong_pct=flat).mean_reversion_active:
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
        cd = i + LOOKAHEAD
        i += 1
    return {"roi": (equity / INITIAL - 1) * 100, "win": (wins / n * 100) if n else 0.0, "trades": n}


def validate(ticker: str) -> dict:
    df = pull(ticker)
    if df.empty or len(df) < 2000:
        return {"ticker": ticker, "verdict": "NO_DATA", "bars": len(df)}
    feat = multi_asset.add_features(df).dropna().reset_index(drop=True)
    split = int(len(feat) * TRAIN_FRAC)
    train, test = feat.iloc[:split].reset_index(drop=True), feat.iloc[split:].reset_index(drop=True)
    bundle = multi_asset.train_classifier(train)
    labels = bundle["labels"]
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    ptr = bundle["model"].predict_proba(train[bundle["features"]])
    pte = bundle["model"].predict_proba(test[bundle["features"]])

    best, best_is = None, -1e9
    for ov, ob, tp, flat, conf in product([20., 25., 30.], [70., 75., 80.], [1.5, 1.8], [2., 3.], [0.60, 0.65]):
        r = replay(train, ptr, labels, adx_th, vol_th, ov=ov, ob=ob, tp=tp, flat=flat, conf=conf)
        if r["trades"] >= MIN_TRAIN_TRADES and r["roi"] > best_is:
            best_is, best = r["roi"], dict(ov=ov, ob=ob, tp=tp, flat=flat, conf=conf, **r)
    if best is None:
        return {"ticker": ticker, "verdict": "THIN", "bars": len(feat)}
    oos = replay(test, pte, labels, adx_th, vol_th, ov=best["ov"], ob=best["ob"],
                 tp=best["tp"], flat=best["flat"], conf=best["conf"])
    if oos["trades"] < MIN_TEST_TRADES:
        verdict = "INCONCLUSIVE"
    elif oos["roi"] > 0:
        verdict = "PASS"
    else:
        verdict = "OVERFIT"
    return {
        "ticker": ticker, "verdict": verdict, "bars": len(feat),
        "is_roi": round(best["roi"], 2), "is_win": round(best["win"], 1), "is_trades": best["trades"],
        "oos_roi": round(oos["roi"], 2), "oos_win": round(oos["win"], 1), "oos_trades": oos["trades"],
        "config": {"rsi": f"{best['ov']:.0f}/{best['ob']:.0f}", "tp": best["tp"],
                   "flat": best["flat"], "conf": best["conf"]},
    }


def main():
    tickers = [t.upper() for t in sys.argv[1:]] or ["BTC", "ETH", "SOL", "MNT", "ENA"]
    out = {}
    for t in tickers:
        print(f"validating {t} ...", end=" ", flush=True)
        try:
            res = validate(t)
        except Exception as e:  # noqa: BLE001
            res = {"ticker": t, "verdict": "ERROR", "error": str(e)[:80]}
        out[t] = res
        v = res.get("verdict")
        if v == "PASS":
            print(f"PASS  OOS {res['oos_roi']:+.2f}% win {res['oos_win']:.0f}% ({res['oos_trades']} trd)")
        elif v in ("OVERFIT", "INCONCLUSIVE"):
            print(f"{v}  OOS {res.get('oos_roi')}% ({res.get('oos_trades')} trd)")
        else:
            print(v)

    path = ROOT / "data" / "universe_results.json"
    path.write_text(json.dumps(out, indent=2))
    passed = [t for t, r in out.items() if r.get("verdict") == "PASS"]
    print(f"\nPASSED ({len(passed)}/{len(tickers)}): {', '.join(passed)}")
    print(f"saved -> {path}")


if __name__ == "__main__":
    main()
