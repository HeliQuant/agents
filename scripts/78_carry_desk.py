"""scripts/78 — Carry Desk: HeliQuant's LIVE delta-neutral funding-carry signal.

Reads current funding and reports where the firm could harvest market-neutral carry RIGHT NOW (no
prediction), with the crash-robustness verdict from scripts/77. This is the live view of the strategy
documented in profile/STRATEGY_CARRY.md — an advisory yield desk, not a directional vote.

Run:  python scripts/78_carry_desk.py            # HYPE SUI BTC ETH
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.carry_signal import RISK_FREE_PCT, carry_brief, live_carry  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    syms = [s.upper() for s in sys.argv[1:]] or ["HYPEUSDT", "SUIUSDT", "BTCUSDT", "ETHUSDT"]
    print(f"HeliQuant CARRY DESK — live delta-neutral funding carry (vs ~{RISK_FREE_PCT:.0f}% risk-free)\n")
    print(f"{'asset':10}{'carry/yr':>10}{'last-7d/yr':>12}{'fund +ve':>9}{'crash':>9}  verdict")
    print("-" * 78)
    for s in syms:
        c = live_carry(s)
        if not c:
            print(f"{s:10}  no funding data"); continue
        print(f"{c['symbol']:10}{c['carry_ann_pct']:>+9.1f}%{c['recent7d_ann_pct']:>+11.1f}%"
              f"{c['pos_pct']:>8.0f}%{c['crash_class']:>9}  {c['verdict']}")
    print("-" * 78)
    brief = carry_brief(tuple(syms))
    print("\n" + (brief if brief else "CARRY DESK: nothing harvestable right now (all thin or crash-fragile) -> abstain."))
    print("\n(Advisory only. Market-neutral yield — never a directional vote; never overrides the PM's gates.)")


if __name__ == "__main__":
    main()
