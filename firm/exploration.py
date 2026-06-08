"""firm/exploration.py — disciplined EXPLORATION mode ("coba-coba"): hunt for an edge when none is validated.

Default = ABSTAIN when no edge. Safe, but never DISCOVERS edges. This adds a controlled PAPER loop: when
the PM abstains, the firm logs a hypothesis trial (BUDGET=4) driven by its FLOW desks (Smart-Money, On-chain,
flow-intel, and the live WHALE desk) — the reads you trust when you can't predict price. Each trial is PAPER
(records entry price + direction; resolved by the 24h price move — zero real money, exploration without an
edge is negative-EV so it must cost nothing). After 4 trials resolve, the learning gate fires:
  * net loss  -> the losing CONDITION signatures are logged as FAILED (never repeated) + a COOLDOWN — that's
                 the evidence-based answer to "why was I wrong"; it earns 4 fresh tries after the cooldown.
  * net win   -> the condition class becomes a CANDIDATE edge (still must pass the full gate before real capital).
Hypothesis-driven, validation-gated, paper-only — an edge-DISCOVERY loop, never a licence to gamble.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "exploration_state.json"
BUDGET = 4               # paper trials per round
HORIZON_H = 24           # judge each trial on the 24h move (same as the edges)
COOLDOWN_H = 48          # after a net-loss round, wait this long before exploring again
DESKS = ("Smart-Money Flow", "On-chain/Risk", "flow-intel", "whale")


_DEFAULT = {"mode": "idle", "trials": [], "failed_conditions": [], "rounds": 0, "cooldown_until": 0}


def _load() -> dict:
    try:
        from firm import state_store
        return state_store.load("exploration", dict(_DEFAULT)) or dict(_DEFAULT)
    except Exception:  # noqa: BLE001
        return dict(_DEFAULT)


def _save(s: dict) -> None:
    try:
        from firm import state_store
        state_store.save("exploration", s)
    except Exception:  # noqa: BLE001
        pass


def _cond_key(reads: dict) -> str:
    """Signature of the flow reads — so a losing condition class is recognised and not repeated."""
    out = []
    for d in DESKS:
        v = str(reads.get(d, "na")).lower()
        s = "bull" if any(k in v for k in ("bull", "long", "accum")) else \
            "bear" if any(k in v for k in ("bear", "short", "distrib")) else "neu"
        out.append(f"{d[:4]}:{s}")
    return "|".join(out)


def status() -> dict:
    s = _load()
    open_t = [t for t in s["trials"] if t.get("outcome") is None]
    cd = max(0, int((s.get("cooldown_until", 0) - time.time()) / 3600))
    return {"mode": s["mode"], "round": s["rounds"], "open_trials": len(open_t), "budget": BUDGET,
            "cooldown_h_left": cd, "failed_conditions": s.get("failed_conditions", [])}


def propose_trial(asset: str, flow_reads: dict) -> dict | None:
    """Propose a hypothesis trial from the flow-desk consensus, or None. Skips during cooldown, when the
    open-trial budget is full, when there's no consensus, or when this condition class already failed."""
    s = _load()
    if time.time() < s.get("cooldown_until", 0):
        return None
    if len([t for t in s["trials"] if t.get("outcome") is None]) >= BUDGET:
        return None
    cond = _cond_key(flow_reads)
    if cond in s.get("failed_conditions", []):
        return None  # learned: this flow-consensus has no edge -> don't repeat ("why I was wrong")
    bull = sum(1 for d in DESKS if any(k in str(flow_reads.get(d, "")).lower() for k in ("bull", "long", "accum")))
    bear = sum(1 for d in DESKS if any(k in str(flow_reads.get(d, "")).lower() for k in ("bear", "short", "distrib")))
    if abs(bull - bear) < 2:
        return None  # require a STRONG consensus (net >=2 of the flow desks agree) — fewer, higher-conviction
    direction = "LONG" if bull > bear else "SHORT"
    return {"asset": asset.upper(), "direction": direction, "condition": cond,
            "hypothesis": f"flow desks lean {direction.lower()} ({bull}-{bear} of {len(DESKS)})"}


def record_trial(asset: str, direction: str, condition: str, entry: float, hypothesis: str) -> None:
    s = _load()
    s["mode"] = "exploring"
    s["trials"].append({"asset": asset.upper(), "direction": direction, "condition": condition,
                        "entry": float(entry), "epoch": time.time(), "hypothesis": hypothesis, "outcome": None})
    _save(s)


def resolve_and_learn(price_by_asset: dict) -> dict:
    """Resolve trials whose 24h horizon elapsed (by price move); once BUDGET trials resolve, run the gate."""
    s = _load()
    now = time.time()
    for t in s["trials"]:
        if t.get("outcome") is not None:
            continue
        if now - t.get("epoch", now) < HORIZON_H * 3600:
            continue  # not matured yet
        cur = price_by_asset.get(t["asset"])
        if cur is None:
            continue
        move = (cur / t["entry"] - 1) * (1 if t["direction"] == "LONG" else -1)
        t["outcome"] = round(move * 100, 2)
    resolved = [t for t in s["trials"] if t.get("outcome") is not None]
    verdict = {"learned": False}
    if len(resolved) >= BUDGET:
        net = sum(t["outcome"] for t in resolved)
        wins = sum(1 for t in resolved if t["outcome"] > 0)
        if net <= 0:
            for t in resolved:
                if t["outcome"] <= 0 and t["condition"] not in s["failed_conditions"]:
                    s["failed_conditions"].append(t["condition"])
            s["cooldown_until"] = now + COOLDOWN_H * 3600
            s["mode"] = "learning"
            verdict = {"learned": True, "result": "net-loss", "net_pct": round(net, 2), "wins": wins,
                       "lesson": "flow-consensus had no edge -> conditions logged FAILED + cooldown"}
        else:
            s["mode"] = "candidate-found"
            verdict = {"learned": True, "result": "net-win", "net_pct": round(net, 2), "wins": wins,
                       "lesson": "flow-consensus paid -> CANDIDATE (forward-confirm before real capital)"}
        s["rounds"] += 1
        s["trials"] = []
    _save(s)
    return verdict


if __name__ == "__main__":
    print("exploration status:", json.dumps(status(), indent=2))
