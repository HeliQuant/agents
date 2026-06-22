"""scripts/80 — execute the delta-neutral CARRY on the exchange demo (long spot + short perp).

The ONLY honest way to "live-trade" the carry: NOT the dead directional edge (the firm refuses it), but the
validated market-neutral CARRY — long spot + short perp, equal base size, so price cancels and you collect
funding. Uses the firm's Bitget demo executor (firm.exchange_executor). DRY_RUN by default; --live fires.

DEMO IS PERP-ONLY: the Bitget demo has NO spot, so the SPOT leg is recorded as an honest PAPER fill and only
the PERP leg fires on the venue — the position is effectively perp-only on the demo. The Bitget demo also
lists only BTC/ETH/XRP perps, so any other asset's perp leg degrades to paper too (no fabricated fill).

HONEST: demo fills aren't realistic and the carry's profit accrues via 8h funding over a quarter — so this
proves the EXECUTION PIPELINE (the perp leg fires, the spot leg is booked as paper), not a session P&L.
Profit != what you'll see in five minutes.

Run:  python scripts/80_carry_execute.py --check
      python scripts/80_carry_execute.py --open               # DRY: print the two legs
      python scripts/80_carry_execute.py --open --live        # fire on the demo (perp leg real, spot paper)
      python scripts/80_carry_execute.py --close --live
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import exchange_executor as ex  # noqa: E402

SYMBOL = "BTCUSDT"  # demo perps = BTC/ETH/XRP; other assets degrade to paper on the perp leg too


def _spot_balance(coin: str) -> float:
    try:
        bal = ex.get_wallet_balance()
        for c in bal.get("coins", []):
            if c.get("coin") == coin:
                return float(c.get("wallet_balance") or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _paper_spot(side: str, qty, base: str) -> None:
    """Spot leg is PAPER — the Bitget demo is perp-only, so we never hit the venue for spot."""
    print(f"  [paper] spot leg not on Bitget demo — {side} {qty} {base} recorded as paper")


def _perp(side: str, qty) -> dict:
    """Real perp market order on the demo (DRY_RUN-guarded inside the executor)."""
    return ex.place_market_order(SYMBOL, side, qty, reduce_only=False)


def _perp_lot():
    """(qtyStep, minOrderQty) for the perp — orders must be whole multiples of step."""
    inst = ex.get_instrument(SYMBOL)
    step = float(inst.get("qty_step") or 0.001)
    minq = float(inst.get("min_order_qty") or step)
    return step, minq


def open_carry(notional: float, live: bool) -> None:
    """Long ~$notional of spot (PAPER on the demo) + SHORT the matching base on the perp (REAL) -> delta-neutral
    in design. Sizes to the perp's LOT STEP (the constraining leg) so both legs match and net delta ~0."""
    import time
    ex.set_dry_run(not live)
    base = SYMBOL[:-4]  # e.g. BTC from BTCUSDT
    step, minq = _perp_lot()
    target = max(int((notional / max(step, 1e-9)) ) * step, minq) if notional >= step else minq
    # Spot leg is paper-only, so we size in base units off the perp's lot step (no live venue price read).
    print(f"OPEN CARRY {SYMBOL}: long ~{target} {base} spot (PAPER) + short {target} perp  "
          f"(lot {step}; delta-neutral design; demo={ex.is_demo()})")
    _paper_spot("Buy", target, base)  # leg 1: long spot — PAPER (demo is perp-only)
    if not live:
        _perp("Sell", target)
        print("\n(DRY_RUN — nothing sent. Add --live to fire the perp leg on the demo.)")
        return
    time.sleep(1.5)
    _perp("Sell", target)  # leg 2: short the matching base on the perp -> hedge (REAL)
    print("\nVERIFY:")
    print(f"  spot {base} long: PAPER (demo is perp-only)")
    pos = ex.get_positions(SYMBOL)
    print(f"  perp short: {pos if pos else 'flat'}")
    print(f"  net delta: paper-spot + real-perp short -> effectively perp-only on the demo")


def close_carry(live: bool) -> None:
    ex.set_dry_run(not live)
    base = SYMBOL[:-4]
    print(f"CLOSE CARRY {SYMBOL}: sell spot (PAPER) + buy-back perp short")
    _paper_spot("Sell", "held", base)  # spot leg is paper
    pos = ex.get_positions(SYMBOL)
    if pos:
        _perp("Buy", pos[0]["size"])  # reduce-only effect: closes short
    else:
        print("  perp: nothing open.")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="HeliQuant delta-neutral carry executor (Bitget demo, perp-only).")
    ap.add_argument("--check", action="store_true", help="auth + USDT balance (read-only)")
    ap.add_argument("--open", action="store_true", help="open the carry (DRY unless --live)")
    ap.add_argument("--close", action="store_true", help="close the carry (DRY unless --live)")
    ap.add_argument("--notional", type=float, default=100.0, help="USDT notional for the carry (default 100 = 1% of 10k)")
    ap.add_argument("--symbol", default="BTCUSDT", help="perp symbol (default BTCUSDT; demo perps = BTC/ETH/XRP)")
    ap.add_argument("--live", action="store_true", help="actually transmit the perp leg (demo)")
    args = ap.parse_args()

    global SYMBOL
    SYMBOL = args.symbol.upper()
    print(f"[carry-exec] symbol={SYMBOL}  demo={ex.is_demo()}  live={args.live}\n")
    try:
        if args.check:
            st = ex.check_auth(); print("auth:", st)
            bal = ex.get_wallet_balance()
            print(f"USDT equity={bal.get('equity_usdt')} available={bal.get('available_usdt')} | total_equity={bal.get('total_equity')}")
            if not bal.get("total_equity"):
                print("\n⚠️  demo wallet EMPTY — fund the Bitget demo account before --live.")
        if args.open:
            open_carry(args.notional, args.live)
        if args.close:
            close_carry(args.live)
        if not any([args.check, args.open, args.close]):
            ap.print_help()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {str(e)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
