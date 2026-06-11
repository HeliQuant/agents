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

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
BYBIT = "https://api.bybit.com"

BASKET = ["MNT", "BTC", "ETH", "SOL", "HYPE", "SUI"]
TARGET = 100
SLOTS_PER_ASSET = 4
HORIZON_H = 4               # no-edge max hold: time-exit if neither SL nor TP is hit first
HORIZON_EDGE_H = 24         # EDGE assets let winners run far longer (backtested: trail+long-cap pays only WITH edge)
TRAIL_K = 1.8               # chandelier trailing-stop distance (xATR) — ratchets toward price on edge trades
EDGE_SIZE_MULT = 2.0        # edge + regime-favored -> size up 2x (scripts/90: amplifying pays ONLY where edge exists)
COST_RT = 0.0020
COOLDOWN_H = 12
MAX_DATA_AGE_H = 12
ATR_TP_MULT = 2.5           # take-profit at 2.5x the 1h ATR (reachable within the 4h horizon if it trends)
ATR_SL_MULT = 1.8           # stop-loss at 1.8x ATR -> R:R ~1.4, both sized to the asset's real volatility
ATR_FALLBACK_PCT = 0.006    # 0.6% if ATR can't be measured
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


def _save(s: dict, pos: list, log=None) -> None:
    from firm import state_store
    b1 = state_store.save("campaign_state", s)
    b2 = state_store.save("campaign_pos", pos)
    if log and (b2 != "supabase" or state_store.LAST_SAVE_ERR.get("campaign_pos")):
        err = state_store.LAST_SAVE_ERR.get("campaign_pos", "")
        log(f"  ⚠ campaign_pos -> {b2} (NOT persisted across redeploys){' · ' + err if err else ''}")


def live_prices() -> dict:
    """Live marks for the basket. Bybit perp tickers FIRST (the venue's own price — reachable from
    Railway's Amsterdam region), DeFiLlama batch fallback, CoinGecko last."""
    out: dict = {}
    for a in _GECKO_IDS:
        try:
            r = requests.get(f"{BYBIT}/v5/market/tickers",
                             params={"category": "linear", "symbol": f"{a}USDT"}, timeout=10).json()
            out[a] = float(r["result"]["list"][0]["lastPrice"])
        except Exception:  # noqa: BLE001
            out[a] = None
    if all(v for v in out.values()):
        return out
    ids = ",".join(f"coingecko:{g}" for g in _GECKO_IDS.values())
    try:
        r = requests.get(f"https://coins.llama.fi/prices/current/{ids}", timeout=15).json()
        coins = r.get("coins", {})
        for a, g in _GECKO_IDS.items():
            out[a] = out.get(a) or coins.get(f"coingecko:{g}", {}).get("price")
        if any(v for v in out.values()):
            return out
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": ",".join(_GECKO_IDS.values()), "vs_currencies": "usd"},
                         timeout=15).json()
        for a, g in _GECKO_IDS.items():
            out[a] = out.get(a) or (r.get(g) or {}).get("usd")
    except Exception:  # noqa: BLE001
        pass
    return out


def _refresh_if_stale(asset: str, max_age_h: float = 2.0, log=print) -> None:
    """Self-collect fresh positioning data when stale — Bybit is reachable from Amsterdam, so the
    campaign feeds ITSELF (no local feeder dependency)."""
    if _data_age_h(asset) <= max_age_h:
        return
    try:
        from importlib import import_module
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import_module("scripts.73_collect_alt").collect(asset.upper())
        log(f"  [campaign] self-refreshed {asset} positioning data")
    except Exception as e:  # noqa: BLE001
        log(f"  [campaign] {asset} self-refresh failed ({str(e)[:60]})")


# ── TESTNET execution rail (the "live trade" layer) ──────────────────────────────────────────────
# HARD CONSTITUTION GATE: the campaign may only ever touch TESTNET money. Real capital requires a
# validated edge through the PM — that gate lives elsewhere and is untouched. Enable with
# CAMPAIGN_EXECUTE=1 + Bybit TESTNET keys in env. LONG-only (spot can't short); paper ledger stays
# the source of truth for PnL — testnet fills are the proof of execution, not the accounting.

