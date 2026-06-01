"""Forward-log smart-money whale flow — builds the time-series for TREND + VALIDATION.

Each run snapshots the current whale_flow_summary + spot price + timestamp to
data/whale_flow_log.jsonl. Run periodically (hourly/daily cron). Over time this enables:
  - flow TREND   (is net-accumulation accelerating vs the previous snapshot?)
  - VALIDATION   (does whale net-flow at time t predict the asset's return t -> t+h?)

Why forward-log: whale flow has NO historical archive (GeckoTerminal serves only recent
trades), so the only honest way to measure its predictive value is to record it live and
score it as price evolves. Mirrors scripts/26 (regime paper-trade).

Run: python scripts/30_whale_flow_log.py [TICKER]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm import whale_manager as wm  # noqa: E402

LOG = ROOT / "data" / "whale_flow_log.jsonl"


def spot_price(ticker: str) -> float:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv")
    return round(float(df["close"].iloc[-1]), 6)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MNT").upper()

    flow = wm.whale_flow_summary(wm.load())
    entry = {
        "logged_utc": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "price": spot_price(ticker),
        "net_bias": flow.get("net_bias"),
        "net_score": flow.get("net_score"),
        "net_flow_usd": flow.get("net_flow_usd"),
        "pct_accumulating": flow.get("pct_accumulating"),
        "conviction": flow.get("conviction"),
    }
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    rows = [json.loads(ln) for ln in LOG.open(encoding="utf-8") if ln.strip()]
    same = [r for r in rows if r.get("ticker") == ticker]
    print(f"logged: {ticker} | net_bias={entry['net_bias']} net_score={entry['net_score']:+} "
          f"flow=${entry['net_flow_usd']:,.0f} | price=${entry['price']}")
    print(f"whale_flow_log.jsonl: {len(rows)} total snapshot(s), {len(same)} for {ticker}")
    if len(same) >= 2:
        d = float(same[-1]["net_score"]) - float(same[-2]["net_score"])
        print(f"flow TREND vs prev: net_score {float(same[-2]['net_score']):+.3f} -> "
              f"{float(same[-1]['net_score']):+.3f} ({d:+.3f})  | price ${same[-2]['price']} -> ${same[-1]['price']}")
    else:
        print("(need >=2 snapshots over time for flow-trend; ~20+ spaced snapshots for validation IC)")


if __name__ == "__main__":
    main()
