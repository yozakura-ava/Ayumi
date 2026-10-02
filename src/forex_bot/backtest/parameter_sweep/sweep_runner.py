from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

from ..engine import BacktestConfig, Bar
from ..enhanced_engine import EnhancedBacktestEngine
from ..strategies import ISignalStrategy
from .grid import GridPoint, ParameterGrid
from .result import SweepResult, SweepRow

logger = logging.getLogger(__name__)

StrategyFactory = Callable[[GridPoint], ISignalStrategy]


def _worker_entry(args: tuple) -> dict[str, Any] | None:
    config_dict, bars_data, strategy_config = args
    config = BacktestConfig(**config_dict)
    bars = [Bar(**b) for b in bars_data]
    strategy = _rebuild_strategy(strategy_config)
    engine = EnhancedBacktestEngine(config=config, strategies=[strategy])
    try:
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
        logger.warning("Backtest failed in worker: %s", exc)
        raise


def _rebuild_strategy(config: dict[str, Any]) -> ISignalStrategy:
    cls_name = config["__class__"]
    params = {k: v for k, v in config.items() if k != "__class__"}
    from ..strategies import (
        BBStrategy,
        HighConvictionStrategy,
        KeltnerChannelBreakoutStrategy,
        MACrossStrategy,
        MomentumBreakoutStrategy,
        ROCMStrategy,
        RSIStrategy,
        SRBreakoutStrategy,
        SupertrendRSIBlendStrategy,
    )

    registry: dict[str, type] = {
        "MACrossStrategy": MACrossStrategy,
        "BBStrategy": BBStrategy,
        "KeltnerChannelBreakoutStrategy": KeltnerChannelBreakoutStrategy,
        "RSIStrategy": RSIStrategy,
        "SRBreakoutStrategy": SRBreakoutStrategy,
        "ROCMStrategy": ROCMStrategy,
        "HighConvictionStrategy": HighConvictionStrategy,
        "SupertrendRSIBlendStrategy": SupertrendRSIBlendStrategy,
        "MomentumBreakoutStrategy": MomentumBreakoutStrategy,
    }
    cls = registry.get(cls_name)
    if cls is None:
        raise ValueError(f"Unknown strategy class: {cls_name}")
    return cls(**params)


def _serialize_strategy(strategy: ISignalStrategy) -> dict[str, Any]:
    params = {k: v for k, v in strategy.__dict__.items() if not k.startswith("_")}
    params["__class__"] = type(strategy).__name__
    return params


def _serialize_bars(bars: list[Bar]) -> list[dict[str, Any]]:
    from dataclasses import asdict

    return [asdict(b) for b in bars]


class SweepRunner:
    def __init__(
        self,
        config: BacktestConfig,
        bars: list[Bar],
        strategy_factory: StrategyFactory,
        max_workers: int | None = None,
    ):
        self._config = config
        self._bars = bars
        self._strategy_factory = strategy_factory
        self._max_workers = max_workers if max_workers is not None else os.cpu_count()

    def run(self, grid: ParameterGrid) -> SweepResult:
        rows: list[SweepRow] = []
        tasks: list[tuple] = []
        grid_points: list[GridPoint] = []

        config_dict = self._config_to_dict()
        bars_data = _serialize_bars(self._bars)

        for point in grid:
            strategy = self._strategy_factory(point)
            strategy_config = _serialize_strategy(strategy)
            tasks.append((config_dict, bars_data, strategy_config))
            grid_points.append(point)

        if self._max_workers is not None and self._max_workers > 1 and len(tasks) > 1:
            with ProcessPoolExecutor(max_workers=self._max_workers) as executor:
                future_to_idx = {executor.submit(_worker_entry, task): idx for idx, task in enumerate(tasks)}
                results: list[dict[str, Any] | None] = [None] * len(tasks)  # type: ignore[assignment]
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    results[idx] = future.result()
        else:
            results = [_worker_entry(task) for task in tasks]

        for point, result in zip(grid_points, results):  # noqa: B905
            if result is not None:
                rows.append(SweepRow(params=point.params, **result))

        return SweepResult(rows=rows)

    def _config_to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self._config)
