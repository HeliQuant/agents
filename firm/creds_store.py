"""firm/creds_store.py — per-user credential store (local SQLite). NO CUSTODY.

This runs on the USER's OWN machine (agents-localReady). The onboarding form POSTs the user's keys to
THIS engine's /register endpoint over the user's own tunnel — the keys never touch the shared frontend
server. We persist them in a local SQLite (data/credentials.db) and load them into the process env so
the firm runs with them across restarts. Writes are gated by HQ_SETUP_TOKEN (see app.py /register).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "credentials.db"


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.execute("CREATE TABLE IF NOT EXISTS creds (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    return c


def save_creds(creds: dict) -> int:
    """Upsert non-empty credentials into the local SQLite. Returns the count written."""
    n = 0
    with _conn() as c:
        for k, v in (creds or {}).items():
            if not isinstance(k, str) or not k.strip():
                continue
            val = "" if v is None else str(v)
            if not val.strip():
                continue
            c.execute(
                "INSERT INTO creds(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k.strip(), val.strip()),
            )
            n += 1
    return n


def load_creds() -> dict:
    try:
        with _conn() as c:
            return {k: v for k, v in c.execute("SELECT k, v FROM creds").fetchall()}
    except Exception:  # noqa: BLE001
        return {}


def apply_to_env() -> int:
    """Load stored creds into os.environ WITHOUT overwriting an already-set env var. Returns count applied."""
    n = 0
    for k, v in load_creds().items():
        if v and not os.environ.get(k):
            os.environ[k] = v
            n += 1
    return n


def stored_keys() -> list[str]:
    """The credential KEY NAMES on file (never the values) — safe to surface for an onboarding status."""
    return sorted(load_creds().keys())
