"""Scheduled macro event-risk calendar (FOMC + US CPI).

Research-backed (deep-research 2026-06, peer-reviewed Yang & Wang FRL 2026 + Nazaruk KSE 2025,
3-0 adversarially verified): around FOMC/CPI releases BTC intraday volatility ~doubles (0.66%->1.25%)
and volume ~2.5x, but DIRECTION (cumulative abnormal return) is statistically ZERO. So the only
survivable systematic move is DE-RISK / ABSTAIN in the window — never a directional bet. The calendar
is public + scheduled, so this needs no paid API.

Update FOMC_2026 / CPI_2026 from the Fed + BLS schedules as the year advances.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# FOMC rate-decision days (decision ~14:00 ET = 18:00 UTC). Public Fed schedule.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16"]
# US CPI release days (~08:30 ET = 12:30 UTC). Public BLS schedule (approximate — verify monthly).
CPI_2026 = ["2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
            "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-13", "2026-11-12", "2026-12-10"]


def event_window(now: datetime | None = None) -> tuple[str | None, str]:
    """Return (event, reason) if `now` is inside a macro de-risk window, else (None, "").
    FOMC: t-1h .. t+3h around 18:00 UTC. CPI: t-30m .. t+3h around 12:30 UTC."""
    now = now or datetime.now(timezone.utc)
    for d in FOMC_2026:
        ev = datetime.fromisoformat(d).replace(tzinfo=timezone.utc, hour=18, minute=0)
        if ev - timedelta(hours=1) <= now <= ev + timedelta(hours=3):
            return "FOMC", f"FOMC decision window ({d} 14:00 ET) — vol ~2x, direction is noise"
    for d in CPI_2026:
        ev = datetime.fromisoformat(d).replace(tzinfo=timezone.utc, hour=12, minute=30)
        if ev - timedelta(minutes=30) <= now <= ev + timedelta(hours=3):
            return "CPI", f"CPI release window ({d} 08:30 ET) — vol ~2x, direction is noise"
    # LIVE layer (Fincept free macro calendar): AUGMENTS the hardcoded FOMC/CPI base with other
    # high-impact US prints (NFP, PCE, GDP, rate decisions) so the de-risk gate self-updates instead
    # of needing manual date entry. Fails soft -> the hardcoded base above always stands on its own.
    return live_event_window(now)


# ── LIVE high-impact US macro calendar (Fincept free tier) ──────────────────────────────────────
# The upcoming-events feed has NO usable impact field (importance is always 0), so we whitelist the
# true market-movers by event name. Times in the feed are UTC, 12-hour ("01:15 PM"). Cached 3h.
_LIVE_KEYWORDS = (
    "fed interest rate", "fomc", "federal funds", "interest rate decision",
    "inflation rate", "cpi", "pce price", "core pce",
    "non-farm", "non farm", "nonfarm", "unemployment rate", "gdp growth",
)
_LIVE_TTL = 3 * 3600
_LIVE_CACHE: dict = {}   # "ev" -> (epoch, [(dt_utc, name)])


def _live_us_events() -> list[tuple[datetime, str]]:
    """Upcoming high-impact US macro prints from Fincept, whitelisted by name. Fails soft to []."""
    import time as _t
    hit = _LIVE_CACHE.get("ev")
    if hit and _t.time() - hit[0] < _LIVE_TTL:
        return hit[1]
    out: list[tuple[datetime, str]] = []
    try:
        import os
        import requests
        key = os.environ.get("FINCEPT_API_KEY")
        if not key:
            return []
        # limit=500 widens the horizon to ~8 days (the default 100 is a dense ~2-day global window that
        # misses the next FOMC/CPI); we then whitelist to US market-movers below.
        evs = requests.get(
            "https://api.fincept.in/macro/upcoming-events?limit=500",
            headers={"X-API-Key": key, "User-Agent": "Mozilla/5.0"}, timeout=8,
        ).json().get("data", {}).get("events", [])
        for e in evs:
            if str(e.get("country", "")).upper() != "US":
                continue
            name = str(e.get("event", ""))
            if not any(k in name.lower() for k in _LIVE_KEYWORDS):
                continue
            try:
                dt = datetime.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %I:%M %p").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError, TypeError):
                continue
            out.append((dt, name))
    except Exception:   # noqa: BLE001 — best-effort; the hardcoded FOMC/CPI base still holds
        return []
    _LIVE_CACHE["ev"] = (_t.time(), out)
    return out


def live_event_window(now: datetime | None = None) -> tuple[str | None, str]:
    """(event, reason) if now is within a high-impact US-print de-risk window (t-1h .. t+2h), else (None, '')."""
    now = now or datetime.now(timezone.utc)
    for dt, name in _live_us_events():
        if dt - timedelta(hours=1) <= now <= dt + timedelta(hours=2):
            return "MACRO", f"{name} window — high-impact US print, vol up, direction noise"
    return None, ""
