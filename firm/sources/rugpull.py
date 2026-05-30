"""Rugpull / token-security screener via GoPlus Security API (free tier).

Returns a RiskAssessment for a given token address on a given chain
(default: Mantle mainnet chain_id 5000). The composite voter uses
`trade_safe=False` as a hard veto — no trade regardless of other signals.

API: https://api.gopluslabs.io/api/v1/token_security/{chain_id}
     ?contract_addresses={token_address}

Notes:
  - MNT itself is the gas token; not all token-security checks apply to
    the native token. Use this for ERC-20 token trades on Mantle (Merchant
    Moe pairs, memecoins, etc.).
  - Free tier limits ~10 req/min — cache aggressively.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import requests

GOPLUS_BASE = "https://api.gopluslabs.io/api/v1/token_security"
MANTLE_MAINNET_CHAIN_ID = 5000


@dataclass(frozen=True)
class RiskAssessment:
    token_address: str
    chain_id: int
    is_honeypot: bool
    top_10_concentration_pct: float | None
    owner_can_change_balance: bool
    is_mintable: bool
    is_proxy: bool
    risk_score: float                            # 0..1, higher = riskier
    trade_safe: bool                             # composite gate
    severity: Literal["safe", "watch", "danger"]
    reasoning: str
    is_mocked: bool = False


class RugpullScreener:
    _cache: dict[str, tuple[float, RiskAssessment]] = {}

    def __init__(self, chain_id: int = MANTLE_MAINNET_CHAIN_ID, cache_seconds: int = 300) -> None:
        self.chain_id = chain_id
        self.cache_seconds = cache_seconds

    def assess(self, token_address: str) -> RiskAssessment:
        token_address = token_address.lower()
        cache_key = f"{self.chain_id}:{token_address}"
        now = time.time()
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self.cache_seconds:
                return cached

        try:
            r = requests.get(
                f"{GOPLUS_BASE}/{self.chain_id}",
                params={"contract_addresses": token_address},
                timeout=10,
                headers={"User-Agent": "mantle-trading-firm/0.1"},
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:  # noqa: BLE001
            return self._mock(token_address, f"API error: {type(e).__name__}")

        result = (payload.get("result") or {}).get(token_address)
        if not result:
            return self._mock(token_address, "Token not found in GoPlus database")

        is_honeypot = result.get("is_honeypot") == "1"
        is_mintable = result.get("is_mintable") == "1"
        is_proxy = result.get("is_proxy") == "1"
        owner_can_change_balance = result.get("owner_change_balance") == "1"

        # Top 10 holder concentration
        top10 = 0.0
        for h in (result.get("holders") or [])[:10]:
            try:
                pct = float(h.get("percent", 0) or 0)
                top10 += pct
            except Exception:
                pass
        top10_pct = top10 * 100 if top10 < 1 else top10  # API sometimes uses 0..1

        # Composite risk score (0..1)
        risk = 0.0
        flags: list[str] = []
        if is_honeypot:
            risk += 0.6
            flags.append("HONEYPOT")
        if owner_can_change_balance:
            risk += 0.25
            flags.append("owner_changes_balance")
        if is_mintable:
            risk += 0.10
            flags.append("mintable")
        if is_proxy:
            risk += 0.05
            flags.append("proxy")
        if top10_pct > 80:
            risk += 0.25
            flags.append(f"top10={top10_pct:.0f}%")
        elif top10_pct > 50:
            risk += 0.10
            flags.append(f"top10={top10_pct:.0f}%")
        risk = min(risk, 1.0)

        trade_safe = (not is_honeypot) and risk < 0.5
        if risk < 0.2:
            severity: Literal["safe", "watch", "danger"] = "safe"
        elif risk < 0.5:
            severity = "watch"
        else:
            severity = "danger"

        reasoning = (
            f"{severity.upper()} risk={risk:.2f}: "
            + (", ".join(flags) if flags else "no red flags")
        )

        out = RiskAssessment(
            token_address=token_address,
            chain_id=self.chain_id,
            is_honeypot=is_honeypot,
            top_10_concentration_pct=top10_pct,
            owner_can_change_balance=owner_can_change_balance,
            is_mintable=is_mintable,
            is_proxy=is_proxy,
            risk_score=risk,
            trade_safe=trade_safe,
            severity=severity,
            reasoning=reasoning,
        )
        self._cache[cache_key] = (now, out)
        return out

    def _mock(self, token_address: str, why: str) -> RiskAssessment:
        return RiskAssessment(
            token_address=token_address,
            chain_id=self.chain_id,
            is_honeypot=False,
            top_10_concentration_pct=None,
            owner_can_change_balance=False,
            is_mintable=False,
            is_proxy=False,
            risk_score=0.0,
            trade_safe=True,
            severity="watch",
            reasoning=f"MOCK: {why}",
            is_mocked=True,
        )
