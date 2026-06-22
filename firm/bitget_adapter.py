"""firm/bitget_adapter.py — Bitget v2 execution venue (DEMO trading).

Bitget demo = the **SUSDT-FUTURES** product (margin coin SUSDT, demo coins, no real funds), in the live
environment — NOT the `paptrading` header. Demo symbols are S-prefixed: SBTCSUSDT / SETHSUSDT / SXRPSUSDT
(BTC/ETH/XRP). HMAC-SHA256 signing (Bitget v2). Reads balance/positions/ticker; places/closes market
orders. Keys from agents/.env: BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE / BITGET_DEMO=1.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests

BASE = "https://api.bitget.com"
DEMO_SYMBOLS = {"BTC": "SBTCSUSDT", "ETH": "SETHSUSDT", "XRP": "SXRPSUSDT"}  # the only demo SUSDT-FUTURES pairs
_ENV_LOADED = False


def _load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    _ENV_LOADED = True


def _env(k: str, default: str = "") -> str:
    _load_env()
    return os.environ.get(k, default).strip()


def is_demo() -> bool:
    return _env("BITGET_DEMO", "1").lower() in {"1", "true", "yes"}


def _product() -> str:
    return "SUSDT-FUTURES" if is_demo() else "USDT-FUTURES"


def _margin() -> str:
    return "SUSDT" if is_demo() else "USDT"


def to_symbol(asset: str) -> str | None:
    """Firm asset (e.g. 'BTC') -> Bitget symbol. Demo supports BTC/ETH/XRP only (None otherwise)."""
    a = asset.upper().replace("SUSDT", "").replace("USDT", "")
    return DEMO_SYMBOLS.get(a) if is_demo() else f"{a}USDT"


def supported(asset: str) -> bool:
    return to_symbol(asset) is not None


_SPEC_CACHE: dict = {}


def contract_spec(asset: str) -> dict | None:
    sym = to_symbol(asset)
    if not sym:
        return None
    if not _SPEC_CACHE:
        for c in (_request("GET", "/api/v2/mix/market/contracts", params={"productType": _product()}) or []):
            _SPEC_CACHE[c["symbol"]] = c
    return _SPEC_CACHE.get(sym)


def round_size(asset: str, usd: float, price: float) -> float:
    """Base-coin qty for ~`usd` notional at `price`, floored to the contract step (bumped to min)."""
    spec = contract_spec(asset)
    if not spec or price <= 0:
        return 0.0
    place = int(spec.get("volumePlace", 4) or 4)
    minq = float(spec.get("minTradeNum", 0) or 0)
    f = 10 ** place
    return max(int((usd / price) * f) / f, minq)


def _keys() -> tuple[str, str, str]:
    return _env("BITGET_API_KEY"), _env("BITGET_API_SECRET"), _env("BITGET_PASSPHRASE")


def _sign(ts: str, method: str, request_path: str, body: str = "") -> str:
    _, secret, _ = _keys()
    mac = hmac.new(secret.encode(), f"{ts}{method.upper()}{request_path}{body}".encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    method = method.upper()
    query = ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))) if params else ""
    request_path = path + query
    body_str = json.dumps(body) if body else ""
    key, _, passph = _keys()
    ts = str(int(time.time() * 1000))
    headers = {
        "ACCESS-KEY": key,
        "ACCESS-SIGN": _sign(ts, method, request_path, body_str),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passph,
        "Content-Type": "application/json",
        "locale": "en-US",
    }
    r = requests.request(method, BASE + request_path, headers=headers, data=(body_str or None), timeout=15)
    j = r.json()
    if str(j.get("code")) not in {"00000", "0"}:
        raise RuntimeError(f"Bitget {path} HTTP{r.status_code} code={j.get('code')} msg={j.get('msg')}")
    return j.get("data")


# ── reads ──────────────────────────────────────────────────────────────────
def accounts() -> list:
    return _request("GET", "/api/v2/mix/account/accounts", params={"productType": _product()}) or []


def get_balance() -> tuple[float, float]:
    for a in accounts():
        if a.get("marginCoin") == _margin():
            return float(a.get("available", 0) or 0), float(a.get("usdtEquity", a.get("accountEquity", 0)) or 0)
    return 0.0, 0.0


def account_summary() -> dict:
    """Fuller account read for the UI: available + equity + live unrealized P&L (account-level, from the API)."""
    for a in accounts():
        if a.get("marginCoin") == _margin():
            return {
                "available_usd": float(a.get("available", 0) or 0),
                "equity_usd": float(a.get("usdtEquity", a.get("accountEquity", 0)) or 0),
                "unrealized_pnl_usd": float(a.get("unrealizedPL", a.get("crossedUnrealizedPL", 0)) or 0),
            }
    return {"available_usd": 0.0, "equity_usd": 0.0, "unrealized_pnl_usd": 0.0}


def get_ticker(asset: str) -> float:
    sym = to_symbol(asset)
    if not sym:
        raise ValueError(f"{asset} not on Bitget demo (supports {list(DEMO_SYMBOLS)})")
    d = _request("GET", "/api/v2/mix/market/ticker", params={"symbol": sym, "productType": _product()})
    row = d[0] if isinstance(d, list) and d else (d or {})
    return float(row.get("lastPr") or row.get("last") or 0)


# ── public market data (keyless · MAINNET) — the firm's price/kline/funding feed, no API key needed ──
def _pub_symbol(asset: str) -> str:
    a = asset.upper().replace("SUSDT", "").replace("USDT", "")
    return f"{a}USDT"


def _public(path: str, params: dict | None = None):
    """Unsigned public GET (mainnet market data — no key)."""
    r = requests.get(BASE + path, params=params, timeout=15)
    j = r.json()
    if str(j.get("code")) not in {"00000", "0"}:
        raise RuntimeError(f"Bitget {path} code={j.get('code')} msg={j.get('msg')}")
    return j.get("data")


def market_snapshot(asset: str) -> dict:
    """Public mainnet ticker (keyless): last, funding rate, open interest, 24h volume + change."""
    sym = _pub_symbol(asset)
    d = _public("/api/v2/mix/market/ticker", {"symbol": sym, "productType": "usdt-futures"})
    t = (d[0] if isinstance(d, list) and d else (d or {})) or {}
    return {
        "asset": asset.upper(), "symbol": sym,
        "last": float(t.get("lastPr") or 0),
        "funding_rate": float(t.get("fundingRate") or 0),
        "open_interest": float(t.get("holdingAmount") or 0),
        "vol_24h": float(t.get("baseVolume") or 0),
        "change_24h_pct": round(float(t.get("change24h") or 0) * 100, 2),
        "source": "bitget-mainnet",
    }


def public_candles(asset: str, granularity: str = "1H", limit: int = 100) -> list:
    """Public mainnet OHLCV (keyless). Returns [{t,o,h,l,c,vol}]."""
    sym = _pub_symbol(asset)
    rows = _public("/api/v2/mix/market/candles",
                   {"symbol": sym, "productType": "usdt-futures", "granularity": granularity, "limit": str(limit)}) or []
    return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "vol": float(r[5])}
            for r in rows]


def get_positions() -> list:
    return _request("GET", "/api/v2/mix/position/all-position", params={"productType": _product(), "marginCoin": _margin()}) or []


# ── trade ──────────────────────────────────────────────────────────────────
def place_market_order(asset: str, side: str, size: float, reduce_only: bool = False) -> dict:
    """side 'buy'(long)/'sell'(short); size in base coin. One-way mode. Returns {orderId, ...}."""
    sym = to_symbol(asset)
    if not sym:
        raise ValueError(f"{asset} not tradable on Bitget demo (supports {list(DEMO_SYMBOLS)})")
    body = {
        "symbol": sym, "productType": _product(), "marginMode": "isolated", "marginCoin": _margin(),
        "size": str(size), "side": side.lower(), "orderType": "market",
        "reduceOnly": "YES" if reduce_only else "NO",
    }
    return _request("POST", "/api/v2/mix/order/place-order", body=body)


def flatten() -> list:
    """Close every open position with a market reduce-only order."""
    done = []
    for p in get_positions():
        size = float(p.get("total", 0) or 0)
        if size <= 0:
            continue
        sym = p.get("symbol", "")
        asset = next((a for a, s in DEMO_SYMBOLS.items() if s == sym), sym)
        side = "sell" if p.get("holdSide") == "long" else "buy"
        try:
            r = place_market_order(asset, side, size, reduce_only=True)
            done.append({"symbol": sym, "closed": size, "side": side, "orderId": r.get("orderId") if isinstance(r, dict) else r})
        except Exception as e:  # noqa: BLE001
            done.append({"symbol": sym, "error": str(e)[:120]})
    return done


def set_position_mode(one_way: bool = True) -> dict:
    """Set one-way (unilateral) vs hedge position mode for the product (fixes order code 40774)."""
    return _request("POST", "/api/v2/mix/account/set-position-mode",
                    body={"productType": _product(), "posMode": "one_way_mode" if one_way else "hedge_mode"})


def status() -> dict:
    out = {"demo": is_demo(), "product": _product(), "margin": _margin(), "symbols": list(DEMO_SYMBOLS), "keys_present": all(_keys())}
    try:
        avail, equity = get_balance()
        out.update(connected=True, available=avail, equity=equity)
    except Exception as e:  # noqa: BLE001
        out.update(connected=False, error=str(e)[:200])
    return out


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print("=== Bitget DEMO adapter ===")
    print(json.dumps(status(), indent=2))
    for a in ("BTC", "ETH"):
        try:
            print(f"{a} ({to_symbol(a)}) last:", get_ticker(a))
        except Exception as e:  # noqa: BLE001
            print(f"{a} ticker error:", str(e)[:120])
    if "--order" in sys.argv:
        try:
            try:
                set_position_mode(True)
            except Exception as e:  # noqa: BLE001
                print("set-mode note:", str(e)[:120])
            print("TEST ORDER:", json.dumps(place_market_order("BTC", "buy", 0.001), indent=2))
            print("positions:", json.dumps(get_positions(), indent=2)[:600])
        except Exception as e:  # noqa: BLE001
            print("order error:", str(e)[:200])
