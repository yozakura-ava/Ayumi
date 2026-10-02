#!/usr/bin/env python3
"""
5-Window Walk-Forward Evaluation Script for Supertrend + RSI Blend Strategy

Runs the SupertrendRSIBlendStrategy across 5 walk-forward windows on
EURUSD and GBPUSD H1 data, collecting per-window metrics and generating
an evaluation report.

Usage:
    python scripts/run_supertrend_rsi_walkforward.py
"""

import argparse
import json
import sys
from pathlib import Path

EURUSD_PATH = "data/forex/historical/EURUSD_H1.csv"
GBPUSD_PATH = "data/forex/historical/GBPUSD_H1.csv"
REPORT_DIR = Path("reports/walk_forward")

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader
from backtest.engine import BacktestConfig, BacktestMetrics
from backtest.enhanced_engine import EnhancedBacktestEngine
from backtest.strategies import SupertrendRSIBlendStrategy
from common.resource_limits import add_resource_args, run_limited
from quant.go_nogo_criteria import PerWindowCriteria

STRICT_PER_WINDOW = PerWindowCriteria(
    min_trades=5,
    win_rate=0.55,
    profit_factor=1.5,
    max_drawdown=0.05,
)


def _metrics_to_dict(m: BacktestMetrics) -> dict:
    return {
        "starting_balance": m.starting_balance,
        "ending_balance": m.ending_balance,
        "total_pnl": m.total_pnl,
        "total_pnl_pct": m.total_pnl_pct,
        "win_rate": m.win_rate,
        "total_trades": m.total_trades,
        "winning_trades": m.winning_trades,
        "losing_trades": m.losing_trades,
        "profit_factor": m.profit_factor,
        "max_drawdown_pct": m.max_drawdown_pct,
        "sharpe_ratio": m.sharpe_ratio,
        "avg_risk_reward": m.avg_risk_reward,
        "expectancy": m.expectancy,
        "rejected_signals": getattr(m, "rejected_signals", 0),
    }


def _aggregate_metrics(results: list) -> dict:
    total_windows = len(results)
    windows_passed = sum(1 for r in results if r.get("passed_go_nogo", False))
    test_trades = sum(r.get("test_metrics", {}).get("total_trades", 0) for r in results)
    avg_test_winrate = 0.0
    avg_test_pf = 0.0
    avg_test_dd = 0.0
    count = 0
    for r in results:
        tm = r.get("test_metrics", {})
        if tm.get("total_trades", 0) > 0:
            avg_test_winrate += tm.get("win_rate", 0)
            avg_test_pf += tm.get("profit_factor", 0)
            avg_test_dd += tm.get("max_drawdown_pct", 0)
            count += 1
    if count > 0:
        avg_test_winrate /= count
        avg_test_pf /= count
        avg_test_dd /= count
    return {
        "total_windows": total_windows,
        "windows_passed": windows_passed,
        "windows_passed_ratio": windows_passed / total_windows if total_windows > 0 else 0,
        "total_test_trades": test_trades,
        "avg_test_win_rate": avg_test_winrate,
        "avg_test_profit_factor": avg_test_pf,
        "avg_test_max_drawdown": avg_test_dd,
    }