def _exec_mod():
    if os.environ.get("CAMPAIGN_EXECUTE", "0").strip() not in {"1", "true", "yes"}:
        return None
    try:
        from firm import bybit_executor as ex
        if not ex.is_testnet():
            return None  # NEVER real money from the campaign — testnet or nothing
        return ex
    except Exception:  # noqa: BLE001
        return None


def _spot_step(symbol: str) -> float | None:
    try:
        r = requests.get("https://api-testnet.bybit.com/v5/market/instruments-info",
                         params={"category": "spot", "symbol": symbol}, timeout=10).json()
        return float(r["result"]["list"][0]["lotSizeFilter"]["basePrecision"])
    except Exception:  # noqa: BLE001
        return None


def _exec_open(p: dict, log=print) -> None:
    """LONG open -> real market BUY on Bybit TESTNET spot (proven rail: 100/100 fills)."""
    ex = _exec_mod()
    if not ex or p["dir"] != "LONG":
        return
    sym = f"{p['asset']}USDT"
    try:
        s = ex._trade_session()
        base_coin = p["asset"]
        bal0 = _wallet_coin(ex, base_coin)
        ex._ok(s.place_order(category="spot", symbol=sym, side="Buy", orderType="Market",
                             qty=str(p["size_usd"]), marketUnit="quoteCoin"))
        time.sleep(0.8)
        qty = max(0.0, _wallet_coin(ex, base_coin) - bal0)
        if qty > 0:
            p["exec_qty"] = qty
            p["venue"] = "bybit-testnet-spot"
            log(f"  ⚡ EXECUTED #{p['id']} BUY {qty} {base_coin} on Bybit TESTNET (real fill, fake money)")
    except Exception as e:  # noqa: BLE001
        log(f"  [exec] #{p['id']} {sym} buy skipped ({str(e)[:60]}) — paper only")


def _exec_close(p: dict, log=print) -> None:
    ex = _exec_mod()
    if not ex or not p.get("exec_qty"):
        return
    sym = f"{p['asset']}USDT"
    try:
        s = ex._trade_session()
        step_sz = _spot_step(sym) or 0.0001
        qty = int(p["exec_qty"] / step_sz) * step_sz
        if qty <= 0:
            return
        ex._ok(s.place_order(category="spot", symbol=sym, side="Sell", orderType="Market",
                             qty=str(round(qty, 8))))
        p["exec_closed"] = True
        log(f"  ⚡ EXECUTED #{p['id']} SELL {qty} {p['asset']} on Bybit TESTNET (position closed on venue)")
    except Exception as e:  # noqa: BLE001
        log(f"  [exec] #{p['id']} {sym} sell failed ({str(e)[:60]}) — paper close stands")


