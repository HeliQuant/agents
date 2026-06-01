"""Elfa smart-social client (v2) — narrative/mindshare intelligence for the org's Smart-Social desk.

Tracks where crypto ATTENTION is (mindshare/trending) + sentiment of *smart* accounts (not retail
noise). Cached (10-min TTL). Strong for majors/narratives; Mantle-eco social mindshare may be thin
(we check + report honestly).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.elfa.ai"
_TTL = 600
_CACHE: dict[str, tuple[float, dict]] = {}


def _key() -> str | None:
    k = os.environ.get("ELFA_API_KEY")
    if k:
        return k
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in (ROOT / ".env").read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith("ELFA_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError, FileNotFoundError):
            continue
    return None


def trending(time_window: str = "24h", limit: int = 12) -> dict:
    """Top trending tokens by social mindshare (mentions + change%). Cached."""
    key = _key()
    if not key:
        return {"available": False, "note": "no ELFA_API_KEY"}
    ck = f"trending:{time_window}"
    now = time.monotonic()
    if ck in _CACHE and now - _CACHE[ck][0] < _TTL:
        return _CACHE[ck][1]
    try:
        r = requests.get(f"{BASE}/v2/aggregations/trending-tokens",
                         headers={"x-elfa-api-key": key},
                         params={"timeWindow": time_window, "pageSize": limit}, timeout=20)
        if not r.ok:
            out = {"available": False, "note": f"HTTP {r.status_code}: {r.text[:60]}"}
        else:
            d = (r.json().get("data") or {}).get("data", [])
            out = {"available": True, "time_window": time_window,
                   "trending": [{"token": x.get("token"), "mentions": x.get("current_count"),
                                 "change_pct": x.get("change_percent")} for x in d[:limit]],
                   "source": "Elfa smart-social mindshare (Twitter/X + Telegram, smart accounts)"}
    except Exception as e:  # noqa: BLE001
        out = {"available": False, "note": f"error {str(e)[:50]}"}
    _CACHE[ck] = (now, out)
    return out
