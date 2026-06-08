"""firm/asset_efficiency.py — DYNAMIC per-asset CHARACTER read that ranks the desks before every trade.

The honest premise: HOW you should read an asset depends on WHAT the asset is. Before each decision the firm
profiles the asset from its own data and tilts the desk ranking accordingly:
  * PREDICTABLE  (a validated edge, or returns showing real structure / autocorrelation like a pattern-ful
                 asset)  -> trust the PREDICTION desks (Regime/Technical, OI-Contrarian).
  * EFFICIENT    (near a random walk + high volatility, no edge — you genuinely can't predict it, e.g. BTC)
                 -> trust the FLOW-following desks (Smart-Money, On-chain, whale, flow-intel) — follow the
                 informed money instead of guessing direction.
  * NEUTRAL      (mixed) -> no tilt.

Bidirectional + data-driven. Overlays on the track-record weights (desk_performance), stays bounded
[0.6,1.4], advisory only — never mutes a desk, never touches the validation / R:R gates. Honest about its
limits: autocorrelation is a rough 'is there structure' proxy, NOT proof of a tradeable edge — the validated
registry remains the real test; this only decides WHOSE read to weight while that question is open.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATED = ROOT / "data" / "validated_edges.json"

PREDICTION_DESKS = ["Regime/Technical", "OI-Contrarian"]               # need a predictable signal to add value
FLOW_DESKS = ["Smart-Money Flow", "On-chain/Risk", "whale", "flow-intel"]   # follow informed actors directly
LO, HI = 0.6, 1.4
BOOST, FADE = 1.25, 0.80
PATTERN_HI, PATTERN_LO = 0.10, 0.05    # avg |autocorr| thresholds: >=HI = structured, <LO = random-walk-ish
VOL_HI = 0.60                          # ~60%/yr realized vol = "volatile"


def _series(asset: str):
    import numpy as np
    import pandas as pd
    fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return None
    c = pd.read_csv(fp)["close"].astype(float).values
    return c[-720:] if len(c) >= 220 else None  # last ~30d hourly


def _realized_vol(asset: str) -> float | None:
    import numpy as np
    c = _series(asset)
    if c is None:
        return None
    return float(np.std(np.diff(np.log(c))) * np.sqrt(24 * 365))


def _pattern_score(asset: str) -> float | None:
    """Avg |autocorrelation| of returns at lags 1/6/24. ~0 = random walk (efficient); higher = structure
    (trend or mean-reversion = a 'pattern' a prediction desk could exploit). Rough proxy, not an edge proof."""
    import numpy as np
    c = _series(asset)
    if c is None:
        return None
    r = np.diff(np.log(c))
    if len(r) < 100 or np.std(r) == 0:
        return None
    acfs = []
    for lag in (1, 6, 24):
        if len(r) > lag + 30:
            a, b = r[:-lag], r[lag:]
            sa, sb = np.std(a), np.std(b)
            if sa > 0 and sb > 0:
                acfs.append(abs(float(np.corrcoef(a, b)[0, 1])))
    return round(float(np.mean(acfs)), 3) if acfs else None


def efficiency_read(asset: str) -> dict:
    """Profile the asset's CHARACTER -> predictable / efficient / neutral, with the evidence."""
    asset = asset.upper()
    try:
        validated = json.loads(VALIDATED.read_text()) if VALIDATED.exists() else {}
    except (ValueError, OSError):
        validated = {}
    has_edge = asset in validated
    vol = _realized_vol(asset)
    pat = _pattern_score(asset)
    if has_edge:
        char, reason = "predictable", f"{asset} has a VALIDATED edge ({validated[asset].get('edge')}) -> prediction works"
    elif pat is not None and pat >= PATTERN_HI:
        char, reason = "patterned", f"{asset} returns show structure (avg|acf| {pat} >= {PATTERN_HI}) -> prediction viable; lean to prediction desks"
    elif pat is not None and pat < PATTERN_LO:   # no edge + near random-walk -> efficient (vol = intensifier, not a gate)
        vtxt = f", vol ~{vol*100:.0f}%/yr" if vol else ""
        char, reason = "efficient", f"{asset} near random-walk (avg|acf| {pat} < {PATTERN_LO}{vtxt}) + no validated edge -> can't predict; FOLLOW flow"
    else:
        char, reason = "neutral", f"{asset} mild structure (acf {pat}, vol {vol}) but no edge -> no strong tilt"
    score = {"predictable": 1.0, "patterned": 0.7, "efficient": min(1.0, 0.6 + (0.0 if vol is None else min(0.4, max(0.0, (vol - 0.5) * 0.4)))), "neutral": 0.0}[char]
    return {"asset": asset, "character": char, "has_edge": has_edge, "vol_annual": vol,
            "pattern_score": pat, "tilt_strength": round(score, 2), "reason": reason}


def efficiency_tilt(asset: str, weights: dict) -> tuple[dict, str]:
    """Bidirectional desk tilt from the asset character. predictable/patterned -> PREDICTION desks up;
    efficient -> FLOW desks up. neutral -> unchanged (org behaves exactly as before)."""
    r = efficiency_read(asset)
    char = r["character"]
    if char == "neutral":
        return weights, ""
    w = dict(weights)
    s = r["tilt_strength"]
    boost, fade = 1 + (BOOST - 1) * s, 1 - (1 - FADE) * s
    if char in ("predictable", "patterned"):
        up, down, label = PREDICTION_DESKS, FLOW_DESKS, "PREDICTION desks (Regime, OI)"
        # patterned-but-unvalidated leans softer on fading flow (no proven edge yet)
        if char == "patterned":
            fade = 1 - (1 - FADE) * s * 0.5
    else:  # efficient
        up, down, label = FLOW_DESKS, PREDICTION_DESKS, "FLOW desks (Smart-Money, On-chain, whale, flow-intel)"
    for d in up:
        if d in w:
            w[d] = round(min(HI, max(LO, w[d] * boost)), 2)
    for d in down:
        if d in w:
            w[d] = round(min(HI, max(LO, w[d] * fade)), 2)
    return w, f"CHARACTER TILT — {r['reason']}. Prioritizing {label} for this asset."


if __name__ == "__main__":
    import sys
    base = {d: 1.0 for d in PREDICTION_DESKS + FLOW_DESKS + ["Macro (Allora)", "Research", "Smart-Social"]}
    for a in (sys.argv[1:] or ["MNT", "BTC", "HYPE"]):
        r = efficiency_read(a)
        _, brief = efficiency_tilt(a, base)
        print(f"\n{a}: character={r['character']} (vol={r['vol_annual']}, pattern={r['pattern_score']}, strength={r['tilt_strength']})")
        print(f"  {r['reason']}")
