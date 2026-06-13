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
    return None, ""
