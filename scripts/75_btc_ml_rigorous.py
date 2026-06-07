"""scripts/75 — RIGOROUS re-test of the UAS BTC ML strategy under HeliQuant's honesty gate.

The UAS doc (Hasil_Test_BTCUSD.md) reported BTC 5m RF +36% ROI / PF 2.22 / 62% win — BUT its own §8
admits: NO fees, 17-day single-regime data, confidence tuned on the test set, and CV accuracy 49%
(≈ random). This script keeps the SAME core idea (EMA/RSI/Stoch/ATR features + RF + triple-barrier
SL/TP at 1.0x/1.46x ATR, 10-bar horizon) but ADDS the four things it lacked, so the result is trustable:

  1. REAL transaction cost — taker fee + slippage on every trade (the #1 thing missing).
  2. WALK-FORWARD — 5 anchored folds, not one 80/20 split.
  3. OUT-OF-SAMPLE confidence threshold — grid-searched on a validation slice of TRAIN, applied to TEST
     (kills the selection bias of picking the threshold on the test set).
  4. BUY & HOLD baseline + a much BIGGER dataset (months of Binance 5m, vs 17 days on Hyperliquid).

Verdict rule (same spirit as the edge_lab gate): to be REAL, net-of-fee ROI must be > 0 AND > buy&hold
AND profit-factor > 1, CONSISTENTLY across the walk-forward folds. A zero-fee column is printed too, to
show exactly how much of the headline was just unmodelled cost.

Run:  python scripts/75_btc_ml_rigorous.py            # ~6 months, RF
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BINANCE = "https://fapi.binance.com/fapi/v1/klines"

FEE = 0.00055        # Binance/Bybit taker ~5.5 bps per side
SLIP = 0.00010       # 1 bp/side slippage on liquid 5m BTC (conservative-low)
COST_PER_SIDE = FEE + SLIP
TP_MULT, SL_MULT, HORIZON = 1.46, 1.0, 10   # exactly the UAS doc's SL/TP/horizon
RISK_PCT = 0.01      # 1% risk per trade (UAS doc), risk = SL distance
LEV_CAP = 20.0       # cap notional/equity (a tight ATR SL implies high leverage; 20x is realistic)


# ─────────────────────────── data ───────────────────────────
def fetch_btc_5m(months: int = 6) -> pd.DataFrame:
    cache = DATA / "btc_5m_binance.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        span_d = (df["t"].iloc[-1] - df["t"].iloc[0]) / 1000 / 86400
        if span_d >= months * 28:
            print(f"  [cache] {len(df)} bars ({span_d:.0f}d) <- {cache.name}")
            return df
    now = int(time.time() * 1000)
    start = now - months * 30 * 86400 * 1000
    rows: dict[int, list] = {}
    end = now
    while end > start:
        try:
            j = requests.get(BINANCE, params={"symbol": "BTCUSDT", "interval": "5m",
                                              "limit": 1500, "endTime": end}, timeout=20).json()
        except Exception as e:  # noqa: BLE001
            print(f"  fetch error: {str(e)[:60]}"); break
        if not isinstance(j, list) or not j:
            break
        for k in j:
            rows[int(k[0])] = k
        oldest = min(int(k[0]) for k in j)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.12)
    lst = sorted(rows.values(), key=lambda k: int(k[0]))
    df = pd.DataFrame({
        "t": [int(k[0]) for k in lst],
        "open": [float(k[1]) for k in lst], "high": [float(k[2]) for k in lst],
        "low": [float(k[3]) for k in lst], "close": [float(k[4]) for k in lst],
        "volume": [float(k[5]) for k in lst],
    })
    df.to_csv(cache, index=False)
    return df


# ─────────────────────────── indicators / features ───────────────────────────
def _rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def _adx(df, n=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = _atr(df, n)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    df["atr"] = _atr(df)
    df["atr_frac"] = df["atr"] / c
    for n in (20, 50, 100):
        df[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
        df[f"px_ema{n}"] = c / df[f"ema{n}"] - 1
    df["ema_gap_20_50"] = df["ema20"] / df["ema50"] - 1
    df["ema_gap_50_100"] = df["ema50"] / df["ema100"] - 1
    df["rsi"] = _rsi(c)
    lo14, hi14 = df["low"].rolling(14).min(), df["high"].rolling(14).max()
    df["stoch_k"] = 100 * (c - lo14) / (hi14 - lo14).replace(0, np.nan)
    df["adx"] = _adx(df)
    for n in (1, 5, 10, 20):
        df[f"ret{n}"] = c.pct_change(n)
    df["vol20"] = df["ret1"].rolling(20).std()
    df["mom10"] = c / c.shift(10) - 1
    df["vol_z"] = (df["volume"] - df["volume"].rolling(50).mean()) / df["volume"].rolling(50).std()
    dt = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["hour"] = dt.dt.hour
    df["dow"] = dt.dt.dayofweek
    df["ny_session"] = ((dt.dt.hour >= 13) & (dt.dt.hour < 21)).astype(int)
    df["london_session"] = ((dt.dt.hour >= 7) & (dt.dt.hour < 16)).astype(int)
    return df


FEATS = ["atr_frac", "px_ema20", "px_ema50", "px_ema100", "ema_gap_20_50", "ema_gap_50_100",
         "rsi", "stoch_k", "adx", "ret1", "ret5", "ret10", "ret20", "vol20", "mom10", "vol_z",
         "hour", "dow", "ny_session", "london_session"]


def triple_barrier_label(df: pd.DataFrame) -> np.ndarray:
    """1 if a LONG trade hits +TP_MULT*ATR before -SL_MULT*ATR within HORIZON bars, else 0."""
    c, h, l, a = df["close"].values, df["high"].values, df["low"].values, df["atr"].values
    n = len(df); lab = np.full(n, np.nan)
    for i in range(n - HORIZON):
        e, ai = c[i], a[i]
        if not (ai > 0):
            continue
        tp, sl = e + TP_MULT * ai, e - SL_MULT * ai
        out = 0
        for j in range(i + 1, i + 1 + HORIZON):
            if h[j] >= tp:
                out = 1; break
            if l[j] <= sl:
                out = 0; break
        lab[i] = out
    return lab


# ─────────────────────────── fee-aware SL/TP backtest ───────────────────────────
def backtest(df: pd.DataFrame, proba: np.ndarray, buy_thr: float, sell_thr: float,
             cost_per_side: float) -> dict:
    """Non-overlapping SL/TP trades. LONG if P(long-win)>buy_thr, SHORT if <sell_thr. 1% risk sizing
    (risk=SL dist), leverage-capped, net of cost_per_side*2 (entry+exit) on notional. Returns metrics."""
    c, h, l, a = df["close"].values, df["high"].values, df["low"].values, df["atr"].values
    n = len(df); eq = 1.0; i = 0
    wins = losses = 0; gw = gl = 0.0; rets = []; levs = []
    while i < n - HORIZON:
        p = proba[i]; ai = a[i]; e = c[i]
        if not (ai > 0) or np.isnan(p):
            i += 1; continue
        direction = 1 if p > buy_thr else (-1 if p < sell_thr else 0)
        if direction == 0:
            i += 1; continue
        if direction == 1:
            tp, sl = e + TP_MULT * ai, e - SL_MULT * ai
        else:
            tp, sl = e - TP_MULT * ai, e + SL_MULT * ai
        exit_px, exit_j = c[min(i + HORIZON, n - 1)], i + HORIZON
        for j in range(i + 1, min(i + 1 + HORIZON, n)):
            if direction == 1:
                if h[j] >= tp: exit_px, exit_j = tp, j; break
                if l[j] <= sl: exit_px, exit_j = sl, j; break
            else:
                if l[j] <= tp: exit_px, exit_j = tp, j; break
                if h[j] >= sl: exit_px, exit_j = sl, j; break
        gross = (exit_px / e - 1) * direction
        sl_dist = SL_MULT * ai / e
        lev = min(RISK_PCT / sl_dist, LEV_CAP) if sl_dist > 0 else 0
        notional = lev * eq
        levs.append(lev)
        pnl = notional * gross - notional * cost_per_side * 2
        eq += pnl
        r = pnl / (eq - pnl) if (eq - pnl) > 0 else 0
        rets.append(r)
        if pnl >= 0: wins += 1; gw += pnl
        else: losses += 1; gl += -pnl
        i = exit_j + 1
    trades = wins + losses
    return {
        "roi_pct": (eq - 1) * 100, "trades": trades,
        "win_pct": 100 * wins / trades if trades else 0.0,
        "pf": (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0),
        "avg_bps": float(np.mean(rets) * 1e4) if rets else 0.0,
        "avg_lev": float(np.mean(levs)) if levs else 0.0,
    }


def pick_thresholds(df_val, proba_val, cost) -> tuple[float, float]:
    best, best_roi = (0.6, 0.4), -1e9
    for bt in (0.52, 0.55, 0.58, 0.62, 0.66):
        for st in (0.34, 0.38, 0.42, 0.46):
            m = backtest(df_val, proba_val, bt, st, cost)
            if m["trades"] >= 8 and m["roi_pct"] > best_roi:
                best_roi, best = m["roi_pct"], (bt, st)
    return best


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    from sklearn.ensemble import RandomForestClassifier

    print("RIGOROUS BTC ML re-test (UAS strategy + fees + walk-forward + OOS threshold + B&H)\n")
    print("Fetching Binance BTCUSDT 5m...")
    df = fetch_btc_5m(months=6)
    df = build_features(df)
    df["label"] = triple_barrier_label(df)
    df = df.dropna(subset=FEATS + ["label"]).reset_index(drop=True)
    span_d = (df["t"].iloc[-1] - df["t"].iloc[0]) / 1000 / 86400
    print(f"usable: {len(df)} bars ({span_d:.0f}d), label balance long-win={df['label'].mean():.2%}\n")

    N = len(df); folds = 5; purge = HORIZON
    init = int(N * 0.5); test_len = (N - init) // folds
    print(f"{'fold':5}{'test period (bars)':22}{'B&H':>8}{'  ZERO-FEE':>20}{'   WITH-FEE (real)':>26}")
    print(f"{'':5}{'':22}{'':>8}{'roi%/pf/win/n':>20}{'roi%/pf/win/n/lev/bps':>26}")
    print("-" * 90)
    agg = {"zf": [], "wf": [], "bh": []}
    for k in range(folds):
        tr_end = init + k * test_len
        te_s, te_e = tr_end + purge, min(tr_end + purge + test_len, N)
        if te_e - te_s < 40:
            continue
        val_len = max(int(tr_end * 0.15), 500)
        core_end = tr_end - val_len - purge
        Xtr, ytr = df[FEATS].iloc[:core_end], df["label"].iloc[:core_end]
        rf = RandomForestClassifier(n_estimators=200, max_depth=10, min_samples_leaf=50,
                                    class_weight="balanced", n_jobs=-1, random_state=42)
        rf.fit(Xtr, ytr)
        val = df.iloc[core_end + purge:tr_end].reset_index(drop=True)
        pv = rf.predict_proba(val[FEATS])[:, 1]
        bt, st = pick_thresholds(val, pv, COST_PER_SIDE)
        test = df.iloc[te_s:te_e].reset_index(drop=True)
        pt = rf.predict_proba(test[FEATS])[:, 1]
        m0 = backtest(test, pt, bt, st, 0.0)            # zero-fee (replicates the doc's optimism)
        mf = backtest(test, pt, bt, st, COST_PER_SIDE)  # real cost
        bh = (test["close"].iloc[-1] / test["close"].iloc[0] - 1) * 100
        agg["zf"].append(m0["roi_pct"]); agg["wf"].append(mf["roi_pct"]); agg["bh"].append(bh)
        per = f"{pd.to_datetime(df['t'].iloc[te_s],unit='ms').strftime('%m-%d')}→{pd.to_datetime(df['t'].iloc[te_e-1],unit='ms').strftime('%m-%d')}"
        print(f"{k:<5}{per:22}{bh:>+7.1f}%{m0['roi_pct']:>+8.1f}/{m0['pf']:>4.2f}/{m0['win_pct']:>3.0f}/{m0['trades']:>3}"
              f"{mf['roi_pct']:>+9.1f}/{mf['pf']:>4.2f}/{mf['win_pct']:>3.0f}/{mf['trades']:>3}/{mf['avg_lev']:>3.0f}x/{mf['avg_bps']:>+5.0f}")

    print("-" * 90)
    zf, wf, bh = np.array(agg["zf"]), np.array(agg["wf"]), np.array(agg["bh"])
    print(f"AGG  zero-fee mean ROI {zf.mean():+.1f}% ({(zf>0).sum()}/{len(zf)} +ve)  |  "
          f"WITH-FEE mean ROI {wf.mean():+.1f}% ({(wf>0).sum()}/{len(wf)} +ve)  |  B&H mean {bh.mean():+.1f}%")
    real = (wf > 0).sum() >= np.ceil(len(wf) / 2) and wf.mean() > 0 and wf.mean() > bh.mean()
    print(f"\nVERDICT: net-of-fee {'PASSES' if real else 'FAILS'} the gate "
          f"(majority folds +ve AND mean>0 AND mean>buy&hold). "
          f"{'REAL edge — celebrate + confirm forward.' if real else 'Headline was unmodelled-cost / overfit — honest ABSTAIN on BTC.'}")
    print(f"fee impact: zero-fee mean {zf.mean():+.1f}% → with-fee {wf.mean():+.1f}% "
          f"(cost ate {zf.mean()-wf.mean():.1f} pts; avg ~{COST_PER_SIDE*2*1e4:.0f}bps/trade round-trip on capped lev).")


if __name__ == "__main__":
    main()
