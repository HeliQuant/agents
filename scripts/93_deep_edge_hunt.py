"""
scripts/93_deep_edge_hunt.py — the needle-in-the-haystack search.

Widens the self-learning hunt along the dimensions edge_lab hasn't swept yet:
  • EVERY asset that has positioning data (not just BTC/MNT)
  • COMPOSITE / interaction signals on top of the 4 base sources:
      oi_price_div  — OI building AGAINST price (positioning divergence)
      funding_dev   — funding's deviation from its own weekly norm (crowding, not raw level)
      flow_x_oi     — order-flow imbalance CONFIRMED by OI change
  • MULTIPLE horizons (4/6/8/12/24/48h)
…all under the EXACT same rigorous gate as the registry: extreme-gated (p20/p80, train-only),
cost-aware OOS, per-asset Benjamini-Hochberg FDR across every (source×horizon) test, then
walk-forward + stability on any survivor. More tests ⇒ stricter FDR — that's the point.

If a needle clears it, aggression is EARNED (deploy + size up on THAT, per asset). If nothing
clears, the firm keeps abstaining — and we report it straight.
"""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import edge_lab as el  # noqa: E402

HORIZONS = [4, 6, 8, 12, 24, 48]
DATA = ROOT / "data"
_orig_H = el.H


def _composites():
    """Extra signal sources (read el.H at call time). Each: df -> pd.Series."""
    return {
        "oi_price_div": lambda df: df["oi"].pct_change(el.H) - df["close"].pct_change(el.H),
        "funding_dev": lambda df: df["funding"] - df["funding"].rolling(168).mean(),
        "flow_x_oi": lambda df: (df["buy_ratio"] - 0.5) * df["oi"].pct_change(el.H),
    }


def hunt(asset: str) -> list[dict]:
    fp = DATA / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return []
    df = pd.read_csv(fp).sort_values("timestamp").reset_index(drop=True)
    if "close" not in df or len(df) < 400:
        return []
    close = df["close"].values
    sources = dict(el.SIGNAL_SOURCES)
    for k, fn in _composites().items():
        if {"oi", "funding", "buy_ratio", "close"} <= set(df.columns):
            sources[k] = fn
    rows = []
    for h in HORIZONS:
        el.H = h
        for src, fn in sources.items():
            try:
                sig = fn(df).values
            except Exception:  # noqa: BLE001
                continue
            if np.isfinite(sig).sum() < 80 or len(np.unique(sig[np.isfinite(sig)])) < 6:
                continue
            m = el.validate_signal(close, sig)
            if m:
                rows.append({"asset": asset.upper(), "h": h, "src": src,
                             "oos": m["oos_roi_pct"], "pval": m["pval"], "passed": m["passed"],
                             "contra": m["contrarian"], "trades": m["trades"]})
    fdr = el.benjamini_hochberg({f"{r['h']}:{r['src']}": r["pval"] for r in rows}, alpha=0.10)
    for r in rows:
        r["fdr"] = bool(fdr.get(f"{r['h']}:{r['src']}", False))
    return rows


def main() -> None:
    assets = sorted({p.stem.replace("_positioning", "").upper()
                     for p in DATA.glob("*_positioning.csv")})
    print(f"DEEP EDGE HUNT · {len(assets)} assets × up to {4 + len(_composites())} sources × {len(HORIZONS)} horizons")
    print(f"assets: {', '.join(assets)}\n")
    survivors = []
    for a in assets:
        rows = hunt(a)
        passed_oos = [r for r in rows if r["passed"]]
        cleared = [r for r in rows if r["passed"] and r["fdr"]]
        survivors += cleared
        best = max(rows, key=lambda r: r["oos"]) if rows else None
        top = max(passed_oos, key=lambda r: r["oos"]) if passed_oos else None
        line = (f"{a:<6} {len(rows):>3} tests · {len(passed_oos)} pass-OOS · {len(cleared)} clear-FDR"
                f" · best {best['src'] if best else '-'}@{best['h'] if best else '-'}h {best['oos'] if best else 0:+.0f}%")
        if top:
            line += f" · topOOS {top['src']}@{top['h']}h {top['oos']:+.0f}% p={top['pval']:.3f}"
        print(line)

    print("\n---- FDR-cleared candidates (would earn a slot if walk-forward robust) ----")
    if not survivors:
        print("  NONE — no edge clears the cost-aware OOS + FDR gate on any asset/source/horizon.")
        print("  Honest verdict: no validated needle. The firm keeps abstaining; aggression stays unearned.")
    else:
        for r in survivors:
            el.H = r["h"]
            df = pd.read_csv(DATA / f"{r['asset'].lower()}_positioning.csv").sort_values("timestamp").reset_index(drop=True)
            src_fn = dict(el.SIGNAL_SOURCES, **_composites())[r["src"]]
            wf = el.walk_forward(df["close"].values, src_fn(df).values)
            robust = bool(wf and wf.get("consistent"))
            print(f"  {r['asset']} {r['src']}@{r['h']}h {'contra' if r['contra'] else 'mom'}: "
                  f"OOS {r['oos']:+.1f}% p={r['pval']:.3f} trades={r['trades']} · "
                  f"walk-forward {'ROBUST -> EARNS aggression' if robust else 'NOT robust -> hold'}")
    el.H = _orig_H
    print("\n(same gate as the registry — extreme-gated p20/p80, cost-aware OOS, FDR, walk-forward)")


if __name__ == "__main__":
    main()
