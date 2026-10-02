#!/usr/bin/env python3
"""
Optuna-Based Bayesian Parameter Optimization for Session Range MR Strategy

Runs Optuna TPE optimization with walk-forward validation objective.
Compares Optuna results against default parameters and manual sweep baselines.

Usage:
    python scripts/run_session_range_mr_optuna.py [--trials N] [--pair PAIR]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader  # noqa: E402
from backtest.engine import Bar  # noqa: E402, I001
from backtest.parameter_sweep.optuna_optimizer import (  # noqa: E402
    OptunaOptimizer,
    session_range_mr_search_space,
)
from backtest.walk_forward_runner import run_strategy_walk_forward  # noqa: E402
from common.resource_limits import add_resource_args, run_limited  # noqa: E402
from quant.walk_forward import comparison_report  # noqa: E402
from strategies.session_range_mean_reversion import (  # noqa: E402
    SessionRangeMeanReversionStrategy,
    SessionRangeMRConfig,
)

DATA_DIR = Path("data/forex/historical")
REPORT_DIR = Path("reports/optuna")

GBPUSD_PATH = DATA_DIR / "GBPUSD_H1.csv"
EURUSD_PATH = DATA_DIR / "EURUSD_H1.csv"

DEFAULT_CONFIG = {
    "n_windows": 5,
    "train_ratio": 0.70,
    "val_ratio": 0.15,
    "overlap_ratio": 0.20,
    "initial_balance": 10000.0,
    "spread_pips": 0.0,
    "commission_per_lot": 3.5,
}


def make_strategy(params: Dict[str, Any]) -> SessionRangeMeanReversionStrategy:
    config = SessionRangeMRConfig(**params)
    return SessionRangeMeanReversionStrategy(config=config)


def run_baseline(bars: List[Bar], pair: str) -> Any:
    strategy = SessionRangeMeanReversionStrategy()
    return run_strategy_walk_forward(
        bars=bars,
        strategy_factory=lambda: strategy,
        pair=pair,
        **DEFAULT_CONFIG,
    )


def run_optuna(
    bars: List[Bar],
    pair: str,
    n_trials: int = 100,
    seed: int = 42,
) -> Any:
    search_space = session_range_mr_search_space()
    optimizer = OptunaOptimizer(
        bars=bars,
        strategy_factory=make_strategy,
        pair=pair,
        search_space=search_space,
        n_trials=n_trials,
        seed=seed,
        **DEFAULT_CONFIG,
    )
    return optimizer.optimize()


def main() -> None:
    parser = argparse.ArgumentParser(description="Optuna optimization for Session Range MR")
    add_resource_args(parser)
    parser.add_argument("--trials", type=int, default=100, help="Number of Optuna trials")
    parser.add_argument("--pair", type=str, default="GBPUSD", help="Currency pair")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    pair = args.pair
    data_files = {
        "GBPUSD": GBPUSD_PATH,
        "EURUSD": EURUSD_PATH,
    }
    csv_path = data_files.get(pair)
    if csv_path is None or not csv_path.exists():
        print(f"Data file not found for {pair}: {csv_path}")
        sys.exit(1)

    loader = CsvDataLoader()
    bars = loader.load(str(csv_path))
    if not bars:
        print(f"No bars loaded for {pair}: {csv_path}")
        sys.exit(1)
    print(f"Loaded {len(bars)} bars for {pair}: {bars[0].time} -> {bars[-1].time}")

    print(f"\n{'=' * 70}")
    print(f"  BASELINE: Default Session Range MR Parameters ({pair})")
    print(f"{'=' * 70}")
    baseline_wf = run_baseline(bars, pair)
    if baseline_wf.aggregated:
        agg = baseline_wf.aggregated
        print(f"  Win Rate:     {agg.mean_win_rate:.4f}")
        print(f"  Profit Factor: {agg.mean_profit_factor:.4f}")
        print(f"  Max Drawdown:  {agg.mean_max_drawdown:.4f}")
        print(f"  Sharpe Ratio:  {agg.mean_sharpe_ratio:.4f}")
        print(f"  Trade Count:   {agg.mean_trade_count:.1f}")
        print(f"  Total PnL:     {agg.mean_total_pnl:.2f}")
        print(f"  Windows Pass:  {agg.windows_passed}/{agg.total_windows}")
        print(f"  GO/NO-GO:      {'GO' if baseline_wf.go_nogo else 'NO-GO'}")

    print(f"\n{'=' * 70}")
    print(f"  OPTUNA BAYESIAN OPTIMIZATION ({pair})")
    print(f"  Trials: {args.trials}, Seed: {args.seed}")
    print(f"{'=' * 70}")

    opt_result = run_optuna(bars, pair, n_trials=args.trials, seed=args.seed)

    print("\n  Optimization complete:")
    print(f"    Total trials:    {opt_result.n_trials}")
    print(f"    Completed:       {opt_result.study_summary.get('n_complete', 0)}")
    print(f"    Pruned:          {opt_result.study_summary.get('n_pruned', 0)}")
    print(f"    Best score:      {opt_result.best_value:.4f}")
    print(f"    GO/NO-GO:        {'GO' if opt_result.go_nogo else 'NO-GO'}")

    print("\n  Best parameters:")
    for key, value in sorted(opt_result.best_params.items()):
        print(f"    {key}: {value}")

    if opt_result.best_walk_forward and opt_result.best_walk_forward.aggregated:
        agg = opt_result.best_walk_forward.aggregated
        print("\n  Best walk-forward metrics:")
        print(f"    Win Rate:      {agg.mean_win_rate:.4f}")
        print(f"    Profit Factor: {agg.mean_profit_factor:.4f}")
        print(f"    Max Drawdown:   {agg.mean_max_drawdown:.4f}")
        print(f"    Sharpe Ratio:   {agg.mean_sharpe_ratio:.4f}")
        print(f"    Trade Count:    {agg.mean_trade_count:.1f}")
        print(f"    Total PnL:      {agg.mean_total_pnl:.2f}")
        print(f"    Windows Pass:   {agg.windows_passed}/{agg.total_windows}")

    print(f"\n{'=' * 70}")
    print(f"  COMPARISON: Default vs Optuna-Optimized ({pair})")
    print(f"{'=' * 70}")
    if opt_result.best_walk_forward and baseline_wf.aggregated:
        print(comparison_report(baseline_wf, opt_result.best_walk_forward))

    report = {
        "pair": pair,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_trials": args.trials,
        "seed": args.seed,
        "baseline": {
            "go_nogo": baseline_wf.go_nogo,
            "windows_passed": sum(1 for m in baseline_wf.per_window if m.passed_go_nogo),
            "total_windows": len(baseline_wf.per_window),
        },
        "optuna": {
            "best_params": opt_result.best_params,
            "best_score": opt_result.best_value,
            "go_nogo": opt_result.go_nogo,
            "n_trials": opt_result.n_trials,
            "study_summary": opt_result.study_summary,
        },
    }
    if baseline_wf.aggregated:
        agg = baseline_wf.aggregated
        report["baseline"]["metrics"] = {
            "win_rate": agg.mean_win_rate,
            "profit_factor": agg.mean_profit_factor,
            "max_drawdown": agg.mean_max_drawdown,
            "sharpe_ratio": agg.mean_sharpe_ratio,
            "trade_count": agg.mean_trade_count,
            "total_pnl": agg.mean_total_pnl,
        }
    if opt_result.best_walk_forward and opt_result.best_walk_forward.aggregated:
        agg = opt_result.best_walk_forward.aggregated
        report["optuna"]["metrics"] = {
            "win_rate": agg.mean_win_rate,
            "profit_factor": agg.mean_profit_factor,
            "max_drawdown": agg.mean_max_drawdown,
            "sharpe_ratio": agg.mean_sharpe_ratio,
            "trade_count": agg.mean_trade_count,
            "total_pnl": agg.mean_total_pnl,
        }

    report_path = REPORT_DIR / f"{pair}_session_range_mr_optuna.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  Report saved: {report_path}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
