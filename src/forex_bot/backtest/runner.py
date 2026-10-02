#!/usr/bin/env python3
"""
Baseline Strategy Backtest Runner

Runs backtests for all 5 baseline strategies:
- MA Crossover
- Bollinger Band Mean Reversion
- RSI Divergence
- S/R Breakout
- Momentum ROC

Usage:
    python -m backtest.runner [data_file] [timeframe]

Examples:
    python -m backtest.runner                          # Use EURUSD_H1.csv default
    python -m backtest.runner /path/to/data.csv H1    # Custom data and timeframe
"""

import json
import math
import os
import sys
from pathlib import Path

# Grid strategy archived — lazy import to avoid collection errors
try:
    from strategies.grid.adapter import GridStrategyAdapter
except ImportError:
    GridStrategyAdapter = None  # type: ignore


from backtest import (
    AmalgamatedBacktestEngine,
    AmalgamationConfig,
    BacktestConfig,
    BacktestMetrics,
    BBStrategy,
    CommodityMeanReversionStrategy,
    CommodityTrendStrategy,
    ConfidenceMethod,
    CsvDataLoader,
    EnhancedBacktestEngine,
    GridStrategy,
    HybridStrategy,
    MACrossStrategy,
    MomentumBreakoutStrategy,
    MultiStrategyBacktestEngine,
    MultiStrategyConfig,
    ROCMStrategy,
    RSIStrategy,
    SRBreakoutStrategy,
    StatArbStrategy,
    TradeManagementConfig,
    VolatilitySqueezeStrategy,
    VotingMethod,
)
from backtest.engine import get_spread_for_pair

try:
    from backtest.grid_strategy import GridConfig
except ImportError:
    GridConfig = None  # type: ignore
from backtest.hybrid_strategy import HybridConfig
from quant.go_nogo_criteria import PerWindowCriteria

RUNNER_PER_WINDOW = PerWindowCriteria(
    min_trades=5,
    win_rate=0.55,
    profit_factor=1.5,
    max_drawdown=0.05,
)

DEFAULT_DATA_FILE = "$AYUMI_ROOT/data/forex/historical/EURUSD_H1.csv"


def run_individual_backtests(bars, config):
    strategies = [
        MACrossStrategy(fast_period=5, slow_period=13, atr_multiplier=2.0),
        BBStrategy(period=20, std_dev=2.0),
        RSIStrategy(period=14, oversold=35, overbought=65),
        SRBreakoutStrategy(lookback=50, confirmation_bars=1, breakout_threshold=0.0001),
        ROCMStrategy(period=12, roc_threshold=0.3),
        MomentumBreakoutStrategy(fast_period=9, slow_period=21, adx_threshold=25.0),
        VolatilitySqueezeStrategy(),
    ]

    engine = MultiStrategyBacktestEngine(config, strategies)
    results = engine.run_all_strategies(bars)

    print("\n" + "=" * 70)
    print("          INDIVIDUAL BASELINE STRATEGY BACKTEST RESULTS")
    print("=" * 70)

    for name, result in results.items():
        m = result.metrics
        print(f"\n### {name}")
        print(f"   Trades:        {m.total_trades}")
        print(f"   Win Rate:      {m.win_rate:.1f}%")
        print(f"   P&L:          ${m.total_pnl:.2f} ({m.total_pnl_pct:.2f}%)")
        print(f"   Profit Factor: {m.profit_factor:.2f}")
        print(f"   Sharpe:        {m.sharpe_ratio:.2f}")
        print(f"   Max DD:        {m.max_drawdown_pct:.2f}%")
        print(f"   Expectancy:   ${m.expectancy:.2f}")

    return results


def run_combined_backtest(bars, config):
    strategies = [MACrossStrategy(), BBStrategy(), ROCMStrategy()]

    multi_config = MultiStrategyConfig(min_combined_confidence=0.50)

    engine = MultiStrategyBacktestEngine(config, strategies, multi_config)
    individual, combined = engine.run_combined_strategies(strategies, bars)

    print("\n" + "=" * 70)
    print("          COMBINED STRATEGY BACKTEST RESULTS (MA + BB + ROC)")
    print("=" * 70)

    m = combined
    print("\n### Combined (MA Crossover + Bollinger Band + ROC Momentum)")
    print(f"   Trades:        {m.total_trades}")
    print(f"   Win Rate:      {m.win_rate:.1f}%")
    print(f"   P&L:          ${m.total_pnl:.2f} ({m.total_pnl_pct:.2f}%)")
    print(f"   Profit Factor: {m.profit_factor:.2f}")
    print(f"   Sharpe:        {m.sharpe_ratio:.2f}")
    print(f"   Max DD:        {m.max_drawdown_pct:.2f}%")
    print(f"   Expectancy:   ${m.expectancy:.2f}")

    return individual, combined


