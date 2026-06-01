"""Validate Layer-1 signals: do they actually PREDICT forward returns? (the backtestable ones)

Backtestable now:
  - SENTIMENT proxy — reconstructed from historical OHLC (24h/7d/14d price-change, the dominant
    components of firm/sources/sentiment.py; volume/trending dropped as not historically available)
  - FEAR & GREED — alternative.me historical daily API, vs forward BTC daily returns

Metrics per signal:
  - IC      = Pearson corr(signal, forward return)            |IC|>~0.03 usable, ~0 noise
  - rankIC  = Spearman (rank) corr — robust to outliers
  - hit%    = directional agreement sign(signal)==sign(fwd)
  - tercile spread = mean forward return in top-1/3 signal minus bottom-1/3 signal

NOT backtestable (no history) -> must forward-log instead: Allora, Whale-flow.

Run: python scripts/29_validate_signals.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
H_SENT = 24  # forward horizon (hours) for the sentiment test


def ic_report(name: str, sig: np.ndarray, fwd: np.ndarray) -> str:
    m = (~np.isnan(sig)) & (~np.isnan(fwd))
    s, f = sig[m], fwd[m]
    if len(s) < 50:
        return f"{name:24} too few points ({len(s)})"
    ic = float(np.corrcoef(s, f)[0, 1])
    rs, rf = pd.Series(s).rank().values, pd.Series(f).rank().values
    ric = float(np.corrcoef(rs, rf)[0, 1])
    hit = float(np.mean(np.sign(s) == np.sign(f)))
    q1, q2 = np.quantile(s, [1 / 3, 2 / 3])
    top = float(f[s >= q2].mean()) * 100
    bot = float(f[s <= q1].mean()) * 100
    return (f"{name:24} n={len(s):5} | IC={ic:+.3f} rankIC={ric:+.3f} | hit={hit*100:4.1f}% | "
            f"top33%={top:+.3f}% bot33%={bot:+.3f}% spread={top-bot:+.3f}%")


def sentiment_signal(close: np.ndarray) -> np.ndarray:
    """Reconstruct the price-based sentiment proxy (sentiment.py weights 0.40/0.30/0.10, renorm)."""
    n = len(close)
    sig = np.full(n, np.nan)
    for i in range(336, n):
        r24 = close[i] / close[i - 24] - 1
        r7 = close[i] / close[i - 168] - 1
        r14 = close[i] / close[i - 336] - 1
        sig[i] = (0.40 * math.tanh(r24 * 100 / 5)
                  + 0.30 * math.tanh(r7 * 100 / 15)
                  + 0.10 * math.tanh(r14 * 100 / 25)) / 0.80
    return sig


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print("=== Layer-1 signal validation — do they PREDICT forward returns? ===\n")
    print(f"SENTIMENT proxy (reconstructed) vs forward {H_SENT}h return:")
    for t in ["MNT", "BTC", "ETH"]:
        df = pd.read_csv(ROOT / "data" / f"{t.lower()}_features.csv").dropna().reset_index(drop=True)
        close = df["close"].values
        sig = sentiment_signal(close)
        fwd = np.full(len(close), np.nan)
        for i in range(len(close) - H_SENT):
            fwd[i] = close[i + H_SENT] / close[i] - 1
        print("  " + ic_report(f"{t} sentiment->{H_SENT}h", sig, fwd))

    print("\nFEAR & GREED (alternative.me historical) vs forward BTC daily return:")
    try:
        j = requests.get("https://api.alternative.me/fng/?limit=0&format=json", timeout=25).json()
        fng = pd.DataFrame(j["data"])
        fng["day"] = pd.to_datetime(fng["timestamp"].astype(int), unit="s", utc=True).dt.floor("D")
        fng["value"] = fng["value"].astype(float)
        fng_s = fng.set_index("day")["value"].sort_index()

        btc = pd.read_csv(ROOT / "data" / "btc_features.csv")
        btc["dt"] = pd.to_datetime(btc["timestamp"], unit="ms", utc=True)
        btc_daily = btc.set_index("dt")["close"].resample("1D").last().dropna()

        merged = pd.DataFrame({"fng": fng_s}).join(pd.DataFrame({"close": btc_daily}), how="inner").dropna()
        merged["fwd1"] = merged["close"].shift(-1) / merged["close"] - 1
        merged["fwd3"] = merged["close"].shift(-3) / merged["close"] - 1
        print(f"  (overlap: {len(merged)} days, {merged.index.min().date()} -> {merged.index.max().date()})")
        for h in ["fwd1", "fwd3"]:
            print("  " + ic_report(f"F&G -> {h} BTC", merged["fng"].values, merged[h].values))
        print("  NOTE: negative IC => CONTRARIAN works (low fear&greed precedes higher forward return)")
    except Exception as e:  # noqa: BLE001
        print("  F&G fetch/align failed:", str(e)[:120])


if __name__ == "__main__":
    main()
