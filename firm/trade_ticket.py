"""firm/trade_ticket.py — the deterministic TRADE-TICKET layer (Layer 2 + Layer 3).

The LLM org (desks -> bull/bear debate -> PM) decides DIRECTION + CONVICTION (judgment).
This module turns that judgment into a reproducible, risk-controlled TRADE TICKET —
entry zone, structural stop, invalidation, take-profit ladder, R:R per target, and
position size — then GATES it. No number here is invented: every level comes from the
real ATR + swing structure in ``data/{ticker}_features.csv``; the sizing formula is the
exact one the backtested engine uses (``scripts/multi_asset.simulate_outcome``).

Design validated against 4 production agents:
  * Freqtrade        — deterministic sizing (risk / stop-distance) + exit ladder
  * TradingAgents    — LLM trader/PM emits direction + conviction (our org already does)
  * Thiny-AI         — policy gate (max position, allowlist, slippage)
  * NeuroBro         — setup = hypothesis w/ invalidation; structural stop; R:R>=2 gate;
                       crowded book (stretched OI/funding) -> size DOWN

Sizing is CONVICTION-SCALED (bounded): base risk per trade is modulated by the PM's
conviction tier within a tight band (0.5%-1.5%); the STOP DISTANCE then sets the
notional; crowding only ever SIZES DOWN. The LLM judges direction; the code computes
the numbers; the gate enforces safety — an honest, reproducible boundary.
"""
from __future__ import annotations

# ── constants (mirror scripts/multi_asset.py — the backtested engine is the source of truth) ──
SL_MULT = 1.0          # baseline stop = 1.0 x ATR (and the floor of the structural-stop clamp)
SWAP_FEE = 0.0010      # per-side execution cost; round-trip = 2 x SWAP_FEE (reported as a caveat)
BASE_RISK = 0.01       # 1% base risk per trade (= multi_asset.RISK_PER_TRADE)

# Conviction tier -> risk fraction (SAFE-mode band). User policy: conviction-scaled, BOUNDED.
CONVICTION_RISK = {"low": 0.005, "medium": 0.010, "high": 0.015}

# ── Dynamic aggression — preset "Berani terukur" (measured-brave) ──────────────────────────────
# SAFE mode = the conviction band above. AGGRESSIVE mode = fractional-Kelly on a MEASURED, OOS-
# validated edge. Aggression is EARNED by data, never by mood; a drawdown breaker forces SAFE.
# Big NOTIONAL (leverage) can happen even in SAFE via a tight stop — that is NOT aggression; the
# aggression dial is risk% (capped at RISK_MAX) and it only lifts when a validated edge exists.
KELLY_FRACTION = 0.25       # quarter-Kelly (full Kelly = ruin-prone)
RISK_MAX = 0.03             # 3% max risk/trade ceiling (only reachable with a validated edge)
RISK_MIN = 0.0025           # 0.25% floor
LEVERAGE_CAP = 5.0          # notional <= 5x equity (perp; stop must sit inside liquidation distance)
DD_BREAKER = 0.20           # equity drawdown > 20% -> force SAFE mode until recovery
MIN_EDGE_SAMPLE = 20        # need >= 20 resolved/backtested trades before trusting Kelly p_win/payoff

# TP ladder + structure params
TP1_RR_FLOOR = 2.0     # measured TP1 R:R when there's no overhead structure (breakout)
TP1_SIZE_PCT = 60      # take 60% at TP1, let 40% run to TP2
SWING_LOOKBACK = 20    # bars used for the structural swing low / high
STOP_BUFFER_ATR = 0.15 # stop sits just BEYOND the structural level by this ATR buffer
SL_DIST_MIN_ATR = 0.6  # clamp stop distance to a sane band (keeps sizing stable + honest)
SL_DIST_MAX_ATR = 2.5

# Gate params (Layer 3)
MIN_RR_GATE = 2.0      # hard: R:R to the first meaningful target must be >= 2.0 (NeuroBro)
MAX_POSITION_PCT = LEVERAGE_CAP * 100.0  # notional cap as % of equity (perp leverage <= LEVERAGE_CAP x)


def _r(x: float, p: int = 6) -> float:
    return round(float(x), p)


