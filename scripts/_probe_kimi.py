"""Probe why OpenRouter Kimi K2.6 deep calls fail (429? model gone? key issue?)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firm.llm_client import _complete_one, _free_pool, _provider

cfg = _provider("openrouter", "moonshotai/kimi-k2.6:free")
print("openrouter keys loaded:", len(cfg["keys"]), "| model_override:", cfg["model_override"])

pool = _free_pool(cfg["base"], cfg["key"])
print("free pool sample:", pool[:6])
print("kimi in free pool:", any("kimi" in m.lower() for m in pool))

try:
    txt, used = _complete_one('Reply with JSON {"ok":true}', "ping", 30, 0, True,
                              "openrouter", "moonshotai/kimi-k2.6:free")
    print("KIMI OK:", used, "|", txt[:60])
except Exception as e:  # noqa: BLE001
    print("KIMI FAIL:", str(e)[:220])
