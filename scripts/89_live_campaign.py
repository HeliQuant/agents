"""scripts/89 — LIVE CAMPAIGN: open 100 desk-driven positions (paper capital, real prices, real learning).

THE BRIEF (user): "make HeliQuant brave — don't stop until 100 open positions — but stay true to the
firm's principles and desks." The honest synthesis:

  * REAL capital still requires a validated edge — the PM gate is untouched. The registry is empty,
    so no real-money ENTER. That rule IS the product.
  * PAPER positions cost zero capital — exploration's whole design. This campaign scales it into a
    full hunt: every position is OPENED BY THE DESKS (quantitative ones: flow-intel learned signals,
    z-score extremes on OI/funding/flow/momentum, the live whale read), sized by conviction tier,
    entered at the LIVE Bybit price, resolved on the 4h move net of 20bps costs, and LEARNED from:
    a losing round logs its condition signatures (never repeated) + a cooldown for that asset.
  * 4h horizon is a DECLARED hypothesis class (intraday flow), distinct from the edge lab's 24h
    validation standard — campaign results feed the lab, they don't skip it.
  * Every figure in the ledger is computed from real prices. Nothing is invented.

Run:  python scripts/89_live_campaign.py            # default: 6 assets, target 100, cycle 15m
      python scripts/89_live_campaign.py --target 100 --cycle-min 15
State: data/campaign_state.json · Ledger: data/campaign_ledger.jsonl (every open + close, with the
desk votes that justified it). Mirrored to Supabase key "campaign" so the cloud/FE can read it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm import state_store  # noqa: E402
from firm.flow_intel import synthesize as flow_synth  # noqa: E402

BASKET = ["MNT", "BTC", "ETH", "SOL", "HYPE", "SUI"]
SLOTS_PER_ASSET = 4          # concurrent open positions per asset (exploration's budget)
HORIZON_H = 4                # intraday-flow hypothesis class (declared; edge-lab standard stays 24h)
COST_RT = 0.0020             # 20 bps round-trip cost applied to every paper close (honest PnL)
COOLDOWN_H = 12              # losing round -> the asset sits out
VIRTUAL_FULL = 100.0         # virtual $ per STRONG-conviction position
VIRTUAL_LEAN = 50.0          # virtual $ per LEAN-conviction position
STATE_F = ROOT / "data" / "campaign_state.json"
LEDGER_F = ROOT / "data" / "campaign_ledger.jsonl"
BYBIT = "https://api.bybit.com"

_DEF = {"opened": 0, "closed": 0, "wins": 0, "net_usd": 0.0, "positions": [],
        "failed_conditions": [], "cooldown_until": {}, "rounds": {}}


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_F.exists():
        try:
            return json.loads(STATE_F.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return json.loads(json.dumps(_DEF))


def save_state(s: dict) -> None:
    STATE_F.write_text(json.dumps(s, indent=1), encoding="utf-8")
    try:
        state_store.save("campaign", {k: s[k] for k in ("opened", "closed", "wins", "net_usd")}
                         | {"open_now": len([p for p in s["positions"] if p.get("exit") is None]),
                            "utc": datetime.now(timezone.utc).isoformat()})
    except Exception:  # noqa: BLE001
        pass


def ledger(rec: dict) -> None:
    with LEDGER_F.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def live_price(asset: str) -> float | None:
    """LIVE last price from Bybit public tickers (real entry/exit marks — not a stale bar)."""
    try:
        r = requests.get(f"{BYBIT}/v5/market/tickers",
                         params={"category": "linear", "symbol": f"{asset.upper()}USDT"}, timeout=12)
        return float(r.json()["result"]["list"][0]["lastPrice"])
    except Exception:  # noqa: BLE001
        return None


def refresh_data(asset: str) -> bool:
    """Fresh Bybit positioning data -> data/{asset}_positioning.csv (the desks read this)."""
    try:
        import_module("scripts.73_collect_alt").collect(asset.upper())
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"  {asset}: data refresh FAILED ({str(e)[:60]})")
        return False


def desk_votes(asset: str) -> tuple[int, list[str]]:
    """The quantitative desks vote a direction. Positive = LONG, negative = SHORT.
    Sources (all real, all logged): flow-intel synthesis (learned, FDR-gated), each flow signal at a
    z-extreme (direction = learned if validated, else the firm's standing positioning-fade prior),
    and the live Hyperliquid whale read. No LLM calls — this is the data floor."""
    votes: list[tuple[int, str]] = []
    try:
        fi = flow_synth(asset)
    except Exception:  # noqa: BLE001
        fi = {"stance": "NEUTRAL", "reads": {}, "learned": {}}
    if fi.get("stance") == "LONG":
        votes.append((1, "flow-intel:LONG"))
    elif fi.get("stance") == "SHORT":
        votes.append((-1, "flow-intel:SHORT"))
    # signal extremes: fade positioning (oi/funding/flow_imbalance), ride momentum — unless the
    # lab LEARNED otherwise for this asset (learned direction wins when the signal validates)
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
    net = sum(v for v, _ in votes)
    return net, [r for _, r in votes]


def cond_key(asset: str, reasons: list[str]) -> str:
    return f"{asset}|" + "|".join(sorted(r.split(":")[0] + ":" + ("L" if "->L" in r or ":LONG" in r else "S")
                                          for r in reasons))


def try_open(s: dict, asset: str, target: int) -> None:
    if s["opened"] >= target:
        return
    if _now() < s["cooldown_until"].get(asset, 0):
        return
    open_n = len([p for p in s["positions"] if p["asset"] == asset and p.get("exit") is None])
    if open_n >= SLOTS_PER_ASSET:
        return
    net, reasons = desk_votes(asset)
    if net == 0 or not reasons:
        return  # the desks are flat — the firm does not coin-flip, even on paper
    cond = cond_key(asset, reasons)
    if cond in s["failed_conditions"]:
        return  # learned: this exact desk-consensus class lost a round — don't repeat it
    direction = "LONG" if net > 0 else "SHORT"
    tier = "STRONG" if abs(net) >= 2 else "LEAN"
    size = VIRTUAL_FULL if tier == "STRONG" else VIRTUAL_LEAN
    px = live_price(asset)
    if not px:
        return
    pos = {"id": s["opened"] + 1, "asset": asset, "dir": direction, "tier": tier, "size_usd": size,
           "entry": px, "t_open": _now(), "utc_open": datetime.now(timezone.utc).isoformat(),
           "votes": net, "reasons": reasons, "cond": cond, "exit": None}
    s["positions"].append(pos)
    s["opened"] += 1
    ledger({"event": "OPEN", **{k: pos[k] for k in ("id", "asset", "dir", "tier", "size_usd", "entry",
                                                     "utc_open", "votes", "reasons")}})
    _log(f"  🟢 OPEN #{pos['id']:>3} {direction:<5} {asset:<4} @ {px:<10} {tier} (net {net:+}) — {'; '.join(reasons[:3])}")


def resolve(s: dict) -> None:
    for p in s["positions"]:
        if p.get("exit") is not None:
            continue
        if _now() - p["t_open"] < HORIZON_H * 3600:
            continue
        px = live_price(p["asset"])
        if not px:
            continue
        sign = 1 if p["dir"] == "LONG" else -1
        net_pct = sign * (px / p["entry"] - 1) - COST_RT
        pnl = round(net_pct * p["size_usd"], 4)
        p["exit"] = px
        p["net_pct"] = round(net_pct * 100, 3)
        p["pnl_usd"] = pnl
        p["utc_close"] = datetime.now(timezone.utc).isoformat()
        s["closed"] += 1
        s["net_usd"] = round(s["net_usd"] + pnl, 4)
        if pnl > 0:
            s["wins"] += 1
        ledger({"event": "CLOSE", "id": p["id"], "asset": p["asset"], "dir": p["dir"], "exit": px,
                "net_pct": p["net_pct"], "pnl_usd": pnl, "utc_close": p["utc_close"]})
        _log(f"  {'🟩' if pnl > 0 else '🟥'} CLOSE #{p['id']:>3} {p['dir']:<5} {p['asset']:<4} "
             f"{p['net_pct']:+.2f}% (${pnl:+.2f}) | total ${s['net_usd']:+.2f}")
        # learning gate per asset: every SLOTS_PER_ASSET closes, judge the round
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
                _log(f"  📚 LEARN {p['asset']}: round net ${r['pnl']:+.2f} -> conditions logged FAILED + {COOLDOWN_H}h cooldown")
            else:
                _log(f"  📈 ROUND {p['asset']}: net ${r['pnl']:+.2f} positive -> condition class stays live")
            s["rounds"][p["asset"]] = {"closed": 0, "pnl": 0.0, "conds": []}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="HeliQuant live campaign — 100 desk-driven paper positions.")
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--cycle-min", type=int, default=15)
    ap.add_argument("--assets", nargs="*", default=BASKET)
    args = ap.parse_args()
    assets = [a.upper() for a in args.assets]
    s = load_state()
    _log(f"CAMPAIGN start — target {args.target} positions | basket {assets} | "
         f"horizon {HORIZON_H}h | slots {SLOTS_PER_ASSET}/asset | resume: opened={s['opened']} closed={s['closed']}")
    _log("principle: paper capital only — REAL capital still requires a validated edge (PM gate untouched)")
    while True:
        cyc_t0 = _now()
        for a in assets:
            if refresh_data(a):
                try_open(s, a, args.target)
        resolve(s)
        save_state(s)
        open_now = len([p for p in s["positions"] if p.get("exit") is None])
        wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
        _log(f"── progress: opened {s['opened']}/{args.target} · open now {open_now} · closed {s['closed']} "
             f"· win {wr:.0f}% · net ${s['net_usd']:+.2f} · failed-conds {len(s['failed_conditions'])}")
        if s["opened"] >= args.target and open_now == 0:
            break
        time.sleep(max(args.cycle_min, 1) * 60)
    wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
    _log(f"CAMPAIGN COMPLETE: {s['opened']} opened, {s['closed']} closed, win {wr:.1f}%, net ${s['net_usd']:+.2f}")
    _log("ledger: data/campaign_ledger.jsonl — every position carries the desk votes that justified it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
