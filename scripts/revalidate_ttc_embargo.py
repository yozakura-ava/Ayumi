#!/usr/bin/env python3
"""Revalidation script: TTC XAUUSD with embargo_bars=96.

Runs walk-forward validation for TTC XAUUSD M15 strategy with and without
embargo to compare metrics and determine if the anomalous PF=13.38 was
inflated by autocorrelation leakage.

Usage:
    cd $AYUMI_ROOT
    PYTHONPATH=src/forex_bot python3 scripts/revalidate_ttc_embargo.py
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure forex_bot is on the path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from backtest.engine import Bar
from quant.walk_forward import WalkForwardResults, run_strategy


def load_m15_bars(csv_path: str, max_rows: int | None = None) -> list[Bar]:
    """Load M15 CSV data into Bar objects."""
    bars: list[Bar] = []
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if max_rows and i >= max_rows:
                break
            try:
                dt = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            bars.append(
                Bar(
                    time=dt,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                )
            )
    return bars


def print_results(label: str, results: WalkForwardResults) -> None:
    """Print walk-forward results summary."""
    print(f"\n{'=' * 70}")
    print(f"  {label}")
    print(f"{'=' * 70}")

    if results.aggregated:
        agg = results.aggregated
        print(f"  Mean Win Rate:      {agg.mean_win_rate:.4f} (±{agg.std_win_rate:.4f})")
        print(f"  Mean Profit Factor: {agg.mean_profit_factor:.4f} (±{agg.std_profit_factor:.4f})")
        print(f"  Mean Max Drawdown:  {agg.mean_max_drawdown:.4f} (±{agg.std_max_drawdown:.4f})")
        print(f"  Mean Sharpe Ratio:  {agg.mean_sharpe_ratio:.4f} (±{agg.std_sharpe_ratio:.4f})")
        print(f"  Mean Trade Count:   {agg.mean_trade_count:.1f} (±{agg.std_trade_count:.1f})")
        print(f"  Mean Total PnL:     ${agg.mean_total_pnl:.2f} (±${agg.std_total_pnl:.2f})")
        print(f"  Windows Passed:     {agg.windows_passed}/{agg.total_windows}")

    print(f"  GO/NO-GO:           {'GO' if results.go_nogo else 'NO-GO'}")

    if results.go_nogo_result:
        stat = results.go_nogo_result
        print(f"  Statistical Decision: {stat.decision.value}")
        if stat.p_value is not None:
            print(f"  P-value:            {stat.p_value:.4f}")
        print(f"  Total OOS Trades:   {stat.total_oos_trades}")
        print(f"  Min Trades Met:     {stat.min_trades_met}")
        print(f"  Significance Met:   {stat.significance_met}")

    print("\n  Per-Window Breakdown:")
    print(f"  {'Win':<5} {'WR':>8} {'PF':>10} {'MaxDD':>10} {'Sharpe':>10} {'Trades':>8} {'PnL':>12} {'GO?':>5}")
    print(f"  {'-' * 70}")
    for m in results.per_window:
        go = "YES" if m.passed_go_nogo else "NO"
        print(
            f"  {m.window_index:<5} {m.win_rate:>8.4f} {m.profit_factor:>10.4f} {m.max_drawdown:>10.4f} {m.sharpe_ratio:>10.4f} {m.trade_count:>8} ${m.total_pnl:>11.2f} {go:>5}"  # noqa: E501
        )


def main():
    data_path = str(PROJECT_ROOT / "data" / "forex" / "historical" / "XAUUSD_M15.csv")
    if not os.path.exists(data_path):
        print(f"ERROR: Data file not found: {data_path}")
        sys.exit(1)

    print(f"Loading M15 bars from {data_path}...")
    bars = load_m15_bars(data_path)
    print(f"Loaded {len(bars)} bars ({bars[0].time} to {bars[-1].time})")

    # Import strategy
    from strategies.ttc_xauusd import TTCXAUUSDStrategy

    # Run WITHOUT embargo (baseline - should match Jul 24 revalidation)
    print("\nRunning baseline (embargo_bars=0)...")
    strategy_base = TTCXAUUSDStrategy()
    results_base = run_strategy(
        strategy=strategy_base,
        bars=bars,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        embargo_bars=0,
    )
    print_results("BASELINE (no embargo)", results_base)

    # Run WITH embargo_bars=96 (24h M15)
    print("\nRunning with embargo_bars=96 (24h M15 autocorrelation buffer)...")
    strategy_emb = TTCXAUUSDStrategy()
    results_emb = run_strategy(
        strategy=strategy_emb,
        bars=bars,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        embargo_bars=96,
    )
    print_results("EMBARGO (96 bars = 24h)", results_emb)

    # Comparison summary
    print(f"\n{'=' * 70}")
    print("  COMPARISON SUMMARY")
    print(f"{'=' * 70}")

    if results_base.aggregated and results_emb.aggregated:
        a = results_base.aggregated
        e = results_emb.aggregated
        print(f"  {'Metric':<25} {'Baseline':>12} {'Embargo':>12} {'Delta':>12}")
        print(f"  {'-' * 65}")
        print(
            f"  {'Profit Factor':<25} {a.mean_profit_factor:>12.4f} {e.mean_profit_factor:>12.4f} {e.mean_profit_factor - a.mean_profit_factor:>+12.4f}"  # noqa: E501
        )
        print(
            f"  {'Win Rate':<25} {a.mean_win_rate:>12.4f} {e.mean_win_rate:>12.4f} {e.mean_win_rate - a.mean_win_rate:>+12.4f}"  # noqa: E501
        )
        print(
            f"  {'Sharpe Ratio':<25} {a.mean_sharpe_ratio:>12.4f} {e.mean_sharpe_ratio:>12.4f} {e.mean_sharpe_ratio - a.mean_sharpe_ratio:>+12.4f}"  # noqa: E501
        )
        print(
            f"  {'Max Drawdown':<25} {a.mean_max_drawdown:>12.4f} {e.mean_max_drawdown:>12.4f} {e.mean_max_drawdown - a.mean_max_drawdown:>+12.4f}"  # noqa: E501
        )
        print(
            f"  {'Trade Count':<25} {a.mean_trade_count:>12.1f} {e.mean_trade_count:>12.1f} {e.mean_trade_count - a.mean_trade_count:>+12.1f}"  # noqa: E501
        )
        print(f"  {'Windows Passed':<25} {a.windows_passed:>12} {e.windows_passed:>12}")
        print(
            f"  {'GO/NO-GO':<25} {'GO' if results_base.go_nogo else 'NO-GO':>12} {'GO' if results_emb.go_nogo else 'NO-GO':>12}"  # noqa: E501
        )

    # Decision per card's decision matrix
    print(f"\n{'=' * 70}")
    print("  DECISION MATRIX")
    print(f"{'=' * 70}")
    if results_emb.aggregated:
        pf = results_emb.aggregated.mean_profit_factor
        wp = results_emb.aggregated.windows_passed
        total = results_emb.aggregated.total_windows
        if pf > 3.0 and wp >= 3:
            decision = "GO — Promote to FTMO live (alongside Killzone Momentum)"
        elif pf >= 2.0 or wp >= 2:
            decision = "DEFER — Consider blend in next sprint"
        else:
            decision = "SHELVE — Edge was likely leakage, not signal"
        print(f"  Post-embargo PF:     {pf:.4f}")
        print(f"  Windows passed:     {wp}/{total}")
        print(f"  Decision:           {decision}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
