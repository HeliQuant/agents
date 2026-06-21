"""scripts/bitget_paper_trade.py — HeliQuant decisions EXECUTED on Bitget demo futures + Bitget-format log.

The firm's quantitative desks vote a direction (flow-intel + funding/OI z-extremes + Hyperliquid whale),
sized by conviction tier; we EXECUTE on **Bitget demo** (SUSDT-FUTURES, no real funds — long AND short),
log every fill in the Bitget submission format, then flatten and log the close (balance change). Real
prices, real Bitget execution, real desk reasoning — nothing invented.

Bitget demo supports BTC/ETH/XRP (S-prefixed). Run: python scripts/bitget_paper_trade.py
Output: data/bitget_paper_log.json  (the submission's paper-trading log)
"""
from __future__ import annotations

import importlib.util as iu
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from firm import bitget_adapter as bg  # noqa: E402

# load the floor module (digit-prefixed) for its desk_votes()
_spec = iu.spec_from_file_location("fc", ROOT / "scripts" / "89_live_campaign.py")
fc = iu.module_from_spec(_spec)
_spec.loader.exec_module(fc)

ASSETS = ["BTC", "ETH"]      # Bitget demo futures pairs
NOTIONAL = 200.0             # demo $ per position
LOG = ROOT / "data" / "bitget_paper_log.json"


def main() -> int:
    print("=== HeliQuant × Bitget demo paper-trade ===")
    st = bg.status()
    print(f"venue: {st.get('product')} | connected: {st.get('connected')} | balance: {st.get('available')} SUSDT")
    if not st.get("connected"):
        print("Bitget not connected — abort.")
        return 1
    try:
        bg.set_position_mode(True)
    except Exception as e:  # noqa: BLE001
        print("set-mode note:", str(e)[:80])

    entries = []
    for a in ASSETS:
        net, reasons = fc.desk_votes(a)
        if net == 0 or not reasons:
            print(f"{a}: desks flat (net 0) → ABSTAIN (the firm doesn't coin-flip)")
            continue
        direction = "LONG" if net > 0 else "SHORT"
        side = "buy" if net > 0 else "sell"
        px = bg.get_ticker(a)
        qty = bg.round_size(a, NOTIONAL, px)
        sl = round(px * (1 - 0.02), 2) if direction == "LONG" else round(px * (1 + 0.02), 2)
        tp = round(px * (1 + 0.04), 2) if direction == "LONG" else round(px * (1 - 0.04), 2)
        r = bg.place_market_order(a, side, qty)
        time.sleep(1.2)
        avail, _ = bg.get_balance()
        e = {
            "event": "OPEN",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pair": bg.to_symbol(a), "side": direction, "price": px, "size": qty,
            "venue": "bitget-demo-futures", "tier": "STRONG" if abs(net) >= 2 else "LEAN",
            "desk_signals": {"net_votes": net, "reasons": reasons[:5]},
            "pm_decision": f"ENTER {direction} (desk-consensus, exploration class)",
            "risk_params": {"stop_loss": sl, "take_profit": tp, "rr_ratio": 2.0},
            "order_id": r.get("orderId") if isinstance(r, dict) else r,
            "balance_susdt": round(avail, 2),
        }
        entries.append(e)
        print(f"  🟢 OPEN {direction:5} {a:4} @ {px} qty {qty} (net {net:+}) — {'; '.join(reasons[:3])}")

    # flatten + log the closes (balance change)
    if entries:
        time.sleep(2)
        for c in bg.flatten():
            a = next((x for x, s in bg.DEMO_SYMBOLS.items() if s == c.get("symbol")), c.get("symbol"))
            px = bg.get_ticker(a) if a in bg.DEMO_SYMBOLS else None
            entries.append({
                "event": "CLOSE", "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pair": c.get("symbol"), "closed_size": c.get("closed"), "exit_price": px,
                "order_id": c.get("orderId"),
            })
            print(f"  ⬜ CLOSE {c.get('symbol')} size {c.get('closed')}")
        avail, eq = bg.get_balance()
        print(f"  final balance: {round(avail,2)} SUSDT")

    LOG.write_text(json.dumps({"venue": "bitget-demo-futures", "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "note": "HeliQuant desk-driven decisions executed on Bitget demo (no real funds); exploration 4h-class, real prices",
                               "trades": entries}, indent=2), encoding="utf-8")
    print(f"\nsaved → {LOG}  ({len([e for e in entries if e['event']=='OPEN'])} opens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
