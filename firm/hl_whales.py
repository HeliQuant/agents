"""firm/hl_whales.py — DYNAMIC per-asset whale tracker via Hyperliquid (public positions + PnL).

The honest "follow the whales" desk. Hyperliquid publicly exposes (a) a leaderboard of ~38k traders with
their PnL, and (b) any address's live perp positions + unrealized PnL. So for the asset currently traded,
this auto-discovers the top traders (by recent PnL), then reads THEIR position in that asset — net long vs
short, total notional, and their PnL — i.e. "what is the smart money actually doing in BTC / HYPE / SOL
right now." Covers ~230 HL perps (BTC, ETH, SOL, HYPE, SUI, ...) and works from the cloud (HL is reachable
from Railway). Honest scope: HL is ONE venue (the very biggest BTC whales may sit on CME/Binance) — but
these are real, ranked, publicly-verifiable smart-money positions, not a guess.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
CACHE = ROOT / "data" / "hl_whales_cache.json"
REFRESH_H = 12          # re-pull the (large) leaderboard at most every 12h
DEFAULT_N = 40          # how many top traders to follow


def _window_pnl(row: dict, window: str) -> float:
    for w in row.get("windowPerformances", []):
        if w and w[0] == window:
            try:
                return float(w[1].get("pnl", 0))
            except Exception:  # noqa: BLE001
                return 0.0
    return 0.0


def top_addresses(n: int = DEFAULT_N, window: str = "month") -> list[str]:
    """Cached top-N HL trader addresses ranked by `window` PnL. Refreshes the leaderboard every REFRESH_H."""
    from firm import state_store
    c = state_store.load("hl_whales", {}) or {}   # cache in Supabase -> survives redeploys; cloud skips the 32MB fetch
    if c.get("window") == window and (time.time() - c.get("epoch", 0)) < REFRESH_H * 3600 and c.get("addrs"):
        return c["addrs"][:n]
    try:
        rows = requests.get(LEADERBOARD, timeout=90).json().get("leaderboardRows", [])
    except Exception:  # noqa: BLE001
        return (c.get("addrs") or [])[:n]   # fetch failed (slow cloud) -> fall back to stale cache
    ranked = sorted(rows, key=lambda r: _window_pnl(r, window), reverse=True)
    addrs = [r["ethAddress"] for r in ranked[:max(n, DEFAULT_N) * 2]]  # cache a bit extra
    state_store.save("hl_whales", {"epoch": time.time(), "window": window, "addrs": addrs})
    return addrs[:n]


_CS_CACHE: dict[str, tuple[float, dict]] = {}   # addr -> (epoch, clearinghouseState); reused across assets
_CS_TTL = 300                                    # 5-min cache -> whale_read(BTC) reuses whale_read(MNT)'s fetches


def _clearinghouse(addr: str) -> dict:
    now = time.time()
    if addr in _CS_CACHE and now - _CS_CACHE[addr][0] < _CS_TTL:
        return _CS_CACHE[addr][1]
    try:
        cs = requests.post(INFO, json={"type": "clearinghouseState", "user": addr}, timeout=12).json()
    except Exception:  # noqa: BLE001
        cs = {}
    _CS_CACHE[addr] = (now, cs)
    return cs


def _position(addr: str, asset: str) -> dict | None:
    """That address's live position in `asset` on HL (None if flat). Uses the per-cycle clearinghouse cache."""
    cs = _clearinghouse(addr)
    for ap in cs.get("assetPositions", []):
        p = ap.get("position", {})
        if p.get("coin") == asset.upper():
            szi = float(p.get("szi", 0))
            if szi == 0:
                return None
            return {"side": "LONG" if szi > 0 else "SHORT", "notional_usd": abs(float(p.get("positionValue", 0))),
                    "upnl_usd": float(p.get("unrealizedPnl", 0)), "roe": float(p.get("returnOnEquity", 0))}
    return None


def whale_read(asset: str, n: int = DEFAULT_N, window: str = "month") -> dict:
    """What the top-N HL traders are DOING in `asset` right now — net long/short, notional, PnL, stance."""
    addrs = top_addresses(n, window)
    longs = shorts = 0
    long_usd = short_usd = 0.0
    roes = []
    for a in addrs:
        pos = _position(a, asset)
        if not pos:
            continue
        roes.append(pos["roe"])
        if pos["side"] == "LONG":
            longs += 1; long_usd += pos["notional_usd"]
        else:
            shorts += 1; short_usd += pos["notional_usd"]
    in_pos = longs + shorts
    net_usd = long_usd - short_usd
    stance = "NEUTRAL"
    if in_pos:
        stance = "LONG" if net_usd > 0 else "SHORT" if net_usd < 0 else "NEUTRAL"
    avg_roe = round(sum(roes) / len(roes) * 100, 1) if roes else None
    brief = (f"WHALE DESK (HL top-{n} by {window} PnL — live positions in {asset.upper()}): "
             + (f"{in_pos} hold it — {longs} LONG (${long_usd/1e6:.1f}M) vs {shorts} SHORT (${short_usd/1e6:.1f}M) "
                f"-> net {stance} (${net_usd/1e6:+.1f}M); their avg ROE {avg_roe}%. Following informed flow."
                if in_pos else f"none of the top-{n} HL traders hold {asset.upper()} right now -> no whale signal."))
    return {"asset": asset.upper(), "whales_in_position": in_pos, "long": longs, "short": shorts,
            "long_usd": round(long_usd), "short_usd": round(short_usd), "net_usd": round(net_usd),
            "avg_roe_pct": avg_roe, "stance": stance, "n_tracked": len(addrs), "brief": brief}


def whale_detail(asset: str, n: int = DEFAULT_N, window: str = "month", top: int = 10) -> list[dict]:
    """The individual top-PnL HL traders HOLDING `asset` right now — address, side, notional, ROE, unrealized PnL.
    Sorted by notional desc, capped to `top`. Powers the clickable per-whale wallet rows on the /whales page.
    Reuses the clearinghouse cache warmed by whale_read, so it adds no extra fetches when called alongside it."""
    rows = []
    for a in top_addresses(n, window):
        pos = _position(a, asset)
        if not pos:
            continue
        rows.append({"address": a, "side": pos["side"], "notional_usd": round(pos["notional_usd"]),
                     "roe_pct": round(pos["roe"] * 100, 1), "upnl_usd": round(pos["upnl_usd"])})
    rows.sort(key=lambda r: r["notional_usd"], reverse=True)
    return rows[:top]


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    for a in (sys.argv[1:] or ["BTC"]):
        r = whale_read(a, n=30)
        print(f"\n{r['brief']}")
        print(f"  raw: long={r['long']} short={r['short']} net=${r['net_usd']:,} avg_roe={r['avg_roe_pct']}% (tracked {r['n_tracked']})")
