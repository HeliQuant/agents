"""Rolling / adaptive walk-forward for trend-following — the REALISTIC deployment test.

scripts/19 froze ONE config over a single 60/40 split (failed OOS on full history).
Real systematic strategies RE-TUNE periodically. This rolls an anchored window:

  every `TEST_BARS`, re-train the regime classifier + re-optimize the momentum config
  on the trailing `TRAIN_BARS`, LOCK it, trade the next `TEST_BARS` out-of-sample, then
  roll forward. The OOS periods are chained (compounded) — nothing is ever tuned on the
  data it is then tested on.

If the chained OOS is positive, that is an HONEST, deployable edge. If not, even
periodic re-tuning doesn't save it.

Run: python scripts/25_walkforward_rolling.py MNT [METH ETH ...]
Writes: data/rolling_walkforward.json
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


def _load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


wf = _load("wf19", "19_walkforward_trend.py")     # reuse its run()/simulate()/constants
ma = _load("multi_asset", "multi_asset.py")

TRAIN_BARS = 4320   # ~180 days trailing train/optimize window
TEST_BARS = 1440    # ~60 days forward OOS, then re-tune and roll
GRID = list(product([30.0, 40.0], [10, 20], [2.0, 2.5], [0.65]))  # trimmed for rolling compute


def best_config(train, ptr, labels, adx_th, vol_th):
    best, best_roi = None, -1e9
    for adx_min, bwin, tp, conf in GRID:
        r = wf.run(train, ptr, labels, adx_th, vol_th, adx_min=adx_min, bwin=bwin, tp=tp, conf=conf)
        if r["trades"] >= wf.MIN_TRAIN and r["roi"] > best_roi:
            best_roi, best = r["roi"], (adx_min, bwin, tp, conf)
    return best


def rolling(ticker: str) -> dict:
    fp = ROOT / "data" / f"{ticker.lower()}_features.csv"
    if not fp.exists():
        return {"ticker": ticker, "verdict": "NO_DATA"}
    df = pd.read_csv(fp).dropna().reset_index(drop=True)
    eq, total_trades, folds = 1.0, 0, []
    i = TRAIN_BARS
    while i + TEST_BARS <= len(df):
        train = df.iloc[i - TRAIN_BARS:i].reset_index(drop=True)
        test = df.iloc[i:i + TEST_BARS].reset_index(drop=True)
        try:
            bundle = ma.train_classifier(train)
        except Exception:  # noqa: BLE001
            i += TEST_BARS
            continue
        feats, labels = bundle["features"], bundle["labels"]
        adx_th, vol_th = bundle["adx_strong_threshold"], bundle["vol_high_threshold"]
        ptr = bundle["model"].predict_proba(train[feats])
        cfg = best_config(train, ptr, labels, adx_th, vol_th)
        if cfg is None:
            folds.append({"fold": len(folds) + 1, "cfg": "none(thin)", "oos_roi": 0.0, "trades": 0, "win": 0.0})
            i += TEST_BARS
            continue
        pte = bundle["model"].predict_proba(test[feats])
        adx_min, bwin, tp, conf = cfg
        oos = wf.run(test, pte, labels, adx_th, vol_th, adx_min=adx_min, bwin=bwin, tp=tp, conf=conf)
        eq *= (1 + oos["roi"] / 100.0)
        total_trades += oos["trades"]
        folds.append({"fold": len(folds) + 1, "cfg": f"adx{adx_min:.0f}/bw{bwin}/tp{tp}",
                      "oos_roi": oos["roi"], "trades": oos["trades"], "win": oos["win"]})
        i += TEST_BARS

    traded = [f for f in folds if f["trades"] > 0]
    pos = sum(1 for f in traded if f["oos_roi"] > 0)
    return {
        "ticker": ticker,
        "train_bars": TRAIN_BARS, "test_bars": TEST_BARS,
        "folds": len(folds), "traded_folds": len(traded), "pos_folds": pos,
        "total_oos_roi_compounded": round((eq - 1) * 100, 2),
        "total_oos_trades": total_trades,
        "fold_detail": folds,
    }


def main():
    tickers = [t.upper() for t in sys.argv[1:]] or ["MNT"]
    out = {}
    for t in tickers:
        print(f"rolling WF {t} (train {TRAIN_BARS}b / test {TEST_BARS}b, re-tune each fold) ...", flush=True)
        res = rolling(t)
        out[t] = res
        if "total_oos_roi_compounded" in res:
            print(f"  {t}: chained OOS {res['total_oos_roi_compounded']:+.2f}%  | "
                  f"{res['total_oos_trades']} trades | {res['pos_folds']}/{res['traded_folds']} folds positive")
            for f in res["fold_detail"]:
                print(f"      fold {f['fold']:2} [{f['cfg']:>14}] OOS {f['oos_roi']:+7.2f}%  ({f['trades']} trd)")
        else:
            print(f"  {t}: {res.get('verdict')}")
    (ROOT / "data" / "rolling_walkforward.json").write_text(json.dumps(out, indent=2))
    print("\nsaved -> data/rolling_walkforward.json")


if __name__ == "__main__":
    main()
