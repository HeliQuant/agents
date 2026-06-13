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


def _get(path: str, params: dict, ck: str) -> dict | None:
    """Cached GET against the Elfa data API. Returns the parsed JSON `data`, or None on any failure."""
    key = _key()
    if not key:
        return None
    now = time.monotonic()
    if ck in _CACHE and now - _CACHE[ck][0] < _TTL:
        return _CACHE[ck][1]
    try:
        r = requests.get(f"{BASE}{path}", headers={"x-elfa-api-key": key}, params=params, timeout=20)
        out = r.json().get("data") if r.ok else None
    except Exception:  # noqa: BLE001
        out = None
    _CACHE[ck] = (now, out)
    return out


def asset_social(ticker: str) -> dict:
    """Per-asset smart-social read: the most significant mentions (engagement-weighted) + token news for
    THIS ticker — richer than 'is it trending'. In-scope on the data tier (chat/AI analysis is paywalled).
    """
    tk = ticker.upper().replace("WMNT", "MNT")
    mentions = _get("/v2/data/top-mentions", {"ticker": tk, "timeWindow": "24h", "pageSize": 5}, f"tm:{tk}") or []
    news = _get("/v2/data/token-news", {"ticker": tk, "pageSize": 4}, f"tn:{tk}") or []
    if not isinstance(mentions, list):
        mentions = (mentions or {}).get("data", []) if isinstance(mentions, dict) else []
    if not isinstance(news, list):
        news = (news or {}).get("data", []) if isinstance(news, dict) else []
    top = [{"text": (m.get("content") or m.get("text") or "")[:200],
            "likes": m.get("likeCount"), "views": m.get("viewCount"), "at": m.get("mentionedAt")}
           for m in mentions[:5] if isinstance(m, dict)]
    total_views = sum((m.get("views") or 0) for m in top)
    return {
        "asset": tk,
        "top_mentions": top,
        "mention_count": len(top),
        "total_views_24h": total_views,
        "token_news": [{"text": (n.get("content") or n.get("text") or "")[:160], "at": n.get("mentionedAt")}
                       for n in news[:4] if isinstance(n, dict)],
        "source": "Elfa data API — top-mentions + token-news (smart accounts; chat/AI analysis is paid-tier)",
    }