def crowding_factor_from_score(flow_score: float | None, mode: str = "POSITIONING") -> tuple[float, str]:
    """Stretched positioning (a crowded one-sided book) -> size DOWN. POSITIONING-mode only.

    ``flow_score`` is the engine's contrarian score = 100 - buy_ratio*100, so 50 = balanced
    book and 0/100 = fully one-sided (crowded). A more crowded book risks a sharper reversal
    (NeuroBro), so we shrink the size modestly. Returns (factor in [0.7, 1.0], reason).
    """
    if mode != "POSITIONING" or flow_score is None:
        return 1.0, "no perp-positioning crowding signal (CONTRACT/spot asset)"
    extremity = min(1.0, abs(float(flow_score) - 50.0) / 50.0)  # 0 balanced .. 1 one-sided
    factor = round(1.0 - 0.30 * extremity, 3)                   # modest: down to 0.70 at full extreme
    why = (f"book {'crowded' if extremity > 0.5 else 'mild'} (score {flow_score:.0f}, "
           f"extremity {extremity:.2f}) -> size x{factor}")
    return factor, why


def build_levels(direction: str, entry: float, atr: float, dynamic_rr: float,
                 swing_low: float, swing_high: float) -> dict:
    """Pure price math: entry zone, invalidation, structural stop, TP ladder, R:R per TP."""
    sign = 1 if direction.upper() == "LONG" else -1
    invalidation = swing_low if sign > 0 else swing_high   # structure the thesis leans on
    target_struct = swing_high if sign > 0 else swing_low   # natural profit objective (range edge)

    # Stop just BEYOND the structural level, clamped to a sane distance band.
    raw_stop = invalidation - sign * STOP_BUFFER_ATR * atr
    raw_dist = abs(entry - raw_stop)
    lo, hi = SL_DIST_MIN_ATR * atr, SL_DIST_MAX_ATR * atr
    sl_dist = min(max(raw_dist, lo), hi)
    stop_basis = "structural" if lo <= raw_dist <= hi else ("clamped_min" if raw_dist < lo else "clamped_max")
    stop = entry - sign * sl_dist

    # Entry zone: a real range to scale into (NeuroBro: never a single tick).
    ez = 0.25 * atr
    entry_lo, entry_hi = entry - ez, entry + ez

    # TP1: the structural target if it lies AHEAD of entry; else a measured 2R (breakout).
    ahead = (target_struct - entry) * sign
    notes: list[str] = []
    if ahead > 0.10 * atr:
        tp1, tp1_src = target_struct, "structural (range boundary)"
    else:
        tp1, tp1_src = entry + sign * TP1_RR_FLOOR * sl_dist, "measured 2R (breakout / no overhead structure)"
        notes.append("no_overhead_structure")
    rr1 = abs(tp1 - entry) / sl_dist
    # TP2: a runner beyond structure, at the dynamic R:R (and always past TP1).
    rr2 = max(dynamic_rr, rr1 + 0.5, 2.5)
    tp2 = entry + sign * rr2 * sl_dist

    return {
        "entry": _r(entry),
        "entry_range": [_r(min(entry_lo, entry_hi)), _r(max(entry_lo, entry_hi))],
        "invalidation": _r(invalidation),
        "stop_loss": _r(stop),
        "stop_distance_atr": round(sl_dist / atr, 2),
        "stop_basis": stop_basis,
        "take_profit": [
            {"label": "TP1", "price": _r(tp1), "rr": round(rr1, 2), "close_pct": TP1_SIZE_PCT, "basis": tp1_src},
            {"label": "TP2", "price": _r(tp2), "rr": round(rr2, 2), "close_pct": 100 - TP1_SIZE_PCT, "basis": "runner @ dynamic R:R"},
        ],
        "level_notes": notes,
        "_sl_dist": sl_dist, "_sign": sign,
    }


