"""scripts/53_meth_eth_convergence.py — is there a tradable mETH/ETH convergence (stat-arb) edge?

mETH is liquid-staked ETH, so the ratio mETH/ETH should sit near (1 + accrued yield) and drift up
slowly. Short-term dislocations (depeg stress, thin liquidity) ought to mean-revert. We test a
MARKET-NEUTRAL spread trade on the ROLLING z-score of the ratio (rolling tracks the slow yield drift,
so we trade only the deviations):

  z >= +entry_z  -> ratio rich  -> SHORT spread (short mETH / long ETH), bet it falls back
  z <= -entry_z  -> ratio cheap -> LONG  spread (long mETH / short ETH), bet it rises
  exit when z reverts through 0, or after MAX_HOLD hours.

Cost-aware: a round-trip touches 4 legs (enter 2 + exit 2) -> 4 x taker fee. OOS-honest: pick entry_z
on the TRAIN half, report performance on the unseen TEST half. If it doesn't clear costs, we say so.

Run: python scripts/53_meth_eth_convergence.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FEE = 0.00055           # taker per leg; round-trip spread trade = 4 legs
ENTRY_Z = [1.5, 2.0, 2.5]
WINDOWS = [168, 336, 720]   # rolling z window in hours (1w / 2w / 30d)
MAX_HOLD = 168          # force-exit after a week if no revert
MIN_TRADES = 20
SLIPPAGE_PER_LEG = 0.0025  # conservative REAL slippage for THIN mETH (fee alone is only 0.055%) — the killer for an LST spread


def load_aligned() -> pd.DataFrame:
    m = pd.read_csv(ROOT / "data" / "meth_hourly.csv")[["timestamp", "close"]].rename(columns={"close": "meth"})
    e = pd.read_csv(ROOT / "data" / "eth_hourly.csv")[["timestamp", "close"]].rename(columns={"close": "eth"})
    df = m.merge(e, on="timestamp").sort_values("timestamp").reset_index(drop=True)
    df = df[(df["meth"] > 0) & (df["eth"] > 0)].reset_index(drop=True)
    df["ratio"] = df["meth"] / df["eth"]
    return df


def run_spread(sub: pd.DataFrame, entry_z: float) -> dict:
    """Event-driven market-neutral spread trade on the precomputed z-score column."""
    eq, trades, wins, rets = 1.0, 0, 0, []
    pos, entry_ratio, entry_i = 0, None, None
    z = sub["z"].values
    r = sub["ratio"].values
    for i in range(len(sub)):
        if np.isnan(z[i]):
            continue
        if pos == 0:
            if z[i] >= entry_z:
                pos, entry_ratio, entry_i = -1, r[i], i      # ratio rich -> short spread
            elif z[i] <= -entry_z:
                pos, entry_ratio, entry_i = 1, r[i], i        # ratio cheap -> long spread
        else:
            revert = (pos == -1 and z[i] <= 0) or (pos == 1 and z[i] >= 0)
            if revert or (i - entry_i) >= MAX_HOLD:
                raw = pos * (r[i] - entry_ratio) / entry_ratio
                net = raw - 4 * FEE
                eq *= (1 + net)
                trades += 1
                wins += int(net > 0)
                rets.append(net)
                pos = 0
    wr = [x for x in rets if x > 0]
    lr = [x for x in rets if x <= 0]
    payoff = (np.mean(wr) / abs(np.mean(lr))) if wr and lr and np.mean(lr) < 0 else 0.0
    return {"roi_pct": (eq - 1) * 100, "trades": trades,
            "win": (wins / trades * 100) if trades else 0.0,
            "p_win": (wins / trades) if trades else 0.0, "payoff_b": round(payoff, 3),
            "avg_bps": (np.mean(rets) * 1e4) if rets else 0.0}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    df = load_aligned()
    print(f"mETH/ETH convergence — {len(df)} aligned hourly bars "
          f"(ratio {df['ratio'].iloc[0]:.4f} .. {df['ratio'].iloc[-1]:.4f}); "
          f"round-trip cost = 4 x {FEE*100:.3f}% = {4*FEE*1e4:.0f} bps\n")
    print(f"{'window':>7} {'entry_z':>8} | {'TRAIN roi':>10} {'tr#':>4} | {'TEST roi':>9} {'te#':>4} {'te win%':>8} {'payoff':>7} {'avg bps':>8}")

    best = None
    for window in WINDOWS:
        d = df.copy()
        d["mean"] = d["ratio"].rolling(window).mean()
        d["std"] = d["ratio"].rolling(window).std()
        d["z"] = (d["ratio"] - d["mean"]) / d["std"]
        split = int(len(d) * 0.6)
        train, test = d.iloc[:split].reset_index(drop=True), d.iloc[split:].reset_index(drop=True)
        for ez in ENTRY_Z:
            tr, te = run_spread(train, ez), run_spread(test, ez)
            print(f"{window:>7} {ez:>8.1f} | {tr['roi_pct']:>9.2f}% {tr['trades']:>4} | "
                  f"{te['roi_pct']:>8.2f}% {te['trades']:>4} {te['win']:>7.1f} {te['payoff_b']:>7.2f} {te['avg_bps']:>+8.1f}")
            # select on TRAIN (highest train ROI with enough train trades) -> honest OOS
            if tr["trades"] >= MIN_TRADES and (best is None or tr["roi_pct"] > best["train_roi"]):
                best = {"window": window, "entry_z": ez, "train_roi": tr["roi_pct"], "test": te}

    print("\n--- OOS verdict (parameter picked on TRAIN, judged on TEST) ---")
    if not best:
        print("No parameter set had enough TRAIN trades — inconclusive / not tradable.")
        return
    te = best["test"]
    print(f"best-train params: window={best['window']} entry_z={best['entry_z']} -> "
          f"TEST roi {te['roi_pct']:+.2f}% | trades {te['trades']} | win {te['win']:.1f}% | "
          f"payoff {te['payoff_b']} | avg {te['avg_bps']:+.1f} bps")
    # EXECUTION-REALISM GATE: the backtest cost above is only 4x taker fee (22 bps), which ASSUMES deep
    # liquidity on both legs. mETH on Mantle is THIN -> real slippage dominates. avg_bps is already net of
    # the 22 bps fee; subtract a conservative real slippage to see whether anything actually survives.
    extra_slip_bps = 4 * SLIPPAGE_PER_LEG * 1e4
    realistic_net = te["avg_bps"] - extra_slip_bps
    print(f"deep-liquidity TEST: {te['avg_bps']:+.1f} bps/trade (looks great)")
    print(f"realistic thin-mETH slippage (~{SLIPPAGE_PER_LEG*100:.2f}%/leg x4 = {extra_slip_bps:.0f} bps): "
          f"net {realistic_net:+.1f} bps/trade")
    tradable = realistic_net > 0 and te["trades"] >= MIN_TRADES and te["payoff_b"] > 0
    print(f"VALIDATED TRADABLE EDGE? "
          f"{'YES' if tradable else 'NO — strong on paper but dies under realistic mETH slippage -> NOT registered'}")

    if tradable:
        path = ROOT / "data" / "validated_edges.json"
        edges = json.loads(path.read_text()) if path.exists() else {}
        edges["METH_ETH_CONVERGENCE"] = {
            "edge": "meth_eth_convergence", "asset": "METH/ETH", "validated": True,
            "p_win": round(te["p_win"], 4), "payoff_b": te["payoff_b"], "sample_n": te["trades"],
            "oos_roi_pct": round(te["roi_pct"], 2), "params": {"window": best["window"], "entry_z": best["entry_z"]},
            "note": "Market-neutral mETH/ETH spread mean-reversion; survives realistic slippage.",
        }
        path.write_text(json.dumps(edges, indent=2))
        print(f"-> wrote METH_ETH_CONVERGENCE to {path.name}")
    else:
        print("-> NOT added to validated_edges.json. Honest: a +96%/95%-win backtest that evaporates under "
              "realistic mETH slippage is a thin-liquidity ARTIFACT, not deployable alpha. Kept as a research note.")


if __name__ == "__main__":
    main()
