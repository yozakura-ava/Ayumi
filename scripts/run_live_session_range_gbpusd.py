#!/usr/bin/env python3
"""Forward test runner — Session Range MR GBPUSD on cTrader demo.

Thin wrapper around ForwardTestEngine. All live data wiring, bar aggregation,
strategy evaluation, spread/slippage, and reconnection are handled by the engine.

Usage:
    python scripts/run_live_session_range_gbpusd.py [--live] [--bar-minutes 15] [--reset]
"""

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from common.resource_limits import add_resource_args

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from adapters.ctrader.forward_test_engine import ForwardTestConfig, ForwardTestEngine
from strategies.session_range_mean_reversion import (
    SessionRangeMeanReversionStrategy,
    SessionRangeMRConfig,
)

logger = logging.getLogger("forward_test")

SYMBOL = "GBPUSD"
LOG_DIR = "logs/trades"
STATUS_INTERVAL_S = 300


def _build_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _reset_synthetic_data(log_dir: str):
    log_path = Path(log_dir)
    if not log_path.exists():
        logger.info("No existing trade logs to reset")
        return

    csv_files = list(log_path.glob("session_range_mr_gbpusd_*.csv"))
    if not csv_files:
        logger.info("No synthetic trade logs found")
        return

    archive_dir = log_path / "synthetic_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    removed = 0
    for f in csv_files:
        content = f.read_text()
        if "1.26" in content and ("1.258" in content or "1.262" in content):
            dest = archive_dir / f.name
            shutil.move(str(f), str(dest))
            removed += 1
            logger.info("Archived synthetic log: %s -> %s", f.name, dest)

    logger.info("Reset complete: archived %d synthetic trade log(s)", removed)
    if removed == 0:
        logger.info("No synthetic data detected — logs may contain real data, not clearing")


def main():
    parser = argparse.ArgumentParser(description="Forward test: Session Range MR GBPUSD")
    add_resource_args(parser)
    parser.add_argument("--live", action="store_true", help="Enable live execution")
    parser.add_argument("--bar-minutes", type=int, default=15, help="Bar timeframe in minutes")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear synthetic trade logs and start fresh",
    )
    args = parser.parse_args()

    _build_logging(args.verbose)

    if args.reset:
        _reset_synthetic_data(LOG_DIR)

    config = ForwardTestConfig(
        symbol=SYMBOL,
        bar_period_minutes=args.bar_minutes,
        min_bars_for_evaluation=100,
        min_confidence=0.50,
        live_mode=args.live,
        log_dir=LOG_DIR,
        stats_interval_sec=STATUS_INTERVAL_S,
        clear_stuck_positions_on_start=args.reset,
        reset_on_start=args.reset,
    )

    strategy = SessionRangeMeanReversionStrategy(SessionRangeMRConfig())

    engine = ForwardTestEngine(config=config, strategies=[strategy])

    logger.info(
        "Starting forward test — %s, %dm bars, %s mode",
        SYMBOL,
        args.bar_minutes,
        "LIVE" if args.live else "PAPER",
    )
    logger.info("Spread/slippage simulation enabled on paper fills (via SlippageModel)")

    if not engine.start():
        logger.error("Failed to start forward test engine — exiting")
        return

    last_status = 0.0
    try:
        while engine.is_running:
            time.sleep(1.0)

            now = time.monotonic()
            if now - last_status >= STATUS_INTERVAL_S:
                last_status = now
                stats = engine.get_stats()
                health = stats["health"]
                trading = stats["trading"]

                utc_hour = time.gmtime(time.time()).tm_hour
                if 0 <= utc_hour < 7:
                    session = "ASIAN"
                elif 7 <= utc_hour < 11:
                    session = "LONDON"
                elif 11 <= utc_hour < 16:
                    session = "NY_AM"
                elif 16 <= utc_hour < 20:
                    session = "NY_PM"
                else:
                    session = "OUTSIDE"

                logger.info(
                    "[STATUS] session=%s trades=%d rejected=%d balance=%.2f realized=%.2f unrealized=%.2f spread=%.5f",
                    session,
                    trading["trades_executed"],
                    trading["trades_rejected"],
                    trading["current_balance"],
                    trading["realized_pnl"],
                    trading["unrealized_pnl"],
                    health.get("current_spread", 0),
                )
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        engine.stop()

    stats = engine.get_stats()
    trading = stats["trading"]
    logger.info(
        "[FINAL] trades=%d balance=%.2f realized=%.2f",
        trading["trades_executed"],
        trading["current_balance"],
        trading["realized_pnl"],
    )


if __name__ == "__main__":
    main()
