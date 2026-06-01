"""Bootstrap a Mantle whale SEED LIST for FREE — reconstruct top holders from Transfer logs.

Top-holders API is PRO-gated, but `getLogs` (Transfer events) is FREE → we sum every transfer to
rebuild current balances = the web "Holders" tab, programmatically. Then classify EOA vs contract
(eth_getCode, free) so we can drop contracts/bridges/LPs and surface real wallet candidates.

Run: python scripts/46_mantle_holders.py mETH   (or cmETH)
"""
from __future__ import annotations

import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

KEY = os.environ.get("MANTLESCAN_API_KEY") or "ARKZEP422CW6ASTDKJ2AESEGUVKPHI16RF"
BASE = "https://api.etherscan.io/v2/api"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x0000000000000000000000000000000000000000"
TOKENS = {
    "mETH": ("0xcDA86A272531e8640cD7F1a92c01839911B90bb0", 18, 2180.0),
    "cmETH": ("0xE6829d9a7eE3040e1276Fa75293Bde931859e8fA", 18, 2177.0),
    "WMNT": ("0x78c1b0C915c4FAA5FffA6CAbf0219DA63d7f4cb8", 18, 0.647),
    "USDe": ("0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34", 18, 1.0),
    "FBTC": ("0xC96dE26018A54D51c097160568752c4E3BD6C364", 8, 74680.0),
}
MAX_PAGES = int(sys.argv[2]) if len(sys.argv) > 2 else 60


def get(params):
    params.update({"chainid": 5000, "apikey": KEY})
    return requests.get(BASE, params=params, timeout=40).json()


def is_contract(addr):
    j = get({"module": "proxy", "action": "eth_getCode", "address": addr, "tag": "latest"})
    code = j.get("result", "0x")
    return code not in ("0x", "", None)


def main():
    sym = (sys.argv[1] if len(sys.argv) > 1 else "mETH")
    token, dec, price = TOKENS[sym]
    print(f"Reconstructing {sym} holders from Transfer logs (free getLogs)...\n")
    bal: dict[str, int] = {}
    from_block, pages, total, last_block = 0, 0, 0, 0
    while pages < MAX_PAGES:
        j = get({"module": "logs", "action": "getLogs", "address": token,
                 "topic0": TRANSFER, "fromBlock": from_block, "toBlock": "latest"})
        if j.get("status") != "1":
            if pages == 0:
                print(f"  getLogs status0: msg={j.get('message')} res={str(j.get('result'))[:90]}")
            break
        logs = j.get("result", [])
        if not logs:
            break
        pages += 1
        total += len(logs)
        for lg in logs:
            tp = lg.get("topics", [])
            if len(tp) < 3:
                continue
            frm, to = "0x" + tp[1][-40:], "0x" + tp[2][-40:]
            data = lg.get("data", "0x")
            val = int(data, 16) if data not in ("0x", "", None) else 0
            if frm != ZERO:
                bal[frm] = bal.get(frm, 0) - val
            if to != ZERO:
                bal[to] = bal.get(to, 0) + val
            last_block = int(lg["blockNumber"], 16)
        if len(logs) < 1000:
            break
        from_block = last_block + 1
        time.sleep(0.25)

    truncated = pages >= MAX_PAGES
    print(f"fetched {total} Transfer events / {pages} pages | truncated={truncated} (balances {'UNRELIABLE if truncated' if truncated else 'full-history OK'})\n")
    top = sorted(bal.items(), key=lambda kv: kv[1], reverse=True)[:30]
    print(f"{'#':>2} {'address':44} {'balance':>16} {'USD':>14} {'type':>8}")
    eoa = []
    for i, (addr, b) in enumerate(top, 1):
        if b <= 0:
            continue
        qty = b / 10 ** dec
        contract = is_contract(addr)
        kind = "contract" if contract else "EOA"
        if not contract:
            eoa.append((addr, qty))
        print(f"{i:>2} {addr} {qty:>16,.2f} {qty*price:>13,.0f} {kind:>8}")
        time.sleep(0.2)
    print(f"\n>>> {len(eoa)} EOA (non-contract) candidates = potential real whales to seed + monitor.")
    for a, q in eoa[:15]:
        print(f"    {a}  {q:,.2f} {sym} (${q*price:,.0f})")


if __name__ == "__main__":
    main()
