"""Centralized configuration for all agent workers.

Reads from environment / .env via pydantic-settings. Each agent imports `settings`
from this module — no scattered os.environ lookups.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Mantle RPC + signing ──────────────────────────────────────────
    mantle_sepolia_rpc_url: str = "https://rpc.sepolia.mantle.xyz"
    mantle_mainnet_rpc_url: str = "https://rpc.mantle.xyz"
    executor_private_key: str | None = None  # ONLY used by Execution Agent
    chain_id: int = 5003  # default: Mantle Sepolia

    # ─── Deployed contract addresses (filled after M1 deploy) ──────────
    identity_registry_address: str | None = None
    reputation_registry_address: str | None = None
    validation_registry_address: str | None = None
    allora_consumer_address: str | None = None
    trading_vault_address: str | None = None
    job_manager_address: str | None = None
    principal_token_address: str | None = None  # e.g. USDC on Mantle
    base_token_address: str | None = None       # e.g. WMNT on Mantle
    dex_router_address: str | None = None       # e.g. Merchant Moe router

    # ─── ML model artifact ─────────────────────────────────────────────
    ml_model_path: str = "models/xgb_mnt.pkl"
    ml_confidence_buy: float = 0.65
    ml_confidence_sell: float = 0.55
    ml_min_adx: float = 20.0

    # ─── Allora ────────────────────────────────────────────────────────
    allora_api_url: str = "https://api.allora.network"
    allora_api_key: str | None = None
    allora_chain: str = "mainnet"  # mainnet | testnet
    # Mainnet topic IDs (confirmed via get_all_topics):
    allora_topic_btc_price_8h: int = 14
    allora_topic_btc_logret_24h: int = 15
    allora_topic_btc_logret_20m: int = 18
    allora_topic_eth_price_8h: int = 9

    # ─── LLM (Claude) ──────────────────────────────────────────────────
    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-4-6"

    # ─── Crypto data ───────────────────────────────────────────────────
    binance_api_url: str = "https://api.binance.com"

    # ─── HTTP server ───────────────────────────────────────────────────
    worker_host: str = "0.0.0.0"
    worker_port: int = 8000

    # ─── Risk parameters ───────────────────────────────────────────────
    risk_per_trade_bps: int = 100         # 1%
    max_drawdown_bps: int = 2_000         # 20% kill-switch
    atr_sl_multiplier: float = 1.0
    atr_tp_multiplier: float = 1.46

    # ─── Paperclip callback (for long-running async tasks) ─────────────
    paperclip_callback_url: str = "http://localhost:3100/api/runs/callback"
    paperclip_secret: str | None = Field(default=None, description="Shared HMAC secret")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
