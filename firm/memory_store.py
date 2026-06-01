"""firm/memory_store.py — the agent's MEMORY (Layer 3 learning-loop backend).

A small `MemoryStore` with a ZERO-DEPENDENCY SQLite backend (works today) behind a clean
interface, plus a documented Supabase/pgvector adapter for production semantic recall.

Every PM decision + its trade ticket is logged. When a ticket later resolves (TP/SL/timeout)
the outcome is recorded. Recent same-regime outcomes are recalled back into the PM's context so
the org gets "smarter with use" — the honest nevron-style PLAN->EXECUTE->LEARN->MEMORY loop:
recency + regime recall NOW; pgvector semantic recall is the Supabase upgrade (interface is the
same, so the loop code never changes).

Each decision + lesson is also mirrored to a human-browsable Markdown note in the Obsidian vault
(``agents/knowledge/``), so the agent's memory is fully auditable — honesty-by-design.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "heliquant_memory.db"
VAULT = ROOT / "knowledge"  # Obsidian-compatible markdown vault (agent notes + human research)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    regime      TEXT,
    decision    TEXT,                 -- ENTER | ABSTAIN
    direction   TEXT,                 -- LONG | SHORT | NONE
    confidence  TEXT,
    entry       REAL,
    stop_loss   REAL,
    tp1         REAL,
    reasoning   TEXT,
    ticket_json TEXT,
    status      TEXT DEFAULT 'open',  -- open | resolved | abstain
    outcome     TEXT,                 -- TP | SL | TIMEOUT
    pnl_pct     REAL,
    exit_price  REAL,
    resolved_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_dec_ticker_regime ON decisions(ticker, regime);
CREATE INDEX IF NOT EXISTS idx_dec_status ON decisions(status);
"""


