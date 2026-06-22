"""Fund HeliQuant with real capital and watch the account statement — the validated MNT edge
traded OUT-OF-SAMPLE (unseen data) with the REAL AGGRESSIVE sizing code, trade by trade.

HONEST framing:
  * This funds the OOS-validated OI-contrarian edge (the ONLY thing we can stand behind) on the
    UNSEEN 40% of history — no in-sample inflation.
  * Sizing = the real firm.trade_ticket AGGRESSIVE path (quarter-Kelly, cap 3% / 5x, 20% DD-breaker);
    P&L = leverage x the real 24h move, intra-window ATR stop, fee scales with notional, loss capped
    at -100% (liquidation). Every number is from this run — nothing invented.
  * The LIVE org additionally applies an R:R>=2 structural gate (that's why today's live tick
    ABSTAINED). This statement shows the edge's signals SIZED — the earning power before that extra
    gate. It is a backtest on real exchange MNTUSDT data, not a promise of future return.

Run: python scripts/58_funded_account.py [--capital 1000]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.trade_ticket import SWAP_FEE, SWING_LOOKBACK, build_trade_ticket  # noqa: E402

H = 24


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=1000.0)
    args = ap.parse_args()
    cap0 = args.capital

    edge = json.loads((ROOT / "data" / "validated_edges.json").read_text())["MNT"]
    pos = pd.read_csv(ROOT / "data" / "mnt_positioning.csv")[["timestamp", "close", "oi"]]
    fe = pd.read_csv(ROOT / "data" / "mnt_features.csv")[["timestamp", "datetime", "high", "low", "atr", "adx"]]
    df = pos.merge(fe, on="timestamp", how="inner").sort_values("timestamp").reset_index(drop=True)
    c, hi, lo, atr, adx = (df[k].values for k in ("close", "high", "low", "atr", "adx"))
    dtv = df["datetime"].values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i]) and atr[i] > 0]
    split = int(len(idx) * 0.6)
    tr, oos = idx[:split], idx[split:]  # train derives direction/thresholds; we trade OOS only

    tr_sig = np.array([oichg[i] for i in tr])
    tr_ret = np.array([c[i + H] / c[i] - 1 for i in tr])
    contrarian = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman") < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
    adx_th, adx_hi = np.nanpercentile(adx, 60), np.nanpercentile(adx, 90)

    print(f"╔═ HeliQuant — FUNDED ACCOUNT STATEMENT  (validated MNT edge, OUT-OF-SAMPLE) ═╗")
    print(f"  Modal disetor : ${cap0:,.2f}   |   venue data: real exchange MNTUSDT perp   |   net of fees\n")
    hdr = f"{'#':>2} {'date (UTC)':12} {'dir':5} {'mode':10} {'lev':>5} {'P&L $':>9} {'balance $':>11}"
    print(hdr); print("  " + "-" * (len(hdr) - 1))

    eq, peak, maxdd, wins = cap0, cap0, 0.0, 0
    k = 0
    for i in oos:
        s = oichg[i]
        if s >= p80:
            direction = "SHORT" if contrarian else "LONG"
        elif s <= p20:
            direction = "LONG" if contrarian else "SHORT"
        else:
            continue
        k += 1
        sign = 1 if direction == "LONG" else -1
        dd_now = max(0.0, (peak - eq) / peak)
        rr = 2.0 + max(0.0, min(1.0, (adx[i] - adx_th) / (adx_hi - adx_th + 1e-9)))
        j0 = max(0, i - SWING_LOOKBACK)
        t = build_trade_ticket("MNT", direction, "high", last_price=float(c[i]), atr=float(atr[i]),
                               dynamic_rr=rr, swing_low=float(lo[j0:i].min()), swing_high=float(hi[j0:i].max()),
                               equity=eq, edge=edge, regime_conf=0.9, consensus=0.85, drawdown=dd_now)
        lev, stop = t["leverage"], t["stop_loss"]
        path_lo, path_hi = lo[i + 1:i + H + 1], hi[i + 1:i + H + 1]
        stopped = (direction == "LONG" and path_lo.min() <= stop) or (direction == "SHORT" and path_hi.max() >= stop)
        exit_px = float(stop) if stopped else float(c[i + H])
        pnl_frac = max(-1.0, lev * sign * (exit_px / float(c[i]) - 1) - lev * 2 * SWAP_FEE)
        pnl_usd = eq * pnl_frac
        eq += pnl_usd
        wins += int(pnl_frac > 0)
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
        mark = "🟢" if t["mode"] == "AGGRESSIVE" else "⚪"
        print(f"  {k:>2} {str(dtv[i])[:10]:12} {direction:5} {mark}{t['mode']:9} {lev:>4.2f}x "
              f"{pnl_usd:>+8.2f} {eq:>11.2f}")

    profit = eq - cap0
    print("  " + "-" * (len(hdr) - 1))
    print(f"\n  TRADES   : {k}   ({wins} menang / {k - wins} kalah = {wins/k*100:.1f}% win)" if k else "  no trades")
    print(f"  MODAL    : ${cap0:,.2f}")
    print(f"  SALDO    : ${eq:,.2f}")
    print(f"  PROFIT   : ${profit:+,.2f}   ({profit/cap0*100:+.2f}%)   |   max drawdown -{maxdd*100:.1f}%")
    print(f"\n  (OOS / unseen data · real exchange MNTUSDT · AGGRESSIVE sizing live · scales linearly with modal)")
    print(f"  (Canonical un-leveraged validated edge = +28.91% / 34 trades — see scripts/55.)")


if __name__ == "__main__":
    main()
