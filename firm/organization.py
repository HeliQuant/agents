"""HeliQuant — Autonomous Intelligence Organization.

One self-running 'firm of AI agents' for on-chain trading on Mantle, run as a single
orchestrated loop. Every desk produces a REAL analytical output (no placeholders); the
Portfolio Manager synthesizes ALL of them into the most rational decision.

  PHASE 0 · OPERATIONS (data steward)   — keeps the org's own data fresh (whale watchlist;
                                          migrates via real GeckoTerminal when stale/bad)
  PHASE 1 · ANALYSIS (5 specialist desks, each a different model):
      1. Regime/Technical  — XGBoost regime engine (82.6% OOS) + ADX + dynamic R:R + honest
                             OOS-validation status
      2. Macro (Allora)    — REAL Allora decentralised-AI BTC+ETH 8h price prediction (topics 14, 9)
      3. On-chain/Risk     — whale watchlist health + REAL GoPlus token-security (WMNT)
      4. Research          — external macro via free public APIs (Fear&Greed + global market)
  (Sentiment desk dropped 2026-06-01 — redundant with regime momentum + whale mood, validated near-noise.)
  PHASE 2 · DECISION (portfolio manager) — synthesize all 5 desks -> ENTER/ABSTAIN, strictly
                                          within OOS-validated rules.

HONEST BOUNDARY: deterministic tools + real external APIs compute the numbers; the LLM agents
interpret and the head decides. No trend-following TRADING edge survived OOS (full-history +
rolling walk-forward) — so the org reads regime accurately and stays disciplined (abstains)
when no validated edge applies. We never claim profit we can't prove.

Provider-agnostic (bring-your-own-key) via firm.llm_client.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firm import whale_manager as wm  # noqa: E402
from firm import nansen_client as nansen  # noqa: E402
from firm import elfa_client as elfa  # noqa: E402
from firm import trade_ticket as tt  # noqa: E402
from firm.asset_configs import get_config, is_validated  # noqa: E402
from firm.llm_client import active_provider_info, complete  # noqa: E402

_spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)

_spec_sm = importlib.util.spec_from_file_location("sme", ROOT / "scripts" / "51_smart_money_engine.py")
sme = importlib.util.module_from_spec(_spec_sm)
_spec_sm.loader.exec_module(sme)

TREND = ("Trending_Up", "Trending_Down")
WMNT_ADDRESS = "0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8"  # ERC-20 rep of MNT on Mantle

# Model routing: heavy/deep reasoning -> OpenRouter Kimi K2.6 (3 keys); light tasks -> Groq default.
DEEP_PROVIDER, DEEP_MODEL = "openrouter", "moonshotai/kimi-k2.6:free"


def deep(system: str, user: str, **kw) -> tuple[str, str]:
    """Heavy-reasoning route. DEFAULT: Groq gpt-oss-120b (free, reliable, strongest Groq model).
    Kimi K2.6 is Plan B — set env HQ_USE_KIMI=1 to prefer OpenRouter Kimi (needs credit/quota)."""
    import os
    if os.environ.get("HQ_USE_KIMI"):
        try:
            return complete(system, user, provider=DEEP_PROVIDER, model=DEEP_MODEL, **kw)
        except Exception:  # noqa: BLE001
            pass
    try:
        return complete(system, user, provider="groq", model="openai/gpt-oss-120b", **kw)
    except Exception:  # noqa: BLE001
        return complete(system, user, provider="groq", **kw)  # rotating Groq pool if gpt-oss-120b rate-limited


# ────────────────────────────── deterministic / real-source tools ──────────────────────────────

def _state(ticker: str):
    df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_features.csv").dropna().reset_index(drop=True)
    adx_th = float(df["adx"].quantile(0.60))
    vol_th = float(df["volatility_10"].quantile(0.85))
    adx_hi = float(df["adx"].quantile(0.90))
    last = df.iloc[-1]
    regime = ma.detect_current(last, adx_th, vol_th)
    adx = float(last["adx"])
    rr = 2.0 + (3.0 - 2.0) * max(0.0, min(1.0, (adx - adx_th) / (adx_hi - adx_th + 1e-9)))
    return df, last, regime, adx, adx_th, round(rr, 2)


def tool_regime(ticker: str) -> dict:
    df, last, regime, adx, adx_th, rr = _state(ticker)
    track = (
        "NO validated trend-following edge — full-history (2.9y) walk-forward + rolling re-tune "
        "both flat/negative OOS; the earlier +5.58% was a short-window artifact. The regime "
        "classifier IS accurate (82.6% OOS), but accurate regime != trading profit. Discipline: "
        "abstain unless a genuine validated edge applies."
    )
    return {
        "ticker": ticker.upper(),
        "last_price": round(float(last["close"]), 4),
        "current_regime": regime,
        "adx": round(adx, 1),
        "trending_threshold": round(adx_th, 1),
        "trend_following_applicable_now": regime in TREND,
        "recommended_dynamic_rr": rr,
        "regime_classifier_oos_accuracy": "82.6% (91.6% when confident)",
        "oos_validated_trading_edge": is_validated(ticker),
        "status": track,
    }


def _allora_one(fetch) -> dict | None:
    try:
        import asyncio
        sig = asyncio.run(fetch())
        if sig.predicted_usd <= 0 or sig.spot_usd <= 0 or abs(sig.predicted_return) > 0.5:
            return None  # implausible/empty inference (e.g. ETH topic returned 0) -> treat as unavailable
        return {
            "spot_usd": round(sig.spot_usd, 0),
            "predicted_8h_usd": round(sig.predicted_usd, 0),
            "predicted_return_pct": round(sig.predicted_return * 100, 3),
            "direction": "bullish" if sig.direction_bull else ("bearish" if sig.direction_bear else "neutral"),
        }
    except Exception:  # noqa: BLE001
        return None


def tool_macro() -> dict:
    """Macro desk — REAL Allora decentralised-AI 8h predictions (BTC topic 14, ETH topic 9)."""
    from firm.allora_client import fetch_btc_macro_signal, fetch_eth_macro_signal
    btc = _allora_one(fetch_btc_macro_signal)
    eth = _allora_one(fetch_eth_macro_signal)
    if btc or eth:
        return {
            "source": "Allora decentralised-AI 8h price prediction (BTC topic 14, ETH topic 9)",
            "btc": btc, "eth": eth,
            "btc_mnt_8h_correlation": 0.64,
            "note": "Real on-chain decentralised-AI signal. MNT empirically tracks BTC (corr ~0.64).",
        }
    try:
        df, last, regime, adx, _, _ = _state("BTC")
        ret = round((float(last["close"]) / float(df["close"].iloc[-25]) - 1) * 100, 2) if len(df) > 25 else None
        return {"source": "FALLBACK BTC-regime proxy (Allora unavailable)",
                "btc_regime": regime, "btc_recent_24bar_return_pct": ret, "btc_mnt_8h_correlation": 0.64}
    except Exception as e2:  # noqa: BLE001
        return {"available": False, "note": f"macro unavailable: {str(e2)[:60]}"}


def _whale_divergence(net_bias: str, price_ret_pct: float) -> str:
    """The high-value angle: does smart-money flow CONFIRM or DIVERGE from price?"""
    up, down = price_ret_pct > 1.0, price_ret_pct < -1.0
    if net_bias == "accumulating":
        return ("BULLISH divergence (whales buying weakness)" if down
                else "confirms uptrend (whales + price up)" if up
                else "accumulation in flat price (early-bullish?)")
    if net_bias == "distributing":
        return ("BEARISH divergence (whales selling into strength)" if up
                else "confirms downtrend (whales + price down)" if down
                else "distribution in flat price (early-bearish?)")
    return "neutral (no clear smart-money lean)"


def tool_onchain_risk(ticker: str) -> dict:
    out = {"risk_profile": "regime classifier 82.6% OOS; org abstains when no validated edge applies"}
    # REAL GoPlus token-security (WMNT = ERC-20 representation of MNT on Mantle)
    try:
        from firm.sources.rugpull import RugpullScreener
        ra = RugpullScreener().assess(WMNT_ADDRESS)
        out["token_security"] = {
            "token": "WMNT", "source": "MOCK" if ra.is_mocked else "GoPlus live",
            "severity": ra.severity, "risk_score": round(ra.risk_score, 2),
            "honeypot": ra.is_honeypot, "trade_safe": ra.trade_safe, "note": ra.reasoning[:100],
        }
    except Exception as e:  # noqa: BLE001
        out["token_security"] = {"note": f"GoPlus unavailable: {str(e)[:50]}"}
    # whale watchlist health + decision-ready FLOW summary + divergence-vs-price
    try:
        whales = wm.load()
        h = wm.evaluate(whales)
        out["whale_health"] = {"qualify_pct": int(h["keep_fraction"] * 100), "losers": h["losers"],
                               "data_age_hours": h["data_age_hours"], "stale": h["stale"]}
        flow = wm.whale_flow_summary(whales)
        out["whale_flow"] = flow
        try:
            df, last, _, _, _, _ = _state(ticker)
            ret24 = (float(last["close"]) / float(df["close"].iloc[-25]) - 1) * 100 if len(df) > 25 else 0.0
            out["price_recent_24bar_pct"] = round(ret24, 2)
            out["whale_divergence_vs_price"] = _whale_divergence(flow.get("net_bias", "neutral"), ret24)
        except Exception:  # noqa: BLE001
            pass
        out["whale_methodology"] = ("smart-money USD-flow from GeckoTerminal Mantle DEX (~24h, 5 pools, L3); "
                                    "flow aggregate is clean but LAGGED + UNVALIDATED (forward-log via scripts/30) "
                                    "-> positioning/divergence CONTEXT, not a timing trigger")
    except Exception as e:  # noqa: BLE001
        out["whale_note"] = f"watchlist unavailable: {str(e)[:60]}"
    return out


def tool_research() -> dict:
    """Research desk — external macro context via free public APIs (the public-apis catalog)."""
    out: dict = {"sources": []}
    try:
        d = requests.get("https://api.alternative.me/fng/", timeout=10).json()["data"][0]
        out["fear_greed_index"] = {"value": int(d["value"]), "label": d["value_classification"]}
        out["sources"].append("alternative.me Fear&Greed")
    except Exception as e:  # noqa: BLE001
        out["fear_greed_index"] = {"error": str(e)[:40]}
    try:
        g = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json().get("data", {})
        out["global_market"] = {
            "total_mcap_change_24h_pct": round(float(g.get("market_cap_change_percentage_24h_usd", 0)), 2),
            "btc_dominance_pct": round(float(g.get("market_cap_percentage", {}).get("btc", 0)), 1),
            "active_cryptos": g.get("active_cryptocurrencies"),
        }
        out["sources"].append("CoinGecko global")
    except Exception as e:  # noqa: BLE001
        out["global_market"] = {"error": str(e)[:40]}
    try:
        tr = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10).json()
        out["trending_coins"] = [c["item"]["symbol"] for c in tr.get("coins", [])[:5]]
        out["sources"].append("CoinGecko trending")
    except Exception as e:  # noqa: BLE001
        out["trending_coins"] = {"error": str(e)[:40]}
    try:
        pr = requests.get("https://api.coingecko.com/api/v3/simple/price",
                          params={"ids": "bitcoin,ethereum,mantle", "vs_currencies": "usd",
                                  "include_24hr_change": "true"}, timeout=10).json()
        out["majors_24h_change_pct"] = {k: round(float(v.get("usd_24h_change", 0)), 2) for k, v in pr.items()}
        out["sources"].append("CoinGecko prices")
    except Exception as e:  # noqa: BLE001
        out["majors_24h_change_pct"] = {"error": str(e)[:40]}
    out["note"] = "External macro research via free public APIs — extensible from the public-apis catalog."
    return out


def tool_smartmoney(ticker: str) -> dict:
    """Smart-Money Flow desk — the Dynamic Smart-Money Flow Engine (scripts/51). Auto mode per asset:
    CONTRACT (Mantle: on-chain DEX flow_bias + liquidity + mETH staking conviction) or POSITIONING
    (majors: perp OI/funding/long-short, contrarian-to-crowding). Aggregate (manipulation-robust),
    forward-logged -> CONTEXT for the PM, not a claimed alpha."""
    sym = {"MNT": "WMNT"}.get(ticker.strip().upper(), ticker.strip())
    mode = None
    for k, v in sme.MODE.items():  # case-insensitive canonical lookup
        if k.upper() == sym.upper():
            sym, mode = k, v
            break
    if mode is None:
        mode = "POSITIONING" if sym.upper() in ("BTC", "ETH", "SOL") else "CONTRACT"
    try:
        sig = sme.contract_signal(sym) if mode == "CONTRACT" else sme.positioning_signal(sym)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "mode": mode, "note": f"smart-money engine error: {str(e)[:60]}"}
    if not sig:
        return {"available": False, "mode": mode, "note": "no smart-money data for this asset"}
    clean = lambda s: re.sub(r"[^\x00-\x7f]+", " ", str(s)).strip()  # strip emojis/non-ascii for safe logging
    macro_sm = nansen.smart_money_netflow(["ethereum"])  # Nansen macro smart-money (cached; MNT ~0.64 corr to majors)
    return {
        "asset": sym, "mode": mode, "flow_score_0_100": sig["score"], "read": clean(sig["label"]),
        "detail": clean(sig["detail"]),
        "macro_smart_money_nansen": ({"read": macro_sm.get("read"), "net24h_total_usd": macro_sm.get("net24h_total_usd"),
                                      "top_funds_flow": macro_sm.get("top", [])[:4], "credits_left": macro_sm.get("credits_remaining")}
                                     if macro_sm.get("available") else {"note": macro_sm.get("note")}),
        "methodology": ("CONTRACT mode = Mantle on-chain DEX flow_bias + liquidity + mETH staking conviction; "
                        "POSITIONING mode = perp OI/funding/long-short (contrarian-to-crowding). "
                        "macro_smart_money = Nansen funds' real netflow on ETH (macro risk-on/off context). "
                        "Aggregate (manipulation-robust), FORWARD-LOGGED -> CONTEXT, not a timing trigger."),
    }


def tool_smartsocial(ticker: str) -> dict:
    """Smart-Social desk — Elfa narrative/mindshare intelligence (smart accounts, not retail noise).
    Where crypto ATTENTION is rotating + whether our asset is in the conversation. CONTEXT, forward-logged."""
    tr = elfa.trending(time_window="24h", limit=12)
    if not tr.get("available"):
        return {"available": False, "note": tr.get("note", "Elfa unavailable")}
    trending = tr.get("trending", [])
    asset_l = ticker.lower().replace("wmnt", "mnt")
    in_trend = next((x for x in trending if (x.get("token") or "").lower() in (asset_l, "mantle", "meth", "cmeth")), None)
    return {
        "asset": ticker.upper(),
        "asset_in_top_trending": bool(in_trend),
        "asset_mindshare": in_trend or "not in top trending (low social mindshare — expected for a Mantle-eco asset)",
        "top_narratives_24h": trending[:6],
        "methodology": ("Elfa smart-social mindshare (Twitter/X + Telegram, SMART accounts not retail noise). "
                        "Detects where attention/narratives rotate (early-rotation signal). CONTEXT for the PM; "
                        "Mantle-eco social mindshare is typically thin -> mainly a majors/narrative context."),
    }


def tool_oi_contrarian(ticker: str) -> dict:
    """OI-Contrarian desk — HeliQuant's ONE OOS-validated trading edge. Combines the validated-edge
    registry (data/validated_edges.json, from scripts/39's cost-aware OOS backtest) with the LIVE
    OI-contrarian signal (perp OI 24h-change quintiles). When the asset HAS a validated edge AND the
    signal fires, this is a genuine, backtested reason to ENTER — the disciplined exception to abstain."""
    sym = {"WMNT": "MNT"}.get(ticker.strip().upper(), ticker.strip().upper())
    try:
        edges = json.loads((ROOT / "data" / "validated_edges.json").read_text())
    except (FileNotFoundError, ValueError):
        edges = {}
    e = edges.get(sym)
    sig = _oi_contrarian_signal(sym)
    if not e:
        return {"asset": sym, "edge_validated": False, "actionable": False, "live_signal": sig,
                "read": f"NO OOS-validated edge for {sym}; live OI signal={sig or 'none'} = context only, NOT tradeable.",
                "methodology": "Fade perp OI 24h-change extremes; only tradeable where it survives cost-aware OOS."}
    actionable = sig is not None
    return {
        "asset": sym, "edge_validated": True, "actionable": actionable, "live_signal": sig,
        "edge_stats": {"p_win": e["p_win"], "payoff_b": e["payoff_b"], "sample_n": e["sample_n"],
                       "oos_roi_pct": e["oos_roi_pct"]},
        "read": (f"VALIDATED OI-contrarian edge (p_win {e['p_win']}, payoff {e['payoff_b']}, n {e['sample_n']}, "
                 f"OOS {e['oos_roi_pct']}%). " + (f"ACTIONABLE: live fade-the-crowd {sig} setup firing now."
                 if actionable else "No OI extreme right now -> wait/abstain until the signal fires.")),
        "caveat": "Hedge-like, bear-amplified, small-sample (n=34), inconsistent fold-to-fold — fractional-Kelly sized, NOT a guarantee.",
        "methodology": "Fade perp OI 24h-change extremes (top quintile->SHORT, bottom->LONG); the one edge surviving cost-aware OOS.",
    }


def parse_json(txt: str) -> dict:
    if not isinstance(txt, str) or not txt.strip():
        return {"raw": ""}
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {"raw": m.group(0)[:400]}
    return {"raw": txt.strip()[:400]}


# ────────────────────────────── agent system prompts ──────────────────────────────

STEWARD_SYS = (
    "You are the On-chain Data Steward at HeliQuant, an autonomous trading organization on Mantle. "
    "You maintain the smart-money whale watchlist the strategy mirrors. You receive a health report "
    "computed deterministically — NEVER invent numbers, only judge them. Decide MIGRATE (re-scan fresh "
    "on-chain activity, replace underperformers) or KEEP. Migrate if data is stale or too many wallets "
    "are losers/low-quality; following stale or loss-making wallets is a mistake.\n"
    "Output ONLY one JSON object, begin with { end with }:\n"
    '{"action":"migrate|keep","confidence":"low|medium|high","reason":"<1-2 sentences citing the numbers>"}'
)

ANALYST_SYS = (
    "You are a specialist analyst at HeliQuant, an autonomous on-chain AI trading firm on Mantle. "
    "You receive your domain's tool output — real/deterministic. NEVER invent or change those numbers; "
    "only interpret them. Be concise and honest.\n"
    "Output ONLY one JSON object and NOTHING else — no preamble, no step-by-step thinking, no markdown. "
    "Begin your reply with { and end with }.\n"
    'Shape: {"stance":"bullish|bearish|neutral|avoid","confidence":"low|medium|high",'
    '"key_point":"<1-2 sentences citing the tool numbers>"}'
)

PM_SYS = (
    "You are the Portfolio Manager (head) of HeliQuant. Seven specialist desks each gave a domain view "
    "(Regime/Technical, Macro/Allora, On-chain/Risk, Research, Smart-Money Flow, Smart-Social, OI-Contrarian) plus the validated "
    "engine's status, plus a BULL-vs-BEAR researcher debate. Synthesize EVERYTHING and DECIDE the most rational action. Rules:\n"
    "- Trend-following has NO OOS-validated edge (full-history + rolling WF failed); a pure trend thesis -> ABSTAIN. "
    "The regime read can still be accurate.\n"
    "- THE ONE EXCEPTION (validated edge): if the OI-Contrarian desk has edge_validated=true AND actionable=true, an ENTER in "
    "its live_signal direction IS justified — our backtested edge is firing. Size/levels/risk are computed downstream.\n"
    "- Weigh dissent across desks; a hard rugpull veto (trade_safe=false) forces ABSTAIN.\n"
    "- NEVER invent numbers; reference the desks.\n"
    "Output ONLY one JSON object and NOTHING else — begin with { end with }.\n"
    'Shape: {"decision":"ENTER|ABSTAIN","direction":"LONG|SHORT|NONE",'
    '"confidence":"low|medium|high","reasoning":"<2-4 sentences synthesizing the desks>",'
    '"desk_consensus":"<one line: who agreed/dissented>","self_management":["<tool/data action>"]}'
)

BULL_SYS = (
    "You are the BULL researcher at HeliQuant. Using ONLY the desk outputs given, argue the strongest "
    "HONEST case to ENTER (and which direction), citing desk numbers. Concede where signals are weak or "
    "unvalidated — no hype.\nOutput ONE JSON, begin { end }: "
    '{"case":"long|short|no-trade","strongest_points":["..."],"honest_caveats":["..."]}'
)

BEAR_SYS = (
    "You are the BEAR / RISK researcher at HeliQuant. Using ONLY the desk outputs given, argue the case to "
    "ABSTAIN / protect capital, citing desk numbers and the no-validated-edge reality.\n"
    "Output ONE JSON, begin { end }: "
    '{"case":"abstain|short","strongest_points":["..."],"risks":["..."]}'
)

BULL_REBUT_SYS = (
    "You are the BULL researcher. You will see the BEAR's argument. Rebut it HONESTLY using the desk "
    "outputs — concede valid points, defend only where the data supports you.\n"
    "Output ONE JSON, begin { end }: {\"rebuttal\":\"<2-3 sentences>\",\"conceded\":[\"...\"]}"
)

BEAR_REBUT_SYS = (
    "You are the BEAR/RISK researcher. You will see the BULL's argument. Rebut it HONESTLY using the desk "
    "outputs, emphasizing capital protection and the no-validated-edge reality.\n"
    "Output ONE JSON, begin { end }: {\"rebuttal\":\"<2-3 sentences>\",\"conceded\":[\"...\"]}"
)


# ────────────────────────────── org phases ──────────────────────────────

def run_operations(verbose: bool = True) -> dict:
    """PHASE 0 — data steward keeps the whale watchlist healthy (self-maintenance)."""
    health = wm.evaluate(wm.load())
    report = {k: health[k] for k in
              ("total", "keep", "drop", "keep_fraction", "losers", "data_age_hours", "stale", "needs_migration")}
    if verbose:
        print("\n▶ PHASE 0 · OPERATIONS (data steward)")
        print(f"   whale watchlist: {health['total']} tracked, {int(health['keep_fraction']*100)}% qualify, "
              f"{health['losers']} losers, {health['data_age_hours']}h old (stale={health['stale']})")
    used = "fallback"
    try:
        txt, used = complete(STEWARD_SYS, f"Watchlist health report:\n{json.dumps(report, indent=2)}\n\nDecide.",
                             max_tokens=400, expect_json=True)
        decision = parse_json(txt)
    except Exception as e:  # noqa: BLE001
        decision = {"action": "migrate" if health["needs_migration"] else "keep", "confidence": "low",
                    "reason": f"LLM unavailable ({str(e)[:50]}); deterministic rule applied."}
    action = str(decision.get("action", "")).lower()
    if verbose:
        print(f"   steward [{used}] -> {action.upper()} (conf {decision.get('confidence', '?')}): {decision.get('reason', '')}")
    migrated, after = None, None
    if action == "migrate":
        if verbose:
            print("   migrating — re-scanning fresh Mantle DEX activity (real GeckoTerminal)...")
        migrated = wm.migrate()
        after = wm.evaluate(wm.load())
        if verbose:
            print(f"   result: scanned {migrated['scanned']} → {migrated['after_count']} qualified "
                  f"(dropped {len(migrated['dropped'])}, added {len(migrated['added'])}); "
                  f"now {int(after['keep_fraction']*100)}% qualify / {after['losers']} losers")
    elif verbose:
        print("   no migration needed — data healthy.")
    return {"before_health": report, "decision": decision, "steward_model": used, "migrated": migrated,
            "after_health": ({k: after[k] for k in ("total", "keep_fraction", "losers", "data_age_hours", "stale")} if after else None)}


def build_desks(ticker: str) -> list[tuple[str, dict, str]]:
    """The 7 specialist desks, each with a REAL tool output. (Sentiment desk dropped 2026-06-01:
    its price-momentum proxy was redundant with the regime engine's momentum features + the whale
    mood signal, and validated as near-noise — IC ~0.04 MNT, ~0 BTC/ETH. News/social belongs to
    the Research desk, not a separate sentiment desk.)"""
    return [
        ("Regime/Technical", tool_regime(ticker), "regime classification + trend strength + dynamic R:R + OOS-validation status"),
        ("Macro (Allora)", tool_macro(), "decentralised-AI macro via Allora BTC+ETH 8h price prediction"),
        ("On-chain/Risk", tool_onchain_risk(ticker), "whale flow + watchlist health + GoPlus token safety"),
        ("Research", tool_research(), "external macro research: Fear&Greed index + global market via public APIs"),
        ("Smart-Money Flow", tool_smartmoney(ticker), "dynamic whale/contract flow — Mantle on-chain DEX flow + mETH staking (CONTRACT mode) or majors perp positioning (POSITIONING mode) + Nansen macro smart-money netflow"),
        ("Smart-Social", tool_smartsocial(ticker), "narrative/mindshare via Elfa (smart accounts, not retail) — where crypto attention is rotating"),
        ("OI-Contrarian", tool_oi_contrarian(ticker), "the ONE OOS-validated edge — fade perp OI extremes; validated-edge registry + live signal = the disciplined exception to abstain"),
    ]


def run_analysts(desks: list, verbose: bool = True) -> dict:
    """PHASE 1 — each specialist desk (a different model) interprets its real tool output."""
    if verbose:
        print(f"\n▶ PHASE 1 · ANALYSIS ({len(desks)} specialist desks)")
    analysts: dict[str, dict] = {}
    for idx, (name, tool_out, domain) in enumerate(desks):
        used = "—"
        try:
            txt, used = complete(
                ANALYST_SYS,
                f"Your domain: {domain}\nYour tool output:\n{json.dumps(tool_out, indent=2)}",
                max_tokens=700, start=idx, expect_json=True,
            )
            v = parse_json(txt)
        except Exception as e:  # noqa: BLE001
            v = {"stance": "unavailable", "confidence": "low", "key_point": f"LLM error: {str(e)[:90]}"}
        analysts[name] = v
        if verbose:
            print(f"   [{name} · {used}] {str(v.get('stance', '?')).upper()} (conf {v.get('confidence', '?')})")
            print(f"      {v.get('key_point', v.get('raw', ''))}")
        time.sleep(0.8)
    return analysts


def run_debate(desks: list, analysts: dict, verbose: bool = True) -> dict:
    """PHASE 1.5 — bull vs bear researchers debate in 2 rounds (opening + rebuttal) before the PM."""
    ctx = ("Desk tool outputs:\n" + json.dumps({n: o for n, o, _ in desks}, indent=2)[:2800]
           + "\n\nDesk stances:\n" + json.dumps(analysts, indent=2))
    if verbose:
        print("\n▶ PHASE 1.5 · DEBATE (bull vs bear, 2 rounds — deep model: Kimi K2.6)")
    bull, bull_m, bear, bear_m = {}, "—", {}, "—"
    try:
        t, bull_m = deep(BULL_SYS, ctx, max_tokens=600, expect_json=True)
        bull = parse_json(t)
    except Exception as e:  # noqa: BLE001
        bull = {"case": "n/a", "strongest_points": [f"err {str(e)[:50]}"]}
    try:
        t, bear_m = deep(BEAR_SYS, ctx, max_tokens=600, expect_json=True)
        bear = parse_json(t)
    except Exception as e:  # noqa: BLE001
        bear = {"case": "n/a", "risks": [f"err {str(e)[:50]}"]}
    if verbose:
        print(f"   round1 Bull [{bull_m}]: {bull.get('case', '?')} -- {'; '.join(bull.get('strongest_points', [])[:2])}")
        print(f"   round1 Bear [{bear_m}]: {bear.get('case', '?')} -- {'; '.join((bear.get('strongest_points') or bear.get('risks') or [])[:2])}")

    rebut_ctx = ctx + "\n\nBULL argued:\n" + json.dumps(bull) + "\n\nBEAR argued:\n" + json.dumps(bear)
    bull_r, bear_r = {}, {}
    try:
        t, _ = deep(BULL_REBUT_SYS, rebut_ctx, max_tokens=400, expect_json=True)
        bull_r = parse_json(t)
    except Exception as e:  # noqa: BLE001
        bull_r = {"rebuttal": f"err {str(e)[:40]}"}
    try:
        t, _ = deep(BEAR_REBUT_SYS, rebut_ctx, max_tokens=400, expect_json=True)
        bear_r = parse_json(t)
    except Exception as e:  # noqa: BLE001
        bear_r = {"rebuttal": f"err {str(e)[:40]}"}
    if verbose:
        print(f"   round2 Bull rebuts: {str(bull_r.get('rebuttal', ''))[:90]}")
        print(f"   round2 Bear rebuts: {str(bear_r.get('rebuttal', ''))[:90]}")
    return {"bull": bull, "bear": bear, "bull_rebuttal": bull_r, "bear_rebuttal": bear_r}


def _ranked_desk_view(analysts: dict, weights: dict) -> str:
    """Present the desks RANKED by learned reliability + tagged TRUSTED/discount, so the PM weights the
    proven desks heavily and isn't swayed by an unreliable one — fights the 'too many desks = noise' overload."""
    items = sorted(analysts.items(), key=lambda kv: weights.get(kv[0], 1.0), reverse=True)
    out = []
    for name, v in items:
        w = weights.get(name, 1.0)
        tag = "TRUSTED" if w >= 1.1 else "DISCOUNT" if w <= 0.9 else "neutral"
        v = v or {}
        reason = str(v.get("reasoning") or v.get("read") or v.get("note") or v.get("rationale") or "")[:150]
        out.append(f"  [{w:.2f}x {tag}] {name}: {v.get('stance', '?')} ({v.get('confidence', '')}) — {reason}")
    return "\n".join(out)


def run_pm(ticker: str, reg: dict, analysts: dict, debate: dict, verbose: bool = True,
           extra: str = "", memory_brief: str = "", desk_weights_brief: str = "", carry_brief: str = "",
           flow_brief: str = "", whale_brief: str = "", mantle_brief: str = "",
           desk_weights: dict | None = None) -> dict:
    """PHASE 2 — portfolio manager synthesizes all desks into a final decision.
    `extra` carries a risk-gate repair note; `memory_brief` carries recall of past resolved trades
    (the learning-loop); `desk_weights_brief` is an ADVISORY desk-reliability prior (additive — empty
    string means no change, gates are never altered) so the PM gets 'smarter with use'.
    `carry_brief` is an ADVISORY market-neutral yield note (delta-neutral funding carry — additive,
    non-directional; empty means none harvestable -> no change; never alters the directional gates)."""
    head_user = (
        f"Asset: {ticker}\n\n"
        + (("AGENT MEMORY (recall of past resolved trades — weigh this history):\n" + memory_brief + "\n\n")
           if memory_brief else "")
        + (("DESK RELIABILITY PRIORS (learned from track record — ADVISORY only, gates unchanged):\n"
            + desk_weights_brief + "\n\n") if desk_weights_brief else "")
        + ((carry_brief + "\n\n") if carry_brief else "")
        + ((flow_brief + "\n\n") if flow_brief else "")
        + ((whale_brief + "\n\n") if whale_brief else "")
        + ((mantle_brief + "\n\n") if mantle_brief else "")
        + "Validated engine status:\n"
        + json.dumps({
            "current_regime": reg.get("current_regime"),
            "trend_following_applicable_now": reg.get("trend_following_applicable_now"),
            "oos_validated_trading_edge": reg.get("oos_validated_trading_edge"),
            "regime_classifier_oos_accuracy": reg.get("regime_classifier_oos_accuracy"),
            "status": reg.get("status"),
        }, indent=2)
        + ("\n\nDesk views — RANKED by learned reliability for THIS asset (weight TRUSTED desks heavily; "
           "treat DISCOUNT desks as weak priors — don't let a noisy desk sway you):\n"
           + _ranked_desk_view(analysts, desk_weights) if desk_weights
           else "\n\nDesk views (all 7):\n" + json.dumps(analysts, indent=2))
        + "\n\nBull-vs-Bear debate:\n" + json.dumps(debate, indent=2)
        + (("\n\n" + extra) if extra else "")
        + "\n\nMake the final, most rational decision."
    )
    head_model = "—"
    try:
        txt, head_model = deep(PM_SYS, head_user, max_tokens=900, expect_json=True)
        decision = parse_json(txt)
    except Exception as e:  # noqa: BLE001
        decision = {"decision": "ERROR", "reasoning": str(e)[:120]}
    if verbose:
        print("\n▶ PHASE 2 · DECISION (portfolio manager)")
        print(f"   [{head_model}] {decision.get('decision', '?')} "
              f"{decision.get('direction', '')} (conf {decision.get('confidence', '?')})")
        print(f"      {decision.get('reasoning', '')}")
        if decision.get("desk_consensus"):
            print(f"      consensus: {decision['desk_consensus']}")
    return decision


# ───────────── trade-ticket assembly + risk-gate repair loop (Layer 2 + Layer 3) ─────────────
MAX_TICKET_REPAIR = 2
ASSET_ALLOWLIST = ["MNT", "WMNT", "mETH", "METH", "cmETH", "CMETH", "USDe", "USDE", "FBTC", "BTC", "ETH", "SOL"]


def _swings(df) -> tuple[float, float]:
    """Recent structural swing low / high (the levels a stop/target lean on)."""
    tail = df.tail(tt.SWING_LOOKBACK)
    return float(tail["low"].min()), float(tail["high"].max())


def _crowding_factor(desks: list) -> tuple[float, str]:
    """Stretched perp positioning (a crowded book) -> size DOWN. Reads the Smart-Money desk's
    POSITIONING-mode flow_score (= 100 - buy_ratio*100; 50 balanced, 0/100 one-sided). CONTRACT/
    spot assets (e.g. MNT) have no perp-crowding signal -> neutral factor 1.0."""
    sm = next((o for n, o, _ in desks if n == "Smart-Money Flow"), {}) or {}
    if sm.get("mode") == "POSITIONING" and sm.get("flow_score_0_100") is not None:
        return tt.crowding_factor_from_score(sm.get("flow_score_0_100"), "POSITIONING")
    return 1.0, "no perp crowding signal (CONTRACT/spot asset)"


def _oi_contrarian_signal(ticker: str) -> str | None:
    """Live OI-contrarian signal from positioning data: OI 24h-change at top quintile -> SHORT
    (fade crowded longs), bottom quintile -> LONG, else None. Same construction as scripts/39."""
    try:
        df = pd.read_csv(ROOT / "data" / f"{ticker.lower()}_positioning.csv")
    except (FileNotFoundError, OSError):
        return None
    if "oi" not in df.columns or len(df) < 50:
        return None
    oichg = df["oi"].pct_change(24).dropna()
    if oichg.empty:
        return None
    latest, p20, p80 = float(oichg.iloc[-1]), float(oichg.quantile(0.20)), float(oichg.quantile(0.80))
    return "SHORT" if latest >= p80 else "LONG" if latest <= p20 else None


def _edge_profile(ticker: str, regime: str, direction: str) -> dict:
    """Stats that UNLOCK AGGRESSIVE sizing — returned ONLY when (a) the asset has an OOS-validated
    edge in data/validated_edges.json (written by scripts/39) AND (b) that edge's live signal is
    present AND agrees with the PM's direction. Otherwise SAFE. This stops a generic (e.g. trend)
    trade from borrowing an unrelated edge — aggression must match a real, live, validated signal."""
    try:
        edges = json.loads((ROOT / "data" / "validated_edges.json").read_text())
    except (FileNotFoundError, ValueError):
        return {"validated": False, "note": "no validated_edges.json -> SAFE"}
    e = edges.get(ticker.upper())
    if not e:
        return {"validated": False, "note": f"no OOS-validated edge for {ticker.upper()} -> SAFE"}
    # Generalised: read the live signal for THIS asset's ACTUAL edge type (oi/flow/funding/price), so a
    # GRADUATED non-OI edge can size live too. For oi_contrarian this reproduces _oi_contrarian_signal
    # exactly (verified); an unknown/unsupported edge type -> None -> SAFE (conservative). Reads only
    # validated_edges.json, so paper-only CANDIDATE edges can never unlock AGGRESSIVE.
    from firm.edge_lab import live_signal as _live_signal
    sig = _live_signal(ticker, e.get("edge", ""), ROOT / "data").get("signal")
    if sig and sig == direction.upper():
        return {"validated": True, "p_win": e["p_win"], "payoff_b": e["payoff_b"],
                "sample_n": e["sample_n"], "edge": e["edge"],
                "note": f"{e['edge']} signal LIVE + aligned ({sig}); AGGRESSIVE unlocked (OOS {e['oos_roi_pct']}%)"}
    return {"validated": False,
            "note": f"{e['edge']} edge exists for {ticker.upper()} but live signal "
                    f"{('=' + sig) if sig else 'absent'} != decision {direction.upper()} -> SAFE"}


def _consensus(analysts: dict, direction: str) -> float:
    """Desk-agreement strength: fraction of desks whose stance aligns with the chosen direction."""
    want = "bullish" if direction == "LONG" else "bearish"
    stances = [str(v.get("stance", "")).lower() for v in analysts.values() if isinstance(v, dict)]
    if not stances:
        return 0.5
    return round(sum(1 for s in stances if s == want) / len(stances), 2)


def finalize_decision(ticker: str, decision: dict, desks: list, reg: dict,
                      analysts: dict, debate: dict, verbose: bool = True) -> dict:
    """Attach a GATED trade ticket when the PM decides ENTER. On gate failure, re-prompt the PM
    (repair loop, NeuroBro-style 'send it back'); if it still fails, override to a disciplined
    ABSTAIN. The LLM judges direction; this deterministic layer computes size+levels and enforces
    the risk gates — we never emit a trade that can't clear R:R>=2 + a real invalidation."""
    dirn = str(decision.get("direction", "NONE")).upper()
    if decision.get("decision") != "ENTER" or dirn not in ("LONG", "SHORT"):
        decision["trade_ticket"] = None
        decision["ticket_note"] = "no ticket — ABSTAIN/NONE (disciplined: no validated edge / not directional)"
        return decision

    df, last, _regime, _adx, _adx_th, rr = _state(ticker)
    entry, atr = float(last["close"]), float(last["atr"])
    swing_low, swing_high = _swings(df)
    crowd_f, crowd_why = _crowding_factor(desks)
    edge = _edge_profile(ticker, _regime, dirn)

    attempt = 0
    while True:
        conf = str(decision.get("confidence", "medium")).lower()
        ticket = tt.build_trade_ticket(
            ticker, dirn, conf, last_price=entry, atr=atr, dynamic_rr=rr,
            swing_low=swing_low, swing_high=swing_high, equity=1000.0,
            crowding_factor=crowd_f, crowding_reason=crowd_why, allowlist=ASSET_ALLOWLIST,
            edge=edge, regime_conf=0.8, consensus=_consensus(analysts, dirn), drawdown=0.0,
        )
        if ticket.get("valid"):
            decision["trade_ticket"] = ticket
            decision["ticket_note"] = f"ENTER {dirn} ticket passed risk gates ({crowd_why})"
            if verbose:
                tp = ticket["take_profit"]
                print(f"\n▶ TRADE TICKET · {dirn} conf={conf}  [gates PASSED]")
                print(f"   entry {ticket['entry_range']}  stop {ticket['stop_loss']} "
                      f"({ticket['stop_basis']} {ticket['stop_distance_atr']}xATR)  inval {ticket['invalidation']}")
                print(f"   TP1 {tp[0]['price']} (R:R {tp[0]['rr']})  TP2 {tp[1]['price']} (R:R {tp[1]['rr']})")
                print(f"   {ticket.get('mode', 'SAFE')}: risk {ticket['risk_pct']}% -> ${ticket['notional_usd']} "
                      f"({ticket['position_size_pct']}% eq, {ticket.get('leverage')}x)  | {crowd_why}")
            return decision

        if attempt >= MAX_TICKET_REPAIR:
            probs = ticket.get("problems", [])
            decision["decision"] = "ABSTAIN"
            decision["direction"] = "NONE"
            decision["trade_ticket"] = None
            decision["ticket_note"] = f"ABSTAIN — ENTER rejected by risk gates after {attempt} repair(s): {probs}"
            decision["reasoning"] = (str(decision.get("reasoning", ""))[:300]
                                     + f" [Overridden to ABSTAIN: no trade clears the risk gates ({probs}).]")
            if verbose:
                print(f"\n▶ TRADE TICKET · gates FAILED {probs} -> ABSTAIN (disciplined)")
            return decision

        attempt += 1
        repair = (
            f"RISK-GATE FEEDBACK: your ENTER {dirn} does not clear the deterministic risk gates: "
            f"{ticket.get('problems')}. At the current price/structure (entry {entry}, ATR {round(atr, 6)}, "
            f"swing_low {round(swing_low, 6)}, swing_high {round(swing_high, 6)}), this setup cannot reach "
            f"R:R >= {tt.MIN_RR_GATE} to a real target. Reconsider: ABSTAIN, or take the OTHER direction ONLY "
            f"if structure genuinely favors it. Do NOT restate the same sub-2R setup."
        )
        if verbose:
            print(f"   ↻ ticket gate failed {ticket.get('problems')} -> repair re-prompt #{attempt}")
        decision = run_pm(ticker, reg, analysts, debate, verbose=False, extra=repair)
        dirn = str(decision.get("direction", "NONE")).upper()
        if decision.get("decision") != "ENTER" or dirn not in ("LONG", "SHORT"):
            decision["trade_ticket"] = None
            decision["ticket_note"] = f"ABSTAIN after risk-gate repair #{attempt} (disciplined)"
            return decision


def run_firm(ticker: str, verbose: bool = True) -> dict:
    """Analysis + debate + decision + gated trade ticket (no self-maintenance phase)."""
    desks = build_desks(ticker)
    analysts = run_analysts(desks, verbose=verbose)
    debate = run_debate(desks, analysts, verbose=verbose)
    decision = run_pm(ticker, desks[0][1], analysts, debate, verbose=verbose)
    decision = finalize_decision(ticker, decision, desks, desks[0][1], analysts, debate, verbose=verbose)
    return {"desks": {n: o for n, o, _ in desks}, "analysts": analysts, "debate": debate, "decision": decision}


def run_organization(ticker: str, verbose: bool = True, memory_brief: str = "") -> dict:
    """Full autonomous loop: OPERATIONS -> ANALYSIS (5 desks) -> DECISION."""
    info = active_provider_info()
    if verbose:
        print("=" * 74)
        print(f"  HeliQuant — Autonomous Intelligence Organization   |   asset: {ticker}")
        print(f"  provider: {info['provider']} ({info['style']}) | keys: {info.get('num_keys', 0)} | desks: 7")
        print("=" * 74)
    ops = run_operations(verbose=verbose)
    desks = build_desks(ticker)
    analysts = run_analysts(desks, verbose=verbose)
    debate = run_debate(desks, analysts, verbose=verbose)
    # ADDITIVE self-learning: advisory desk-reliability prior (empty if none learned -> org unchanged).
    _w = {}                                                 # tilted desk weights -> ranked desk view for the PM
    try:
        from firm import asset_efficiency as _ae
        from firm import desk_performance as _dp
        _w = _dp.load_weights(ticker)                       # PER-ASSET desk-reliability prior (track record)
        _w, _eff_brief = _ae.efficiency_tilt(ticker, _w)    # tilt toward FLOW desks if asset can't be predicted
        _dw_brief = _dp.weights_brief(_w)
        if _eff_brief:
            _dw_brief = (_eff_brief + "  ||  reliability: " + _dw_brief).strip()  # tilt FIRST (visible + key)
    except Exception:  # noqa: BLE001 — desk-learning is optional; never let it break the org
        _dw_brief = ""
    _fi_stance = _whale_stance = ""   # captured below; feed the exploration hypothesis
    # ADDITIVE carry desk: market-neutral yield advisory (empty if nothing harvestable -> org unchanged).
    try:
        from firm.carry_signal import carry_brief as _cb
        _carry_brief = _cb()
    except Exception:  # noqa: BLE001 — carry desk is optional + network-bound; never let it break the org
        _carry_brief = ""
    # ADDITIVE flow-intel desk: self-learning flow-signal read — honest-abstains when no flow signal predicts.
    try:
        from firm.flow_intel import synthesize as _fsyn
        _fi = _fsyn(ticker)
        _fi_stance = _fi.get("stance", "")
        _flow_brief = (f"FLOW-INTEL DESK (self-learning flow signals — {_fi['reliable_signals']}/4 validate OOS): "
                       f"{_fi['stance']}. {_fi['note'][:150]} Advisory, non-directional.")
    except Exception:  # noqa: BLE001 — flow-intel is optional; never let it break the org
        _flow_brief = ""
    # ADDITIVE whale desk: dynamic per-asset whale tracker — top HL traders' LIVE positions in THIS asset
    # (net long/short + their PnL). Cloud-native (HL reachable from Railway). Empty if asset not on HL.
    try:
        from firm.hl_whales import whale_read as _wr
        _wr_res = _wr(ticker)
        _whale_brief = _wr_res.get("brief", "")
        _whale_stance = _wr_res.get("stance", "")
    except Exception:  # noqa: BLE001 — whale desk is optional + network-bound; never let it break the org
        _whale_brief = ""
    # ADDITIVE Mantle fundamentals desk (DeFiLlama, keyless): chain TVL trend + protocol fees = ecosystem
    # risk-on/off (capital flowing into / out of Mantle). Only for Mantle-eco assets. Advisory context.
    _mantle_brief = ""
    if ticker.upper() in ("MNT", "METH", "CMETH", "FBTC", "USDE", "USDY", "MANTLE"):
        try:
            from firm.defillama_client import mantle_brief as _mb
            _mantle_brief = _mb()
        except Exception:  # noqa: BLE001 — optional + network-bound; never let it break the org
            _mantle_brief = ""
    # fold the NEW directional desks (flow-intel + whale) into the desk set so they're RANKED + weighted
    # ALONGSIDE the 7 LLM desks (carry = market-neutral + exploration = meta -> stay separate).
    if _fi_stance:
        analysts["flow-intel"] = {"stance": _fi_stance, "confidence": "learned", "reasoning": _flow_brief[:150]}
    if _whale_stance:
        analysts["whale"] = {"stance": _whale_stance, "confidence": "live", "reasoning": _whale_brief[:150]}
    if verbose and _dw_brief:
        print(f"   ▸ {_dw_brief[:340]}")
    if verbose and _carry_brief:
        print(f"   ▸ {_carry_brief[:180]}")
    if verbose and _flow_brief:
        print(f"   ▸ {_flow_brief[:200]}")
    if verbose and _whale_brief:
        print(f"   ▸ {_whale_brief[:220]}")
    if verbose and _mantle_brief:
        print(f"   ▸ {_mantle_brief[:240]}")
    decision = run_pm(ticker, desks[0][1], analysts, debate, verbose=verbose,
                      memory_brief=memory_brief, desk_weights_brief=_dw_brief, carry_brief=_carry_brief,
                      flow_brief=_flow_brief, whale_brief=_whale_brief, mantle_brief=_mantle_brief, desk_weights=_w)
    decision = finalize_decision(ticker, decision, desks, desks[0][1], analysts, debate, verbose=verbose)
    # EXPLORATION ("coba-coba"): when the firm ABSTAINS, PAPER-hunt an edge from the flow-desk consensus
    # (Smart-Money + On-chain + flow-intel + whale). Zero real money; resolved on the 24h move; learns which
    # conditions fail (never repeats them) and surfaces a CANDIDATE if a consensus pays. Additive + safe.
    try:
        from pathlib import Path as _P
        import pandas as _pd
        from firm import exploration as _expl
        _fp = _P(__file__).resolve().parents[1] / "data" / f"{ticker.lower()}_positioning.csv"
        _px, _fresh, _age_h = None, False, 9e9
        if _fp.exists():
            _dfx = _pd.read_csv(_fp)
            _px = float(_dfx["close"].iloc[-1])
            try:  # FRESHNESS GATE: stale data poisons paper trials (frozen entry resolved at a fresh price
                  # = fake win/loss — caught live 2026-06-09: 4 BTC trials @ a frozen 73535.8 "won" falsely).
                  # `timestamp` column = epoch MILLISECONDS (must parse with unit="ms", else -> 1970).
                _ts = _pd.to_datetime(float(_dfx["timestamp"].iloc[-1]), unit="ms", utc=True)
                _age_h = (_pd.Timestamp.now(tz="UTC") - _ts).total_seconds() / 3600
                _fresh = _age_h <= 6
            except Exception:  # noqa: BLE001 — can't verify freshness -> don't trial on it
                _fresh = False
        if _px and not _fresh and verbose:
            print(f"   🧪 EXPLORATION paused — positioning data stale ({_age_h:.0f}h old); trials need fresh prices")
        if _px and _fresh:
            _ev = _expl.resolve_and_learn({ticker: _px})
            if verbose and _ev.get("learned"):
                print(f"   🧪 EXPLORATION learned: {_ev['result']} (net {_ev['net_pct']}%) — {_ev['lesson']}")
            if str(decision.get("decision", "")).upper() == "ABSTAIN":
                _reads = {"Smart-Money Flow": (analysts.get("Smart-Money Flow") or {}).get("stance", ""),
                          "On-chain/Risk": (analysts.get("On-chain/Risk") or {}).get("stance", ""),
                          "flow-intel": _fi_stance, "whale": _whale_stance}
                _t = _expl.propose_trial(ticker, _reads)
                if _t:
                    _expl.record_trial(ticker, _t["direction"], _t["condition"], _px, _t["hypothesis"])
                    if verbose:
                        print(f"   🧪 EXPLORATION paper-trial: {_t['direction']} {ticker} @ {_px} — {_t['hypothesis']}")
    except Exception:  # noqa: BLE001 — exploration is additive; never break the org
        pass
    if verbose:
        print("\n" + "=" * 74)
    return {
        "asset": ticker, "provider": info, "operations": ops,
        "desks": {n: o for n, o, _ in desks}, "analysts": analysts, "debate": debate, "decision": decision,
    }
