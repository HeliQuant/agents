"""FETCH real HYPE perp data + run the OI-contrarian validation (the MNT edge) on HYPE.

Sources (probed reachable from here 2026-06-04):
  * Binance USDT-M futures  -> openInterestHist (THE OI-history signal), klines (price), fundingRate.
    Binance hard-caps OI history at ~30 days, so OI is the binding constraint (~30 non-overlap 24h trades).
  * Hyperliquid /info        -> current OI snapshot + 1h candles (deep, ~208d) — kept for reference,
    but NOT used for the OI-history signal because HL exposes no historical OI time-series.
  * Coinalyze                -> 401 (key invalid/inactive) — unreachable, documented, not used.

Methodology: EXACT replica of scripts/39_oi_backtest.py.
  - signal = oi.pct_change(24)
  - non-overlapping 24h windows (entry every 24 bars) -> independent samples
  - direction (contrarian/momentum) + p20/p80 thresholds derived ONLY from TRAIN (60/40 split)
  - net = pos*raw - 2*FEE ; FEE = 0.00055/side (~11 bps round-trip)
  - VALIDATED (MNT bar) = OOS net ROI > 0 AND > buy&hold AND avg_bps comfortably > round-trip fee
                          AND payoff_b > 0 AND trades >= 10.

Writes ONLY new files:
  data/hype_hl_hourly.csv       (price OHLCV, hourly, aligned to OI window)
  data/hype_hl_positioning.csv  (timestamp,datetime,close,funding,oi,buy_ratio) — backtest input
  data/validated_edges_hype.json

Run: python scripts/54_hype_validate.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FEE = 0.00055  # taker per side (same as scripts/39)
H = 24
SYMBOL = "HYPEUSDT"
HL_COIN = "HYPE"
HOUR_MS = 3600_000


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


# --------------------------------------------------------------------------- fetch
def fetch_binance_oi_hist() -> dict[int, float]:
    """Page openInterestHist back to Binance's ~30d floor. Returns {ms: sumOpenInterest(base)}."""
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    out: dict[int, float] = {}
    end = int(time.time() * 1000)
    for _ in range(12):
        r = requests.get(url, params={"symbol": SYMBOL, "period": "1h",
                                      "limit": 500, "endTime": end}, timeout=30)
        if r.status_code != 200:
            break  # 400 at the floor is expected
        j = r.json()
        if not j:
            break
        before = len(out)
        for p in j:
            out[int(p["timestamp"])] = float(p["sumOpenInterest"])
        if len(out) == before:
            break
        end = min(int(p["timestamp"]) for p in j) - HOUR_MS
        time.sleep(0.2)
    return out


