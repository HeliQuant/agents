"""Expose the PER-TRADE ledger of the validated OI-contrarian edge (MNT).

This is NOT a new strategy or a new number. It runs the IDENTICAL methodology as
scripts/39_oi_backtest.py (non-overlap 24h windows, direction + quintile thresholds
derived ONLY from TRAIN, net of 2x taker fee) and simply records every individual
OOS position so we can SEE the decisions, not just the summary.

Sanity gate: the totals printed here MUST match validated_edges.json
(trades=34, win≈58.8%, OOS≈+28.9%). If they don't, something is wrong -> do not trust.

Run: python scripts/55_oi_trade_ledger.py [--capital 1000]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.00055  # exchange perp taker per side (same as scripts/39)
H = 24


def build_ledger(ticker: str = "MNT"):
    df = (
        pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    c = df["close"].values
    dt = df["datetime"].values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i])]
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]

    # direction + thresholds from TRAIN only (no test peeking)
    tr_sig = np.array([oichg[i] for i in tr])
    tr_ret = np.array([c[i + H] / c[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)

    trades = []
    eq = 1.0
    for i in te:
        s = oichg[i]
        if s >= p80:
            pos, why = (-1, "OI top-quintile -> fade") if contrarian else (1, "OI top -> follow")
        elif s <= p20:
            pos, why = (1, "OI bottom-quintile -> fade") if contrarian else (-1, "OI bottom -> follow")
        else:
            continue
        entry_px, exit_px = c[i], c[i + H]
        raw = exit_px / entry_px - 1
        net = pos * raw - 2 * FEE
        eq *= 1 + net
        trades.append(
            {
                "entry_dt": str(dt[i]),
                "exit_dt": str(dt[i + H]),
                "dir": "LONG" if pos == 1 else "SHORT",
                "entry_px": round(float(entry_px), 4),
                "exit_px": round(float(exit_px), 4),
                "oi_chg24_pct": round(float(s) * 100, 1),
                "raw_pct": round(float(raw) * 100, 2),
                "net_pct": round(float(net) * 100, 2),
                "win": bool(net > 0),
                "equity": round(float(eq), 4),
                "why": why,
            }
        )
    return {
        "ticker": ticker,
        "contrarian": bool(contrarian),
        "train_ic": round(float(ic), 3),
        "p20_oi_chg": round(float(p20) * 100, 1),
        "p80_oi_chg": round(float(p80) * 100, 1),
        "trades": trades,
        "final_equity": round(float(eq), 4),
        "oos_roi_pct": round((eq - 1) * 100, 2),
        "n": len(trades),
        "wins": sum(t["win"] for t in trades),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=1000.0, help="hypothetical starting USD (display only)")
    ap.add_argument("--ticker", default="MNT")
    args = ap.parse_args()

    L = build_ledger(args.ticker)
    cap = args.capital
    dirn = "CONTRARIAN (fade OI extremes)" if L["contrarian"] else "MOMENTUM"
    print(f"HeliQuant validated edge — per-trade ledger  |  {L['ticker']}  |  {dirn}")
    print(f"train IC {L['train_ic']:+.3f}  thresholds: OI-24h <= {L['p20_oi_chg']}% = LONG, >= {L['p80_oi_chg']}% = SHORT")
    print(f"net of fees ({FEE*100:.3f}%/side, round-trip ~{2*FEE*1e4:.0f} bps).  OOS only (unseen 40%).\n")

    hdr = f"{'#':>2} {'entry (UTC)':16} {'dir':5} {'entry':>8} {'exit':>8} {'OI24h':>7} {'net%':>7} {'W/L':>3} {'equity$':>9}"
    print(hdr)
    print("-" * len(hdr))
    for k, t in enumerate(L["trades"], 1):
        eq_usd = cap * t["equity"]
        wl = "W " if t["win"] else " L"
        print(
            f"{k:>2} {t['entry_dt'][:16]:16} {t['dir']:5} {t['entry_px']:>8.4f} {t['exit_px']:>8.4f} "
            f"{t['oi_chg24_pct']:>+6.1f}% {t['net_pct']:>+6.2f}% {wl:>3} {eq_usd:>9.2f}"
        )

    end_usd = cap * L["final_equity"]
    profit = end_usd - cap
    win_pct = L["wins"] / L["n"] * 100 if L["n"] else 0
    print("-" * len(hdr))
    print(f"\nDECISIONS: {L['n']} positions  |  WINS: {L['wins']} ({win_pct:.1f}%)  |  LOSSES: {L['n']-L['wins']}")
    print(f"MODAL (hypothetical): ${cap:,.2f}")
    print(f"AKHIR:                ${end_usd:,.2f}")
    print(f"UNTUNG (OOS, net fee): ${profit:+,.2f}  ({L['oos_roi_pct']:+.2f}%)")

    # sanity vs the authoritative registry
    reg = json.loads((ROOT / "data" / "validated_edges.json").read_text())
    v = reg.get(L["ticker"].upper())
    if v:
        ok = (v["sample_n"] == L["n"]) and abs(v["oos_roi_pct"] - L["oos_roi_pct"]) < 0.5
        print(f"\nsanity vs validated_edges.json: n {v['sample_n']}=={L['n']}, "
              f"ROI {v['oos_roi_pct']}% vs {L['oos_roi_pct']}%  ->  {'MATCH ✓' if ok else 'MISMATCH ✗ (do not trust)'}")

    out = ROOT / "data" / "oi_trade_ledger_mnt.json"
    out.write_text(json.dumps(L, indent=2))
    print(f"\nwrote ledger -> {out.name}")


if __name__ == "__main__":
    main()
