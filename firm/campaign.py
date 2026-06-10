"""firm/campaign.py — THE LIVE CAMPAIGN (cloud edition): 100 desk-driven positions, paper capital.

The user's brief: "make the firm brave — open positions — but stay true to its principles and desks."
The honest synthesis, running ON Railway:

  * REAL capital still requires a validated edge — the PM gate is untouched (registry empty -> no
    real-money ENTER). That discipline IS the product.
  * These positions are PAPER (zero capital at risk) at REAL live prices: entries/exits mark against
    DeFiLlama's price API (CoinGecko fallback) — both reachable from the cloud (Bybit is geo-blocked).
  * Every open is justified by the QUANTITATIVE desks (no LLM burn): flow-intel's learned/FDR-gated
    signals, z-score extremes on OI/funding/flow/momentum from the fed positioning data, and the live
    Hyperliquid whale read. The vote breakdown is stored ON the position — auditable bravery.
  * Conviction tiers: |net votes| >= 2 -> STRONG ($100 virtual) · net 1 -> LEAN ($50). Zero votes ->
    no position; the firm does not coin-flip, even on paper.
  * 4h horizon = a DECLARED intraday-flow hypothesis class (the edge lab's validation standard stays
    24h). Closes are net of 20bps round-trip cost. A losing round (4 closes on an asset, net <= 0)
    logs its condition signatures as FAILED (never repeated) + a 12h cooldown — the learning loop.
  * Stale-data gate: votes need positioning data <= 12h old (frozen-entry lesson, 2026-06-09). Price
    marks are always live, so entries are never fabricated.

State persists in Supabase ("campaign" + "campaign_pos") -> survives redeploys, FE-readable.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]

BASKET = ["MNT", "BTC", "ETH", "SOL", "HYPE", "SUI"]
TARGET = 100
SLOTS_PER_ASSET = 4
HORIZON_H = 4
COST_RT = 0.0020
COOLDOWN_H = 12
MAX_DATA_AGE_H = 12
VIRTUAL_FULL = 100.0
VIRTUAL_LEAN = 50.0

_GECKO_IDS = {"MNT": "mantle", "BTC": "bitcoin", "ETH": "ethereum",
              "SOL": "solana", "HYPE": "hyperliquid", "SUI": "sui"}

_DEF = {"opened": 0, "closed": 0, "wins": 0, "net_usd": 0.0,
        "failed_conditions": [], "cooldown_until": {}, "rounds": {}, "done": False}


def _now() -> float:
    return time.time()


def _load() -> tuple[dict, list]:
    from firm import state_store
    s = state_store.load("campaign_state", None) or dict(_DEF)
    pos = state_store.load("campaign_pos", None) or []
    return s, pos


def _save(s: dict, pos: list) -> None:
    from firm import state_store
    state_store.save("campaign_state", s)
    state_store.save("campaign_pos", pos)


def live_prices() -> dict:
    """One batched call for the whole basket. DeFiLlama first (generous, keyless), CoinGecko fallback."""
    ids = ",".join(f"coingecko:{g}" for g in _GECKO_IDS.values())
    try:
        r = requests.get(f"https://coins.llama.fi/prices/current/{ids}", timeout=15).json()
        coins = r.get("coins", {})
        out = {a: coins.get(f"coingecko:{g}", {}).get("price") for a, g in _GECKO_IDS.items()}
        if any(v for v in out.values()):
            return out
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ",".join(_GECKO_IDS.values()), "vs_currencies": "usd"},
                         timeout=15).json()
        return {a: (r.get(g) or {}).get("usd") for a, g in _GECKO_IDS.items()}
    except Exception:  # noqa: BLE001
        return {}


def _data_age_h(asset: str) -> float:
    fp = ROOT / "data" / f"{asset.lower()}_positioning.csv"
    if not fp.exists():
        return 9e9
    try:
        df = pd.read_csv(fp)
        ts = pd.to_datetime(float(df["timestamp"].iloc[-1]), unit="ms", utc=True)
        return (pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return 9e9


def desk_votes(asset: str) -> tuple[int, list[str]]:
    """Quantitative desks vote a direction (positive=LONG). All evidence logged on the position."""
    votes: list[tuple[int, str]] = []
    age = _data_age_h(asset)
    if age <= MAX_DATA_AGE_H:
        try:
            from firm.flow_intel import synthesize as flow_synth
            fi = flow_synth(asset)
        except Exception:  # noqa: BLE001
            fi = {"stance": "NEUTRAL", "reads": {}, "learned": {}}
        if fi.get("stance") == "LONG":
            votes.append((1, "flow-intel:LONG"))
        elif fi.get("stance") == "SHORT":
            votes.append((-1, "flow-intel:SHORT"))
        # standing prior: fade positioning extremes, ride momentum — unless the lab learned otherwise
        prior = {"oi_chg24": -1, "funding": -1, "flow_imbalance": -1, "price_mom24": 1}
        for sig, read in (fi.get("reads") or {}).items():
            if not read.get("extreme"):
                continue
            z = read["z"]
            learned = (fi.get("learned") or {}).get(sig, {})
            if learned.get("validates"):
                base = -1 if learned.get("direction") == "contrarian" else 1
            else:
                base = prior.get(sig, -1)
            d = base * (1 if z > 0 else -1)
            votes.append((d, f"{sig}:z{z:+.1f}->{'L' if d > 0 else 'S'}"))
    try:
        from firm.hl_whales import whale_read
        wr = whale_read(asset)
        if wr.get("stance") == "LONG":
            votes.append((1, "whale:LONG"))
        elif wr.get("stance") == "SHORT":
            votes.append((-1, "whale:SHORT"))
    except Exception:  # noqa: BLE001
        pass
    return sum(v for v, _ in votes), [r for _, r in votes]


def _cond_key(asset: str, reasons: list[str]) -> str:
    return f"{asset}|" + "|".join(sorted(r.split(":")[0] + (":L" if ("->L" in r or ":LONG" in r) else ":S")
                                          for r in reasons))


def step(log=print) -> dict:
    """One campaign step: resolve matured positions, open new desk-justified ones. Called by the app
    loop every CAMPAIGN_STEP_MIN. Cheap (no LLM): a few HTTP calls + CSV reads."""
    s, pos = _load()
    if s.get("done"):
        return s
    prices = live_prices()

    # ── resolve matured ──
    for p in pos:
        if p.get("exit") is not None or _now() - p["t_open"] < HORIZON_H * 3600:
            continue
        px = prices.get(p["asset"])
        if not px:
            continue
        sign = 1 if p["dir"] == "LONG" else -1
        net_pct = sign * (px / p["entry"] - 1) - COST_RT
        pnl = round(net_pct * p["size_usd"], 4)
        p.update(exit=px, net_pct=round(net_pct * 100, 3), pnl_usd=pnl,
                 utc_close=datetime.now(timezone.utc).isoformat())
        s["closed"] += 1
        s["net_usd"] = round(s["net_usd"] + pnl, 4)
        if pnl > 0:
            s["wins"] += 1
        log(f"  {'🟩' if pnl > 0 else '🟥'} CAMPAIGN CLOSE #{p['id']} {p['dir']} {p['asset']} "
            f"{p['net_pct']:+.2f}% (${pnl:+.2f}) | total ${s['net_usd']:+.2f}")
        r = s["rounds"].setdefault(p["asset"], {"closed": 0, "pnl": 0.0, "conds": []})
        r["closed"] += 1
        r["pnl"] = round(r["pnl"] + pnl, 4)
        r["conds"].append(p["cond"])
        if r["closed"] >= SLOTS_PER_ASSET:
            if r["pnl"] <= 0:
                for c in set(r["conds"]):
                    if c not in s["failed_conditions"]:
                        s["failed_conditions"].append(c)
                s["cooldown_until"][p["asset"]] = _now() + COOLDOWN_H * 3600
                log(f"  📚 CAMPAIGN LEARN {p['asset']}: round ${r['pnl']:+.2f} -> conditions FAILED + cooldown")
            s["rounds"][p["asset"]] = {"closed": 0, "pnl": 0.0, "conds": []}

    # ── open new (desk-justified only) ──
    for a in BASKET:
        if s["opened"] >= TARGET:
            break
        if _now() < s["cooldown_until"].get(a, 0):
            continue
        if len([p for p in pos if p["asset"] == a and p.get("exit") is None]) >= SLOTS_PER_ASSET:
            continue
        net, reasons = desk_votes(a)
        if net == 0 or not reasons:
            continue  # desks flat -> no coin-flips, even on paper
        cond = _cond_key(a, reasons)
        if cond in s["failed_conditions"]:
            continue
        px = prices.get(a)
        if not px:
            continue
        direction = "LONG" if net > 0 else "SHORT"
        tier = "STRONG" if abs(net) >= 2 else "LEAN"
        p = {"id": s["opened"] + 1, "asset": a, "dir": direction, "tier": tier,
             "size_usd": VIRTUAL_FULL if tier == "STRONG" else VIRTUAL_LEAN,
             "entry": px, "t_open": _now(), "utc_open": datetime.now(timezone.utc).isoformat(),
             "votes": net, "reasons": reasons[:6], "cond": cond, "exit": None}
        pos.append(p)
        s["opened"] += 1
        log(f"  🟢 CAMPAIGN OPEN #{p['id']} {direction} {a} @ {px} {tier} (net {net:+}) — {'; '.join(reasons[:3])}")

    open_now = len([p for p in pos if p.get("exit") is None])
    if s["opened"] >= TARGET and open_now == 0:
        s["done"] = True
        wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
        log(f"  🏁 CAMPAIGN COMPLETE: {s['opened']} opened · win {wr:.1f}% · net ${s['net_usd']:+.2f}")
    _save(s, pos)
    return {**s, "open_now": open_now}


def status() -> dict:
    s, pos = _load()
    open_pos = [p for p in pos if p.get("exit") is None]
    recent = [p for p in pos if p.get("exit") is not None][-10:]
    wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
    return {"target": TARGET, "opened": s["opened"], "closed": s["closed"], "open_now": len(open_pos),
            "win_pct": round(wr, 1), "net_usd": s["net_usd"], "done": s.get("done", False),
            "failed_conditions": len(s["failed_conditions"]),
            "open_positions": [{k: p[k] for k in ("id", "asset", "dir", "tier", "entry", "votes", "utc_open")}
                               for p in open_pos],
            "recent_closes": [{k: p.get(k) for k in ("id", "asset", "dir", "net_pct", "pnl_usd", "utc_close")}
                              for p in recent],
            "principle": "paper capital at live prices — REAL capital still requires a validated edge"}
