#!/usr/bin/env python3
"""Ayumi Forward Test Launcher — Paper mode, SRMR+ on GBPUSD.

Launches the forward test engine with:
- SRMR+ strategy (session range mean reversion, Optuna-validated)
- cTrader paper trading connection
- FTMO challenge risk profile
- Full signal pipeline (confidence → profile router → position sizer)

Usage:
    python scripts/launch_forward_test.py [--dry-run]

    --dry-run: Validate config and imports without connecting to cTrader
"""

from __future__ import annotations

import argparse
import logging
import os
import signal as sig_module
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from adapters.ctrader.forward_test_engine import ForwardTestConfig, ForwardTestEngine
from adapters.ctrader.models import cTraderCredentials
from adapters.ctrader.order_manager import PositionSizeConfig
from adapters.ctrader.risk_guard import FTMOConfig
from common.logging_config import setup_logging
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

logger = logging.getLogger("ayumi.launcher")


def build_credentials() -> cTraderCredentials:
    """Build cTrader credentials from .env."""
    return cTraderCredentials(
        host=os.getenv("CTRADER_HOST", "live-uk-eqx-01.p.c-trader.com"),
        port=int(os.getenv("CTRADER_SSL_PORT", "5212")),
        use_ssl=True,
        username=os.getenv("CTRADER_ACCOUNT", ""),
        password=os.getenv("CTRADER_PASSWORD", ""),
        sender_comp_id=os.getenv("CTRADER_SENDER_COMP_ID", ""),
        target_comp_id=os.getenv("CTRADER_TARGET_COMP_ID", ""),
        sender_sub_id=os.getenv("CTRADER_SENDER_SUB_ID", ""),
    )


def build_ftmo_config() -> FTMOConfig:
    """FTMO Challenge profile — $100K account."""
    return FTMOConfig()  # Defaults match FTMO challenge rules


def build_position_config() -> PositionSizeConfig:
    """Conservative position sizing for forward test."""
    return PositionSizeConfig(
        risk_per_trade_pct=0.005,  # 0.5% risk per trade
        max_lot_size=1.0,
        min_lot_size=0.01,
    )


def build_strategy() -> SRMRPlusStrategy:
    """SRMR+ with validated parameters."""
    return SRMRPlusStrategy(config=SRMRPlusConfig())


def build_forward_config() -> ForwardTestConfig:
    """Forward test configuration."""
    return ForwardTestConfig(
        symbol="GBPUSD",
        starting_balance=100_000.0,
        min_confidence=0.50,
        max_bars_per_symbol=500,
        min_bars_for_evaluation=50,
    )


def check_market_open() -> bool:
    """Check if forex market is currently open."""
    from adapters.ctrader.forward_test_engine import _is_forex_market_closed

    return not _is_forex_market_closed()


def main():
    parser = argparse.ArgumentParser(description="Ayumi Forward Test Launcher")
    parser.add_argument("--dry-run", action="store_true", help="Validate without connecting")
    args = parser.parse_args()

    setup_logging(level="INFO")
    logger.info("=== Ayumi Forward Test Launcher ===")
    logger.info("Time: %s UTC", datetime.now(timezone.utc).isoformat())

    # ── Single-instance guard (B1) ─────────────────────────────────────────
    from adapters.ctrader.pid_guard import acquire_pid_lock

    _pid_path = PROJECT_ROOT / "data" / "forward_test.pid"
    _pid_ctx = acquire_pid_lock(_pid_path)
    _pid_guard = _pid_ctx.__enter__()
    _pid_guard.write_pid()

    # Build all components
    credentials = build_credentials()
    ftmo_config = build_ftmo_config()
    position_config = build_position_config()
    strategy = build_strategy()
    config = build_forward_config()

    logger.info("Strategy: SRMR+ (session range mean reversion)")
    logger.info("Symbol: %s", config.symbol)
    logger.info("Balance: $%.2f", config.starting_balance)
    logger.info("Risk/trade: %.1f%%", ftmo_config.max_position_size_pct * 100)
    logger.info("Daily loss limit: %.1f%%", ftmo_config.daily_loss_limit_pct * 100)
    logger.info("Max drawdown: %.1f%%", ftmo_config.total_drawdown_limit_pct * 100)
    logger.info("Min R:R: %.1f", ftmo_config.min_risk_reward)

    if args.dry_run:
        logger.info("=== DRY RUN — validating pipeline ===")
        # Validate imports and config construction
        from forward_test.blend_runner import BlendForwardTestRunner

        runner_config = {
            "account_balance": config.starting_balance,
            "risk_per_trade_pct": 0.005,
            "daily_risk_cap_pct": 0.03,
            "max_sniper": 3,
            "max_swarm": 5,
            "spread_pips": {"GBPUSD": 1.5},
            "state_path": str(PROJECT_ROOT / "data" / "forward_test_state.json"),
        }
        _runner = BlendForwardTestRunner(runner_config)
        logger.info("BlendForwardTestRunner initialized OK")
        logger.info("=== DRY RUN PASSED ===")
        return

    # Live mode
    if not check_market_open():
        logger.warning("Market is CLOSED. Waiting for market open...")
        logger.info("Forex market opens Sunday 21:00 UTC / 5:00 PM EDT")
        # Don't exit — ForwardTestEngine has its own weekend detection and will wait
    else:
        logger.info("Market is OPEN")

    engine = ForwardTestEngine(
        config=config,
        strategies=[strategy],
        ftmo_config=ftmo_config,
        position_config=position_config,
        credentials=credentials,
    )

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping engine...")
        engine.stop()
        logger.info("Engine stopped. Final state saved.")
        sys.exit(0)

    sig_module.signal(sig_module.SIGINT, shutdown)
    sig_module.signal(sig_module.SIGTERM, shutdown)

    logger.info("=== STARTING FORWARD TEST ===")
    engine.start()

    # Keep alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
