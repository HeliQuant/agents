"""TradingView multi-timeframe TA consensus (free, anonymous) — used as an ALIGNMENT GATE, not a
signal source. The floor trades a 1h signal; when the DAILY (1D) consensus clearly OPPOSES the 1h
direction the entry is a counter-daily bounce ("buying the top in a daily downtrend") — the floor's
main directional bleed. This is NOT an edge: it's a lagging, MA-heavy aggregate of the same trend
indicators we already know don't predict. Its value is the cross-timeframe STRUCTURE (1h vs 1D),
used to fade or abstain on bounces against the daily trend.

Endpoint: POST scanner.tradingview.com/global/scan (no auth, no key). Source pattern lifted from
@mathieuc/tradingview getTA (src/miscRequests.js). Raw consensus values run ~-1..+1:
  >= +0.1 buy · <= -0.1 sell · |v| >= 0.5 strong.
Fails SOFT (returns None) everywhere so a feed hiccup can never break the floor.
"""
import time
import requests

# our 6 assets -> the exchange:symbol TradingView resolves (probed live 2026-06-14: all 6 covered,
# incl the usually-missing alts MNT/HYPE on Bybit and SUI on Binance).
_TICKERS = {
    "BTC": "BINANCE:BTCUSDT", "ETH": "BINANCE:ETHUSDT", "SOL": "BINANCE:SOLUSDT",
    "MNT": "BYBIT:MNTUSDT", "HYPE": "BYBIT:HYPEUSDT", "SUI": "BINANCE:SUIUSDT",
}
_TF = ["60", "240", "1D"]   # 1h, 4h, 1D (the bare '1D' field has no suffix on the endpoint)
_TTL = 300                  # 5-min cache — consensus moves slowly; don't hammer an unofficial endpoint
_CACHE: dict = {}           # asset -> (epoch, {tf: value})

# gate thresholds, on the raw -1..+1 scale (daily opposing our side):
FADE_OPP = 0.15             # mild counter-daily -> trade it smaller + on the short fade-horizon
ABSTAIN_OPP = 0.50          # strong counter-daily (clear top/bottom chase) -> skip the entry entirely


def _cols():
    return [f"Recommend.All|{t}" if t != "1D" else "Recommend.All" for t in _TF]


def consensus(asset: str):
    """{'60': x, '240': y, '1D': z} raw TV consensus (-1..+1) per timeframe, or None on any failure."""
    tk = _TICKERS.get(asset)
    if not tk:
        return None
    hit = _CACHE.get(asset)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        r = requests.post(
            "https://scanner.tradingview.com/global/scan",
            json={"symbols": {"tickers": [tk]}, "columns": _cols()},
            timeout=8, headers={"User-Agent": "Mozilla/5.0"},
        )
        rows = r.json().get("data", [])
        if not rows or "d" not in rows[0]:
            return None
        d = rows[0]["d"]
        out = {tf: d[i] for i, tf in enumerate(_TF) if i < len(d)}
        _CACHE[asset] = (time.time(), out)
        return out
    except Exception:   # noqa: BLE001 — best-effort gate; never break the floor on a feed hiccup
        return None


def alignment(asset: str, direction: str):
    """Compare the floor's 1h direction to the DAILY consensus. Returns:
       None      — TV unavailable (no gate applied),
       'ok'      — aligned or neutral daily,
       'fade'    — mild counter-daily bounce (trade smaller + shorter hold),
       'abstain' — strong counter-daily (clear top/bottom chase: skip).
    """
    c = consensus(asset)
    if not c or c.get("1D") is None:
        return None
    daily = c["1D"]
    opp = -daily if direction == "LONG" else daily   # > 0 means the daily trend OPPOSES our side
    if opp >= ABSTAIN_OPP:
        return "abstain"
    if opp >= FADE_OPP:
        return "fade"
    return "ok"
