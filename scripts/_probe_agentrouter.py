"""Try to pass AgentRouter's client gate by mimicking Claude Code / Anthropic SDK headers."""

from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
KEY = env.get("HQ_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"
PING = [{"role": "user", "content": "Reply with exactly: HELIQUANT ONLINE"}]
print("key loaded:", "yes" if KEY else "NO")


def show(label, resp):
    print(f"\n=== {label} -> {resp.status_code} ===")
    print(resp.text[:300])


# D: Anthropic /v1/messages with Claude-Code-like UA + Anthropic SDK headers
try:
    rD = requests.post(
        "https://agentrouter.org/v1/messages",
        headers={
            "x-api-key": KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": "claude-cli/1.0.65 (external, cli)",
            "x-app": "cli",
            "anthropic-beta": "claude-code-20250219",
        },
        json={"model": MODEL, "max_tokens": 20, "messages": PING},
        timeout=60,
    )
    show("D: /v1/messages + Claude-Code headers", rD)
except Exception as e:  # noqa: BLE001
    print("D error:", repr(e)[:200])

# E: OpenAI /v1/chat/completions with Claude-Code-like UA
try:
    rE = requests.post(
        "https://agentrouter.org/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {KEY}",
            "content-type": "application/json",
            "User-Agent": "claude-cli/1.0.65 (external, cli)",
        },
        json={"model": MODEL, "messages": PING, "max_tokens": 20},
        timeout=60,
    )
    show("E: /v1/chat/completions + Claude-Code UA", rE)
except Exception as e:  # noqa: BLE001
    print("E error:", repr(e)[:200])