def _wallet_coin(ex, coin: str) -> float:
    try:
        res = ex._ok(ex._read_session().get_wallet_balance(accountType="UNIFIED"))
        for a in res.get("list", []):
            for c in a.get("coin", []):
                if c.get("coin") == coin:
                    return float(c.get("walletBalance") or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


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


def _atr_pct(asset: str) -> float:
    """ATR(14) on 1h candles as a fraction of price — the asset's REAL volatility, which sets the SL/TP
    distance (so a calm asset gets tight stops, a wild one gets room). Bybit kline (reachable from the
    cloud); falls back to close-to-close realized vol from the fed data, then a flat 0.6%."""
    try:
        r = requests.get(f"{BYBIT}/v5/market/kline",
                         params={"category": "linear", "symbol": f"{asset}USDT", "interval": "60", "limit": "50"},
                         timeout=12).json()
        rows = list(reversed(r["result"]["list"]))  # Bybit returns newest-first
        highs = [float(x[2]) for x in rows]
        lows = [float(x[3]) for x in rows]
        closes = [float(x[4]) for x in rows]
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
               for i in range(1, len(rows))]
        if len(trs) >= 14 and closes[-1] > 0:
            atr = sum(trs[-14:]) / 14
            if atr > 0:
                return min(0.06, atr / closes[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        c = pd.read_csv(ROOT / "data" / f"{asset.lower()}_positioning.csv")["close"].astype(float).tail(48)
        v = float(c.pct_change().dropna().std())
        if v > 0:
            return min(0.05, max(0.002, v))
    except Exception:  # noqa: BLE001
        pass
    return ATR_FALLBACK_PCT


def _has_edge(asset: str) -> bool:
    """True only when the asset carries a registry-VALIDATED edge (validated_edges.json). Empty today
    by design — aggression must be earned by OOS+walk-forward+FDR, never dialed up (scripts/90)."""
    try:
        from firm.asset_efficiency import efficiency_read
        return bool(efficiency_read(asset).get("has_edge"))
    except Exception:  # noqa: BLE001
        return False


def _trend(asset: str) -> str:
    """Light 1h-kline regime read -> 'up' / 'down' / 'flat' (price vs SMA24 + short slope). A cheap,
    RESPONSIVE proxy for the 82.6% regime classifier, used to keep the campaign from FIGHTING a clear
    trend: don't short a strong uptrend, don't long a strong downtrend.

    Sensor window TUNED by backtest (contrarian entry + veto, OOS pooled): a 24h window catches a
    regime flip ~a day sooner than 48h and roughly HALVES the bleed (-86.8% -> -57.6%), while 12h /
    EMA9-21-cross over-react and whipsaw straight back to -87% / -98%. 24h is the responsive sweet
    spot. (Live evidence that drove this: every counter-trend short bled while trend-aligned longs won.)"""
    try:
        r = requests.get(f"{BYBIT}/v5/market/kline",
                         params={"category": "linear", "symbol": f"{asset}USDT", "interval": "60", "limit": "60"},
                         timeout=12).json()
        closes = [float(x[4]) for x in reversed(r["result"]["list"])]
        if len(closes) < 30:
            return "flat"
        sma = sum(closes[-24:]) / 24          # 24h: responsive sweet spot (backtested)
        slope = closes[-1] - closes[-6]
        if closes[-1] > sma and slope > 0:
            return "up"
        if closes[-1] < sma and slope < 0:
            return "down"
        return "flat"
    except Exception:  # noqa: BLE001
        return "flat"


def step(log=print) -> dict:
    """One campaign step: resolve matured positions, open new desk-justified ones. Called by the app
    loop every CAMPAIGN_STEP_MIN. Cheap (no LLM): a few HTTP calls + CSV reads."""
    s, pos = _load()
    if s.get("done"):
        return s
    prices = live_prices()

    # ── backfill ATR SL/TP onto legacy open positions (opened before the risk model) so they too
    #    exit on a level instead of only at the 4h cap — clears slots faster for fresh signals ──
    for p in pos:
        if p.get("exit") is None and p.get("sl") is None:
            atr = _atr_pct(p["asset"])
            sl_d, tp_d = ATR_SL_MULT * atr, ATR_TP_MULT * atr
            sgn = 1 if p["dir"] == "LONG" else -1
            p["sl"] = round(p["entry"] * (1 - sgn * sl_d), 8)
            p["tp"] = round(p["entry"] * (1 + sgn * tp_d), 8)
            p["sl_pct"], p["tp_pct"], p["atr_pct"] = round(sl_d * 100, 2), round(tp_d * 100, 2), round(atr * 100, 3)
            log(f"  ⚙ CAMPAIGN backfill SL/TP #{p['id']} {p['dir']} {p['asset']}: SL {p['sl']} · TP {p['tp']}")

    # ── resolve: SL hit, TP hit, or 4h time-exit (whichever comes first) ──
    for p in pos:
        if p.get("exit") is not None:
            continue
        px = prices.get(p["asset"])
        if not px:
            continue
        reason = None
        if p.get("edge"):  # EDGE asset -> chandelier trailing stop: let the winner run, ratchet stop toward price
            a_atr = ((p.get("atr_pct") or 0) / 100) or ATR_FALLBACK_PCT
            if p["dir"] == "SHORT":
                p["best"] = min(p.get("best", p["entry"]), px)
                p["trail"] = min(p.get("trail", p.get("sl", px)), p["best"] * (1 + TRAIL_K * a_atr))
                if px >= p["trail"]:
                    reason = "TRAIL"
            else:
                p["best"] = max(p.get("best", p["entry"]), px)
                p["trail"] = max(p.get("trail", p.get("sl", px)), p["best"] * (1 - TRAIL_K * a_atr))
                if px <= p["trail"]:
                    reason = "TRAIL"
        elif p.get("sl") is not None and p.get("tp") is not None:
            if p["dir"] == "LONG":
                reason = "SL" if px <= p["sl"] else "TP" if px >= p["tp"] else None
            else:  # SHORT: SL above entry, TP below
                reason = "SL" if px >= p["sl"] else "TP" if px <= p["tp"] else None
        if reason is None and _now() - p["t_open"] >= p.get("cap_h", HORIZON_H) * 3600:
            reason = "TIME"
        if reason is None:
            continue  # still open, neither level hit nor matured
        sign = 1 if p["dir"] == "LONG" else -1
        net_pct = sign * (px / p["entry"] - 1) - COST_RT
        pnl = round(net_pct * p["size_usd"], 4)
        p.update(exit=px, exit_reason=reason, net_pct=round(net_pct * 100, 3), pnl_usd=pnl,
                 utc_close=datetime.now(timezone.utc).isoformat())
        s["closed"] += 1
        s["net_usd"] = round(s["net_usd"] + pnl, 4)
        if pnl > 0:
            s["wins"] += 1
        icon = {"TP": "🎯", "SL": "🛑", "TIME": "⏱", "TRAIL": "🪤"}[reason]
        log(f"  {'🟩' if pnl > 0 else '🟥'}{icon} CAMPAIGN CLOSE #{p['id']} {p['dir']} {p['asset']} {reason} "
            f"{p['net_pct']:+.2f}% (${pnl:+.2f}) | total ${s['net_usd']:+.2f}")
        _exec_close(p, log)
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
        _refresh_if_stale(a, log=log)  # Amsterdam: Bybit reachable -> the campaign feeds itself
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
        # ── REGIME ENTRY GATE: don't fight a clear trend. Live diagnosis — every counter-trend short
        #    bled (-1.3..-3.3%) while trend-aligned longs won; the contrarian desk vote was getting run
        #    over by the move. Skip an entry the prevailing 1h regime clearly opposes (proxy classifier). ──
        trend = _trend(a)
        if (trend == "up" and direction == "SHORT") or (trend == "down" and direction == "LONG"):
            s["skips"] = s.get("skips", 0) + 1   # auditable proof the veto fires (surfaced on /campaign)
            sk = s.setdefault("recent_skips", [])
            sk.append({"asset": a, "dir": direction, "regime": trend,
                       "utc": datetime.now(timezone.utc).isoformat()})
            del sk[:-8]
            log(f"  ⊘ CAMPAIGN SKIP {a} {direction}: counter-trend (regime={trend}) — don't fight the move")
            continue
        tier = "STRONG" if abs(net) >= 2 else "LEAN"
        atr = _atr_pct(a)
        sl_d, tp_d = ATR_SL_MULT * atr, ATR_TP_MULT * atr
        sgn = 1 if direction == "LONG" else -1
        sl = round(px * (1 - sgn * sl_d), 8)   # LONG: SL below entry · SHORT: SL above
        tp = round(px * (1 + sgn * tp_d), 8)   # LONG: TP above entry · SHORT: TP below
        # ── edge-gated aggression (scripts/90): amplify ONLY where a validated edge exists. No edge ->
        #    base size, tight 4h cap, fixed SL/TP (unchanged). Edge -> let it run (trailing) and, if the
        #    regime backs it, size up 2x. Today validated_edges.json is empty, so this stays disciplined. ──
        edge = _has_edge(a)
        favors = (trend == "down" and direction == "SHORT") or (trend == "up" and direction == "LONG")
        base = VIRTUAL_FULL if tier == "STRONG" else VIRTUAL_LEAN
        size = base * EDGE_SIZE_MULT if (edge and favors) else base
        cap_h = HORIZON_EDGE_H if edge else HORIZON_H
        p = {"id": s["opened"] + 1, "asset": a, "dir": direction, "tier": tier,
             "size_usd": size, "edge": edge, "cap_h": cap_h, "best": px, "trail": sl,
             "entry": px, "sl": sl, "tp": tp, "atr_pct": round(atr * 100, 3),
             "sl_pct": round(sl_d * 100, 2), "tp_pct": round(tp_d * 100, 2),
             "t_open": _now(), "utc_open": datetime.now(timezone.utc).isoformat(),
             "votes": net, "reasons": reasons[:6], "cond": cond, "exit": None}
        pos.append(p)
        s["opened"] += 1
        ex_txt = f"EDGE×{EDGE_SIZE_MULT:g} trail/{cap_h}h" if (edge and size > base) else ("EDGE trail" if edge else f"{cap_h}h cap")
        log(f"  🟢 CAMPAIGN OPEN #{p['id']} {direction} {a} @ {px} {tier} ${size:g} [{ex_txt}] (net {net:+}) "
            f"SL {sl} (−{sl_d*100:.1f}%) · TP {tp} (+{tp_d*100:.1f}%) · ATR {atr*100:.2f}% — {'; '.join(reasons[:2])}")
        _exec_open(p, log)  # LONGs become REAL Bybit TESTNET spot fills when CAMPAIGN_EXECUTE=1

    open_now = len([p for p in pos if p.get("exit") is None])
    if s["opened"] >= TARGET and open_now == 0:
        s["done"] = True
        wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
        log(f"  🏁 CAMPAIGN COMPLETE: {s['opened']} opened · win {wr:.1f}% · net ${s['net_usd']:+.2f}")
    s["last_step_utc"] = datetime.now(timezone.utc).isoformat()  # heartbeat: proves the step actually runs
    _save(s, pos, log)
    try:  # readback verify — proves sl/tp actually persisted (diagnoses the not-showing issue)
        from firm import state_store
        chk = state_store.load("campaign_pos", [])
        n = sum(1 for p in chk if isinstance(p, dict) and p.get("sl") is not None)
        log(f"  [campaign] persist check: {len(chk)} pos in store · {n} carry sl/tp")
    except Exception as e:  # noqa: BLE001
        log(f"  [campaign] persist check failed: {str(e)[:70]}")
    return {**s, "open_now": open_now}


def status() -> dict:
    s, pos = _load()
    open_pos = [p for p in pos if p.get("exit") is None]
    recent = [p for p in pos if p.get("exit") is not None][-12:]
    wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
    closed = [p for p in pos if p.get("exit") is not None]
    by_reason = {r: len([p for p in closed if p.get("exit_reason") == r]) for r in ("TP", "SL", "TIME", "TRAIL")}
    from firm import state_store  # diagnostics: is the step alive, and do writes actually persist?
    save_err = state_store.LAST_SAVE_ERR.get("campaign_pos", "")
    diag = {"campaign_on": os.environ.get("CAMPAIGN", "1") == "1",
            "last_step_utc": s.get("last_step_utc"),
            "persist": "local" if save_err else "supabase", "save_err": save_err}
    prices = live_prices() if open_pos else {}   # one batched mark so the FE can place each car live

    def _view(p: dict) -> dict:
        v = {k: p.get(k) for k in ("id", "asset", "dir", "tier", "entry", "sl", "tp",
                                   "sl_pct", "tp_pct", "votes", "reasons", "utc_open")}
        now = prices.get(p["asset"])
        if now:
            sign = 1 if p["dir"] == "LONG" else -1
            v["now"] = now
            v["upnl_pct"] = round((sign * (now / p["entry"] - 1) - COST_RT) * 100, 3)  # net of cost, honest
        return v

    return {"target": TARGET, "opened": s["opened"], "closed": s["closed"], "open_now": len(open_pos),
            "win_pct": round(wr, 1), "net_usd": s["net_usd"], "done": s.get("done", False),
            "testnet_fills": len([p for p in pos if p.get("venue")]),
            "exits_by_reason": by_reason, "failed_conditions": len(s["failed_conditions"]),
            "horizon_h": HORIZON_H, **diag,
            "edge_open": len([p for p in open_pos if p.get("edge")]),
            "sized_up_open": len([p for p in open_pos if (p.get("size_usd") or 0) > VIRTUAL_FULL]),
            "skips": s.get("skips", 0), "recent_skips": s.get("recent_skips", [])[-6:],
            "risk_model": (f"edge-gated · NO-edge: SL {ATR_SL_MULT}×ATR / TP {ATR_TP_MULT}×ATR / {HORIZON_H}h cap · "
                           f"EDGE: trailing {TRAIL_K}×ATR / {HORIZON_EDGE_H}h, regime-favored size ×{EDGE_SIZE_MULT:g}"),
            "open_positions": [_view(p) for p in open_pos],
            "recent_closes": [{k: p.get(k) for k in ("id", "asset", "dir", "exit", "exit_reason",
                                                     "net_pct", "pnl_usd", "utc_close")} for p in recent],
            "principle": "paper capital at live prices — REAL capital still requires a validated edge"}