def analyze_walk_forward(bars, config, train_ratio=0.7):
    split_idx = int(len(bars) * train_ratio)
    train_bars = bars[:split_idx]
    test_bars = bars[split_idx:]

    print("\n" + "=" * 70)
    print("          WALK-FORWARD VALIDATION")
    print("=" * 70)
    print(f"\nTraining period: {train_bars[0].time} to {train_bars[-1].time}")
    print(f"Testing period:   {test_bars[0].time} to {test_bars[-1].time}")
    print(f"Train bars:      {len(train_bars)}")
    print(f"Test bars:       {len(test_bars)}")

    strategies = [
        MACrossStrategy(),
        BBStrategy(),
        RSIStrategy(),
        SRBreakoutStrategy(),
        ROCMStrategy(),
        MomentumBreakoutStrategy(),
        CommodityTrendStrategy(),
        CommodityMeanReversionStrategy(),
        GridStrategy(),
        StatArbStrategy(),
        VolatilitySqueezeStrategy(),
    ]

    engine = MultiStrategyBacktestEngine(config, strategies)

    print("\n### Training Period Results")
    train_results = engine.run_all_strategies(train_bars)
    for name, result in train_results.items():
        m = result.metrics
        print(
            f"   {name}: Trades={m.total_trades}, WinRate={m.win_rate:.1f}%, PF={m.profit_factor:.2f}, P&L=${m.total_pnl:.2f}"  # noqa: E501
        )

    print("\n### Testing Period Results")
    test_results = engine.run_all_strategies(test_bars)
    for name, result in test_results.items():
        m = result.metrics
        print(
            f"   {name}: Trades={m.total_trades}, WinRate={m.win_rate:.1f}%, PF={m.profit_factor:.2f}, P&L=${m.total_pnl:.2f}"  # noqa: E501
        )

    return train_results, test_results


def analyze_rolling_walk_forward(
    bars,
    config,
    strategies,
    n_windows: int = 3,
    train_ratio: float = 0.6,
    test_ratio: float = 0.2,
    run_fn=None,
):
    """Run walk-forward validation across N rolling windows.

    Each window splits a contiguous slice of bars into train and test
    segments.  The windows are anchored (non-overlapping train periods)
    so that every bar belongs to exactly one train or one test segment.

    Args:
        bars: Full bar list.
        config: BacktestConfig instance.
        strategies: List of ISignalStrategy instances.
        n_windows: Minimum number of rolling windows (default 3).
        train_ratio: Fraction of each window used for training (default 0.6).
        test_ratio: Fraction of each window used for testing (default 0.2).
            The remaining ``1 - train_ratio - test_ratio`` is a gap between
            train and test (purge period).
        run_fn: Callable ``(bars, config, strategies) -> dict``
            that returns ``{strategy_name: BacktestMetrics}``.  When ``None``,
            uses ``MultiStrategyBacktestEngine.run_all_strategies``.

    Returns:
        List of dicts, one per window, each containing:
            - ``window_id``: int
            - ``train_start``, ``train_end``, ``test_start``, ``test_end``: datetime
            - ``train_bars``, ``test_bars``: int
            - ``train_results``, ``test_results``: dict of strategy metrics
    """
    total = len(bars)
    window_size = total // n_windows
    results = []

    for w in range(n_windows):
        start = w * window_size
        end = (w + 1) * window_size if w < n_windows - 1 else total
        window_bars = bars[start:end]
        wlen = len(window_bars)

        if wlen < 100:
            results.append(
                {
                    "window_id": w,
                    "train_start": bars[start].time,
                    "train_end": bars[start].time,
                    "test_start": bars[start].time,
                    "test_end": bars[start].time,
                    "train_bars": 0,
                    "test_bars": 0,
                    "train_results": {},
                    "test_results": {},
                    "error": "Insufficient bars for window",
                }
            )
            continue

        train_end_idx = int(wlen * train_ratio)
        gap_end_idx = int(wlen * (train_ratio + test_ratio))

        train_bars = window_bars[:train_end_idx]
        test_bars = window_bars[gap_end_idx:]

        if run_fn is None:
            engine = MultiStrategyBacktestEngine(config, strategies)
            train_metrics = {name: r.metrics for name, r in engine.run_all_strategies(train_bars).items()}
            test_metrics = {name: r.metrics for name, r in engine.run_all_strategies(test_bars).items()}
        else:
            train_metrics = run_fn(train_bars, config, strategies)
            test_metrics = run_fn(test_bars, config, strategies)

        results.append(
            {
                "window_id": w,
                "train_start": train_bars[0].time,
                "train_end": train_bars[-1].time,
                "test_start": test_bars[0].time if test_bars else bars[start].time,
                "test_end": test_bars[-1].time if test_bars else bars[start].time,
                "train_bars": len(train_bars),
                "test_bars": len(test_bars),
                "train_results": train_metrics,
                "test_results": test_metrics,
            }
        )

    return results


