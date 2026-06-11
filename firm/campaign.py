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
TARGET = 1000              # the floor is a CONTINUOUS exploration engine — the first 100 was the proof;
                           #   keep it running (and learning) instead of freezing 'done' at 100
SLOTS_PER_ASSET = 4
HORIZON_H = 4               # no-edge max hold: time-exit if neither SL nor TP is hit first
HORIZON_EDGE_H = 24         # EDGE assets let winners run far longer (backtested: trail+long-cap pays only WITH edge)
TRAIL_K = 1.8               # chandelier trailing-stop distance (xATR) — ratchets toward price on edge trades
EDGE_SIZE_MULT = 2.0        # edge + regime-favored -> size up 2x (scripts/90: amplifying pays ONLY where edge exists)
COST_RT = 0.0020
COOLDOWN_H = 12
FAIL_BAN_H = 24             # a failed condition is benched 24h then FORGIVEN + re-tried (regime may have
                            #   turned) — bans EXPIRE so the firm can adapt back, never starves permanently
LEARN_MIN_N = 4             # min realized closes on a (condition@regime) before its record steers sizing
                            #   (=SLOTS_PER_ASSET so the fade engages right when a losing round bans it)
LEARN_FADE = 0.5            # a condition with a PROVEN-losing realized record trades at half size (learned
                            #   caution that PERSISTS past the 24h ban) — still a probe, so it can re-learn
MAX_DATA_AGE_H = 12
ATR_TP_MULT = 2.5           # take-profit at 2.5x the 1h ATR (reachable within the 4h horizon if it trends)
ATR_SL_MULT = 1.8           # stop-loss at 1.8x ATR -> R:R ~1.4, both sized to the asset's real volatility
ATR_FALLBACK_PCT = 0.006    # 0.6% if ATR can't be measured
VIRTUAL_FULL = 100.0
VIRTUAL_LEAN = 50.0

_GECKO_IDS = {"MNT": "mantle", "BTC": "bitcoin", "ETH": "ethereum",
              "SOL": "solana", "HYPE": "hyperliquid", "SUI": "sui"}

_DEF = {"opened": 0, "closed": 0, "wins": 0, "net_usd": 0.0,
        "failed_conditions": {}, "cooldown_until": {}, "rounds": {}, "done": False}


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
    # MANTLE-NATIVE lean for the flagship (MNT + Mantle-eco): capital flowing INTO Mantle (rising chain
    # TVL) = risk-on -> long lean; OUT = risk-off -> short. Real DeFiLlama data so HeliQuant's home asset
    # isn't idle when the generic desks are flat. Advisory/exploration — the cond_record loop fades it.
    if asset.upper() in ("MNT", "METH", "CMETH", "FBTC", "USDE", "USDY"):
        lean, why = _mantle_lean()
        if lean:
            votes.append((lean, why))
    return sum(v for v, _ in votes), [r for _, r in votes]


