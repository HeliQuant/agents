"""Research probe — explore the exchange TESTNET endpoints. PUBLIC (market) = no auth. PRIVATE = needs secret (HMAC).
NOTE: exploratory artifact. The testnet host below is the same venue the executor signs against (kept intact)."""
import os
import requests

BASE = "https://api-testnet.bybit.com"  # executor's testnet venue host — kept for parity with the executor
RO_KEY = os.environ.get("BYBIT_API_KEY_READONLY", "")   # testnet read-only key (config name shared with the executor)


def show(label, url, params):
    try:
        r = requests.get(url, params=params, timeout=15)
        j = r.json()
        print(f"\n=== {label} -> HTTP {r.status_code} retCode {j.get('retCode')} ({j.get('retMsg')}) ===")
        return j
    except Exception as e:  # noqa: BLE001
        print(f"\n=== {label} -> FAIL {type(e).__name__}: {str(e)[:60]} ===")
        return {}


# ---- PUBLIC (no auth needed) ----
print("################ PUBLIC ENDPOINTS (no key/secret needed) ################")

j = show("market/instruments-info (spot)", f"{BASE}/v5/market/instruments-info", {"category": "spot"})
syms = [x["symbol"] for x in j.get("result", {}).get("list", [])]
mantle = [s for s in syms if any(t in s for t in ("MNT", "METH", "FBTC", "CMETH", "USDE", "USDY"))]
print(f"   total spot symbols: {len(syms)} | Mantle-related: {mantle[:20]}")

j = show("market/tickers (MNTUSDT spot)", f"{BASE}/v5/market/tickers", {"category": "spot", "symbol": "MNTUSDT"})
for t in j.get("result", {}).get("list", [])[:1]:
    print(f"   last={t.get('lastPrice')} 24h%={t.get('price24hPcnt')} vol24h={t.get('volume24h')} turnover24h={t.get('turnover24h')}")

j = show("market/orderbook (MNTUSDT spot, depth 5)", f"{BASE}/v5/market/orderbook", {"category": "spot", "symbol": "MNTUSDT", "limit": 5})
ob = j.get("result", {})
print(f"   top bid: {ob.get('b', [['?','?']])[0]} | top ask: {ob.get('a', [['?','?']])[0]}")

j = show("market/kline (MNTUSDT 1h x3)", f"{BASE}/v5/market/kline", {"category": "spot", "symbol": "MNTUSDT", "interval": "60", "limit": 3})
for k in j.get("result", {}).get("list", [])[:3]:
    print(f"   {k}")

# linear (perps) — for leverage/short
j = show("market/instruments-info (linear perps) MNTUSDT", f"{BASE}/v5/market/instruments-info", {"category": "linear", "symbol": "MNTUSDT"})
for x in j.get("result", {}).get("list", [])[:1]:
    print(f"   {x.get('symbol')} status={x.get('status')} maxLev={x.get('leverageFilter', {}).get('maxLeverage')}")

# ---- PRIVATE (needs secret to sign) — demonstrate it requires auth ----
print("\n################ PRIVATE ENDPOINT (needs secret/HMAC) ################")
try:
    r = requests.get(f"{BASE}/v5/account/wallet-balance",
                     params={"accountType": "UNIFIED"},
                     headers={"X-BAPI-API-KEY": RO_KEY},  # key but NO signature
                     timeout=15)
    print(f"account/wallet-balance (key, NO signature) -> HTTP {r.status_code}: {str(r.json())[:140]}")
    print("   ^ expected to fail: private endpoints need X-BAPI-SIGN (HMAC of params w/ SECRET) + timestamp")
except Exception as e:  # noqa: BLE001
    print("FAIL:", str(e)[:80])
