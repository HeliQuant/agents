"""Nansen: validate key + check credits (funded?) + does smart-money netflow cover MANTLE?"""
import json
import sys
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]


def load_key():
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            for line in (ROOT / ".env").read_text(encoding=enc).splitlines():
                line = line.lstrip("﻿").strip()
                if line.startswith("NANSEN_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError, FileNotFoundError):
            continue
    return None


KEY = load_key()
BASE = "https://api.nansen.ai"
H = {"apikey": KEY, "Content-Type": "application/json"}
print(f"key: {KEY[:8]}..{KEY[-4:] if KEY else ''}\n")

for chain in ["mantle", "ethereum"]:
    body = {"chains": [chain], "pagination": {"page": 1, "per_page": 8},
            "order_by": [{"field": "net_flow_24h_usd", "direction": "DESC"}],
            "filters": {"include_native_tokens": True, "include_stablecoins": False}}
    try:
        r = requests.post(f"{BASE}/api/v1/smart-money/netflow", headers=H, json=body, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[{chain}] FAIL {str(e)[:60]}"); continue
    cred_r = r.headers.get("X-Nansen-Credits-Remaining") or r.headers.get("xnansencreditsremaining")
    cred_u = r.headers.get("X-Nansen-Credits-Used") or r.headers.get("xnansencreditsused")
    print(f"=== chain={chain} -> HTTP {r.status_code} | credits used={cred_u} remaining={cred_r} ===")
    if not r.ok:
        print(f"   {r.text[:200]}"); continue
    data = r.json().get("data", [])
    print(f"   {len(data)} tokens (smart-money netflow):")
    for t in data[:8]:
        print(f"     {t.get('token_symbol'):10} net24h ${t.get('net_flow_24h_usd'):>14,.0f}  net7d ${t.get('net_flow_7d_usd'):>14,.0f}  traders={t.get('trader_count')}")
