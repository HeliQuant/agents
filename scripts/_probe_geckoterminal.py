"""Quick probe — does GeckoTerminal expose pagination / whale filter / older data?"""
import requests

POOL = "0xeafc4d6d4c3391cd4fc10c85d2f5f972d58c0dd5"  # USDe/WMNT 0.25%
URL = f"https://api.geckoterminal.com/api/v2/networks/mantle/pools/{POOL}/trades"

r = requests.get(URL, timeout=15)
data = r.json()["data"]
print(f"Page 1: {len(data)} trades")
print(f"  oldest:  {data[-1]['attributes']['block_timestamp']}")
print(f"  newest:  {data[0]['attributes']['block_timestamp']}")

# Filter by whale-size
r = requests.get(URL, params={"trade_volume_in_usd_greater_than": 1000}, timeout=15)
whales = r.json().get("data", [])
print()
print(f"Whale trades (>$1000): {len(whales)}")
if whales:
    sample = whales[0]["attributes"]
    print(f"  Sample whale: ${float(sample['volume_in_usd']):,.0f} by {sample['tx_from_address']}")

# Bigger threshold
r = requests.get(URL, params={"trade_volume_in_usd_greater_than": 10000}, timeout=15)
big = r.json().get("data", [])
print(f"Big whale trades (>$10K): {len(big)}")
if big:
    print(f"  Oldest in this filter: {big[-1]['attributes']['block_timestamp']}")

# Unique traders summary
addrs = set()
for t in data:
    addrs.add(t["attributes"]["tx_from_address"])
print()
print(f"Unique traders in latest 300 trades: {len(addrs)}")
