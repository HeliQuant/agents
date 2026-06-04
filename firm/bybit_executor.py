"""firm/bybit_executor.py — Line-1 execution (Path A): LIVE perp orders on Bybit TESTNET.

This is the venue-execution half of HeliQuant's DUAL-EXECUTION model. On an ENTER decision the
org records the trade two ways:
  * Line 1 (this module)        a real USDT-perp market order on Bybit testnet  (the trade actually
                                 fires on a liquid venue — verifiable in the Bybit account / order
                                 history) — paired with —
  * Line 2 (onchain_recorder)   an immutable SHA-256 anchor of the decision on Mantle Sepolia.

WHY a CEX testnet and not an on-chain swap: the Mantle DEX for MNT is thin (high slippage / liquidity
risk — our S3 finding). Bybit's MNTUSDT perp is liquid (25x lev listed), so it's the honest place to
execute a directional ticket; the on-chain line keeps the auditable, can't-be-faked record. Testnet =
no real capital at risk while we prove the full path end-to-end.

SIGNING: we use pybit (`from pybit.unified_trading import HTTP`) which performs Bybit v5 HMAC signing
(X-BAPI-SIGN + timestamp + recv-window) correctly. We deliberately do NOT hand-roll auth.

SAFETY (defense in depth, nothing fires by accident):
  1. ``DRY_RUN`` module flag defaults True — ``place_market_order`` LOGS the intended order and
     returns without sending. A live order requires DRY_RUN=False (CLI: pass ``--live``).
  2. Two SEPARATE sessions: a READ session (read-only key) for balances/positions, and a TRADE
     session (trade key) for orders — least privilege; queries never touch the order-capable key.
  3. ``decision_to_order`` is HARD-GATED: it returns an order spec ONLY for a PM ``ENTER`` whose
     OOS-validated edge is live + actionable (it mirrors the org's own gate). ABSTAIN -> None.
  4. Leverage is capped to ``trade_ticket.LEVERAGE_CAP``; the ticket's own notional/size is respected.

Sessions are LAZY-INIT, so ``import firm.bybit_executor`` needs no keys and touches no network — the
sandbox (which cannot reach Bybit) can import + offline-verify; live calls run on the user's machine.

Keys (read from agents/.env then project-root .env — values NEVER printed):
  BYBIT_API_KEY / BYBIT_API_SECRET                    (trade-enabled)
  BYBIT_API_KEY_READONLY / BYBIT_API_SECRET_READONLY  (read-only)
  BYBIT_TESTNET=true                                  (truthy -> testnet endpoint)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Mirror trade_ticket's cap so venue leverage can never exceed the ticket layer's policy.
from firm import trade_ticket as tt

ROOT = Path(__file__).resolve().parents[1]

# v5 USDT-perpetuals live under the "linear" product category. MNT's testnet perp is MNTUSDT —
# the exact symbol used by scripts/35_collect_bybit.py + scripts/37_collect_bybit_positioning.py.
CATEGORY = "linear"
DEFAULT_SYMBOL = "MNTUSDT"

# ── SAFETY: nothing fires live unless this is explicitly turned off (CLI --live / set_dry_run). ──
DRY_RUN = True

LEVERAGE_CAP = tt.LEVERAGE_CAP  # notional <= LEVERAGE_CAP x equity (single source of truth = ticket layer)

_TRUTHY = {"1", "true", "yes", "y", "on"}

# Lazy session singletons (None until first use) so importing this module needs no keys/network.
_READ_SESSION = None
_TRADE_SESSION = None


# ─────────────────────────────── env loading (no values ever printed) ───────────────────────────────
def _env(key: str) -> str | None:
    """Read an env var from the process env, then agents/.env, then project-root .env — the SAME
    multi-encoding fallback as firm/onchain_recorder._env. Returns the value (callers must NEVER
    print secret values)."""
    v = os.environ.get(key)
    if v:
        return v
    for envfile in (ROOT / ".env", ROOT.parent / ".env"):  # agents/.env, then project-root .env
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                for line in envfile.read_text(encoding=enc).splitlines():
                    line = line.lstrip("﻿").strip()
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
            except (UnicodeError, ValueError, FileNotFoundError):
                continue
    return None


def is_testnet() -> bool:
    """True unless BYBIT_TESTNET is explicitly falsy. Default-safe: absent -> testnet."""
    raw = _env("BYBIT_TESTNET")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def set_dry_run(enabled: bool) -> None:
    """Toggle the module DRY_RUN flag (the CLI's --live sets this False)."""
    global DRY_RUN
    DRY_RUN = bool(enabled)


# ─────────────────────────────── lazy session factories ───────────────────────────────
def _new_session(key: str | None, secret: str | None):
    """Build a pybit v5 HTTP session (import pybit lazily so module import needs no dependency/network).
    A wide recv_window (20s) absorbs clock skew between the host and Bybit's server (ErrCode 10002)."""
    from pybit.unified_trading import HTTP  # lazy: import only when a session is actually needed
    return HTTP(testnet=is_testnet(), api_key=key, api_secret=secret, recv_window=20000)


def _read_session():
    """READ session — uses the READ-ONLY key (balances/positions). Least privilege for queries."""
    global _READ_SESSION
    if _READ_SESSION is None:
        key, secret = _env("BYBIT_API_KEY_READONLY"), _env("BYBIT_API_SECRET_READONLY")
        if not (key and secret):
            raise RuntimeError(
                "missing BYBIT_API_KEY_READONLY / BYBIT_API_SECRET_READONLY in .env (read session)"
            )
        _READ_SESSION = _new_session(key, secret)
    return _READ_SESSION


def _trade_session():
    """TRADE session — uses the TRADE-enabled key (order placement only)."""
    global _TRADE_SESSION
    if _TRADE_SESSION is None:
        key, secret = _env("BYBIT_API_KEY"), _env("BYBIT_API_SECRET")
        if not (key and secret):
            raise RuntimeError("missing BYBIT_API_KEY / BYBIT_API_SECRET in .env (trade session)")
        _TRADE_SESSION = _new_session(key, secret)
    return _TRADE_SESSION


def _ok(resp: dict) -> dict:
    """Unwrap a v5 response; raise on non-zero retCode (retMsg only — never echoes credentials)."""
    if not isinstance(resp, dict):
        raise RuntimeError(f"unexpected Bybit response type: {type(resp).__name__}")
    if resp.get("retCode") not in (0, None):
        raise RuntimeError(f"Bybit retCode {resp.get('retCode')}: {resp.get('retMsg')}")
    return resp.get("result", {}) or {}


# ─────────────────────────────── read-only operations ───────────────────────────────
def check_auth() -> dict:
    """Verify BOTH keys end-to-end (read-only, safe). READ key -> wallet-balance; TRADE key ->
    a benign account-info query (proves the order key authenticates without placing anything).
    Returns a status dict; secret values are never included."""
    status: dict = {"testnet": is_testnet(), "read_key_ok": False, "trade_key_ok": False}
    try:
        _ok(_read_session().get_wallet_balance(accountType="UNIFIED"))
        status["read_key_ok"] = True
    except Exception as e:  # noqa: BLE001
        status["read_error"] = str(e)[:160]
    try:
        # account-info needs a signed request -> proves the TRADE key/secret authenticate (no order sent).
        _ok(_trade_session().get_account_info())
        status["trade_key_ok"] = True
    except Exception as e:  # noqa: BLE001
        status["trade_error"] = str(e)[:160]
    status["both_keys_ok"] = status["read_key_ok"] and status["trade_key_ok"]
    return status


def get_wallet_balance(account_type: str = "UNIFIED") -> dict:
    """USDT balance summary from the READ session. {equity, available, wallet_balance, coins:[...]}"""
    res = _ok(_read_session().get_wallet_balance(accountType=account_type))
    out: dict = {"account_type": account_type, "equity_usdt": None, "available_usdt": None,
                 "wallet_balance_usdt": None, "coins": []}
    for acct in res.get("list", []):
        if acct.get("totalEquity"):
            out["total_equity"] = float(acct["totalEquity"])
        for c in acct.get("coin", []):
            coin = c.get("coin")
            eq = c.get("equity") or c.get("usdValue")
            avail = c.get("availableToWithdraw") or c.get("walletBalance")
            out["coins"].append({"coin": coin,
                                 "equity": float(eq) if eq not in (None, "") else None,
                                 "wallet_balance": float(c["walletBalance"]) if c.get("walletBalance") else None})
            if coin == "USDT":
                out["equity_usdt"] = float(eq) if eq not in (None, "") else None
                out["available_usdt"] = float(avail) if avail not in (None, "") else None
                out["wallet_balance_usdt"] = float(c["walletBalance"]) if c.get("walletBalance") else None
    return out


def get_positions(symbol: str = DEFAULT_SYMBOL) -> list[dict]:
    """Open linear positions for ``symbol`` from the READ session. Empty list = flat."""
    res = _ok(_read_session().get_positions(category=CATEGORY, symbol=symbol))
    out: list[dict] = []
    for p in res.get("list", []):
        size = float(p.get("size") or 0)
        if size == 0:
            continue
        out.append({
            "symbol": p.get("symbol"), "side": p.get("side"), "size": size,
            "avg_price": float(p["avgPrice"]) if p.get("avgPrice") else None,
            "leverage": float(p["leverage"]) if p.get("leverage") else None,
            "unrealised_pnl": float(p["unrealisedPnl"]) if p.get("unrealisedPnl") else None,
            "position_value": float(p["positionValue"]) if p.get("positionValue") else None,
        })
    return out


def get_instrument(symbol: str = DEFAULT_SYMBOL) -> dict:
    """Public instrument filters (min qty / qty step / max leverage). Read session; no auth needed
    but reusing it is fine. Used to size a valid order + the demo's minimum-qty order."""
    res = _ok(_read_session().get_instruments_info(category=CATEGORY, symbol=symbol))
    lst = res.get("list", [])
    if not lst:
        raise RuntimeError(f"no instrument info for {symbol} ({CATEGORY})")
    it = lst[0]
    lot = it.get("lotSizeFilter", {})
    lev = it.get("leverageFilter", {})
    return {
        "symbol": it.get("symbol"), "status": it.get("status"),
        "min_order_qty": float(lot.get("minOrderQty")) if lot.get("minOrderQty") else None,
        "qty_step": float(lot.get("qtyStep")) if lot.get("qtyStep") else None,
        "max_leverage": float(lev.get("maxLeverage")) if lev.get("maxLeverage") else None,
    }


# ─────────────────────────────── order placement (DRY-RUN guarded) ───────────────────────────────
def _round_qty(qty: float, step: float | None) -> float:
    """Floor qty to the instrument's lot step (Bybit rejects off-grid qty)."""
    if not step or step <= 0:
        return qty
    return (int(qty / step)) * step


def place_market_order(symbol: str, side: str, qty: float, reduce_only: bool = False) -> dict:
    """Place a linear market order via the TRADE session.

    DRY_RUN (default True): LOGS the intended order and returns {"dry_run": True, ...} WITHOUT sending.
    Set DRY_RUN=False (CLI --live / set_dry_run(False)) to actually transmit. ``reduce_only=True`` only
    shrinks/closes an existing position (never opens)."""
    side = side.capitalize()  # Bybit expects "Buy" / "Sell"
    if side not in ("Buy", "Sell"):
        raise ValueError(f"side must be Buy/Sell, got {side!r}")
    intended = {"category": CATEGORY, "symbol": symbol, "side": side, "orderType": "Market",
                "qty": str(qty), "reduceOnly": reduce_only}
    if DRY_RUN:
        print(f"[DRY_RUN] would place {side} MARKET {qty} {symbol} "
              f"(reduceOnly={reduce_only}, testnet={is_testnet()}) - NOT sent. "
              f"Set DRY_RUN=False / pass --live to transmit.")
        return {"dry_run": True, "sent": False, "intended_order": intended}
    res = _ok(_trade_session().place_order(**intended))
    print(f"[LIVE] {side} MARKET {qty} {symbol} sent (testnet={is_testnet()}) -> orderId {res.get('orderId')}")
    return {"dry_run": False, "sent": True, "order_id": res.get("orderId"),
            "order_link_id": res.get("orderLinkId"), "intended_order": intended}


def close_position(symbol: str = DEFAULT_SYMBOL) -> dict:
    """Flatten any open position on ``symbol`` with an opposite-side reduce-only market order.
    Respects DRY_RUN. No-op (returns flat) when there's nothing to close."""
    pos = get_positions(symbol)
    if not pos:
        return {"closed": False, "reason": "no open position", "symbol": symbol}
    p = pos[0]
    opp = "Sell" if p["side"] == "Buy" else "Buy"
    res = place_market_order(symbol, opp, p["size"], reduce_only=True)
    return {"closed": not res.get("dry_run", True), "symbol": symbol,
            "was": {"side": p["side"], "size": p["size"]}, "close_order": res}


# ─────────────────────────────── decision -> order mapper (HARD-GATED) ───────────────────────────────
def _validated_edge_actionable(decision: dict) -> tuple[bool, str]:
    """Mirror the ORG's own ENTER gate (organization.PM_SYS 'THE ONE EXCEPTION' + _edge_profile):
    a venue order is justified ONLY when the OI-Contrarian desk reports edge_validated=true AND
    actionable=true AND its live_signal direction agrees with the PM's direction. We re-derive this
    from the decision's attached desks/ticket so the executor enforces the SAME boundary the org does
    — defense in depth, not a second opinion.

    Accepts the edge context either as ``decision['oi_contrarian']`` (the desk output, if the caller
    attaches it) or, failing that, infers from ``trade_ticket.edge_validated`` + mode AGGRESSIVE
    (which the ticket layer sets ONLY when the live validated edge aligned — see _edge_profile)."""
    dirn = str(decision.get("direction", "")).upper()
    oi = decision.get("oi_contrarian") or {}
    if oi:
        if not (oi.get("edge_validated") and oi.get("actionable")):
            return False, "OI-Contrarian desk not validated+actionable -> no order"
        sig = str(oi.get("live_signal", "")).upper()
        if sig and sig != dirn:
            return False, f"validated edge signal {sig} != decision {dirn} -> no order"
        return True, f"validated OI-contrarian edge live + aligned ({dirn})"
    # Fallback: the ticket layer only stamps mode=AGGRESSIVE + edge_validated when the live validated
    # edge aligned with direction (organization._edge_profile -> size_position). Trust that stamp.
    tk = decision.get("trade_ticket") or {}
    if tk.get("edge_validated") and str(tk.get("mode", "")).upper().startswith("AGGRESSIVE"):
        return True, f"ticket edge_validated + AGGRESSIVE ({dirn}) -> validated edge was live + aligned"
    return False, "no validated-edge context (oi_contrarian/ticket) -> disciplined no-order (matches org abstain bias)"


def decision_to_order(decision: dict, equity_usdt: float, symbol: str = DEFAULT_SYMBOL) -> dict | None:
    """Map a firm decision -> a Bybit order spec, or None (no order).

    Returns None UNLESS ALL hold (mirrors the org's ENTER discipline):
      * decision['decision'] == 'ENTER' and a directional LONG/SHORT,
      * a VALID, attached trade_ticket (gates already passed in finalize_decision),
      * the validated-edge gate passes (validated OI-contrarian edge live + aligned).
    Sizing RESPECTS the ticket: notional = position_size_pct% of equity (the ticket already capped it
    at LEVERAGE_CAP x and crowding-sized it down); leverage is re-capped here as belt-and-suspenders.
    qty = notional_usd / last_price, floored to the instrument lot step.

    The returned spec is descriptive — it does NOT place anything. Pass it to place_market_order
    (which is itself DRY_RUN-guarded)."""
    if str(decision.get("decision", "")).upper() != "ENTER":
        return None
    dirn = str(decision.get("direction", "")).upper()
    if dirn not in ("LONG", "SHORT"):
        return None
    ticket = decision.get("trade_ticket")
    if not isinstance(ticket, dict) or not ticket.get("valid"):
        return None  # no gated ticket -> no order (finalize_decision would have ABSTAINed)
    ok, why = _validated_edge_actionable(decision)
    if not ok:
        return None

    side = "Buy" if dirn == "LONG" else "Sell"
    # Respect the ticket's sizing; re-cap leverage as defense in depth.
    leverage = min(float(ticket.get("leverage", 1.0) or 1.0), LEVERAGE_CAP)
    size_pct = float(ticket.get("position_size_pct", leverage * 100.0) or 0.0)
    notional = min(equity_usdt * (size_pct / 100.0), equity_usdt * LEVERAGE_CAP)

    last_price = float(ticket.get("entry") or 0.0)
    inst = {}
    try:
        inst = get_instrument(symbol)  # for lot-step rounding + min qty (best-effort; offline -> skip)
    except Exception:  # noqa: BLE001
        pass
    qty = (notional / last_price) if last_price > 0 else 0.0
    qty = _round_qty(qty, inst.get("qty_step"))
    min_qty = inst.get("min_order_qty")
    if min_qty and qty < min_qty:
        qty = min_qty  # never below the venue minimum (keeps a tiny-but-valid order)

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
        prog="python -m firm.bybit_executor",
        description="HeliQuant Bybit TESTNET executor (Path A). Read ops are safe; orders are DRY_RUN "
                    "unless --live is passed. Run on a network that can reach Bybit (e.g. WARP).")
    ap.add_argument("--check", action="store_true", help="verify BOTH keys + show USDT balance (read-only, safe)")
    ap.add_argument("--positions", action="store_true", help="show open positions for --symbol")
    ap.add_argument("--demo-order", action="store_true",
                    help="tiny minimum-qty MARKET order (DRY_RUN print unless --live is also passed)")
    ap.add_argument("--close", action="store_true", help="flatten open position on --symbol (respects --live)")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"perp symbol (default {DEFAULT_SYMBOL})")
    ap.add_argument("--side", default="Buy", choices=["Buy", "Sell"], help="demo-order side (default Buy)")
    ap.add_argument("--live", action="store_true",
                    help="DISABLE DRY_RUN — actually transmit orders (testnet). Without this, orders are printed only.")
    args = ap.parse_args(argv)

    if args.live:
        set_dry_run(False)
    print(f"[bybit_executor] testnet={is_testnet()}  DRY_RUN={DRY_RUN}  symbol={args.symbol}\n")

    if not any([args.check, args.positions, args.demo_order, args.close]):
        ap.print_help()
        return 0

    try:
        if args.check:
            st = check_auth()
            print("AUTH CHECK:")
            _print(st)
            if st.get("read_key_ok"):
                print("\nWALLET BALANCE (USDT):")
                _print(get_wallet_balance())
            if not st.get("both_keys_ok"):
                print("\nNOTE: a key failed — verify .env keys + that this host can reach Bybit (HTTP 000 = blocked).")

        if args.positions:
            print(f"OPEN POSITIONS ({args.symbol}):")
            pos = get_positions(args.symbol)
            _print(pos if pos else {"flat": True, "symbol": args.symbol})

        if args.demo_order:
            inst = get_instrument(args.symbol)
            qty = inst.get("min_order_qty") or 1.0
            print(f"DEMO ORDER — minimum qty {qty} {args.symbol} (instrument: status={inst.get('status')}, "
                  f"maxLev={inst.get('max_leverage')}):")
            _print(place_market_order(args.symbol, args.side, qty))
            if DRY_RUN:
                print("\n(DRY_RUN — nothing was sent. Re-run with --live to place this tiny real testnet order.)")

        if args.close:
            print(f"CLOSE POSITION ({args.symbol}):")
            _print(close_position(args.symbol))
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {str(e)[:200]}")
        print("If this is HTTP 000 / timeout: the host cannot reach Bybit — run on a WARP-enabled machine.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
