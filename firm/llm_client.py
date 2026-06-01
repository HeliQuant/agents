"""Provider-agnostic LLM client for HeliQuant — bring-your-own-key (BYO).

Two API shapes cover virtually every provider:
  - "openai"    : POST {base}/chat/completions, Authorization: Bearer
                  (OpenAI, OpenRouter, Groq, DeepSeek, Together, xAI, ... and most others)
  - "anthropic" : POST {base}/v1/messages, x-api-key + anthropic-version
                  (Anthropic direct)

Choose a provider with env `HQ_PROVIDER` (default: openrouter). Override the model with
`HQ_MODEL`, the base URL with `HQ_BASE_URL`, and the key with `HQ_API_KEY` (or just set the
provider's own *_API_KEY in .env). In production, an operator plugs in *their* provider —
no lock-in.

Reliability: rotates across a model pool (e.g. OpenRouter free), backs off on 429/5xx, and
skips null-content / non-JSON replies when `expect_json=True`.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

_ENV_CACHE: dict[str, str] = {}


def _load_env() -> dict[str, str]:
    if _ENV_CACHE:
        return _ENV_CACHE
    for base in (Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[1], Path.cwd()):
        p = base / ".env"
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    _ENV_CACHE.setdefault(k.strip(), v.strip())
            break
    return _ENV_CACHE


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _load_env().get(name) or default


def _collect_keys(key_env: str) -> list[str]:
    """Gather every key for a provider so we can rotate across them (the operator's
    'multiply your keys' trick to spread free-tier rate limits over several accounts):
      - HQ_API_KEY (explicit override; comma-separated allowed) first, then
      - the provider's own key + numbered variants: OPENROUTER_API_KEY, _2, _3, _4, _5
    Returns a de-duplicated, order-preserving list."""
    raw: list[str] = []
    hq = _env("HQ_API_KEY")
    if hq:
        raw.append(hq)
    for suffix in ("", "_2", "_3", "_4", "_5"):
        v = _env(key_env + suffix)
        if v:
            raw.append(v)
    seen, out = set(), []
    for item in raw:
        for k in item.split(","):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
    return out


PROVIDERS = {
    "openrouter": {"base": "https://openrouter.ai/api/v1", "style": "openai", "key_env": "OPENROUTER_API_KEY", "free_pool": True},
    "groq":       {"base": "https://api.groq.com/openai/v1", "style": "openai", "key_env": "GROQ_API_KEY", "models": ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.1-8b-instant"]},
    "openai":     {"base": "https://api.openai.com/v1", "style": "openai", "key_env": "OPENAI_API_KEY", "default_model": "gpt-4o-mini"},
    "deepseek":   {"base": "https://api.deepseek.com", "style": "openai", "key_env": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat"},
    "anthropic":  {"base": "https://api.anthropic.com", "style": "anthropic", "key_env": "ANTHROPIC_API_KEY", "default_model": "claude-haiku-4-5-20251001"},
}


def _provider(name: str | None = None, model: str | None = None) -> dict:
    explicit = name is not None
    name = (name or _env("HQ_PROVIDER") or "openrouter").lower()
    cfg = dict(PROVIDERS.get(name, PROVIDERS["openrouter"]))
    cfg["name"] = name
    if not explicit and _env("HQ_BASE_URL"):
        cfg["base"] = _env("HQ_BASE_URL")
    cfg["keys"] = _collect_keys(cfg["key_env"])
    cfg["key"] = cfg["keys"][0] if cfg["keys"] else ""
    cfg["model_override"] = model if model else (None if explicit else _env("HQ_MODEL"))
    return cfg


def _is_free(m: dict) -> bool:
    try:
        p = m.get("pricing", {})
        return float(p.get("prompt", "1")) == 0 and float(p.get("completion", "1")) == 0
    except Exception:
        return False


def _free_pool(base: str, key: str) -> list[str]:
    try:
        r = requests.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        free = [m["id"] for m in r.json().get("data", []) if _is_free(m)]
        order = ["kimi", "deepseek", "qwen", "llama", "glm", "mistral", "gemini", "gpt", "nemotron"]
        ranked = sorted(free, key=lambda i: next((k for k, p in enumerate(order) if p in i.lower()), 99))
        return ranked[:6]
    except Exception:
        return []


def _models(cfg: dict) -> list[str]:
    if cfg.get("model_override"):
        return [cfg["model_override"]]
    if cfg.get("free_pool"):
        pool = _free_pool(cfg["base"], cfg["key"])
        if pool:
            return pool
        return ["moonshotai/kimi-k2.6:free", "meta-llama/llama-3.3-70b-instruct:free"]  # free offline fallback
    if cfg.get("models"):
        return list(cfg["models"])
    return [cfg.get("default_model", "gpt-4o-mini")]


def _has_json(s: str) -> bool:
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return False
    try:
        json.loads(m.group(0))
        return True
    except Exception:
        return False


def _request(cfg: dict, key: str, model: str, system: str, user: str, max_tokens: int) -> requests.Response:
    if cfg["style"] == "anthropic":
        return requests.post(
            f"{cfg['base']}/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": user}], "temperature": 0.2},
            timeout=120,
        )
    return requests.post(
        f"{cfg['base']}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
              "max_tokens": max_tokens, "temperature": 0.2},
        timeout=120,
    )


def _extract(cfg: dict, j: dict) -> str | None:
    if cfg["style"] == "anthropic":
        return "".join(p.get("text", "") for p in j.get("content", []) if isinstance(p, dict)) or None
    msg = j["choices"][0].get("message", {})
    return msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content")


def _complete_one(system, user, max_tokens, start, expect_json, provider, model):
    cfg = _provider(provider, model)
    keys = cfg["keys"]
    if not keys:
        raise RuntimeError(f"no API key for provider '{cfg['name']}' — set {cfg['key_env']} (or HQ_API_KEY) in .env")
    models = _models(cfg)
    pool = models[start % len(models):] + models[:start % len(models)]
    last = "no attempts made"
    for model_ in pool:
        for ki, key in enumerate(keys):
            try:
                r = _request(cfg, key, model_, system, user, max_tokens)
                if r.status_code in (429, 500, 502, 503):
                    last = f"HTTP {r.status_code} on {model_} (key {ki + 1}/{len(keys)})"
                    continue  # a different key has its own quota; else fall through to next model
                r.raise_for_status()
                content = _extract(cfg, r.json())
                if not (isinstance(content, str) and content.strip()):
                    last = f"empty content from {model_}"
                    continue
                if expect_json and not _has_json(content):
                    last = f"no parseable JSON from {model_}"
                    break  # this model won't emit JSON; move to the next model
                return content, f"{cfg['name']}:{model_}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__} on {model_}: {str(e)[:50]}"
        time.sleep(1.5)  # gentle spacing between models (free-tier friendly)
    raise RuntimeError(f"all attempts failed ({last})")


def complete(system: str, user: str, max_tokens: int = 700, start: int = 0, expect_json: bool = False,
             provider: str | None = None, model: str | None = None, fallback: str | None = None) -> tuple[str, str]:
    """Return (content, 'provider:model'). Optionally route to a specific `provider`/`model`
    (e.g. heavy reasoning on a stronger model) with graceful `fallback` to another provider if
    the primary fails. Rotates across keys then models; skips 429/5xx / null / non-JSON."""
    try:
        return _complete_one(system, user, max_tokens, start, expect_json, provider, model)
    except Exception:  # noqa: BLE001
        if fallback and fallback != (provider or ""):
            return _complete_one(system, user, max_tokens, start, expect_json, fallback, None)
        raise


def active_provider_info() -> dict:
    cfg = _provider()
    return {
        "provider": cfg["name"],
        "base": cfg["base"],
        "style": cfg["style"],
        "has_key": bool(cfg["keys"]),
        "num_keys": len(cfg["keys"]),
        "models": _models(cfg) if cfg["keys"] else [],
    }
