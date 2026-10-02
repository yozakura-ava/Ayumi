"""Walk-forward test for SRMR+ strategy on GBPUSD M15."""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.walk_forward_runner import run_strategy_walk_forward
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "forex", "historical", "GBPUSD_M15.csv")


def main():
    print("=" * 60)
    print("SRMR+ Walk-Forward Test — GBPUSD M15")
    print("=" * 60)

    loader = CsvDataLoader()
    bars = loader.load(DATA_PATH)
    print(f"\nLoaded {len(bars)} bars")
    print(f"Date range: {bars[0].time} to {bars[-1].time}")

    config = SRMRPlusConfig()

    print(f"\nConfig: ADX<{config.adx_max_threshold}, RSI L<{config.rsi_long_level}/H>{config.rsi_short_level}")
    print(f"Session range min: {config.session_range_min_pips} pips")
    print(f"Entry near extreme: {config.entry_near_extreme_pips} pips")
    print(f"TP1 RR: {config.tp1_rr}, TP2 RR: {config.tp2_rr}")

    results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=lambda: SRMRPlusStrategy(config),
        pair="GBPUSD",
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        overlap_ratio=0.2,
        initial_balance=10000,
        spread_pips=1.5,
        commission_per_lot=3.5,
        min_confidence=0.30,
    )

    print("\n" + "-" * 60)
    print("WINDOW RESULTS")
    print("-" * 60)

    for wm in results.per_window:
        status = "PASS" if wm.passed_go_nogo else "FAIL"
        print(
            f"  Window {wm.window_index}: WR={wm.win_rate:.1%}  "
            f"PF={wm.profit_factor:.2f}  DD={wm.max_drawdown:.1%}  "
            f"Sharpe={wm.sharpe_ratio:.2f}  Trades={wm.trade_count}  "
            f"PnL=${wm.total_pnl:.2f}  [{status}]"
        )

    print("\n" + "-" * 60)
    print("AGGREGATED")
    print("-" * 60)

    if results.aggregated:
        a = results.aggregated
        print(f"  Mean WR:       {a.mean_win_rate:.1%} (±{a.std_win_rate:.1%})")
        print(f"  Mean PF:       {a.mean_profit_factor:.2f} (±{a.std_profit_factor:.2f})")
        print(f"  Mean DD:       {a.mean_max_drawdown:.1%} (±{a.std_max_drawdown:.1%})")
        print(f"  Mean Sharpe:   {a.mean_sharpe_ratio:.2f} (±{a.std_sharpe_ratio:.2f})")
        print(f"  Mean Trades:   {a.mean_trade_count:.0f} (±{a.std_trade_count:.0f})")
        print(f"  Mean PnL:      ${a.mean_total_pnl:.2f}")
        print(f"  Windows Pass:  {a.windows_passed}/{a.total_windows}")

    print(f"\n  GO/NO-GO: {'GO' if results.go_nogo else 'NO-GO'}")

    print("\n" + "-" * 60)
    print("SUCCESS CRITERIA (from AYUAA-784)")
    print("-" * 60)
    if results.aggregated:
        wr_ok = results.aggregated.mean_win_rate >= 0.40
        pf_ok = results.aggregated.mean_profit_factor >= 1.0
        windows_ok = results.aggregated.windows_passed >= 3
        print(f"  Walk-forward WR >= 40%:  {'YES' if wr_ok else 'NO'} ({results.aggregated.mean_win_rate:.1%})")
        print(f"  Walk-forward PF >= 1.0:  {'YES' if pf_ok else 'NO'} ({results.aggregated.mean_profit_factor:.2f})")
        print(
            f"  >= 3/5 windows passing:  {'YES' if windows_ok else 'NO'} ({results.aggregated.windows_passed}/{results.aggregated.total_windows})"  # noqa: E501
        )
        all_ok = wr_ok and pf_ok and windows_ok
        print(f"\n  Overall: {'PASS' if all_ok else 'FAIL'}")

    report = {
        "strategy": "SRMR+",
        "pair": "GBPUSD",
        "timeframe": "M15",
        "go_nogo": results.go_nogo,
        "windows": [
            {
                "index": wm.window_index,
                "win_rate": wm.win_rate,
                "profit_factor": wm.profit_factor,
                "max_drawdown": wm.max_drawdown,
                "sharpe_ratio": wm.sharpe_ratio,
                "trade_count": wm.trade_count,
                "total_pnl": wm.total_pnl,
                "passed": wm.passed_go_nogo,
            }
            for wm in results.per_window
        ],
    }
    if results.aggregated:
        report["aggregated"] = {
            "mean_win_rate": results.aggregated.mean_win_rate,
            "std_win_rate": results.aggregated.std_win_rate,
            "mean_profit_factor": results.aggregated.mean_profit_factor,
            "std_profit_factor": results.aggregated.std_profit_factor,
            "mean_max_drawdown": results.aggregated.mean_max_drawdown,
            "mean_sharpe_ratio": results.aggregated.mean_sharpe_ratio,
            "mean_trade_count": results.aggregated.mean_trade_count,
            "mean_total_pnl": results.aggregated.mean_total_pnl,
            "windows_passed": results.aggregated.windows_passed,
            "total_windows": results.aggregated.total_windows,
        }

    reports_dir = os.path.join(os.path.dirname(__file__), "..", "reports", "srmr_plus")
    os.makedirs(reports_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = os.path.join(reports_dir, f"srmr_plus_M15_{ts}.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    main()
