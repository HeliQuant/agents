"""HeliQuant — BTC edge hunt, round 3: a LESS-EFFICIENT VENUE (scripts/74).

Every BTC signal we tried on Bybit/Binance was fee-eaten or a walk-forward artifact, because those are
the most-arbitraged BTC venues on earth. Honest new angle: **Hyperliquid** is an on-chain perp DEX —
smaller, younger, less arbitraged. The SAME contrarian edge that dies on Bybit BTC *might* survive here,
and HL exposes a signal the CEXes don't: **premium** (perp price vs oracle) — a direct crowd-positioning
gauge. We test funding_fade AND premium_fade on HL BTC under the EXACT same gate (cost-aware OOS +
walk-forward + drop-best-fold). No overfit, no forced trade. Either it clears honestly or BTC stays abstained.

Run: python scripts/74_btc_hyperliquid.py
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.edge_lab import FEE, validate_signal, walk_forward  # noqa: E402

API = "https://api.hyperliquid.xyz/info"


def fetch_funding(coin: str, days: int = 195) -> pd.DataFrame:  # HL retains ~200d of 1h candles
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    rows: dict[int, dict] = {}
    cur = start
    for _ in range(40):  # 500 hourly rows/call ~ 21d -> ~26 calls for 540d
        try:
            j = requests.post(API, json={"type": "fundingHistory", "coin": coin, "startTime": cur}, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"    funding error: {str(e)[:60]}")
            break
        if not isinstance(j, list) or not j:
            break
        for x in j:
            t = int(x["time"])
            rows[t] = {"timestamp": t, "funding": float(x["fundingRate"]), "premium": float(x["premium"])}
        newest = max(int(x["time"]) for x in j)
        if newest <= cur:
            break
        cur = newest + 1
        if newest >= now - 3600 * 1000:
            break
        time.sleep(0.1)
    return pd.DataFrame(sorted(rows.values(), key=lambda r: r["timestamp"])).reset_index(drop=True) if rows else pd.DataFrame()


def fetch_candles(coin: str, days: int = 195) -> pd.DataFrame:
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    rows: dict[int, dict] = {}
    cur = start
    for _ in range(40):
        end = min(cur + 4000 * 3600 * 1000, now)
        try:
            j = requests.post(API, json={"type": "candleSnapshot",
                                         "req": {"coin": coin, "interval": "1h", "startTime": cur, "endTime": end}},
                              timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"    candle error: {str(e)[:60]}")
            break
        if not isinstance(j, list) or not j:
            break
        for k in j:
            t = int(k["t"])
            rows[t] = {"timestamp": t, "close": float(k["c"])}
        newest = max(int(k["t"]) for k in j)
        if newest <= cur:
            break
        cur = newest + 1
        if newest >= now - 3600 * 1000:
            break
        time.sleep(0.1)
    return pd.DataFrame(sorted(rows.values(), key=lambda r: r["timestamp"])).reset_index(drop=True) if rows else pd.DataFrame()


def report(name: str, close: np.ndarray, sig: np.ndarray) -> dict | None:
    m = validate_signal(close, sig)
    if not m:
        print(f"  {name:16} insufficient sample")
        return None
    wf = walk_forward(close, sig) or {}
    clears = m["avg_bps"] > 2 * FEE * 1e4
    robust = bool(wf.get("consistent"))
    verdict = ("EARNED ✅" if (m["passed"] and robust) else
               "1-split-only ⚠️ (WF artifact)" if m["passed"] else "—")
    print(f"  {name:16}{('contrarian' if m['contrarian'] else 'momentum'):11}"
          f"{m['oos_roi_pct']:>+9.2f}%  b&h{m['buyhold_pct']:>+7.1f}%{m['trades']:>5}t"
          f"{m['avg_bps']:>+8.1f}bps {'>fee' if clears else '<FEE':5} "
          f"WF {wf.get('positive','?')}/{wf.get('folds','?')} ex-best {wf.get('ex_best_mean','?')}%  {verdict}")
    return {"name": name, **m, "wf": wf, "clears_fee": clears, "robust": robust}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("BTC edge hunt round 3 — Hyperliquid (less-efficient on-chain venue)")
    print(f"gate: OOS ROI>0 & >b&h & avg_bps>{2*FEE*1e4:.0f} & trades>=20 & walk-forward robust (drop-best-fold)\n")

    fund = fetch_funding("BTC")
    cdl = fetch_candles("BTC")
    if fund.empty or cdl.empty:
        print("  no data — abort"); return
    df = pd.merge_asof(cdl.sort_values("timestamp"), fund.sort_values("timestamp"),
                       on="timestamp", direction="backward", tolerance=2 * 3600 * 1000)
    df = df.dropna(subset=["funding", "premium"]).reset_index(drop=True)
    span = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 1000 / 86400
    print(f"HL BTC merged: {len(df)} hourly bars ({span:.0f}d), "
          f"{datetime.fromtimestamp(df['timestamp'].iloc[0]/1000,tz=timezone.utc).date()} -> "
          f"{datetime.fromtimestamp(df['timestamp'].iloc[-1]/1000,tz=timezone.utc).date()}")
    df.to_csv(ROOT / "data" / "btc_hyperliquid.csv", index=False)
    close = df["close"].values
    bh = (close[-1] / close[0] - 1) * 100
    print(f"BTC spot over window: {bh:+.1f}% (buy&hold)\n")

    print(f"{'signal':16}{'dir':11}{'OOS ROI':>10}{'  b&h':>10}{'trades':>5}{'avg_bps':>9}{'fee':>6}  walk-forward")
    print("-" * 96)
    results = []
    # 1. funding level (fade extremes) — does the CEX-dead edge live on the DEX?
    results.append(report("funding_fade", close, df["funding"].values))
    # 2. premium (perp vs oracle) — HL-exclusive crowd-positioning signal, fade extremes
    results.append(report("premium_fade", close, df["premium"].values))
    # 3. funding 24h change
    results.append(report("funding_chg24", close, df["funding"].diff(24).fillna(0).values))
    # 4. premium 24h change
    results.append(report("premium_chg24", close, df["premium"].diff(24).fillna(0).values))
    # 5. premium momentum (maybe HL trends, not reverts)
    results.append(report("premium_mom6", close, df["premium"].rolling(6).mean().fillna(0).values))

    earned = [r for r in results if r and r["passed"] and r["robust"]]
    print("\n" + "=" * 96)
    if earned:
        print(f"BTC on Hyperliquid — EARNED a robust edge: {', '.join(r['name'] for r in earned)}  "
              f"-> candidate (probation), NOT auto-live. Confirm forward before capital.")
    else:
        clears = [r for r in results if r and r["clears_fee"] and r["passed"]]
        print("BTC on Hyperliquid — NO robust fee-clearing edge.",
              f"({len(clears)} cleared 1-split but failed walk-forward -> artifacts)" if clears
              else "(nothing even cleared a single split)")
        print("Honest verdict stands: BTC abstained. The DEX venue is efficient too at the 1h/24h horizon.")
    print("data -> data/btc_hyperliquid.csv")


if __name__ == "__main__":
    main()
