#!/usr/bin/env python3
"""
Quick test of regime router parameter sweep functionality.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

import logging

from backtest import CsvDataLoader
from backtest.engine import BacktestConfig
from backtest.enhanced_engine import EnhancedBacktestEngine
from backtest.strategies import RegimeRouterConfig, RegimeSwitchingRouter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_regime_router():
    """Test basic regime router functionality."""
    logger.info("Testing RegimeSwitchingRouter...")

    config = BacktestConfig(
        starting_balance=100_000.0,
        spread_pips=1.0,
        commission_per_lot=7.0,
    )

    router_config = RegimeRouterConfig(
        adx_trend_threshold=25.0,
        adx_strong_trend_threshold=40.0,
        adx_range_threshold=20.0,
        atr_volatility_percentile=75.0,
    )

    router = RegimeSwitchingRouter(config=router_config)

    logger.info(f"Created router: {router.name}")

    loader = CsvDataLoader()
    bars = loader.load("data/forex/historical/EURUSD_H1.csv")
    bars = bars[-1000:]

    logger.info(f"Loaded {len(bars)} bars")

    try:
        engine = EnhancedBacktestEngine(config=config, strategies=[router])
        metrics = engine.run_strategy(router, bars)

        logger.info("Backtest completed successfully!")
        logger.info(f"Win Rate: {metrics.win_rate:.2f}%")
        logger.info(f"Total Return: {metrics.total_pnl_pct:.2f}%")
        logger.info(f"Max Drawdown: {metrics.max_drawdown_pct:.2f}%")
        logger.info(f"Sharpe Ratio: {metrics.sharpe_ratio:.3f}")
        logger.info(f"Profit Factor: {metrics.profit_factor:.3f}")
        logger.info(f"Total Trades: {metrics.total_trades}")

        return True

    except Exception as e:
        logger.error(f"Backtest failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_regime_router()
    sys.exit(0 if success else 1)
