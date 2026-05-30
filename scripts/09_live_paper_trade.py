"""LIVE forward paper-trading — run MANTIS Signal Agent every N minutes on real
market data and log each decision + hypothetical PnL.

How it works:
  - Every TICK_MINUTES: call Signal Agent (real MNT + real Allora + real sources)
  - If signal is BUY/SELL: open paper position at current MNT spot
  - Track open position vs live price; close on SL/TP hit OR after MAX_HOLD_BARS
  - Persist every tick + every closed trade to JSONL for later analysis
  - Print live trace so you can watch it work

Usage:
    python scripts/09_live_paper_trade.py --minutes 60         # 60-min session
    python scripts/09_live_paper_trade.py --minutes 240 --tick 5

Outputs:
    data/live_ticks.jsonl    one line per tick (decision + reasoning + price)
    data/live_trades.jsonl   one line per closed trade
    data/live_summary.json   running stats: win rate, PnL, equity
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env before importing firm modules
env_file = ROOT.parent / ".env"
if env_file.exists():
    for ln in env_file.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from firm.agents.signal import run as run_signal  # noqa: E402
from firm.schemas import SignalInput, TradeDirection  # noqa: E402

TICKS_OUT = ROOT / "data" / "live_ticks.jsonl"
TRADES_OUT = ROOT / "data" / "live_trades.jsonl"
SUMMARY_OUT = ROOT / "data" / "live_summary.json"

INITIAL_EQUITY = 1_000.0
RISK_PER_TRADE = 0.01
SWAP_FEE = 0.0010
SL_ATR_MULT = 1.0
TP_ATR_MULT = 1.46
MAX_HOLD_TICKS = 96     # safety: close after this many ticks regardless

COINGECKO_PRICE = "https://api.coingecko.com/api/v3/simple/price"


@dataclass
class OpenPosition:
    opened_at: str
    direction: str            # "BUY" | "SELL"
    entry: float
    stop_loss: float
    take_profit: float
    notional: float
    bars_open: int = 0
    forward_conf: float | None = None
    regime: str | None = None


@dataclass
class Session:
    started_at: str
    tick_minutes: int
    equity: float = INITIAL_EQUITY
    trades: int = 0
    wins: int = 0
    losses: int = 0
    closed_pnl: float = 0.0
    open_position: OpenPosition | None = None
    last_price: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


def fetch_spot_mnt_usd() -> float:
    r = requests.get(
        COINGECKO_PRICE,
        params={"ids": "mantle", "vs_currencies": "usd"},
        timeout=10,
        headers={"User-Agent": "mantle-mantis/0.1"},
    )
    r.raise_for_status()
    return float(r.json()["mantle"]["usd"])


def fetch_atr_proxy() -> float:
    """Pull last few prices and use simple rolling range as ATR proxy."""
    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/mantle/market_chart",
        params={"vs_currency": "usd", "days": "1"},
        timeout=10,
        headers={"User-Agent": "mantle-mantis/0.1"},
    )
    r.raise_for_status()
    prices = [float(p) for _, p in r.json()["prices"][-24:]]
    if len(prices) < 2:
        return 0.005
    return max(prices) - min(prices)


async def maybe_decide(now_iso: str) -> dict[str, Any]:
    output = await run_signal(SignalInput(pair="MNT/USDC"))
    return {
        "tick_at": now_iso,
        "direction": output.direction.value,
        "confidence": float(output.confidence),
        "allora_value": output.allora_value,
        "ml_buy": output.ml_buy_prob,
        "ml_sell": output.ml_sell_prob,
        "ml_adx": output.ml_adx,
        "reasoning": output.reasoning,
    }


def open_position(session: Session, decision: dict[str, Any], spot: float, atr: float) -> None:
    direction = decision["direction"]
    if direction == "BUY":
        sl = spot - atr * SL_ATR_MULT
        tp = spot + atr * TP_ATR_MULT
    else:
        sl = spot + atr * SL_ATR_MULT
        tp = spot - atr * TP_ATR_MULT

    notional = (session.equity * RISK_PER_TRADE) * (spot / max(atr * SL_ATR_MULT, 1e-9))
    notional = min(notional, session.equity * 0.95)

    session.open_position = OpenPosition(
        opened_at=decision["tick_at"],
        direction=direction,
        entry=spot,
        stop_loss=sl,
        take_profit=tp,
        notional=notional,
        forward_conf=decision.get("confidence"),
        regime=str(decision.get("reasoning"))[:120],
    )
    print(
        f"  >>> OPEN {direction} @ ${spot:.4f}  SL=${sl:.4f}  TP=${tp:.4f}  "
        f"notional=${notional:.2f}"
    )


def maybe_close_position(session: Session, spot: float, now_iso: str) -> dict[str, Any] | None:
    pos = session.open_position
    if pos is None:
        return None
    pos.bars_open += 1

    outcome = None
    exit_price = spot
    if pos.direction == "BUY":
        if spot >= pos.take_profit:
            outcome, exit_price = "TP", pos.take_profit
        elif spot <= pos.stop_loss:
            outcome, exit_price = "SL", pos.stop_loss
    else:
        if spot <= pos.take_profit:
            outcome, exit_price = "TP", pos.take_profit
        elif spot >= pos.stop_loss:
            outcome, exit_price = "SL", pos.stop_loss

    if outcome is None and pos.bars_open >= MAX_HOLD_TICKS:
        outcome = "TIMEOUT"

    if outcome is None:
        return None  # still open

    if pos.direction == "BUY":
        pnl_pct = (exit_price - pos.entry) / pos.entry
    else:
        pnl_pct = (pos.entry - exit_price) / pos.entry

    gross = pos.notional * pnl_pct
    fee = pos.notional * SWAP_FEE * 2
    net = gross - fee
    session.equity += net
    session.trades += 1
    if net > 0:
        session.wins += 1
    else:
        session.losses += 1
    session.closed_pnl += net

    closed = {
        "opened_at": pos.opened_at,
        "closed_at": now_iso,
        "direction": pos.direction,
        "entry": pos.entry,
        "exit": exit_price,
        "outcome": outcome,
        "notional": pos.notional,
        "net_pnl": net,
        "bars_open": pos.bars_open,
        "regime": pos.regime,
        "equity_after": session.equity,
    }
    session.open_position = None
    print(f"  <<< CLOSE {outcome}  PnL=${net:+.2f}  equity=${session.equity:.2f}")
    return closed


def persist_summary(session: Session) -> None:
    TICKS_OUT.parent.mkdir(parents=True, exist_ok=True)
    win_rate = session.wins / session.trades if session.trades else 0.0
    SUMMARY_OUT.write_text(json.dumps({
        "started_at": session.started_at,
        "tick_minutes": session.tick_minutes,
        "equity": session.equity,
        "initial_equity": INITIAL_EQUITY,
        "roi_pct": (session.equity / INITIAL_EQUITY - 1.0) * 100.0,
        "closed_trades": session.trades,
        "wins": session.wins,
        "losses": session.losses,
        "win_rate": win_rate,
        "closed_pnl_usd": session.closed_pnl,
        "open_position": asdict(session.open_position) if session.open_position else None,
    }, indent=2))


async def main_loop(total_minutes: int, tick_minutes: int) -> None:
    session = Session(
        started_at=datetime.now(timezone.utc).isoformat(),
        tick_minutes=tick_minutes,
    )
    end_at = time.time() + total_minutes * 60
    tick_idx = 0

    TICKS_OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"=== MANTIS Live Paper-Trading ===")
    print(f"  duration: {total_minutes} min")
    print(f"  tick:     {tick_minutes} min")
    print(f"  outputs:  {TICKS_OUT.name}, {TRADES_OUT.name}, {SUMMARY_OUT.name}")
    print()

    while time.time() < end_at:
        tick_idx += 1
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        print(f"[Tick {tick_idx} @ {now.strftime('%Y-%m-%d %H:%M:%S')} UTC]")

        try:
            spot = fetch_spot_mnt_usd()
            session.last_price = spot
            print(f"  MNT spot: ${spot:.4f}")
        except Exception as e:  # noqa: BLE001
            print(f"  spot fetch FAILED: {e}")
            await asyncio.sleep(tick_minutes * 60)
            continue

        closed = maybe_close_position(session, spot, now_iso)
        if closed is not None:
            with TRADES_OUT.open("a") as f:
                f.write(json.dumps(closed) + "\n")

        decision = await maybe_decide(now_iso)
        decision["spot"] = spot
        with TICKS_OUT.open("a") as f:
            f.write(json.dumps(decision) + "\n")
        print(f"  decision: {decision['direction']} (conf={decision['confidence']:.2f})")
        print(f"  reason:   {decision['reasoning'][:140]}{'...' if len(decision['reasoning']) > 140 else ''}")

        if session.open_position is None and decision["direction"] in ("BUY", "SELL"):
            try:
                atr = fetch_atr_proxy()
                if atr > 0:
                    open_position(session, decision, spot, atr)
            except Exception as e:  # noqa: BLE001
                print(f"  atr fetch FAILED: {e}")

        persist_summary(session)
        print(f"  session: trades={session.trades} wins={session.wins} "
              f"losses={session.losses} equity=${session.equity:.2f}")
        print()

        if time.time() >= end_at:
            break
        await asyncio.sleep(tick_minutes * 60)

    print("=== Session ended ===")
    print(f"  total ticks:   {tick_idx}")
    print(f"  closed trades: {session.trades}")
    print(f"  wins/losses:   {session.wins}/{session.losses}")
    print(f"  final equity:  ${session.equity:.2f}  (ROI {(session.equity/INITIAL_EQUITY-1)*100:+.2f}%)")
    if session.open_position is not None:
        print(f"  open at end:   {session.open_position.direction} @ ${session.open_position.entry:.4f}")
    persist_summary(session)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=30, help="total session length in minutes")
    parser.add_argument("--tick", type=int, default=5, help="tick interval in minutes")
    args = parser.parse_args()
    asyncio.run(main_loop(args.minutes, args.tick))
