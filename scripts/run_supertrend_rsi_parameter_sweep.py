#!/usr/bin/env python3
"""
Parameter Sweep + Walk-Forward for Supertrend RSI Blend Strategy (H1)

Runs a parameter sweep on the SupertrendRSIBlendStrategy across EURUSD and GBPUSD H1 data,
identifies top 5 parameter sets per pair, then runs full 5-window walk-forward validation.

Usage:
    python scripts/run_supertrend_rsi_parameter_sweep.py

GO Criteria: 3/5 windows pass (WR>55%, PF>1.2, DD<10%)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

EURUSD_PATH = "data/forex/historical/EURUSD_H1.csv"
GBPUSD_PATH = "data/forex/historical/GBPUSD_H1.csv"
REPORT_DIR = Path("reports/walk_forward")
SWEEP_REPORT_DIR = Path("reports/parameter_sweep")

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader  # noqa: E402
from backtest.engine import BacktestConfig  # noqa: E402, I001
from backtest.enhanced_engine import EnhancedBacktestEngine  # noqa: E402
from backtest.parameter_sweep import ParameterGrid, SweepRunner  # noqa: E402
from backtest.strategies import SupertrendRSIBlendStrategy  # noqa: E402
from common.resource_limits import add_resource_args, run_limited  # noqa: E402
from quant.go_nogo_criteria import PerWindowCriteria  # noqa: E402

SWEEP_PER_WINDOW = PerWindowCriteria(
    min_trades=5,
    win_rate=0.55,
    profit_factor=1.2,
    max_drawdown=0.10,
)


PARAM_SPACE: Dict[str, List[Any]] = {
    "supertrend_period": [7, 10, 12, 14, 21],
    "supertrend_multiplier": [2.0, 2.5, 3.0, 3.5],
    "rsi_period": [10, 14, 21],
    "rsi_threshold": [30.0, 40.0, 50.0, 60.0],
    "adx_min": [15.0, 20.0, 25.0, 30.0],
    "sl_atr_multiplier": [1.5, 2.0, 2.5, 3.0],
}

BACKTEST_CONFIG = {
    "starting_balance": 10000.0,
    "risk_per_trade_pct": 0.005,
    "max_daily_drawdown_pct": 0.03,
    "max_total_drawdown_pct": 0.05,
    "spread_pips": 0.0,
    "commission_per_lot": 3.5,
    "leverage": 100,
    "min_confidence": 0.5,
    "max_open_trades": 3,
    "min_bars_before_signal": 30,
    "round_trip_spread": True,
    "slippage_pips": 0.2,
    "swap_per_lot_per_day": -2.0,
}


def create_strategy(params: Dict[str, Any]) -> SupertrendRSIBlendStrategy:
    return SupertrendRSIBlendStrategy(
        supertrend_period=params["supertrend_period"],
        supertrend_multiplier=params["supertrend_multiplier"],
        rsi_period=params["rsi_period"],
        rsi_threshold=params["rsi_threshold"],
        adx_min=params["adx_min"],
        sl_atr_multiplier=params["sl_atr_multiplier"],
    )


def _strategy_factory(point):
    return create_strategy(point.params)


def run_sweep(
    bars: list,
    pair: str,
    config: BacktestConfig,
    max_workers: int | None = None,
) -> tuple:
    grid = ParameterGrid(PARAM_SPACE)
    print(f"  Parameter space: {len(grid)} combinations")

    runner = SweepRunner(
        config=config,
        bars=bars,
        strategy_factory=_strategy_factory,
        max_workers=max_workers,
    )
    result = runner.run(grid)

    return result, grid


def run_walk_forward_for_params(
    bars: list,
    params: Dict[str, Any],
    pair: str,
    config: BacktestConfig,
    n_windows: int = 5,
    train_ratio: float = 0.60,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
) -> Dict[str, Any]:
    strategy = create_strategy(params)
    engine = EnhancedBacktestEngine(config=config, strategies=[strategy])

    total = len(bars)
    window_size = total // n_windows
    buffer_ratio = 1.0 - train_ratio - val_ratio - test_ratio
    if buffer_ratio < 0:
        buffer_ratio = 0.0

    results = []

    for w in range(n_windows):
        start = w * window_size
        end = (w + 1) * window_size if w < n_windows - 1 else total
        window_bars = bars[start:end]
        wlen = len(window_bars)

        train_end = int(wlen * train_ratio)
        val_end = int(wlen * (train_ratio + val_ratio))
        buffer_end = int(wlen * (train_ratio + val_ratio + buffer_ratio))

        train_bars = window_bars[:train_end]
        val_bars = window_bars[train_end:val_end]
        test_bars = window_bars[buffer_end:]

        if len(test_bars) < 30:
            continue

        engine.run_strategy(strategy, train_bars)
        engine.run_strategy(strategy, val_bars)
        test_metrics = engine.run_strategy(strategy, test_bars)

        pw_result = SWEEP_PER_WINDOW.evaluate(
            trade_count=test_metrics.total_trades,
            win_rate=test_metrics.win_rate / 100.0,
            profit_factor=test_metrics.profit_factor,
            total_pnl=test_metrics.total_pnl if hasattr(test_metrics, "total_pnl") else 0.0,
            max_drawdown=test_metrics.max_drawdown_pct / 100.0,
        )
        passed = pw_result.passed

        results.append(
            {
                "window_id": w,
                "train_bars": len(train_bars),
                "val_bars": len(val_bars),
                "test_bars": len(test_bars),
                "test_trades": test_metrics.total_trades,
                "test_wr": test_metrics.win_rate,
                "test_pf": test_metrics.profit_factor,
                "test_dd": test_metrics.max_drawdown_pct,
                "passed_go_nogo": passed,
            }
        )

    windows_passed = sum(1 for r in results if r["passed_go_nogo"])
    total_test_trades = sum(r["test_trades"] for r in results)
    avg_wr = sum(r["test_wr"] for r in results) / len(results) if results else 0
    avg_pf = sum(r["test_pf"] for r in results) / len(results) if results else 0
    avg_dd = sum(r["test_dd"] for r in results) / len(results) if results else 0

    return {
        "params": params,
        "per_window": results,
        "windows_passed": windows_passed,
        "total_test_trades": total_test_trades,
        "avg_test_wr": avg_wr,
        "avg_test_pf": avg_pf,
        "avg_test_dd": avg_dd,
        "go_nogo": windows_passed >= 3,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Supertrend RSI blend parameter sweep + walk-forward")
    add_resource_args(parser)
    parser.add_argument("--max-workers", type=int, default=2, help="ProcessPoolExecutor max workers")
    args = parser.parse_args()

    SWEEP_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    loader = CsvDataLoader()

    pairs = [
        ("EURUSD", EURUSD_PATH),
        ("GBPUSD", GBPUSD_PATH),
    ]

    all_sweep_results = {}
    top_params_by_pair = {}

    for pair, csv_path in pairs:
        print(f"\n{'=' * 70}")
        print(f"  PARAMETER SWEEP: {pair}")
        print(f"{'=' * 70}")

        bars = loader.load(csv_path)
        print(f"  Loaded {len(bars)} bars: {bars[0].time} → {bars[-1].time}")

        config = BacktestConfig(pair=pair, **BACKTEST_CONFIG)

        print("\n  Running parameter sweep...")
        sweep_result, grid = run_sweep(bars, pair, config, max_workers=args.max_workers)

        print(f"  Sweep complete: {len(sweep_result)} parameter sets with trades")

        sweep_data = {
            "pair": pair,
            "param_space": PARAM_SPACE,
            "total_combinations": len(grid),
            "sets_with_trades": len(sweep_result),
            "results": [
                {
                    "params": row.params,
                    "win_rate": row.win_rate,
                    "profit_factor": row.profit_factor,
                    "max_drawdown_pct": row.max_dd,
                    "sharpe_ratio": row.sharpe_ratio,
                    "total_trades": row.trade_count,
                    "total_return": row.total_return,
                }
                for row in sweep_result
            ],
        }

        sweep_path = SWEEP_REPORT_DIR / f"{pair}_supertrend_sweep.json"
        with open(sweep_path, "w") as f:
            json.dump(sweep_data, f, indent=2)
        print(f"  Sweep report saved: {sweep_path}")

        all_sweep_results[pair] = sweep_data

        profitable = sweep_result.filter(lambda r: r.profit_factor > 1.0 and r.trade_count >= 10)
        print(f"  Profitable sets (PF>1.0, trades>=10): {len(profitable)}")

        top5 = sweep_result.top_n(5, metric="profit_factor", ascending=False)
        top_params_by_pair[pair] = top5

        print("\n  Top 5 parameter sets by profit_factor:")
        for i, row in enumerate(top5):
            print(
                f"    {i + 1}. PF={row.profit_factor:.2f}, WR={row.win_rate:.1f}%, "
                f"DD={row.max_dd:.2f}%, trades={row.trade_count}"
            )
            print(f"       {row.params}")

    print(f"\n{'=' * 70}")
    print("  WALK-FORWARD VALIDATION ON TOP 5 PARAM SETS")
    print(f"{'=' * 70}")

    final_results = {}

    for pair, top5 in top_params_by_pair.items():
        print(f"\n  {pair}:")
        bars = loader.load(pairs[[p for p, _ in pairs].index(pair)][1])
        config = BacktestConfig(pair=pair, **BACKTEST_CONFIG)

        pair_wf_results = []
        for i, row in enumerate(top5):
            print(f"\n  [{i + 1}/{len(top5)}] Testing params: {row.params}")
            wf_result = run_walk_forward_for_params(bars, row.params, pair, config)

            status = "GO" if wf_result["go_nogo"] else "NO-GO"
            print(f"      Windows passed: {wf_result['windows_passed']}/5")
            print(
                f"      Avg test WR: {wf_result['avg_test_wr']:.1f}%, PF: {wf_result['avg_test_pf']:.2f}, DD: {wf_result['avg_test_dd']:.2f}%"  # noqa: E501
            )
            print(f"      Status: {status}")

            pair_wf_results.append(wf_result)

        final_results[pair] = pair_wf_results

        wf_report_path = REPORT_DIR / f"{pair}_supertrend_rsi_sweep_walkforward.json"
        with open(wf_report_path, "w") as f:
            json.dump(pair_wf_results, f, indent=2, default=str)
        print(f"\n  Walk-forward report saved: {wf_report_path}")

    print(f"\n{'=' * 70}")
    print("  FINAL SUMMARY")
    print(f"{'=' * 70}")

    for pair, results in final_results.items():
        print(f"\n  {pair}:")
        for i, r in enumerate(results):
            status = "GO" if r["go_nogo"] else "NO-GO"
            print(f"    Param set {i + 1}: {r['params']}")
            print(f"      -> {status} ({r['windows_passed']}/5 windows passed)")

        go_sets = [r for r in results if r["go_nogo"]]
        if go_sets:
            print(f"\n  *** {pair}: {len(go_sets)} parameter set(s) passed 3/5 windows - GO! ***")
        else:
            print(f"\n  {pair}: No parameter sets passed - NO-GO")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
