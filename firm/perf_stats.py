"""Rigorous performance analytics for the campaign trade ledger — Sharpe, Sortino, Calmar, max
drawdown, profit factor, expectancy — computed from REAL resolved trades (per-trade return =
pnl_usd / size_usd). Pure numpy, so it adds ZERO deploy weight (no matplotlib/quantstats on Railway);
the full quantstats HTML tear-sheet is generated offline by scripts/96 as a pitch artifact, while this
powers the live /performance panel.

HONEST by construction: on a small sample the annualized figures are noisy, so `n` is always surfaced
and a `sample_warning` fires under 30 trades — nobody should read a Sharpe off 20 trades as gospel.
"""
import math
from datetime import datetime


def _span_days(resolved):
    """Calendar span of the ledger in days (the floor is event-based, not fixed-bar)."""
    try:
        ts = sorted(datetime.fromisoformat(t["utc_open"].replace("Z", "+00:00")).timestamp()
                    for t in resolved if t.get("utc_open"))
        span = ts[-1] - ts[0]
        return (span / 86400.0) if span > 0 else None
    except Exception:   # noqa: BLE001
        return None


def compute(trades):
    """quantstats-grade metrics from a list of trade dicts (needs pnl_usd + size_usd + utc_open)."""
    resolved = [t for t in trades if t.get("pnl_usd") is not None and t.get("size_usd")]
    rets = [t["pnl_usd"] / t["size_usd"] for t in resolved if t["size_usd"]]
    n = len(rets)
    if n == 0:
        return {"n": 0, "note": "no resolved trades yet"}
    import numpy as np
    r = np.array(rets, dtype=float)
    wins, losses = r[r > 0], r[r < 0]
    mean = float(r.mean())
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    dd_sd = float(losses.std(ddof=1)) if losses.size > 1 else 0.0
    eq = np.cumprod(1 + r)                       # compounded equity curve
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min())   # most-negative drawdown
    sharpe_pt = (mean / sd) if sd > 0 else 0.0
    sortino_pt = (mean / dd_sd) if dd_sd > 0 else 0.0
    # ANNUALIZE ONLY ON A MEANINGFUL SPAN. The floor trades every few minutes; over a 3-day window that
    # extrapolates to ~19k trades/yr, turning a small per-trade loss into a fantasy "-100% CAGR / -51
    # Sharpe". That's LESS honest, not more. So annualized figures are emitted only with >=30 days AND
    # >=30 trades; below that we report the per-trade stats (which need no extrapolation) and say why.
    span_days = _span_days(resolved)
    meaningful = bool(span_days and span_days >= 30 and n >= 30)
    ppy = (n / (span_days / 365.25)) if span_days else None
    sharpe_ann = sharpe_pt * math.sqrt(ppy) if (ppy and meaningful) else None
    sortino_ann = sortino_pt * math.sqrt(ppy) if (ppy and meaningful) else None
    cagr = (float(eq[-1]) ** (ppy / n) - 1) if (ppy and meaningful and eq[-1] > 0) else None
    calmar = (cagr / abs(max_dd)) if (cagr is not None and max_dd < 0) else None
    pf = (float(wins.sum()) / abs(float(losses.sum()))) if (losses.size and losses.sum() != 0) else None
    return {
        "n": n,
        "win_rate": round(100 * float((r > 0).mean()), 1),
        "total_return_pct": round(100 * float(eq[-1] - 1), 2),
        "mean_trade_pct": round(100 * mean, 3),
        "vol_trade_pct": round(100 * sd, 3),
        "sharpe_per_trade": round(sharpe_pt, 3),
        "sortino_per_trade": round(sortino_pt, 3),
        "sharpe_annualized": round(sharpe_ann, 2) if sharpe_ann is not None else None,
        "sortino_annualized": round(sortino_ann, 2) if sortino_ann is not None else None,
        "max_drawdown_pct": round(100 * max_dd, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "cagr_pct": round(100 * cagr, 1) if cagr is not None else None,
        "profit_factor": round(pf, 2) if pf is not None else None,
        "avg_win_pct": round(100 * float(wins.mean()), 3) if wins.size else None,
        "avg_loss_pct": round(100 * float(losses.mean()), 3) if losses.size else None,
        "best_pct": round(100 * float(r.max()), 2),
        "worst_pct": round(100 * float(r.min()), 2),
        "span_days": round(span_days, 1) if span_days else None,
        "trades_per_year": round(ppy) if (ppy and meaningful) else None,
        "annualized": meaningful,
        "equity_curve": [round(float(x), 4) for x in eq],
        "sample_warning": None if meaningful else
        f"sample too small to annualize honestly (span {round(span_days or 0,1)}d, n {n}; need >=30d & >=30 trades) - per-trade stats only",
    }
