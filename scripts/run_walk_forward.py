#!/usr/bin/env python3
"""
Generic Walk-Forward Runner CLI

Runs walk-forward validation for any registered strategy.

Usage:
    python scripts/run_walk_forward.py --strategy ma_crossover --pair EURUSD
    python scripts/run_walk_forward.py --strategy grid --pair GBPUSD --windows 5
    python scripts/run_walk_forward.py --list
"""

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader  # noqa: E402
from backtest.builtin_strategies import register_builtin_strategies  # noqa: E402, I001
from backtest.engine import get_spread_for_pair  # noqa: E402
from backtest.walk_forward_runner import (  # noqa: E402
    get_registered_strategies,
    run_named_strategy_walk_forward,
)
from common.resource_limits import add_resource_args, run_limited  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generic walk-forward runner for any strategy")
    add_resource_args(p)
    p.add_argument(
        "--strategy",
        type=str,
        required=False,
        help="Strategy name (use --list to see available)",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List available strategies and exit",
    )
    p.add_argument(
        "--pair",
        type=str,
        default="EURUSD",
        help="Currency pair (default: EURUSD)",
    )
    p.add_argument(
        "--data",
        type=str,
        default=None,
        help="Path to CSV data file (auto-detected from pair if omitted)",
    )
    p.add_argument(
        "--windows",
        type=int,
        default=5,
        help="Number of walk-forward windows (default: 5)",
    )
    p.add_argument(
        "--train-ratio",
        type=float,
        default=0.7,
        help="Train split ratio (default: 0.7)",
    )
    p.add_argument(
        "--balance",
        type=float,
        default=10000,
        help="Starting balance (default: 10000)",
    )
    p.add_argument(
        "--spread",
        type=float,
        default=None,
        help="Spread in pips (auto-detect from pair if omitted)",
    )
    p.add_argument(
        "--commission",
        type=float,
        default=None,
        help="Commission per lot (default: 3.5)",
    )
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to save JSON report",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    register_builtin_strategies(pair=args.pair)

    if args.list:
        print("Available strategies:")
        for name in get_registered_strategies():
            print(f"  - {name}")
        return

    if not args.strategy:
        parser.error("--strategy is required (use --list to see options)")

    data_file = args.data
    if data_file is None:
        pair_upper = args.pair.upper().replace("/", "")
        data_file = str(Path(project_root) / "data" / "forex" / "historical" / f"{pair_upper}_H1.csv")

    if not Path(data_file).exists():
        print(f"Error: Data file not found: {data_file}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.pair} data from {data_file}")
    loader = CsvDataLoader()
    bars = loader.load(data_file)
    print(f"Loaded {len(bars)} bars: {bars[0].time} to {bars[-1].time}")

    spread = args.spread if args.spread is not None else get_spread_for_pair(args.pair)
    print(f"Strategy: {args.strategy} | Pair: {args.pair} | Windows: {args.windows}")
    print(f"Train ratio: {args.train_ratio:.0%} | Spread: {spread} pips")

    results = run_named_strategy_walk_forward(
        strategy_name=args.strategy,
        bars=bars,
        pair=args.pair,
        n_windows=args.windows,
        train_ratio=args.train_ratio,
        initial_balance=args.balance,
        spread_pips=spread,
        commission_per_lot=args.commission,
    )

    print(f"\n{'=' * 70}")
    print(f"  WALK-FORWARD RESULTS: {args.strategy} on {args.pair}")
    print(f"{'=' * 70}")
    print(f"  Windows: {len(results.per_window)}")
    print(f"  GO/NO-GO: {'GO' if results.go_nogo else 'NO-GO'}")

    if results.aggregated:
        a = results.aggregated
        print("\n  Aggregate Metrics:")
        print(f"    Win Rate:       {a.mean_win_rate:.2%} (std {a.std_win_rate:.2%})")
        print(f"    Profit Factor:  {a.mean_profit_factor:.2f} (std {a.std_profit_factor:.2f})")
        print(f"    Max Drawdown:   {a.mean_max_drawdown:.2%} (std {a.std_max_drawdown:.2%})")
        print(f"    Sharpe Ratio:   {a.mean_sharpe_ratio:.2f} (std {a.std_sharpe_ratio:.2f})")
        print(f"    Trade Count:    {a.mean_trade_count:.0f} (std {a.std_trade_count:.0f})")
        print(f"    Total PnL:      ${a.mean_total_pnl:.2f} (std ${a.std_total_pnl:.2f})")
        print(f"    Windows Passed: {a.windows_passed}/{a.total_windows}")

    print("\n  Per-Window Details:")
    print(f"  {'Win':<8} {'PF':>8} {'MaxDD':>8} {'Sharpe':>8} {'Trades':>8} {'PnL':>12} {'GO?':>6}")
    for m in results.per_window:
        print(
            f"  {m.win_rate:<8.2%} {m.profit_factor:>8.2f} {m.max_drawdown:>8.2%} "
            f"{m.sharpe_ratio:>8.2f} {m.trade_count:>8} "
            f"${m.total_pnl:>10.2f} {'YES' if m.passed_go_nogo else 'NO':>6}"
        )
    print("=" * 70)

    if args.output:
        report = {
            "strategy": args.strategy,
            "pair": args.pair,
            "n_windows": args.windows,
            "train_ratio": args.train_ratio,
            "spread_pips": spread,
            "initial_balance": args.balance,
            "go_nogo": results.go_nogo,
            "per_window": [
                {
                    "window_index": m.window_index,
                    "win_rate": m.win_rate,
                    "profit_factor": m.profit_factor,
                    "max_drawdown": m.max_drawdown,
                    "sharpe_ratio": m.sharpe_ratio,
                    "trade_count": m.trade_count,
                    "total_pnl": m.total_pnl,
                    "passed_go_nogo": m.passed_go_nogo,
                }
                for m in results.per_window
            ],
            "aggregated": (
                {
                    "mean_win_rate": results.aggregated.mean_win_rate,
                    "std_win_rate": results.aggregated.std_win_rate,
                    "mean_profit_factor": results.aggregated.mean_profit_factor,
                    "std_profit_factor": results.aggregated.std_profit_factor,
                    "mean_max_drawdown": results.aggregated.mean_max_drawdown,
                    "std_max_drawdown": results.aggregated.std_max_drawdown,
                    "mean_sharpe_ratio": results.aggregated.mean_sharpe_ratio,
                    "std_sharpe_ratio": results.aggregated.std_sharpe_ratio,
                    "mean_trade_count": results.aggregated.mean_trade_count,
                    "std_trade_count": results.aggregated.std_trade_count,
                    "mean_total_pnl": results.aggregated.mean_total_pnl,
                    "std_total_pnl": results.aggregated.std_total_pnl,
                    "windows_passed": results.aggregated.windows_passed,
                    "total_windows": results.aggregated.total_windows,
                }
                if results.aggregated
                else None
            ),
        }
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n  Report saved: {args.output}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
