"""Free, keyless market-context feeds (harvested from FinceptTerminal's adapter list — we reimplement
the public endpoints, we do NOT vendor its AGPL code). These are CONTEXT, not edge: the crypto Fear &
Greed index is a slow, contrarian-ish mood gauge and the BTC network stats are backdrop. Surfaced on
the dashboard and available as an optional macro-desk input — never a hard trade gate. All calls fail
SOFT (return None) so a feed outage can't break anything downstream.
"""
import time
import requests

_CACHE: dict = {}
_TTL = 600   # 10-min cache — both feeds move slowly (F&G is daily, chain stats are per-block)


def _cached(key, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        v = fn()
    except Exception:   # noqa: BLE001 — best-effort context; never raise to the caller
        return None
    if v is not None:
        _CACHE[key] = (time.time(), v)
    return v


def fear_greed():
    """Crypto Fear & Greed index (alternative.me): {value 0-100, classification, ts} or None.
    Low = fear (contrarian-bullish), high = greed (contrarian-bearish). A slow daily gauge, not alpha."""
    def _f():
        d = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8).json().get("data", [])
        if not d:
            return None
        row = d[0]
        return {"value": int(row["value"]), "classification": row["value_classification"],
                "ts": int(row["timestamp"])}
    return _cached("fng", _f)


def btc_network():
    """BTC network backdrop (blockchain.com, keyless): price, hash_rate, difficulty, mempool, block time."""
    def _f():
        s = requests.get("https://api.blockchain.info/stats", timeout=8).json()
        try:
            mem = int(requests.get("https://api.blockchain.info/q/unconfirmedcount", timeout=6).text)
        except Exception:   # noqa: BLE001
            mem = None
        return {"price_usd": s.get("market_price_usd"), "hash_rate": s.get("hash_rate"),
                "difficulty": s.get("difficulty"), "mempool": mem,
                "mins_between_blocks": s.get("minutes_between_blocks"), "n_tx_24h": s.get("n_tx")}
    return _cached("btc_net", _f)


def risk_tilt():
    """Map Fear & Greed to a mild CONTRARIAN macro tilt label (context only, not auto-applied to trades):
    extreme fear -> risk-on for longs (capitulation), extreme greed -> risk-off (euphoria)."""
    fg = fear_greed()
    if not fg:
        return None
    v = fg["value"]
    if v <= 25:
        tilt = "risk-on (extreme fear = contrarian-bullish)"
    elif v >= 75:
        tilt = "risk-off (extreme greed = contrarian-bearish)"
    else:
        tilt = "neutral"
    return {"value": v, "classification": fg["classification"], "tilt": tilt}


def read():
    """Combined context payload for the dashboard / macro desk."""
    return {"fear_greed": fear_greed(), "btc_network": btc_network(), "risk_tilt": risk_tilt()}
