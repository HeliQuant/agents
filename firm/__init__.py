"""Mantle AI Trading Firm — Python worker agents orchestrated by Paperclip.

Architecture:
  Paperclip (Node.js control plane) → HTTP webhook → this FastAPI server
  → one of:
    /agent/research   — scan news/sentiment via Claude, push to signal context
    /agent/signal     — fuse Allora prediction + ML model → trade decision
    /agent/risk       — gate trade size and drawdown
    /agent/execution  — sign + submit tx to TradingVault on Mantle
    /agent/reputation — read settled-job event, update ERC-8004 reputation
"""
