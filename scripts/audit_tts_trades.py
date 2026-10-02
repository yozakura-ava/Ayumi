#!/usr/bin/env python3
"""Audit TTS walk-forward trades — extract per-trade pattern details."""

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from backtest.engine import BacktestConfig, Bar
from backtest.multi_strategy_engine import MultiStrategyBacktestEngine
from backtest.strategies.tts_strategy import TTSStrategy
from quant.walk_forward import WalkForwardValidator


def load_bars(csv_path: str) -> list[Bar]:
    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["Date"])
    df = df.sort_values("time").reset_index(drop=True)
    return [
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


def main():
    pair = "EURUSD"
    csv_path = f"data/forex/historical/{pair}_M15.csv"
    spread_pips = 1.2
    min_confidence = 0.30
    min_quality = 0.20
    n_windows = 3
    train_ratio = 0.65
    val_ratio = 0.15

    bars = load_bars(csv_path)
    print(f"Loaded {len(bars)} bars: {bars[0].time} → {bars[-1].time}")

    config = BacktestConfig(
        starting_balance=10000,
        spread_pips=spread_pips,
        commission_per_lot=3.5,
        pair=pair,
        min_confidence=min_confidence,
    )

    validator = WalkForwardValidator(
        data=bars,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
    )

    all_results = {}
    for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):  # noqa: B007
        print(f"\n{'=' * 60}")
        print(f"Window {idx}: train={len(train_bars)}, test={len(test_bars)}")
        print(
            f"  Test range: {test_bars[0].time if test_bars else 'N/A'} → {test_bars[-1].time if test_bars else 'N/A'}"
        )

        strategy = TTSStrategy(
            symbol=pair,
            min_confidence=min_confidence,
            min_quality_score=min_quality,
            lookback=200,
        )
        strategy.reset()

        engine = MultiStrategyBacktestEngine(config, [strategy])
        result = engine.run_all_strategies(test_bars)

        strategy_name = strategy.name
        if strategy_name not in result:
            print(f"  No results for {strategy_name}")
            continue

        metrics = result[strategy_name].metrics
        trades = metrics.trades

        print(f"  Trades: {len(trades)}")
        print(f"  Win rate: {metrics.win_rate:.1%}")
        print(f"  Net P&L: ${metrics.total_pnl:.2f}")

        trade_details = []
        for t in trades:
            # Parse rationale for pattern info
            rationale = t.rationale
            pattern_type = "unknown"
            direction_raw = "unknown"

            # Parse pattern type from rationale: "TTC/TBD EURUSD: M long @ ..."
            if "pattern_type=" in rationale or ": M " in rationale or ": W " in rationale:
                # Standard format: "TTC/TBD EURUSD: M long @ ..."
                parts = rationale.split(": ", 1)
                if len(parts) >= 2:
                    sub = parts[1]
                    tokens = sub.split()
                    if tokens:
                        pattern_type = tokens[0]  # "M", "W", "FL-001", etc.
                    if len(tokens) >= 2:
                        direction_raw = tokens[1]  # "long" or "short"

            trade_details.append(
                {
                    "bar_entry": t.entry_bar_index,
                    "bar_exit": t.exit_bar_index,
                    "entry_time": str(t.entry_time),
                    "exit_time": str(t.exit_time),
                    "pattern_type": pattern_type,
                    "direction": direction_raw,
                    "confidence": t.confidence_score,
                    "entry_price": t.entry_price,
                    "stop_loss": t.stop_loss,
                    "tp1": t.take_profit_1,
                    "exit_price": t.exit_price,
                    "pips": t.pips,
                    "pnl": t.profit_loss,
                    "outcome": t.outcome.value,
                    "exit_reason": t.exit_reason.value,
                    "rationale": rationale,
                }
            )

        all_results[f"window_{idx}"] = {
            "trade_count": len(trades),
            "win_rate": metrics.win_rate,
            "net_pnl": metrics.total_pnl,
            "trades": trade_details,
        }

        # Print trade table for this window
        if trades:
            print(
                f"\n  {'#':>3} {'Time':<20} {'Pattern':<8} {'Dir':<5} {'Conf':>5} {'Entry':>10} {'SL':>10} {'Exit':>10} {'Pips':>8} {'PnL':>10} {'Result':<6} {'ExitWhy':<12}"  # noqa: E501
            )
            print(f"  {'─' * 120}")
            for i, td in enumerate(trade_details):
                print(
                    f"  {i + 1:>3} {td['entry_time']:<20} {td['pattern_type']:<8} {td['direction']:<5} {td['confidence']:>5.2f} {td['entry_price']:>10.5f} {td['stop_loss']:>10.5f} {td['exit_price']:>10.5f} {td['pips']:>8.1f} {td['pnl']:>10.2f} {td['outcome']:<6} {td['exit_reason']:<12}"  # noqa: E501
                )

    # ── Analysis ──
    print(f"\n{'=' * 60}")
    print("ANALYSIS")
    print(f"{'=' * 60}")

    all_trades = []
    for wdata in all_results.values():
        all_trades.extend(wdata["trades"])

    if not all_trades:
        print("NO TRADES FOUND — strategy produced zero signals.")
        print("Possible causes:")
        print("  1. Gate validation blocking all patterns")
        print("  2. HTF phase filter rejecting (consolidating/conflicting)")
        print("  3. Kill zone filter too restrictive")
        print("  4. Confidence threshold too high")
        return

    total = len(all_trades)
    wins = [t for t in all_trades if t["outcome"] == "win"]
    losses = [t for t in all_trades if t["outcome"] == "loss"]
    print(f"\nTotal trades: {total}, Wins: {len(wins)}, Losses: {len(losses)}")
    print(f"Overall win rate: {len(wins) / total:.1%}")

    # By pattern type
    print("\n--- By Pattern Type ---")
    pattern_stats = {}
    for t in all_trades:
        pt = t["pattern_type"]
        if pt not in pattern_stats:
            pattern_stats[pt] = {"wins": 0, "losses": 0, "pnl": 0, "pips": []}
        if t["outcome"] == "win":
            pattern_stats[pt]["wins"] += 1
        else:
            pattern_stats[pt]["losses"] += 1
        pattern_stats[pt]["pnl"] += t["pnl"]
        pattern_stats[pt]["pips"].append(t["pips"])

    for pt, s in sorted(pattern_stats.items()):
        n = s["wins"] + s["losses"]
        wr = s["wins"] / n if n > 0 else 0
        avg_pips = sum(s["pips"]) / n if n > 0 else 0
        print(f"  {pt:<12}: {n:>3} trades, WR={wr:>5.1%}, PnL=${s['pnl']:>8.2f}, avg_pips={avg_pips:>7.1f}")

    # By direction
    print("\n--- By Direction ---")
    dir_stats = {}
    for t in all_trades:
        d = t["direction"]
        if d not in dir_stats:
            dir_stats[d] = {"wins": 0, "losses": 0, "pnl": 0}
        if t["outcome"] == "win":
            dir_stats[d]["wins"] += 1
        else:
            dir_stats[d]["losses"] += 1
        dir_stats[d]["pnl"] += t["pnl"]
    for d, s in sorted(dir_stats.items()):
        n = s["wins"] + s["losses"]
        wr = s["wins"] / n if n > 0 else 0
        print(f"  {d:<8}: {n:>3} trades, WR={wr:>5.1%}, PnL=${s['pnl']:>8.2f}")

    # By confidence bucket
    print("\n--- By Confidence ---")
    conf_buckets = {"0.30-0.40": [], "0.40-0.50": [], "0.50-0.60": [], "0.60+": []}
    for t in all_trades:
        c = t["confidence"]
        if c < 0.40:
            conf_buckets["0.30-0.40"].append(t)
        elif c < 0.50:
            conf_buckets["0.40-0.50"].append(t)
        elif c < 0.60:
            conf_buckets["0.50-0.60"].append(t)
        else:
            conf_buckets["0.60+"].append(t)

    for bucket, trades in conf_buckets.items():
        if not trades:
            print(f"  {bucket}: no trades")
            continue
        w = sum(1 for t in trades if t["outcome"] == "win")
        wr = w / len(trades)
        print(f"  {bucket}: {len(trades):>3} trades, WR={wr:>5.1%}")

    # By exit reason
    print("\n--- By Exit Reason ---")
    exit_stats = {}
    for t in all_trades:
        er = t["exit_reason"]
        if er not in exit_stats:
            exit_stats[er] = {"wins": 0, "losses": 0, "pnl": 0}
        if t["outcome"] == "win":
            exit_stats[er]["wins"] += 1
        else:
            exit_stats[er]["losses"] += 1
        exit_stats[er]["pnl"] += t["pnl"]
    for er, s in sorted(exit_stats.items()):
        n = s["wins"] + s["losses"]
        wr = s["wins"] / n if n > 0 else 0
        print(f"  {er:<20}: {n:>3} trades, WR={wr:>5.1%}, PnL=${s['pnl']:>8.2f}")

    # Session info from rationale (if available)
    print("\n--- Sample Rationales ---")
    for i, t in enumerate(all_trades[:5]):
        print(f"  Trade {i + 1}: {t['rationale'][:120]}")

    # Save full results
    out_path = Path("reports/tts_walkforward/audit_trades_detail.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull details saved to: {out_path}")


if __name__ == "__main__":
    main()