def run_amalgamation_backtest(bars, config):
    strategies = [
        MACrossStrategy(fast_period=5, slow_period=13, atr_multiplier=2.0),
        BBStrategy(period=20, std_dev=2.0),
        RSIStrategy(period=14, oversold=35, overbought=65),
        SRBreakoutStrategy(lookback=50, confirmation_bars=1, breakout_threshold=0.0001),
        ROCMStrategy(period=12, roc_threshold=0.3),
        MomentumBreakoutStrategy(fast_period=9, slow_period=21, adx_threshold=25.0),
    ]

    configs = [
        (
            "Weighted+Confluence",
            AmalgamationConfig(
                voting_method=VotingMethod.WEIGHTED,
                confidence_method=ConfidenceMethod.CONFLUENCE,
                min_combined_confidence=0.45,
                min_confluence=2,
                confluence_bonus=0.10,
                session_filter_enabled=True,
            ),
        ),
        (
            "Weighted+Mean",
            AmalgamationConfig(
                voting_method=VotingMethod.WEIGHTED,
                confidence_method=ConfidenceMethod.MEAN,
                min_combined_confidence=0.45,
                min_confluence=2,
                session_filter_enabled=True,
            ),
        ),
        (
            "Confluence+Confluence",
            AmalgamationConfig(
                voting_method=VotingMethod.CONFLUENCE,
                confidence_method=ConfidenceMethod.CONFLUENCE,
                min_combined_confidence=0.45,
                min_confluence=3,
                confluence_bonus=0.15,
                session_filter_enabled=True,
            ),
        ),
        (
            "NoSessionFilter",
            AmalgamationConfig(
                voting_method=VotingMethod.WEIGHTED,
                confidence_method=ConfidenceMethod.CONFLUENCE,
                min_combined_confidence=0.40,
                min_confluence=2,
                confluence_bonus=0.10,
                session_filter_enabled=False,
            ),
        ),
    ]

    print("\n" + "=" * 70)
    print("       AMALGAMATION ENGINE BACKTEST RESULTS")
    print("=" * 70)

    for label, amal_config in configs:
        engine = AmalgamatedBacktestEngine(config, strategies, amal_config)
        metrics = engine.run(bars)
        print(f"\n### Config: {label}")
        print(f"   Trades:        {metrics.total_trades}")
        print(f"   Win Rate:      {metrics.win_rate:.1f}%")
        print(f"   P&L:          ${metrics.total_pnl:.2f} ({metrics.total_pnl_pct:.2f}%)")
        print(f"   Profit Factor: {metrics.profit_factor:.2f}")
        print(f"   Sharpe:        {metrics.sharpe_ratio:.2f}")
        print(f"   Max DD:        {metrics.max_drawdown_pct:.2f}%")
        print(f"   Expectancy:   ${metrics.expectancy:.2f}")
        print(f"   Rejected:      {metrics.rejected_signals}")

    return configs


def run_enhanced_ab_comparison(bars, config):
    strategies = [
        MACrossStrategy(fast_period=5, slow_period=13, atr_multiplier=2.0),
        BBStrategy(period=20, std_dev=2.0),
        RSIStrategy(period=14, oversold=35, overbought=65),
        SRBreakoutStrategy(lookback=50, confirmation_bars=1, breakout_threshold=0.0001),
        ROCMStrategy(period=12, roc_threshold=0.3),
        MomentumBreakoutStrategy(fast_period=9, slow_period=21, adx_threshold=25.0),
    ]

    tm_configs = [
        ("Conservative", TradeManagementConfig.conservative()),
        ("Aggressive", TradeManagementConfig.aggressive()),
        ("FTMO", TradeManagementConfig.ftmo()),
    ]

    print("\n" + "=" * 70)
    print("       A/B BACKTEST: BASELINE vs ENHANCED TRADE MANAGEMENT")
    print("=" * 70)

    baseline_engine = MultiStrategyBacktestEngine(config, strategies)
    baseline_results = baseline_engine.run_all_strategies(bars)

    for label, tm_config in tm_configs:
        print(f"\n{'─' * 70}")
        print(f"  Enhanced Config: {label}")
        print(f"{'─' * 70}")

        enhanced_engine = EnhancedBacktestEngine(config, strategies, tm_config)
        enhanced_results = enhanced_engine.run_all_strategies(bars)

        print(f"\n  {'Strategy':<30} {'Metric':<18} {'Baseline':>10} {'Enhanced':>10} {'Delta':>10}")
        print(f"  {'─' * 78}")

        for strategy in strategies:
            name = strategy.name
            if name not in baseline_results or name not in enhanced_results:
                continue

            bm = baseline_results[name].metrics
            em = enhanced_results[name].metrics

            comparisons = [
                (
                    "Trades",
                    f"{bm.total_trades}",
                    f"{em.total_trades}",
                    f"{em.total_trades - bm.total_trades:+d}",
                ),
                (
                    "Win Rate %",
                    f"{bm.win_rate:.1f}",
                    f"{em.win_rate:.1f}",
                    f"{em.win_rate - bm.win_rate:+.1f}",
                ),
                (
                    "Profit Factor",
                    f"{bm.profit_factor:.2f}",
                    f"{em.profit_factor:.2f}",
                    f"{em.profit_factor - bm.profit_factor:+.2f}",
                ),
                (
                    "P&L $",
                    f"{bm.total_pnl:.2f}",
                    f"{em.total_pnl:.2f}",
                    f"{em.total_pnl - bm.total_pnl:+.2f}",
                ),
                (
                    "Sharpe",
                    f"{bm.sharpe_ratio:.2f}",
                    f"{em.sharpe_ratio:.2f}",
                    f"{em.sharpe_ratio - bm.sharpe_ratio:+.2f}",
                ),
                (
                    "Max DD %",
                    f"{bm.max_drawdown_pct:.2f}",
                    f"{em.max_drawdown_pct:.2f}",
                    f"{em.max_drawdown_pct - bm.max_drawdown_pct:+.2f}",
                ),
                (
                    "Expectancy $",
                    f"{bm.expectancy:.2f}",
                    f"{em.expectancy:.2f}",
                    f"{em.expectancy - bm.expectancy:+.2f}",
                ),
                (
                    "Avg R:R",
                    f"{bm.avg_risk_reward:.2f}",
                    f"{em.avg_risk_reward:.2f}",
                    f"{em.avg_risk_reward - bm.avg_risk_reward:+.2f}",
                ),
            ]

            for metric_name, b_val, e_val, delta in comparisons:
                print(f"  {name:<30} {metric_name:<18} {b_val:>10} {e_val:>10} {delta:>10}")

    return tm_configs


