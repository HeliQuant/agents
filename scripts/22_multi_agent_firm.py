"""HeliQuant Multi-Agent Firm — specialist analyst desk + portfolio-manager head.

Analysis + decision only (no on-chain self-maintenance phase). Thin entrypoint over
firm.organization (the single shared brain). For the FULL autonomous loop that also
self-manages its data sources, see scripts/24_autonomous_org.py.

Run: python scripts/22_multi_agent_firm.py [TICKER]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm.llm_client import active_provider_info  # noqa: E402
from firm.organization import run_firm  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MNT").upper()
    info = active_provider_info()
    print("=" * 66)
    print(f"  HeliQuant Multi-Agent Firm  |  asset: {ticker}")
    print(f"  provider: {info['provider']} ({info['style']}) | keys: {info.get('num_keys', 0)}")
    print("=" * 66)

    firm = run_firm(ticker, verbose=True)
    out = {"asset": ticker, "provider": info, **firm}
    (ROOT / "data" / f"firm_decision_{ticker.lower()}.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nsaved -> data/firm_decision_{ticker.lower()}.json")


if __name__ == "__main__":
    main()
