"""scripts/82 — SIGNAL-DRIVEN BUY-AND-HOLD executor (the fix from the live-trade lesson, NOT scalping).

Reads the validated edge's LIVE signal FIRST (read the market), and only acts on an actionable LONG: it
BUYS spot, then HOLDS for the edge's horizon (24h) — exiting when the horizon elapses OR the signal flips.
This is the opposite of the 100x round-trip stress test (which only paid the spread): a real edge-driven
position that needs the market to MOVE to profit. Spot-only, so it executes the LONG side of the edge
(SHORT needs derivatives, which the exchange geo-bans here -> use Hyperliquid). Profit = price move − realistic cost.

Run:  python scripts/82_edge_execute.py --open  --live --symbol MNTUSDT --notional 100   # enter if signal LONG
      python scripts/82_edge_execute.py --status                                          # show held position
      python scripts/82_edge_execute.py --manage --live                                   # exit if horizon/flip
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))
from firm import bybit_executor as ex  # noqa: E402
from firm.edge_lab import H as EDGE_HORIZON_H, live_signal  # noqa: E402

# The hold is NOT an arbitrary choice: it's pulled straight from edge_lab.H — the SAME holding window the
# edge's +25.1% OOS was validated on. So "buy then hold N hours" is the validated edge's own rule executed
# faithfully, not a test setting. Change the edge's horizon and the executor follows automatically.
HORIZON_H = EDGE_HORIZON_H
POS_FILE = DATA / "live_position.json"
# the executor signs against this venue; the price read below must use the SAME testnet host
EXCHANGE_TESTNET = "https://api-testnet.bybit.com"


def _price(symbol: str) -> float:
    """SPOT last price — MUST match the spot position's leg (testnet perp != spot, mixing them = fake P&L)."""
    import requests
    return float(requests.get(EXCHANGE_TESTNET + "/v5/market/tickers", params={"category": "spot", "symbol": symbol}, timeout=10)
                 .json()["result"]["list"][0]["lastPrice"])


def _bal(coin: str) -> float:
    res = ex._ok(ex._read_session().get_wallet_balance(accountType="UNIFIED"))
    for a in res.get("list", []):
        for c in a.get("coin", []):
            if c.get("coin") == coin:
                return float(c.get("walletBalance") or 0)
    return 0.0


def _spot(side: str, qty, symbol: str, quote: bool = False) -> dict:
    intended = {"category": "spot", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty)}
    if quote:
        intended["marketUnit"] = "quoteCoin"
    if ex.DRY_RUN:
        print(f"  [DRY] {side} spot {qty} {symbol} ({'USDT' if quote else 'base'}) — not sent")
        return {"dry_run": True}
    res = ex._ok(ex._trade_session().place_order(**intended))
    print(f"  [LIVE] {side} spot {qty} {symbol} -> orderId {res.get('orderId')}")
    return res


def open_position(symbol: str, edge: str, notional: float, live: bool) -> None:
    base = symbol[:-4]
    sig = live_signal(base, edge, DATA)
    s, act = sig.get("signal"), sig.get("actionable")
    print(f"READ MARKET: {symbol} edge={edge} -> signal={s}, actionable={act}")
    if s == "SHORT":
        print("  SHORT signal — spot can't short (derivatives geo-banned here). ABSTAIN. (route shorts to Hyperliquid.)")
        return
    if s != "LONG" or not act:
        print("  not an actionable LONG -> ABSTAIN. (reads the market first; no signal = no trade, no churn.)")
        return
    if POS_FILE.exists():
        print("  already holding a position (see --status); not stacking. ABSTAIN.")
        return
    ex.set_dry_run(not live)
    px = _price(symbol)
    before = _bal(base) if live else 0.0
    print(f"  signal LONG @ ~${px:.4f} -> BUY ${notional} {base} spot, then HOLD {HORIZON_H}h (per edge):")
    _spot("Buy", notional, symbol, quote=True)
    if not live:
        print("  (DRY — nothing sent. Add --live to enter for real.)")
        return
    time.sleep(1.5)
    qty = round(_bal(base) - before, 6)
    entry_px = notional / qty if qty > 0 else px
    now = datetime.now(timezone.utc)
    pos = {"symbol": symbol, "base": base, "edge": edge, "qty": qty, "entry_px": round(entry_px, 6),
           "notional": notional, "entry_utc": now.isoformat(),
           "hold_until_utc": (now + timedelta(hours=HORIZON_H)).isoformat(), "signal": "LONG"}
    POS_FILE.write_text(json.dumps(pos, indent=2))
    print(f"  ✓ OPENED: long {qty} {base} @ ~${entry_px:.4f}. HOLDING until {pos['hold_until_utc']} "
          f"(24h) or until the signal flips. THIS IS A HELD POSITION — not a scalp. Profit needs price to move up.")


def status() -> None:
    if not POS_FILE.exists():
        print("no open position. (run --open to enter when the edge signals LONG)")
        return
    pos = json.loads(POS_FILE.read_text())
    px = _price(pos["symbol"])
    unreal = (px - pos["entry_px"]) * pos["qty"]
    now = datetime.now(timezone.utc)
    left = (datetime.fromisoformat(pos["hold_until_utc"]) - now).total_seconds() / 3600
    print(f"HOLDING {pos['qty']} {pos['base']} @ entry ${pos['entry_px']:.4f} | now ${px:.4f} "
          f"| unrealized P&L ${unreal:+.2f} | {left:+.1f}h until horizon exit")


def manage(live: bool) -> None:
    if not POS_FILE.exists():
        print("no open position to manage.")
        return
    pos = json.loads(POS_FILE.read_text())
    base = pos["base"]
    now = datetime.now(timezone.utc)
    sig = live_signal(base, pos["edge"], DATA)
    expired = now >= datetime.fromisoformat(pos["hold_until_utc"])
    flipped = sig.get("signal") != "LONG"
    px = _price(pos["symbol"])
    if not (expired or flipped):
        left = (datetime.fromisoformat(pos["hold_until_utc"]) - now).total_seconds() / 3600
        print(f"HOLDING {base}: {left:.1f}h left, signal still LONG, unrealized ${(px-pos['entry_px'])*pos['qty']:+.2f}"
              f" -> KEEP HOLDING (not churning).")
        return
    reason = "24h horizon elapsed" if expired else "signal flipped (no longer LONG)"
    ex.set_dry_run(not live)
    print(f"EXIT ({reason}): selling {base} spot...")
    bal = _bal(base) if live else pos["qty"]
    _spot("Sell", round(bal, 5), pos["symbol"])
    if live:
        realized = (px - pos["entry_px"]) * pos["qty"]
        print(f"  ✓ CLOSED: held to {reason}. exit ~${px:.4f} vs entry ${pos['entry_px']:.4f} | realized ~${realized:+.2f}")
        POS_FILE.unlink()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Signal-driven buy-and-hold executor (exchange spot, testnet).")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--manage", action="store_true")
    ap.add_argument("--symbol", default="MNTUSDT")
    ap.add_argument("--edge", default="oi_contrarian")
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    print(f"[edge-exec] {args.symbol} edge={args.edge} live={args.live}  (BUY-AND-HOLD, not scalp)\n")
    try:
        if args.open:
            open_position(args.symbol, args.edge, args.notional, args.live)
        elif args.manage:
            manage(args.live)
        else:
            status()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}: {str(e)[:160]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