def _metrics_to_dict(m) -> dict:
    return {
        "trades": m.total_trades,
        "win_rate": round(m.win_rate, 2),
        "profit_factor": round(m.profit_factor, 4),
        "sharpe": round(m.sharpe_ratio, 4),
        "max_dd": round(m.max_drawdown_pct, 2),
        "total_pnl": round(m.total_pnl, 2),
        "avg_rr": round(m.avg_risk_reward, 4),
        "expectancy": round(m.expectancy, 2),
        "rejected": m.rejected_signals,
    }


def _aggregate_metrics(window_results: list) -> dict:
    valid = [w for w in window_results if "error" not in w]
    if not valid:
        return {}

    def _mean(key: str) -> float:
        vals = [w["test_metrics"][key] for w in valid]
        return sum(vals) / len(vals) if vals else 0.0

    def _std(key: str) -> float:
        vals = [w["test_metrics"][key] for w in valid]
        if len(vals) < 2:
            return 0.0
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))

    keys = ["win_rate", "profit_factor", "sharpe", "max_dd", "total_pnl", "trades"]
    agg = {}
    for k in keys:
        agg[f"mean_{k}"] = round(_mean(k), 4)
        agg[f"std_{k}"] = round(_std(k), 4)

    passed = sum(1 for w in valid if w.get("passed_go_nogo", False))
    agg["windows_passed"] = passed
    agg["total_windows"] = len(valid)
    agg["go_nogo"] = len(valid) >= 3 and passed >= 2
    return agg


def _print_summary_table(window_results: list, agg: dict, pair: str) -> None:
    valid = [w for w in window_results if "error" not in w]

    print("\n" + "─" * 80)
    print(f"  PER-WINDOW RESULTS — {pair}")
    print(f"  {'Window':<8} {'Trades':>8} {'WR%':>8} {'PF':>8} {'MaxDD%':>8} {'Sharpe':>8} {'PnL':>10} {'GO?':>6}")
    print("  " + "─" * 76)

    for w in valid:
        tm = w["test_metrics"]
        go = "YES" if w.get("passed_go_nogo", False) else "NO"
        print(
            f"  {w['window_id']:<8} {tm['trades']:>8} {tm['win_rate']:>8.1f} "
            f"{tm['profit_factor']:>8.2f} {tm['max_dd']:>8.2f} "
            f"{tm['sharpe']:>8.2f} {tm['total_pnl']:>10.2f} {go:>6}"
        )

    for w in window_results:
        if "error" in w:
            print(f"  {w['window_id']:<8} {'ERROR: ' + w['error']}")

    if agg:
        print("\n" + "─" * 80)
        print("  AGGREGATE METRICS")
        print("  " + "─" * 76)
        go_str = "GO" if agg["go_nogo"] else "NO-GO"
        print(f"  Windows Passed: {agg['windows_passed']}/{agg['total_windows']}  →  {go_str}")
        print(f"  {'Metric':<20} {'Mean':>12} {'Std':>12}")
        print("  " + "─" * 44)
        labels = [
            ("Win Rate %", "win_rate"),
            ("Profit Factor", "profit_factor"),
            ("Sharpe Ratio", "sharpe"),
            ("Max Drawdown %", "max_dd"),
            ("Total PnL $", "total_pnl"),
            ("Trade Count", "trades"),
        ]
        for label, key in labels:
            print(f"  {label:<20} {agg[f'mean_{key}']:>12.4f} {agg[f'std_{key}']:>12.4f}")
    print("─" * 80)


