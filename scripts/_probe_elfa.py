"""Elfa: validate key + check if funded (credits) + see smart-social data. Try base/auth matrix."""
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
                if line.startswith("ELFA_API_KEY="):
                    return line.split("=", 1)[1].strip()
        except (UnicodeError, ValueError, FileNotFoundError):
            continue
    return None


KEY = load_key()
print(f"key: {KEY[:10]}..{KEY[-4:] if KEY else ''}\n")

bases = ["https://api.elfa.ai", "https://www.elfa.ai/api"]
auths = [("x-elfa-api-key", {"x-elfa-api-key": KEY}), ("Bearer", {"Authorization": f"Bearer {KEY}"})]
working = None
for base in bases:
    for name, hdr in auths:
        for ping in ("/v1/ping", "/v2/ping", "/ping"):
            try:
                r = requests.get(base + ping, headers=hdr, timeout=10)
            except Exception:  # noqa: BLE001
                continue
            if r.status_code in (200, 401, 403, 429):
                print(f"  {base}{ping} [{name}] -> {r.status_code}: {r.text[:80]}")
            if r.status_code == 200 and working is None:
                working = (base, name, hdr)

if not working:
    print("\nNo working base/auth/ping found — checking key-status directly on api.elfa.ai...")
    for name, hdr in auths:
        try:
            r = requests.get("https://api.elfa.ai/v1/key-status", headers=hdr, timeout=10)
            print(f"  key-status [{name}] -> {r.status_code}: {r.text[:200]}")
            if r.status_code == 200:
                working = ("https://api.elfa.ai", name, hdr)
        except Exception as e:  # noqa: BLE001
            print(f"  key-status [{name}] FAIL {str(e)[:40]}")

if working:
    base, name, hdr = working
    print(f"\n✓ WORKS: {base} [{name}]")
    for label, path, params in [
        ("key-status (CREDITS?)", "/v1/key-status", {}),
        ("trending-tokens", "/v1/trending-tokens", {"timeWindow": "24h", "pageSize": 5}),
        ("trending v2", "/v2/aggregations/trending-tokens", {"timeWindow": "24h"}),
    ]:
        try:
            r = requests.get(base + path, headers=hdr, params=params, timeout=15)
            print(f"\n{label} ({path}) -> {r.status_code}: {r.text[:280]}")
        except Exception as e:  # noqa: BLE001
            print(f"\n{label} FAIL {str(e)[:50]}")
