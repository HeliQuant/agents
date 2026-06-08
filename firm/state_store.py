"""firm/state_store.py — persistent KV store (Supabase `hq_state`) with local-JSON fallback.

Railway's filesystem is EPHEMERAL — every redeploy wipes local files, so the carry cache / exploration
state / whale leaderboard had to be re-pushed each time. This stores them in Supabase (external, survives
redeploys): the local engine writes once, the cloud reads across redeploys. No Supabase creds -> falls back
to local JSON (so it works the same offline / in tests). Service_role key bypasses RLS for writes; the
table is public-read so the frontend can show the same state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "data" / "state"
TABLE = "hq_state"


def _env(key: str) -> str | None:
    v = os.environ.get(key)
    if v:
        return v
    for envfile in (ROOT / ".env", ROOT.parent / ".env"):  # agents/.env, then project-root .env
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                for line in envfile.read_text(encoding=enc).splitlines():
                    line = line.lstrip("﻿").strip()
                    if line.startswith(f"{key}="):
                        return line.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
            except (UnicodeError, ValueError, FileNotFoundError):
                continue
    return None


def _client():
    url, key = _env("SUPABASE_URL"), _env("SUPABASE_KEY")
    if not (url and key):
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception:  # noqa: BLE001
        return None


def load(key: str, default=None):
    """Read a value by key — Supabase first, then local fallback, then default."""
    sb = _client()
    if sb:
        try:
            r = sb.table(TABLE).select("value").eq("key", key).limit(1).execute()
            if r.data:
                return r.data[0]["value"]
        except Exception:  # noqa: BLE001
            pass
    p = LOCAL / f"{key.replace(':', '_')}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return default


def save(key: str, value) -> str:
    """Upsert a value by key — Supabase (persistent) if keyed, else local JSON. Returns the backend used."""
    sb = _client()
    if sb:
        try:
            sb.table(TABLE).upsert({"key": key, "value": value}).execute()
            return "supabase"
        except Exception:  # noqa: BLE001
            pass
    try:
        LOCAL.mkdir(parents=True, exist_ok=True)
        (LOCAL / f"{key.replace(':', '_')}.json").write_text(json.dumps(value))
    except Exception:  # noqa: BLE001
        pass
    return "local"


if __name__ == "__main__":
    import time
    print("backend:", "supabase" if _client() else "local-only")
    print("save:", save("selftest", {"ok": True, "ts": "probe"}))
    print("load:", load("selftest"))