class MemoryStore:
    """SQLite-backed agent memory. Swap for SupabaseMemoryStore in prod (same interface)."""

    def __init__(self, db_path: str | Path = DB_PATH, vault: str | Path | None = VAULT):
        self.db_path = str(db_path)
        self.vault = Path(vault) if vault else None
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        if self.vault:
            for sub in ("decisions", "lessons"):
                (self.vault / sub).mkdir(parents=True, exist_ok=True)

    # ── write ────────────────────────────────────────────────────────────────
    def record_decision(self, ts: str, ticker: str, regime: str, decision: dict) -> int:
        """Log one PM decision (+ ticket if any). ENTER w/ valid ticket -> status 'open'."""
        tk = decision.get("trade_ticket") or {}
        tps = tk.get("take_profit") or [{}]
        is_enter = decision.get("decision") == "ENTER" and bool(tk)
        row = {
            "ts": ts, "ticker": ticker.upper(), "regime": regime,
            "decision": decision.get("decision"), "direction": decision.get("direction"),
            "confidence": decision.get("confidence"),
            "entry": tk.get("entry"), "stop_loss": tk.get("stop_loss"), "tp1": tps[0].get("price"),
            "reasoning": str(decision.get("reasoning", ""))[:500],
            "ticket_json": json.dumps(tk) if tk else None,
            "status": "open" if is_enter else "abstain",
        }
        cur = self.conn.execute(
            "INSERT INTO decisions (ts,ticker,regime,decision,direction,confidence,entry,"
            "stop_loss,tp1,reasoning,ticket_json,status) VALUES "
            "(:ts,:ticker,:regime,:decision,:direction,:confidence,:entry,:stop_loss,:tp1,"
            ":reasoning,:ticket_json,:status)", row,
        )
        self.conn.commit()
        rid = int(cur.lastrowid)
        self._note_decision(rid, row, tk)
        return rid

    def record_outcome(self, rid: int, outcome: str, pnl_pct: float,
                       exit_price: float, resolved_ts: str) -> None:
        """Resolve an open trade (TP/SL/TIMEOUT) — the LEARN step."""
        self.conn.execute(
            "UPDATE decisions SET status='resolved', outcome=?, pnl_pct=?, exit_price=?, "
            "resolved_ts=? WHERE id=?", (outcome, pnl_pct, exit_price, resolved_ts, rid),
        )
        self.conn.commit()
        r = self.conn.execute("SELECT * FROM decisions WHERE id=?", (rid,)).fetchone()
        if r:
            self._note_lesson(dict(r))

    # ── read / recall ──────────────────────────────────────────────────────────
    def open_tickets(self, ticker: str | None = None) -> list[dict]:
        q = "SELECT * FROM decisions WHERE status='open'"
        args: tuple = ()
        if ticker:
            q += " AND ticker=?"
            args = (ticker.upper(),)
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def recall(self, ticker: str, regime: str | None = None, k: int = 5) -> list[dict]:
        """Most recent RESOLVED decisions for this ticker (regime-matched first). The recall
        the PM learns from. (Recency+regime now; pgvector semantic recall = Supabase upgrade.)"""
        rows = self.conn.execute(
            "SELECT ticker,regime,decision,direction,outcome,pnl_pct,ts FROM decisions "
            "WHERE ticker=? AND status='resolved' ORDER BY (regime=?) DESC, id DESC LIMIT ?",
            (ticker.upper(), regime or "", k),
        ).fetchall()
        return [dict(r) for r in rows]

    def brief(self, ticker: str, regime: str | None = None, k: int = 5) -> str:
        """A compact, FACTUAL memory brief for the PM context (no invented 'lessons')."""
        recalled = self.recall(ticker, regime, k)
        if not recalled:
            return f"AGENT MEMORY: no prior resolved {ticker.upper()} trades (cold start)."
        items, wins, losses = [], 0, 0
        for r in recalled:
            if r["decision"] == "ENTER" and r["outcome"]:
                tag = f"{r['regime']}/{r['direction']} {r['outcome']} {r['pnl_pct']:+.2f}%"
                if (r["pnl_pct"] or 0) > 0:
                    wins += 1
                else:
                    losses += 1
            else:
                tag = f"{r['regime']}/ABSTAIN"
            items.append(tag)
        return (f"AGENT MEMORY — last {len(recalled)} resolved {ticker.upper()} decisions: "
                f"[{', '.join(items)}]. Resolved ENTERs: {wins}W/{losses}L. "
                f"(Recency+regime recall; weigh this history in your decision.)")

    def stats(self) -> dict:
        c = self.conn.execute(
            "SELECT COUNT(*) n, SUM(status='open') o, SUM(status='resolved') r, "
            "SUM(status='abstain') a, SUM(status='resolved' AND pnl_pct>0) w FROM decisions"
        ).fetchone()
        n, o, r, a, w = (c["n"] or 0, c["o"] or 0, c["r"] or 0, c["a"] or 0, c["w"] or 0)
        return {"total": n, "open": o, "resolved": r, "abstain": a,
                "resolved_winrate": round(w / r * 100, 1) if r else None}

    def close(self) -> None:
        self.conn.close()

    # ── Obsidian vault mirror (human-auditable markdown) ────────────────────────
    def _write_note(self, sub: str, name: str, text: str) -> None:
        if not self.vault:
            return
        try:
            (self.vault / sub / f"{name}.md").write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _note_decision(self, rid: int, row: dict, tk: dict) -> None:
        body = (f"---\nid: {rid}\nticker: {row['ticker']}\nts: {row['ts']}\n"
                f"decision: {row['decision']}\nstatus: {row['status']}\n---\n\n"
                f"# {row['ticker']} · {row['decision']} {row['direction'] or ''} ({row['ts']})\n\n"
                f"- regime: {row['regime']} · confidence: {row['confidence']}\n"
                f"- reasoning: {row['reasoning']}\n")
        if tk:
            tp = tk.get("take_profit") or []
            body += (f"\n## Trade ticket\n- entry_range: {tk.get('entry_range')}\n"
                     f"- stop: {tk.get('stop_loss')} ({tk.get('stop_basis')}) · invalidation: {tk.get('invalidation')}\n"
                     f"- TP: " + " · ".join(f"{t.get('label')} {t.get('price')} (R:R {t.get('rr')})" for t in tp) +
                     f"\n- size: risk {tk.get('risk_pct')}% -> ${tk.get('notional_usd')} ({tk.get('position_size_pct')}% eq)\n")
        body += f"\nLinked: [[{row['ticker']}-history]] · [[trade-ticket-method]]\n"
        self._write_note("decisions", f"{row['ts'].replace(':', '').replace(' ', '_')}_{row['ticker']}_{rid}", body)

    def _note_lesson(self, r: dict) -> None:
        verdict = "WIN" if (r.get("pnl_pct") or 0) > 0 else "LOSS"
        body = (f"---\nid: {r['id']}\nticker: {r['ticker']}\noutcome: {r['outcome']}\n"
                f"pnl_pct: {r['pnl_pct']}\n---\n\n"
                f"# Lesson · {r['ticker']} {r['direction']} -> {r['outcome']} ({verdict})\n\n"
                f"- regime at entry: {r['regime']} · confidence: {r['confidence']}\n"
                f"- entry {r['entry']} -> exit {r['exit_price']} = {r['pnl_pct']:+.2f}%\n"
                f"- decided {r['ts']}, resolved {r['resolved_ts']}\n\n"
                f"Linked: [[{r['ticker']}-history]] · [[trade-ticket-method]]\n")
        self._write_note("lessons", f"{r['id']}_{r['ticker']}_{r['outcome']}", body)


