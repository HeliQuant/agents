"""scripts/84 — DATA BRIDGE: keep the always-on cloud loop fed with fresh Bybit data.

Bybit (and Binance) are CloudFront/geo-BLOCKED from Railway's IP (verified via /probe: 403/451), but they
work fine from your LOCAL machine (Bybit via WARP). So this machine acts as the gateway: it fetches fresh
perp positioning data (public data — nothing sketchy), commits it, and pushes to HeliQuant/agents. Railway
auto-deploys on push, and the cloud loop re-validates / self-learns on the NEW data.

Run this periodically (e.g. daily, with WARP on). Schedule it via Windows Task Scheduler for hands-off.

Run:  python scripts/84_push_data.py                 # MNT (the validated edge) by default
      python scripts/84_push_data.py MNT HYPE SUI     # refresh several
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _git(*args) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    assets = [a.upper() for a in sys.argv[1:]] or ["MNT"]
    print(f"DATA BRIDGE — fetching fresh Bybit positioning for {assets} (needs WARP on for Bybit)\n")

    from importlib import import_module
    coll = import_module("scripts.73_collect_alt")
    fetched = []
    for a in assets:
        try:
            coll.collect(a)  # writes data/{a.lower()}_positioning.csv
            fetched.append(a)
        except Exception as e:  # noqa: BLE001
            print(f"  {a} FETCH FAILED: {str(e)[:80]} (Bybit reachable? WARP on?)")

    if not fetched:
        print("\nnothing fetched — is WARP on / Bybit reachable? Aborting (no push).")
        return 1

    # stage only the positioning CSVs we refreshed, then commit + push
    paths = [f"data/{a.lower()}_positioning.csv" for a in fetched]
    rc, _ = _git("add", *paths)
    rc, out = _git("status", "--porcelain", *paths)
    if not out.strip():
        print("\nno data changes since last push (market data identical) — skipping commit.")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"chore(data): refresh positioning {','.join(fetched)} @ {stamp} (data-bridge for cloud)"
    rc, out = _git("commit", "-q", "-m", msg, "-m",
                   "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
    if rc != 0:
        print("commit failed:", out[:200]); return 1
    rc, out = _git("push", "origin", "main")
    if rc != 0:
        print("push failed:", out[:200]); return 1
    print(f"\n✓ PUSHED fresh data for {','.join(fetched)} -> Railway will auto-deploy + self-learn on it.")
    print("  (Schedule this daily via Task Scheduler to keep the cloud loop fed automatically.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
