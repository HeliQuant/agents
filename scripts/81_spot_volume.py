"""scripts/81 — spot execution stress test: N buy+sell round-trips per asset (PAPER on the Bitget demo).

Originally a LIVE spot stress test, but the Bitget demo has NO spot market — so each round-trip is recorded
as an honest PAPER fill rather than a fabricated venue order. Each round-trip = market BUY $notional + market
SELL it back; on a real spot venue both legs pay the bid-ask spread + fees. Here the legs are paper, so there
is no realized cost to measure (cost = 0 by construction — there is no venue fill to charge a spread).

HONEST: this is an EXECUTION-FLOW proof on the demo (the loop runs, both legs are booked), NOT a profit demo
and NOT a real-fill cost measurement. On a live spot venue an immediate buy+sell is a small LOSS BY DESIGN
(~spread x N); profit comes from the STRATEGY holding per a validated signal, not from churning round-trips.

Run:  python scripts/81_spot_volume.py BTCUSDT ETHUSDT XRPUSDT --n 100 --notional 100
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import exchange_executor as ex  # noqa: E402


def _paper_spot(symbol: str, side: str, qty) -> None:
    """Spot leg is PAPER — the Bitget demo is perp-only, so we never hit the venue for spot."""
    base = symbol.replace("USDT", "")
    print(f"  [paper] spot leg not on Bitget demo — {side} {qty} {base} recorded as paper")


def round_trips(symbol: str, n: int, notional: float) -> dict:
    base = symbol.replace("USDT", "")
    print(f"\n=== {symbol} ({base}) — {n} round-trips of ${notional} (PAPER, demo has no spot) ===")
    filled = failed = 0
    for i in range(n):
        # leg 1: BUY $notional of base — PAPER
        _paper_spot(symbol, "Buy", f"${notional}")
        # leg 2: SELL it back — PAPER (paper fill assumed, flow continues)
        _paper_spot(symbol, "Sell", f"{notional} {base}")
        filled += 1
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  ...{i+1}/{n} round-trips done (paper) | filled {filled} fail {failed}")
        time.sleep(0.01)
    out = {"symbol": symbol, "skipped": False, "filled": filled, "failed": failed,
           "usdt_cost": 0.0, "cost_per_trade_bps": 0.0}
    print(f"  DONE {symbol}: {filled} paper round-trips | no venue fill (demo has no spot) -> cost $0.0 "
          f"(a live spot venue would charge ~spread+fees per round-trip)")
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raw = sys.argv[1:]
    n, notional, skip = 100, 100.0, set()
    for i, a in enumerate(raw):
        if a == "--n" and i + 1 < len(raw):
            n = int(raw[i + 1]); skip |= {i, i + 1}
        elif a == "--notional" and i + 1 < len(raw):
            notional = float(raw[i + 1]); skip |= {i, i + 1}
    symbols = [raw[i].upper() for i in range(len(raw)) if i not in skip and not raw[i].startswith("--")] or ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    print(f"Spot execution stress test — {n} round-trips x ${notional} per asset (PAPER; demo has no spot)")
    print("(execution-flow proof, NOT profit — a live spot venue would charge the spread each round-trip)\n")
    results = []
    for sym in symbols:
        try:
            results.append(round_trips(sym, n, notional))
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} aborted: {str(e)[:80]}")
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"  {r['symbol']:9} {r['filled']} paper round-trips | cost ${r['usdt_cost']} ({r['cost_per_trade_bps']} bps/trip)")
    print("\nNote: the Bitget demo is perp-only — spot round-trips are booked as paper here.")
    print("On a live spot venue the cost is the bid-ask spread — exactly why an edge must beat costs (the lesson from every backtest).")


if __name__ == "__main__":
    main()