# ── Supabase / pgvector adapter (production) — documented stub ──────────────────
class SupabaseMemoryStore:
    """PROD backend (same interface as MemoryStore). Not wired until creds are provided.

    Setup (when ready):
      1. Supabase project -> SQL editor -> `create extension if not exists vector;`
      2. table `decisions` mirrors the SQLite schema above + a `regime_embedding vector(1536)` column.
      3. env: SUPABASE_URL, SUPABASE_KEY ; pip install supabase
      4. `recall()` becomes a pgvector cosine search over regime/desk-context embeddings (semantic),
         replacing the SQLite recency+regime match — the loop code is unchanged.
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "SupabaseMemoryStore is a documented stub — provide SUPABASE_URL/KEY and run the "
            "schema, then implement record_decision/record_outcome/recall against the table. "
            "Until then use MemoryStore (SQLite)."
        )


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    # smoke test on an in-memory DB + temp vault (no pollution of the real store)
    tmp_vault = ROOT / "data" / "_memtest_vault"
    m = MemoryStore(db_path=":memory:", vault=tmp_vault)
    sample_ticket = {
        "entry": 73761.6, "entry_range": [73717.06, 73806.08], "stop_loss": 73612.29,
        "stop_basis": "structural", "invalidation": 73639.0,
        "take_profit": [{"label": "TP1", "price": 74161.83, "rr": 2.68},
                        {"label": "TP2", "price": 74236.47, "rr": 3.18}],
        "risk_pct": 1.5, "notional_usd": 950.0, "position_size_pct": 95.0,
    }
    id1 = m.record_decision("2026-06-02 10:00", "BTC", "Trending_Up",
                            {"decision": "ENTER", "direction": "LONG", "confidence": "high",
                             "reasoning": "smoke test", "trade_ticket": sample_ticket})
    id2 = m.record_decision("2026-06-02 11:00", "MNT", "Ranging",
                            {"decision": "ABSTAIN", "direction": "NONE", "confidence": "low",
                             "reasoning": "ranging, no edge", "trade_ticket": None})
    print(f"recorded ids: {id1}, {id2}")
    m.record_outcome(id1, "TP", 1.07, 74161.83, "2026-06-02 14:00")
    print("stats:", m.stats())
    print("recall BTC:", m.recall("BTC", "Trending_Up"))
    print("brief BTC:", m.brief("BTC", "Trending_Up"))
    print("brief MNT:", m.brief("MNT", "Ranging"))
    print(f"vault notes written under: {tmp_vault}")
    m.close()