def run_supertrend_walkforward(
    bars: list,
    pair: str = "EURUSD",
    n_windows: int = 5,
    train_ratio: float = 0.60,
    val_ratio: float = 0.15,
    test_ratio: float = 0.15,
    report_path: str | None = None,
) -> dict:
    strategy = SupertrendRSIBlendStrategy()
    config = BacktestConfig(
        starting_balance=10000.0,
        risk_per_trade_pct=0.005,
        max_daily_drawdown_pct=0.03,
        max_total_drawdown_pct=0.05,
        spread_pips=0.0,
        commission_per_lot=3.5,
        leverage=100,
        min_confidence=0.5,
        max_open_trades=3,
        min_bars_before_signal=30,
        round_trip_spread=True,
        slippage_pips=0.2,
        swap_per_lot_per_day=-2.0,
        pair=pair,
    )

    engine = EnhancedBacktestEngine(config=config, strategies=[strategy])

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
    print("   SUPERTREND + RSI BLEND — WALK-FORWARD BACKTEST")
    print("=" * 70)
    print(f"   Pair: {pair} | Bars: {total} | Windows: {n_windows}")
    print(f"   Split: train={train_ratio:.0%} val={val_ratio:.0%} test={test_ratio:.0%} buffer={buffer_ratio:.0%}")
    print("   Config: FTMO (0.5% risk, 3% daily DD, 5% total DD, max 3 trades)")
    print("   Strategy: Supertrend RSI Blend (H1)")

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

        train_metrics = engine.run_strategy(strategy, train_bars)
        val_metrics = engine.run_strategy(strategy, val_bars)
        test_metrics = engine.run_strategy(strategy, test_bars)

        pw_result = STRICT_PER_WINDOW.evaluate(
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
    go_nogo = agg["windows_passed"] >= 3

    print("\n" + "=" * 70)
    print("   SUMMARY")
    print("=" * 70)
    print(f"   Windows passed: {agg['windows_passed']}/{agg['total_windows']}")
    print(f"   Total test trades: {agg['total_test_trades']}")
    print(f"   Avg test WR: {agg['avg_test_win_rate']:.1f}%")
    print(f"   Avg test PF: {agg['avg_test_profit_factor']:.2f}")
    print(f"   Avg test DD: {agg['avg_test_max_drawdown']:.2f}%")
    print(f"\n   GO/NO-GO: {'GO' if go_nogo else 'NO-GO'} (need 3/5 windows passing)")

    report = {
        "config": {
            "pair": pair,
            "n_windows": n_windows,
            "train_ratio": train_ratio,
            "val_ratio": val_ratio,
            "test_ratio": test_ratio,
            "buffer_ratio": buffer_ratio,
            "total_bars": total,
            "strategy": "SupertrendRSIBlendStrategy",
        },
        "per_window": results,
        "aggregated": agg,
        "go_nogo": go_nogo,
    }

    if report_path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n   Report saved: {report_path}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Supertrend RSI 5-window walk-forward")
    add_resource_args(parser)
    _args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    loader = CsvDataLoader()

    pairs = [
        ("EURUSD", EURUSD_PATH),
        ("GBPUSD", GBPUSD_PATH),
    ]

    all_results = {}

    for pair, csv_path in pairs:
        print(f"\n{'=' * 70}")
        print(f"  Loading {pair} data from {csv_path}")
        print(f"{'=' * 70}")

        bars = loader.load(csv_path)
        print(f"  Loaded {len(bars)} bars: {bars[0].time} → {bars[-1].time}")

        report_path = str(REPORT_DIR / f"{pair}_supertrend_rsi_5window.json")
        result = run_supertrend_walkforward(
            bars=bars,
            pair=pair,
            n_windows=5,
            train_ratio=0.60,
            val_ratio=0.15,
            test_ratio=0.15,
            report_path=report_path,
        )
        all_results[pair] = result

    combined_path = str(REPORT_DIR / "combined_supertrend_rsi_5window.json")
    with open(combined_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Combined report saved: {combined_path}")

    print(f"\n{'=' * 70}")
    print("  GO/NO-GO SUMMARY")
    print(f"{'=' * 70}")

    for pair, result in all_results.items():
        agg = result.get("aggregated", {})
        print(f"\n  {pair}:")
        print(f"    Windows passed: {agg.get('windows_passed', 0)}/{agg.get('total_windows', 0)}")
        print(f"    GO/NO-GO: {'GO' if result.get('go_nogo') else 'NO-GO'}")

    print(f"\n  Reports saved to: {REPORT_DIR}/")
    print(f"  {'=' * 70}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
