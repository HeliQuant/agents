"""HeliQuant — Autonomous Intelligence Organization (flagship entrypoint).

One command runs the full autonomous loop:
    PHASE 0 · OPERATIONS  — data steward self-maintenance (whale watchlist health -> migrate if needed)
    PHASE 1 · ANALYSIS    — 3 specialist analyst agents (each a different model)
    PHASE 2 · DECISION    — portfolio manager head -> ENTER / ABSTAIN

Deterministic tools compute the numbers + validated strategy; the LLM agents interpret,
the steward decides when to refresh data, the head decides the trade — all within the
OOS-validated rules. Provider-agnostic (BYO key). See firm/organization.py.

Run: python scripts/24_autonomous_org.py [TICKER]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.organization import run_organization  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MNT").upper()
    result = run_organization(ticker, verbose=True)
    out = ROOT / "data" / f"org_run_{ticker.lower()}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"saved -> data/org_run_{ticker.lower()}.json")


if __name__ == "__main__":
    main()
