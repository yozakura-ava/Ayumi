#!/usr/bin/env python3
"""
Parameter Sweep + Walk-Forward for Volatility Squeeze (H1)

Runs a parameter sweep for Volatility Squeeze strategy on GBPJPY and EURUSD H1 data,
selects top 5 parameter sets per pair, then runs full 5-window walk-forward validation.

Usage:
    python scripts/run_volatility_squeeze_sweep_walkforward.py
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).parent.parent
scripts_dir = Path(__file__).parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))
sys.path.insert(0, str(scripts_dir))

from backtest import CsvDataLoader
from backtest.engine import BacktestConfig
from backtest.parameter_sweep.grid import ParameterGrid
from common.resource_limits import add_resource_args, run_limited
from strategies.volatility_squeeze import (
    VolatilitySqueezeConfig,
    VolatilitySqueezeStrategy,
)
from volatility_sweep_runner import SweepRunner

EURUSD_PATH = "data/forex/historical/EURUSD_H1.csv"
GBPJPY_PATH = "data/forex/historical/GBPJPY_H1.csv"
REPORT_DIR = Path("reports/parameter_sweeps")
WALKFORWARD_REPORT_DIR = Path("reports/walk_forward")


PARAM_GRID = {
    "bb_period": [15, 20, 25],
    "bb_std": [1.5, 2.0, 2.5],
    "kc_period": [14, 20],
    "kc_atr_multiplier": [1.5, 2.0, 2.5],
    "squeeze_bars_threshold": [3, 5, 7],
    "adx_min": [15, 20, 25],
    "squeeze_release_mode": ["strict", "moderate", "loose"],
}

SWEEP_BARS_SUBSET = 5000


def run_sweep(
    bars: list,
    pair: str,
    param_grid: dict[str, list[Any]],
    report_path: Path | None = None,
    max_workers: int = 2,  # F821 fix (card 9cdbfd0a): was `args.max_workers` (undefined in this scope)
) -> dict:
    sweep_bars = bars[:SWEEP_BARS_SUBSET]

    config = BacktestConfig(
        starting_balance=10000.0,
        spread_pips=1.5 if pair == "EURUSD" else 3.0,
        commission_per_lot=3.5,
        pair=pair,
        min_bars_before_signal=50,
    )

    def strategy_factory(point):
        return VolatilitySqueezeStrategy(
            config=VolatilitySqueezeConfig(
                bb_period=point.params["bb_period"],
                bb_std_dev=point.params["bb_std"],
                kc_period=point.params["kc_period"],
                kc_atr_multiplier=point.params["kc_atr_multiplier"],
                min_squeeze_bars=point.params["squeeze_bars_threshold"],
                adx_min=point.params["adx_min"],
                squeeze_release_mode=point.params["squeeze_release_mode"],
            )
        )

    grid = ParameterGrid(param_grid)
    runner = SweepRunner(
        config=config,
        bars=sweep_bars,
        strategy_factory=strategy_factory,
        max_workers=max_workers,
    )
    result = runner.run(grid)

    print(f"\n  Swept {len(grid)} parameter combinations on {len(sweep_bars)} bars")
    print(f"  Generated {len(result)} results with trades")

    trade_results = [row for row in result if row.trade_count > 0]
    print(f"  Results with trades: {len(trade_results)}")

    if report_path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "pair": pair,
            "total_combinations": len(grid),
            "sweep_bars_used": len(sweep_bars),
            "results_with_trades": len(trade_results),
            "all_results": [
                {
                    "params": row.params,
                    "win_rate": row.win_rate,
                    "profit_factor": row.profit_factor,
                    "max_dd": row.max_dd,
                    "sharpe_ratio": row.sharpe_ratio,
                    "trade_count": row.trade_count,
                    "total_return": row.total_return,
                }
                for row in result
            ],
            "results_with_trades_list": [
                {
                    "params": row.params,
                    "win_rate": row.win_rate,
                    "profit_factor": row.profit_factor,
                    "max_dd": row.max_dd,
                    "sharpe_ratio": row.sharpe_ratio,
                    "trade_count": row.trade_count,
                    "total_return": row.total_return,
                }
                for row in trade_results
            ],
        }
        with open(report_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"  Sweep report saved: {report_path}")

    return {
        "all_results": result,
        "trade_results": trade_results,
    }


def run_walkforward_on_params(
    bars: list,
    pair: str,
    params: dict,
    n_windows: int = 5,
    train_ratio: float = 0.7,
) -> dict:
    from backtest.walk_forward_runner import run_strategy_walk_forward

    def strategy_factory():
        return VolatilitySqueezeStrategy(
            config=VolatilitySqueezeConfig(
                bb_period=params["bb_period"],
                bb_std_dev=params["bb_std"],
                kc_period=params["kc_period"],
                kc_atr_multiplier=params["kc_atr_multiplier"],
                min_squeeze_bars=params["squeeze_bars_threshold"],
                adx_min=params["adx_min"],
                squeeze_release_mode=params["squeeze_release_mode"],
            )
        )

    results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=strategy_factory,
        pair=pair,
        n_windows=n_windows,
        train_ratio=train_ratio,
    )

    return results


def filter_min_trades_per_window(
    all_results: list,
    bars: list,
    pair: str,
    min_trades: int = 5,
    n_windows: int = 5,
) -> list:

    filtered = []
    for row in all_results:
        params = row.params
        wf_result = run_walkforward_on_params(
            bars=bars,
            pair=pair,
            params=params,
            n_windows=n_windows,
        )
        min_trades_in_windows = min(w.trade_count for w in wf_result.per_window if w is not None)
        if min_trades_in_windows >= min_trades:
            filtered.append((row, wf_result))
        else:
            print(f"    Rejected {params}: min trades {min_trades_in_windows} < {min_trades}")

    return filtered


def top_n_with_scores(trade_results, n: int = 5) -> list:
    scored = []
    for row in trade_results:
        score = 0.0
        if row.win_rate > 0:
            score += row.win_rate * 0.3
        if row.profit_factor > 0:
            score += min(row.profit_factor / 3.0, 1.0) * 0.3
        if row.max_dd > 0:
            score += (1.0 - min(row.max_dd / 0.10, 1.0)) * 0.2
        if row.sharpe_ratio > 0:
            score += min(row.sharpe_ratio / 2.0, 1.0) * 0.2
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in scored[:n]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Volatility Squeeze sweep + walk-forward")
    add_resource_args(parser)
    parser.add_argument("--max-workers", type=int, default=2, help="ProcessPoolExecutor max workers")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    WALKFORWARD_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    loader = CsvDataLoader()

    pairs = [
        ("GBPJPY", GBPJPY_PATH),
        ("EURUSD", EURUSD_PATH),
    ]

    all_sweep_results = {}
    all_top_params = {}

    for pair, csv_path in pairs:
        print(f"\n{'=' * 70}")
        print(f"  PARAMETER SWEEP: Volatility Squeeze on {pair}")
        print(f"{'=' * 70}")

        bars = loader.load(csv_path)
        print(f"  Loaded {len(bars)} bars: {bars[0].time} -> {bars[-1].time}")

        sweep_report_path = REPORT_DIR / f"{pair}_volatility_squeeze_sweep.json"
        sweep_result = run_sweep(
            bars=bars,
            pair=pair,
            param_grid=PARAM_GRID,
            report_path=sweep_report_path,
            max_workers=args.max_workers,
        )

        trade_results = sweep_result["trade_results"]
        if not trade_results:
            print(f"\n  WARNING: No trades generated for {pair} with any parameter combination!")
            all_sweep_results[pair] = {"trade_results": [], "all_results": []}
            all_top_params[pair] = []
            continue

        top5 = top_n_with_scores(trade_results, n=5)
        all_top_params[pair] = top5
        all_sweep_results[pair] = sweep_result

        print(f"\n  Top 5 parameter sets for {pair}:")
        for i, row in enumerate(top5):
            print(
                f"    {i + 1}. bb_p={row.params['bb_period']}, bb_s={row.params['bb_std']}, "
                f"kc_p={row.params['kc_period']}, kc_mult={row.params['kc_atr_multiplier']}, "
                f"squeeze={row.params['squeeze_bars_threshold']}, adx={row.params['adx_min']}, "
                f"mode={row.params['squeeze_release_mode']}"
            )
            print(
                f"       WR={row.win_rate:.1%}, PF={row.profit_factor:.2f}, "
                f"DD={row.max_dd:.1%}, Sharpe={row.sharpe_ratio:.2f}, Trades={row.trade_count}"
            )

    print(f"\n{'=' * 70}")
    print("  WALK-FORWARD VALIDATION: Top 5 params per pair")
    print(f"{'=' * 70}")

    walkforward_results = {}

    for pair, top5 in all_top_params.items():
        if not top5:
            print(f"\n  Skipping {pair} - no parameter sets with trades")
            walkforward_results[pair] = {
                "go_nogo": False,
                "per_window": [],
                "top5_results": [],
            }
            continue

        bars = loader.load(EURUSD_PATH if pair == "EURUSD" else GBPJPY_PATH)
        print(f"\n  Running 5-window walk-forward for {pair} top 5 params on {len(bars)} bars...")

        pair_wf_results = []
        for i, row in enumerate(top5):
            params = row.params
            print(
                f"\n    [{i + 1}/5] Testing: bb_p={params['bb_period']}, bb_s={params['bb_std']}, "
                f"kc_p={params['kc_period']}, kc_mult={params['kc_atr_multiplier']}, "
                f"squeeze={params['squeeze_bars_threshold']}, adx={params['adx_min']}, "
                f"mode={params['squeeze_release_mode']}"
            )

            wf_result = run_walkforward_on_params(bars, pair, params)

            go_status = "GO" if wf_result.go_nogo else "NO-GO"
            passed = sum(1 for m in wf_result.per_window if m.passed_go_nogo)
            total = len(wf_result.per_window)

            if wf_result.aggregated:
                print(f"       {go_status} ({passed}/{total} windows)")
                print(
                    f"       WR={wf_result.aggregated.mean_win_rate:.1%}, "
                    f"PF={wf_result.aggregated.mean_profit_factor:.2f}, "
                    f"DD={wf_result.aggregated.mean_max_drawdown:.1%}, "
                    f"Sharpe={wf_result.aggregated.mean_sharpe_ratio:.2f}"
                )
            else:
                print("       NO-GO (no aggregated metrics)")

            pair_wf_results.append(
                {
                    "params": params,
                    "go_nogo": wf_result.go_nogo,
                    "aggregated": wf_result.aggregated,
                    "per_window": wf_result.per_window,
                }
            )

        any_go = any(r["go_nogo"] for r in pair_wf_results)
        print(f"\n  {pair} Summary: {'AT LEAST ONE PARAM SET PASSED' if any_go else 'ALL PARAM SETS FAILED'}")

        walkforward_results[pair] = {
            "go_nogo": any_go,
            "top5_results": pair_wf_results,
        }

    combined_sweep_path = REPORT_DIR / "combined_volatility_squeeze_sweep.json"
    with open(combined_sweep_path, "w") as f:
        json.dump(all_sweep_results, f, indent=2, default=str)
    print(f"\n  Combined sweep report: {combined_sweep_path}")

    print(f"\n{'=' * 70}")
    print("  FINAL SUMMARY: Volatility Squeeze Walk-Forward")
    print(f"{'=' * 70}")

    for pair, wf_result in walkforward_results.items():
        go_nogo = wf_result["go_nogo"]
        top5_results = wf_result.get("top5_results", [])

        best_params = None
        best_windows_passed = 0
        for r in top5_results:
            if r["aggregated"]:
                passed = r["aggregated"].windows_passed
                if passed > best_windows_passed:
                    best_windows_passed = passed
                    best_params = r["params"]

        status = "GO" if go_nogo else "NO-GO"
        print(f"\n  {pair}: {status}")
        if best_params:
            print(
                f"    Best: bb_p={best_params['bb_period']}, bb_s={best_params['bb_std']}, "
                f"kc_p={best_params['kc_period']}, kc_mult={best_params['kc_atr_multiplier']}, "
                f"squeeze={best_params['squeeze_bars_threshold']}, adx={best_params['adx_min']}, "
                f"mode={best_params['squeeze_release_mode']}"
            )
            print(f"    Windows passed: {best_windows_passed}/5")

    overall_go = any(r["go_nogo"] for r in walkforward_results.values())
    print(f"\n  OVERALL: {'GO' if overall_go else 'NO-GO'} (need at least one pair with 3/5 windows passing)")

    final_report = {
        "strategy": "VolatilitySqueeze",
        "pairs": list(walkforward_results.keys()),
        "overall_go": overall_go,
        "pair_results": walkforward_results,
    }

    final_report_path = WALKFORWARD_REPORT_DIR / "volatility_squeeze_sweep_walkforward_results.json"
    with open(final_report_path, "w") as f:
        json.dump(final_report, f, indent=2, default=str)
    print(f"\n  Final report saved: {final_report_path}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
