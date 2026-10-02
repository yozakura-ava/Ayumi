"""Walk-forward test for SRMR+ strategy across EURUSD, USDJPY, XAUUSD (M15)."""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.walk_forward_runner import run_strategy_walk_forward
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "forex", "historical")

PAIRS = ["EURUSD", "USDJPY", "XAUUSD"]

SPREAD_PIPS = {
    "EURUSD": 1.5,
    "USDJPY": 1.5,
    "XAUUSD": 2.5,
}


def run_pair(pair: str) -> dict | None:
    csv_path = os.path.join(DATA_DIR, f"{pair}_M15.csv")
    if not os.path.exists(csv_path):
        print(f"  WARNING: {csv_path} not found, skipping {pair}")
        return None

    loader = CsvDataLoader()
    bars = loader.load(csv_path)
    print(f"  Loaded {len(bars)} bars ({bars[0].time} to {bars[-1].time})")

    config = SRMRPlusConfig()

    results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=lambda: SRMRPlusStrategy(config),
        pair=pair,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        overlap_ratio=0.2,
        initial_balance=10000,
        spread_pips=SPREAD_PIPS.get(pair, 1.5),
        commission_per_lot=3.5,
        min_confidence=0.30,
    )

    print(f"\n  WINDOW RESULTS — {pair}")
    for wm in results.per_window:
        status = "PASS" if wm.passed_go_nogo else "FAIL"
        print(
            f"    W{wm.window_index}: WR={wm.win_rate:.1%}  "
            f"PF={wm.profit_factor:.2f}  DD={wm.max_drawdown:.1%}  "
            f"Trades={wm.trade_count}  PnL=${wm.total_pnl:.2f}  [{status}]"
        )

    if results.aggregated:
        a = results.aggregated
        print(f"\n  AGGREGATED — {pair}")
        print(f"    Mean WR:     {a.mean_win_rate:.1%} (±{a.std_win_rate:.1%})")
        print(f"    Mean PF:     {a.mean_profit_factor:.2f} (±{a.std_profit_factor:.2f})")
        print(f"    Mean DD:     {a.mean_max_drawdown:.1%} (±{a.std_max_drawdown:.1%})")
        print(f"    Mean Trades: {a.mean_trade_count:.0f} (±{a.std_trade_count:.0f})")
        print(f"    Mean PnL:    ${a.mean_total_pnl:.2f}")
        print(f"    Windows Pass: {a.windows_passed}/{a.total_windows}")

    pair_report = {
        "pair": pair,
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
        pair_report["aggregated"] = {
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
    return pair_report


def main():
    print("=" * 60)
    print("SRMR+ Multi-Pair Walk-Forward Validation")
    print("Pairs: EURUSD, USDJPY, XAUUSD (M15)")
    print("=" * 60)

    all_results = {}
    for pair in PAIRS:
        print(f"\n{'=' * 60}")
        print(f"Testing: {pair} M15")
        print("=" * 60)
        report = run_pair(pair)
        if report:
            all_results[pair] = report

    print(f"\n{'=' * 60}")
    print("SUMMARY — Multi-Pair Validation (AYUAA-785)")
    print("=" * 60)

    pairs_passing = 0
    for pair, report in all_results.items():
        agg = report.get("aggregated", {})
        wr = agg.get("mean_win_rate", 0)
        pf = agg.get("mean_profit_factor", 0)
        wr_ok = wr >= 0.40
        pf_ok = pf >= 1.0
        passed = wr_ok and pf_ok
        if passed:
            pairs_passing += 1
        print(
            f"  {pair}: WR={wr:.1%} ({'OK' if wr_ok else 'FAIL'})  "
            f"PF={pf:.2f} ({'OK' if pf_ok else 'FAIL'})  "
            f"[{'PASS' if passed else 'FAIL'}]"
        )

    print(f"\n  Pairs passing (>=2/3): {pairs_passing}/3")
    print(f"  Overall: {'PASS' if pairs_passing >= 2 else 'FAIL'}")

    full_report = {
        "strategy": "SRMR+",
        "timeframe": "M15",
        "success_criteria": "WR >= 40%, PF >= 1.0 for at least 2/3 pairs",
        "pairs_passing": pairs_passing,
        "total_pairs": len(all_results),
        "overall_pass": pairs_passing >= 2,
        "results": all_results,
    }

    reports_dir = os.path.join(os.path.dirname(__file__), "..", "reports", "srmr_plus")
    os.makedirs(reports_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = os.path.join(reports_dir, f"srmr_plus_multi_pair_M15_{ts}.json")
    with open(report_path, "w") as f:
        json.dump(full_report, f, indent=2)
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    main()
