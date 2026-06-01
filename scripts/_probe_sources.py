"""Probe which real intelligence sources are LIVE right now (before wiring into the org)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

# 1) Allora (real decentralised-AI macro prediction)
try:
    from firm.allora_client import fetch_btc_macro_signal
    sig = asyncio.run(fetch_btc_macro_signal())
    print(f"Allora       : OK  BTC spot ${sig.spot_usd:,.0f} -> pred ${sig.predicted_usd:,.0f} "
          f"({sig.predicted_return*100:+.2f}%) bull={sig.direction_bull} bear={sig.direction_bear}")
except Exception as e:  # noqa: BLE001
    print(f"Allora       : FAIL  {type(e).__name__}: {str(e)[:140]}")

# 2) GoPlus rugpull (real token-security) — test on WMNT (Mantle ERC-20)
try:
    from firm.sources.rugpull import RugpullScreener
    ra = RugpullScreener().assess("0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8")  # WMNT
    print(f"GoPlus       : {'MOCK' if ra.is_mocked else 'OK'}  severity={ra.severity} "
          f"risk={ra.risk_score:.2f} honeypot={ra.is_honeypot} :: {ra.reasoning[:80]}")
except Exception as e:  # noqa: BLE001
    print(f"GoPlus       : FAIL  {type(e).__name__}: {str(e)[:140]}")

# 3) Sentiment (CoinGecko price-based proxy)
try:
    from firm.sources.sentiment import SentimentSource
    s = SentimentSource("mantle").fetch()
    print(f"Sentiment    : OK  {s.direction.upper()} score={s.score:+.3f} conf={s.confidence:.2f} "
          f"(24h {s.price_change_24h_pct}, 7d {s.price_change_7d_pct})")
except Exception as e:  # noqa: BLE001
    print(f"Sentiment    : FAIL  {type(e).__name__}: {str(e)[:140]}")

# 4) Research API sample — Fear & Greed index (alternative.me, no key)
try:
    import requests
    j = requests.get("https://api.alternative.me/fng/", timeout=10).json()
    d = j["data"][0]
    print(f"FearGreed    : OK  {d['value']}/100 ({d['value_classification']})")
except Exception as e:  # noqa: BLE001
    print(f"FearGreed    : FAIL  {type(e).__name__}: {str(e)[:140]}")
