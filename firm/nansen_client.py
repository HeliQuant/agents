"""Nansen smart-money client — CREDIT-FRUGAL (50 credits/call, ~1000 total → cache hard).

Used by the org's Smart-Money desk for MACRO smart-money context: what funds/smart-traders are
net-accumulating vs distributing on Ethereum (the macro reference; MNT empirically tracks BTC/ETH
~0.64). Mantle-direct smart-money is thin (verified: ~1-2 traders), so Nansen's value here = the
rich majors signal as macro context. Cached (10-min TTL) to conserve the limited credit pool.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.nansen.ai"
_TTL = 600  # 10-min cache — credits are scarce (50/call)
_CACHE: dict[str, tuple[float, dict]] = {}


def _key() -> str | None:
    k = os.environ.get("NANSEN_API_KEY")
    if k:
        return k
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in (ROOT / ".env").read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith("NANSEN_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError, FileNotFoundError):
            continue
    return None


def smart_money_netflow(chains: list[str], limit: int = 6) -> dict:
    """Top tokens by smart-money 24h net flow on `chains`. CACHED to save credits.
    Returns {available, credits_remaining, top:[{sym,net24h_usd,net7d_usd,traders}], read}."""
    key = _key()
    if not key:
        return {"available": False, "note": "no NANSEN_API_KEY"}
    ck = ",".join(sorted(chains))
    now = time.monotonic()
    if ck in _CACHE and now - _CACHE[ck][0] < _TTL:
        return _CACHE[ck][1]
    body = {"chains": chains, "pagination": {"page": 1, "per_page": limit},
            "order_by": [{"field": "net_flow_24h_usd", "direction": "DESC"}],
            "filters": {"include_native_tokens": True, "include_stablecoins": False}}
    try:
        r = requests.post(f"{BASE}/api/v1/smart-money/netflow",
                          headers={"apikey": key, "Content-Type": "application/json"}, json=body, timeout=30)
        cred = r.headers.get("X-Nansen-Credits-Remaining") or r.headers.get("xnansencreditsremaining")
        if not r.ok:
            out = {"available": False, "note": f"HTTP {r.status_code}: {r.text[:80]}"}
        else:
            data = r.json().get("data", [])
            top = [{"sym": d.get("token_symbol"), "net24h_usd": round(d.get("net_flow_24h_usd", 0)),
                    "net7d_usd": round(d.get("net_flow_7d_usd", 0)), "traders": d.get("trader_count")}
                   for d in data[:limit]]
            net_total = sum(t["net24h_usd"] for t in top)
            read = ("smart money net-ACCUMULATING (risk-on)" if net_total > 0
                    else "smart money net-DISTRIBUTING (risk-off)" if net_total < 0 else "neutral")
            out = {"available": True, "credits_remaining": cred, "chains": chains, "top": top,
                   "net24h_total_usd": net_total, "read": read,
                   "source": "Nansen smart-money netflow (funds + smart traders, DEX + CEX)"}
    except Exception as e:  # noqa: BLE001
        out = {"available": False, "note": f"error {str(e)[:50]}"}
    _CACHE[ck] = (now, out)
    return out
