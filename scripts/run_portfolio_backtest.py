#!/usr/bin/env python3
"""Multi-Strategy Portfolio Backtester — AYU-72

Runs all candidate strategies on the same data window, computes signal-level
correlation, selects least-correlated strategies with positive edge, builds
a portfolio backtest with FTMO compliance, and runs walk-forward validation.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from common.resource_limits import add_resource_args

if TYPE_CHECKING:
    # F821 fix (card 9cdbfd0a): names used in string annotations only; runtime
    # imports happen lazily inside functions (backtest.engine, backtest.portfolio_blend,
    # quant.walk_forward).
    from backtest.engine import BacktestConfig, BacktestMetrics
    from backtest.portfolio_blend import CorrelationResult, SelectionResult
    from quant.walk_forward import WalkForwardResults

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

DATA_DIR = str(project_root / "data" / "forex" / "historical")
REPORTS_DIR = project_root / "reports"


def build_strategy_factories() -> dict:
    from strategies.gbpusd_bb_reversion import BBMeanReversionStrategy
    from strategies.killzone_momentum import KillzoneMomentumStrategy
    from strategies.momentum import (
        DonchianBreakoutStrategy,
        MATrendFollowingStrategy,
    )
    from strategies.session_range_mean_reversion import (
        SessionRangeMeanReversionStrategy,
    )
    from strategies.srmr_plus import SRMRPlusStrategy
    from strategies.volatility_squeeze import VolatilitySqueezeStrategy

    return {
        "SRM": SessionRangeMeanReversionStrategy,
        "SRMR+": SRMRPlusStrategy,
        "Killzone Momentum": KillzoneMomentumStrategy,
        "Volatility Squeeze": VolatilitySqueezeStrategy,
        "Donchian Breakout": DonchianBreakoutStrategy,
        "MA Trend (EMA+ADX)": MATrendFollowingStrategy,
        "BB Mean Reversion": BBMeanReversionStrategy,
    }


def build_ftmo_config(pair: str, initial_balance: float) -> "BacktestConfig":
    from backtest.engine import BacktestConfig, get_spread_for_pair

    return BacktestConfig(
        starting_balance=initial_balance,
        risk_per_trade_pct=0.005,
        max_daily_drawdown_pct=0.03,
        max_total_drawdown_pct=0.05,
        max_open_trades=3,
        spread_pips=get_spread_for_pair(pair),
        commission_per_lot=3.5,
        slippage_pips=0.2,
        swap_per_lot_per_day=-2.0,
        round_trip_spread=True,
        pair=pair,
    )


def run_portfolio_on_bars(
    strategy_factories: dict,
    selected_keys: list[str],
    bars: list,
    pair: str,
    initial_balance: float,
):
    from backtest.multi_strategy_engine import MultiStrategyBacktestEngine

    config = build_ftmo_config(pair, initial_balance)
    selected_factories = {k: strategy_factories[k] for k in selected_keys}
    strategies = []
    for name, factory in selected_factories.items():  # noqa: B007
        try:
            s = factory()
            if hasattr(s, "set_balance"):
                s.set_balance(initial_balance)
            strategies.append(s)
        except Exception:  # noqa: S112
            continue

    if not strategies:
        from backtest.engine import BacktestMetrics

        return BacktestMetrics(
            starting_balance=initial_balance,
            ending_balance=initial_balance,
            total_pnl=0.0,
            total_pnl_pct=0.0,
            win_rate=0.0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            breakeven_trades=0,
            avg_win=0.0,
            avg_loss=0.0,
            largest_win=0.0,
            largest_loss=0.0,
            profit_factor=0.0,
            max_drawdown_pct=0.0,
            max_drawdown_dollar=0.0,
            max_daily_loss_dollar=0.0,
            sharpe_ratio=0.0,
            avg_risk_reward=0.0,
            expectancy=0.0,
            avg_holding_bars=0.0,
            equity_curve=[initial_balance],
            trades=[],
            total_spread_cost=0.0,
            total_commission_cost=0.0,
            rejected_signals=0,
        )

    engine = MultiStrategyBacktestEngine(config, strategies)
    _, combined_metrics = engine.run_combined_strategies(strategies, bars)
    return combined_metrics


def run_walk_forward(
    strategy_factories: dict,
    bars: list,
    pair: str,
    initial_balance: float,
    n_windows: int = 5,
):
    from backtest.portfolio_blend import (
        compute_signal_correlation,
        inventory_strategies_on_data,
        select_least_correlated,
    )
    from quant.walk_forward import (
        WalkForwardResults,
        WalkForwardValidator,
        WindowMetrics,
        _mean,
        _std,
    )

    validator = WalkForwardValidator(
        data=bars,
        n_windows=n_windows,
        train_ratio=0.60,
        val_ratio=0.15,
        overlap_ratio=0.10,
    )

    per_window: list[WindowMetrics] = []

    for win_idx, (train_bars, val_bars, test_bars) in enumerate(validator.split()):  # noqa: B007
        inventory = inventory_strategies_on_data(strategy_factories, train_bars, pair, initial_balance)

        if len(inventory) < 2:
            per_window.append(
                WindowMetrics(
                    window_index=win_idx,
                    win_rate=0.0,
                    profit_factor=0.0,
                    max_drawdown=0.0,
                    sharpe_ratio=0.0,
                    trade_count=0,
                    total_pnl=0.0,
                    passed_go_nogo=False,
                )
            )
            continue

        signal_corr = compute_signal_correlation(inventory, len(train_bars))
        selection = select_least_correlated(inventory, signal_corr, max_strategies=4, min_pf=0.0, min_wr=0.0)

        if not selection.selected:
            per_window.append(
                WindowMetrics(
                    window_index=win_idx,
                    win_rate=0.0,
                    profit_factor=0.0,
                    max_drawdown=0.0,
                    sharpe_ratio=0.0,
                    trade_count=0,
                    total_pnl=0.0,
                    passed_go_nogo=False,
                )
            )
            continue

        portfolio_metrics = run_portfolio_on_bars(
            strategy_factories,
            selection.selected,
            test_bars,
            pair,
            initial_balance,
        )

        trades = portfolio_metrics.trades
        pnls = [t.profit_loss for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_win = sum(wins)
        total_loss = abs(sum(losses))
        total_pnl = sum(pnls)
        trade_count = len(pnls)

        win_rate = len(wins) / trade_count if trade_count > 0 else 0.0
        pf = total_win / total_loss if total_loss > 0 else (10.0 if total_win > 0 else 0.0)

        balance = initial_balance
        peak = initial_balance
        max_dd = 0.0
        for p in pnls:
            balance = max(0.0, balance + p)
            if balance > peak:
                peak = balance
            if peak > 0:
                dd = (peak - balance) / peak
                if dd > max_dd:
                    max_dd = dd

        sharpe = portfolio_metrics.sharpe_ratio

        passed = trade_count >= 20 and win_rate > 0.55 and pf > 1.5 and total_pnl > 0 and max_dd < 0.05 and sharpe > 0.5

        per_window.append(
            WindowMetrics(
                window_index=win_idx,
                win_rate=win_rate,
                profit_factor=pf,
                max_drawdown=max_dd,
                sharpe_ratio=sharpe,
                trade_count=trade_count,
                total_pnl=total_pnl,
                passed_go_nogo=passed,
            )
        )

    if per_window:
        wr_vals = [m.win_rate for m in per_window]
        pf_vals = [m.profit_factor for m in per_window]
        dd_vals = [m.max_drawdown for m in per_window]
        sr_vals = [m.sharpe_ratio for m in per_window]
        tc_vals = [float(m.trade_count) for m in per_window]
        pnl_vals = [m.total_pnl for m in per_window]

        aggregated = WalkForwardValidator.__mro__[0].__new__(WalkForwardValidator) if False else None

        from quant.walk_forward import AggregatedMetrics

        aggregated = AggregatedMetrics(
            mean_win_rate=_mean(wr_vals),
            std_win_rate=_std(wr_vals, _mean(wr_vals)),
            mean_profit_factor=_mean(pf_vals),
            std_profit_factor=_std(pf_vals, _mean(pf_vals)),
            mean_max_drawdown=_mean(dd_vals),
            std_max_drawdown=_std(dd_vals, _mean(dd_vals)),
            mean_sharpe_ratio=_mean(sr_vals),
            std_sharpe_ratio=_std(sr_vals, _mean(sr_vals)),
            mean_trade_count=_mean(tc_vals),
            std_trade_count=_std(tc_vals, _mean(tc_vals)),
            mean_total_pnl=_mean(pnl_vals),
            std_total_pnl=_std(pnl_vals, _mean(pnl_vals)),
            windows_passed=sum(1 for m in per_window if m.passed_go_nogo),
            total_windows=len(per_window),
        )
    else:
        aggregated = None

    windows_passed = sum(1 for m in per_window if m.passed_go_nogo)
    total = len(per_window)
    go_nogo = total >= 3 and windows_passed >= 2

    return WalkForwardResults(
        per_window=per_window,
        aggregated=aggregated,
        go_nogo=go_nogo,
    )


def format_report(
    inventory: dict,
    correlation: "CorrelationResult",
    selection: "SelectionResult",
    portfolio_metrics: "BacktestMetrics",
    wf_results: "WalkForwardResults | None",
    pair: str,
) -> str:
    lines: list[str] = []
    lines.append("=" * 90)
    lines.append(f"MULTI-STRATEGY PORTFOLIO BACKTEST — {pair} H1")
    lines.append("=" * 90)
    lines.append("")

    lines.append("STRATEGY INVENTORY (Individual Results)")
    lines.append("-" * 90)
    lines.append(
        f"{'Strategy':<25} {'WR%':>6} {'PF':>7} {'Sharpe':>7} {'DD%':>7} {'Trades':>7} {'PnL':>10} {'Selected':>10}"
    )
    for key, inv in inventory.items():
        m = inv.metrics
        sel = "YES" if key in selection.selected else ""
        lines.append(
            f"{key:<25} {m.win_rate:>6.1f} {m.profit_factor:>7.2f} "
            f"{m.sharpe_ratio:>7.2f} {m.max_drawdown_pct:>7.2f} {m.total_trades:>7} "
            f"${m.total_pnl:>9.2f} {sel:>10}"
        )
    lines.append("")

    lines.append("SIGNAL CORRELATION MATRIX")
    lines.append("-" * 90)
    keys = list(correlation.matrix.keys())
    header = f"{'':<25}" + "".join(f"{k[:12]:>13}" for k in keys)
    lines.append(header)
    for key_a in keys:
        row = f"{key_a[:25]:<25}"
        for key_b in keys:
            val = correlation.matrix[key_a][key_b]
            marker = "*" if abs(val) > 0.5 else " "
            row += f"{val:>12.3f}{marker}"
        lines.append(row)
    lines.append("\n  * = |correlation| > 0.5 (high)")
    lines.append(f"  Average |correlation|: {correlation.average_correlation:.3f}")
    lines.append("")

    if selection.skipped:
        lines.append("SKIPPED (high correlation with selected)")
        lines.append("-" * 90)
        for candidate, reason in selection.skipped:
            lines.append(f"  {candidate:<25} (corr with {reason} > 0.5)")
        lines.append("")

    lines.append(f"SELECTED STRATEGIES ({len(selection.selected)})")
    lines.append("-" * 90)
    for key in selection.selected:
        w = selection.weights.get(key, 0.0)
        lines.append(f"  {key:<25} weight={w:.2%}")
    lines.append("")

    lines.append("PORTFOLIO COMBINED METRICS")
    lines.append("-" * 90)
    m = portfolio_metrics
    lines.append(f"  Starting Balance:  ${m.starting_balance:>10.2f}")
    lines.append(f"  Ending Balance:    ${m.ending_balance:>10.2f}")
    lines.append(f"  Total P&L:         ${m.total_pnl:>10.2f} ({m.total_pnl_pct:>7.2f}%)")
    lines.append(f"  Win Rate:          {m.win_rate:>10.1f}%")
    lines.append(f"  Profit Factor:     {m.profit_factor:>10.2f}")
    lines.append(f"  Sharpe Ratio:      {m.sharpe_ratio:>10.2f}")
    lines.append(f"  Max Drawdown:      {m.max_drawdown_pct:>10.2f}%")
    lines.append(f"  Total Trades:      {m.total_trades:>10}")
    lines.append("")

    lines.append("COMPARISON: INDIVIDUAL vs PORTFOLIO")
    lines.append("-" * 90)
    lines.append(f"{'Strategy':<25} {'WR%':>6} {'PF':>7} {'Sharpe':>7} {'DD%':>7} {'Trades':>7}")
    for key, inv in inventory.items():
        im = inv.metrics
        lines.append(
            f"{key:<25} {im.win_rate:>6.1f} {im.profit_factor:>7.2f} "
            f"{im.sharpe_ratio:>7.2f} {im.max_drawdown_pct:>7.2f} {im.total_trades:>7}"
        )
    lines.append(
        f"{'PORTFOLIO':<25} {m.win_rate:>6.1f} {m.profit_factor:>7.2f} "
        f"{m.sharpe_ratio:>7.2f} {m.max_drawdown_pct:>7.2f} {m.total_trades:>7}"
    )
    lines.append("")

    if wf_results:
        wf = wf_results
        lines.append("WALK-FORWARD VALIDATION")
        lines.append("-" * 90)
        lines.append(f"  GO/NO-GO: {'GO' if wf.go_nogo else 'NO-GO'}")
        if wf.aggregated:
            a = wf.aggregated
            lines.append(f"  Mean Win Rate:      {a.mean_win_rate:>10.2%}")
            lines.append(f"  Mean Profit Factor: {a.mean_profit_factor:>10.2f}")
            lines.append(f"  Mean Sharpe:        {a.mean_sharpe_ratio:>10.2f}")
            lines.append(f"  Mean Max DD:        {a.mean_max_drawdown:>10.2%}")
            lines.append(f"  Mean Trade Count:   {a.mean_trade_count:>10.0f}")
            lines.append(f"  Windows Passed:     {a.windows_passed}/{a.total_windows}")

        lines.append("")
        lines.append(f"  {'Win':<8} {'PF':>8} {'MaxDD':>8} {'Sharpe':>8} {'Trades':>8} {'PnL':>12} {'GO?':>6}")
        for wm in wf.per_window:
            lines.append(
                f"  {wm.win_rate:<8.2%} {wm.profit_factor:>8.2f} {wm.max_drawdown:>8.2%} "
                f"{wm.sharpe_ratio:>8.2f} {wm.trade_count:>8} "
                f"${wm.total_pnl:>10.2f} {'YES' if wm.passed_go_nogo else 'NO':>6}"
            )
        lines.append("")

    best_individual_sharpe = max((inv.metrics.sharpe_ratio for inv in inventory.values()), default=0.0)
    best_individual_dd = min(
        (inv.metrics.max_drawdown_pct for inv in inventory.values() if inv.metrics.max_drawdown_pct > 0),
        default=100.0,
    )
    lines.append("SUCCESS CRITERIA CHECK")
    lines.append("-" * 90)
    lines.append(
        f"  Portfolio Sharpe > best individual: "
        f"{m.sharpe_ratio:.2f} > {best_individual_sharpe:.2f} → "
        f"{'PASS' if m.sharpe_ratio > best_individual_sharpe else 'FAIL'}"
    )
    lines.append(
        f"  Portfolio Max DD < best individual: "
        f"{m.max_drawdown_pct:.2f}% < {best_individual_dd:.2f}% → "
        f"{'PASS' if m.max_drawdown_pct < best_individual_dd else 'FAIL'}"
    )
    if wf_results:
        min_oos_trades = min((wm.trade_count for wm in wf_results.per_window), default=0)
        lines.append(f"  Min OOS trades >= 20: {min_oos_trades} → {'PASS' if min_oos_trades >= 20 else 'FAIL'}")
        lines.append(f"  Walk-forward GO/NO-GO: {'PASS' if wf_results.go_nogo else 'FAIL'}")
    lines.append("")
    lines.append("=" * 90)

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-strategy portfolio backtest")
    add_resource_args(parser)
    parser.add_argument("--pair", type=str, default="EURUSD", help="Pair to test (default: EURUSD)")
    parser.add_argument("--timeframe", type=str, default="H1", help="Timeframe (default: H1)")
    parser.add_argument("--balance", type=float, default=10000, help="Starting balance")
    parser.add_argument("--windows", type=int, default=5, help="Walk-forward windows")
    parser.add_argument("--output", type=str, default=None, help="Output JSON path")
    parser.add_argument(
        "--no-walk-forward",
        action="store_true",
        help="Skip walk-forward validation",
    )
    args = parser.parse_args()

    from backtest.data_loader import CsvDataLoader
    from backtest.portfolio_blend import (
        compute_signal_correlation,
        inventory_strategies_on_data,
        select_least_correlated,
    )

    data_path = f"{DATA_DIR}/{args.pair}_{args.timeframe}.csv"
    print(f"Loading {args.pair} {args.timeframe} from {data_path}")

    loader = CsvDataLoader()
    try:
        bars = loader.load(data_path)
    except FileNotFoundError:
        print(f"ERROR: Data file not found: {data_path}")
        sys.exit(1)

    print(f"Loaded {len(bars)} bars\n")

    strategy_factories = build_strategy_factories()
    print(f"Testing {len(strategy_factories)} strategies:")
    for name in strategy_factories:
        print(f"  - {name}")
    print()

    t0 = time.time()

    print("Step 1: Running individual strategy inventory...")
    inventory = inventory_strategies_on_data(strategy_factories, bars, args.pair, args.balance)
    print(f"  {len(inventory)} strategies produced signals\n")

    print("Step 2: Computing signal-level correlation...")
    signal_corr = compute_signal_correlation(inventory, len(bars))
    print(f"  Average |correlation|: {signal_corr.average_correlation:.3f}\n")

    print("Step 3: Selecting least-correlated strategies...")
    selection = select_least_correlated(inventory, signal_corr, max_strategies=4, min_pf=0.0, min_wr=0.0)
    print(f"  Selected {len(selection.selected)} strategies:")
    for key in selection.selected:
        w = selection.weights.get(key, 0.0)
        print(f"    - {key} (weight={w:.2%})")
    if selection.skipped:
        print(f"  Skipped {len(selection.skipped)} due to high correlation")
    print()

    print("Step 4: Running portfolio backtest...")
    portfolio_metrics = run_portfolio_on_bars(
        strategy_factories,
        selection.selected,
        bars,
        args.pair,
        args.balance,
    )
    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.1f}s\n")

    wf_results = None
    if not args.no_walk_forward:
        print("Step 5: Running walk-forward validation...")
        wf_results = run_walk_forward(strategy_factories, bars, args.pair, args.balance, args.windows)
        print(f"  GO/NO-GO: {'GO' if wf_results.go_nogo else 'NO-GO'}")
        if wf_results.aggregated:
            a = wf_results.aggregated
            print(f"  Mean WR={a.mean_win_rate:.2%}, PF={a.mean_profit_factor:.2f}, Sharpe={a.mean_sharpe_ratio:.2f}")
            print(f"  Windows: {a.windows_passed}/{a.total_windows} passed")
        print()

    report = format_report(inventory, signal_corr, selection, portfolio_metrics, wf_results, args.pair)
    print(report)

    output_path = args.output or str(REPORTS_DIR / f"portfolio_backtest_{args.pair}_{args.timeframe}.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    report_data = {
        "pair": args.pair,
        "timeframe": args.timeframe,
        "initial_balance": args.balance,
        "selected_strategies": selection.selected,
        "weights": selection.weights,
        "skipped": selection.skipped,
        "signal_correlation_average": signal_corr.average_correlation,
        "individual": {
            key: {
                "win_rate": inv.metrics.win_rate,
                "profit_factor": inv.metrics.profit_factor,
                "sharpe_ratio": inv.metrics.sharpe_ratio,
                "max_drawdown_pct": inv.metrics.max_drawdown_pct,
                "trade_count": inv.metrics.total_trades,
                "total_pnl": inv.metrics.total_pnl,
                "signal_count": len(inv.signals),
            }
            for key, inv in inventory.items()
        },
        "portfolio": {
            "win_rate": portfolio_metrics.win_rate,
            "profit_factor": portfolio_metrics.profit_factor,
            "sharpe_ratio": portfolio_metrics.sharpe_ratio,
            "max_drawdown_pct": portfolio_metrics.max_drawdown_pct,
            "total_trades": portfolio_metrics.total_trades,
            "total_pnl": portfolio_metrics.total_pnl,
            "total_pnl_pct": portfolio_metrics.total_pnl_pct,
            "ending_balance": portfolio_metrics.ending_balance,
        },
        "walk_forward": None,
    }

    if wf_results:
        report_data["walk_forward"] = {
            "go_nogo": wf_results.go_nogo,
            "per_window": [
                {
                    "window_index": wm.window_index,
                    "win_rate": wm.win_rate,
                    "profit_factor": wm.profit_factor,
                    "max_drawdown": wm.max_drawdown,
                    "sharpe_ratio": wm.sharpe_ratio,
                    "trade_count": wm.trade_count,
                    "total_pnl": wm.total_pnl,
                    "passed_go_nogo": wm.passed_go_nogo,
                }
                for wm in wf_results.per_window
            ],
            "aggregated": (
                {
                    "mean_win_rate": wf_results.aggregated.mean_win_rate,
                    "std_win_rate": wf_results.aggregated.std_win_rate,
                    "mean_profit_factor": wf_results.aggregated.mean_profit_factor,
                    "mean_max_drawdown": wf_results.aggregated.mean_max_drawdown,
                    "mean_sharpe_ratio": wf_results.aggregated.mean_sharpe_ratio,
                    "mean_trade_count": wf_results.aggregated.mean_trade_count,
                    "windows_passed": wf_results.aggregated.windows_passed,
                    "total_windows": wf_results.aggregated.total_windows,
                }
                if wf_results.aggregated
                else None
            ),
        }

    with open(output_path, "w") as f:
        json.dump(report_data, f, indent=2)
    print(f"Report saved: {output_path}")


if __name__ == "__main__":
    main()
