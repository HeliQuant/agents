"""Is the OI-change signal actually TRADEABLE net of fees? (IC said MNT oi_chg24 contrarian, big+stable)

Honest, overfit-resistant:
  * NON-OVERLAPPING 24h windows (entry every 24 bars) -> independent samples, no overlap inflation.
  * Direction + quintile thresholds derived ONLY from TRAIN (no test peeking).
  * Rule: oi_chg24 in top quintile -> short (if contrarian) / long (if momentum); bottom quintile -> opposite; else flat.
  * Net return = pos*raw_return - 2*taker_fee (exchange perp taker ~0.055%/side). Funding ignored (≈neutral/favorable for contrarian shorts).
  * Report OOS net ROI vs buy&hold over the SAME span.

Run: python scripts/39_oi_backtest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ["MNT", "BTC", "ETH", "SOL"]
FEE = 0.00055  # taker per side
H = 24


def backtest(ticker):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i])]
    if len(idx) < 60:
        return None
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]

    tr_sig = np.array([oichg[i] for i in tr])
    tr_ret = np.array([c[i + H] / c[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)

    eq, trades, wins, rets = 1.0, 0, 0, []
    for i in te:
        s = oichg[i]
        raw = c[i + H] / c[i] - 1
        if s >= p80:
            pos = -1 if contrarian else 1
        elif s <= p20:
            pos = 1 if contrarian else -1
        else:
            continue
        net = pos * raw - 2 * FEE
        eq *= (1 + net); trades += 1; wins += int(net > 0); rets.append(net)
    bh = c[te[-1] + H] / c[te[0]] - 1
    wins_r = [r for r in rets if r > 0]
    loss_r = [r for r in rets if r <= 0]
    avg_win = float(np.mean(wins_r)) if wins_r else 0.0
    avg_loss = float(np.mean(loss_r)) if loss_r else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else 0.0
    return {
        "train_ic": ic, "dir": "contrarian" if contrarian else "momentum",
        "oos_roi": (eq - 1) * 100, "trades": trades,
        "win": (wins / trades * 100) if trades else 0,
        "p_win": (wins / trades) if trades else 0.0, "payoff_b": round(payoff, 3),
        "avg_win_bps": round(avg_win * 1e4, 1), "avg_loss_bps": round(avg_loss * 1e4, 1),
        "avg_bps": (np.mean(rets) * 1e4) if rets else 0, "bh": bh * 100,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(f"OI-change cost-aware backtest (non-overlap {H}h, fee {FEE*100:.3f}%/side, train-derived dir+thresholds)\n")
    print(f"{'asset':6} {'train_IC':>9} {'dir':>11} {'OOS net ROI':>12} {'trades':>7} {'win%':>6} {'payoff':>7} {'avg bps':>8} {'buy&hold':>9}")
    results = {}
    for t in ASSETS:
        r = backtest(t)
        results[t] = r
        if not r:
            print(f"{t:6} insufficient"); continue
        print(f"{t:6} {r['train_ic']:+9.3f} {r['dir']:>11} {r['oos_roi']:+11.2f}% {r['trades']:7} "
              f"{r['win']:6.1f} {r['payoff_b']:7.2f} {r['avg_bps']:+8.1f} {r['bh']:+8.2f}%")
    print("\nRead: OOS net ROI > 0 AND > buy&hold AND avg_bps comfortably > 0 (covers slippage) = tradeable.")
    print("avg_bps near/below ~11 (the round-trip fee) = signal eaten by costs.")

    # Persist edges that clear the honest tradeable bar -> consumed by the sizing layer's AGGRESSIVE gate.
    rt_fee_bps = 2 * FEE * 1e4
    edges = {}
    for t, r in results.items():
        if r and (r["dir"] == "contrarian" and r["oos_roi"] > 0 and r["oos_roi"] > r["bh"]
                  and r["avg_bps"] > rt_fee_bps and r["payoff_b"] > 0 and r["trades"] >= 10):
            edges[t.upper()] = {
                "edge": "oi_contrarian", "asset": t.upper(), "validated": True,
                "p_win": round(r["p_win"], 4), "payoff_b": r["payoff_b"], "sample_n": r["trades"],
                "oos_roi_pct": round(r["oos_roi"], 2),
                "note": ("OI-contrarian, OOS (non-overlap 24h, cost-aware). Hedge-like + bear-amplified "
                         "+ small-sample -> caveated; sizing still gates on sample_n>=20."),
            }
    out = ROOT / "data" / "validated_edges.json"
    out.write_text(json.dumps(edges, indent=2))
    print(f"\nwrote {len(edges)} validated edge(s) -> {out.name}")
    for k, v in edges.items():
        print(f"  {k}: p_win={v['p_win']} payoff_b={v['payoff_b']} n={v['sample_n']} (OOS {v['oos_roi_pct']}%)")


if __name__ == "__main__":
    main()
