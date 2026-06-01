"""Whale Self-Management Agent — the LLM keeps the smart-money watchlist healthy.

This is HeliQuant's on-chain self-management loop (the agent maintaining its own
data sources, per the 'autonomous agent' vision):

  1. evaluate()  the current watchlist vs quality criteria          (deterministic)
  2. the LLM 'Whale Manager' agent decides MIGRATE or KEEP, and why  (judgement)
  3. if MIGRATE -> migrate() re-scans fresh on-chain activity + filters by criteria
  4. re-evaluate -> show the health improvement

HONEST: discovery uses real GeckoTerminal Mantle DEX trades (firm/sources/whale_indexer).
Numbers are computed by tools; the LLM only decides and explains.

Run: python scripts/23_whale_self_management.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm import whale_manager as wm  # noqa: E402
from firm.llm_client import active_provider_info, complete  # noqa: E402

WM_SYS = (
    "You are the On-chain Whale Manager at HeliQuant, an autonomous trading agent on Mantle. "
    "You maintain a watchlist of smart-money wallets the strategy mirrors. You are given a "
    "health report computed deterministically — NEVER invent or change numbers, only judge them. "
    "Decide whether to MIGRATE (re-scan fresh on-chain activity and replace underperformers) or "
    "KEEP the list. Migrate if the data is stale or too many wallets are losers / low quality — "
    "following stale or loss-making wallets is a mistake.\n"
    "Output ONLY one JSON object and nothing else, begin with { end with }:\n"
    '{"action":"migrate|keep","confidence":"low|medium|high","reason":"<2-3 sentences citing the numbers>"}'
)


def _hp(h: dict) -> str:
    return (f"tracked={h['total']} qualify={h['keep']} drop={h['drop']} "
            f"({int(h['keep_fraction'] * 100)}%) | losers={h['losers']} | "
            f"data_age={h['data_age_hours']}h stale={h['stale']}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    info = active_provider_info()
    print("=" * 70)
    print("  HeliQuant — Whale Watchlist Self-Management (on-chain autonomy)")
    print(f"  provider: {info['provider']} ({info['style']}) | keys: {info.get('num_keys', 0)}")
    print("=" * 70)

    health = wm.evaluate(wm.load())
    print("\n[1] CURRENT WATCHLIST HEALTH")
    print(f"    {_hp(health)}")
    for v in [v for v in health["verdicts"] if v["status"] == "drop"][:6]:
        print(f"      DROP {v['address'][:14]}..  {', '.join(v['reasons'])}")

    report = {k: health[k] for k in
              ("total", "keep", "drop", "keep_fraction", "losers", "data_age_hours", "stale", "needs_migration")}
    print("\n[2] WHALE MANAGER (LLM) DECIDING...")
    used = "fallback"
    try:
        txt, used = complete(WM_SYS, f"Watchlist health report:\n{json.dumps(report, indent=2)}\n\nDecide.",
                             max_tokens=500, expect_json=True)
        m = re.search(r"\{.*\}", txt, re.S)
        decision = json.loads(m.group(0)) if m else {"action": "keep", "reason": txt[:200]}
    except Exception as e:  # noqa: BLE001
        decision = {"action": "migrate" if health["needs_migration"] else "keep",
                    "confidence": "low",
                    "reason": f"LLM unavailable ({str(e)[:50]}); fell back to deterministic rule."}
    print(f"    -> {str(decision.get('action', '?')).upper()} (conf {decision.get('confidence', '?')}) [{used}]")
    print(f"    reason: {decision.get('reason', '')}")

    if str(decision.get("action", "")).lower() == "migrate":
        print("\n[3] MIGRATING — re-scanning fresh Mantle DEX activity (real GeckoTerminal)...")
        diff = wm.migrate()
        print(f"    scanned {diff['scanned']} fresh wallets -> {diff['after_count']} qualified & saved")
        print(f"    dropped={len(diff['dropped'])}  added={len(diff['added'])}  retained={len(diff['retained'])}")
        for a in diff["added"][:6]:
            print(f"      + {a[:14]}..")
        new_health = wm.evaluate(wm.load())
        print("\n[4] NEW WATCHLIST HEALTH (after migration)")
        print(f"    {_hp(new_health)}")
        print(f"\n    delta: qualify {int(health['keep_fraction'] * 100)}% -> {int(new_health['keep_fraction'] * 100)}%"
              f" | losers {health['losers']} -> {new_health['losers']}"
              f" | age {health['data_age_hours']}h -> {new_health['data_age_hours']}h")
    else:
        print("\n[3] No migration — watchlist healthy enough.")


if __name__ == "__main__":
    main()
