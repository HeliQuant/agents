"""firm/exchange_executor.py — Line-1 execution: LIVE perp orders on Bitget DEMO (testnet).

The venue-execution half of HeliQuant's DUAL-EXECUTION model (the full-Bitget successor to the firm's
earlier testnet execution path). On an ENTER decision the firm records the trade two ways:
  * Line 1 (this module)        a real USDT-perp market order on the Bitget SUSDT-FUTURES demo (the
                                 trade actually fires on a liquid venue — verifiable in the Bitget
                                 account / order history) — paired with —
  * Line 2 (onchain_recorder)   an immutable SHA-256 anchor of the decision on Mantle Sepolia.

All venue I/O delegates to firm.bitget_adapter (HMAC-signed Bitget v2). The Bitget demo lists only
BTC / ETH / XRP perps — any other asset returns an honest "paper (unsupported on demo)" result rather
than a fabricated fill, and the demo has NO spot, so spot-only strategies degrade to paper.

SAFETY (defense in depth, nothing fires by accident):
  1. ``DRY_RUN`` module flag defaults True — ``place_market_order`` LOGS the intended order and
     returns without sending. A live order requires DRY_RUN=False (CLI: pass ``--live``).
  2. ``decision_to_order`` is HARD-GATED: it returns an order spec ONLY for a PM ``ENTER`` whose
     OOS-validated edge is live + actionable (mirrors the org's own gate). ABSTAIN -> None.
  3. Leverage is capped to ``trade_ticket.LEVERAGE_CAP``; the ticket's own notional/size is respected.

Keys (read by firm.bitget_adapter from agents/.env then root .env — values NEVER printed):
  BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE   ·   BITGET_DEMO=1 (demo, default-safe)
"""
from __future__ import annotations

import argparse
import sys

from firm import bitget_adapter as bg
from firm import trade_ticket as tt

# ── SAFETY: nothing fires live unless this is explicitly turned off (CLI --live / set_dry_run). ──
DRY_RUN = True
LEVERAGE_CAP = tt.LEVERAGE_CAP  # notional <= LEVERAGE_CAP x equity (single source of truth = ticket layer)


def is_testnet() -> bool:
    """True when pointed at the Bitget DEMO (no real funds). Default-safe (BITGET_DEMO absent -> demo)."""
    return bg.is_demo()


def is_demo() -> bool:
    return bg.is_demo()


def set_dry_run(enabled: bool) -> None:
    """Toggle the module DRY_RUN flag (the CLI's --live sets this False)."""
    global DRY_RUN
    DRY_RUN = bool(enabled)


def _asset(symbol: str) -> str:
    """'BTCUSDT' / 'SBTCSUSDT' / 'BTC' -> 'BTC' (the firm asset bitget_adapter keys on)."""
    return symbol.upper().replace("SUSDT", "").replace("USDT", "").replace("USDC", "")


def _side(side: str) -> str:
    """'Buy'/'buy'/'long' -> 'buy'; anything else -> 'sell' (bitget_adapter wants lowercase)."""
    return "buy" if str(side).lower()[:1] in ("b", "l") else "sell"


# ─────────────────────────────── read-only operations ───────────────────────────────
def check_auth() -> dict:
    """Verify the Bitget keys end-to-end (read-only, safe) — an account read proves they authenticate
    without placing anything. Secret values are never included."""
    status: dict = {"demo": is_demo(), "keys_ok": False}
    try:
        s = bg.account_summary()
        status.update({"keys_ok": True, "equity_usdt": s.get("equity_usd"),
                       "available_usdt": s.get("available_usd")})
    except Exception as e:  # noqa: BLE001
        status["error"] = str(e)[:160]
    return status


def get_wallet_balance(account_type: str = "UNIFIED") -> dict:
    """USDT balance summary from the Bitget account. {equity_usdt, available_usdt, coins:[...]}"""
    s = bg.account_summary()
    eq, avail = s.get("equity_usd"), s.get("available_usd")
    return {"account_type": account_type, "equity_usdt": eq, "available_usdt": avail,
            "wallet_balance_usdt": avail, "total_equity": eq,
            "coins": [{"coin": bg._margin(), "equity": eq, "wallet_balance": avail}]}


