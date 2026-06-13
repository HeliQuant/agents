"""Learning #1 — MFE/MAE-based TP/SL calibration.

Diagnoses the "1 TP in 128 trades" problem honestly: for every closed campaign trade it measures how far
price ACTUALLY travelled in our favour (Maximum Favourable Excursion) and against us (Maximum Adverse
Excursion) *within the real hold window*, expressed in units of the entry 1h-ATR (the same unit the
campaign sizes TP/SL in). If the median MFE is well below the current TP multiplier (2.5x ATR), the TP is
simply unreachable in the window — that's why wins are rare. From the realized excursions it suggests a
per-asset TP/SL that trades can actually reach.

MEASURE FIRST. This module only computes + writes the calibration; wiring it into the campaign is a
separate, deliberate step taken only after the numbers justify it.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CALIB = ROOT / "data" / "tp_sl_calibration.json"
BYBIT = "https://api.bybit.com"
CUR_TP_MULT = 2.5   # what the campaign uses today (ATR_TP_MULT) — the bar we test reachability against
CUR_SL_MULT = 1.8   # ATR_SL_MULT


def _to_ms(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def _klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Bybit linear kline in [start,end] -> chronological list of [start,o,h,l,c,vol,turnover]."""
    try:
        r = requests.get(f"{BYBIT}/v5/market/kline",
                         params={"category": "linear", "symbol": f"{symbol}USDT", "interval": interval,
                                 "start": start_ms, "end": end_ms, "limit": 1000}, timeout=15)
        return list(reversed((r.json().get("result") or {}).get("list", [])))  # API returns newest-first
    except Exception:  # noqa: BLE001
        return []


def _atr_pct_at(symbol: str, end_ms: int) -> float | None:
    """ATR(14) on 1h candles ending at end_ms, as a fraction of the last close — matches campaign._atr_pct."""
    rows = _klines(symbol, "60", end_ms - 60 * 3600 * 1000, end_ms)
    if len(rows) < 16:
        return None
    highs, lows, closes = ([float(x[i]) for x in rows] for i in (2, 3, 4))
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(rows))]
    if len(trs) < 14 or closes[-1] <= 0:
        return None
    return (sum(trs[-14:]) / 14) / closes[-1]


def excursion(symbol: str, entry: float, side: str, start_ms: int, end_ms: int) -> tuple | None:
    """(MFE%, MAE%) over the hold window from 15m candles — the real best/worst the trade saw."""
    rows = _klines(symbol, "15", start_ms, end_ms + 15 * 60 * 1000)
    if not rows or entry <= 0:
        return None
    hi = max(float(x[2]) for x in rows)
    lo = min(float(x[3]) for x in rows)
    if side.upper() == "LONG":
        return (hi / entry - 1.0, 1.0 - lo / entry, len(rows))
    return (1.0 - lo / entry, hi / entry - 1.0, len(rows))  # SHORT


def calibrate(trades: list, verbose: bool = True) -> dict:
    """Per-asset MFE/MAE stats (in ATR units) + suggested reachable TP/SL multipliers."""
    by: dict[str, list] = {}
    for t in trades:
        a = (t.get("asset") or "").upper()
        entry, side = t.get("entry"), t.get("dir")
        s_ms, e_ms = _to_ms(t.get("utc_open") or ""), _to_ms(t.get("utc_close") or "")
        if not (a and entry and side and s_ms and e_ms) or e_ms <= s_ms:
            continue
        atrp = _atr_pct_at(a, s_ms)
        ex = excursion(a, float(entry), side, s_ms, e_ms)
        time.sleep(0.05)
        if not ex or not atrp or atrp <= 0:
            continue
        mfe, mae, _ = ex
        by.setdefault(a, []).append((mfe / atrp, mae / atrp))
    out: dict[str, dict] = {}
    for a, rows in by.items():
        if len(rows) < 5:
            continue
        mfes = sorted(r[0] for r in rows)
        maes = sorted(r[1] for r in rows)
        p60 = mfes[int(0.6 * (len(mfes) - 1))]
        out[a] = {
            "n": len(rows),
            "median_mfe_atr": round(statistics.median(mfes), 2),
            "median_mae_atr": round(statistics.median(maes), 2),
            "p60_mfe_atr": round(p60, 2),
            "pct_reached_cur_tp": round(sum(1 for m in mfes if m >= CUR_TP_MULT) / len(mfes), 2),
            "pct_would_hit_cur_sl": round(sum(1 for m in maes if m >= CUR_SL_MULT) / len(maes), 2),
            # a reachable TP (>=~half of trades can hit it) + an SL just beyond the typical adverse wiggle
            "suggested_tp_mult": max(0.8, round(p60, 1)),
            "suggested_sl_mult": max(0.8, round(statistics.median(maes) + 0.3, 1)),
        }
        if verbose:
            o = out[a]
            print(f"  {a:5} n={o['n']:3}  MFE med {o['median_mfe_atr']:.2f} / p60 {o['p60_mfe_atr']:.2f} ATR  "
                  f"MAE med {o['median_mae_atr']:.2f} ATR  reached 2.5xTP {o['pct_reached_cur_tp']*100:.0f}%  "
                  f"-> TP {o['suggested_tp_mult']}x / SL {o['suggested_sl_mult']}x")
    return out
