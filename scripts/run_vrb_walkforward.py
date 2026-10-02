#!/usr/bin/env python3
"""
Walk-Forward Backtest — VRB (Volatility Regime Breakout)

Runs walk-forward evaluation of VRB strategy on GBPUSD M15.

Usage:
    python scripts/run_vrb_walkforward.py [--pairs GBPUSD] [--timeframe M15]
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from backtest.engine import Bar
from backtest.walk_forward_runner import run_strategy_walk_forward
from common.resource_limits import add_resource_args, run_limited
from signal_engine.risk_sizer import ConfidencePositionSizer
from strategies.volatility_regime_breakout import (
    VolatilityRegimeBreakoutStrategy,
    VRBConfig,
)

PAIRS_CONFIG = {
    "GBPUSD": {
        "csv": "data/forex/historical/GBPUSD_{tf}.csv",
        "spread": 1.5,
    },
    "EURUSD": {
        "csv": "data/forex/historical/EURUSD_{tf}.csv",
        "spread": 1.2,
    },
    "USDJPY": {
        "csv": "data/forex/historical/USDJPY_{tf}.csv",
        "spread": 1.3,
    },
}

DEFAULT_PAIRS = ["GBPUSD"]
DEFAULT_TIMEFRAME = "M15"
DEFAULT_WINDOWS = 5
DEFAULT_TRAIN_RATIO = 0.65
DEFAULT_VAL_RATIO = 0.15
DEFAULT_MIN_CONFIDENCE = 0.30


def load_bars(csv_path: str) -> list[Bar]:
    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["Date"])
    df = df.sort_values("time").reset_index(drop=True)
    bars = [
        Bar(
            time=row["time"].to_pydatetime(),
            open=row["Open"],
            high=row["High"],
            low=row["Low"],
            close=row["Close"],
            volume=row.get("Volume", 0),
        )
        for _, row in df.iterrows()
    ]
    return bars


def run_pair(
    pair: str,
    tf: str,
    min_confidence: float,
    n_windows: int,
    train_ratio: float,
    val_ratio: float,
    risk_sizer: ConfidencePositionSizer,
) -> dict:
    cfg = PAIRS_CONFIG[pair]
    csv_path = cfg["csv"].format(tf=tf)
    spread = cfg["spread"]

    print(f"\n{'─' * 60}")
    print(f"  {pair} | {tf} | conf>={min_confidence}")
    print(f"{'─' * 60}")

    bars = load_bars(csv_path)
    if len(bars) < 500:
        print(f"  SKIP — only {len(bars)} bars")
        return {"pair": pair, "tf": tf, "error": "Insufficient bars"}

    print(f"  Bars: {len(bars)} | {bars[0].time} → {bars[-1].time}")

    vrb_config = VRBConfig(min_confidence=min_confidence)

    def factory() -> VolatilityRegimeBreakoutStrategy:
        return VolatilityRegimeBreakoutStrategy(vrb_config)

    result = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        spread_pips=spread,
        commission_per_lot=3.5,
        min_confidence=min_confidence,
        risk_sizer=risk_sizer,
    )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="VRB Walk-Forward Backtest")
    add_resource_args(parser)
    parser.add_argument(
        "--pairs",
        nargs="+",
        default=DEFAULT_PAIRS,
        choices=list(PAIRS_CONFIG.keys()),
    )
    parser.add_argument(
        "--timeframe",
        "--tf",
        default=DEFAULT_TIMEFRAME,
        choices=["M5", "M15", "H1", "H4", "D1"],
    )
    parser.add_argument("--windows", type=int, default=DEFAULT_WINDOWS)
    parser.add_argument("--train-ratio", type=float, default=DEFAULT_TRAIN_RATIO)
    parser.add_argument("--val-ratio", type=float, default=DEFAULT_VAL_RATIO)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--report-dir", default="reports/vrb_walkforward")
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=0.01,
        help="Max risk %% per trade at highest confidence tier (default: 0.01 = 1%%)",
    )
    args = parser.parse_args()

    risk_sizer = ConfidencePositionSizer(account_size=10000.0)
    print(f"  Risk sizer tiers: {[(t.min_confidence, t.max_confidence, t.risk_pct) for t in risk_sizer.tiers]}")

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    print(f"\n{'═' * 60}")
    print(f"  VRB Walk-Forward — {args.timeframe}")
    print(f"  Pairs: {args.pairs}")
    print(f"  Windows: {args.windows} | Train: {args.train_ratio} | Val: {args.val_ratio}")
    print(f"  Min confidence: {args.min_confidence}")
    print(f"{'═' * 60}")

    all_results = {}
    all_trade_records = {}
    go_nogo = {}

    for pair in args.pairs:
        result = run_pair(
            pair=pair,
            tf=args.timeframe,
            min_confidence=args.min_confidence,
            n_windows=args.windows,
            train_ratio=args.train_ratio,
            val_ratio=args.val_ratio,
            risk_sizer=risk_sizer,
        )
        all_results[pair] = result

        trade_records = getattr(result, "_trade_records", [])
        if trade_records:
            all_trade_records[pair] = [{**t, "pair": pair} for t in trade_records]
            print(f"  Trade records: {len(trade_records)}")

        agg = getattr(result, "aggregated", None)
        if agg is not None:
            net_profit = getattr(agg, "mean_total_pnl", 0)
            win_rate = getattr(agg, "mean_win_rate", 0)
            profit_factor = getattr(agg, "mean_profit_factor", 0)
            max_dd = getattr(agg, "mean_max_drawdown", 1.0) * 100
            total_trades = int(getattr(agg, "mean_trade_count", 0))
            windows_passed = getattr(agg, "windows_passed", 0)
            total_windows = getattr(agg, "total_windows", 0)

            passes = total_trades >= 20 and max_dd <= 5.0 and net_profit > 0 and win_rate >= 0.40
            go_nogo[pair] = {
                "pass": passes,
                "net_profit": net_profit,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "max_dd": max_dd,
                "total_trades": total_trades,
                "windows_passed": windows_passed,
                "total_windows": total_windows,
            }
            status = "GO" if passes else "NO-GO"
            print(
                f"\n  {pair}: {status} | P&L: ${net_profit:.2f} | WR: {win_rate:.1%} | "
                f"PF: {profit_factor:.2f} | DD: {max_dd:.2f}% | Trades: {total_trades} | "
                f"Windows: {windows_passed}/{total_windows}"
            )
        else:
            print(f"\n  {pair}: No results")

    report_path = report_dir / f"vrb_{args.timeframe}_{timestamp}.json"
    flat_trades = []
    for pair, records in all_trade_records.items():  # noqa: B007
        flat_trades.extend(records)

    report = {
        "strategy": "VolatilityRegimeBreakoutStrategy",
        "timeframe": args.timeframe,
        "config": {
            "min_confidence": args.min_confidence,
            "n_windows": args.windows,
            "train_ratio": args.train_ratio,
            "val_ratio": args.val_ratio,
        },
        "per_pair": {
            pair: {
                "per_window": [  # noqa: B035
                    {
                        "win_rate": m.win_rate,
                        "profit_factor": m.profit_factor,
                        "max_drawdown": m.max_drawdown,
                        "trade_count": m.trade_count,
                        "passed": m.passed_go_nogo,
                    }
                    for m in r.per_window
                ]
                if hasattr(r, "per_window")
                else []
                for pair, r in all_results.items()
            }
        },
        "trade_records": flat_trades,
        "go_nogo": go_nogo,
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nReport: {report_path}")

    print(f"\n{'═' * 60}")
    print("  SUMMARY")
    print(f"{'═' * 60}")
    print(f"  {'Pair':<10} {'P&L':>10} {'WR':>7} {'PF':>6} {'DD%':>6} {'Trades':>7} {'Win':>4} {'Verdict':<10}")
    print(f"  {'─' * 65}")
    for pair in args.pairs:
        verdict = go_nogo.get(pair, {})
        status = "GO" if verdict.get("pass") else "NO-GO"
        pnl = verdict.get("net_profit", 0)
        wr = verdict.get("win_rate", 0)
        pf = verdict.get("profit_factor", 0)
        dd = verdict.get("max_dd", 0)
        trades = verdict.get("total_trades", 0)
        wins = int(verdict.get("windows_passed", 0))
        print(
            f"  {pair:<10} ${pnl:>8.2f} {wr:>6.1%} {pf:>5.2f} {dd:>5.2f}% {trades:>6} {wins:>3}/{verdict.get('total_windows', 0)} {status}"  # noqa: E501
        )
    print(f"  {'─' * 65}")

    go_count = sum(1 for v in go_nogo.values() if v["pass"])
    print(f"\n  {go_count}/{len(go_nogo)} pairs passed")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
