"""Dynamic whale-watchlist self-management — 'migrate the losers'.

The watchlist is NOT static. The agent keeps it healthy: score each tracked
wallet against quality criteria, and when too many underperform or the data
goes stale, MIGRATE — re-scan fresh on-chain activity (real GeckoTerminal data
via whale_indexer) and replace losers with new qualifying smart-money wallets.

Deterministic tools here compute health + perform the migration (never invent
numbers). The LLM (see scripts/23_whale_self_management.py) decides WHEN to
migrate and explains why. This is the agent's on-chain self-management loop.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from firm.sources.whale_indexer import build_watchlist

ROOT = Path(__file__).resolve().parents[1]
WATCHLIST = ROOT / "data" / "whale_watchlist.json"

# ── Quality criteria for a wallet worth following (smart money) ──
MIN_VOLUME_USD = 1_000.0      # below this = dust, no signal value
LOSS_TOLERANCE_USD = -50.0    # clean realized PnL below this = proven loser -> drop
PRICE_RATIO_MAX = 3.0         # avg_buy vs avg_sell within this factor => single-asset, PnL trustworthy
MAX_STALE_HOURS = 36.0        # data older than this => stale -> migrate
MIN_KEEP_FRACTION = 0.6       # fewer than this share qualifying => migrate


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _pnl_reliable(w: dict) -> bool:
    """Realized PnL is trustworthy ONLY for a single-pool, two-sided trader whose avg
    buy/sell prices sit on the same scale. A multi-pool trader blends prices across
    different base tokens (WETH ~$2000, MNT ~$0.6, stables ~$1), so the computed PnL is a
    meaningless artifact — we refuse to score it (honesty over a flashy number). Likewise
    a one-sided trader has no realized round-trip."""
    pools = w.get("pools") or []
    b, s = float(w.get("avg_buy_price") or 0), float(w.get("avg_sell_price") or 0)
    if len(pools) != 1 or b <= 0 or s <= 0:
        return False
    return max(b, s) / min(b, s) <= PRICE_RATIO_MAX


def assess(w: dict) -> dict:
    """Per-wallet verdict against the quality criteria."""
    reasons: list[str] = []
    status = "keep"
    vol = float(w.get("total_volume_usd") or 0)
    pnl = float(w.get("realized_pnl_usd") or 0)
    reliable = _pnl_reliable(w)

    if vol < MIN_VOLUME_USD:
        status = "drop"
        reasons.append(f"dust (${vol:,.0f} < ${MIN_VOLUME_USD:,.0f})")
    if reliable and pnl < LOSS_TOLERANCE_USD:
        status = "drop"
        reasons.append(f"proven loser (realized PnL ${pnl:+,.0f}, single-pool)")
    if not reliable and pnl != 0:
        reasons.append("PnL unreliable (multi-pool/one-sided) -> not scored")
    if status == "keep":
        if reliable and pnl > 0:
            reasons.append(f"profitable (${pnl:+,.0f}, single-pool)")
        if w.get("direction_bias") == "accumulating":
            reasons.append("accumulating (smart-money entry)")
        if not reasons:
            reasons.append("active, size ok")
    return {
        "address": w.get("address", "?"),
        "status": status,
        "realized_pnl_usd": pnl if reliable else None,
        "pnl_reliable": reliable,
        "direction_bias": w.get("direction_bias", "?"),
        "total_volume_usd": vol,
        "reasons": reasons,
    }


def evaluate(whales: list[dict], now: datetime | None = None) -> dict:
    """Health report for the whole watchlist + a migration recommendation."""
    now = now or datetime.now(timezone.utc)
    verdicts = [assess(w) for w in whales]
    keeps = [v for v in verdicts if v["status"] == "keep"]
    losers = [v for v in verdicts if any("loser" in r for r in v["reasons"])]

    seen = [t for t in (_parse_ts(w.get("last_seen_utc")) for w in whales) if t]
    newest = max(seen) if seen else None
    age_h = (now - newest).total_seconds() / 3600.0 if newest else None

    keep_frac = len(keeps) / len(whales) if whales else 0.0
    stale = bool(age_h is not None and age_h > MAX_STALE_HOURS)
    needs_migration = stale or keep_frac < MIN_KEEP_FRACTION

    return {
        "total": len(whales),
        "keep": len(keeps),
        "drop": len(whales) - len(keeps),
        "keep_fraction": round(keep_frac, 2),
        "losers": len(losers),
        "data_age_hours": round(age_h, 1) if age_h is not None else None,
        "stale": stale,
        "needs_migration": needs_migration,
        "verdicts": verdicts,
    }


def load(path: Path = WATCHLIST) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def whale_flow_summary(whales: list[dict]) -> dict:
    """Aggregate the watchlist into a decision-ready smart-money FLOW summary.

    USD-denominated (sum of buy/sell *volume_usd*), so it is NOT affected by the cross-token
    PnL problem — total USD bought vs sold by tracked smart-money is clean. `net_score` in
    [-1,+1] = net / gross flow."""
    if not whales:
        return {"available": False, "net_bias": "neutral"}
    total_buy = sum(float(w.get("buy_volume_usd") or 0) for w in whales)
    total_sell = sum(float(w.get("sell_volume_usd") or 0) for w in whales)
    net, gross = total_buy - total_sell, total_buy + total_sell
    net_score = net / gross if gross > 0 else 0.0
    n = len(whales)
    n_acc = sum(1 for w in whales if w.get("direction_bias") == "accumulating")
    n_dist = sum(1 for w in whales if w.get("direction_bias") == "distributing")
    nets = sorted((abs(float(w.get("net_volume_usd") or 0)) for w in whales), reverse=True)
    top1 = (nets[0] / sum(nets)) if nets and sum(nets) > 0 else 0.0
    bias = "accumulating" if net_score > 0.15 else ("distributing" if net_score < -0.15 else "neutral")
    return {
        "net_bias": bias,
        "net_flow_usd": round(net, 0),
        "net_score": round(net_score, 3),
        "total_buy_usd": round(total_buy, 0),
        "total_sell_usd": round(total_sell, 0),
        "n_whales": n, "n_accumulating": n_acc, "n_distributing": n_dist,
        "pct_accumulating": round(n_acc / n * 100) if n else 0,
        "conviction": "concentrated" if top1 >= 0.5 else "broad",
        "top1_net_share_pct": round(top1 * 100),
    }


def migrate(now: datetime | None = None, top_k: int = 20, path: Path = WATCHLIST) -> dict:
    """Re-scan fresh on-chain activity, keep only wallets passing criteria, save the
    new list. Returns a diff vs the previous list. Uses REAL GeckoTerminal Mantle data."""
    now = now or datetime.now(timezone.utc)
    before = load(path)
    before_addrs = {w["address"] for w in before}

    fresh = [asdict(p) for p in build_watchlist(top_k=top_k * 2, out_path=None)]  # scan wide, then filter
    qualified = [w for w in fresh if assess(w)["status"] == "keep"][:top_k]

    path.write_text(json.dumps(qualified, indent=2), encoding="utf-8")
    after_addrs = {w["address"] for w in qualified}

    return {
        "before_count": len(before),
        "scanned": len(fresh),
        "after_count": len(qualified),
        "dropped": sorted(before_addrs - after_addrs),
        "added": sorted(after_addrs - before_addrs),
        "retained": sorted(before_addrs & after_addrs),
    }
