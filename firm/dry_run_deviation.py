"""firm/dry_run_deviation.py — execution-vs-model honesty check (adopted from QuantDinger's dry_run_deviation).

Every edge in HeliQuant is validated against a MODELED round-trip cost (edge_lab: ~20 bps = fee+spread+
slippage). That assumption is only honest if it holds in REAL execution. scripts/81 runs live spot round-trips
on the exchange and logs the realized cost/round-trip (bps); here we compare realized-vs-modeled per asset:
  • real <= modeled  -> the backtest cost assumption HOLDS live; the OOS numbers are trustworthy.
  • real >  modeled  -> flag it; edges on that asset must be DISCOUNTED (real execution is worse than modeled).
Pure honesty layer — proves the validated numbers survive contact with a real order book. No alpha, just truth.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.edge_lab import COST  # noqa: E402

LOG = ROOT / "data" / "exec_cost_log.jsonl"
MODELED_RT_BPS = round(2 * COST * 1e4, 1)   # the backtest's assumed round-trip cost (fee + spread + slippage)


def log_measurement(symbol: str, cost_bps: float, filled: int, notional: float) -> dict:
    """Append one realized execution-cost measurement (called by scripts/81 after live round-trips)."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol.upper(),
           "cost_per_trade_bps": round(float(cost_bps), 1), "filled": int(filled), "notional": float(notional)}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def latest_by_symbol() -> dict:
    """Most-recent realized cost per symbol from the log (only runs that actually filled)."""
    out: dict = {}
    if not LOG.exists():
        return out
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("filled", 0) > 0:
            out[r["symbol"]] = r   # later lines overwrite -> latest wins
    return out


def report() -> list[dict]:
    """Compare realized round-trip cost vs the modeled cost, per symbol."""
    rows = []
    for sym, r in sorted(latest_by_symbol().items()):
        real = r["cost_per_trade_bps"]
        excess = round(real - MODELED_RT_BPS, 1)
        holds = real <= MODELED_RT_BPS
        rows.append({"symbol": sym, "real_rt_bps": real, "modeled_rt_bps": MODELED_RT_BPS,
                     "excess_bps": excess, "model_holds": holds, "filled": r["filled"],
                     "verdict": ("execution within model — OOS numbers hold live" if holds
                                 else f"execution {excess:+.1f} bps OVER model — discount edges on {sym}")})
    return rows


def brief() -> str:
    """One-line honesty note for FINDINGS / the PM. Empty if no measurements yet."""
    rows = report()
    if not rows:
        return ""
    ok = sum(1 for r in rows if r["model_holds"])
    parts = [f"{r['symbol']} {r['real_rt_bps']}bps" + ("✓" if r["model_holds"] else f"(+{r['excess_bps']} OVER)")
             for r in rows]
    return (f"EXECUTION-vs-MODEL ({ok}/{len(rows)} within {MODELED_RT_BPS}bps modeled cost): "
            + ", ".join(parts) + ". Real exchange fills validate the backtest cost assumption.")


if __name__ == "__main__":
    print(f"modeled round-trip cost (edge_lab): {MODELED_RT_BPS} bps\n")
    rows = report()
    for r in rows:
        print(f"  {r['symbol']:9} real {r['real_rt_bps']:>5} bps vs model {r['modeled_rt_bps']} bps -> "
              f"{'HOLDS' if r['model_holds'] else 'OVER '} ({r['excess_bps']:+} bps) | {r['verdict']}")
    print("\n" + (brief() or "no measurements yet — run scripts/81 (live exchange fills) to populate."))
