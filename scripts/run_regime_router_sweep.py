#!/usr/bin/env python3
"""
Parameter sweep for RegimeSwitchingRouter portfolio optimization.

Focus areas:
- Regime detection thresholds (ADX, ATR percentile)
- Strategy weight allocations per regime
- Position sizing parameters
- Walk-forward window configurations
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.engine import BacktestConfig, Bar
from backtest.enhanced_engine import EnhancedBacktestEngine
from backtest.parameter_sweep.grid import GridPoint, ParameterGrid
from backtest.parameter_sweep.result import SweepResult, SweepRow
from backtest.strategies import (
    RegimeRouterConfig,
    RegimeSwitchingRouter,
)
from common.resource_limits import add_resource_args, run_limited
from quant.walk_forward import run_strategy as run_walk_forward

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class WalkForwardSweepRow:
    """Extended sweep row with walk-forward results."""

    row: SweepRow
    walk_forward_go_nogo: bool = False
    walk_forward_windows_passed: int = 0
    walk_forward_total_windows: int = 0

    def get(self, key: str, default: float = 0.0) -> float:
        """Delegate get to the underlying SweepRow."""
        return self.row.get(key, default)


@dataclass
class RegimeRouterSweepConfig:
    """Configuration for regime router parameter sweep."""

    data_path: str = "data/forex/historical"
    symbol: str = "EURUSD"
    timeframe: str = "H1"
    n_bars: int = 10000

    output_dir: str = "reports/regime_router_sweeps"

    max_workers: Optional[int] = 2

    regime_detection_params: Dict[str, List[Any]] = field(
        default_factory=lambda: {
            "adx_trend_threshold": [20.0, 25.0, 30.0],
            "adx_strong_trend_threshold": [35.0, 40.0, 45.0],
            "adx_range_threshold": [15.0, 20.0, 25.0],
            "atr_volatility_percentile": [70.0, 75.0, 80.0, 85.0],
            "atr_lookback": [30, 50, 70],
            "adx_period": [14],
        }
    )

    size_multiplier_params: Dict[str, List[Any]] = field(
        default_factory=lambda: {
            "trending_size_multiplier": [0.8, 1.0, 1.2],
            "ranging_size_multiplier": [0.8, 1.0, 1.2],
            "volatile_size_multiplier": [0.3, 0.5, 0.7],
            "transition_size_multiplier": [0.3, 0.5, 0.7],
        }
    )

    confidence_params: Dict[str, List[Any]] = field(
        default_factory=lambda: {
            "min_confidence": [0.50, 0.55, 0.60, 0.65],
        }
    )

    walk_forward_params: Dict[str, List[Any]] = field(
        default_factory=lambda: {
            "n_windows": [3, 5, 7],
            "train_ratio": [0.5, 0.6, 0.7],
            "val_ratio": [0.15, 0.20, 0.25],
            "overlap_ratio": [0.0, 0.1, 0.2],
        }
    )


def load_bars(
    symbol: str,
    timeframe: str,
    data_path: str,
    n_bars: int = 10000,
) -> List[Bar]:
    """Load historical bars from CSV file."""
    csv_path = os.path.join(data_path, f"{symbol}_{timeframe}.csv")

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Data file not found: {csv_path}")

    from backtest import CsvDataLoader

    loader = CsvDataLoader()
    bars = loader.load(csv_path)
    bars = bars[-n_bars:]

    logger.info(f"Loaded {len(bars)} bars from {csv_path}")
    return bars


def create_regime_router(grid_point: GridPoint) -> RegimeSwitchingRouter:
    """Create a RegimeSwitchingRouter from grid point parameters."""
    params = grid_point.params

    config = RegimeRouterConfig(
        adx_trend_threshold=params.get("adx_trend_threshold", 25.0),
        adx_strong_trend_threshold=params.get("adx_strong_trend_threshold", 40.0),
        adx_range_threshold=params.get("adx_range_threshold", 20.0),
        atr_volatility_percentile=params.get("atr_volatility_percentile", 75.0),
        atr_lookback=params.get("atr_lookback", 50),
        adx_period=params.get("adx_period", 14),
        trending_size_multiplier=params.get("trending_size_multiplier", 1.0),
        ranging_size_multiplier=params.get("ranging_size_multiplier", 1.0),
        volatile_size_multiplier=params.get("volatile_size_multiplier", 0.5),
        transition_size_multiplier=params.get("transition_size_multiplier", 0.5),
        min_confidence=params.get("min_confidence", 0.55),
    )

    return RegimeSwitchingRouter(config=config)


def run_backtest(
    strategy: RegimeSwitchingRouter,
    bars: List[Bar],
    config: BacktestConfig,
) -> Optional[Dict[str, Any]]:
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


def run_walk_forward_validation(
    strategy: RegimeSwitchingRouter,
    bars: List[Bar],
    n_windows: int = 5,
    train_ratio: float = 0.6,
    val_ratio: float = 0.2,
    overlap_ratio: float = 0.1,
) -> Optional[Dict[str, Any]]:
    """Run walk-forward validation and return results."""
    try:
        results = run_walk_forward(
            strategy=strategy,
            bars=bars,
            n_windows=n_windows,
            train_ratio=train_ratio,
            val_ratio=val_ratio,
            overlap_ratio=overlap_ratio,
        )

        return {
            "go_nogo": results.go_nogo,
            "windows_passed": sum(1 for m in results.per_window if m.passed_go_nogo),
            "total_windows": len(results.per_window),
            "aggregated_sharpe": results.aggregated.mean_sharpe_ratio if results.aggregated else 0.0,
            "aggregated_return": results.aggregated.mean_total_pnl if results.aggregated else 0.0,
            "aggregated_max_dd": results.aggregated.mean_max_drawdown if results.aggregated else 0.0,
        }
    except Exception as exc:
        logger.warning(f"Walk-forward validation failed: {exc}")
        return None


def worker_entry(args: Tuple) -> Optional[Dict[str, Any]]:
    """Worker entry point for parallel processing."""
    (
        grid_point,
        bars_data,
        config_dict,
        run_walk_forward_flag,
        walk_forward_params,
    ) = args

    try:
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

        walk_forward_result = None
        if run_walk_forward_flag:
            walk_forward_result = run_walk_forward_validation(
                strategy,
                bars,
                **walk_forward_params,
            )

        result = {
            "params": grid_point.params,
            "backtest": backtest_result,
            "walk_forward": walk_forward_result,
            "walk_forward_params": walk_forward_params,
        }

        return result

    except Exception as exc:
        logger.warning(f"Worker failed: {exc}")
        return None


class RegimeRouterSweepRunner:
    """Run parameter sweeps for RegimeSwitchingRouter."""

    def __init__(self, config: RegimeRouterSweepConfig):
        self._config = config
        self._output_dir = Path(config.output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def build_parameter_space(self) -> Dict[str, List[Any]]:
        """Build combined parameter space from all focus areas."""
        param_space = {}

        param_space.update(self._config.regime_detection_params)
        param_space.update(self._config.size_multiplier_params)
        param_space.update(self._config.confidence_params)
        param_space.update(self._config.walk_forward_params)

        return param_space

    def run_sweep(
        self,
        run_walk_forward: bool = True,
    ) -> SweepResult:
        """Run the parameter sweep."""
        logger.info("Starting regime router parameter sweep")

        bars = load_bars(
            symbol=self._config.symbol,
            timeframe=self._config.timeframe,
            data_path=self._config.data_path,
            n_bars=self._config.n_bars,
        )

        param_space = self.build_parameter_space()
        grid = ParameterGrid(param_space)

        logger.info(f"Parameter space size: {grid.size} combinations")

        config = BacktestConfig(
            starting_balance=100_000.0,
            spread_pips=1.0,
            commission_per_lot=7.0,
        )

        rows = []
        tasks = []

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

        for point in grid:
            walk_forward_params = {
                "n_windows": point.params.get("n_windows", 5),
                "train_ratio": point.params.get("train_ratio", 0.6),
                "val_ratio": point.params.get("val_ratio", 0.2),
                "overlap_ratio": point.params.get("overlap_ratio", 0.1),
            }

            task_args = (
                point,
                bars_data,
                config_dict,
                run_walk_forward,
                walk_forward_params,
            )
            tasks.append(task_args)

        max_workers = self._config.max_workers or 2

        if max_workers and max_workers > 1 and len(tasks) > 1:
            logger.info(f"Running {len(tasks)} tasks with {max_workers} workers")
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {executor.submit(worker_entry, task): idx for idx, task in enumerate(tasks)}

                results: List[Optional[Dict[str, Any]]] = [None] * len(tasks)

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception as exc:
                        logger.warning(f"Task {idx} failed: {exc}")
        else:
            logger.info(f"Running {len(tasks)} tasks sequentially")
            results = [worker_entry(task) for task in tasks]

        for result in results:
            if result is not None and result["backtest"] is not None:
                backtest = result["backtest"]
                walk_forward = result.get("walk_forward")

                row_params = result["params"].copy()

                if walk_forward:
                    walk_forward_params = result["walk_forward_params"]
                    row_params.update(
                        {
                            "n_windows": walk_forward_params.get("n_windows", 5),
                            "train_ratio": walk_forward_params.get("train_ratio", 0.6),
                            "val_ratio": walk_forward_params.get("val_ratio", 0.2),
                            "overlap_ratio": walk_forward_params.get("overlap_ratio", 0.1),
                        }
                    )

                row = SweepRow(
                    params=row_params,
                    win_rate=backtest["win_rate"],
                    max_dd=backtest["max_dd"],
                    total_return=backtest["total_return"],
                    sharpe_ratio=backtest["sharpe_ratio"],
                    trade_count=backtest["trade_count"],
                    profit_factor=backtest["profit_factor"],
                )

                if walk_forward:
                    row = WalkForwardSweepRow(
                        row=row,
                        walk_forward_go_nogo=walk_forward["go_nogo"],
                        walk_forward_windows_passed=walk_forward["windows_passed"],
                        walk_forward_total_windows=walk_forward["total_windows"],
                    )

                rows.append(row)

        logger.info(f"Completed {len(rows)} successful runs")
        return SweepResult(rows=rows)

    def save_results(self, result: SweepResult, suffix: str = "") -> str:
        """Save sweep results to JSON file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self._config.symbol}_{self._config.timeframe}_regime_router_sweep{suffix}_{timestamp}.json"
        filepath = self._output_dir / filename

        def unwrap_row(row):
            """Unwrap WalkForwardSweepRow to get the underlying SweepRow."""
            if isinstance(row, WalkForwardSweepRow):
                return row.row
            return row

        output_data = {
            "symbol": self._config.symbol,
            "timeframe": self._config.timeframe,
            "sweep_type": "regime_router_portfolio",
            "timestamp": timestamp,
            "total_runs": len(result),
            "successful_runs": len(result.rows),
            "top_by_sharpe": [
                {
                    "params": unwrap_row(row).params,
                    "win_rate": unwrap_row(row).win_rate,
                    "sharpe_ratio": unwrap_row(row).sharpe_ratio,
                    "profit_factor": unwrap_row(row).profit_factor,
                    "max_dd": unwrap_row(row).max_dd,
                    "total_return": unwrap_row(row).total_return,
                    "trade_count": unwrap_row(row).trade_count,
                }
                for row in result.top_n(10, "sharpe_ratio")
            ],
            "top_by_win_rate": [
                {
                    "params": unwrap_row(row).params,
                    "win_rate": unwrap_row(row).win_rate,
                    "sharpe_ratio": unwrap_row(row).sharpe_ratio,
                    "profit_factor": unwrap_row(row).profit_factor,
                    "max_dd": unwrap_row(row).max_dd,
                    "total_return": unwrap_row(row).total_return,
                    "trade_count": unwrap_row(row).trade_count,
                }
                for row in result.top_n(10, "win_rate")
            ],
            "top_by_profit_factor": [
                {
                    "params": unwrap_row(row).params,
                    "win_rate": unwrap_row(row).win_rate,
                    "sharpe_ratio": unwrap_row(row).sharpe_ratio,
                    "profit_factor": unwrap_row(row).profit_factor,
                    "max_dd": unwrap_row(row).max_dd,
                    "total_return": unwrap_row(row).total_return,
                    "trade_count": unwrap_row(row).trade_count,
                }
                for row in result.top_n(10, "profit_factor")
            ],
        }

        if any(isinstance(row, WalkForwardSweepRow) for row in result.rows):
            go_nogo_results = [
                row for row in result.rows if isinstance(row, WalkForwardSweepRow) and row.walk_forward_go_nogo
            ]

            output_data["walk_forward_go_nogo_count"] = len(go_nogo_results)
            output_data["top_walk_forward_by_sharpe"] = [
                {
                    "params": unwrap_row(row).params,
                    "win_rate": unwrap_row(row).win_rate,
                    "sharpe_ratio": unwrap_row(row).sharpe_ratio,
                    "profit_factor": unwrap_row(row).profit_factor,
                    "max_dd": unwrap_row(row).max_dd,
                    "total_return": unwrap_row(row).total_return,
                    "trade_count": unwrap_row(row).trade_count,
                    "walk_forward_windows_passed": row.walk_forward_windows_passed,
                    "walk_forward_total_windows": row.walk_forward_total_windows,
                }
                for row in sorted(
                    go_nogo_results,
                    key=lambda r: unwrap_row(r).sharpe_ratio,
                    reverse=True,
                )[:10]
            ]

        with open(filepath, "w") as f:
            json.dump(output_data, f, indent=2)

        logger.info(f"Results saved to {filepath}")
        return str(filepath)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Regime Router parameter sweep")
    add_resource_args(parser)
    parser.add_argument("--max-workers", type=int, default=2, help="ProcessPoolExecutor max workers")
    args = parser.parse_args()

    config = RegimeRouterSweepConfig(
        symbol="EURUSD",
        timeframe="H1",
        n_bars=10000,
        max_workers=args.max_workers,
    )

    runner = RegimeRouterSweepRunner(config)

    result = runner.run_sweep(run_walk_forward=True)

    filepath = runner.save_results(result)
    print(f"\nSweep completed. Results saved to: {filepath}")

    print("\nTop 5 by Sharpe Ratio:")
    for i, row in enumerate(result.top_n(5, "sharpe_ratio"), 1):
        print(f"  {i}. Sharpe: {row.sharpe_ratio:.3f}, Return: {row.total_return:.2f}%, Win Rate: {row.win_rate:.1f}%")
        print(f"     Params: {row.params}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
