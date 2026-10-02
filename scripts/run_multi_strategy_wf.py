#!/usr/bin/env python3
"""
Multi-Strategy Walk-Forward Evaluation — Sequential + CPU-Capped

Runs WF validation on all blend strategies across available symbols.
One at a time, CPU-limited to 20%, memory-capped at 2GB.

Usage:
    python scripts/run_multi_strategy_wf.py
    python scripts/run_multi_strategy_wf.py --dry-run
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader
from backtest.builtin_strategies import register_builtin_strategies
from backtest.engine import get_spread_for_pair
from backtest.walk_forward_runner import (
    run_named_strategy_walk_forward,
)
from common.resource_limits import cpu_limited, memory_capped

# Register all builtin strategies before any WF runs
register_builtin_strategies()

# Strategy × Symbol matrix to evaluate
# Maps blend strategy names to WF runner strategy names
EVALUATION_MATRIX = [
    # SRMR+ — already validated for XAUUSD, re-validate others
    {
        "strategy": "session_range_mr",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "session_range_mr",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {
        "strategy": "session_range_mr",
        "pair": "USDJPY",
        "data": "data/forex/historical/USDJPY_H1.csv" if Path("data/forex/historical/USDJPY_H1.csv").exists() else None,
    },
    {
        "strategy": "session_range_mr",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # BB+RSI Mean Reversion → bollinger
    {
        "strategy": "bollinger",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "bollinger",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "bollinger", "pair": "USDJPY", "data": None},
    {
        "strategy": "bollinger",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # Killzone Momentum → momentum
    {
        "strategy": "momentum",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "momentum",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "momentum", "pair": "USDJPY", "data": None},
    {
        "strategy": "momentum",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # Donchian Channel Breakout → sr_breakout
    {
        "strategy": "sr_breakout",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "sr_breakout",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "sr_breakout", "pair": "USDJPY", "data": None},
    {
        "strategy": "sr_breakout",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # Simple RSI Threshold → rsi
    {
        "strategy": "rsi",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "rsi",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "rsi", "pair": "USDJPY", "data": None},
    {
        "strategy": "rsi",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # Volatility Squeeze → volatility_squeeze
    {
        "strategy": "volatility_squeeze",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "volatility_squeeze",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "volatility_squeeze", "pair": "USDJPY", "data": None},
    {
        "strategy": "volatility_squeeze",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # Keltner → keltner (proxy for Session Breakout)
    {
        "strategy": "keltner",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "keltner",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "keltner", "pair": "USDJPY", "data": None},
    {
        "strategy": "keltner",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # MA Crossover → ma_crossover (proxy for trend-following)
    {
        "strategy": "ma_crossover",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "ma_crossover",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "ma_crossover", "pair": "USDJPY", "data": None},
    {
        "strategy": "ma_crossover",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # High Conviction → high_conviction
    {
        "strategy": "high_conviction",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "high_conviction",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {"strategy": "high_conviction", "pair": "USDJPY", "data": None},
    {
        "strategy": "high_conviction",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
    # Grid → grid
    {
        "strategy": "grid",
        "pair": "GBPUSD",
        "data": "data/forex/historical/GBPUSD_H1.csv",
    },
    {
        "strategy": "grid",
        "pair": "EURUSD",
        "data": "data/forex/historical/EURUSD_H1.csv",
    },
    {
        "strategy": "grid",
        "pair": "XAUUSD",
        "data": "data/forex/historical/XAUUSD_H1.csv",
    },
]

REPORT_DIR = Path("reports/multi-strategy-wf-2026-07-08")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def run_single_wf(strategy: str, pair: str, data_path: str, windows: int = 5) -> dict:
    """Run a single WF evaluation. Returns result dict."""
    result = {
        "strategy": strategy,
        "pair": pair,
        "data_path": data_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    if data_path is None or not Path(data_path).exists():
        result["status"] = "skipped"
        result["reason"] = f"No data file for {pair}"
        return result

    try:
        loader = CsvDataLoader()
        bars = loader.load(data_path)
        if not bars or len(bars) < 200:
            result["status"] = "skipped"
            result["reason"] = f"Insufficient bars: {len(bars) if bars else 0}"
            return result

        spread = get_spread_for_pair(pair)
        wf_result = run_named_strategy_walk_forward(
            strategy_name=strategy,
            bars=bars,
            pair=pair,
            spread_pips=spread,
            n_windows=windows,
        )

        # Handle both dict and dataclass results
        if isinstance(wf_result, dict):
            rd = wf_result
        else:
            rd = {
                "windows_passed": getattr(wf_result, "windows_passed", 0),
                "go_nogo": getattr(wf_result, "go_nogo", False),
                "mean_profit_factor": getattr(wf_result, "mean_profit_factor", 0),
                "mean_win_rate": getattr(wf_result, "mean_win_rate", 0),
                "mean_sharpe_ratio": getattr(wf_result, "mean_sharpe_ratio", 0),
                "mean_max_drawdown": getattr(wf_result, "mean_max_drawdown", 0),
                "mean_trade_count": getattr(wf_result, "mean_trade_count", 0),
                "mean_total_pnl": getattr(wf_result, "mean_total_pnl", 0),
                "per_window": getattr(wf_result, "per_window", []),
            }

        result["status"] = "complete"
        result["windows_passed"] = rd.get("windows_passed", 0)
        result["windows_total"] = windows
        result["go_nogo"] = rd.get("go_nogo", False)
        result["mean_profit_factor"] = rd.get("mean_profit_factor", 0)
        result["mean_win_rate"] = rd.get("mean_win_rate", 0)
        result["mean_sharpe"] = rd.get("mean_sharpe_ratio", 0)
        result["mean_max_drawdown"] = rd.get("mean_max_drawdown", 0)
        result["mean_trade_count"] = rd.get("mean_trade_count", 0)
        result["mean_total_pnl"] = rd.get("mean_total_pnl", 0)
        result["per_window"] = [
            w
            if isinstance(w, dict)
            else {
                k: getattr(w, k, None)
                for k in [
                    "window",
                    "train_start",
                    "train_end",
                    "test_start",
                    "test_end",
                    "trades",
                    "win_rate",
                    "profit_factor",
                    "total_pnl",
                    "max_drawdown",
                    "sharpe_ratio",
                ]
            }
            for w in rd.get("per_window", [])
        ]

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()[-500:]

    return result


def main():
    parser = argparse.ArgumentParser(description="Multi-strategy WF evaluation (sequential, CPU-capped)")
    parser.add_argument("--dry-run", action="store_true", help="List evaluations without running")
    parser.add_argument("--windows", type=int, default=5, help="Number of WF windows (default: 5)")
    args = parser.parse_args()

    # Filter out entries with no data
    evaluations = [e for e in EVALUATION_MATRIX if e["data"] is not None and Path(e["data"]).exists()]
    skipped = [e for e in EVALUATION_MATRIX if e not in evaluations]

    print("Multi-Strategy WF Evaluation")
    print(f"  Total evaluations: {len(EVALUATION_MATRIX)}")
    print(f"  Will run: {len(evaluations)}")
    print(f"  Skipped (no data): {len(skipped)}")
    print(f"  Windows per eval: {args.windows}")
    print("  CPU cap: 20%, Memory cap: 2048MB")
    print("  Mode: SEQUENTIAL (one at a time)")
    print()

    if args.dry_run:
        print("Dry run — evaluations that would run:")
        for i, e in enumerate(evaluations, 1):
            print(f"  {i}. {e['strategy']} on {e['pair']} ({e['data']})")
        return

    all_results = []

    # Run sequentially with CPU + memory caps
    with cpu_limited(percent=20):
        with memory_capped(mb=2048):
            for i, eval_config in enumerate(evaluations, 1):
                strategy = eval_config["strategy"]
                pair = eval_config["pair"]
                data_path = eval_config["data"]

                print(
                    f"[{i}/{len(evaluations)}] {strategy} on {pair}...",
                    end=" ",
                    flush=True,
                )
                start = time.time()

                result = run_single_wf(strategy, pair, data_path, args.windows)
                elapsed = time.time() - start

                if result["status"] == "complete":
                    pf = result.get("mean_profit_factor", 0)
                    wr = result.get("mean_win_rate", 0)
                    passed = result.get("windows_passed", 0)
                    total = result.get("windows_total", 0)
                    pnl = result.get("mean_total_pnl", 0)
                    print(f"DONE ({elapsed:.1f}s) — {passed}/{total} PASS | PF={pf:.2f} | WR={wr:.1%} | PnL={pnl:.0f}")
                elif result["status"] == "skipped":
                    print(f"SKIP — {result.get('reason', 'unknown')}")
                else:
                    print(f"ERROR — {result.get('error', 'unknown')}")

                all_results.append(result)

                # Save incremental results after each run
                with open(REPORT_DIR / "results.jsonl", "a") as f:
                    f.write(json.dumps(result) + "\n")

    # Generate summary
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_evaluations": len(evaluations),
        "completed": sum(1 for r in all_results if r["status"] == "complete"),
        "skipped": sum(1 for r in all_results if r["status"] == "skipped"),
        "errors": sum(1 for r in all_results if r["status"] == "error"),
        "results": all_results,
    }

    # Identify viable strategies (positive PF + 3+ windows passing)
    viable = [
        r
        for r in all_results
        if r["status"] == "complete" and r.get("mean_profit_factor", 0) > 1.0 and r.get("windows_passed", 0) >= 3
    ]
    summary["viable_strategies"] = viable

    with open(REPORT_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {summary['completed']} complete, {summary['skipped']} skipped, {summary['errors']} errors")
    print(f"Viable strategies (PF>1.0, 3+ windows): {len(viable)}")
    for v in viable:
        print(
            f"  ✅ {v['strategy']} on {v['pair']} — PF={v['mean_profit_factor']:.2f} WR={v['mean_win_rate']:.1%} {v['windows_passed']}/{v['windows_total']} PASS"  # noqa: E501
        )
    print(f"\nFull report: {REPORT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
