#!/usr/bin/env python3
"""
5-Window Walk-Forward Evaluation Script

Runs the hybrid ICT/SMC + Quantitative Overlay strategy across
5 walk-forward windows on EURUSD and GBPUSD H1 data, collecting
per-window metrics and generating an evaluation report.

Usage:
    python scripts/run_5window_walkforward.py
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

from backtest import CsvDataLoader  # noqa: E402
from backtest.runner import run_hybrid_backtest  # noqa: E402, I001
from common.resource_limits import add_resource_args, run_limited  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="5-window walk-forward evaluation (ICT/SMC hybrid)")
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

        report_path = str(REPORT_DIR / f"{pair}_5window_walkforward.json")
        result = run_hybrid_backtest(
            bars=bars,
            pair=pair,
            n_windows=5,
            train_ratio=0.60,
            val_ratio=0.15,
            test_ratio=0.15,
            report_path=report_path,
        )
        all_results[pair] = result

    combined_path = str(REPORT_DIR / "combined_5window_evaluation.json")
    with open(combined_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Combined report saved: {combined_path}")

    print(f"\n{'=' * 70}")
    print("  FILTER REJECTION SUMMARY")
    print(f"{'=' * 70}")

    for pair, result in all_results.items():
        print(f"\n  {pair}:")
        per_window = result.get("per_window", [])
        total_evaluated = 0
        total_rejected = 0
        for w in per_window:
            train_m = w.get("train_metrics", {})
            val_m = w.get("val_metrics", {})
            test_m = w.get("test_metrics", {})
            total_evaluated += train_m.get("rejected", 0) + val_m.get("rejected", 0) + test_m.get("rejected", 0)
            total_rejected += train_m.get("rejected", 0) + val_m.get("rejected", 0) + test_m.get("rejected", 0)
        agg = result.get("aggregated", {})
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
