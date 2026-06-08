"""scripts/88 — CARRY PUSH: local half of the connector for the carry desk.

Bybit funding is geo-blocked from the cloud (Railway 403), so the carry desk can't compute there. This runs
LOCALLY (Bybit reachable via WARP), computes the live delta-neutral funding carry for each symbol, and POSTs
the result to the cloud /ingest endpoint. The cloud carry desk then reads the pushed result
(data/{symbol}_carry.json) instead of calling Bybit — so it works in the cloud too. Run alongside the data
engine (scripts/85), e.g. daily.

Run:  python scripts/88_carry_push.py --cloud https://<app>.up.railway.app --token <INGEST_TOKEN> --once
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.carry_signal import live_carry  # noqa: E402

CARRY_SYMBOLS = ["HYPEUSDT", "SUIUSDT", "MNTUSDT", "BTCUSDT", "ETHUSDT"]


def push_once(cloud: str, token: str, symbols: list[str]) -> None:
    for sym in symbols:
        try:
            c = live_carry(sym)  # computed from Bybit funding (needs WARP locally)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: compute FAILED ({str(e)[:60]}) — WARP on / Bybit reachable?"); continue
        if not c or c.get("source", "").startswith("cached"):
            print(f"  {sym}: no live carry (Bybit unreachable here?)"); continue
        try:
            r = requests.post(cloud.rstrip("/") + "/ingest", json={"asset": sym, "carry": c},
                              headers={"Authorization": f"Bearer {token}"}, timeout=30)
            print(f"  {sym}: carry {c['carry_ann_pct']:+.1f}%/yr ({c['verdict'][:30]}) -> ingest {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: POST FAILED ({str(e)[:70]})")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Push locally-computed carry to the cloud carry desk.")
    ap.add_argument("symbols", nargs="*", default=CARRY_SYMBOLS)
    ap.add_argument("--cloud", default=os.environ.get("CLOUD_URL", ""))
    ap.add_argument("--token", default=os.environ.get("INGEST_TOKEN", ""))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=360)  # carry moves slowly -> push every 6h is plenty
    args = ap.parse_args()
    syms = [s.upper() for s in args.symbols] or CARRY_SYMBOLS
    if not args.cloud or not args.token:
        print("ERROR: need --cloud URL and --token (or env CLOUD_URL / INGEST_TOKEN, matching Railway)."); return 1
    print(f"CARRY PUSH -> {args.cloud} | symbols={syms}\n")
    while True:
        print(f"-- push {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC --")
        push_once(args.cloud, args.token, syms)
        if not args.loop:
            break
        time.sleep(max(args.interval, 1) * 60)
    print("\ndone. (Run with --loop to keep the cloud carry desk fed; keep WARP on.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
