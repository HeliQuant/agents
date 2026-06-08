"""firm/exploration.py — disciplined EXPLORATION mode ("coba-coba"): hunt for an edge when none is validated.

HeliQuant's default is ABSTAIN when no edge is validated. That's safe but never DISCOVERS new edges. This
adds a controlled exploration loop: when the PM abstains, instead of doing nothing, the firm may run a small
budget of TESTNET trials (BUDGET=4, zero real money) driven by its FLOW-following desks (Smart-Money,
On-chain, flow-intel, Carry) — the desks you trust when you can't predict price. Each trial tests a specific
HYPOTHESIS (a directional read tied to a condition signature), never a random punt.

After the budget is spent, the learning gate evaluates the trials:
  * a condition that paid off + validates -> graduates toward a CANDIDATE edge (probation, never instant-live);
  * a net loss -> the condition is logged as FAILED (don't repeat it) and exploration enters COOLDOWN until
    new data / a new regime appears. "Why was I wrong?" is answered by evidence, then it earns 4 fresh tries.

Honest guardrails: TESTNET only (exploration without an edge is negative-EV — never risk real capital on a
guess); hypothesis-driven (not random); validation-gated; and it never overrides the real-capital discipline
— it's a cheap edge-DISCOVERY loop, not a licence to gamble.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "exploration_state.json"
BUDGET = 4                  # testnet trials per exploration round
HORIZON_H = 24             # each trial is judged on the same 24h horizon as the edges
COOLDOWN_ROUNDS = 1        # rounds to wait after a net-loss round before exploring the same condition class


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"mode": "idle", "trials": [], "failed_conditions": [], "rounds": 0, "cooldown": 0}


def _save(s: dict) -> None:
    try:
        STATE.write_text(json.dumps(s, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _condition_key(reads: dict) -> str:
    """A signature of the flow-desk reads so we can recognise (and not repeat) a losing condition class."""
    parts = []
    for d in ("Smart-Money Flow", "On-chain/Risk", "flow-intel", "carry"):
        v = str(reads.get(d, "na")).lower()
        parts.append(f"{d[:4]}:{'bull' if 'bull' in v or 'long' in v else 'bear' if 'bear' in v or 'short' in v else 'neu'}")
    return "|".join(parts)


def status() -> dict:
    s = _load()
    open_trials = [t for t in s["trials"] if t.get("outcome") is None]
    return {"mode": s["mode"], "round": s["rounds"], "trials_used": len(s["trials"]),
            "budget": BUDGET, "open_trials": len(open_trials), "cooldown": s.get("cooldown", 0),
            "failed_conditions": s.get("failed_conditions", [])}


def propose_trial(asset: str, flow_reads: dict) -> dict | None:
    """Given the FLOW-desk reads, propose a hypothesis trial — or None if no fresh actionable read.
    Returns {asset, direction, condition, hypothesis} for the executor to run on TESTNET."""
    s = _load()
    if s.get("cooldown", 0) > 0:
        return None
    if len([t for t in s["trials"] if t.get("outcome") is None]) >= BUDGET:
        return None  # budget of open trials reached -> wait for them to resolve + learn
    cond = _condition_key(flow_reads)
    if cond in s.get("failed_conditions", []):
        return None  # already learned this condition has no edge -> don't repeat (the "why I was wrong")
    # directional lean = majority of the flow desks (the ones we trust when we can't predict price)
    bull = sum(1 for d in ("Smart-Money Flow", "On-chain/Risk", "flow-intel")
               if any(k in str(flow_reads.get(d, "")).lower() for k in ("bull", "long", "accumul")))
    bear = sum(1 for d in ("Smart-Money Flow", "On-chain/Risk", "flow-intel")
               if any(k in str(flow_reads.get(d, "")).lower() for k in ("bear", "short", "distrib")))
    if bull == bear:
        return None  # no flow consensus -> nothing worth even a testnet trial
    direction = "LONG" if bull > bear else "SHORT"
    return {"asset": asset.upper(), "direction": direction, "condition": cond,
            "hypothesis": f"flow desks lean {direction.lower()} (smart-money/on-chain/flow-intel {bull}-{bear})"}


def record_trial(asset: str, direction: str, condition: str, entry: float, hypothesis: str) -> None:
    """Log a TESTNET trial the executor just opened. Resolved later by resolve_and_learn."""
    s = _load()
    s["mode"] = "exploring"
    s["trials"].append({"asset": asset.upper(), "direction": direction, "condition": condition,
                        "entry": float(entry), "hypothesis": hypothesis, "outcome": None})
    _save(s)


def resolve_and_learn(price_by_asset: dict) -> dict:
    """Resolve trials whose horizon elapsed (caller passes current prices) and, once the round's budget is
    spent, run the learning gate: net-loss -> log failed condition(s) + cooldown; net-win -> flag candidate."""
    s = _load()
    for t in s["trials"]:
        if t.get("outcome") is not None:
            continue
        cur = price_by_asset.get(t["asset"])
        if cur is None:
            continue
        move = (cur / t["entry"] - 1) * (1 if t["direction"] == "LONG" else -1)
        t["outcome"] = round(move * 100, 2)  # % pnl of the directional trial (testnet)
    resolved = [t for t in s["trials"] if t.get("outcome") is not None]
    verdict = {"learned": False}
    if len(resolved) >= BUDGET:
        net = sum(t["outcome"] for t in resolved)
        wins = sum(1 for t in resolved if t["outcome"] > 0)
        if net <= 0:  # the round lost -> learn which conditions failed, don't repeat, cool down
            for t in resolved:
                if t["outcome"] <= 0 and t["condition"] not in s["failed_conditions"]:
                    s["failed_conditions"].append(t["condition"])
            s["cooldown"] = COOLDOWN_ROUNDS
            s["mode"] = "learning"
            verdict = {"learned": True, "result": "net-loss", "net_pct": round(net, 2), "wins": wins,
                       "lesson": "flow-lean had no edge in these conditions -> logged as failed, cooldown"}
        else:  # a winning round -> the condition class is a CANDIDATE (still must pass the full gate before live)
            s["mode"] = "candidate-found"
            verdict = {"learned": True, "result": "net-win", "net_pct": round(net, 2), "wins": wins,
                       "lesson": "flow-lean paid off -> promote to CANDIDATE edge (forward-confirm before real capital)"}
        s["rounds"] += 1
        s["trials"] = []  # reset budget for the next round
        if s.get("cooldown", 0) > 0:
            s["cooldown"] -= 0  # cooldown decremented by the loop each cycle (see scripts wiring)
    _save(s)
    return verdict


if __name__ == "__main__":
    print("exploration status:", json.dumps(status(), indent=2))
