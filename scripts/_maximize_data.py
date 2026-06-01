"""Maximize the backtest dataset: re-fetch max-depth Pyth hourly history for every
asset, then rebuild its feature CSV. Newer tokens return only what exists; deep-history
assets (MNT, majors) jump from ~1y to their full ~2-3y. Run once before the walk-forward.

Run: python scripts/_maximize_data.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, fname: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pyth = _load("collect_pyth", "17_collect_pyth.py")
ma = _load("multi_asset", "multi_asset.py")

TICKERS = ["MNT", "METH", "CMETH", "FBTC", "BTC", "ETH", "SOL", "ENA", "USDE"]
TARGET_DAYS = 1095  # 3y target; Pyth returns whatever it actually has

print(f"{'asset':6} {'bars':>7} {'days':>6} {'feat_rows':>10}")
for t in TICKERS:
    sym = pyth.PYTH_SYMBOL.get(t, f"Crypto.{t}/USD")
    df = pyth.fetch_history(sym, TARGET_DAYS)
    if df.empty:
        print(f"{t:6} {'NO DATA':>7}")
        continue
    df.to_csv(ROOT / "data" / f"{t.lower()}_hourly.csv", index=False)
    feat = ma.add_features(df)
    feat.to_csv(ROOT / "data" / f"{t.lower()}_features.csv", index=False)
    days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 1000 / 86400
    print(f"{t:6} {len(df):7} {days:6.0f} {len(feat.dropna()):10}", flush=True)

print("done — features rebuilt for all assets.")