def get_positions(symbol: str | None = None) -> list[dict]:
    """Open demo positions, normalized to {symbol, side, size, avg_price, unrealised_pnl}. Optionally
    filtered to one symbol/asset. Empty list = flat."""
    out: list[dict] = []
    want = _asset(symbol) if symbol else None
    for p in bg.get_positions():
        size = float(p.get("total") or p.get("size") or 0)
        if size == 0:
            continue
        sym = p.get("symbol", "")
        if want and want not in sym:
            continue
        out.append({
            "symbol": sym,
            "side": "Buy" if str(p.get("holdSide", "")).lower() == "long" else "Sell",
            "size": size,
            "avg_price": float(p["openPriceAvg"]) if p.get("openPriceAvg") else None,
            "unrealised_pnl": float(p["unrealizedPL"]) if p.get("unrealizedPL") else None,
        })
    return out


def get_instrument(symbol: str) -> dict:
    """Contract filters (min qty / qty step / status) for sizing a valid order. Marks an asset that
    isn't on the demo as unsupported (so callers degrade to paper instead of erroring)."""
    a = _asset(symbol)
    spec = bg.contract_spec(a) or {}
    place = int(spec.get("volumePlace", 4) or 4)
    return {
        "symbol": bg.to_symbol(a),
        "status": "supported" if bg.supported(a) else "unsupported_on_demo",
        "min_order_qty": float(spec.get("minTradeNum")) if spec.get("minTradeNum") else None,
        "qty_step": 10 ** (-place),
        "max_leverage": float(spec.get("maxLever")) if spec.get("maxLever") else None,
    }


# ─────────────────────────────── order placement (DRY-RUN guarded) ───────────────────────────────
def _round_qty(qty: float, step: float | None) -> float:
    """Floor qty to the contract lot step (off-grid qty is rejected)."""
    if not step or step <= 0:
        return qty
    return (int(qty / step)) * step


def place_market_order(symbol: str, side: str, qty: float, reduce_only: bool = False) -> dict:
    """Place a perp market order on the Bitget demo via bitget_adapter.

    DRY_RUN (default True): LOGS the intended order and returns {"dry_run": True, ...} WITHOUT sending.
    An asset not listed on the demo (anything but BTC/ETH/XRP) returns an honest paper result — never a
    fabricated fill. ``reduce_only=True`` only shrinks/closes an existing position."""
    a = _asset(symbol)
    bside = _side(side)
    intended = {"asset": a, "side": bside, "qty": qty, "reduce_only": reduce_only, "demo": is_demo()}
    if not bg.supported(a):
        print(f"[paper] {a} not on Bitget demo (BTC/ETH/XRP) — recorded as paper, no venue fill.")
        return {"sent": False, "paper": True, "reason": f"{a} unsupported on Bitget demo", "intended_order": intended}
    if DRY_RUN:
        print(f"[DRY_RUN] would place {bside.upper()} MARKET {qty} {a} "
              f"(reduceOnly={reduce_only}, demo={is_demo()}) - NOT sent. Pass --live to transmit.")
        return {"dry_run": True, "sent": False, "intended_order": intended}
    bg.set_position_mode(True)
    res = bg.place_market_order(a, bside, qty, reduce_only=reduce_only)
    oid = res.get("orderId") if isinstance(res, dict) else res
    print(f"[LIVE] {bside.upper()} MARKET {qty} {a} sent (demo={is_demo()}) -> orderId {oid}")
    return {"dry_run": False, "sent": True, "order_id": oid, "intended_order": intended}


def close_position(symbol: str) -> dict:
    """Flatten any open position on ``symbol`` with an opposite reduce-only market order (DRY_RUN-aware)."""
    pos = get_positions(symbol)
    if not pos:
        return {"closed": False, "reason": "no open position", "symbol": symbol}
    p = pos[0]
    opp = "Sell" if p["side"] == "Buy" else "Buy"
    res = place_market_order(symbol, opp, p["size"], reduce_only=True)
    return {"closed": bool(res.get("sent")), "symbol": symbol,
            "was": {"side": p["side"], "size": p["size"]}, "close_order": res}


# ─────────────────────────────── decision -> order mapper (HARD-GATED) ───────────────────────────────
def _validated_edge_actionable(decision: dict) -> tuple[bool, str]:
    """Mirror the ORG's own ENTER gate: a venue order is justified ONLY when the OI-Contrarian desk
    reports edge_validated + actionable AND its live_signal agrees with the PM's direction — re-derived
    from the decision so the executor enforces the SAME boundary the org does (defense in depth)."""
    dirn = str(decision.get("direction", "")).upper()
    oi = decision.get("oi_contrarian") or {}
    if oi:
        if not (oi.get("edge_validated") and oi.get("actionable")):
            return False, "OI-Contrarian desk not validated+actionable -> no order"
        sig = str(oi.get("live_signal", "")).upper()
        if sig and sig != dirn:
            return False, f"validated edge signal {sig} != decision {dirn} -> no order"
        return True, f"validated OI-contrarian edge live + aligned ({dirn})"
    tk = decision.get("trade_ticket") or {}
    if tk.get("edge_validated") and str(tk.get("mode", "")).upper().startswith("AGGRESSIVE"):
        return True, f"ticket edge_validated + AGGRESSIVE ({dirn}) -> validated edge was live + aligned"
    return False, "no validated-edge context -> disciplined no-order (matches org abstain bias)"


