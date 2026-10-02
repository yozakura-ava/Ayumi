#!/usr/bin/env python3
"""
Quick test of regime router parameter sweep with small parameter space.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

import json
import logging

from backtest import CsvDataLoader
from backtest.engine import BacktestConfig
from backtest.enhanced_engine import EnhancedBacktestEngine
from backtest.parameter_sweep.grid import GridPoint, ParameterGrid
from backtest.strategies import RegimeRouterConfig, RegimeSwitchingRouter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_regime_router(grid_point: GridPoint) -> RegimeSwitchingRouter:
    """Create a RegimeSwitchingRouter from grid point parameters."""
    params = grid_point.params

    config = RegimeRouterConfig(
        adx_trend_threshold=params.get("adx_trend_threshold", 25.0),
        adx_strong_trend_threshold=params.get("adx_strong_trend_threshold", 40.0),
        adx_range_threshold=params.get("adx_range_threshold", 20.0),
        atr_volatility_percentile=params.get("atr_volatility_percentile", 75.0),
        trending_size_multiplier=params.get("trending_size_multiplier", 1.0),
        ranging_size_multiplier=params.get("ranging_size_multiplier", 1.0),
        volatile_size_multiplier=params.get("volatile_size_multiplier", 0.5),
        transition_size_multiplier=params.get("transition_size_multiplier", 0.5),
        min_confidence=params.get("min_confidence", 0.55),
    )

    return RegimeSwitchingRouter(config=config)


def run_backtest(
    strategy: RegimeSwitchingRouter,
    bars,
    config: BacktestConfig,
):
    """Run a single backtest and return metrics."""
    try:
        engine = EnhancedBacktestEngine(config=config, strategies=[strategy])
        metrics = engine.run_strategy(strategy, bars)

        return {
            "win_rate": metrics.win_rate,
            "max_dd": metrics.max_drawdown_pct,
            "total_return": metrics.total_pnl_pct,
            "sharpe_ratio": metrics.sharpe_ratio,
            "trade_count": metrics.total_trades,
            "profit_factor": metrics.profit_factor,
        }
    except Exception as exc:
        logger.warning(f"Backtest failed: {exc}")
        return None


def worker_entry(args):
    """Worker entry point for parallel processing."""
    (grid_point, bars_data, config_dict) = args

    try:
        from datetime import datetime

        from backtest.engine import Bar

        bars = []
        for b in bars_data:
            bar = Bar(
                time=datetime.fromisoformat(b["time"]),
                open=b["open"],
                high=b["high"],
                low=b["low"],
                close=b["close"],
                volume=b["volume"],
            )
            bars.append(bar)

        config = BacktestConfig(**config_dict)

        strategy = create_regime_router(grid_point)

        backtest_result = run_backtest(strategy, bars, config)
        if backtest_result is None:
            return None

        result = {
            "params": grid_point.params,
            "backtest": backtest_result,
        }

        return result

    except Exception as exc:
        logger.warning(f"Worker failed: {exc}")
        return None


def test_parameter_sweep():
    """Test parameter sweep with small parameter space."""
    logger.info("Testing parameter sweep...")

    loader = CsvDataLoader()
    bars = loader.load("data/forex/historical/EURUSD_H1.csv")
    bars = bars[-1000:]

    logger.info(f"Loaded {len(bars)} bars")

    param_space = {
        "adx_trend_threshold": [20.0, 25.0],
        "atr_volatility_percentile": [75.0],
        "trending_size_multiplier": [1.0],
        "min_confidence": [0.55],
    }

    grid = ParameterGrid(param_space)
    logger.info(f"Parameter space size: {grid.size} combinations")

    config = BacktestConfig(
        starting_balance=100_000.0,
        spread_pips=1.0,
        commission_per_lot=7.0,
    )

    config_dict = {
        "starting_balance": config.starting_balance,
        "spread_pips": config.spread_pips,
        "commission_per_lot": config.commission_per_lot,
    }

    bars_data = [
        {
            "time": bar.time.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]

    tasks = []
    for point in grid:
        task_args = (point, bars_data, config_dict)
        tasks.append(task_args)

    logger.info(f"Running {len(tasks)} tasks sequentially")

    results = []
    for task in tasks:
        result = worker_entry(task)
        if result is not None and result["backtest"] is not None:
            results.append(result)

    logger.info(f"Completed {len(results)} successful runs")

    if results:
        sorted_by_sharpe = sorted(results, key=lambda r: r["backtest"]["sharpe_ratio"], reverse=True)

        logger.info("\nTop 5 by Sharpe Ratio:")
        for i, result in enumerate(sorted_by_sharpe[:5], 1):
            metrics = result["backtest"]
            logger.info(
                f"{i}. Sharpe: {metrics['sharpe_ratio']:.3f}, Return: {metrics['total_return']:.2f}%, Win Rate: {metrics['win_rate']:.1f}%"  # noqa: E501
            )
            logger.info(f"   Params: {result['params']}")

    output_path = "reports/regime_router_sweeps/test_sweep_results.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(
            {
                "total_runs": len(tasks),
                "successful_runs": len(results),
                "results": results,
            },
            f,
            indent=2,
        )

    logger.info(f"\nResults saved to {output_path}")

    return len(results) > 0


if __name__ == "__main__":
    success = test_parameter_sweep()
    sys.exit(0 if success else 1)