def _run_window_backtest(
    bars: list,
    strategy: HybridStrategy,
    starting_balance: float = 10000.0,
    risk_per_trade_pct: float = 0.005,
    max_daily_drawdown_pct: float = 0.03,
    max_total_drawdown_pct: float = 0.05,
    spread_pips: float = 0.0,
    commission_per_lot: float = 3.5,
    leverage: int = 100,
    max_open_trades: int = 3,
    round_trip_spread: bool = True,
    slippage_pips: float = 0.2,
    swap_per_lot_per_day: float = -2.0,
    pair: str = "",
) -> BacktestMetrics:
    from backtest.ict_smc.models import ICTMarketState

    if len(bars) < 30:
        return BacktestMetrics(
            starting_balance=starting_balance,
            ending_balance=starting_balance,
            total_pnl=0,
            total_pnl_pct=0,
            win_rate=0,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            breakeven_trades=0,
            avg_win=0,
            avg_loss=0,
            largest_win=0,
            largest_loss=0,
            profit_factor=0,
            max_drawdown_pct=0,
            max_drawdown_dollar=0,
            max_daily_loss_dollar=0,
            sharpe_ratio=0,
            avg_risk_reward=0,
            expectancy=0,
            avg_holding_bars=0,
            equity_curve=[],
            trades=[],
            total_spread_cost=0,
            total_commission_cost=0,
            rejected_signals=0,
        )

    balance = starting_balance
    peak_balance = starting_balance
    max_dd = 0.0
    current_day = None
    daily_start = starting_balance
    daily_dd_halted = False
    open_trades: list = []
    trade_records: list = []
    equity_curve = [balance]
    total_spread_cost = 0.0
    total_commission_cost = 0.0

    effective_spread = spread_pips if spread_pips > 0 else get_spread_for_pair(pair)

    closes = [b.close for b in bars]
    high_series = [b.high for b in bars]
    low_series = [b.low for b in bars]
    atr_values = []
    for j in range(len(bars)):
        lookback = min(15, j + 1)
        if lookback < 2:
            atr_values.append(0.0001)
            continue
        tr_sum = 0.0
        count = 0
        for k in range(j - lookback + 1, j + 1):
            if k > 0:
                tr = max(
                    bars[k].high - bars[k].low,
                    abs(bars[k].high - bars[k - 1].close),
                    abs(bars[k].low - bars[k - 1].close),
                )
                tr_sum += tr
                count += 1
        atr_values.append(tr_sum / count if count > 0 else 0.0001)

    for i in range(30, len(bars)):
        bar = bars[i]
        day = bar.time.date()
        if current_day is not None and day != current_day:
            daily_loss_pct = (daily_start - balance) / daily_start if daily_start > 0 else 0
            if daily_loss_pct >= max_daily_drawdown_pct:
                daily_dd_halted = True
            else:
                daily_dd_halted = False
            current_day = day
            daily_start = balance
        elif current_day is None:
            current_day = day
            daily_start = balance

        if daily_dd_halted:
            equity_curve.append(balance)
            continue

        if balance > peak_balance:
            peak_balance = balance
        dd_pct = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
        if dd_pct > max_dd:
            max_dd = dd_pct
        if dd_pct >= max_total_drawdown_pct:
            break

        ict_state = ICTMarketState(bars=bars[: i + 1])
        signal = strategy.evaluate(
            ict_state,
            h4_bars=bars[: i + 1],
            atr_series=atr_values[: i + 1],
            high_series=high_series[: i + 1],
            low_series=low_series[: i + 1],
            close_series=closes[: i + 1],
            bar_time=bar.time,
        )

        if signal is None:
            equity_curve.append(balance)
            continue

        for trade in list(open_trades):
            hit_sl = False
            if trade["direction"] == "long" and bar.low <= trade["sl"]:
                hit_sl = True
            elif trade["direction"] == "short" and bar.high >= trade["sl"]:
                hit_sl = True

            hit_tp = False
            if trade["direction"] == "long" and bar.high >= trade["tp1"]:
                hit_tp = True
            elif trade["direction"] == "short" and bar.low <= trade["tp1"]:
                hit_tp = True

            if hit_sl:
                pip_val = 0.0001 if signal.entry_price < 50 else 0.01
                exit_price = trade["sl"]
                if round_trip_spread:
                    spread_exit = effective_spread * pip_val
                    if trade["direction"] == "long":
                        exit_price -= spread_exit
                    else:
                        exit_price += spread_exit
                slippage_exit = slippage_pips * pip_val
                if trade["direction"] == "long":
                    exit_price -= slippage_exit
                else:
                    exit_price += slippage_exit
                if trade["direction"] == "long":
                    pips = (exit_price - trade["entry"]) / pip_val
                else:
                    pips = (trade["entry"] - exit_price) / pip_val
                commission_cost = commission_per_lot * trade["lots"]
                total_commission_cost += commission_cost
                entry_spread_cost = effective_spread * pip_val * trade["lots"] * 100000
                exit_spread_cost = effective_spread * pip_val * trade["lots"] * 100000 if round_trip_spread else 0
                total_spread_cost += entry_spread_cost + exit_spread_cost
                pnl = pips * trade["lots"] * pip_val * 100000 - commission_cost
                balance = max(0.0, balance + pnl)
                if balance > peak_balance:
                    peak_balance = balance
                dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
                if dd > max_dd:
                    max_dd = dd
                trade_records.append(
                    {
                        "pnl": pnl,
                        "outcome": "loss" if pnl < -0.01 else "breakeven",
                    }
                )
                open_trades.remove(trade)
                equity_curve.append(balance)
            elif hit_tp:
                pip_val = 0.0001 if signal.entry_price < 50 else 0.01
                exit_price = trade["tp1"]
                if round_trip_spread:
                    spread_exit = effective_spread * pip_val
                    if trade["direction"] == "long":
                        exit_price -= spread_exit
                    else:
                        exit_price += spread_exit
                slippage_exit = slippage_pips * pip_val
                if trade["direction"] == "long":
                    exit_price -= slippage_exit
                else:
                    exit_price += slippage_exit
                if trade["direction"] == "long":
                    pips = (exit_price - trade["entry"]) / pip_val
                else:
                    pips = (trade["entry"] - exit_price) / pip_val
                commission_cost = commission_per_lot * trade["lots"]
                total_commission_cost += commission_cost
                entry_spread_cost = effective_spread * pip_val * trade["lots"] * 100000
                exit_spread_cost = effective_spread * pip_val * trade["lots"] * 100000 if round_trip_spread else 0
                total_spread_cost += entry_spread_cost + exit_spread_cost
                pnl = pips * trade["lots"] * pip_val * 100000 - commission_cost
                balance = max(0.0, balance + pnl)
                if balance > peak_balance:
                    peak_balance = balance
                dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
                if dd > max_dd:
                    max_dd = dd
                trade_records.append(
                    {
                        "pnl": pnl,
                        "outcome": "win" if pnl > 0.01 else "breakeven",
                    }
                )
                open_trades.remove(trade)
                equity_curve.append(balance)

        if len(open_trades) >= max_open_trades:
            equity_curve.append(balance)
            continue

        entry = signal.entry_price
        sl = signal.stop_loss
        tp1 = signal.take_profit_1 if signal.take_profit_1 else entry

        pip_val = 0.0001 if entry < 50 else 0.01
        spread_cost = effective_spread * pip_val
        if signal.direction.value == "long":
            entry += spread_cost
        else:
            entry -= spread_cost

        slippage_cost = slippage_pips * pip_val
        if signal.direction.value == "long":
            entry += slippage_cost
        else:
            entry -= slippage_cost

        risk_dist = abs(entry - sl)
        if risk_dist == 0:
            equity_curve.append(balance)
            continue

        risk_amount = balance * risk_per_trade_pct
        lots = risk_amount / (risk_dist * 100000)
        lots = min(lots, (balance * leverage) / (entry * 100000))
        if lots <= 0:
            equity_curve.append(balance)
            continue

        direction = "long" if signal.direction.value == "long" else "short"
        open_trades.append(
            {
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "lots": lots,
            }
        )
        equity_curve.append(balance)

    for trade in open_trades:
        pip_val = 0.0001 if trade["entry"] < 50 else 0.01
        pips = 0.0
        commission_cost = commission_per_lot * trade["lots"]
        total_commission_cost += commission_cost
        pnl = -commission_cost
        balance = max(0.0, balance + pnl)
        trade_records.append({"pnl": pnl, "outcome": "breakeven"})

    pnls = [t["pnl"] for t in trade_records]
    wins = [p for p in pnls if p > 0.01]
    losses = [p for p in pnls if p < -0.01]
    total_wins = sum(wins)
    total_losses = abs(sum(losses))

    win_rate = (len(wins) / len(pnls) * 100) if pnls else 0.0
    pf = total_wins / total_losses if total_losses > 0 else (999.0 if total_wins > 0 else 0.0)

    returns = []
    for j in range(1, len(equity_curve)):
        if equity_curve[j - 1] != 0:
            returns.append((equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1])
    if returns:
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
        sharpe = (mean_r / std_r * math.sqrt(6048)) if std_r > 0 else (999.0 if mean_r > 0 else 0.0)
    else:
        sharpe = 0.0

    return BacktestMetrics(
        starting_balance=starting_balance,
        ending_balance=balance,
        total_pnl=balance - starting_balance,
        total_pnl_pct=(balance - starting_balance) / starting_balance,
        win_rate=win_rate,
        total_trades=len(trade_records),
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=len(trade_records) - len(wins) - len(losses),
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        largest_win=max(wins) if wins else 0.0,
        largest_loss=min(losses) if losses else 0.0,
        profit_factor=pf,
        max_drawdown_pct=max_dd * 100,
        max_drawdown_dollar=max_dd * peak_balance,
        max_daily_loss_dollar=0.0,
        sharpe_ratio=sharpe,
        avg_risk_reward=abs(sum(wins) / len(wins) / abs(sum(losses) / len(losses)))
        if wins and losses and abs(sum(losses)) > 0.01
        else 0.0,
        expectancy=(win_rate / 100 * (sum(wins) / len(wins) if wins else 0))
        - ((1 - win_rate / 100) * abs(sum(losses) / len(losses) if losses else 0)),
        avg_holding_bars=0.0,
        equity_curve=equity_curve,
        trades=[],
        total_spread_cost=total_spread_cost,
        total_commission_cost=total_commission_cost,
        rejected_signals=strategy.metrics.total_evaluated - strategy.metrics.passed,
    )


