"""Verify OpenRouter works before building the orchestrator.
Reads OPENROUTER_API_KEY from .env (never hardcoded), lists FREE models
(Kimi/DeepSeek/Qwen/GLM), then runs one real chat completion to confirm it replies.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
KEY = env.get("OPENROUTER_API_KEY", "")
print("key loaded:", "yes" if KEY else "NO")

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def is_free(m: dict) -> bool:
    try:
        p = m.get("pricing", {})
        return float(p.get("prompt", "1")) == 0 and float(p.get("completion", "1")) == 0
    except Exception:
        return False


# 1) list free models
r = requests.get("https://openrouter.ai/api/v1/models", headers=HEADERS, timeout=30)
print("models endpoint:", r.status_code)
data = r.json().get("data", [])
free = [m["id"] for m in data if is_free(m)]
print(f"total models {len(data)} | free {len(free)}")
preferred = (
    [i for i in free if re.search(r"kimi", i, re.I)]
    or [i for i in free if re.search(r"deepseek|qwen|glm", i, re.I)]
    or free
)
print("free candidates (top 8):", preferred[:8])

# 2) test one real completion
model = preferred[0] if preferred else None
if model:
    cr = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: HELIQUANT ONLINE"}],
            "max_tokens": 30,
        },
        timeout=90,
    )
    print(f"\ntest model: {model} | status {cr.status_code}")
    if cr.status_code == 200:
        print("reply:", cr.json()["choices"][0]["message"]["content"].strip())
    else:
        print("error:", cr.text[:300])
else:
    print("no free model found to test")