def size_position(entry: float, sl_dist: float, confidence: str, equity: float,
                  crowding_factor: float = 1.0, *, edge: dict | None = None,
                  regime_conf: float = 1.0, consensus: float = 1.0, drawdown: float = 0.0) -> dict:
    """Dynamic SAFE/AGGRESSIVE sizing. notional = risk$ * (entry/sl_dist) (the backtested engine's
    formula). AGGRESSIVE = fractional-Kelly, unlocked ONLY by a MEASURED, OOS-validated edge with
    enough sample; otherwise SAFE = the bounded conviction band. A drawdown breaker forces SAFE.
    Aggression is earned by data, never by mood."""
    edge = edge or {}
    validated = bool(edge.get("validated")) and int(edge.get("sample_n", 0) or 0) >= MIN_EDGE_SAMPLE
    breaker = drawdown >= DD_BREAKER
    f_star = None
    if validated and not breaker:
        p = min(1.0, max(0.0, float(edge.get("p_win", 0.0) or 0.0)))
        b = max(1e-6, float(edge.get("payoff_b", 0.0) or 0.0))
        f_star = max(0.0, p - (1.0 - p) / b)                       # Kelly fraction (clamped >= 0)
        risk_pct = KELLY_FRACTION * f_star * max(0.0, min(1.0, regime_conf)) * max(0.0, min(1.0, consensus))
        risk_pct = min(max(risk_pct, RISK_MIN), RISK_MAX)
        mode = "AGGRESSIVE"
    else:
        risk_pct = CONVICTION_RISK.get(str(confidence).lower(), BASE_RISK)
        mode = "SAFE/dd-breaker" if breaker else "SAFE"
    risk_pct *= max(0.0, min(1.0, crowding_factor))                # crowding sizes DOWN only
    risk_usd = equity * risk_pct
    raw_notional = risk_usd * (entry / sl_dist)
    cap = equity * LEVERAGE_CAP
    notional = min(raw_notional, cap)
    effective_risk_usd = notional * (sl_dist / entry)   # actual $ at risk to the stop (< risk_usd when cap binds)
    return {
        "mode": mode,
        "risk_pct": round(risk_pct * 100, 3),
        "effective_risk_pct": round(effective_risk_usd / equity * 100, 3),
        "risk_usd": _r(risk_usd, 2),
        "notional_usd": _r(notional, 2),
        "position_size_pct": round(notional / equity * 100, 1),
        "leverage": round(notional / equity, 2),
        "kelly_fraction_star": round(f_star, 3) if f_star is not None else None,
        "crowding_factor": round(crowding_factor, 2),
        "size_capped": raw_notional > cap,
    }


def validate_ticket(ticket: dict, *, allowlist: list[str] | None = None,
                    max_position_pct: float = MAX_POSITION_PCT) -> tuple[bool, list[str]]:
    """Layer 3 gate. Returns (ok, problems). Mirrors NeuroBro's mandatory checks +
    Thiny's policy caps. A failing ticket must be repaired or the trade ABSTAINED."""
    p: list[str] = []
    d = str(ticket.get("direction", "")).upper()
    entry, stop, inval = ticket.get("entry"), ticket.get("stop_loss"), ticket.get("invalidation")
    tps = ticket.get("take_profit") or []

    rr1 = tps[0]["rr"] if tps else 0.0
    if rr1 < MIN_RR_GATE:
        p.append(f"rr_to_tp1={rr1}<{MIN_RR_GATE}")          # don't trade into resistance
    if inval is None:
        p.append("missing_invalidation")                    # no invalidation -> no setup
    elif d == "LONG" and not (stop < entry and inval <= entry):
        p.append("long_levels_inconsistent")
    elif d == "SHORT" and not (stop > entry and inval >= entry):
        p.append("short_levels_inconsistent")
    sd = ticket.get("stop_distance_atr", 0.0)
    if not (SL_DIST_MIN_ATR - 1e-6 <= sd <= SL_DIST_MAX_ATR + 1e-6):
        p.append(f"stop_distance_atr={sd}_out_of_band")
    if ticket.get("position_size_pct", 0.0) > max_position_pct + 1e-6:
        p.append("position_exceeds_cap")
    if allowlist is not None and d in ("LONG", "SHORT"):
        if str(ticket.get("ticker", "")).upper() not in {a.upper() for a in allowlist}:
            p.append("ticker_not_in_allowlist")
    return (len(p) == 0, p)


def build_trade_ticket(ticker: str, direction: str, confidence: str, *, last_price: float,
                       atr: float, dynamic_rr: float, swing_low: float, swing_high: float,
                       equity: float = 1000.0, crowding_factor: float = 1.0,
                       crowding_reason: str = "", allowlist: list[str] | None = None,
                       edge: dict | None = None, regime_conf: float = 1.0,
                       consensus: float = 1.0, drawdown: float = 0.0) -> dict:
    """Assemble + gate a full trade ticket from the PM's (direction, conviction) + real structure.
    `edge` (validated/p_win/payoff_b/sample_n) drives SAFE-vs-AGGRESSIVE sizing."""
    if atr is None or atr <= 0:
        return {"ticker": ticker.upper(), "direction": direction.upper(), "valid": False,
                "problems": ["atr<=0 (cannot size/level)"], "trade_ticket": None}
    lv = build_levels(direction, last_price, atr, dynamic_rr, swing_low, swing_high)
    sz = size_position(last_price, lv["_sl_dist"], confidence, equity, crowding_factor,
                       edge=edge, regime_conf=regime_conf, consensus=consensus, drawdown=drawdown)
    ticket = {
        "ticker": ticker.upper(),
        "direction": direction.upper(),
        "conviction": str(confidence).lower(),
        **{k: v for k, v in lv.items() if not k.startswith("_")},
        **sz,
        "edge_validated": bool((edge or {}).get("validated")),
        "crowding_reason": crowding_reason,
        "round_trip_fee_pct": round(SWAP_FEE * 2 * 100, 3),
        "framing": "A hypothesis with a defined invalidation — NOT a prediction. Levels from real ATR + "
                   "swing structure; size = SAFE conviction band, or AGGRESSIVE fractional-Kelly only when "
                   "an OOS-validated edge applies.",
    }
    ok, problems = validate_ticket(ticket, allowlist=allowlist)
    ticket["valid"] = ok
    ticket["problems"] = problems
    return ticket


