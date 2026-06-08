"""scripts/88 — CARRY + WHALE feeder: compute locally (Bybit reachable via WARP), persist to SUPABASE.

Bybit funding is geo-blocked from the cloud, so the carry desk can't compute there. This runs LOCALLY,
computes the live delta-neutral funding carry per symbol, and writes it to Supabase (`hq_state`). The cloud
carry desk reads it from Supabase — which SURVIVES redeploys (no more re-push after every deploy). Also
refreshes the Hyperliquid whale-leaderboard cache to Supabase so the cloud never does the heavy 32MB fetch.

Run:  python scripts/88_carry_push.py                       # once (uses SUPABASE creds from .env)
      python scripts/88_carry_push.py --loop --interval 360 # keep Supabase fed (carry moves slowly)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.carry_signal import live_carry  # noqa: E402

CARRY_SYMBOLS = ["HYPEUSDT", "SUIUSDT", "MNTUSDT", "BTCUSDT", "ETHUSDT"]


def push_once(symbols: list[str]) -> None:
    from firm import state_store
    for sym in symbols:
        try:
            c = live_carry(sym)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: compute FAILED ({str(e)[:60]}) — WARP on / Bybit reachable?"); continue
        if not c or str(c.get("source", "")).startswith("cached"):
            print(f"  {sym}: no live carry (Bybit unreachable here?)"); continue
        backend = state_store.save(f"carry:{sym.upper()}", c)
        print(f"  {sym}: carry {c['carry_ann_pct']:+.1f}%/yr ({c['verdict'][:28]}) -> {backend}")
    # refresh the HL whale-leaderboard cache into Supabase (cloud reads it, skips the 32MB fetch)
    try:
        from firm.hl_whales import top_addresses
        addrs = top_addresses(40, "month")
        print(f"  whale leaderboard -> Supabase: {len(addrs)} top traders cached")
    except Exception as e:  # noqa: BLE001
        print(f"  whale refresh skipped: {str(e)[:60]}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Feed carry + whale cache to Supabase (survives cloud redeploys).")
    ap.add_argument("symbols", nargs="*", default=CARRY_SYMBOLS)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=360)  # carry moves slowly -> every 6h is plenty
    args = ap.parse_args()
    syms = [s.upper() for s in args.symbols] or CARRY_SYMBOLS
    print(f"CARRY+WHALE feeder -> Supabase | symbols={syms}\n")
    while True:
        print(f"-- {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC --")
        push_once(syms)
        if not args.loop:
            break
        time.sleep(max(args.interval, 1) * 60)
    print("\ndone. (Persisted to Supabase -> survives redeploys. Run --loop to keep fresh; WARP on.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
