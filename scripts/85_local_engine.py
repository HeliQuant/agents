"""scripts/85 — LOCAL ENGINE: the data edge that feeds the cloud brain (local half of the connector).

HeliQuant is MODULAR by necessity: exchanges (Bybit/Binance) are geo-blocked from the cloud (Railway:
403/451), so the data + execution edge must run on a machine that CAN reach them (yours, via WARP). This
engine fetches fresh perp positioning data locally and POSTs it to the deployed cloud's `/ingest` endpoint,
so the always-on firm self-learns on LIVE data without needing exchange access itself.

JUDGES: run this on your machine (WARP on for Bybit), point it at the cloud URL + token, and the cloud
brain consumes your feed. Execution stays local too (scripts/82/83) — only the analysis/monitoring is cloud.

Setup:  set CLOUD_URL and INGEST_TOKEN (env or flags). INGEST_TOKEN must match Railway's.
Run:    python scripts/85_local_engine.py MNT --cloud https://...up.railway.app --once
        python scripts/85_local_engine.py MNT HYPE SUI --loop --interval 30   # continuous feeder
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def push_once(cloud: str, token: str, assets: list[str]) -> None:
    coll = import_module("scripts.73_collect_alt")
    for a in assets:
        a = a.upper()
        try:
            coll.collect(a)  # fetch fresh Bybit positioning -> data/{a}_positioning.csv (needs WARP)
        except Exception as e:  # noqa: BLE001
            print(f"  {a}: fetch FAILED ({str(e)[:70]}) — is WARP on / Bybit reachable?"); continue
        f = ROOT / "data" / f"{a.lower()}_positioning.csv"
        if not f.exists():
            print(f"  {a}: no csv produced — skip"); continue
        csv = f.read_text(encoding="utf-8")
        try:
            r = requests.post(cloud.rstrip("/") + "/ingest", json={"asset": a, "csv": csv},
                              headers={"Authorization": f"Bearer {token}"}, timeout=40)
            print(f"  {a}: ingest -> {r.status_code} {str(r.text)[:140]}")
        except Exception as e:  # noqa: BLE001
            print(f"  {a}: POST FAILED ({str(e)[:80]}) — cloud URL correct + reachable?")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="HeliQuant local data engine -> feeds the cloud /ingest.")
    ap.add_argument("assets", nargs="*", default=["MNT"], help="assets to fetch+push (default MNT)")
    ap.add_argument("--cloud", default=os.environ.get("CLOUD_URL", ""), help="cloud base URL (or env CLOUD_URL)")
    ap.add_argument("--token", default=os.environ.get("INGEST_TOKEN", ""), help="ingest token (or env INGEST_TOKEN)")
    ap.add_argument("--once", action="store_true", help="push once and exit")
    ap.add_argument("--loop", action="store_true", help="push continuously every --interval minutes")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    assets = [a.upper() for a in args.assets] or ["MNT"]
    if not args.cloud or not args.token:
        print("ERROR: need --cloud URL and --token (or env CLOUD_URL / INGEST_TOKEN). "
              "INGEST_TOKEN must match what you set in Railway."); return 1
    print(f"LOCAL ENGINE -> {args.cloud}  | assets={assets} | mode={'loop' if args.loop else 'once'}\n")
    while True:
        print(f"── push {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC ──")
        push_once(args.cloud, args.token, assets)
        if not args.loop:
            break
        time.sleep(max(args.interval, 1) * 60)
    print("\ndone. (Run with --loop to keep the cloud fed; keep WARP on.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
