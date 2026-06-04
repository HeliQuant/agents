"""BTC Frontier-2: cross-exchange BASIS edge hunt. Fetch Hyperliquid BTC perp (the friend's tip — a 3rd
venue, permissionless) and align with Binance (already collected in btc_orderflow.csv). Test whether the
HL-vs-Binance basis dislocation mean-reverts as a MARKET-NEUTRAL pairs trade, under the same gate
(cost-aware OOS + walk-forward). Note: pairs = TWO legs → DOUBLE fees (~22 bps round-trip).

Honest: find a robust fee-clearing basis edge, or confirm perp-perp basis is arbitraged away (expected:
cross-exchange basis on BTC is the most-arbed quantity in crypto → likely tiny). Hyperliquid is ALSO our
permissionless EXECUTION venue (solves the Bybit geo-block) — valuable regardless of the test outcome.

Run: python scripts/72_btc_basis.py
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
HL = "https://api.hyperliquid.xyz/info"
FEE = 0.00055      # per side, per leg
H = 24


def fetch_hl(start_ms: int, end_ms: int) -> pd.DataFrame:
    """Hyperliquid 1h candles for BTC (paginate; ~5000/req max)."""
    out, cur = [], start_ms
    while cur < end_ms:
        body = {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h",
                                                  "startTime": cur, "endTime": end_ms}}
        r = requests.post(HL, json=body, timeout=25).json()
        if not isinstance(r, list) or not r:
            break
        out += r
        last = int(r[-1]["t"])
        if last <= cur or len(r) < 2:
            break
        cur = last + 1
        time.sleep(0.3)
    rows = [{"timestamp": int(k["t"]), "hl_close": float(k["c"])} for k in out]
    return pd.DataFrame(rows).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    bf = DATA / "btc_orderflow.csv"
    if not bf.exists():
        print("need data/btc_orderflow.csv (Binance) first — run scripts/71.")
        return
    binance = pd.read_csv(bf)[["timestamp", "close"]].rename(columns={"close": "bin_close"})
    print("BTC Frontier-2: fetching Hyperliquid BTC perp (friend's tip)...")
    try:
        hl = fetch_hl(int(binance["timestamp"].iloc[0]), int(binance["timestamp"].iloc[-1]))
    except Exception as e:  # noqa: BLE001
        print(f"Hyperliquid fetch failed ({str(e)[:80]}).")
        return
    if len(hl) < 500:
        print(f"only {len(hl)} HL bars — insufficient.")
        return
    df = binance.merge(hl, on="timestamp", how="inner").sort_values("timestamp").reset_index(drop=True)
    df.to_csv(DATA / "btc_basis.csv", index=False)
    basis_bps = ((df["hl_close"] - df["bin_close"]) / df["bin_close"] * 1e4)
    print(f"aligned {len(df)} hourly bars (HL ∩ Binance).")
    print(f"HL-vs-Binance basis: mean {basis_bps.mean():+.2f} bps · avg |basis| {basis_bps.abs().mean():.2f} bps · "
          f"std {basis_bps.std():.2f} bps  (round-trip pairs fee ~{4*FEE*1e4:.0f} bps)\n")

    # market-neutral PAIRS test: fade the basis. spread_ret_24h = hl_ret - bin_ret.
    bn, hlc = df["bin_close"].values, df["hl_close"].values
    sig = basis_bps.values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(sig[i])]
    if len(idx) < 50:
        print("insufficient non-overlap windows.")
        return
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    # direction from train: does high basis precede negative spread_ret (convergence)?
    tr_sig = np.array([sig[i] for i in tr])
    tr_spread = np.array([(hlc[i + H] / hlc[i] - 1) - (bn[i + H] / bn[i] - 1) for i in tr])
    contr = pd.Series(tr_sig).corr(pd.Series(tr_spread), method="spearman") < 0
    plo, phi = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
    rets = []
    for i in te:
        s = sig[i]
        pos = (-1 if contr else 1) if s >= phi else (1 if contr else -1) if s <= plo else 0
        if pos == 0:
            continue
        spread = (hlc[i + H] / hlc[i] - 1) - (bn[i + H] / bn[i] - 1)
        rets.append(pos * spread - 4 * FEE)   # 2 legs in + out = 4x fee
    if len(rets) < 20:
        print(f"only {len(rets)} pairs trades — insufficient.")
        return
    eq = float(np.prod([1 + r for r in rets]))
    avg_bps = float(np.mean(rets) * 1e4)
    print(f"PAIRS (fade basis, market-neutral, 2 legs): OOS {(eq-1)*100:+.2f}% · {len(rets)} trades · "
          f"avg {avg_bps:+.1f} bps/trade · pairs-fee bar {4*FEE*1e4:.0f} bps")
    clears = avg_bps > 4 * FEE * 1e4 and eq > 1
    print(f"\n{'🟢 clears fee — worth walk-forward' if clears else '⚪ does NOT clear pairs fee'}: "
          f"{'basis edge candidate' if clears else 'cross-exchange basis is arbitraged away (as expected) — ruled out'}.")
    if not clears:
        print("   Honest: perp-perp basis on BTC is tiny + the 2-leg fee (~22 bps) buries it. Hyperliquid still")
        print("   onboarded as a DATA venue (btc_basis.csv) + our permissionless EXECUTION venue (no geo-block).")


if __name__ == "__main__":
    main()
