"""Sync the EDGE registry to Supabase edges_hq so the firm's edge state — incl. CANDIDATE / probation
edges like HYPE — is visible in the DB and to the frontend (RLS public-read).

Two distinct self-learning subsystems, two homes:
  * DESK reliability  -> desk_outcomes_hq (which of the 7 desks to trust)
  * EDGE registry     -> edges_hq (which edges are validated vs candidate/probation)  <- THIS

Local validated_edges.json + candidate_edges.json stay the source of truth; this mirrors them to the
cloud (upsert by asset). No-op if no SUPABASE creds. Run: python scripts/67_sync_registry.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.desk_performance import _supabase  # reuse the same client/creds path  # noqa: E402

DATA = ROOT / "data"
TABLE = "edges_hq"


def _rows(path: Path, tier: str) -> list[dict]:
    d = json.loads(path.read_text()) if path.exists() else {}
    return [{
        "asset": asset, "edge": e.get("edge"), "tier": tier, "validated": bool(e.get("validated")),
        "p_win": e.get("p_win"), "payoff_b": e.get("payoff_b"), "sample_n": e.get("sample_n"),
        "oos_roi_pct": e.get("oos_roi_pct"), "confirmations": int(e.get("confirmations", 0) or 0),
        "last_confirm_bar": e.get("last_confirm_bar"), "note": e.get("note"),
    } for asset, e in d.items()]


def sync() -> int:
    """Upsert the local edge registry into Supabase edges_hq. Returns #rows synced (0 if no creds)."""
    rows = _rows(DATA / "validated_edges.json", "validated") + _rows(DATA / "candidate_edges.json", "candidate")
    sb = _supabase()
    if not sb or not rows:
        return 0
    sb.table(TABLE).upsert(rows, on_conflict="asset").execute()
    return len(rows)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    rows = _rows(DATA / "validated_edges.json", "validated") + _rows(DATA / "candidate_edges.json", "candidate")
    print("EDGE registry: " + ", ".join(
        f"{r['asset']}({r['tier']}{' ·conf'+str(r['confirmations']) if r['confirmations'] else ''})" for r in rows))
    if not _supabase():
        print("no SUPABASE creds → local JSON is source of truth; nothing synced.")
        return
    n = sync()
    print(f"✅ synced {n} edge(s) → Supabase {TABLE} (FE-readable). HYPE's candidate/probation state now in the DB.")


if __name__ == "__main__":
    main()
