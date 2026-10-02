#!/usr/bin/env python3
"""
VAPS vs Fixed Sizing A/B Walk-Forward Comparison

Runs identical walk-forward windows with fixed fractional sizing and
VAPS volatility-adaptive position sizing on the same strategy and data.

Usage:
    python scripts/run_vaps_ab_walkforward.py --pair GBPUSD --strategy session_range_mr
    python scripts/run_vaps_ab_walkforward.py --pair GBPUSD --windows 5 --output results.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.builtin_strategies import register_builtin_strategies  # noqa: E402, I001
from backtest.data_loader import CsvDataLoader  # noqa: E402
from backtest.engine import (  # noqa: E402
    BacktestConfig,
    Bar,
    get_spread_for_pair,
)
from backtest.strategies import ISignalStrategy  # noqa: E402
from backtest.vaps_engine import VAPSBacktestEngine  # noqa: E402
from backtest.walk_forward_runner import (  # noqa: E402
    STRATEGY_REGISTRY,
    run_strategy_walk_forward,
)
from common.resource_limits import add_resource_args, run_limited  # noqa: E402
from quant.vaps import VAPSConfig  # noqa: E402
from quant.walk_forward import (  # noqa: E402
    AggregatedMetrics,
    WalkForwardValidator,
    _compute_metrics,
    _mean,
    _std,
)


def _run_vaps_window(
    strategy_factory: Callable[[], ISignalStrategy],
    test_bars: List[Bar],
    all_bars_for_atr: List[Bar],
    pair: str,
    initial_balance: float,
    spread_pips: float,
    vaps_config: VAPSConfig,
) -> list[dict[str, Any]]:
    config = BacktestConfig(
        starting_balance=initial_balance,
        spread_pips=spread_pips,
        commission_per_lot=3.5,
        pair=pair,
    )
    strategy = strategy_factory()
    engine = VAPSBacktestEngine(config, [strategy], vaps_config=vaps_config)

    try:
        engine._all_bars = all_bars_for_atr
        engine._atr_history = []
        result = engine.run_all_strategies(test_bars)
        metrics_obj = result[strategy.name].metrics
        trades = [{"pnl": t.profit_loss} for t in metrics_obj.trades if hasattr(t, "profit_loss")]
        if not trades and metrics_obj.total_trades > 0:
            trades = [
                {"pnl": metrics_obj.total_pnl / metrics_obj.total_trades} for _ in range(metrics_obj.total_trades)
            ]
    except ValueError:
        trades = []

    return trades


def run_vaps_ab_walk_forward(
    strategy_factory: Callable[[], ISignalStrategy],
    bars: List[Bar],
    pair: str,
    n_windows: int = 5,
    train_ratio: float = 0.7,
    val_ratio: float = 0.15,
    overlap_ratio: float = 0.2,
    initial_balance: float = 10000,
    spread_pips: Optional[float] = None,
    vaps_config: Optional[VAPSConfig] = None,
) -> dict[str, Any]:
    cfg = vaps_config or VAPSConfig()
    effective_spread = spread_pips if spread_pips is not None else get_spread_for_pair(pair)

    fixed_results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=strategy_factory,
        pair=pair,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        overlap_ratio=overlap_ratio,
        initial_balance=initial_balance,
        spread_pips=effective_spread,
    )

    validator = WalkForwardValidator(
        data=bars,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        overlap_ratio=overlap_ratio,
    )

    vaps_per_window = []
    window_details = []

    for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):
        all_bars_for_atr = train_bars + val_bars + test_bars
        vaps_trades = _run_vaps_window(
            strategy_factory=strategy_factory,
            test_bars=test_bars,
            all_bars_for_atr=all_bars_for_atr,
            pair=pair,
            initial_balance=initial_balance,
            spread_pips=effective_spread,
            vaps_config=cfg,
        )
        vaps_metrics = _compute_metrics(
            idx, vaps_trades, initial_balance=initial_balance, bars_in_window=test_bars
        )
        vaps_per_window.append(vaps_metrics)

        fixed_m = fixed_results.per_window[idx]
        detail = {
            "window_id": idx,
            "test_start": str(test_bars[0].time) if test_bars else None,
            "test_end": str(test_bars[-1].time) if test_bars else None,
            "test_bars": len(test_bars),
            "fixed": {
                "trades": fixed_m.trade_count,
                "win_rate": round(fixed_m.win_rate * 100, 2),
                "profit_factor": round(fixed_m.profit_factor, 4),
                "sharpe": round(fixed_m.sharpe_ratio, 4),
                "max_dd": round(fixed_m.max_drawdown * 100, 2),
                "total_pnl": round(fixed_m.total_pnl, 2),
                "passed_go_nogo": fixed_m.passed_go_nogo,
            },
            "vaps": {
                "trades": vaps_metrics.trade_count,
                "win_rate": round(vaps_metrics.win_rate * 100, 2),
                "profit_factor": round(vaps_metrics.profit_factor, 4),
                "sharpe": round(vaps_metrics.sharpe_ratio, 4),
                "max_dd": round(vaps_metrics.max_drawdown * 100, 2),
                "total_pnl": round(vaps_metrics.total_pnl, 2),
                "passed_go_nogo": vaps_metrics.passed_go_nogo,
            },
        }

        if fixed_m.win_rate > 0:
            detail["wr_improvement"] = round((vaps_metrics.win_rate - fixed_m.win_rate) * 100, 2)
        else:
            detail["wr_improvement"] = 0.0

        if fixed_m.profit_factor > 0:
            detail["pf_improvement"] = round(vaps_metrics.profit_factor - fixed_m.profit_factor, 4)
        else:
            detail["pf_improvement"] = 0.0

        window_details.append(detail)

    fixed_agg = fixed_results.aggregated
    vaps_agg = None
    if vaps_per_window:
        wr_values = [m.win_rate for m in vaps_per_window]
        pf_values = [m.profit_factor for m in vaps_per_window]
        dd_values = [m.max_drawdown for m in vaps_per_window]
        sr_values = [m.sharpe_ratio for m in vaps_per_window]
        tc_values = [float(m.trade_count) for m in vaps_per_window]
        pnl_values = [m.total_pnl for m in vaps_per_window]

        vaps_agg = AggregatedMetrics(
            mean_win_rate=_mean(wr_values),
            std_win_rate=_std(wr_values, _mean(wr_values)),
            mean_profit_factor=_mean(pf_values),
            std_profit_factor=_std(pf_values, _mean(pf_values)),
            mean_max_drawdown=_mean(dd_values),
            std_max_drawdown=_std(dd_values, _mean(dd_values)),
            mean_sharpe_ratio=_mean(sr_values),
            std_sharpe_ratio=_std(sr_values, _mean(sr_values)),
            mean_trade_count=_mean(tc_values),
            std_trade_count=_std(tc_values, _mean(tc_values)),
            mean_total_pnl=_mean(pnl_values),
            std_total_pnl=_std(pnl_values, _mean(pnl_values)),
            windows_passed=sum(1 for m in vaps_per_window if m.passed_go_nogo),
            total_windows=len(vaps_per_window),
        )

    fixed_go = fixed_results.go_nogo
    vaps_windows_passed = sum(1 for m in vaps_per_window if m.passed_go_nogo)
    vaps_total = len(vaps_per_window)
    vaps_go = vaps_total >= 3 and vaps_windows_passed >= 2

    report = {
        "config": {
            "pair": pair,
            "n_windows": n_windows,
            "total_bars": len(bars),
            "vaps_lookback": cfg.lookback,
            "vaps_low_mult": cfg.low_multiplier,
            "vaps_normal_mult": cfg.normal_multiplier,
            "vaps_high_mult": cfg.high_multiplier,
            "vaps_extreme_mult": cfg.extreme_multiplier,
            "vaps_low_pctile": cfg.low_pctile,
            "vaps_normal_pctile": cfg.normal_pctile,
            "vaps_high_pctile": cfg.high_pctile,
            "spread_pips": effective_spread,
            "initial_balance": initial_balance,
        },
        "summary": {
            "fixed_go_nogo": fixed_go,
            "fixed_windows_passed": (fixed_agg.windows_passed if fixed_agg else 0),
            "vaps_go_nogo": vaps_go,
            "vaps_windows_passed": vaps_windows_passed,
            "vaps_outperforms_fixed": _vaps_outperforms(fixed_agg, vaps_agg),
        },
        "aggregated": {
            "fixed": (
                {
                    "mean_wr": round(fixed_agg.mean_win_rate * 100, 2),
                    "mean_pf": round(fixed_agg.mean_profit_factor, 4),
                    "mean_dd": round(fixed_agg.mean_max_drawdown * 100, 2),
                    "mean_sharpe": round(fixed_agg.mean_sharpe_ratio, 4),
                    "mean_pnl": round(fixed_agg.mean_total_pnl, 2),
                    "windows_passed": fixed_agg.windows_passed,
                }
                if fixed_agg
                else None
            ),
            "vaps": (
                {
                    "mean_wr": round(vaps_agg.mean_win_rate * 100, 2),
                    "mean_pf": round(vaps_agg.mean_profit_factor, 4),
                    "mean_dd": round(vaps_agg.mean_max_drawdown * 100, 2),
                    "mean_sharpe": round(vaps_agg.mean_sharpe_ratio, 4),
                    "mean_pnl": round(vaps_agg.mean_total_pnl, 2),
                    "windows_passed": vaps_agg.windows_passed,
                }
                if vaps_agg
                else None
            ),
        },
        "per_window": window_details,
    }

    return report


def _vaps_outperforms(
    fixed_agg: Optional[AggregatedMetrics],
    vaps_agg: Optional[AggregatedMetrics],
) -> bool:
    if fixed_agg is None or vaps_agg is None:
        return False
    wr_better = vaps_agg.mean_win_rate >= fixed_agg.mean_win_rate
    pf_better = vaps_agg.mean_profit_factor >= fixed_agg.mean_profit_factor
    pnl_better = vaps_agg.mean_total_pnl >= fixed_agg.mean_total_pnl
    dd_ok = vaps_agg.mean_max_drawdown <= fixed_agg.mean_max_drawdown * 1.05
    return wr_better and pf_better and pnl_better and dd_ok


def print_report(report: dict[str, Any]) -> None:
    cfg = report["config"]
    summary = report["summary"]
    agg = report["aggregated"]

    print(f"\n{'=' * 76}")
    print(f"  VAPS A/B WALK-FORWARD: {cfg['pair']} Session Range MR")
    print(f"{'=' * 76}")
    print(f"  Windows: {cfg['n_windows']} | Bars: {cfg['total_bars']} | Spread: {cfg['spread_pips']} pips")
    print(
        f"  VAPS: lookback={cfg['vaps_lookback']}, "
        f"low={cfg['vaps_low_mult']}x (<{cfg['vaps_low_pctile']}%), "
        f"normal={cfg['vaps_normal_mult']}x ({cfg['vaps_low_pctile']}-{cfg['vaps_normal_pctile']}%), "
        f"high={cfg['vaps_high_mult']}x ({cfg['vaps_normal_pctile']}-{cfg['vaps_high_pctile']}%), "
        f"extreme={cfg['vaps_extreme_mult']}x (>{cfg['vaps_high_pctile']}%)"
    )

    print(f"\n  {'Metric':<20} {'Fixed':>12} {'VAPS':>12} {'Delta':>12}")
    print(f"  {'-' * 56}")

    if agg["fixed"] and agg["vaps"]:
        f, v = agg["fixed"], agg["vaps"]
        rows = [
            ("Win Rate (%)", f["mean_wr"], v["mean_wr"], v["mean_wr"] - f["mean_wr"]),
            ("Profit Factor", f["mean_pf"], v["mean_pf"], v["mean_pf"] - f["mean_pf"]),
            ("Max DD (%)", f["mean_dd"], v["mean_dd"], v["mean_dd"] - f["mean_dd"]),
            (
                "Sharpe Ratio",
                f["mean_sharpe"],
                v["mean_sharpe"],
                v["mean_sharpe"] - f["mean_sharpe"],
            ),
            (
                "Total PnL ($)",
                f["mean_pnl"],
                v["mean_pnl"],
                v["mean_pnl"] - f["mean_pnl"],
            ),
        ]
        for label, fv, vv, delta in rows:
            sign = "+" if delta >= 0 else ""
            print(f"  {label:<20} {fv:>12.2f} {vv:>12.2f} {sign}{delta:>11.2f}")

    print("\n  GO/NO-GO Summary:")
    print(
        f"    Fixed: {'GO' if summary['fixed_go_nogo'] else 'NO-GO'} ({summary['fixed_windows_passed']} windows passed)"
    )
    print(
        f"    VAPS:  {'GO' if summary['vaps_go_nogo'] else 'NO-GO'} ({summary['vaps_windows_passed']} windows passed)"
    )
    print(f"    VAPS outperforms: {summary['vaps_outperforms_fixed']}")

    print("\n  Per-Window Comparison:")
    print(
        f"  {'#':>2} {'FixWR':>7} {'VapWR':>7} {'FixPF':>7} {'VapPF':>7} "
        f"{'FixDD':>7} {'VapDD':>7} {'FixPnL':>10} {'VapPnL':>10}"
    )
    for w in report["per_window"]:
        fx, vx = w["fixed"], w["vaps"]
        print(
            f"  {w['window_id']:>2} {fx['win_rate']:>6.1f}% {vx['win_rate']:>6.1f}% "
            f"{fx['profit_factor']:>7.2f} {vx['profit_factor']:>7.2f} "
            f"{fx['max_dd']:>6.2f}% {vx['max_dd']:>6.2f}% "
            f"${fx['total_pnl']:>9.0f} ${vx['total_pnl']:>9.0f}"
        )

    print(f"{'=' * 76}\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="VAPS vs Fixed Sizing A/B Walk-Forward Comparison")
    add_resource_args(p)
    p.add_argument("--pair", type=str, default="GBPUSD", help="Currency pair")
    p.add_argument("--strategy", type=str, default="session_range_mr", help="Strategy name")
    p.add_argument("--data", type=str, default=None, help="Path to CSV data file")
    p.add_argument("--windows", type=int, default=5, help="Number of windows")
    p.add_argument("--train-ratio", type=float, default=0.7, help="Train split ratio")
    p.add_argument("--balance", type=float, default=10000, help="Starting balance")
    p.add_argument("--spread", type=float, default=None, help="Spread in pips")
    p.add_argument("--vaps-lookback", type=int, default=50, help="VAPS ATR lookback")
    p.add_argument("--vaps-low", type=float, default=1.5, help="VAPS low vol multiplier")
    p.add_argument("--vaps-normal", type=float, default=1.0, help="VAPS normal vol multiplier")
    p.add_argument("--vaps-high", type=float, default=0.7, help="VAPS high vol multiplier")
    p.add_argument("--vaps-extreme", type=float, default=0.5, help="VAPS extreme vol multiplier")
    p.add_argument(
        "--vaps-low-pctile",
        type=float,
        default=30.0,
        help="VAPS low percentile threshold",
    )
    p.add_argument(
        "--vaps-normal-pctile",
        type=float,
        default=70.0,
        help="VAPS normal percentile threshold",
    )
    p.add_argument(
        "--vaps-high-pctile",
        type=float,
        default=90.0,
        help="VAPS high percentile threshold",
    )
    p.add_argument("--output", type=str, default=None, help="Path to save JSON report")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    register_builtin_strategies(pair=args.pair)

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

    if args.strategy not in STRATEGY_REGISTRY:
        available = ", ".join(sorted(STRATEGY_REGISTRY.keys()))
        print(
            f"Error: Unknown strategy '{args.strategy}'. Available: {available}",
            file=sys.stderr,
        )
        sys.exit(1)

    vaps_config = VAPSConfig(
        lookback=args.vaps_lookback,
        low_multiplier=args.vaps_low,
        normal_multiplier=args.vaps_normal,
        high_multiplier=args.vaps_high,
        extreme_multiplier=args.vaps_extreme,
        low_pctile=args.vaps_low_pctile,
        normal_pctile=args.vaps_normal_pctile,
        high_pctile=args.vaps_high_pctile,
    )

    print("\nRunning fixed-sizing baseline...")
    report = run_vaps_ab_walk_forward(
        strategy_factory=STRATEGY_REGISTRY[args.strategy],
        bars=bars,
        pair=args.pair,
        n_windows=args.windows,
        train_ratio=args.train_ratio,
        initial_balance=args.balance,
        spread_pips=args.spread,
        vaps_config=vaps_config,
    )

    print_report(report)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Report saved: {args.output}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
