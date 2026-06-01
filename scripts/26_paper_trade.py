"""Live predict-only paper-trade — honest forward-test of the regime engine.

NO positions are taken. Each run:
  1. (re)train the regime classifier on history EXCLUDING the last LIVE_DAYS days
  2. score forward-regime predictions over that recent OOS window (outcomes known) —
     an immediate, honest 'how accurate, live-like' reading (model never saw this window)
  3. log the CURRENT bar's forward-(4h) regime prediction to data/paper_trade_log.jsonl
     — a true forward-test entry, scored automatically on a later run once 4h has elapsed
     and fresh data is fetched (run scripts/17_collect_pyth.py + _maximize_data first)
  4. score any matured entries from previous runs against realized outcomes

This runs the REAL pipeline (Pyth data -> features -> classifier -> prediction) exactly
as production would, and accumulates a genuine live track record over time.

Honest boundary: this measures REGIME-PREDICTION accuracy, NOT trading profit.

Run: python scripts/26_paper_trade.py MNT
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

LIVE_DAYS = 30
HORIZON = ma.FORECAST_HORIZON  # forward bars (4h)
GATE = 0.65                    # confidence gate the org uses
LOG = ROOT / "data" / "paper_trade_log.jsonl"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MNT").upper()
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")

    live_bars = LIVE_DAYS * 24
    train_df = df.iloc[:-live_bars].reset_index(drop=True)
    recent = df.iloc[-live_bars:].reset_index(drop=True)

    bundle = ma.train_classifier(train_df)
    adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
    model = bundle["model"]

    # (2) recent OOS scoring — model never saw this window
    recent = recent.copy()
    recent["regime_now"] = ma.label_regimes(recent, adx_th, vol_th)
    recent["regime_future"] = recent["regime_now"].shift(-HORIZON)
    scored = recent.dropna(subset=["regime_future"]).reset_index(drop=True)
    proba = model.predict_proba(scored[ma.FEATURES])
    pred_idx = proba.argmax(axis=1)
    conf = proba.max(axis=1)
    true_idx = scored["regime_future"].map(ma.LABEL_TO_INT).astype(int).values
    correct = pred_idx == true_idx
    gate = conf >= GATE

    print(f"=== {ticker} PAPER-TRADE (predict-only, live forward-test) ===")
    print(f"  model trained on {len(train_df)} bars (excludes last {LIVE_DAYS}d, never saw the test window)")
    print(f"  recent OOS window: {len(scored)} predictions, outcomes known")
    print(f"  regime prediction accuracy       : {correct.mean()*100:5.1f}%")
    if gate.sum():
        print(f"  accuracy when confident (>= {GATE}) : {correct[gate].mean()*100:5.1f}%  ({int(gate.sum())} preds)")

    # (3) log the live prediction for the latest closed bar (genuine forward-test)
    last = df.iloc[-1]
    p = model.predict_proba(df.iloc[[-1]][ma.FEATURES])[0]
    pi = int(p.argmax())
    now_ts = last["datetime"]
    entry = {
        "logged_utc": now_ts.isoformat(),
        "ticker": ticker,
        "price": round(float(last["close"]), 4),
        "current_regime": ma.detect_current(last, adx_th, vol_th),
        "predicted_forward_regime": ma.REGIME_LABELS[pi],
        "confidence": round(float(p[pi]), 3),
        "target_utc": (now_ts + timedelta(hours=HORIZON)).isoformat(),
        "scored": False, "actual_forward_regime": None, "correct": None,
    }
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"\n  LIVE prediction logged: {entry['predicted_forward_regime']} "
          f"(conf {entry['confidence']}) for +{HORIZON}h @ ${entry['price']}")

    # (4) score matured prior entries whose target time is now covered by fresh data
    df["regime_now_full"] = ma.label_regimes(df, adx_th, vol_th)
    rows = [json.loads(ln) for ln in LOG.open(encoding="utf-8") if ln.strip()]
    newly = 0
    for e in rows:
        if e.get("scored") or e.get("ticker") != ticker:
            continue
        tgt = pd.to_datetime(e["target_utc"], utc=True, errors="coerce")
        future = df[df["datetime"] >= tgt]
        if len(future):
            actual = future.iloc[0]["regime_now_full"]
            e["actual_forward_regime"] = actual
            e["correct"] = bool(actual == e["predicted_forward_regime"])
            e["scored"] = True
            newly += 1
    with LOG.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e) + "\n")
    live_scored = [e for e in rows if e.get("scored") and e.get("ticker") == ticker]
    if live_scored:
        acc = sum(1 for e in live_scored if e["correct"]) / len(live_scored)
        print(f"  LIVE log: {len(live_scored)} scored ({newly} new) — live accuracy {acc*100:.1f}%")
    else:
        print(f"  LIVE log: {len(rows)} prediction(s) pending (scored once +{HORIZON}h elapses & data refetched)")


if __name__ == "__main__":
    main()