def _mantle_lean() -> tuple[int, str]:
    """Directional lean from DeFiLlama Mantle chain-TVL flow — 7d trend primary, 30d fallback."""
    try:
        from firm.defillama_client import chain_tvl
        t = chain_tvl("Mantle")
        if not t:
            return 0, ""
        c7, c30 = t.get("chg7d_pct", 0.0), t.get("chg30d_pct", 0.0)
        if c7 > 1:
            lean, why = 1, f"mantle-tvl:+{c7:.0f}%/7d->L"
        elif c7 < -1:
            lean, why = -1, f"mantle-tvl:{c7:.0f}%/7d->S"
        elif c30 > 5:
            lean, why = 1, f"mantle-tvl:+{c30:.0f}%/30d->L"
        elif c30 < -5:
            lean, why = -1, f"mantle-tvl:{c30:.0f}%/30d->S"
        else:
            return 0, ""
        # CONFLUENCE: TVL is a slow FUNDAMENTAL read, not a 4h price predictor. Only act when price
        # agrees with it — never short MNT into a rising price just because TVL fell (that's what
        # jebol'd the flagship). If price contradicts, defer (the trend-follow path then rides price).
        pt = _trend("MNT")
        if (lean < 0 and pt == "up") or (lean > 0 and pt == "down"):
            return 0, ""
        return lean, why
    except Exception:  # noqa: BLE001
        return 0, ""


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
    s["done"] = False  # continuous autonomous floor — clear any stale 'done' so it never freezes
    # migrate legacy permanent bans (list) -> expiring bans (dict). Forgive them now (expiry 0): the
    # regime that failed them has likely turned, so re-try; if they still lose they re-ban with a TTL.
    if isinstance(s.get("failed_conditions"), list):
        s["failed_conditions"] = {c: 0.0 for c in s["failed_conditions"]}
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
        # LEARN FROM THE OUTCOME: accumulate this condition's REALIZED track record (n / wins / pnl).
        # Every close teaches it — the mistake isn't just benched, it's studied and steers future sizing.
        rkey = f"{p['cond']} @{p.get('regime', 'flat')}"  # regime-aware: learn 'loses in up', not 'always'
        rec = s.setdefault("cond_record", {}).setdefault(rkey, {"n": 0, "wins": 0, "pnl": 0.0})
        rec["n"] += 1
        rec["wins"] += int(pnl > 0)
        rec["pnl"] = round(rec["pnl"] + pnl, 4)
        r = s["rounds"].setdefault(p["asset"], {"closed": 0, "pnl": 0.0, "conds": []})
        r["closed"] += 1
        r["pnl"] = round(r["pnl"] + pnl, 4)
        r["conds"].append(p["cond"])
        if r["closed"] >= SLOTS_PER_ASSET:
            if r["pnl"] <= 0:
                for c in set(r["conds"]):
                    s["failed_conditions"][c] = _now() + FAIL_BAN_H * 3600   # benched, not banned forever
                s["cooldown_until"][p["asset"]] = _now() + COOLDOWN_H * 3600
                log(f"  📚 CAMPAIGN LEARN {p['asset']}: round ${r['pnl']:+.2f} -> conditions benched {FAIL_BAN_H}h + cooldown")
            s["rounds"][p["asset"]] = {"closed": 0, "pnl": 0.0, "conds": []}

    # ── open new (desk-justified only) ──
    scan: dict = {}   # per-asset: WHY it did/didn't open this step (diagnostic, surfaced on /campaign)
    for a in BASKET:
        # NO lifetime cap — fully autonomous, trades continuously. The only throttle is SLOTS_PER_ASSET
        # (max concurrent positions per asset); lifetime opened just keeps climbing.
        cd = s["cooldown_until"].get(a, 0) - _now()
        if cd > 0:
            scan[a] = f"cooldown {cd / 3600:.1f}h"
            continue
        if len([p for p in pos if p["asset"] == a and p.get("exit") is None]) >= SLOTS_PER_ASSET:
            scan[a] = "slots full"
            continue
        _refresh_if_stale(a, log=log)  # Amsterdam: Bybit reachable -> the campaign feeds itself
        net, reasons = desk_votes(a)
        if net == 0 or not reasons:
            # desks flat (no positioning extreme fired). If the REGIME is clear, ride it as trend-follow
            # exploration (the regime itself is the thesis) — keeps the floor working between extremes.
            # Still NOT a coin-flip: a flat regime stays skipped, and the loop fades it if it bleeds.
            t = _trend(a)
            if t in ("up", "down"):
                net = 1 if t == "up" else -1
                reasons = [f"trend-follow:{t}"]
            else:
                scan[a] = "desks flat + regime flat"
                continue
        cond = _cond_key(a, reasons)
        if _now() < s["failed_conditions"].get(cond, 0):
            scan[a] = "benched (failed cond)"
            continue  # still benched (ban not yet expired); after FAIL_BAN_H it's forgiven + re-tried
        px = prices.get(a)
        if not px:
            scan[a] = "no price"
            continue
        direction = "LONG" if net > 0 else "SHORT"
        # ── REGIME ENTRY GATE: don't fight a clear trend. Live diagnosis — every counter-trend short
        #    bled (-1.3..-3.3%) while trend-aligned longs won; the contrarian desk vote was getting run
        #    over by the move. Skip an entry the prevailing 1h regime clearly opposes (proxy classifier). ──
        trend = _trend(a)
        flipped = False
        if (trend == "up" and direction == "SHORT") or (trend == "down" and direction == "LONG"):
            # Contrarian wants to FIGHT the trend. Veto that side — but rather than go dormant, take the
            # TREND-ALIGNED side as exploration (trend-aligned longs won live; cond_record fades it if it
            # bleeds, cooldown cools it). Keeps the floor active in trends instead of locked, and feeds
            # the learning loop which judges whether trend-following pays per asset.
            s["skips"] = s.get("skips", 0) + 1
            sk = s.setdefault("recent_skips", [])
            sk.append({"asset": a, "dir": direction, "regime": trend,
                       "utc": datetime.now(timezone.utc).isoformat()})
            del sk[:-8]
            direction = "LONG" if trend == "up" else "SHORT"
            reasons = [f"trend-follow:{trend}"]
            cond = _cond_key(a, reasons)
            if _now() < s["failed_conditions"].get(cond, 0):
                scan[a] = "veto->trend benched"
                continue
            flipped = True
            scan[a] = f"veto contrarian -> trend-follow {direction}"
            log(f"  ↪ CAMPAIGN FLIP {a}: contrarian vetoed (regime {trend}) -> trend-follow {direction} (exploration)")
        tier = "LEAN" if flipped else ("STRONG" if abs(net) >= 2 else "LEAN")
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
        # LEARNED SIZING: fade a condition with a PROVEN-losing record IN THIS REGIME (regime-aware: a
        #   short that bled in an up-market isn't penalised in a down-market). Persists past the 24h ban.
        #   We DON'T size winners up — that needs a validated edge, not luck.
        rkey = f"{cond} @{trend}"
        rec = s.get("cond_record", {}).get(rkey)
        learned = "n/a"
        if rec and rec["n"] >= LEARN_MIN_N:
            avg = rec["pnl"] / rec["n"]
            if avg < 0:
                size *= LEARN_FADE
                learned = f"FADE @{trend} (record {rec['wins']}/{rec['n']} ${rec['pnl']:+.2f})"
            else:
                learned = f"trusted @{trend} ({rec['wins']}/{rec['n']} ${rec['pnl']:+.2f})"
        cap_h = HORIZON_EDGE_H if edge else HORIZON_H
        p = {"id": s["opened"] + 1, "asset": a, "dir": direction, "tier": tier,
             "size_usd": size, "edge": edge, "cap_h": cap_h, "best": px, "trail": sl,
             "entry": px, "sl": sl, "tp": tp, "atr_pct": round(atr * 100, 3),
             "sl_pct": round(sl_d * 100, 2), "tp_pct": round(tp_d * 100, 2),
             "t_open": _now(), "utc_open": datetime.now(timezone.utc).isoformat(),
             "votes": net, "reasons": reasons[:6], "cond": cond, "regime": trend, "exit": None}
        pos.append(p)
        s["opened"] += 1
        ex_txt = f"EDGE×{EDGE_SIZE_MULT:g} trail/{cap_h}h" if (edge and size > base) else ("EDGE trail" if edge else f"{cap_h}h cap")
        log(f"  🟢 CAMPAIGN OPEN #{p['id']} {direction} {a} @ {px} {tier} ${size:g} [{ex_txt}] (net {net:+}) "
            f"SL {sl} (−{sl_d*100:.1f}%) · TP {tp} (+{tp_d*100:.1f}%) · ATR {atr*100:.2f}% · learned:{learned} — {'; '.join(reasons[:2])}")
        _exec_open(p, log)  # LONGs become REAL Bybit TESTNET spot fills when CAMPAIGN_EXECUTE=1
        scan[a] = f"OPENED {direction} {tier}" + (f" · {learned}" if learned != "n/a" else "")

    s["last_scan"] = scan  # why each asset did/didn't open this step (surfaced on /campaign)
    open_now = len([p for p in pos if p.get("exit") is None])
    s["done"] = False  # never completes — continuous autonomous floor
    s["last_step_utc"] = datetime.now(timezone.utc).isoformat()  # heartbeat: proves the step actually runs
    _anchor_closed(pos, log)  # seal each resolved trade on Mantle (best-effort; gated by CAMPAIGN_ANCHOR)
    _save(s, pos, log)
    try:  # readback verify — proves sl/tp actually persisted (diagnoses the not-showing issue)
        from firm import state_store
        chk = state_store.load("campaign_pos", [])
        n = sum(1 for p in chk if isinstance(p, dict) and p.get("sl") is not None)
        log(f"  [campaign] persist check: {len(chk)} pos in store · {n} carry sl/tp")
    except Exception as e:  # noqa: BLE001
        log(f"  [campaign] persist check failed: {str(e)[:70]}")
    return {**s, "open_now": open_now}


