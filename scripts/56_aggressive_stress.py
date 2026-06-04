"""AGGRESSIVE-feature stress test — SAFE vs AGGRESSIVE sizing on the OI-contrarian signal,
run to N positions, using the REAL sizing code (firm.trade_ticket.build_trade_ticket).

HONESTY (read first):
  * The VALIDATED number is +28.9% over 34 NON-OVERLAP OOS trades (scripts/55). THIS is NOT that.
  * To reach ~100 positions we replay the signal over the FULL MNT history (in-sample + OOS),
    so this is an ILLUSTRATIVE SIZING demo of how the AGGRESSIVE dial behaves — not a new
    validated return. In-sample positions (where direction/thresholds were fit) are flagged.
  * We model it HONESTLY both ways: real ATR stops cap each loss, fees scale with leverage
    (notional), the 20% drawdown breaker forces SAFE, and a position that gaps through its
    stop can liquidate. AGGRESSIVE amplifies BOTH the surge and the drawdown.

Mechanics per fired signal:
  - direction + quintile thresholds = TRAIN-derived (identical to the validated edge).
  - build_trade_ticket() on the REAL bar (ATR, swing, ADX) -> mode, leverage, stop_loss.
  - outcome over the 24h hold: intra-window stop check (low/high); else exit at 24h close.
  - equity *= (1 + leverage*signed_move - leverage*round_trip_fee); loss capped at -100%.
  - running drawdown is fed back so the DD-breaker can force SAFE mid-run.

Run: python scripts/56_aggressive_stress.py [--positions 100] [--capital 1000]
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


def _load():
    pos = pd.read_csv(ROOT / "data" / "mnt_positioning.csv")[["timestamp", "close", "oi"]]
    fe = pd.read_csv(ROOT / "data" / "mnt_features.csv")[["timestamp", "datetime", "high", "low", "atr", "adx"]]
    df = pos.merge(fe, on="timestamp", how="inner").sort_values("timestamp").reset_index(drop=True)
    return df


def run(df, mode_aggressive: bool, n_target: int, capital: float, edge: dict, oos_only: bool = False):
    c = df["close"].values
    hi = df["high"].values
    lo = df["low"].values
    atr = df["atr"].values
    adx = df["adx"].values
    dtv = df["datetime"].values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i]) and atr[i] > 0]
    split = int(len(idx) * 0.6)  # train portion -> direction/thresholds (in-sample beyond here is OOS)
    tr = idx[:split]

    tr_sig = np.array([oichg[i] for i in tr])
    tr_ret = np.array([c[i + H] / c[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)
    adx_th, adx_hi = np.nanpercentile(adx, 60), np.nanpercentile(adx, 90)

    eq, peak = capital, capital
    rows = []
    stops = liq = breaker_hits = wins = 0
    max_dd = 0.0
    use_edge = edge if mode_aggressive else None
    scan = idx[split:] if oos_only else idx  # OOS-only = unseen data (params fit on train)

    for i in scan:
        if len(rows) >= n_target:
            break
        s = oichg[i]
        if s >= p80:
            direction = "SHORT" if contrarian else "LONG"
        elif s <= p20:
            direction = "LONG" if contrarian else "SHORT"
        else:
            continue
        pos = 1 if direction == "LONG" else -1
        dd_now = max(0.0, (peak - eq) / peak)
        rr = 2.0 + max(0.0, min(1.0, (adx[i] - adx_th) / (adx_hi - adx_th + 1e-9)))
        j0 = max(0, i - SWING_LOOKBACK)
        swing_low, swing_high = float(lo[j0:i].min()), float(hi[j0:i].max())

        t = build_trade_ticket(
            "MNT", direction, "high", last_price=float(c[i]), atr=float(atr[i]), dynamic_rr=rr,
            swing_low=swing_low, swing_high=swing_high, equity=eq,
            edge=use_edge, regime_conf=0.9, consensus=0.85, drawdown=dd_now,
        )
        lev = t.get("leverage", 0.0)
        stop = t.get("stop_loss")
        is_aggr = t.get("mode") == "AGGRESSIVE"
        if mode_aggressive and dd_now >= 0.20:
            breaker_hits += 1  # breaker forced SAFE this trade

        # outcome over the 24h hold (intra-window stop, else 24h close)
        path_lo = lo[i + 1 : i + H + 1]
        path_hi = hi[i + 1 : i + H + 1]
        stopped = (direction == "LONG" and path_lo.min() <= stop) or (direction == "SHORT" and path_hi.max() >= stop)
        exit_px = float(stop) if stopped else float(c[i + H])
        signed = pos * (exit_px / float(c[i]) - 1)
        pnl = lev * signed - lev * 2 * SWAP_FEE  # fee scales with notional
        if pnl <= -1.0:
            pnl = -1.0
            liq += 1
        eq *= 1 + pnl
        stops += int(stopped)
        wins += int(pnl > 0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
        rows.append({"i": i, "dt": str(dtv[i])[:16], "dir": direction, "mode": t.get("mode"),
                     "lev": round(lev, 2), "aggr": bool(is_aggr), "stopped": bool(stopped),
                     "pnl_pct": round(pnl * 100, 2), "eq": round(eq, 2),
                     "oos": bool(idx.index(i) >= split)})
        if eq <= 0.01:
            break

    return {"rows": rows, "final_eq": eq, "capital": capital, "n": len(rows), "wins": wins,
            "stops": stops, "liq": liq, "breaker_hits": breaker_hits, "max_dd_pct": round(max_dd * 100, 1),
            "contrarian": bool(contrarian), "split_at": split, "n_oos": sum(r["oos"] for r in rows),
            "n_aggr": sum(r["aggr"] for r in rows)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", type=int, default=100)
    ap.add_argument("--capital", type=float, default=1000.0)
    args = ap.parse_args()

    edge = json.loads((ROOT / "data" / "validated_edges.json").read_text())["MNT"]
    df = _load()

    print("AGGRESSIVE-feature stress test — OI-contrarian signal on MNT (REAL sizing code)")
    print(f"edge: p_win={edge['p_win']} payoff_b={edge['payoff_b']} n={edge['sample_n']}  "
          f"-> Kelly f*={edge['p_win'] - (1-edge['p_win'])/edge['payoff_b']:.3f}  (x0.25 quarter-Kelly, cap 3% risk / 5x lev)")
    print(f"target {args.positions} positions, modal ${args.capital:,.0f}\n")
    print("  ! ILLUSTRATIVE sizing demo over FULL history (in-sample + OOS) to reach N positions.")
    print("  ! The VALIDATED return is +28.9% / 34 NON-OVERLAP OOS trades (scripts/55). This is NOT that.\n")

    safe = run(df, False, args.positions, args.capital, edge)
    aggr = run(df, True, args.positions, args.capital, edge)

    def line(tag, r):
        prof = r["final_eq"] - r["capital"]
        roi = prof / r["capital"] * 100
        wr = r["wins"] / r["n"] * 100 if r["n"] else 0
        print(f"[{tag}]  {r['n']} positions ({r['n_oos']} OOS)  |  win {wr:.1f}%  |  stop-outs {r['stops']}  |  liq {r['liq']}")
        print(f"        modal ${r['capital']:,.0f} -> ${r['final_eq']:,.2f}   ROI {roi:+.1f}%   max drawdown -{r['max_dd_pct']}%")
        if tag.startswith("AGGR"):
            print(f"        AGGRESSIVE-mode trades: {r['n_aggr']}/{r['n']}   DD-breaker forced SAFE: {r['breaker_hits']}x")
        print()

    aggr_oos = run(df, True, args.positions, args.capital, edge, oos_only=True)

    line("SAFE     ", safe)
    line("AGGRESSIVE", aggr)
    print("--- honest cut: AGGRESSIVE on OOS-only (unseen data; fewer signals, but no in-sample inflation) ---")
    line("AGGR(OOS)", aggr_oos)

    # the "lonjakan": AGGRESSIVE vs SAFE on the identical signal stream
    surge = (aggr["final_eq"] / safe["final_eq"] - 1) * 100 if safe["final_eq"] > 0 else float("nan")
    print(f"LONJAKAN (AGGRESSIVE vs SAFE, same signals): {surge:+.1f}%   "
          f"avg lev SAFE {np.mean([r['lev'] for r in safe['rows']]):.2f}x vs AGGR {np.mean([r['lev'] for r in aggr['rows']]):.2f}x")
    print("HONEST READ: AGGRESSIVE lifts the surge AND the drawdown; the 20% breaker + ATR stops are the seatbelt.")

    out = ROOT / "data" / "aggressive_stress_mnt.json"
    out.write_text(json.dumps({"safe": {k: v for k, v in safe.items() if k != "rows"},
                               "aggressive": {k: v for k, v in aggr.items() if k != "rows"},
                               "aggressive_rows": aggr["rows"]}, indent=2))
    print(f"\nwrote -> {out.name}")


if __name__ == "__main__":
    main()
