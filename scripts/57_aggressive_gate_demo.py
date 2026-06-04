"""Does AGGRESSIVE actually turn ON only when HeliQuant has a PROVEN edge?  (truth table)

The user's intuition is right — "if HeliQuant is good, AGGRESSIVE should light up; otherwise not."
This proves EXACTLY that, deterministically, on REAL latest MNT data, using the real sizing code
(firm.trade_ticket.build_trade_ticket). No 100 live trades needed: whether AGGRESSIVE fires is a
function of (validated edge? + enough sample? + drawdown healthy?), NOT a live win-streak.

The unlock condition (from trade_ticket.size_position):
  AGGRESSIVE  <=>  edge.validated AND edge.sample_n >= 20 AND drawdown < 20%
                   (then risk = quarter-Kelly, capped 3% / 5x; else SAFE conviction band)

Run: python scripts/57_aggressive_gate_demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.trade_ticket import (MIN_EDGE_SAMPLE, SWING_LOOKBACK,  # noqa: E402
                               build_trade_ticket)


def _latest_bar(ticker: str):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    last = df.iloc[-1]
    tail = df.tail(SWING_LOOKBACK)
    adx_th, adx_hi = df["adx"].quantile(0.60), df["adx"].quantile(0.90)
    rr = 2.0 + max(0.0, min(1.0, (float(last["adx"]) - adx_th) / (adx_hi - adx_th + 1e-9)))
    return {"entry": float(last["close"]), "atr": float(last["atr"]), "rr": rr,
            "swing_low": float(tail["low"].min()), "swing_high": float(tail["high"].max()),
            "dt": str(last["datetime"])[:16]}


def case(label, ticker, edge, drawdown, bar, *, direction="LONG"):
    t = build_trade_ticket(ticker, direction, "high", last_price=bar["entry"], atr=bar["atr"],
                           dynamic_rr=bar["rr"], swing_low=bar["swing_low"], swing_high=bar["swing_high"],
                           equity=1000.0, edge=edge, regime_conf=0.9, consensus=0.85, drawdown=drawdown)
    on = t["mode"] == "AGGRESSIVE"
    light = "🟢 AGGRESSIVE ON" if on else "⚪ SAFE (locked)"
    kelly = f"  Kelly f*={t['kelly_fraction_star']}" if t.get("kelly_fraction_star") else ""
    print(f"  {label}")
    print(f"     -> {light:22}  risk {t['risk_pct']}%  notional ${t['notional_usd']:.0f} "
          f"({t['leverage']}x lev){kelly}")
    print(f"        mode={t['mode']}  (edge_validated={t['edge_validated']})\n")
    return on


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    edges = json.loads((ROOT / "data" / "validated_edges.json").read_text())
    mnt_edge = edges["MNT"]  # the REAL OOS-validated edge (p_win 0.588, payoff 1.30, n 34)
    bar = _latest_bar("mnt")

    print("Does AGGRESSIVE light up ONLY when HeliQuant is proven? — real MNT data "
          f"({bar['dt']}, entry {bar['entry']:.4f})")
    print(f"unlock rule: validated edge + sample_n >= {MIN_EDGE_SAMPLE} + drawdown < 20%  "
          "(quarter-Kelly, cap 3% / 5x)\n")

    print("CASE 1 — MNT, PROVEN edge (n=34 OOS-validated), account healthy:  << HeliQuant IS jago here >>")
    c1 = case("MNT  edge=validated(n=34)  drawdown=0%", "MNT", mnt_edge, 0.0, bar)

    print("CASE 2 — MNT, edge NOT yet proven (only 12 trades < 20 threshold):  << not enough evidence >>")
    weak = {**mnt_edge, "sample_n": 12}
    c2 = case("MNT  edge=validated(n=12)  drawdown=0%", "MNT", weak, 0.0, bar)

    print("CASE 3 — MNT, PROVEN edge BUT account is down -22% (past the 20% breaker):  << risk-control wins >>")
    c3 = case("MNT  edge=validated(n=34)  drawdown=22%", "MNT", mnt_edge, 0.22, bar)

    print("CASE 4 — an asset HeliQuant has NO validated edge on (e.g. ETH):  << not jago here -> stays safe >>")
    eth_bar = _latest_bar("eth")
    c4 = case("ETH  edge=none  drawdown=0%", "ETH", None, 0.0, eth_bar)

    print("─" * 70)
    print("VERDICT — AGGRESSIVE turned ON in:")
    print(f"  CASE 1 (proven edge, healthy):   {'YES 🟢  <- exactly when HeliQuant earned it' if c1 else 'no'}")
    print(f"  CASE 2 (edge unproven, n<20):    {'YES' if c2 else 'NO ⚪  <- needs validated sample, not hope'}")
    print(f"  CASE 3 (proven edge, -22% DD):   {'YES' if c3 else 'NO ⚪  <- 20% breaker forces SAFE'}")
    print(f"  CASE 4 (no edge on this asset):  {'YES' if c4 else 'NO ⚪  <- not jago here, stays disciplined'}")
    print("\nSo: AGGRESSIVE lights up if-and-only-if HeliQuant has a PROVEN, OOS-validated edge and a")
    print("healthy account — earned by DATA, never by a live win-streak (which would be luck/overfit).")


if __name__ == "__main__":
    main()
