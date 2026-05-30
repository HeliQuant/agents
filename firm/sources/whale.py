"""Whale Tracker — on-chain "smart money" flow via Mantle JSON-RPC.

Scans Mantle DEX swap events for the MNT/USDC pool over a recent window
(default last 1 hour ~ 1800 blocks @ 2s block time) and aggregates:
  - Notional volume per swap (filter: > min_notional_usd)
  - Net direction (BUY = MNT in / USDC out; SELL = MNT out / USDC in)
  - Net flow = sum(BUY notional) - sum(SELL notional) over window

Bullish if net flow >> 0, bearish if << 0.

NOTE: For the hackathon we use a pragmatic heuristic — any swap > $10K
counts as a "whale-tier" trade. Real smart money tagging (Nansen-style)
would require labelled wallet datasets we don't have access to.

If RPC is rate-limited or the configured pool address is unset, we fall
back to a deterministic mocked signal so the composite Signal Agent
keeps functioning end-to-end. We log a warning so it's visible in the
reasoning trace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import structlog
from web3 import Web3

log = structlog.get_logger()


@dataclass(frozen=True)
class WhaleFlowSignal:
    direction: Literal["bullish", "bearish", "neutral"]
    score: float                        # [-1, +1]
    confidence: float                   # 0..1 (data quality)
    net_flow_usd: float                 # raw value
    buy_volume_usd: float
    sell_volume_usd: float
    trade_count: int
    window_blocks: int
    pool_address: str | None
    is_mocked: bool
    reasoning: str


# Uniswap V2-compatible swap event topic
#   keccak256("Swap(address,uint256,uint256,uint256,uint256,address)")
SWAP_EVENT_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


class WhaleTracker:
    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        pool_address: str | None = None,
        min_notional_usd: float = 10_000.0,
        window_blocks: int = 1800,           # ~1 hour @ 2s block
        mnt_price_hint_usd: float = 0.63,    # fallback if no oracle on chain
    ) -> None:
        self.rpc_url = rpc_url or os.environ.get(
            "MANTLE_MAINNET_RPC_URL", "https://rpc.mantle.xyz"
        )
        self.pool_address = pool_address or os.environ.get("MNT_USDC_POOL_ADDRESS")
        self.min_notional_usd = min_notional_usd
        self.window_blocks = window_blocks
        self.mnt_price_hint_usd = mnt_price_hint_usd

    def fetch(self) -> WhaleFlowSignal:
        if not self.pool_address:
            return self._mock(
                "MNT_USDC_POOL_ADDRESS env not configured -- returning mock signal."
            )

        try:
            w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 10}))
            latest = w3.eth.block_number
            from_block = max(latest - self.window_blocks, 0)
            logs = w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": latest,
                "address": Web3.to_checksum_address(self.pool_address),
                "topics": [SWAP_EVENT_TOPIC],
            })
        except Exception as e:  # noqa: BLE001
            log.warning("whale.rpc_fail", error=str(e)[:160])
            return self._mock(f"RPC error: {type(e).__name__}")

        buy_volume = 0.0
        sell_volume = 0.0
        trade_count = 0

        # Uniswap V2 Swap event data layout:
        #   amount0In, amount1In, amount0Out, amount1Out   (each uint256)
        for ev in logs:
            try:
                data = bytes.fromhex(ev["data"][2:])
                if len(data) < 128:
                    continue
                amount0_in = int.from_bytes(data[0:32], "big")
                amount1_in = int.from_bytes(data[32:64], "big")
                amount0_out = int.from_bytes(data[64:96], "big")
                amount1_out = int.from_bytes(data[96:128], "big")
            except Exception:
                continue

            # We don't know which token is 0 vs 1 without a token0() call;
            # use a conservative heuristic: USDC value side has 6 decimals,
            # MNT side has 18. So we expect one side to be MUCH bigger raw.
            usdc_value_in = amount0_in if amount0_in < 1e15 else amount1_in
            usdc_value_out = amount0_out if amount0_out < 1e15 else amount1_out
            notional = max(usdc_value_in, usdc_value_out) / 1e6  # USDC is 6 decimals

            if notional < self.min_notional_usd:
                continue
            trade_count += 1
            # BUY = MNT bought = USDC IN to pool
            if usdc_value_in > usdc_value_out:
                buy_volume += notional
            else:
                sell_volume += notional

        net_flow = buy_volume - sell_volume
        total_vol = buy_volume + sell_volume
        if total_vol > 0:
            normalized = net_flow / total_vol  # [-1, +1]
        else:
            normalized = 0.0

        if normalized > 0.15:
            direction = "bullish"
        elif normalized < -0.15:
            direction = "bearish"
        else:
            direction = "neutral"

        return WhaleFlowSignal(
            direction=direction,
            score=normalized,
            confidence=min(1.0, trade_count / 20.0),
            net_flow_usd=net_flow,
            buy_volume_usd=buy_volume,
            sell_volume_usd=sell_volume,
            trade_count=trade_count,
            window_blocks=self.window_blocks,
            pool_address=self.pool_address,
            is_mocked=False,
            reasoning=(
                f"{direction.upper()} score={normalized:+.3f}: "
                f"{trade_count} whale trades > ${self.min_notional_usd:,.0f} in last "
                f"{self.window_blocks} blocks; buy=${buy_volume:,.0f}, "
                f"sell=${sell_volume:,.0f}, net=${net_flow:+,.0f}."
            ),
        )

    def _mock(self, why: str) -> WhaleFlowSignal:
        return WhaleFlowSignal(
            direction="neutral",
            score=0.0,
            confidence=0.0,
            net_flow_usd=0.0,
            buy_volume_usd=0.0,
            sell_volume_usd=0.0,
            trade_count=0,
            window_blocks=self.window_blocks,
            pool_address=self.pool_address,
            is_mocked=True,
            reasoning=f"MOCK: {why}",
        )
