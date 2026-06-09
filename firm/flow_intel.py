"""firm/flow_intel.py — self-learning FLOW-INTELLIGENCE core (foundation of the big on-chain/flow desk).

The honest version of "agents that really learn from the market": the LEARNING is rigorous validation,
not vibes. This desk runs the full flow-signal suite (OI change, funding, order-flow imbalance, price
momentum) on an asset's real history, and for EACH signal learns — via the same cost-aware OOS gate as
edge_lab — whether it actually predicts the next move and in which direction. It then reads the CURRENT
value of each signal and synthesizes a flow stance WEIGHTED by what has proven reliable. Signals that don't
predict get ~zero weight (honest: most don't). Real-time-only reads (Nansen smart-money, whale divergence)
plug in as additional voices that earn weight FORWARD via desk_performance outcome-logging.

This is the substrate the org's on-chain/smart-money desks (the 3-4 LLM analysts) interpret — they argue
over the SAME learned, validated signals instead of guessing. v1 = the validatable flow suite + synthesis.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import firm.edge_lab as el  # noqa: E402

# the flow/on-chain-derived signals we can VALIDATE on stored history (edge_lab's suite)
FLOW_SIGNALS = ["oi_chg24", "funding", "flow_imbalance", "price_mom24"]
_LABEL = {"oi_chg24": "OI change (positioning)", "funding": "funding (leverage bias)",
          "flow_imbalance": "order-flow imbalance (taker buy/sell)", "price_mom24": "price momentum"}


def learn_reliable(asset: str) -> dict:
    """For each flow signal, LEARN (cost-aware OOS) whether it predicts + its direction. The real 'learning'.
    Returns {signal: {validates, oos_roi_pct, trades, direction, reliability 0..1}}."""
    fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
    out: dict = {}
    if not fp.exists():
        return out
    df = pd.read_csv(fp)
    close = df["close"].values
    metrics: dict = {}
    for s in FLOW_SIGNALS:
        try:
            sig = el.SIGNAL_SOURCES[s](df).values
            metrics[s] = el.validate_signal(close, sig)
        except Exception:  # noqa: BLE001
            metrics[s] = None
    # FDR correction (Benjamini-Hochberg) across ALL signals tested here: testing 4 signals at once means
    # one can look great by luck, so a signal must clear BOTH its own cost-aware OOS gate AND BH — caps the
    # false-discovery rate, the multiple-testing trap that sinks naive "best-of-N" backtests.
    fdr = el.benjamini_hochberg({s: m["pval"] for s, m in metrics.items() if m}, alpha=0.10)
    for s in FLOW_SIGNALS:
        m = metrics[s]
        if not m:
            out[s] = {"validates": False, "reliability": 0.0, "note": "insufficient data"}
            continue
        validates = bool(m["passed"] and fdr.get(s, False))
        # reliability: only signals surviving OOS + FDR earn weight; scaled by net OOS ROI (capped)
        rel = round(min(1.0, max(0.0, m["oos_roi_pct"] / 30.0)), 2) if validates else 0.0
        out[s] = {"validates": validates, "oos_roi_pct": m["oos_roi_pct"], "trades": m["trades"],
                  "direction": "contrarian" if m.get("contrarian") else "momentum", "reliability": rel,
                  "pval": m.get("pval"), "fdr_pass": bool(fdr.get(s, False))}
    return out


def current_reads(asset: str) -> dict:
    """Current value + extremity (z-score over trailing window) of each flow signal."""
    fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
    out: dict = {}
    if not fp.exists():
        return out
    df = pd.read_csv(fp)
    for s in FLOW_SIGNALS:
        try:
            ser = pd.Series(el.SIGNAL_SOURCES[s](df).values).dropna()
            if len(ser) < 200:
                continue
            recent = ser.iloc[-720:]
            z = float((ser.iloc[-1] - recent.mean()) / (recent.std() + 1e-12))
            out[s] = {"value": round(float(ser.iloc[-1]), 5), "z": round(z, 2),
                      "extreme": abs(z) >= 1.5}
        except Exception:  # noqa: BLE001
            continue
    return out


def synthesize(asset: str) -> dict:
    """Combine learned reliability + current reads into ONE honest flow stance.
    Only RELIABLE signals at an EXTREME vote; their direction respects what was learned (contrarian/momentum)."""
    learned = learn_reliable(asset)
    reads = current_reads(asset)
    votes, rationale = [], []
    for s in FLOW_SIGNALS:
        L, R = learned.get(s, {}), reads.get(s, {})
        if not L.get("validates") or not R:
            continue
        if not R.get("extreme"):
            continue
        # high signal -> contrarian fades it (SHORT), momentum follows it (LONG); flip for low
        z = R["z"]
        base = -1 if z > 0 else 1            # contrarian default
        if L["direction"] == "momentum":
            base = -base
        votes.append(base * L["reliability"])
        rationale.append(f"{_LABEL[s]} reliable(OOS{L['oos_roi_pct']:+.0f}%/{L['direction']}) + extreme(z{z:+.1f}) -> "
                         f"{'LONG' if base > 0 else 'SHORT'}")
    score = round(sum(votes), 2)
    n_reliable = sum(1 for v in learned.values() if v.get("validates"))
    if not votes:
        stance = "NEUTRAL"
        note = (f"{n_reliable}/{len(FLOW_SIGNALS)} flow signals validate; none at an actionable extreme now"
                if n_reliable else "NO flow signal predicts this asset OOS — honest abstain (don't guess flow)")
    else:
        stance = "LONG" if score > 0 else "SHORT"
        note = "; ".join(rationale)
    return {"asset": asset.upper(), "stance": stance, "score": score, "reliable_signals": n_reliable,
            "learned": learned, "reads": reads, "note": note}


if __name__ == "__main__":
    import json
    for a in (sys.argv[1:] or ["BTC", "MNT"]):
        r = synthesize(a)
        print(f"\n=== FLOW-INTEL {r['asset']}: {r['stance']} (score {r['score']}, "
              f"{r['reliable_signals']}/{len(FLOW_SIGNALS)} signals validate) ===")
        print(f"  {r['note']}")
        for s, L in r["learned"].items():
            print(f"   {s:14} validates={L.get('validates')} rel={L.get('reliability')} "
                  f"OOS={L.get('oos_roi_pct')}")