def decision_to_order(decision: dict, equity_usdt: float, symbol: str = "BTCUSDT") -> dict | None:
    """Map a firm decision -> a Bitget order spec, or None (no order). Returns None UNLESS the decision
    is an ENTER with a directional LONG/SHORT, a VALID attached trade_ticket, and the validated-edge gate
    passes. Sizing RESPECTS the ticket; leverage re-capped here as belt-and-suspenders. The spec is
    descriptive — pass it to place_market_order (itself DRY_RUN-guarded)."""
    if str(decision.get("decision", "")).upper() != "ENTER":
        return None
    dirn = str(decision.get("direction", "")).upper()
    if dirn not in ("LONG", "SHORT"):
        return None
    ticket = decision.get("trade_ticket")
    if not isinstance(ticket, dict) or not ticket.get("valid"):
        return None
    ok, why = _validated_edge_actionable(decision)
    if not ok:
        return None

    side = "Buy" if dirn == "LONG" else "Sell"
    leverage = min(float(ticket.get("leverage", 1.0) or 1.0), LEVERAGE_CAP)
    size_pct = float(ticket.get("position_size_pct", leverage * 100.0) or 0.0)
    notional = min(equity_usdt * (size_pct / 100.0), equity_usdt * LEVERAGE_CAP)

    last_price = float(ticket.get("entry") or 0.0)
    inst = {}
    try:
        inst = get_instrument(symbol)
    except Exception:  # noqa: BLE001
        pass
    qty = (notional / last_price) if last_price > 0 else 0.0
    qty = _round_qty(qty, inst.get("qty_step"))
    min_qty = inst.get("min_order_qty")
    if min_qty and qty < min_qty:
        qty = min_qty

    return {
        "symbol": symbol, "side": side, "qty": qty, "reduce_only": False,
        "leverage": round(leverage, 2), "notional_usdt": round(notional, 2),
        "ticket_mode": ticket.get("mode"), "from_direction": dirn,
        "gate": why, "last_price": last_price,
    }


# ─────────────────────────────── CLI (offline-safe defaults) ───────────────────────────────
def _print(obj) -> None:
    import json
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        prog="python -m firm.exchange_executor",
        description="HeliQuant Bitget DEMO executor. Read ops are safe; orders are DRY_RUN unless --live.")
    ap.add_argument("--check", action="store_true", help="verify keys + show USDT balance (read-only, safe)")
    ap.add_argument("--positions", action="store_true", help="show open positions (optionally --symbol)")
    ap.add_argument("--demo-order", action="store_true", help="tiny minimum-qty MARKET order (DRY_RUN unless --live)")
    ap.add_argument("--close", action="store_true", help="flatten open position on --symbol (respects --live)")
    ap.add_argument("--symbol", default="BTCUSDT", help="perp symbol (default BTCUSDT)")
    ap.add_argument("--side", default="Buy", choices=["Buy", "Sell"], help="demo-order side (default Buy)")
    ap.add_argument("--live", action="store_true", help="DISABLE DRY_RUN — actually transmit (demo).")
    args = ap.parse_args(argv)

    if args.live:
        set_dry_run(False)
    print(f"[exchange_executor] demo={is_demo()}  DRY_RUN={DRY_RUN}  symbol={args.symbol}\n")

    if not any([args.check, args.positions, args.demo_order, args.close]):
        ap.print_help()
        return 0
    try:
        if args.check:
            print("AUTH CHECK:")
            _print(check_auth())
        if args.positions:
            pos = get_positions(args.symbol)
            print(f"OPEN POSITIONS ({args.symbol}):")
            _print(pos if pos else {"flat": True, "symbol": args.symbol})
        if args.demo_order:
            inst = get_instrument(args.symbol)
            qty = inst.get("min_order_qty") or 0.001
            print(f"DEMO ORDER — minimum qty {qty} {args.symbol} (status={inst.get('status')}):")
            _print(place_market_order(args.symbol, args.side, qty))
            if DRY_RUN:
                print("\n(DRY_RUN — nothing sent. Re-run with --live to place this tiny real demo order.)")
        if args.close:
            print(f"CLOSE POSITION ({args.symbol}):")
            _print(close_position(args.symbol))
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {str(e)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