ANCHOR_PER_STEP = 12   # cap on-chain anchors per step (bounds gas + step latency)


def _anchor_closed(pos: list, log=print) -> None:
    """Seal RESOLVED trades on Mantle Sepolia — one 0-value self-send per trade carrying its outcome
    hash. Backfills any closed trade without an anchor yet (bounded per step). Gated by CAMPAIGN_ANCHOR
    (default ON; set 0 to disable). Best-effort — never blocks or fails the paper close."""
    if os.environ.get("CAMPAIGN_ANCHOR", "1") == "0":
        return
    todo = [p for p in pos if p.get("exit") is not None and not p.get("anchor_tx")][:ANCHOR_PER_STEP]
    if not todo:
        return
    try:
        from firm.onchain_recorder import batch_anchor, canonical_trade
        res = batch_anchor([canonical_trade(p) for p in todo])
        by_id = {r["id"]: r for r in res}
        for p in todo:
            r = by_id.get(p["id"])
            if r:
                p["anchor_tx"] = r["tx_hash"]
        if res:
            log(f"  [campaign] anchored {len(res)} trade(s) on Mantle · latest {res[-1]['tx_hash'][:14]}...")
    except Exception as e:  # noqa: BLE001
        log(f"  [campaign] anchor skipped ({str(e)[:60]})")


