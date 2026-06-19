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


def get_ticker(asset: str) -> float:
    sym = to_symbol(asset)
    if not sym:
        raise ValueError(f"{asset} not on Bitget demo (supports {list(DEMO_SYMBOLS)})")
    d = _request("GET", "/api/v2/mix/market/ticker", params={"symbol": sym, "productType": _product()})
    row = d[0] if isinstance(d, list) and d else (d or {})
    return float(row.get("lastPr") or row.get("last") or 0)


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
