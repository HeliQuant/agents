"""Verify candidate mETH whales' CURRENT balance (free tokenbalance) — fixes the truncated-reconstruction
unreliability. Drops any who've since sold; keeps real current EOA holders → the seed list to monitor.
Also pulls tx-count (nonce) to flag bot/CEX-like churners (huge nonce) vs real holders.
Run: python scripts/47_verify_whale_seed.py
"""
from __future__ import annotations

import json
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
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
METH = "0xcDA86A272531e8640cD7F1a92c01839911B90bb0"
PRICE = 2180.0

CANDIDATES = [
    # 14 EOA candidates from full(500k)-reconstruction + 1 prior-verified holder
    "0x588846213a30fd36244e0ae0ebb2374516da836c", "0x7427b4fd78974ba1c3b5d69e2f1b8acf654feb44",
    "0x3d2b93409174c9cf48fbe31447ef6fc738f0d7a2", "0x7fe2baffd481a8776a9ead15a8ed17fe37107903",
    "0x651fac183d2ac9753bec39f7530adf1b873f0314", "0x8f456e525ed0115e22937c5c8afac061cc697f21",
    "0xaf3116348d1536fbe53e6bc232646f0d3fcec534", "0x6cea03069a82943d7e20e256a2892b95b15bd6ef",
    "0x7a87ca4dd95e5dcf19923e897a96515e9fdee649", "0x8fba04c7900df7e3c30bca8335d04f55d980049d",
    "0x334f12afb7d8740868be04719639616533075234", "0x78e41df3514dd274f774a0af9d5c19fe7b89a13d",
    "0x44b91961c27efe1698091adcd94d2ffa7411e811", "0x1e4e8a18581f696b90e4bd21ec65c84c785ebf57",
    "0x537037c5ae805b9d4cecab5ee07f12a8e59a15b2",
]


def get(params):
    params.update({"chainid": 5000, "apikey": KEY})
    return requests.get(BASE, params=params, timeout=30).json()


def main():
    rows = []
    for a in CANDIDATES:
        jb = get({"module": "account", "action": "tokenbalance", "contractaddress": METH, "address": a, "tag": "latest"})
        try:
            bal = int(jb.get("result", "0")) / 1e18
        except (ValueError, TypeError):
            bal = 0.0
        time.sleep(0.2)
        jn = get({"module": "proxy", "action": "eth_getTransactionCount", "address": a, "tag": "latest"})
        try:
            nonce = int(jn.get("result", "0x0"), 16)
        except (ValueError, TypeError):
            nonce = -1
        time.sleep(0.2)
        rows.append({"address": a, "meth_now": round(bal, 3), "usd_now": round(bal * PRICE), "nonce": nonce})

    rows.sort(key=lambda r: r["meth_now"], reverse=True)
    print(f"{'address':44} {'mETH now':>10} {'USD now':>13} {'nonce':>8}  note")
    seed = []
    for r in rows:
        # real holder = still holds meaningfully + not an insane-nonce churner
        keep = r["meth_now"] >= 5 and r["nonce"] < 50000
        note = "SEED" if keep else ("sold/empty" if r["meth_now"] < 5 else "high-nonce churner?")
        if keep:
            seed.append(r)
        print(f"{r['address']} {r['meth_now']:>10,.2f} {r['usd_now']:>12,} {r['nonce']:>8}  {note}")

    out = ROOT / "data" / "whale_seed_meth.json"
    out.write_text(json.dumps({"token": "mETH", "contract": METH, "wallets": seed}, indent=2))
    held = sum(r["meth_now"] for r in seed)
    print(f"\n>>> {len(seed)} VERIFIED current EOA mETH whales (>=5 mETH, sane nonce) — total {held:,.0f} mETH (${held*PRICE:,.0f})")
    print(f"saved seed -> {out.name}  (next: monitor these via free tokentx polling)")


if __name__ == "__main__":
    main()
