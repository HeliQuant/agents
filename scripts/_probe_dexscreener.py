"""Does DexScreener cover Mantle + our core assets? (free, no key, 60 rpm)"""
import requests

BASE = "https://api.dexscreener.com"
for q in ("mETH", "MNT", "cmETH", "USDe"):
    try:
        j = requests.get(f"{BASE}/latest/dex/search", params={"q": q}, timeout=15).json()
    except Exception as e:  # noqa: BLE001
        print(f"q={q}: FAIL {e}"); continue
    pairs = [p for p in j.get("pairs", []) if p.get("chainId") == "mantle"]
    print(f"\nq={q!r}: {len(j.get('pairs', []))} pairs total | {len(pairs)} on MANTLE")
    for p in sorted(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0, reverse=True)[:5]:
        liq = (p.get("liquidity") or {}).get("usd")
        vol = (p.get("volume") or {}).get("h24")
        tx = (p.get("txns") or {}).get("h24") or {}
        print(f"  {p['baseToken']['symbol']:8}/{p['quoteToken']['symbol']:6} {p['dexId']:12} "
              f"${p.get('priceUsd')}  liq=${liq:,.0f}  vol24h=${vol:,.0f}  buys/sells24h={tx.get('buys')}/{tx.get('sells')}" if liq and vol else
              f"  {p['baseToken']['symbol']}/{p['quoteToken']['symbol']} {p['dexId']} ${p.get('priceUsd')}")
