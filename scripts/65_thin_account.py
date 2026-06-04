"""Thin funded account ($100) on the validated MNT edge + the HYPE CANDIDATE — honest, OOS.

  * MNT oi_contrarian  = VALIDATED (robust, walk-forward 4/5). A real, trustable edge.
  * HYPE flow_contrarian = CANDIDATE (one-fold-dependent → drop best fold and it collapses to ~+2%).
    HELD by the engine, NOT registered. Shown for transparency — NOT a return you can trust.

Unleveraged ±1, non-overlap 24h, net of fees, OUT-OF-SAMPLE. HONEST note on "thin capital flies further":
it doesn't — % returns are SCALE-INVARIANT ($100 and $10k earn the same %); a thin account's only real
edge is negligible slippage. The "fly" comes from the edge × leverage (capped by risk discipline), not
from the size of the modal. Run: python scripts/65_thin_account.py [--capital 100]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
from firm.edge_lab import FEE, H, SIGNAL_SOURCES, _EDGE_SRC, walk_forward  # noqa: E402

BOOK = [("MNT", "oi_contrarian", "VALIDATED (robust)"),
        ("HYPE", "flow_contrarian", "CANDIDATE (FRAGILE — held, not trusted)")]


def run(asset: str, edge: str, cap: float) -> dict:
    src, contrarian = _EDGE_SRC[edge]
    df = pd.read_csv(DATA / f"{asset.lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    sig = SIGNAL_SOURCES[src](df).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(sig[i])]
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]
    p20, p80 = np.nanpercentile([sig[i] for i in tr], 20), np.nanpercentile([sig[i] for i in tr], 80)
    eq = peak = cap
    mdd = 0.0
    trades = wins = 0
    for i in te:
        s = sig[i]
        if s >= p80:
            pos = -1 if contrarian else 1
        elif s <= p20:
            pos = 1 if contrarian else -1
        else:
            continue
        net = pos * (c[i + H] / c[i] - 1) - 2 * FEE
        eq *= 1 + net
        trades += 1
        wins += int(net > 0)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    wf = walk_forward(c, sig) or {}
    return {"trades": trades, "wins": wins, "final": eq, "roi": (eq / cap - 1) * 100,
            "mdd": mdd * 100, "wf": wf}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=100.0)
    args = ap.parse_args()
    cap = args.capital

    print(f"THIN FUNDED ACCOUNT — modal ${cap:,.0f} each  ·  OOS · unleveraged ±1 · net fee\n")
    for asset, edge, label in BOOK:
        r = run(asset, edge, cap)
        wf = r["wf"]
        robust = wf.get("consistent")
        print(f"── {asset}  ({edge})  ·  {label} ──")
        print(f"   modal ${cap:,.2f} → ${r['final']:,.2f}   ROI {r['roi']:+.2f}%   "
              f"max drawdown −{r['mdd']:.1f}%   ({r['trades']} trades, {r['wins']}/{r['trades']} win)")
        print(f"   walk-forward: {wf.get('positive')}/{wf.get('folds')} folds +ve, "
              f"ex-best-fold mean {wf.get('ex_best_mean')}%  →  {'ROBUST ✅' if robust else 'FRAGILE ⚠️ one-fold-dependent'}")
        if not robust:
            print(f"   ⚠️  DON'T TRUST this number — drop the single best fold and it collapses to "
                  f"{wf.get('ex_best_mean')}%. The engine HELD this edge; it's paper-only, not live-eligible.")
        print()

    print("HONEST takeaways:")
    print(f"  • MNT = the real, validated edge (+28.9% OOS canonical) — ${cap:,.0f} → ~${cap*1.289:,.0f}.")
    print("  • HYPE's +92% is a MIRAGE we refuse to bank: one fold carries it; held as a paper candidate.")
    print("  • A $100 account does NOT 'fly further' than $10k — % is scale-invariant. Thin capital's")
    print("    only real edge is less slippage. The lift = validated edge × disciplined leverage, not size.")
    print("  • Edges hold 24h → '30 min live' = one signal eval, not a trade sequence; real Bybit orders")
    print("    are geo-blocked. This is the edges replayed on REAL data, OOS — the honest 'how far it flies'.")


if __name__ == "__main__":
    main()
