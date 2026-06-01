"""scripts/52_autonomous_loop.py — HeliQuant AUTONOMOUS LOOP.

The honest nevron-style cycle:
    RECALL  -> PLAN -> TICKET -> EXECUTE(record) -> LEARN(resolve) -> MEMORY -> REST

Modes:
  --replay TICKER [--bars N]   Deterministic, NO-LLM backtest of the loop over REAL history. Drives
                               gated trade-tickets + MemoryStore + lessons -> an HONEST ticket-gated
                               winrate, and populates the agent's memory + Obsidian vault.
                               (Illustrative regime-structural signal — NOT a validated-alpha claim;
                               we EXPECT trend-following to underperform, and we report it honestly.)
  --once TICKER                One LIVE tick: recall memory -> run the real LLM org -> ticket ->
                               record to memory + vault. (needs an LLM provider configured)
  --interval SEC --ticker T    Always-on live loop (Ctrl-C to stop).

Execution here = paper ledger + memory record + Obsidian note (NO risky live swap). The on-chain
record (Mantle TradingVault) and Bybit-testnet order are the two execution lines, wired separately.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402

from firm import trade_ticket as tt  # noqa: E402
from firm.memory_store import MemoryStore  # noqa: E402

# multi_asset lives in scripts/ (not a package) — load it directly
_spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)  # type: ignore[union-attr]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _resolve(window: pd.DataFrame, sign: int, tp: float, sl: float) -> tuple[str, float]:
    """Did TP or SL get hit first in the lookahead window? (whichever bar comes first wins)."""
    if sign > 0:
        hit_tp, hit_sl = window[window["high"] >= tp], window[window["low"] <= sl]
    else:
        hit_tp, hit_sl = window[window["low"] <= tp], window[window["high"] >= sl]
    if len(hit_tp) and len(hit_sl):
        return ("TP", tp) if hit_tp.index[0] < hit_sl.index[0] else ("SL", sl)
    if len(hit_tp):
        return "TP", tp
    if len(hit_sl):
        return "SL", sl
    return "TIMEOUT", float(window["close"].iloc[-1])


# ─────────────────────────────── REPLAY (deterministic, no-LLM) ───────────────────────────────
def replay(ticker: str, bars: int | None = None) -> None:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    la = ma.LOOKAHEAD
    if bars:
        df = df.tail(bars + la + 40).reset_index(drop=True)
    adx_th = float(df["adx"].quantile(0.60))
    vol_th = float(df["volatility_10"].quantile(0.85))
    adx_hi = float(df["adx"].quantile(0.90))

    db = ROOT / "data" / f"replay_{ticker.lower()}_memory.db"
    if db.exists():
        db.unlink()  # fresh replay each run
    mem = MemoryStore(db_path=db, vault=None)  # replay: DB only; live --once writes Obsidian vault notes

    equity = 1000.0
    n_abstain = n_gated = n_enter = 0
    i, cooldown = 30, -1
    print(f"\n=== AUTONOMOUS LOOP · REPLAY {ticker.upper()} "
          f"({len(df)} bars, lookahead {la}h) — deterministic, no-LLM ===")
    print("(Illustrative regime-structural signal through the REAL ticket gates + memory. "
          "Honest expectation: trend-following has no validated edge.)\n")

    while i < len(df) - la:
        if i <= cooldown:
            i += 1
            continue
        row = df.iloc[i]
        regime = ma.detect_current(row, adx_th, vol_th)
        direction = {"Trending_Up": "LONG", "Trending_Down": "SHORT"}.get(regime)
        if direction is None:        # only trade trends in this illustrative replay
            n_abstain += 1
            i += 1
            continue
        atr, entry = float(row["atr"]), float(row["close"])
        if atr <= 0:
            i += 1
            continue
        rr = 2.0 + max(0.0, min(1.0, (float(row["adx"]) - adx_th) / (adx_hi - adx_th + 1e-9)))
        tail = df.iloc[max(0, i - tt.SWING_LOOKBACK):i + 1]
        swing_low, swing_high = float(tail["low"].min()), float(tail["high"].max())
        conf = "high" if float(row["adx"]) > adx_hi else "medium"
        ticket = tt.build_trade_ticket(ticker, direction, conf, last_price=entry, atr=atr,
                                       dynamic_rr=rr, swing_low=swing_low, swing_high=swing_high,
                                       equity=equity, allowlist=[ticker.upper()])
        if not ticket["valid"]:
            n_gated += 1            # ticket gate refused (e.g. R:R<2) — disciplined skip
            i += 1
            continue
        n_enter += 1
        rid = mem.record_decision(f"bar{i}", ticker, regime,
                                  {"decision": "ENTER", "direction": direction, "confidence": conf,
                                   "reasoning": f"{regime} structural ticket", "trade_ticket": ticket})
        sign = 1 if direction == "LONG" else -1
        outcome, exitp = _resolve(df.iloc[i + 1:i + la + 1], sign,
                                  ticket["take_profit"][0]["price"], ticket["stop_loss"])
        pnl_pct = ((exitp - entry) / entry * 100) if sign > 0 else ((entry - exitp) / entry * 100)
        notional = ticket["notional_usd"]
        equity += notional * (pnl_pct / 100) - notional * tt.SWAP_FEE * 2
        mem.record_outcome(rid, outcome, round(pnl_pct, 3), round(exitp, 6), f"bar{i + la}")
        cooldown = i + la
        i += 1
        if equity <= 0:
            print("  !! equity wiped — stopping replay")
            break

    st = mem.stats()
    print(f"decisions: {n_enter} ENTER (ticketed) · {n_gated} gate-rejected · {n_abstain} non-trend ABSTAIN")
    print(f"resolved: {st['resolved']} · ticket-gated WINRATE: {st['resolved_winrate']}%")
    print(f"paper equity: $1000.00 -> ${equity:.2f} ({(equity / 1000 - 1) * 100:+.2f}%)")
    print(f"\nmemory brief (what the PM would recall next):\n  {mem.brief(ticker)}")
    print(f"\nmemory DB: {db}  (live --once mode writes browsable Obsidian notes to knowledge/)")
    print("\nNOTE: this is the LOOP + MEMORY proven on real data. The winrate above is an illustrative "
          "trend-structural signal — consistent with our honest finding that trend-following lacks a "
          "validated edge. AGGRESSIVE sizing stays locked until a strategy survives OOS.")
    mem.close()


# ─────────────────────────────── LIVE tick (real LLM org) ───────────────────────────────
def once(ticker: str, mem: MemoryStore | None = None) -> dict:
    from firm import organization as org

    own = mem is None
    mem = mem or MemoryStore()
    try:
        df, last, regime, *_ = org._state(ticker)
    except Exception:
        regime = ""
    brief = mem.brief(ticker, regime or None)
    print(f"\n[RECALL] {brief}\n")
    res = org.run_organization(ticker, verbose=True, memory_brief=brief)
    dec = res.get("decision", {})
    rid = mem.record_decision(_now(), ticker, regime, dec)
    print(f"\n[EXECUTE] recorded decision id={rid}: {dec.get('decision')} {dec.get('direction', '')}")
    print(f"[MEMORY] {mem.stats()}")
    if own:
        mem.close()
    return dec


def loop(ticker: str, interval: int) -> None:
    print(f"=== AUTONOMOUS LOOP · LIVE every {interval}s on {ticker.upper()} (Ctrl-C to stop) ===")
    mem = MemoryStore()
    try:
        while True:
            once(ticker, mem=mem)
            print(f"\n[REST] sleeping {interval}s...\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        mem.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HeliQuant autonomous loop")
    ap.add_argument("--replay", metavar="TICKER", help="deterministic no-LLM backtest of the loop")
    ap.add_argument("--once", metavar="TICKER", help="one live tick (real LLM org)")
    ap.add_argument("--interval", type=int, help="always-on live loop, seconds between ticks")
    ap.add_argument("--ticker", default="mnt", help="ticker for --interval mode")
    ap.add_argument("--bars", type=int, help="limit replay to the last N bars")
    args = ap.parse_args()
    if args.replay:
        replay(args.replay, args.bars)
    elif args.once:
        once(args.once)
    elif args.interval:
        loop(args.ticker, args.interval)
    else:
        print("usage: --replay TICKER [--bars N] | --once TICKER | --interval SEC --ticker T")
