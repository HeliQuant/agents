"""Which market-data APIs are reachable from here + have OHLCV+volume (incl. MNT)?"""
import requests

TESTS = [
    ("Exchange (Bitget)", "https://api.bitget.com/api/v2/mix/market/candles", {"symbol": "BTCUSDT", "productType": "usdt-futures", "granularity": "1H", "limit": 2}),
    ("Binance", "https://api.binance.com/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 2}),
    ("Binance MNT", "https://api.binance.com/api/v3/klines", {"symbol": "MNTUSDT", "interval": "1h", "limit": 2}),
    ("OKX", "https://www.okx.com/api/v5/market/candles", {"instId": "BTC-USDT", "bar": "1H", "limit": "2"}),
    ("OKX MNT", "https://www.okx.com/api/v5/market/candles", {"instId": "MNT-USDT", "bar": "1H", "limit": "2"}),
    ("Kraken", "https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": "60"}),
    ("CryptoCompare", "https://min-api.cryptocompare.com/data/v2/histohour", {"fsym": "BTC", "tsym": "USD", "limit": 2}),
    ("CryptoCompare MNT", "https://min-api.cryptocompare.com/data/v2/histohour", {"fsym": "MNT", "tsym": "USD", "limit": 2}),
    ("CoinGecko", "https://api.coingecko.com/api/v3/coins/mantle/ohlc", {"vs_currency": "usd", "days": "1"}),
]

for name, url, params in TESTS:
    try:
        r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "heliquant/1.0"})
        sample = str(r.json())[:80]
        print(f"{name:18} HTTP {r.status_code}  {sample}")
    except Exception as e:  # noqa: BLE001
        print(f"{name:18} FAIL  {type(e).__name__}: {str(e)[:60]}")