def fetch_binance_klines(start_ms: int, end_ms: int) -> dict[int, float]:
    """Hourly close keyed by open-time ms, covering [start, end]."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    out: dict[int, float] = {}
    cur = start_ms
    while cur <= end_ms:
        r = requests.get(url, params={"symbol": SYMBOL, "interval": "1h",
                                      "startTime": cur, "endTime": end_ms,
                                      "limit": 1500}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if not j:
            break
        for k in j:
            out[int(k[0])] = float(k[4])  # close
        nxt = int(j[-1][0]) + HOUR_MS
        if nxt <= cur:
            break
        cur = nxt
        if len(j) < 1500:
            break
        time.sleep(0.2)
    return out


def fetch_binance_funding(start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """Funding events (8h cadence) within window, sorted by time."""
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    out: dict[int, float] = {}
    cur = start_ms
    while cur <= end_ms:
        r = requests.get(url, params={"symbol": SYMBOL, "startTime": cur,
                                      "endTime": end_ms, "limit": 1000}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if not j:
            break
        for f in j:
            out[int(f["fundingTime"])] = float(f["fundingRate"])
        nxt = int(j[-1]["fundingTime"]) + 1
        if nxt <= cur:
            break
        cur = nxt
        if len(j) < 1000:
            break
        time.sleep(0.2)
    return sorted(out.items())


def hl_current_oi_snapshot() -> dict | None:
    """Reference only: confirms HL lists HYPE + current OI (no historical OI series on HL)."""
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs"}, timeout=25)
        d = r.json()
        names = [u["name"] for u in d[0]["universe"]]
        if HL_COIN in names:
            ctx = d[1][names.index(HL_COIN)]
            return {"openInterest": float(ctx["openInterest"]),
                    "markPx": float(ctx["markPx"]),
                    "funding": float(ctx["funding"])}
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------- backtest (EXACT scripts/39 logic)
def backtest(df: pd.DataFrame) -> dict | None:
    df = df.sort_values("timestamp").reset_index(drop=True)
    c = df["close"].values
    oichg = df["oi"].pct_change(H).values
    n = len(df)
    idx = [i for i in range(H, n - H, H) if not np.isnan(oichg[i])]
    n_windows = len(idx)
    if n_windows < 20:  # too few even to attempt an honest train/test split
        return {"insufficient": True, "n_windows": n_windows}
    split = int(len(idx) * 0.6)
    tr, te = idx[:split], idx[split:]

    tr_sig = np.array([oichg[i] for i in tr])
    tr_ret = np.array([c[i + H] / c[i] - 1 for i in tr])
    ic = pd.Series(tr_sig).corr(pd.Series(tr_ret), method="spearman")
    contrarian = ic < 0
    p20, p80 = np.nanpercentile(tr_sig, 20), np.nanpercentile(tr_sig, 80)

    eq, trades, wins, rets = 1.0, 0, 0, []
    for i in te:
        s = oichg[i]
        raw = c[i + H] / c[i] - 1
        if s >= p80:
            pos = -1 if contrarian else 1
        elif s <= p20:
            pos = 1 if contrarian else -1
        else:
            continue
        net = pos * raw - 2 * FEE
        eq *= (1 + net)
        trades += 1
        wins += int(net > 0)
        rets.append(net)
    bh = c[te[-1] + H] / c[te[0]] - 1
    wins_r = [r for r in rets if r > 0]
    loss_r = [r for r in rets if r <= 0]
    avg_win = float(np.mean(wins_r)) if wins_r else 0.0
    avg_loss = float(np.mean(loss_r)) if loss_r else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else 0.0
    return {
        "insufficient": False, "n_windows": n_windows, "train_n": len(tr), "test_n": len(te),
        "train_ic": float(ic), "dir": "contrarian" if contrarian else "momentum",
        "oos_roi": (eq - 1) * 100, "trades": trades,
        "win": (wins / trades * 100) if trades else 0,
        "p_win": (wins / trades) if trades else 0.0, "payoff_b": round(payoff, 3),
        "avg_win_bps": round(avg_win * 1e4, 1), "avg_loss_bps": round(avg_loss * 1e4, 1),
        "avg_bps": (float(np.mean(rets)) * 1e4) if rets else 0.0, "bh": bh * 100,
    }


# --------------------------------------------------------------------------- main
def main() -> None:
    print("=" * 74)
    print("HYPE OI-contrarian validation — real fetched data only (no halu)")
    print("=" * 74)

    # 1) HL snapshot (reference / sanity that HYPE is a real, liquid perp)
    snap = hl_current_oi_snapshot()
    print(f"\n[Hyperliquid snapshot — reference only] {snap}")
    print("  (HL exposes NO historical OI time-series -> OI history must come from Binance)")

    # 2) Binance OI history (the binding signal)
    print("\n[Binance] paging openInterestHist to floor ...")
    oi = fetch_binance_oi_hist()
    if not oi:
        print("  FAIL: no OI history returned. Cannot run the OI-contrarian test.")
        (DATA / "validated_edges_hype.json").write_text(
            json.dumps({"_note": "Binance OI history unreachable; HYPE OI-contrarian not testable."}, indent=2))
        return
    oi_keys = sorted(oi)
    print(f"  OI points: {len(oi)} | {iso(oi_keys[0])} .. {iso(oi_keys[-1])} "
          f"(~{(oi_keys[-1]-oi_keys[0])/HOUR_MS/24:.1f} days)")

    # 3) Price klines aligned to OI window (same venue, same timestamps)
    print("[Binance] fetching klines (price) over OI window ...")
    px = fetch_binance_klines(oi_keys[0], oi_keys[-1] + H * HOUR_MS)
    print(f"  klines: {len(px)} hourly closes")

    # 4) Funding (8h) -> forward-fill to hourly
    print("[Binance] fetching fundingRate ...")
    fund = fetch_binance_funding(oi_keys[0], oi_keys[-1] + H * HOUR_MS)
    print(f"  funding events: {len(fund)}")

    # ---- assemble hourly frame on the intersection of timestamps that have BOTH price & OI
    rows = []
    fi = 0
    last_f = np.nan
    common = [t for t in oi_keys if t in px]
    for t in sorted(set(list(px.keys()) + oi_keys)):
        while fi < len(fund) and fund[fi][0] <= t:
            last_f = fund[fi][1]
            fi += 1
        if t in px:
            rows.append({
                "timestamp": t, "datetime": iso(t), "close": px[t],
                "funding": (None if np.isnan(last_f) else last_f),
                "oi": oi.get(t), "buy_ratio": None,  # buy_ratio N/A (no global L/S for HYPE here)
            })
    hourly = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    # positioning CSV (backtest input): keep only rows where BOTH close & oi exist
    pos = hourly.dropna(subset=["close", "oi"]).reset_index(drop=True)

    # hourly OHLCV-ish file: here close-only from klines window (we keep full price coverage)
    hourly_out = DATA / "hype_hl_hourly.csv"
    pos_out = DATA / "hype_hl_positioning.csv"
    hourly.to_csv(hourly_out, index=False)
    pos.to_csv(pos_out, index=False)
    print(f"\n  wrote {hourly_out.name}: {len(hourly)} rows")
    print(f"  wrote {pos_out.name}: {len(pos)} rows (close & oi both present)")
    if len(pos):
        print(f"  positioning span: {pos['datetime'].iloc[0]} .. {pos['datetime'].iloc[-1]}")

    # 5) backtest (EXACT scripts/39 methodology)
    print("\n" + "-" * 74)
    print(f"OI-change cost-aware backtest (non-overlap {H}h, fee {FEE*100:.3f}%/side, train-derived dir+thresholds)")
    r = backtest(pos)

    edges: dict = {}
    rt_fee_bps = 2 * FEE * 1e4
    if r is None or r.get("insufficient"):
        nw = r.get("n_windows") if r else 0
        print(f"\n  INSUFFICIENT: only {nw} non-overlap 24h windows (<20). "
              f"Cannot run an honest train/OOS OI-contrarian test on HYPE.")
        print("  Binance hard-caps HYPE OI history at ~30 days; HL has no OI history. "
              "Verdict: NOT testable for the OI edge with the data reachable today.")
        edges = {"_note": f"HYPE OI history too short ({nw} non-overlap 24h windows < 20) "
                          "to validate the OI-contrarian edge. Price/funding available, OI-history is the gap."}
    else:
        hdr = (f"{'asset':6} {'train_IC':>9} {'dir':>11} {'OOS net ROI':>12} {'trades':>7} "
               f"{'win%':>6} {'payoff':>7} {'avg bps':>8} {'buy&hold':>9}")
        print(f"\n{hdr}")
        print(f"{'HYPE':6} {r['train_ic']:+9.3f} {r['dir']:>11} {r['oos_roi']:+11.2f}% {r['trades']:7} "
              f"{r['win']:6.1f} {r['payoff_b']:7.2f} {r['avg_bps']:+8.1f} {r['bh']:+8.2f}%")
        print(f"\n  windows={r['n_windows']} (train {r['train_n']} / test {r['test_n']}) | "
              f"avg_win={r['avg_win_bps']}bps avg_loss={r['avg_loss_bps']}bps | round-trip fee={rt_fee_bps:.1f}bps")
        print("  Read: OOS net ROI > 0 AND > buy&hold AND avg_bps comfortably > round-trip fee = tradeable.")

        validated = (r["dir"] == "contrarian" and r["oos_roi"] > 0 and r["oos_roi"] > r["bh"]
                     and r["avg_bps"] > rt_fee_bps and r["payoff_b"] > 0 and r["trades"] >= 10)
        # honesty: flag small sample explicitly
        small = r["n_windows"] < 60
        print(f"\n  VALIDATED (MNT bar): {validated}"
              + ("  [+ small-sample caveat: <60 windows]" if small and validated else ""))
        if validated:
            edges["HYPE"] = {
                "edge": "oi_contrarian", "asset": "HYPE", "validated": True,
                "p_win": round(r["p_win"], 4), "payoff_b": r["payoff_b"],
                "sample_n": r["trades"], "oos_roi_pct": round(r["oos_roi"], 2),
                "source": "Binance USDT-M futures openInterestHist (1h) + klines; "
                          f"OI history ~{(oi_keys[-1]-oi_keys[0])/HOUR_MS/24:.0f}d (Binance cap)",
                "note": ("OI-contrarian OOS (non-overlap 24h, cost-aware, train-derived dir+thresholds). "
                         "SMALL SAMPLE (<60 windows, Binance OI 30d cap) -> caveated; sizing must gate on sample_n."),
            }
        else:
            why = []
            if r["dir"] != "contrarian":
                why.append(f"dir={r['dir']} (not contrarian)")
            if not (r["oos_roi"] > 0):
                why.append("OOS ROI <= 0")
            if not (r["oos_roi"] > r["bh"]):
                why.append("OOS <= buy&hold")
            if not (r["avg_bps"] > rt_fee_bps):
                why.append(f"avg_bps {r['avg_bps']:.1f} <= fee {rt_fee_bps:.1f}")
            if not (r["payoff_b"] > 0):
                why.append("payoff<=0")
            if not (r["trades"] >= 10):
                why.append(f"trades {r['trades']}<10")
            print("  -> NOT validated:", "; ".join(why))
            edges = {"_note": "HYPE failed the OI-contrarian validation bar: " + "; ".join(why)
                              + f". (windows={r['n_windows']}, source=Binance OI ~30d cap)"}

    out = DATA / "validated_edges_hype.json"
    out.write_text(json.dumps(edges, indent=2))
    print(f"\nwrote -> {out.name}")
    print(json.dumps(edges, indent=2))


if __name__ == "__main__":
    main()
