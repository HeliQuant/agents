"""scripts/83 — FIRM-GOVERNED execution loop: HeliQuant the FIRM decides, the executor OBEYS.

The fix for the raw-edge executor (scripts/82, which bypassed the brain). Each cycle runs the FULL org
(7 desks -> bull/bear debate -> PM -> deterministic R:R + rugpull gates). ONLY if the PM returns ENTER
LONG with a VALID ticket does it BUY spot and HOLD per the edge horizon. On ABSTAIN it takes no trade (and
closes any open un-sanctioned position). Trades are firm-governed, not signal-mentah.

HONEST about "100 trades": HeliQuant is DISCIPLINED — it ABSTAINS far more than it trades (the R:R≥2.0
gate, regime/on-chain vetoes, etc.). So trades accumulate SLOWLY over real time as the market evolves;
re-running at the same moment gives the same ABSTAIN. 100 real trades is a long-horizon process — forcing
them quickly would mean breaking the very gates that make the firm trustworthy. This loop runs naturally.

Run:  python scripts/83_firm_governed_exec.py --symbol MNTUSDT --cycles 1            # one firm analysis (DRY exec)
      python scripts/83_firm_governed_exec.py --symbol MNTUSDT --cycles 50 --live    # natural loop, gated trades
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
from firm.edge_lab import H as EDGE_H  # noqa: E402
from firm.organization import run_organization  # noqa: E402

POS_FILE = DATA / "live_position.json"
LEDGER = DATA / "firm_trades.jsonl"


def _bal(coin: str) -> float:
    res = ex._ok(ex._read_session().get_wallet_balance(accountType="UNIFIED"))
    return float(next((c.get("walletBalance") for a in res.get("list", []) for c in a.get("coin", [])
                       if c.get("coin") == coin), 0) or 0)


def _spot(side: str, qty, symbol: str, quote: bool = False) -> None:
    intended = {"category": "spot", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty)}
    if quote:
        intended["marketUnit"] = "quoteCoin"
    if ex.DRY_RUN:
        print(f"    [DRY] {side} spot {qty} {symbol} — not sent")
        return
    res = ex._ok(ex._trade_session().place_order(**intended))
    print(f"    [LIVE] {side} spot {qty} {symbol} -> orderId {res.get('orderId')}")


def execute_decision(symbol: str, dec: dict, notional: float, live: bool) -> str:
    """Obey the PM: ENTER LONG + valid ticket -> buy+hold; ABSTAIN -> close any open position."""
    base = symbol[:-4]
    d = str(dec.get("decision", "")).upper()
    direction = str(dec.get("direction", "")).upper()
    ticket_ok = bool((dec.get("trade_ticket") or {}).get("valid"))
    ex.set_dry_run(not live)
    if d == "ENTER" and direction == "LONG" and ticket_ok:
        if POS_FILE.exists():
            print("    already holding -> no stack."); return "HOLD"
        print(f"    PM ENTER LONG + valid ticket -> BUY ${notional} {base} spot, HOLD {EDGE_H}h:")
        before = _bal(base) if live else 0.0
        _spot("Buy", notional, symbol, quote=True)
        if live:
            time.sleep(1.5)
            qty = round(_bal(base) - before, 6)
            now = datetime.now(timezone.utc)
            POS_FILE.write_text(json.dumps({"symbol": symbol, "base": base, "qty": qty,
                "notional": notional, "entry_utc": now.isoformat(),
                "hold_until_utc": (now + timedelta(hours=EDGE_H)).isoformat()}))
            LEDGER.open("a").write(json.dumps({"t": now.isoformat(), "symbol": symbol, "action": "ENTER", "qty": qty}) + "\n")
        return "TRADE"
    # ABSTAIN (or non-LONG / invalid ticket) -> ensure flat
    if POS_FILE.exists():
        pos = json.loads(POS_FILE.read_text())
        now = datetime.now(timezone.utc)
        expired = now >= datetime.fromisoformat(pos["hold_until_utc"])
        if expired:
            print("    firm no longer ENTER + hold elapsed -> close position.")
            bal = _bal(base) if live else pos["qty"]
            _spot("Sell", round(bal, 2), symbol)
            if live:
                POS_FILE.unlink()
                LEDGER.open("a").write(json.dumps({"t": now.isoformat(), "symbol": symbol, "action": "EXIT"}) + "\n")
            return "EXIT"
        print("    firm ABSTAIN but a sanctioned position is mid-hold -> keep holding to horizon.")
        return "HOLD"
    return "FLAT"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Firm-governed execution loop (org decides, executor obeys).")
    ap.add_argument("--symbol", default="MNTUSDT")
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--interval", type=int, default=0, help="seconds between cycles (0 = back-to-back)")
    ap.add_argument("--target-trades", type=int, default=0, help="stop after this many TRADEs (0 = run all cycles)")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    base = args.symbol[:-4]
    print(f"[firm-governed] {args.symbol} cycles={args.cycles} live={args.live}")
    print("HeliQuant decides (7 desks + PM + gates); executor OBEYS. Disciplined -> abstains often.\n")
    trades = 0
    for i in range(args.cycles):
        print(f"── cycle {i+1}/{args.cycles} · {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC ──")
        try:
            res = run_organization(base, verbose=False)
            dec = res.get("decision", {})
            print(f"  PM: {str(dec.get('decision','?')).upper()} {dec.get('direction','')} "
                  f"| {str(dec.get('reasoning') or dec.get('ticket_note') or '')[:110]}")
            action = execute_decision(args.symbol, dec, args.notional, args.live)
            print(f"  -> {action}")
            if action == "TRADE":
                trades += 1
        except Exception as e:  # noqa: BLE001
            print(f"  cycle error: {str(e)[:100]}")
        if args.target_trades and trades >= args.target_trades:
            print(f"\nreached {trades} trades."); break
        if args.interval and i < args.cycles - 1:
            time.sleep(args.interval)
    print(f"\nDONE: {args.cycles} analyses, {trades} firm-sanctioned TRADEs. "
          f"(Few trades = the discipline working, not a bug.)")


if __name__ == "__main__":
    raise SystemExit(main())
