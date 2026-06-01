"""Decisive test: does our Etherscan key have PRO access for `topholders` on Mantle (chainid 5000)?
This is the holders-list approach (top HOLDERS, not transaction churners) the user asked about.
Also reports holder TYPE (C=contract / blank=EOA) so we can filter contracts/CEX."""
import os
import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

KEY = os.environ.get("MANTLESCAN_API_KEY") or "ARKZEP422CW6ASTDKJ2AESEGUVKPHI16RF"
BASE = "https://api.etherscan.io/v2/api"

# 1) get real Mantle ERC-20 contract addresses from DexScreener
ds = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": "mETH"}, timeout=15).json()
addrs = {}
for p in ds.get("pairs", []):
    if p.get("chainId") == "mantle":
        s = p["baseToken"]["symbol"]
        if s in ("mETH", "WMNT", "cmETH") and s not in addrs:
            addrs[s] = p["baseToken"]["address"]
# also pull WMNT/cmETH via another search if missing
for q in ("WMNT", "cmETH"):
    if q not in addrs:
        d2 = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": q}, timeout=15).json()
        for p in d2.get("pairs", []):
            if p.get("chainId") == "mantle" and p["baseToken"]["symbol"] == q:
                addrs[q] = p["baseToken"]["address"]; break
print("Mantle ERC-20 addresses:", addrs)

# 2) topholders (PRO) test
for sym, addr in addrs.items():
    r = requests.get(BASE, params={"chainid": 5000, "module": "token", "action": "topholders",
                                    "contractaddress": addr, "offset": 25, "apikey": KEY}, timeout=30)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        print(f"\n{sym} topholders -> non-json: {r.text[:120]}"); continue
    print(f"\n{sym} ({addr}) topholders -> HTTP {r.status_code} status={j.get('status')} msg={j.get('message')}")
    res = j.get("result")
    if isinstance(res, list) and res:
        print(f"  ✅ {len(res)} holders returned (PRO WORKS):")
        for h in res[:12]:
            t = h.get("TokenHolderAddressType", "?")
            print(f"    {h.get('TokenHolderAddress')}  qty={h.get('TokenHolderQuantity')}  type={t}")
    else:
        print(f"  FAIL result: {str(res)[:200]}")
        print(f"  raw json:    {str(j)[:260]}")
