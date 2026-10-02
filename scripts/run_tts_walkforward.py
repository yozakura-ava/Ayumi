#!/usr/bin/env python3
"""
Walk-Forward Backtest — TTC/TBD Signal Engine (Multi-Instrument)

Runs walk-forward evaluation of TTSStrategy across 5 pairs:
EURUSD, GBPUSD, USDJPY, GBPJPY, XAUUSD

Supports M15, H1, H4, D1 timeframes.

Usage:
    python scripts/run_tts_walkforward.py [--pairs EURUSD GBPUSD] [--timeframe M15]
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
from backtest.strategies import TTSStrategy
from backtest.walk_forward_runner import run_strategy_walk_forward
from common.resource_limits import add_resource_args, run_limited
from ml.per_symbol_configs import DEFAULT_SYMBOL_CONFIG, PER_SYMBOL_CONFIGS
from quant.go_nogo_criteria import PerWindowCriteria
from signal_engine.risk_sizer import (
    ConfidencePositionSizer,
    parse_tiers,
)

TTS_PER_WINDOW = PerWindowCriteria(
    min_trades=20,
    win_rate=0.40,
    profit_factor=1.0,
    max_drawdown=0.05,
)


PAIRS_CONFIG = {
    "EURUSD": {
        "csv": "data/forex/historical/EURUSD_{tf}.csv",
        "spread": 1.2,
    },
    "GBPUSD": {
        "csv": "data/forex/historical/GBPUSD_{tf}.csv",
        "spread": 1.5,
    },
    "USDJPY": {
        "csv": "data/forex/historical/USDJPY_{tf}.csv",
        "spread": 1.3,
    },
    "GBPJPY": {
        "csv": "data/forex/historical/GBPJPY_{tf}.csv",
        "spread": 2.0,
    },
    "XAUUSD": {
        "csv": "data/forex/historical/XAUUSD_{tf}.csv",
        "spread": 3.0,
    },
}

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "XAUUSD"]
DEFAULT_TIMEFRAME = "M15"
DEFAULT_WINDOWS = 5
DEFAULT_TRAIN_RATIO = 0.65
DEFAULT_VAL_RATIO = 0.15
DEFAULT_MIN_CONFIDENCE = None
DEFAULT_MIN_QUALITY = 0.60


def get_per_symbol_min_confidence(pair: str, tf: str) -> float:
    config = PER_SYMBOL_CONFIGS.get(pair.upper(), {}).get(tf, DEFAULT_SYMBOL_CONFIG)
    return config["base_confidence"]


def load_bars(csv_path: str, tf: str) -> list[Bar]:
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
    min_quality_score: float,
    n_windows: int,
    train_ratio: float,
    val_ratio: float,
    risk_sizer: ConfidencePositionSizer,
) -> dict:
    cfg = PAIRS_CONFIG[pair]
    csv_path = cfg["csv"].format(tf=tf)
    spread = cfg["spread"]

    print(f"\n{'─' * 60}")
    print(f"  {pair} | {tf} | conf>={min_confidence} | qual>={min_quality_score}")
    print(f"{'─' * 60}")

    bars = load_bars(csv_path, tf)
    if len(bars) < 500:
        print(f"  SKIP — only {len(bars)} bars")
        return {"pair": pair, "tf": tf, "error": "Insufficient bars"}

    print(f"  Bars: {len(bars)} | {bars[0].time} → {bars[-1].time}")

    def factory() -> TTSStrategy:
        return TTSStrategy(
            symbol=pair,
            min_confidence=min_confidence,
            min_quality_score=min_quality_score,
        )

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
    parser = argparse.ArgumentParser(description="TTC/TBD Walk-Forward Backtest")
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
    parser.add_argument("--min-quality", type=float, default=DEFAULT_MIN_QUALITY)
    parser.add_argument("--report-dir", default="reports/tts_walkforward")
    parser.add_argument(
        "--confidence-tiers",
        type=str,
        default=None,
        help=('JSON list of [min_conf, max_conf, risk_pct] tiers, e.g. "[[0.85,1.0,0.01],[0.70,0.85,0.0075]]"'),
    )
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=0.01,
        help="Max risk %% per trade at highest confidence tier (default: 0.01 = 1%%)",
    )
    args = parser.parse_args()

    # Build risk sizer
    if args.confidence_tiers:
        custom_tiers = parse_tiers(args.confidence_tiers)
        risk_sizer = ConfidencePositionSizer(account_size=10000.0, tiers=custom_tiers)
    else:
        risk_sizer = ConfidencePositionSizer(
            account_size=10000.0,
        )
    print(f"  Risk sizer tiers: {[(t.min_confidence, t.max_confidence, t.risk_pct) for t in risk_sizer.tiers]}")

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    print(f"\n{'═' * 60}")
    print(f"  TTC/TBD Walk-Forward — {args.timeframe}")
    print(f"  Pairs: {args.pairs}")
    print(f"  Windows: {args.windows} | Train: {args.train_ratio} | Val: {args.val_ratio}")
    cli_confidence = args.min_confidence
    print(f"  Min quality: {args.min_quality}")
    if cli_confidence is not None:
        print(f"  Min confidence (CLI override): {cli_confidence}")
    else:
        print("  Min confidence: per-symbol ML values")
    print(f"{'═' * 60}")

    all_results = {}
    all_trade_records = {}
    go_nogo = {}

    for pair in args.pairs:
        pair_confidence = (
            cli_confidence if cli_confidence is not None else get_per_symbol_min_confidence(pair, args.timeframe)
        )
        result = run_pair(
            pair=pair,
            tf=args.timeframe,
            min_confidence=pair_confidence,
            min_quality_score=args.min_quality,
            n_windows=args.windows,
            train_ratio=args.train_ratio,
            val_ratio=args.val_ratio,
            risk_sizer=risk_sizer,
        )
        all_results[pair] = result

        # Extract per-trade records with confidence scores
        trade_records = getattr(result, "_trade_records", [])
        if trade_records:
            all_trade_records[pair] = [{**t, "pair": pair} for t in trade_records]
            print(f"  Trade records: {len(trade_records)} (with confidence scores)")

        # Extract go/nogo from walk-forward results using shared module
        agg = getattr(result, "aggregated", None)
        if agg is not None:
            net_profit = getattr(agg, "mean_total_pnl", 0)
            win_rate = getattr(agg, "mean_win_rate", 0)
            max_dd_decimal = getattr(agg, "mean_max_drawdown", 1.0)
            total_trades = int(getattr(agg, "mean_trade_count", 0))
            profit_factor = getattr(agg, "mean_profit_factor", 0.0)

            pw_result = TTS_PER_WINDOW.evaluate(
                trade_count=total_trades,
                win_rate=win_rate,
                profit_factor=profit_factor,
                total_pnl=net_profit,
                max_drawdown=max_dd_decimal,
            )
            passes = pw_result.passed
            max_dd = max_dd_decimal * 100
            go_nogo[pair] = {
                "pass": passes,
                "net_profit": net_profit,
                "win_rate": win_rate,
                "max_dd": max_dd,
                "total_trades": total_trades,
            }
            status = "✅ GO" if passes else "❌ NO-GO"
            print(
                f"\n  {pair}: {status} | P&L: ${net_profit:.2f} | WR: {win_rate:.1%} | DD: {max_dd:.2f}% | Trades: {total_trades}"  # noqa: E501
            )
        elif isinstance(result, dict) and "aggregated" in result:
            agg = result["aggregated"]
            metrics = agg.get("aggregated_metrics", {})
            net_profit = metrics.get("net_profit", 0)
            win_rate = metrics.get("win_rate", 0)
            max_dd = metrics.get("max_drawdown_pct", 100)
            total_trades = metrics.get("total_trades", 0)
            profit_factor = metrics.get("profit_factor", 0.0)

            pw_result = TTS_PER_WINDOW.evaluate(
                trade_count=total_trades,
                win_rate=win_rate,
                profit_factor=profit_factor,
                total_pnl=net_profit,
                max_drawdown=max_dd / 100.0,
            )
            passes = pw_result.passed
            go_nogo[pair] = {
                "pass": passes,
                "net_profit": net_profit,
                "win_rate": win_rate,
                "max_dd": max_dd,
                "total_trades": total_trades,
            }
            status = "✅ GO" if passes else "❌ NO-GO"
            print(
                f"\n  {pair}: {status} | P&L: ${net_profit:.2f} | WR: {win_rate:.1%} | DD: {max_dd:.2f}% | Trades: {total_trades}"  # noqa: E501
            )
        else:
            print(f"\n  {pair}: No results")

    # Save report
    report_path = report_dir / f"tts_{args.timeframe}_{timestamp}.json"
    # Flatten all trade records
    flat_trades = []
    for pair, records in all_trade_records.items():  # noqa: B007
        flat_trades.extend(records)

    report = {
        "strategy": "TTSStrategy",
        "timeframe": args.timeframe,
        "config": {
            "min_confidence": (
                {p: get_per_symbol_min_confidence(p, args.timeframe) for p in args.pairs}
                if cli_confidence is None
                else cli_confidence
            ),
            "min_quality_score": args.min_quality,
            "n_windows": args.windows,
            "train_ratio": args.train_ratio,
            "val_ratio": args.val_ratio,
        },
        "per_pair": all_results,
        "trade_records": flat_trades,
        "go_nogo": go_nogo,
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nReport: {report_path}")

    # ── Tier distribution analysis ──────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  TIER DISTRIBUTION")
    print(f"{'═' * 60}")
    tier_dist = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    for record in flat_trades:
        conf = record.get("confidence_score", record.get("confidence", 0))
        tier = 5 if conf >= 0.85 else (4 if conf >= 0.70 else (3 if conf >= 0.55 else (2 if conf >= 0.40 else 1)))
        tier_dist[tier] += 1
    total_trades_all = sum(tier_dist.values())
    for t in sorted(tier_dist.keys(), reverse=True):
        count = tier_dist[t]
        pct = count / total_trades_all * 100 if total_trades_all > 0 else 0
        print(f"  Tier {t}: {count:>4} trades ({pct:.1f}%)")
    print(f"  Total: {total_trades_all} trades")

    # ── Confluence factor frequency ──────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  CONFLUENCE FACTOR FREQUENCY")
    print(f"{'═' * 60}")
    boost_counts = {}
    import re

    for record in flat_trades:
        rationale = record.get("rationale", "")
        boosts = re.findall(r"\('([^']+)',\s*[\d.]+\)", rationale) if "boosts=" in rationale else []
        for b in boosts:
            boost_counts[b] = boost_counts.get(b, 0) + 1
    for name, count in sorted(boost_counts.items(), key=lambda x: -x[1]):
        pct = count / total_trades_all * 100 if total_trades_all > 0 else 0
        print(f"  {name:<25} {count:>4} ({pct:.1f}%)")

    # ── Per-boost win rate analysis ──────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  PER-BOOST WIN RATE (top factors)")
    print(f"{'═' * 60}")

    top_boosts = sorted(boost_counts.items(), key=lambda x: -x[1])[:10]
    for boost_name, _ in top_boosts:
        wins = 0
        total = 0
        for record in flat_trades:
            rationale = record.get("rationale", "")
            has_boost = f"('{boost_name}'," in rationale
            if has_boost:
                total += 1
                if record.get("pnl", 0) > 0:
                    wins += 1
        wr = wins / total * 100 if total > 0 else 0
        print(f"  {boost_name:<25} WR: {wr:>5.1f}% ({wins}/{total} trades)")

    # ── Summary table ────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("  SUMMARY")
    print(f"{'═' * 60}")
    print(
        f"  {'Pair':<10} {'P&L':>10} {'WR':>7} {'DD%':>6} {'Trades':>7} {'T5':>4} {'T4':>4} {'T3':>4} {'Verdict':<10}"
    )
    print(f"  {'─' * 70}")
    for pair in args.pairs:
        records = all_trade_records.get(pair, [])
        verdict = go_nogo.get(pair, {})
        status = "✅ GO" if verdict.get("pass") else "❌ NO-GO"
        total = len(records)
        wins = sum(1 for r in records if r.get("pnl", 0) > 0)
        wr = wins / total * 100 if total > 0 else 0
        t5 = sum(1 for r in records if r.get("confidence_score", r.get("confidence", 0)) >= 0.85)
        t4 = sum(1 for r in records if 0.70 <= r.get("confidence_score", r.get("confidence", 0)) < 0.85)
        t3 = sum(1 for r in records if 0.55 <= r.get("confidence_score", r.get("confidence", 0)) < 0.70)
        pnl = verdict.get("net_profit", 0)
        dd = verdict.get("max_dd", 0)
        print(f"  {pair:<10} ${pnl:>8.2f} {wr:>6.1f}% {dd:>5.2f}% {total:>6} {t5:>4} {t4:>4} {t3:>4} {status}")
    print(f"  {'─' * 70}")

    go_count = sum(1 for v in go_nogo.values() if v["pass"])
    print(f"\n  {go_count}/{len(go_nogo)} pairs passed FTMO-style criteria")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
