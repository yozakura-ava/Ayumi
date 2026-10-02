#!/usr/bin/env python3
"""
Trailing Stop Parameter Sweep + Walk-Forward Validation

Sweeps TrailingStopConfig parameters (method, atr_multiplier, step_pips,
time_tighten_bars, sar settings) using a fixed MomentumBreakoutStrategy
on GBPUSD H1 data. Top configs go through 5-window walk-forward validation.

GO Criteria: 3/5 windows pass (WR>55%, PF>1.2, DD<10%)
NO-GO: Fewer than 3 windows pass

Usage:
    python scripts/run_trailing_stop_sweep.py
    python scripts/run_trailing_stop_sweep.py --pair GBPUSD --max-workers 2
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.engine import BacktestConfig, Bar
from backtest.enhanced_engine import EnhancedBacktestEngine
from backtest.parameter_sweep.grid import ParameterGrid
from backtest.parameter_sweep.result import SweepResult, SweepRow
from backtest.strategies import MomentumBreakoutStrategy
from backtest.trade_management import (
    TradeManagementConfig,
    TrailingStopMethod,
)
from common.resource_limits import add_resource_args, run_limited
from quant.walk_forward import WalkForwardValidator  # noqa: I001

REPORT_DIR = Path("reports/parameter_sweeps")
WALKFORWARD_REPORT_DIR = Path("reports/walk_forward")
SWEEP_BARS_SUBSET = 5000
N_WINDOWS = 5
TRAIN_RATIO = 0.60
VAL_RATIO = 0.15

GBPUSD_PATH = "data/forex/historical/GBPUSD_H1.csv"
EURUSD_PATH = "data/forex/historical/EURUSD_H1.csv"

PARAM_SPACE_ATR = {
    "atr_multiplier": [1.0, 1.5, 2.0, 2.5, 3.0],
}

PARAM_SPACE_STEP = {
    "step_pips": [5.0, 10.0, 15.0, 20.0],
}

PARAM_SPACE_TIME = {
    "time_tighten_bars": [12, 24, 36],
    "time_tighten_pct": [0.4, 0.5, 0.6],
}

PARAM_SPACE_SAR = {
    "sar_af_start": [0.01, 0.02, 0.03],
    "sar_af_max": [0.15, 0.20, 0.25],
}

ALL_METHODS = {
    TrailingStopMethod.ATR: PARAM_SPACE_ATR,
    TrailingStopMethod.STEP: PARAM_SPACE_STEP,
    TrailingStopMethod.TIME: PARAM_SPACE_TIME,
    TrailingStopMethod.PARABOLIC_SAR: PARAM_SPACE_SAR,
}

BASE_STRATEGY_PARAMS = {
    "fast_period": 9,
    "slow_period": 21,
    "adx_threshold": 25.0,
    "atr_multiplier": 2.0,
}

BACKTEST_CONFIG = {
    "starting_balance": 10000.0,
    "risk_per_trade_pct": 0.005,
    "max_daily_drawdown_pct": 0.03,
    "max_total_drawdown_pct": 0.05,
    "spread_pips": 0.5,
    "commission_per_lot": 3.5,
    "leverage": 100,
    "min_confidence": 0.5,
    "max_open_trades": 1,
    "min_bars_before_signal": 30,
    "round_trip_spread": True,
    "slippage_pips": 0.2,
    "swap_per_lot_per_day": -2.0,
}


def make_tm_config(method: TrailingStopMethod, params: dict[str, Any]) -> TradeManagementConfig:
    tm_config = TradeManagementConfig()
    tm_config.trailing_stop.enabled = True
    tm_config.trailing_stop.method = method
    tm_config.trailing_stop.atr_multiplier = params.get("atr_multiplier", 1.5)
    tm_config.trailing_stop.step_pips = params.get("step_pips", 10.0)
    tm_config.trailing_stop.time_tighten_bars = params.get("time_tighten_bars", 24)
    tm_config.trailing_stop.time_tighten_pct = params.get("time_tighten_pct", 0.5)
    tm_config.trailing_stop.sar_af_start = params.get("sar_af_start", 0.02)
    tm_config.trailing_stop.sar_af_max = params.get("sar_af_max", 0.20)
    tm_config.trailing_stop.only_after_tier = 1
    tm_config.partial_exit.enabled = True
    tm_config.exit_refinement.enabled = False
    tm_config.session_filter.enabled = True
    return tm_config


def run_single_backtest(
    bars: list[Bar],
    tm_config: TradeManagementConfig,
    pair: str,
) -> dict[str, Any] | None:
    config = BacktestConfig(pair=pair, **BACKTEST_CONFIG)
    strategy = MomentumBreakoutStrategy(**BASE_STRATEGY_PARAMS)
    engine = EnhancedBacktestEngine(
        config=config,
        strategies=[strategy],
        tm_config=tm_config,
    )
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
        print(f"        Backtest failed: {exc}")
        return None


def run_sweep_for_method(
    bars: list[Bar],
    pair: str,
    method: TrailingStopMethod,
    method_params: dict[str, list[Any]],
    max_workers: int | None = None,
) -> tuple[SweepResult, list[dict]]:
    grid = ParameterGrid(method_params)
    print(f"    {method.value}: {len(grid)} combinations")

    results: list[SweepRow] = []
    all_params: list[dict] = []

    for point in grid:
        tm_config = make_tm_config(method, point.params)
        result = run_single_backtest(bars, tm_config, pair)
        if result is not None:
            row = SweepRow(params=point.params, **result)
            results.append(row)
            all_params.append(point.params)

    return SweepResult(rows=results), all_params


def run_walk_forward(
    bars: list[Bar],
    method: TrailingStopMethod,
    params: dict[str, Any],
    pair: str,
    config: BacktestConfig,
) -> dict[str, Any]:

    tm_config = make_tm_config(method, params)
    strategy = MomentumBreakoutStrategy(**BASE_STRATEGY_PARAMS)

    validator = WalkForwardValidator(
        data=bars,
        n_windows=N_WINDOWS,
        train_ratio=TRAIN_RATIO,
        val_ratio=VAL_RATIO,
        overlap_ratio=0.2,
    )

    per_window = []
    for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):  # noqa: B007
        if len(test_bars) < config.min_bars_before_signal:
            per_window.append(
                {
                    "window_id": idx,
                    "win_rate": 0,
                    "profit_factor": 0,
                    "max_drawdown": 0,
                    "sharpe_ratio": 0,
                    "trade_count": 0,
                    "total_pnl": 0,
                    "passed_go_nogo": False,
                }
            )
            continue

        engine = EnhancedBacktestEngine(
            config=config,
            strategies=[strategy],
            tm_config=tm_config,
        )
        try:
            metrics = engine.run_strategy(strategy, test_bars)
            passed = metrics.win_rate >= 55.0 and metrics.profit_factor >= 1.2 and metrics.max_drawdown_pct <= 10.0
            per_window.append(
                {
                    "window_id": idx,
                    "win_rate": round(metrics.win_rate, 1),
                    "profit_factor": round(metrics.profit_factor, 2),
                    "max_drawdown": round(metrics.max_drawdown_pct, 2),
                    "sharpe_ratio": round(metrics.sharpe_ratio, 2),
                    "trade_count": metrics.total_trades,
                    "total_pnl": round(metrics.total_pnl_pct, 4),
                    "passed_go_nogo": passed,
                }
            )
        except Exception:
            per_window.append(
                {
                    "window_id": idx,
                    "win_rate": 0,
                    "profit_factor": 0,
                    "max_drawdown": 0,
                    "sharpe_ratio": 0,
                    "trade_count": 0,
                    "total_pnl": 0,
                    "passed_go_nogo": False,
                }
            )

    windows_passed = sum(1 for r in per_window if r["passed_go_nogo"])
    avg_wr = sum(r["win_rate"] for r in per_window) / len(per_window) if per_window else 0
    avg_pf = sum(r["profit_factor"] for r in per_window) / len(per_window) if per_window else 0
    avg_dd = sum(r["max_drawdown"] for r in per_window) / len(per_window) if per_window else 0

    go_nogo = windows_passed >= 3

    return {
        "method": method.value,
        "params": params,
        "per_window": per_window,
        "windows_passed": windows_passed,
        "total_windows": len(per_window),
        "avg_wr": round(avg_wr, 1),
        "avg_pf": round(avg_pf, 2),
        "avg_dd": round(avg_dd, 2),
        "go_nogo": go_nogo,
    }


def main():
    parser = argparse.ArgumentParser(description="Trailing Stop Parameter Sweep")
    add_resource_args(parser)
    parser.add_argument("--pair", default="GBPUSD", choices=["GBPUSD", "EURUSD"])
    parser.add_argument("--max-workers", type=int, default=2)
    args = parser.parse_args()

    pair = args.pair
    max_workers = args.max_workers

    csv_path = GBPUSD_PATH if pair == "GBPUSD" else EURUSD_PATH
    print(f"\n{'=' * 70}")
    print(f"  TRAILING STOP PARAMETER SWEEP: {pair}")
    print(f"{'=' * 70}")

    loader = CsvDataLoader()
    bars = loader.load(csv_path)
    print(f"  Loaded {len(bars)} bars: {bars[0].time} -> {bars[-1].time}")

    sweep_bars = bars[-SWEEP_BARS_SUBSET:] if len(bars) > SWEEP_BARS_SUBSET else bars
    print(f"  Using last {len(sweep_bars)} bars for sweep phase")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    WALKFORWARD_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    method_results: dict[str, list[dict]] = {}

    for method, method_params in ALL_METHODS.items():
        print(f"\n  Sweeping {method.value.upper()}...")
        sweep_result, all_params = run_sweep_for_method(sweep_bars, pair, method, method_params, max_workers)
        print(f"    {len(sweep_result)} combos with trades")

        for row in sweep_result:
            method_results.setdefault(method.value, []).append(
                {
                    "params": row.params,
                    "win_rate": row.win_rate,
                    "max_dd": row.max_dd,
                    "sharpe_ratio": row.sharpe_ratio,
                    "trade_count": row.trade_count,
                    "profit_factor": row.profit_factor,
                    "total_return": row.total_return,
                }
            )

    sweep_data = {
        "pair": pair,
        "methods": list(ALL_METHODS.keys()),
        "results_by_method": method_results,
    }
    sweep_path = REPORT_DIR / f"{pair}_trailing_stop_sweep.json"
    with open(sweep_path, "w") as f:
        json.dump(sweep_data, f, indent=2, default=str)
    print(f"\n  Sweep report saved: {sweep_path}")

    print(f"\n{'=' * 70}")
    print("  WALK-FORWARD VALIDATION (top 3 per method)")
    print(f"{'=' * 70}")

    wf_all: list[dict] = []
    config = BacktestConfig(pair=pair, **BACKTEST_CONFIG)

    for method, method_params in ALL_METHODS.items():  # noqa: B007
        print(f"\n  {method.value.upper()}:")

        rows_data = method_results.get(method.value, [])
        if not rows_data:
            print(f"    No qualifying results for {method.value}")
            continue

        sorted_rows = sorted(
            rows_data,
            key=lambda r: (r["profit_factor"], r["win_rate"]),
            reverse=True,
        )
        top3 = sorted_rows[:3]

        for i, row in enumerate(top3):
            params = row["params"]
            print(f"\n  [{i + 1}/3] Params: {params}")
            print(
                f"       Sweep: WR={row['win_rate']:.1f}%, PF={row['profit_factor']:.2f}, DD={row['max_dd']:.2f}%, trades={row['trade_count']}"  # noqa: E501
            )

            wf_result = run_walk_forward(bars, method, params, pair, config)
            status = "GO" if wf_result["go_nogo"] else "NO-GO"
            print(
                f"       WF:   {wf_result['windows_passed']}/{wf_result['total_windows']} windows passed, avg WR={wf_result['avg_wr']:.1f}%, PF={wf_result['avg_pf']:.2f}, DD={wf_result['avg_dd']:.2f}%"  # noqa: E501
            )
            print(f"       -> {status}")

            wf_all.append(wf_result)

    wf_path = WALKFORWARD_REPORT_DIR / f"{pair}_trailing_stop_sweep_walkforward.json"
    with open(wf_path, "w") as f:
        json.dump(wf_all, f, indent=2, default=str)
    print(f"\n  Walk-forward report saved: {wf_path}")

    print(f"\n{'=' * 70}")
    print("  FINAL SUMMARY")
    print(f"{'=' * 70}")

    go_sets = [r for r in wf_all if r["go_nogo"]]
    if go_sets:
        print(f"\n  *** {len(go_sets)} parameter set(s) passed 3/5 windows - GO! ***")
        for r in go_sets:
            print(
                f"     {r['method']}: {r['params']} -> WR={r['avg_wr']:.1f}%, PF={r['avg_pf']:.2f}, DD={r['avg_dd']:.2f}%"  # noqa: E501
            )
    else:
        print("\n  NO parameter sets passed 3/5 windows - NO-GO")

    print(f"\n{'=' * 70}")
    print("  Done. Reports saved to:")
    print(f"    {sweep_path}")
    print(f"    {wf_path}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
