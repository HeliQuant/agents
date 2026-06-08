"""firm/carry_signal.py — LIVE delta-neutral funding-carry signal (market-neutral, NON-predictive).

The firm's directional desks ask "which way?"; this one asks "where can we earn yield WITHOUT predicting?"
Given an asset, it reads the CURRENT funding regime and reports the trailing annualized carry a delta-
neutral position (long spot + short perp) would earn, vs a risk-free benchmark, plus the crash-robustness
verdict from our stress-test (scripts/77). It's a PORTFOLIO-LEVEL ADVISORY — never a directional vote, never
overrides the disciplined PM. Informs allocation: harvest carry where it's rich + crash-survivable, skip it
where it's thin (BTC/ETH) or fragile.

Honest by construction: the carry RATE is computed live from real funding; the crash verdict is the
documented stress-test result (re-run scripts/77 to refresh). Funding-only approximation — see STRATEGY_CARRY.md.
"""
from __future__ import annotations

import time
import requests

BYBIT = "https://api.bybit.com"
FEE = 0.00055          # taker per side per leg
LEGS_ROUNDTRIP = 4     # open spot + open perp + close spot + close perp
RISK_FREE_PCT = 5.0    # ~stablecoin yield benchmark

# Crash-robustness verdicts from the basis stress-test (scripts/77, 2026-06-07). Honest, dated — not live.
# 'robust' = basis stayed tight through the worst crash; 'lumpy' = basis dislocated (real crash-risk premium).
CRASH_CLASS = {
    "HYPEUSDT": ("robust", "basis held <=29 bps through a -17.3% crash; funding ~0"),
    "SUIUSDT": ("lumpy", "basis dislocated to -166 bps + funding spiked -16.8 bps/8h in worst crashes"),
    "BTCUSDT": ("tame", "basis <=11 bps, funding stayed +ve — but carry too thin to use"),
    "ETHUSDT": ("tame", "majors: tame basis but thin funding"),
    "MNTUSDT": ("moderate", "basis max ~28 bps (near HYPE-robust) but funding turns ~-2 bps/8h in worst "
                            "crashes — Mantle-eco carry +3.3% net/90d (100% WF+), size down vs HYPE"),
}

# Rigorous multi-asset carry scan (scripts/79, 2026-06-07, net of funding+basis-4legs, walk-forward @90d hold):
# HYPE +5.8% (100% WF+, >RF) = primary harvest · SUI +4.3% & MNT +3.3% (100% WF+, sub-RF) = diversifiers ·
# BTC +2.8% & ETH +2.5% (sub-RF, 1 WF bucket -ve) = weak · SOL -0.6% = no carry. All funding-driven (basis~0).


def _fetch_funding(symbol: str, days: int) -> list[float]:
    now = int(time.time() * 1000)
    start = now - days * 86400 * 1000
    rows: dict[int, float] = {}
    cur = now
    for _ in range(12):
        try:
            j = requests.get(BYBIT + "/v5/market/funding/history",
                             params={"category": "linear", "symbol": symbol, "limit": 200, "endTime": cur},
                             timeout=15).json()
        except Exception:  # noqa: BLE001
            break
        lst = j.get("result", {}).get("list", [])
        if not lst:
            break
        for x in lst:
            rows[int(x["fundingRateTimestamp"])] = float(x["fundingRate"])
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if oldest <= start or oldest >= cur:
            break
        cur = oldest - 1
        time.sleep(0.1)
    return [rows[t] for t in sorted(rows) if t >= start]


def _read_cached_carry(symbol: str) -> dict | None:
    """Cloud fallback: Bybit is geo-blocked from Railway, so the LOCAL engine (where Bybit is reachable)
    computes the carry and pushes the result via /ingest -> data/{symbol}_carry.json. Read that here when
    we can't reach Bybit ourselves. Keeps the carry desk alive in the cloud without a live exchange call."""
    try:
        from firm import state_store
        d = state_store.load(f"carry:{symbol.upper()}")
        if d:
            d["source"] = "cached (Supabase, via local engine — survives redeploys)"
            return d
    except Exception:  # noqa: BLE001
        pass
    return None


def live_carry(symbol: str, lookback_days: int = 60, rf_pct: float = RISK_FREE_PCT) -> dict | None:
    """Current carry attractiveness for `symbol` from trailing funding. Returns None if no data.

    annualized carry ≈ Σ(funding over lookback) × 365/lookback (gross; the one-time ~22 bps round-trip is
    negligible amortized over a multi-week hold — see STRATEGY_CARRY §4 on why churning kills it)."""
    f = _fetch_funding(symbol, lookback_days)
    if len(f) < 20:
        return _read_cached_carry(symbol)  # Bybit unreachable (e.g. cloud) -> use the locally-pushed result
    n, span = len(f), lookback_days
    gross_ann = sum(f) * 365 / span * 100
    pos_pct = 100 * sum(1 for x in f if x > 0) / n
    recent = f[-21:]  # ~last 7d (3 settlements/day)
    recent_ann = sum(recent) / max(len(recent), 1) * 3 * 365 * 100
    cls, why = CRASH_CLASS.get(symbol.upper(), ("untested", "no stress-test on record"))
    attractive = gross_ann > rf_pct and cls == "robust"
    return {
        "symbol": symbol.upper(), "settlements": n, "lookback_days": span,
        "carry_ann_pct": round(gross_ann, 1), "recent7d_ann_pct": round(recent_ann, 1),
        "pos_pct": round(pos_pct, 0), "beats_risk_free": gross_ann > rf_pct,
        "crash_class": cls, "crash_note": why, "attractive": attractive,
        "verdict": ("HARVEST — rich + crash-robust" if attractive else
                    "skip — beats RF but crash-lumpy (size down)" if (gross_ann > rf_pct and cls == "lumpy") else
                    "skip — thin (below risk-free)"),
    }


def carry_brief(symbols=("HYPEUSDT", "SUIUSDT", "MNTUSDT", "BTCUSDT", "ETHUSDT")) -> str:
    """One-line advisory string the PM/org can read (best harvestable carry now). Empty if none qualify."""
    best = None
    for s in symbols:
        c = live_carry(s)
        if c and c["attractive"] and (best is None or c["carry_ann_pct"] > best["carry_ann_pct"]):
            best = c
    if not best:
        return ""
    return (f"CARRY DESK (market-neutral, non-directional): best harvestable carry = {best['symbol']} "
            f"~{best['carry_ann_pct']}%/yr (crash-robust, {best['pos_pct']:.0f}% funding +ve). "
            f"Advisory only — a delta-neutral yield option, not a directional signal.")