def run_hybrid_backtest(
    bars: list,
    pair: str = "EURUSD",
    n_windows: int = 5,
    train_ratio: float = 0.60,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    report_path: str | None = None,
) -> dict:
    """Run hybrid ICT/SMC + quantitative filter strategy with FTMO-compliant config.

    Uses 5 walk-forward windows with 60/15/15/10 split
    (train/validation/test/buffer).  Per-window and aggregate metrics
    are computed, a summary table is printed, and a JSON report is saved.

    Args:
        bars: OHLC bar list from CsvDataLoader.
        pair: Currency pair label (default "EURUSD").
        n_windows: Number of rolling windows (default 5).
        train_ratio: Fraction of each window for training (default 0.60).
        val_ratio: Fraction of each window for validation (default 0.15).
        test_ratio: Fraction of each window for testing (default 0.15).
            Remaining (1 - train - val - test) is the purge buffer.
        report_path: If provided, JSON report is written here.

    Returns:
        Dict with "config", "per_window", "aggregated", "go_nogo" keys.
    """
    hybrid_config = HybridConfig(min_confidence=0.5)
    strategy = HybridStrategy(config=hybrid_config)

    total = len(bars)
    window_size = total // n_windows
    if window_size < 100:
        print(f"ERROR: Not enough bars ({total}) for {n_windows} windows")
        return {"per_window": [], "aggregated": {}, "go_nogo": False, "config": {}}

    buffer_ratio = 1.0 - train_ratio - val_ratio - test_ratio
    if buffer_ratio < 0:
        buffer_ratio = 0.0

    results = []
    print("\n" + "=" * 70)
    print("   HYBRID ICT/SMC + QUANT OVERLAY — WALK-FORWARD BACKTEST")
    print("=" * 70)
    print(f"   Pair: {pair} | Bars: {total} | Windows: {n_windows}")
    print(f"   Split: train={train_ratio:.0%} val={val_ratio:.0%} test={test_ratio:.0%} buffer={buffer_ratio:.0%}")
    print("   Config: FTMO (0.5% risk, 3% daily DD, 5% total DD, max 3 trades)")
    print("   Sessions: London (8-12), NY AM (12-16), NY PM (16-20) UTC")
    print("   SL: 2.0x ATR(14) | Min confidence: 0.5 | Min confluences: 2")

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
            results.append(
                {
                    "window_id": w,
                    "train_bars": len(train_bars),
                    "val_bars": len(val_bars),
                    "test_bars": len(test_bars),
                    "error": "Insufficient test bars",
                }
            )
            print(
                f"\n   Window {w}: SKIP — insufficient test bars "
                f"(train={len(train_bars)}, val={len(val_bars)}, test={len(test_bars)})"
            )
            continue

        strategy.reset_metrics()
        train_metrics = _run_window_backtest(train_bars, strategy, pair=pair)
        strategy.reset_metrics()
        val_metrics = _run_window_backtest(val_bars, strategy, pair=pair)
        strategy.reset_metrics()
        test_metrics = _run_window_backtest(test_bars, strategy, pair=pair)

        pw_result = RUNNER_PER_WINDOW.evaluate(
            trade_count=test_metrics.total_trades,
            win_rate=test_metrics.win_rate / 100.0,
            profit_factor=test_metrics.profit_factor,
            total_pnl=test_metrics.total_pnl,
            max_drawdown=test_metrics.max_drawdown_pct / 100.0,
        )
        passed = pw_result.passed

        results.append(
            {
                "window_id": w,
                "train_start": str(train_bars[0].time) if train_bars else None,
                "train_end": str(train_bars[-1].time) if train_bars else None,
                "val_start": str(val_bars[0].time) if val_bars else None,
                "val_end": str(val_bars[-1].time) if val_bars else None,
                "test_start": str(test_bars[0].time) if test_bars else None,
                "test_end": str(test_bars[-1].time) if test_bars else None,
                "train_bars": len(train_bars),
                "val_bars": len(val_bars),
                "test_bars": len(test_bars),
                "train_metrics": _metrics_to_dict(train_metrics),
                "val_metrics": _metrics_to_dict(val_metrics),
                "test_metrics": _metrics_to_dict(test_metrics),
                "passed_go_nogo": passed,
            }
        )

        status = "PASS" if passed else "FAIL"
        print(f"\n   Window {w}: {status}")
        print(
            f"     Train: {train_metrics.total_trades} trades, "
            f"WR={train_metrics.win_rate:.1f}%, PF={train_metrics.profit_factor:.2f}, "
            f"DD={train_metrics.max_drawdown_pct:.2f}%"
        )
        print(
            f"     Val:   {val_metrics.total_trades} trades, "
            f"WR={val_metrics.win_rate:.1f}%, PF={val_metrics.profit_factor:.2f}, "
            f"DD={val_metrics.max_drawdown_pct:.2f}%"
        )
        print(
            f"     Test:  {test_metrics.total_trades} trades, "
            f"WR={test_metrics.win_rate:.1f}%, PF={test_metrics.profit_factor:.2f}, "
            f"DD={test_metrics.max_drawdown_pct:.2f}%, Sharpe={test_metrics.sharpe_ratio:.2f}"
        )

    agg = _aggregate_metrics(results)

    _print_summary_table(results, agg, pair)

    report = {
        "config": {
            "pair": pair,
            "n_windows": n_windows,
            "train_ratio": train_ratio,
            "val_ratio": val_ratio,
            "test_ratio": test_ratio,
            "buffer_ratio": buffer_ratio,
            "total_bars": total,
            "risk_per_trade_pct": 0.005,
            "max_daily_drawdown_pct": 0.03,
            "max_total_drawdown_pct": 0.05,
            "max_open_trades": 3,
            "spread_pips": get_spread_for_pair(pair),
            "commission_per_lot": 3.5,
            "round_trip_spread": True,
            "slippage_pips": 0.2,
            "swap_per_lot_per_day": -2.0,
            "sl_atr_multiplier": 2.0,
            "min_confidence": 0.5,
            "min_confluences": 2,
            "sessions": ["london", "ny_am", "ny_pm"],
        },
        "per_window": results,
        "aggregated": agg,
        "go_nogo": agg.get("go_nogo", False),
    }

    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n   JSON report saved: {report_path}")

    return report


