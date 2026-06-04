"""BTC Frontier-1: REAL order-flow / CVD edge hunt. Collect Binance BTCUSDT perp klines WITH
`takerBuyBaseVolume` (genuine aggressive-buy vs aggressive-sell pressure — NOT the long-short ACCOUNT
ratio we already ruled out), then test taker-imbalance / CVD signals under the SAME honest gate
(cost-aware OOS + walk-forward). Honest: find a robust fee-clearing edge, or rule out order-flow too.

Run: python scripts/71_btc_orderflow.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
from firm.edge_lab import validate_signal, walk_forward  # noqa: E402

BASE = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL, INTERVAL, TARGET = "BTCUSDT", "1h", 9000


def fetch() -> pd.DataFrame:
    out, end = [], None
    while len(out) < TARGET:
        params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1500}
        if end:
            params["endTime"] = end
        r = requests.get(BASE, params=params, timeout=25).json()
        if not isinstance(r, list) or not r:
            break
        out = r + out
        end = int(r[0][0]) - 1
        if len(r) < 1500:
            break
        time.sleep(0.3)
    # kline = [openTime,o,h,l,c,vol,closeTime,qvol,trades,takerBuyBase,takerBuyQuote,ign]
    rows = [{"timestamp": int(k[0]), "close": float(k[4]), "volume": float(k[5]),
             "taker_buy": float(k[9])} for k in out if float(k[5]) > 0]
    df = pd.DataFrame(rows).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    df["taker_buy_frac"] = df["taker_buy"] / df["volume"]   # REAL order-flow: fraction of volume taker-BUY
    return df


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("BTC Frontier-1: collecting REAL order-flow (Binance takerBuyBaseVolume)...")
    try:
        df = fetch()
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed ({str(e)[:80]}) — Binance fapi may be unreachable from here.")
        return
    if len(df) < 500:
        print(f"only {len(df)} bars fetched — insufficient.")
        return
    df.to_csv(DATA / "btc_orderflow.csv", index=False)
    print(f"collected {len(df)} bars ({pd.to_datetime(df['timestamp'].iloc[0], unit='ms').date()} → "
          f"{pd.to_datetime(df['timestamp'].iloc[-1], unit='ms').date()}) -> data/btc_orderflow.csv\n")

    c = df["close"].values
    cands = {
        "taker_buy_frac (level)":  df["taker_buy_frac"].values,
        "cvd_imbalance (2f-1)":    (2 * df["taker_buy_frac"] - 1).values,
        "taker_frac_chg24":        df["taker_buy_frac"].diff(24).values,
        "cvd_cum24 (rolling sum)": (2 * df["taker_buy_frac"] - 1).rolling(24).sum().values,
    }
    hdr = f"{'order-flow signal':26}{'dir':>11}{'OOS ROI':>9}{'b&h':>8}{'trades':>7}{'avg_bps':>9}{'pass':>6}{'robust':>8}"
    print(hdr)
    print("-" * len(hdr))
    winners = []
    for name, sig in cands.items():
        m = validate_signal(c, sig)
        if not m:
            print(f"{name:26}{'insufficient':>11}")
            continue
        wf = walk_forward(c, sig) or {}
        rob = bool(wf.get("consistent"))
        print(f"{name:26}{('contrarian' if m['contrarian'] else 'momentum'):>11}{m['oos_roi_pct']:>+8.1f}%"
              f"{m['buyhold_pct']:>+7.1f}%{m['trades']:>7}{m['avg_bps']:>+9.1f}{('✅' if m['passed'] else '—'):>6}"
              f"{('✅' if rob else '—'):>8}")
        if m["passed"] and rob:
            winners.append((name, m, wf))
    print("-" * len(hdr))
    if winners:
        for name, m, wf in winners:
            print(f"\n🟢 ORDER-FLOW EDGE on BTC: {name} → {m['oos_roi_pct']:+.1f}% OOS, avg {m['avg_bps']}bps, "
                  f"clears fee AND walk-forward-robust (ex-best {wf.get('ex_best_mean')}%). → CANDIDATE → scripts/60 confirms → on-chain.")
    else:
        print("\n⚪ No robust fee-clearing order-flow edge either. Honest: even REAL taker buy/sell pressure")
        print("   on BTC is efficiently priced at the 1h/24h horizon. Frontiers left: liquidation cascades,")
        print("   cross-exchange basis, on-chain netflows (richer data). btc_orderflow.csv saved for reuse.")


if __name__ == "__main__":
    main()