def trade_log(limit: int = 120) -> dict:
    """The TRADE LEDGER — every RESOLVED campaign trade with its full data (newest first). These are
    paper trades at live prices (real fills happen on Bybit testnet when CAMPAIGN_EXECUTE=1); the
    on-chain layer anchors the DECISIONS, not each paper fill — so this is labelled honestly off-chain."""
    s, pos = _load()
    closed = [p for p in pos if p.get("exit") is not None]
    rows = [{k: p.get(k) for k in ("id", "asset", "dir", "tier", "entry", "exit", "exit_reason",
                                   "net_pct", "pnl_usd", "size_usd", "regime", "reasons",
                                   "utc_open", "utc_close", "anchor_tx")}
             for p in closed[-limit:]][::-1]
    wins = sum(1 for p in closed if (p.get("pnl_usd") or 0) > 0)
    return {"count": len(closed), "wins": wins,
            "win_pct": round(100 * wins / len(closed), 1) if closed else 0.0,
            "net_usd": round(s.get("net_usd", 0.0), 4),
            "open_now": len([p for p in pos if p.get("exit") is None]), "trades": rows}


def status() -> dict:
    s, pos = _load()
    open_pos = [p for p in pos if p.get("exit") is None]
    recent = [p for p in pos if p.get("exit") is not None][-12:]
    wr = (s["wins"] / s["closed"] * 100) if s["closed"] else 0.0
    closed = [p for p in pos if p.get("exit") is not None]
    by_reason = {r: len([p for p in closed if p.get("exit_reason") == r]) for r in ("TP", "SL", "TIME", "TRAIL")}
    from firm import state_store  # diagnostics: is the step alive, and do writes actually persist?
    save_err = state_store.LAST_SAVE_ERR.get("campaign_pos", "")
    cr = s.get("cond_record", {})
    learned = {"tracked": len(cr),
               "faded": len([1 for v in cr.values() if v["n"] >= LEARN_MIN_N and v["pnl"] < 0]),
               "records": sorted(({"cond": c, **v} for c, v in cr.items()), key=lambda x: x["pnl"])[:6]}
    diag = {"campaign_on": os.environ.get("CAMPAIGN", "1") == "1",
            "last_step_utc": s.get("last_step_utc"),
            "persist": "local" if save_err else "supabase", "save_err": save_err, "learned": learned}
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

    _fc = s.get("failed_conditions") or {}
    fc_active = len(_fc) if isinstance(_fc, list) else len([1 for e in _fc.values() if _now() < e])
    return {"target": None, "continuous": True,  # no lifetime cap — fully autonomous, never 'done'
            "opened": s["opened"], "closed": s["closed"], "open_now": len(open_pos),
            "win_pct": round(wr, 1), "net_usd": s["net_usd"], "done": False,
            "testnet_fills": len([p for p in pos if p.get("venue")]),
            "exits_by_reason": by_reason, "failed_conditions": fc_active,
            "horizon_h": HORIZON_H, **diag,
            "edge_open": len([p for p in open_pos if p.get("edge")]),
            "sized_up_open": len([p for p in open_pos if (p.get("size_usd") or 0) > VIRTUAL_FULL]),
            "skips": s.get("skips", 0), "recent_skips": s.get("recent_skips", [])[-6:],
            "last_scan": s.get("last_scan", {}),  # per-asset reason it did/didn't open last step
            "risk_model": (f"edge-gated · NO-edge: SL {ATR_SL_MULT}×ATR / TP {ATR_TP_MULT}×ATR / {HORIZON_H}h cap · "
                           f"EDGE: trailing {TRAIL_K}×ATR / {HORIZON_EDGE_H}h, regime-favored size ×{EDGE_SIZE_MULT:g}"),
            "open_positions": [_view(p) for p in open_pos],
            "recent_closes": [{k: p.get(k) for k in ("id", "asset", "dir", "exit", "exit_reason",
                                                     "net_pct", "pnl_usd", "utc_close")} for p in recent],
            "principle": "paper capital at live prices — REAL capital still requires a validated edge"}
