"""Do Bybit positioning signals PREDICT forward returns? Honest, overfit-resistant test.

Method (no threshold tuning -> hard to fool ourselves):
  * IC = Spearman rank corr(signal[t], forward_return[t->t+h]) at h = 4/8/24h.
  * Stability = recompute IC on 1st half vs 2nd half of the timeline; sign must agree.
  * Quantile = bin signal into 5, mean forward return per bin (monotonic = real relationship).
With N~9000, p-values are meaningless (everything is "significant") -> we judge MAGNITUDE +
STABILITY + cross-asset consistency + economic sense. |IC|<0.03 ~ noise.

Run: python scripts/38_positioning_ic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ["MNT", "BTC", "ETH", "SOL"]
HORIZONS = [4, 8, 24]
WIN = 168  # 7d hourly, for z-scores


def build(ticker):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    c = df["close"]
    sig = {}
    if "funding" in df:
        f = df["funding"]
        sig["funding"] = f
        sig["funding_z"] = (f - f.rolling(WIN, min_periods=24).mean()) / f.rolling(WIN, min_periods=24).std()
    if "oi" in df:
        o = df["oi"]
        sig["oi_chg4"] = o.pct_change(4)
        sig["oi_chg24"] = o.pct_change(24)
    if "buy_ratio" in df:
        b = df["buy_ratio"]
        sig["buy_ratio"] = b
        sig["buyratio_z"] = (b - b.rolling(WIN, min_periods=24).mean()) / b.rolling(WIN, min_periods=24).std()
        sig["buyratio_chg24"] = b.diff(24)
    fwd = {h: c.shift(-h) / c - 1 for h in HORIZONS}
    return sig, fwd


def ic_split(s, r):
    d = pd.concat([s.reset_index(drop=True), r.reset_index(drop=True)], axis=1).dropna()
    d.columns = ["s", "r"]
    if len(d) < 300:
        return None, None, None
    mid = len(d) // 2
    return (d["s"].corr(d["r"], method="spearman"),
            d["s"].iloc[:mid].corr(d["r"].iloc[:mid], method="spearman"),
            d["s"].iloc[mid:].corr(d["r"].iloc[mid:], method="spearman"))


def quantile(s, r, nq=5):
    d = pd.concat([s.reset_index(drop=True), r.reset_index(drop=True)], axis=1).dropna()
    d.columns = ["s", "r"]
    try:
        d["q"] = pd.qcut(d["s"], nq, labels=False, duplicates="drop")
    except Exception:  # noqa: BLE001
        return None
    return d.groupby("q")["r"].mean() * 100


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    flagged = []
    for t in ASSETS:
        sig, fwd = build(t)
        print(f"\n================= {t} =================")
        print(f"{'signal':16} {'IC@4h':>8} {'IC@8h':>8} {'IC@24h':>8}   {'half@24h(1st/2nd)':>20}  flag")
        for name, s in sig.items():
            ics = {h: ic_split(s, fwd[h])[0] for h in HORIZONS}
            full24, i1, i2 = ic_split(s, fwd[24])
            stable = (i1 is not None and i2 is not None and (i1 > 0) == (i2 > 0) and min(abs(i1), abs(i2)) >= 0.02)
            strong = stable and full24 is not None and abs(full24) >= 0.04
            flag = "** STRONG" if strong else ("* stable" if stable else "")
            if strong:
                flagged.append((t, name, full24, "contrarian" if full24 < 0 else "momentum"))

            def fmt(x):
                return f"{x:+.3f}" if x is not None else "   n/a"
            half = f"{fmt(i1)}/{fmt(i2)}" if i1 is not None else "n/a"
            print(f"{name:16} {fmt(ics[4]):>8} {fmt(ics[8]):>8} {fmt(ics[24]):>8}   {half:>20}  {flag}")

    # Headline hypothesis: retail crowded-long -> contrarian. Quintiles of buyratio_z vs fwd 24h.
    print("\n\n===== Quintile check: buyratio_z -> mean forward 24h return (%) =====")
    print("(contrarian if Q4[most long] < Q0[least long]; momentum if opposite)")
    for t in ASSETS:
        sig, fwd = build(t)
        if "buyratio_z" not in sig:
            continue
        q = quantile(sig["buyratio_z"], fwd[24])
        if q is None:
            continue
        cells = "  ".join(f"Q{int(i)}:{v:+.2f}" for i, v in q.items())
        spread = q.iloc[-1] - q.iloc[0]
        print(f"  {t:5} {cells}   | Q4-Q0 spread: {spread:+.2f}%")

    print("\n\n===== HONEST VERDICT =====")
    if flagged:
        for t, name, ic, direction in flagged:
            print(f"  {t} {name}: IC@24h {ic:+.3f} ({direction}) — stable+meaningful, worth a cost-aware backtest")
    else:
        print("  No signal cleared |IC|>=0.04 AND half-split stability. Positioning = weak/noisy predictor here.")
        print("  (Still valid as LIVE risk-context for the org; just not a standalone backtested alpha.)")


if __name__ == "__main__":
    main()
