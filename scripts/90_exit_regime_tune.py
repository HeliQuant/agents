"""
scripts/90_exit_regime_tune.py — backtest the two fine-tune levers the campaign asked for.

LEVER A  LET WINNERS RUN   compare exit policies on identical entries:
            cur4  = current campaign (TP 2.5xATR / SL 1.8xATR / 4h time-cap)
            h12   = same TP/SL, 12h time-cap
            trail = chandelier trailing stop, no TP cap, 24h time-cap  (let the move run)
LEVER B  REGIME-GATED SIZE  size by the SAME trailing-indicator regime rule the 82.6%-OOS
            classifier was trained on (firm/ml.build_features + p85 vol / p60 adx, fit on
            TRAIN only): 2x in the trend that favours the trade, 1x in Ranging, SKIP otherwise.

Honesty: path-aware intrabar exits via high/low, round-trip cost 2*COST (~20bps), strict
train/test split (regime thresholds fit on train only -> no lookahead), and BOTH short-only
(matches the live campaign) and symmetric-trend (guards against the falling-market artifact
that flattered earlier short-only numbers). Every printed number comes from this run.
"""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm import ml  # noqa: E402
from firm.edge_lab import COST  # noqa: E402  per-side cost; round trip = 2*COST

DATA = ROOT / "data"
ASSETS = ["BTC", "ETH", "SOL", "HYPE", "SUI", "MNT"]
RT = 2 * COST
ATR_SL, ATR_TP, TRAIL_K = 1.8, 2.5, 1.8
SMA_N, MOM_N = 48, 12
TEST_FRAC = 0.30
CAP_H = {"cur4": 4, "h12": 12, "trail": 24}


def load(asset: str) -> pd.DataFrame | None:
    p = DATA / f"{asset.lower()}_bybit_hourly.csv"
    if not p.exists():
        return None
    df = ml.build_features(pd.read_csv(p, parse_dates=["datetime"])).reset_index(drop=True)
    df["sma"] = df["close"].rolling(SMA_N).mean()
    df["mom"] = df["close"] / df["close"].shift(MOM_N) - 1
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum((df["high"] - df["close"].shift()).abs(), (df["low"] - df["close"].shift()).abs()),
    )
    df["atr"] = tr.rolling(14).mean() / df["close"]
    return df.dropna().reset_index(drop=True)


def label_regime(df: pd.DataFrame, vol_hi: float, adx_hi: float) -> np.ndarray:
    return np.where(
        df["volatility_10"] > vol_hi, "High_Volatility",
        np.where(df["adx"] > adx_hi,
                 np.where(df["ema_slope_5"] > 0, "Trending_Up", "Trending_Down"),
                 "Ranging"),
    )


def simulate(df: pd.DataFrame, side_mode: str, exit_policy: str, sizing: str) -> pd.DataFrame:
    cap = CAP_H[exit_policy]
    close, high, low = df["close"].values, df["high"].values, df["low"].values
    atr, sma, mom, reg = df["atr"].values, df["sma"].values, df["mom"].values, df["regime"].values
    n = len(df)
    rows = []
    i = 0
    while i < n - 1:
        side = 0
        if close[i] < sma[i] and mom[i] < 0:
            side = -1
        elif close[i] > sma[i] and mom[i] > 0 and side_mode == "symmetric":
            side = 1
        if side == 0:
            i += 1
            continue
        if sizing == "regime":
            fav = "Trending_Down" if side == -1 else "Trending_Up"
            w = 2.0 if reg[i] == fav else (1.0 if reg[i] == "Ranging" else 0.0)
            if w == 0.0:
                i += 1
                continue
        else:
            w = 1.0
        entry, a = close[i], atr[i]
        sl = entry * (1 + ATR_SL * a) if side == -1 else entry * (1 - ATR_SL * a)
        tp = entry * (1 - ATR_TP * a) if side == -1 else entry * (1 + ATR_TP * a)
        trail, best = sl, entry
        exit_px, j = None, i + 1
        while j < n:
            held = j - i
            if exit_policy == "trail":
                if side == -1:
                    best = min(best, low[j])
                    trail = min(trail, best * (1 + TRAIL_K * a))
                    if high[j] >= trail:
                        exit_px = trail
                        break
                else:
                    best = max(best, high[j])
                    trail = max(trail, best * (1 - TRAIL_K * a))
                    if low[j] <= trail:
                        exit_px = trail
                        break
            else:  # fixed TP/SL — SL checked first (conservative when both touch in a bar)
                if side == -1:
                    if high[j] >= sl:
                        exit_px = sl
                        break
                    if low[j] <= tp:
                        exit_px = tp
                        break
                else:
                    if low[j] <= sl:
                        exit_px = sl
                        break
                    if high[j] >= tp:
                        exit_px = tp
                        break
            if held >= cap:
                exit_px = close[j]
                break
            j += 1
        if exit_px is None:
            break
        ret = ((1 - exit_px / entry) if side == -1 else (exit_px / entry - 1)) - RT
        rows.append({"side": side, "ret": ret, "w": w, "held": j - i})
        i = j + 1  # one position at a time; resume after the exit bar
    return pd.DataFrame(rows)


