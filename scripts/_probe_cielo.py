"""Validate Cielo key + see what the Mantle smart-money feed looks like (FREE plan, frugal: ~10 credits).
/chains = 0 credits (validates key). /feed = 5 credits. tags/wallets NOT on Free (skip)."""
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def load_key():
    envp = ROOT / ".env"
    if not envp.exists():
        return None
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in envp.read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith("CIELO_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError):
            continue
    return None


KEY = load_key()
print("key loaded:", bool(KEY), f"(...{KEY[-4:]})" if KEY else "")
BASE = "https://feed-api.cielo.finance/api/v1"
H = {"X-API-KEY": KEY or ""}

# 1) chains — free, validates auth
r = requests.get(f"{BASE}/chains", headers=H, timeout=20)
j = r.json()
chains = j.get("data", []) if isinstance(j, dict) else []
print(f"\n/chains -> HTTP {r.status_code} status={j.get('status')} | mantle supported: {'mantle' in chains}")

# 2) feed filtered to Mantle — 5 credits
r = requests.get(f"{BASE}/feed", headers=H,
                 params={"chains": "mantle", "limit": 15, "minUSD": 50, "txTypes": "swap,transfer"}, timeout=40)
try:
    j = r.json()
except Exception:  # noqa: BLE001
    print("feed non-json:", r.text[:200]); raise SystemExit
print(f"\n/feed?chains=mantle -> HTTP {r.status_code} status={j.get('status')} msg={j.get('message')}")
items = (j.get("data") or {}).get("items", []) if isinstance(j.get("data"), dict) else []
print(f"items returned: {len(items)}")
if items:
    print("\nfirst raw item keys:", list(items[0].keys()))
    print("first raw item:", json.dumps(items[0], indent=2)[:900])
    print("\nsmart-money Mantle activity:")
    for it in items[:15]:
        usd = it.get("amount_usd") or it.get("token1_amount_usd") or it.get("token0_amount_usd") or it.get("value_usd")
        sym = it.get("token_symbol") or it.get("token1_symbol") or it.get("token0_symbol") or "?"
        print(f"  {it.get('tx_type'):10} {sym:8} ${usd}  wallet={it.get('wallet_label')}  is_sell={it.get('is_sell')}")

# 3) GLOBAL feed (no chain filter) — diagnose: does the feed return ANYTHING + which chains dominate?
from collections import Counter  # noqa: E402

r = requests.get(f"{BASE}/feed", headers=H, params={"limit": 25, "minUSD": 1000}, timeout=40)
j = r.json()
gitems = (j.get("data") or {}).get("items", []) if isinstance(j.get("data"), dict) else []
print(f"\n/feed GLOBAL (no chain, minUSD 1000) -> HTTP {r.status_code} status={j.get('status')} | items: {len(gitems)}")
chains_seen = Counter(it.get("chain") for it in gitems)
print("  chains in global smart-money feed:", dict(chains_seen))
print("  -> if global has items but mantle=0: Cielo smart-money is thin on Mantle (honest).")
print("  -> if global also 0: account has no tracked wallets/lists yet (needs setup).")
