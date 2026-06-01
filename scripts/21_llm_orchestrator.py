"""HeliQuant LLM Orchestrator MVP — a free LLM (Kimi K2.6 via OpenRouter) is the
fund-manager brain. It reads the VALIDATED quant tools' outputs, makes a disciplined
decision (ENTER / ABSTAIN), self-manages its data/tools, and explains itself.

PRINCIPLE: the LLM MANAGES; the validated tools COMPUTE. The LLM is given numbers by
the tools and reasons over them — it never invents numbers or predicts price itself.
That is why a *free* model can run the brain: the alpha lives in the tools.

Provider-agnostic: set HQ_MODEL / HQ_BASE_URL to use any OpenAI-compatible endpoint.

Run: python scripts/21_llm_orchestrator.py [TICKER]
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── config / secrets ────────────────────────────────────────────────────────
env = {}
for line in (ROOT.parent / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
KEY = env.get("OPENROUTER_API_KEY", "")
BASE_URL = os.environ.get("HQ_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.environ.get("HQ_MODEL", "moonshotai/kimi-k2.6:free")

spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)
from firm.asset_configs import get_config, is_validated  # noqa: E402

TREND = ("Trending_Up", "Trending_Down")

# ── TOOLS (deterministic, validated — these produce the numbers) ─────────────

def tool_market_state(ticker: str) -> dict:
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    adx_th = float(df["adx"].quantile(0.60))
    vol_th = float(df["volatility_10"].quantile(0.85))
    adx_hi = float(df["adx"].quantile(0.90))
    last = df.iloc[-1]
    regime = ma.detect_current(last, adx_th, vol_th)
    adx = float(last["adx"])
    rr = 2.0 + (3.0 - 2.0) * max(0.0, min(1.0, (adx - adx_th) / (adx_hi - adx_th + 1e-9)))
    track = {
        "MNT": "trend-following OOS-validated: +5.58% fixed / +7.51% dynamic-R:R, 58.8% win, 17 trades",
        "METH": "trend-following OOS-validated but THIN: +1.70%, 4 trades (low confidence)",
    }.get(ticker.upper(), "NOT OOS-validated for this asset")
    return {
        "ticker": ticker.upper(),
        "last_price": round(float(last["close"]), 4),
        "current_regime": regime,
        "adx": round(adx, 1),
        "adx_trending_threshold": round(adx_th, 1),
        "trend_following_applicable_now": regime in TREND,
        "recommended_dynamic_rr": round(rr, 2),
        "asset_is_oos_validated": is_validated(ticker),
        "validated_track_record": track,
        "data_bars_available": len(df),
    }


AVAILABLE_TOOLS = {
    "tool_market_state": "regime + ADX + dynamic R:R (already provided below)",
    "get_allora_macro": "live decentralised-AI BTC macro signal (call to add macro confirmation)",
    "scan_whale_flow / refresh_whale_watchlist": "on-chain Mantle smart-money flow; refresh if stale",
    "get_sentiment": "price/volume/trending proxy",
    "check_rugpull": "GoPlus token-safety hard veto",
    "run_walkforward": "OOS-validate a new strategy/config BEFORE trusting it",
}

SYSTEM = """You are HeliQuant's fund-manager brain: an autonomous, on-chain AI trading agent on Mantle.

You do NOT predict prices or compute numbers — your validated quant tools do that. Your job is to MANAGE:
read the tool outputs, make a disciplined decision, self-manage your tools/data, and explain yourself
so a human or a hackathon judge can audit it.

Rules:
- Only the trend-following strategy is OOS-validated, and only on validated assets. If trend-following is
  not applicable now (not a trending regime) or the asset is not validated, ABSTAIN — abstaining is a
  correct, disciplined outcome, not a failure.
- If trending: direction = LONG in Trending_Up, SHORT in Trending_Down. Use the tool's recommended_dynamic_rr.
- NEVER invent a number. Use only numbers from the tool outputs.
Respond with ONLY a JSON object:
{
  "decision": "ENTER" | "ABSTAIN",
  "direction": "LONG" | "SHORT" | "NONE",
  "strategy": "trend_following" | "none",
  "dynamic_rr": <number from tool or null>,
  "confidence": "low" | "medium" | "high",
  "reasoning": "<2-4 sentences, auditable>",
  "self_management": ["<recommended tool/data actions, e.g. 'call get_allora_macro to confirm', or 'refresh_whale_watchlist if signals stale'>"]
}"""


def llm(messages: list[dict]) -> str:
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "max_tokens": 900, "temperature": 0.3},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def main():
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "MNT").upper()
    state = tool_market_state(ticker)

    print("=" * 64)
    print(f"  HeliQuant LLM Orchestrator  |  brain: {MODEL}")
    print("=" * 64)
    print("\n[VALIDATED TOOL OUTPUT — deterministic, the LLM does NOT compute these]")
    print(json.dumps(state, indent=2))

    user = (
        f"Available tools you can recommend calling: {json.dumps(AVAILABLE_TOOLS, indent=2)}\n\n"
        f"Validated tool output for {ticker}:\n{json.dumps(state, indent=2)}\n\n"
        "Make your managerial decision now."
    )
    raw = llm([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])

    print("\n[LLM FUND-MANAGER DECISION — brain reasons over tool outputs]")
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            print(json.dumps(json.loads(m.group(0)), indent=2))
        except Exception:
            print(m.group(0))
    else:
        print(raw)


if __name__ == "__main__":
    main()