def metrics(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return {"n": 0, "win": 0.0, "avg_pct": 0.0, "total_pct": 0.0, "maxdd_pct": 0.0, "sharpe": 0.0}
    r, w = tr["ret"].values, tr["w"].values
    eq, peak, dd = 1.0, 1.0, 0.0
    for ri, wi in zip(r, w):
        eq *= 1 + ri * wi
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    return {
        "n": len(tr), "win": float((r > 0).mean() * 100), "avg_pct": float(np.mean(r) * 100),
        "total_pct": (eq - 1) * 100, "maxdd_pct": dd * 100,
        "sharpe": float(np.mean(r) / (np.std(r) + 1e-12) * np.sqrt(len(r))),
    }


def run() -> None:
    # build per-asset test frames with regime labelled using TRAIN-only thresholds (no lookahead)
    frames = {}
    for a in ASSETS:
        df = load(a)
        if df is None or len(df) < 500:
            print(f"  skip {a}: insufficient data")
            continue
        cut = int(len(df) * (1 - TEST_FRAC))
        train = df.iloc[:cut]
        vol_hi = float(train["volatility_10"].quantile(0.85))
        adx_hi = float(train["adx"].quantile(0.60))
        df["regime"] = label_regime(df, vol_hi, adx_hi)
        frames[a] = df.iloc[cut:].reset_index(drop=True)
    if not frames:
        print("no data")
        return

    def pooled(side_mode: str, exit_policy: str, sizing: str) -> pd.DataFrame:
        return pd.concat([simulate(f, side_mode, exit_policy, sizing) for f in frames.values()], ignore_index=True)

    print("\n================  OOS TEST (last 30%)  ·  round-trip cost {:.0f} bps  ================".format(RT * 1e4))

    print("\nLEVER A — LET WINNERS RUN  (short-only, flat size; identical entries, exit policy varies)")
    print(f"  {'policy':<8}{'n':>6}{'win%':>8}{'avg%/trd':>10}{'total%':>10}{'maxDD%':>9}{'sharpe':>8}{'avgHeld':>9}")
    for pol in ("cur4", "h12", "trail"):
        tr = pooled("short_only", pol, "flat")
        m = metrics(tr)
        held = float(tr["held"].mean()) if len(tr) else 0.0
        print(f"  {pol:<8}{m['n']:>6}{m['win']:>8.1f}{m['avg_pct']:>10.3f}{m['total_pct']:>10.1f}"
              f"{m['maxdd_pct']:>9.1f}{m['sharpe']:>8.2f}{held:>9.1f}")

    print("\nLEVER B — REGIME-GATED SIZE  (best exit per A; flat vs regime 2x; short-only AND symmetric)")
    print(f"  {'mode':<14}{'sizing':<8}{'n':>6}{'win%':>8}{'avg%/trd':>10}{'total%':>10}{'maxDD%':>9}{'sharpe':>8}")
    best_exit = "trail"  # provisional; the A table above is the evidence — printed so the reader can re-judge
    for mode in ("short_only", "symmetric"):
        for sz in ("flat", "regime"):
            m = metrics(pooled(mode, best_exit, sz))
            print(f"  {mode:<14}{sz:<8}{m['n']:>6}{m['win']:>8.1f}{m['avg_pct']:>10.3f}"
                  f"{m['total_pct']:>10.1f}{m['maxdd_pct']:>9.1f}{m['sharpe']:>8.2f}")

    print("\nPER-ASSET (short-only · trail exit · flat size · OOS)")
    print(f"  {'asset':<7}{'n':>6}{'win%':>8}{'avg%/trd':>10}{'total%':>10}{'maxDD%':>9}")
    for a, f in frames.items():
        m = metrics(simulate(f, "short_only", "trail", "flat"))
        print(f"  {a:<7}{m['n']:>6}{m['win']:>8.1f}{m['avg_pct']:>10.3f}{m['total_pct']:>10.1f}{m['maxdd_pct']:>9.1f}")
    print("\n(reminder: paper return %, OOS — real capital still needs a registry-validated edge)")


if __name__ == "__main__":
    run()
