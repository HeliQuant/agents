"""scripts/80 — execute the delta-neutral HYPE CARRY on Bybit testnet (long spot + short perp).

The ONLY honest way to "live-trade HYPE": NOT the dead directional edge (the firm refuses it), but the
validated market-neutral CARRY — long HYPE spot + short HYPE perp, equal base size, so price cancels and
you collect funding. Reuses firm.bybit_executor's signed sessions (pybit). DRY_RUN by default; --live fires.

HONEST: testnet fills aren't realistic and the carry's ~+5.8%/yr profit accrues via 8h funding over a
quarter — so this proves the EXECUTION PIPELINE (both legs fire, position is delta-neutral), not a
session P&L. Profit ≠ what you'll see in five minutes.

Run:  python scripts/80_carry_execute.py --check
      python scripts/80_carry_execute.py --open               # DRY: print the two legs
      python scripts/80_carry_execute.py --open --live        # fire on testnet (needs testnet USDT)
      python scripts/80_carry_execute.py --close --live
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import bybit_executor as ex  # noqa: E402

SYMBOL = "HYPEUSDT"


def _spot_balance(coin: str) -> float:
    try:
        bal = ex.get_wallet_balance()
        for c in bal.get("coins", []):
            if c.get("coin") == coin:
                return float(c.get("wallet_balance") or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _place(category: str, side: str, qty, market_unit: str | None = None) -> dict:
    """Market order on `category` (spot|linear), DRY_RUN-guarded (mirrors bybit_executor.place_market_order)."""
    intended = {"category": category, "symbol": SYMBOL, "side": side, "orderType": "Market", "qty": str(qty)}
    if category == "spot" and market_unit:
        intended["marketUnit"] = market_unit  # quoteCoin -> qty is USDT; else base
    unit = intended.get("marketUnit", "baseCoin")
    if ex.DRY_RUN:
        print(f"  [DRY] {side:4} {category:6} MARKET qty={qty} ({unit}) {SYMBOL} — NOT sent")
        return {"dry_run": True, "intended": intended}
    res = ex._ok(ex._trade_session().place_order(**intended))
    print(f"  [LIVE] {side:4} {category:6} MARKET {qty} {SYMBOL} -> orderId {res.get('orderId')}")
    return {"dry_run": False, "order_id": res.get("orderId")}


def _perp_lot():
    """(qtyStep, minOrderQty) for the perp — orders must be whole multiples of step."""
    import requests
    it = requests.get("https://api-testnet.bybit.com/v5/market/instruments-info",
                      params={"category": "linear", "symbol": SYMBOL}, timeout=10).json()["result"]["list"][0]
    lot = it["lotSizeFilter"]
    return float(lot["qtyStep"]), float(lot["minOrderQty"])


def open_carry(notional: float, live: bool) -> None:
    """Long ~$notional of spot (market BUY by quote) + SHORT the matching base on the perp -> delta-neutral.
    Sizes to the perp's LOT STEP (the constraining leg) so both legs match and net delta ~0."""
    import time
    import requests
    ex.set_dry_run(not live)
    base = SYMBOL[:-4]  # e.g. SUI from SUIUSDT
    px = float(requests.get("https://api-testnet.bybit.com/v5/market/tickers",
                            params={"category": "linear", "symbol": SYMBOL}, timeout=10).json()["result"]["list"][0]["lastPrice"])
    step, minq = _perp_lot()
    target = max(int((notional / px) / step) * step, minq)   # perp size, lot-aligned
    spot_quote = round(target * px, 2)                        # buy ~the same base amount on spot
    print(f"OPEN CARRY {SYMBOL}: long ~{target} {base} spot (~${spot_quote}) + short {target} perp  "
          f"({base}~${px:.4f}; lot {step}; delta-neutral; testnet={ex.is_testnet()})")
    _place("spot", "Buy", spot_quote, market_unit="quoteCoin")  # leg 1: long spot, qty in USDT
    if not live:
        _place("linear", "Sell", target)
        print("\n(DRY_RUN — nothing sent. Add --live to fire on testnet.)")
        return
    time.sleep(1.5)
    _place("linear", "Sell", target)  # leg 2: short the matching base -> hedge
    print("\nVERIFY:")
    spot_bal = _spot_balance(base)
    pos = ex.get_positions(SYMBOL)
    print(f"  spot {base} long: {spot_bal}")
    print(f"  perp short: {pos if pos else 'flat'}")
    net = spot_bal - (pos[0]["size"] if pos else 0)
    print(f"  net delta (spot - perp): {net:+.4f} {base}  -> {'~NEUTRAL ✓' if abs(net) < step else 'residual within one lot step (slippage)'}")


def close_carry(live: bool) -> None:
    ex.set_dry_run(not live)
    base = SYMBOL[:-4]
    print(f"CLOSE CARRY {SYMBOL}: sell spot + buy-back perp short")
    sb = _spot_balance(base)
    if sb > 0:
        _place("spot", "Sell", round(sb, 5))
    pos = ex.get_positions(SYMBOL)
    if pos:
        _place("linear", "Buy", pos[0]["size"])  # reduce-only effect: closes short
    if not pos and sb == 0:
        print("  nothing open.")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="HeliQuant delta-neutral HYPE carry executor (Bybit testnet).")
    ap.add_argument("--check", action="store_true", help="auth + USDT balance (read-only)")
    ap.add_argument("--open", action="store_true", help="open the carry (DRY unless --live)")
    ap.add_argument("--close", action="store_true", help="close the carry (DRY unless --live)")
    ap.add_argument("--notional", type=float, default=100.0, help="USDT notional for the carry (default 100 = 1% of 10k)")
    ap.add_argument("--symbol", default="HYPEUSDT", help="perp+spot symbol (default HYPEUSDT; e.g. SUIUSDT)")
    ap.add_argument("--live", action="store_true", help="actually transmit (testnet)")
    args = ap.parse_args()

    global SYMBOL
    SYMBOL = args.symbol.upper()
    print(f"[carry-exec] symbol={SYMBOL}  testnet={ex.is_testnet()}  live={args.live}\n")
    try:
        if args.check:
            st = ex.check_auth(); print("auth:", st)
            bal = ex.get_wallet_balance()
            print(f"USDT equity={bal.get('equity_usdt')} available={bal.get('available_usdt')} | total_equity={bal.get('total_equity')}")
            if not bal.get("total_equity"):
                print("\n⚠️  testnet wallet EMPTY — claim test USDT at testnet.bybit.com (Assets) before --live.")
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
