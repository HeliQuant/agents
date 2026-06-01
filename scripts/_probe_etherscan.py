"""Probe Etherscan API v2 (multichain) for Mantle (chainid 5000) whale-tracking data."""
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
KEY = env.get("MANTLESCAN_API_KEY") or env.get("ETHERSCAN_API_KEY") or "ARKZEP422CW6ASTDKJ2AESEGUVKPHI16RF"
BASE = "https://api.etherscan.io/v2/api"
WMNT = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"   # wrapped MNT (ERC-20)
METH = "0xcDA86A272531e8640cD7F1a92c01839911B90bb0"   # mETH (ERC-20)
print("key loaded:", "yes" if KEY else "NO")


def get(params):
    params.update({"chainid": 5000, "apikey": KEY})
    return requests.get(BASE, params=params, timeout=25).json()


# 1) native balance of WMNT contract — confirms key + Mantle v2 works
b = get({"module": "account", "action": "balance", "address": WMNT, "tag": "latest"})
print("\n[balance] status:", b.get("status"), "| message:", b.get("message"), "| result:", str(b.get("result"))[:40])

# 2) recent WMNT ERC-20 transfers — the raw whale-tracking feed
tx = get({"module": "account", "action": "tokentx", "contractaddress": WMNT,
          "page": 1, "offset": 10, "sort": "desc"})
print("\n[tokentx WMNT] status:", tx.get("status"), "| message:", tx.get("message"))
res = tx.get("result")
if isinstance(res, list):
    print(f"  got {len(res)} transfers; newest few:")
    for t in res[:5]:
        try:
            val = int(t["value"]) / (10 ** int(t["tokenDecimal"]))
            print(f"   {t['from'][:12]}.. -> {t['to'][:12]}..  {val:,.2f} {t.get('tokenSymbol','')}  blk {t['blockNumber']}")
        except Exception as e:  # noqa: BLE001
            print("   parse err:", str(e)[:60])
else:
    print("  result:", str(res)[:160])
