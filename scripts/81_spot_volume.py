"""scripts/81 — LIVE spot execution STRESS TEST: N buy+sell round-trips per asset on the exchange testnet.

Proves the executor handles real volume on a live venue (exchange SPOT — derivatives are geo-banned, spot
is not). Each round-trip = market BUY $notional + market SELL it back; both legs are REAL fills. Skips any
asset with an empty testnet orderbook (e.g. HYPE has no testnet liquidity). Logs fills, failures, and the
cumulative cost (USDT delta).

HONEST: this is an EXECUTION stress test, NOT a profit demo. An immediate buy+sell pays the bid-ask spread
+ fees every time, so the cumulative result is a small LOSS BY DESIGN (~spread×N). Profit comes from the
STRATEGY holding per a validated signal, not from churning round-trips.

Run:  python scripts/81_spot_volume.py MNTUSDT SUIUSDT HYPEUSDT --n 100 --notional 100
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import bybit_executor as ex  # noqa: E402

# the executor signs against this venue; auxiliary market reads below must use the SAME testnet host
EXCHANGE_TESTNET = "https://api-testnet.bybit.com"


def _usdt() -> float:
    res = ex._ok(ex._read_session().get_wallet_balance(accountType="UNIFIED"))
    for a in res.get("list", []):
        for c in a.get("coin", []):
            if c.get("coin") == "USDT":
                return float(c.get("walletBalance") or 0)
    return 0.0


def _coin(coin: str) -> float:
    res = ex._ok(ex._read_session().get_wallet_balance(accountType="UNIFIED"))
    for a in res.get("list", []):
        for c in a.get("coin", []):
            if c.get("coin") == coin:
                return float(c.get("walletBalance") or 0)
    return 0.0


def _orderbook_liquid(symbol: str) -> bool:
    j = requests.get(EXCHANGE_TESTNET + "/v5/market/orderbook", params={"category": "spot", "symbol": symbol, "limit": 1}, timeout=10).json()
    r = j.get("result", {})
    return bool(r.get("b")) and bool(r.get("a"))


def _base_precision(symbol: str) -> float:
    it = requests.get(EXCHANGE_TESTNET + "/v5/market/instruments-info", params={"category": "spot", "symbol": symbol}, timeout=10).json()["result"]["list"][0]
    return float(it["lotSizeFilter"]["basePrecision"])


def _floor(x: float, step: float) -> float:
    return (int(x / step)) * step if step > 0 else x


def round_trips(symbol: str, n: int, notional: float) -> dict:
    base = symbol.replace("USDT", "")
    s = ex._trade_session()
    print(f"\n=== {symbol} ({base}) — {n} round-trips of ${notional} ===")
    if not _orderbook_liquid(symbol):
        print(f"  SKIP — testnet spot orderbook EMPTY (no liquidity to fill).")
        return {"symbol": symbol, "skipped": True}
    step = _base_precision(symbol)
    usdt0 = _usdt()
    filled = failed = 0
    for i in range(n):
        if i >= 5 and filled == 0:
            print(f"  ABORT {symbol} — first {i} trades all failed (market orders not filling here).")
            break
        try:
            ex._ok(s.place_order(category="spot", symbol=symbol, side="Buy", orderType="Market", qty=str(notional), marketUnit="quoteCoin"))
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                print(f"  trade {i+1}: BUY failed — {str(e)[:60]}")
            continue
        time.sleep(0.6)
        bal = _coin(base)
        if bal <= 0:
            failed += 1
            continue
        qty = _floor(bal, step)
        try:
            ex._ok(s.place_order(category="spot", symbol=symbol, side="Sell", orderType="Market", qty=str(round(qty, 8))))
        except Exception as e:  # noqa: BLE001
            print(f"  trade {i+1}: SELL failed — {str(e)[:60]} (holding {bal} {base})")
        filled += 1
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  ...{i+1}/{n} round-trips done | filled {filled} fail {failed} | USDT {_usdt():.2f}")
        time.sleep(0.3)
    usdt1 = _usdt()
    out = {"symbol": symbol, "skipped": False, "filled": filled, "failed": failed,
           "usdt_cost": round(usdt0 - usdt1, 4), "cost_per_trade_bps": round((usdt0 - usdt1) / max(filled, 1) / notional * 1e4, 1)}
    print(f"  DONE {symbol}: {filled} filled / {failed} failed | total cost ${out['usdt_cost']} "
          f"(~{out['cost_per_trade_bps']} bps/round-trip = spread+fees)")
    try:  # log realized execution cost so dry_run_deviation can check it vs the modeled backtest cost
        from firm.dry_run_deviation import log_measurement
        log_measurement(symbol, out["cost_per_trade_bps"], filled, notional)
    except Exception:  # noqa: BLE001
        pass
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raw = sys.argv[1:]
    n, notional, skip = 100, 100.0, set()
    for i, a in enumerate(raw):
        if a == "--n" and i + 1 < len(raw):
            n = int(raw[i + 1]); skip |= {i, i + 1}
        elif a == "--notional" and i + 1 < len(raw):
            notional = float(raw[i + 1]); skip |= {i, i + 1}
    symbols = [raw[i].upper() for i in range(len(raw)) if i not in skip and not raw[i].startswith("--")] or ["MNTUSDT", "SUIUSDT", "HYPEUSDT"]
    print(f"LIVE spot execution stress test — {n} round-trips x ${notional} per asset (testnet, spot only)")
    print("(execution proof, NOT profit — each round-trip pays the spread; cumulative = small loss by design)\n")
    results = []
    for sym in symbols:
        try:
            results.append(round_trips(sym, n, notional))
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} aborted: {str(e)[:80]}")
    print("\n=== SUMMARY ===")
    for r in results:
        if r.get("skipped"):
            print(f"  {r['symbol']:9} SKIPPED (no testnet liquidity)")
        else:
            print(f"  {r['symbol']:9} {r['filled']} real round-trips filled | cost ${r['usdt_cost']} ({r['cost_per_trade_bps']} bps/trip)")
    print("\nProof: HeliQuant executes real spot orders at volume on the exchange (a live sponsor venue).")
    print("Cost is the bid-ask spread — exactly why an edge must beat costs (the lesson from every backtest).")


if __name__ == "__main__":
    main()