# ─────────────────────────────── illustrative CLI demo ───────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    import pandas as pd

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    ROOT = Path(__file__).resolve().parents[1]
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "mnt").lower()
    df = pd.read_csv(ROOT / "data" / f"{ticker}_features.csv").dropna().reset_index(drop=True)
    last = df.iloc[-1]
    entry, atr = float(last["close"]), float(last["atr"])
    # dynamic R:R 2.0-3.0 from ADX strength (mirrors organization._state — no LLM needed)
    adx_th, adx_hi, adx = df["adx"].quantile(0.60), df["adx"].quantile(0.90), float(last["adx"])
    rr = 2.0 + max(0.0, min(1.0, (adx - adx_th) / (adx_hi - adx_th + 1e-9)))
    tail = df.tail(SWING_LOOKBACK)
    swing_low, swing_high = float(tail["low"].min()), float(tail["high"].max())

    allow = [ticker, "MNT", "WMNT", "METH", "CMETH", "USDE", "FBTC", "BTC", "ETH", "SOL"]
    print(f"\n=== ILLUSTRATIVE trade-ticket on REAL {ticker.upper()} data "
          f"(entry={entry:.6g}, ATR={atr:.6g}, dyn R:R={rr:.2f}) ===")
    print("(Conditional on an ENTER from the PM — the org ABSTAINS when no validated edge applies.)\n")

    def show(tag, t):
        verdict = "VALID" if t.get("valid") else f"REJECTED {t.get('problems')}"
        print(f"[{tag}] {verdict}")
        if "take_profit" in t:
            tp = t["take_profit"]
            print(f"   entry {t['entry_range']}  stop {t['stop_loss']} ({t['stop_basis']} "
                  f"{t['stop_distance_atr']}xATR)  inval {t['invalidation']}")
            print(f"   TP1 {tp[0]['price']} (R:R {tp[0]['rr']})  TP2 {tp[1]['price']} (R:R {tp[1]['rr']})")
            kelly = f"  Kelly f*={t['kelly_fraction_star']}" if t.get("kelly_fraction_star") else ""
            print(f"   {t['mode']}: risk {t['risk_pct']}% (${t['risk_usd']}) -> notional ${t['notional_usd']} "
                  f"({t['position_size_pct']}% eq, {t['leverage']}x lev){kelly}\n")

    # SAFE (today's reality: no OOS-validated edge -> conviction band)
    for direction in ("LONG", "SHORT"):
        show(f"{direction} · SAFE conf=high", build_trade_ticket(
            ticker, direction, "high", last_price=entry, atr=atr, dynamic_rr=rr,
            swing_low=swing_low, swing_high=swing_high, equity=1000.0, allowlist=allow))
    # AGGRESSIVE — use the asset's REAL OOS-validated edge if one exists (else a labeled hypothetical)
    import json
    real_edge = {}
    try:
        real_edge = json.loads((ROOT / "data" / "validated_edges.json").read_text()).get(ticker.upper(), {})
    except (FileNotFoundError, ValueError):
        pass
    if real_edge.get("validated"):
        print(f"--- {ticker.upper()} has an OOS-VALIDATED edge: {real_edge['edge']} "
              f"(REAL backtest p_win={real_edge['p_win']}, payoff_b={real_edge['payoff_b']}, n={real_edge['sample_n']}) ---")
        edge = real_edge
    else:
        print("--- IF an OOS-validated edge applied (HYPOTHETICAL p_win=0.62, payoff_b=1.8, n=40) ---")
        edge = {"validated": True, "p_win": 0.62, "payoff_b": 1.8, "sample_n": 40}
    show("LONG · AGGRESSIVE", build_trade_ticket(
        ticker, "LONG", "high", last_price=entry, atr=atr, dynamic_rr=rr,
        swing_low=swing_low, swing_high=swing_high, equity=1000.0, allowlist=allow,
        edge=edge, regime_conf=0.9, consensus=0.8))
