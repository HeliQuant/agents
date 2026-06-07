"""firm/asset_efficiency.py — EFFICIENCY-AWARE desk prioritization (overlay on desk_performance weights).

The honest premise (proven the hard way): some assets are too EFFICIENT to predict. BTC failed 7 edge-
hunt rounds; MNT's oi-edge just decayed. When NO predictive edge validates for an asset, trying to GUESS
direction is a losing game — the disciplined move is to STOP guessing and FOLLOW informed flow (what the
whales / smart money actually DO). So for such assets this TILTS the desk weights toward the flow-following
desks (Smart-Money, On-chain) and away from the prediction desks (Regime/Technical, OI-Contrarian).

Non-destructive: overlays on the track-record weights from desk_performance, stays bounded [0.6,1.4], stays
ADVISORY (a soft prior for the PM) — it never mutes a desk and never touches the validation / R:R gates.
Has a validated predictive edge -> NOT efficient -> no tilt -> org behaves exactly as before.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATED = ROOT / "data" / "validated_edges.json"

PREDICTION_DESKS = ["Regime/Technical", "OI-Contrarian"]   # need a predictable signal to add value
FLOW_DESKS = ["Smart-Money Flow", "On-chain/Risk"]          # follow informed actors directly
LO, HI = 0.6, 1.4                                            # same bounds as desk_performance — never mute
BOOST, FADE = 1.25, 0.80                                     # multiplicative tilt (then clamped)


def _realized_vol(asset: str) -> float | None:
    """Annualized realized vol from hourly closes (intensifier — 'labil' = more reason to follow flow)."""
    try:
        import numpy as np
        import pandas as pd
        fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
        if not fp.exists():
            return None
        c = pd.read_csv(fp)["close"].astype(float).values
        if len(c) < 200:
            return None
        rets = np.diff(np.log(c[-720:]))  # last ~30d hourly
        return float(np.std(rets) * np.sqrt(24 * 365))
    except Exception:  # noqa: BLE001
        return None


def efficiency_read(asset: str) -> dict:
    """Is this asset 'too efficient to predict'? efficient = NO validated predictive edge for it.
    Returns {efficient, score 0..1, vol_annual, reason}."""
    asset = asset.upper()
    try:
        validated = json.loads(VALIDATED.read_text()) if VALIDATED.exists() else {}
    except (ValueError, OSError):
        validated = {}
    has_edge = asset in validated
    vol = _realized_vol(asset)
    efficient = not has_edge
    # score: 0 if we have an edge; else 0.6 baseline + up to 0.4 from high vol (labil intensifies it)
    score = 0.0 if has_edge else min(1.0, 0.6 + (0.0 if vol is None else min(0.4, max(0.0, (vol - 0.5) * 0.4))))
    if has_edge:
        reason = f"{asset} has a validated predictive edge ({validated[asset].get('edge')}) -> prediction viable, no tilt"
    else:
        vtxt = f", realized vol ~{vol*100:.0f}%/yr (volatile)" if vol else ""
        reason = (f"{asset} has NO validated predictive edge{vtxt} -> guessing direction is a losing game; "
                  f"FOLLOW informed flow instead")
    return {"asset": asset, "efficient": efficient, "score": round(score, 2), "vol_annual": vol, "reason": reason}


def efficiency_tilt(asset: str, weights: dict) -> tuple[dict, str]:
    """Overlay the efficiency tilt on track-record weights. Returns (tilted_weights, brief).
    Not efficient -> weights unchanged, empty brief (org behaves exactly as before)."""
    r = efficiency_read(asset)
    if not r["efficient"]:
        return weights, ""
    w = dict(weights)
    # scale the tilt by the efficiency score (more efficient/volatile -> stronger tilt)
    boost = 1 + (BOOST - 1) * r["score"]
    fade = 1 - (1 - FADE) * r["score"]
    for d in FLOW_DESKS:
        if d in w:
            w[d] = round(min(HI, max(LO, w[d] * boost)), 2)
    for d in PREDICTION_DESKS:
        if d in w:
            w[d] = round(min(HI, max(LO, w[d] * fade)), 2)
    brief = (f"EFFICIENCY TILT — {r['reason']}. Prioritizing flow desks "
             f"(Smart-Money, On-chain) and de-emphasizing prediction desks (Regime, OI) for this read.")
    return w, brief


if __name__ == "__main__":
    import sys
    base = {d: 1.0 for d in PREDICTION_DESKS + FLOW_DESKS + ["Macro (Allora)", "Research", "Smart-Social"]}
    for a in (sys.argv[1:] or ["BTC", "MNT"]):
        r = efficiency_read(a)
        w, brief = efficiency_tilt(a, base)
        print(f"\n{a}: efficient={r['efficient']} score={r['score']}")
        print(f"  {r['reason']}")
        if brief:
            tilted = {d: v for d, v in w.items() if v != 1.0}
            print(f"  tilt -> {tilted}")