def run_grid_walk_forward(
    bars: list,
    pair: str = "EURUSD",
    n_windows: int = 5,
    train_ratio: float = 0.6,
    test_ratio: float = 0.2,
) -> dict:
    """Run walk-forward validation for the grid strategy.

    Uses rolling windows with train/test splits and FTMO-compliant risk
    parameters.  Per-window metrics are computed and an aggregate GO/NO-GO
    assessment is produced.

    Args:
        bars: OHLC bar list from CsvDataLoader.
        pair: Currency pair label (default "EURUSD").
        n_windows: Number of rolling windows (default 5).
        train_ratio: Fraction of each window for training (default 0.6).
        test_ratio: Fraction of each window for testing (default 0.2).
            Remaining fraction is the purge buffer.

    Returns:
        Dict with "per_window", "aggregated", "go_nogo" keys.
    """
    grid_cfg = GridConfig.ftmo(pair)

    total = len(bars)
    window_size = total // n_windows
    if window_size < 100:
        print(f"ERROR: Not enough bars ({total}) for {n_windows} windows")
        return {"per_window": [], "aggregated": {}, "go_nogo": False}

    buffer_ratio = 1.0 - train_ratio - test_ratio
    if buffer_ratio < 0:
        buffer_ratio = 0.0

    config = BacktestConfig(
        starting_balance=10000.0,
        risk_per_trade_pct=0.01,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.05,
        commission_per_lot=3.5,
        leverage=100,
        min_confidence=0.50,
        min_bars_before_signal=30,
        max_open_trades=10,
        pair=pair,
    )

    results = []
    print("\n" + "=" * 70)
    print("   GRID TRADING STRATEGY — WALK-FORWARD BACKTEST")
    print("=" * 70)
    print(f"   Pair: {pair} | Bars: {total} | Windows: {n_windows}")
    print(f"   Split: train={train_ratio:.0%} test={test_ratio:.0%} buffer={buffer_ratio:.0%}")
    print(f"   Grid spacing: {grid_cfg.spacing_in_pips():.1f} pips")
    print(f"   Levels per side: {grid_cfg.levels_per_side}")
    print(f"   Lot sizes: {grid_cfg.lot_sizes}")
    print(f"   FTMO risk: 5% equity stop, 3% daily loss, max {grid_cfg.risk.max_open_positions} positions")

    for w in range(n_windows):
        start = w * window_size
        end = (w + 1) * window_size if w < n_windows - 1 else total
        window_bars = bars[start:end]
        wlen = len(window_bars)

        train_end_idx = int(wlen * train_ratio)
        buffer_end_idx = int(wlen * (train_ratio + buffer_ratio))

        train_bars = window_bars[:train_end_idx]
        test_bars = window_bars[buffer_end_idx:]

        if len(test_bars) < 30:
            results.append(
                {
                    "window_id": w,
                    "train_bars": len(train_bars),
                    "test_bars": len(test_bars),
                    "error": "Insufficient test bars",
                }
            )
            print(f"\n   Window {w}: SKIP — insufficient test bars (train={len(train_bars)}, test={len(test_bars)})")
            continue

        adapter = GridStrategyAdapter(GridConfig.ftmo(pair))
        engine = MultiStrategyBacktestEngine(config, [adapter])

        train_results = engine.run_all_strategies(train_bars)
        test_results = engine.run_all_strategies(test_bars)

        train_m = train_results[adapter.name].metrics
        test_m = test_results[adapter.name].metrics

        pw_result = RUNNER_PER_WINDOW.evaluate(
            trade_count=test_m.total_trades,
            win_rate=test_m.win_rate / 100.0,
            profit_factor=test_m.profit_factor,
            total_pnl=test_m.total_pnl,
            max_drawdown=test_m.max_drawdown_pct / 100.0,
        )
        passed = pw_result.passed

        results.append(
            {
                "window_id": w,
                "train_start": str(train_bars[0].time) if train_bars else None,
                "train_end": str(train_bars[-1].time) if train_bars else None,
                "test_start": str(test_bars[0].time) if test_bars else None,
                "test_end": str(test_bars[-1].time) if test_bars else None,
                "train_bars": len(train_bars),
                "test_bars": len(test_bars),
                "train_metrics": _metrics_to_dict(train_m),
                "test_metrics": _metrics_to_dict(test_m),
                "passed_go_nogo": passed,
            }
        )

        status = "PASS" if passed else "FAIL"
        print(f"\n   Window {w}: {status}")
        print(
            f"     Train: {train_m.total_trades} trades, "
            f"WR={train_m.win_rate:.1f}%, PF={train_m.profit_factor:.2f}, "
            f"DD={train_m.max_drawdown_pct:.2f}%"
        )
        print(
            f"     Test:  {test_m.total_trades} trades, "
            f"WR={test_m.win_rate:.1f}%, PF={test_m.profit_factor:.2f}, "
            f"DD={test_m.max_drawdown_pct:.2f}%, Sharpe={test_m.sharpe_ratio:.2f}"
        )

    agg = _aggregate_metrics(results)
    _print_summary_table(results, agg, pair)

    return {
        "per_window": results,
        "aggregated": agg,
        "go_nogo": agg.get("go_nogo", False),
    }


def main():
    data_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATA_FILE
    sys.argv[2] if len(sys.argv) > 2 else "H1"

    if not os.path.exists(data_file):
        print(f"Error: Data file not found: {data_file}")
        sys.exit(1)

    print(f"\nLoading data from: {data_file}")
    loader = CsvDataLoader()
    bars = loader.load(data_file)
    print(f"Loaded {len(bars)} bars")
    print(f"Period: {bars[0].time} to {bars[-1].time}")

    inferred_tf = loader.infer_timeframe(bars)
    print(f"Inferred timeframe: H1 (approx {inferred_tf.minutes} min)")

    config = BacktestConfig(
        starting_balance=10000.0,
        risk_per_trade_pct=0.01,
        max_daily_drawdown_pct=0.02,
        max_total_drawdown_pct=0.05,
        commission_per_lot=3.5,
        leverage=100,
        min_confidence=0.45,
        min_bars_before_signal=30,
        max_open_trades=1,
    )

    run_individual_backtests(bars, config)

    run_combined_backtest(bars, config)

    run_amalgamation_backtest(bars, config)

    run_enhanced_ab_comparison(bars, config)

    analyze_walk_forward(bars, config)

    print("\n" + "=" * 70)
    print("                    BACKTEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
