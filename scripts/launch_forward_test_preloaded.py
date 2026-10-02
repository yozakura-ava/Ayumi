#!/usr/bin/env python3
"""Forward test launcher with historical bar preloading via OpenAPI."""

from __future__ import annotations

import logging
import os
import signal as sig_module
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from adapters.ctrader.forward_test_engine import ForwardTestConfig, ForwardTestEngine
from adapters.ctrader.models import cTraderCredentials
from adapters.ctrader.open_api_client import CTraderOpenApiClient
from adapters.ctrader.order_manager import PositionSizeConfig
from adapters.ctrader.risk_guard import FTMOConfig
from common.logging_config import setup_logging
from core.types import Bar, BarPeriod
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

logger = logging.getLogger("ayumi.launcher")


def fetch_h1_bars(count: int = 100) -> list[dict]:
    """Fetch H1 bars for GBPUSD via OpenAPI."""
    client = CTraderOpenApiClient(
        client_id=os.getenv("CTRADER_OPENAPI_CLIENT_ID"),
        client_secret=os.getenv("CTRADER_OPENAPI_CLIENT_SECRET"),
        account_id=REDACTED_CTRADER_ACCOUNT,
        access_token=os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN"),
    )
    client.connect()

    to_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    from_ts = to_ts - (count * 3600 * 1000) + 3600 * 1000  # slightly overlapping

    logger.info(f"Fetching {count} H1 bars from OpenAPI...")
    bars = client.get_trendbars(
        symbol_id=2,  # GBPUSD
        period="H1",
        from_ts=from_ts,
        to_ts=to_ts,
        max_bars=count,
    )
    client.disconnect()
    logger.info(f"Fetched {len(bars)} bars")
    return bars


def main():
    setup_logging(level="INFO")
    logger.info("=== Ayumi Forward Test Launcher (Preloaded) ===")

    # Fetch bars first
    raw_bars = fetch_h1_bars(100)
    if not raw_bars:
        logger.error("Failed to fetch historical bars — aborting")
        sys.exit(1)

    # Build components
    credentials = cTraderCredentials(
        host=os.getenv("CTRADER_HOST", "demo-uk-eqx-01.p.c-trader.com"),
        port=int(os.getenv("CTRADER_SSL_PORT", "5212")),
        use_ssl=True,
        username=os.getenv("CTRADER_ACCOUNT", ""),
        password=os.getenv("CTRADER_PASSWORD", ""),
        sender_comp_id=os.getenv("CTRADER_SENDER_COMP_ID", ""),
        target_comp_id=os.getenv("CTRADER_TARGET_COMP_ID", ""),
        sender_sub_id=os.getenv("CTRADER_SENDER_SUB_ID", ""),
    )

    config = ForwardTestConfig(
        symbol="GBPUSD",
        starting_balance=100_000.0,
        min_confidence=0.50,
        max_bars_per_symbol=500,
        min_bars_for_evaluation=50,
    )

    engine = ForwardTestEngine(
        config=config,
        strategies=[SRMRPlusStrategy(config=SRMRPlusConfig())],
        ftmo_config=FTMOConfig(),
        position_config=PositionSizeConfig(
            risk_per_trade_pct=0.005,
            max_lot_size=1.0,
            min_lot_size=0.01,
        ),
        credentials=credentials,
    )

    # Preload bars
    bar_objects = []
    for rb in raw_bars:
        bar_objects.append(
            Bar(
                time=datetime.fromtimestamp(rb["timestamp"] / 1000, tz=timezone.utc),
                open=rb["open"],
                high=rb["high"],
                low=rb["low"],
                close=rb["close"],
                volume=rb["volume"],
                period=BarPeriod.H1,
            )
        )

    engine._bars["GBPUSD"] = bar_objects
    logger.info(f"Preloaded {len(bar_objects)} H1 bars into engine._bars['GBPUSD']")

    # Shutdown handler
    def shutdown(signum, frame):
        logger.info("Shutdown signal — stopping engine...")
        engine.stop()
        sys.exit(0)

    sig_module.signal(sig_module.SIGINT, shutdown)
    sig_module.signal(sig_module.SIGTERM, shutdown)

    logger.info("=== STARTING FORWARD TEST ===")
    engine.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
