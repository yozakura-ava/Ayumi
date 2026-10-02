"""ML-augmented walk-forward backtest for TTSStrategy.

Pipeline:
1. Run rule-based backtest on all data to collect trades with confluence features.
2. Extract confluence feature vectors from trade rationale strings.
3. Train a ConfidenceLearner (RandomForest) per (symbol, timeframe).
4. Re-run backtest with ML-adjusted confidence filtering.
5. Compare results.

Usage:
    cd $AYUMI_ROOT/src/forex_bot && python3 -m ml.backtest_with_ml
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

# Project root: backtest_with_ml.py -> ml/ -> forex_bot/ -> src/ -> project_root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

from backtest.engine import (  # noqa: I001
    BacktestConfig,
    Bar,
    SimulatedTrade,
    TradeOutcome,
    get_spread_for_pair,
)
from backtest.multi_strategy_engine import MultiStrategyBacktestEngine
from backtest.strategies import TTSStrategy
from ml.confidence_learner import ConfidenceLearner
from ml.confluence_features import ConfluenceFeatureExtractor

# ── Configuration ──────────────────────────────────────────────────────

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "XAUUSD"]
TIMEFRAME = "M15"
DATA_DIR = PROJECT_ROOT / "data" / "forex" / "historical"

DEFAULT_CONFIG = {
    "starting_balance": 10000,
    "commission_per_lot": 3.5,
    "min_confidence": 0.50,
    "min_quality_score": 0.60,
}

# ML thresholds
ML_HIGH_PROBA = 0.58  # boost confidence
ML_LOW_PROBA = 0.42  # reduce / skip
ML_CONFIDENCE_BOOST = 0.05
ML_CONFIDENCE_PENALTY = 0.10

# OOS holdout start date — replaces the implicit _2026.csv filename convention.
# When CSV files migrate to DuckDB, use this date to filter holdout rows:
#   WHERE timestamp_utc >= OOS_HOLDOUT_START  -- holdout data
#   WHERE timestamp_utc <  OOS_HOLDOUT_START  -- training data
OOS_HOLDOUT_START = "2026-01-01"


# ── Data Loading ───────────────────────────────────────────────────────


def load_bars(pair: str, tf: str, holdout_only: bool = True) -> list[Bar]:
    """Load historical bars from CSV.

    OOS Holdout Convention
    ----------------------
    Files named ``{PAIR}_{TF}_2026.csv`` contain OOS holdout data
    (>= OOS_HOLDOUT_START).  Files named ``{PAIR}_{TF}.csv`` contain the
    full dataset including the holdout period.

    When *holdout_only* is True (default) the function prefers the
    ``_2026.csv`` file.  If only the full ``.csv`` exists, it filters
    rows to ``>= OOS_HOLDOUT_START`` to preserve the holdout subset.

    When *holdout_only* is False, the full ``.csv`` is loaded without
    filtering.

    DuckDB migration: replace filename selection with
    ``WHERE timestamp_utc >= OOS_HOLDOUT_START``.
    """
    if holdout_only:
        # Prefer the _2026.csv holdout file, fall back to filtered .csv
        csv_path = DATA_DIR / f"{pair}_{tf}_2026.csv"
        if not csv_path.exists():
            csv_path = DATA_DIR / f"{pair}_{tf}.csv"
    else:
        # Prefer the full .csv, fall back to _2026.csv
        csv_path = DATA_DIR / f"{pair}_{tf}.csv"
        if not csv_path.exists():
            csv_path = DATA_DIR / f"{pair}_{tf}_2026.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No data for {pair}/{tf}")

    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["Date"])
    df = df.sort_values("time").reset_index(drop=True)

    # If we fell back to the full .csv but caller wants holdout-only,
    # apply the date filter to preserve the _2026.csv convention.
    if holdout_only and not csv_path.name.endswith("_2026.csv"):
        df = df[df["time"] >= pd.Timestamp(OOS_HOLDOUT_START)].reset_index(drop=True)

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


# ── Trade → Feature Extraction ────────────────────────────────────────


def trade_to_record(trade: SimulatedTrade) -> dict | None:
    """Convert a SimulatedTrade to a feature record dict."""
    if not trade.rationale:
        return None

    return {
        "rationale": trade.rationale,
        "confidence_score": trade.confidence_score,
        "confluence_count": trade.confluence_count,
        "outcome": 1 if trade.outcome == TradeOutcome.WIN else 0,
        "profit_loss": trade.profit_loss,
        "direction": trade.direction.value,
        "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
    }


# ── Phase 1: Collect Trades ───────────────────────────────────────────


def collect_trades(pair: str, tf: str) -> list[SimulatedTrade]:
    """Run rule-based backtest and return all trades."""
    bars = load_bars(pair, tf)
    spread = get_spread_for_pair(pair)

    config = BacktestConfig(
        starting_balance=DEFAULT_CONFIG["starting_balance"],
        spread_pips=spread,
        commission_per_lot=DEFAULT_CONFIG["commission_per_lot"],
        pair=pair,
        min_confidence=DEFAULT_CONFIG["min_confidence"],
    )

    strategy = TTSStrategy(
        symbol=pair,
        min_confidence=DEFAULT_CONFIG["min_confidence"],
        min_quality_score=DEFAULT_CONFIG["min_quality_score"],
    )

    engine = MultiStrategyBacktestEngine(config, [strategy])
    result = engine.run_all_strategies(bars)
    metrics = result[strategy.name].metrics
    return metrics.trades


# ── Phase 2: Train Learners ───────────────────────────────────────────


def train_learners(
    all_trades: dict[str, list[SimulatedTrade]],
    tf: str,
) -> dict[str, ConfidenceLearner]:
    """Train a ConfidenceLearner per symbol."""
    extractor = ConfluenceFeatureExtractor()
    learners = {}

    for pair, trades in all_trades.items():
        records = [trade_to_record(t) for t in trades]
        records = [r for r in records if r is not None]

        if len(records) < 20:
            print(f"  {pair}: SKIP — only {len(records)} trades with rationale (need 20+)")
            continue

        # Extract features
        for r in records:
            r["features"] = extractor.extract(r)

        learner = ConfidenceLearner(symbol=pair, timeframe=tf)
        try:
            learner.train(records, extractor.FEATURE_NAMES)
            learners[pair] = learner
            print(f"  {pair}: Trained on {learner.train_size} trades")
            print(f"    Top features: {learner.get_top_features(5)}")
        except ValueError as e:
            print(f"  {pair}: {e}")

    return learners


# ── Phase 3: ML-Adjusted Backtest ─────────────────────────────────────


class MLFilteredTTSStrategy(TTSStrategy):
    """TTSStrategy wrapper that adjusts confidence based on ML predictions."""

    def __init__(
        self,
        symbol: str,
        learner: ConfidenceLearner,
        extractor: ConfluenceFeatureExtractor,
        **kwargs,
    ):
        super().__init__(symbol=symbol, **kwargs)
        self._learner = learner
        self._extractor = extractor
        self._ml_stats = {"boosted": 0, "penalized": 0, "skipped": 0, "passed": 0}

    def evaluate(self, state):
        signal = super().evaluate(state)
        if signal is None:
            return None

        # Build a fake trade record from the signal to extract features
        fake_record = {
            "rationale": signal.rationale,
            "confidence_score": signal.confidence,
            "confluence_count": 0,  # not available at signal time
        }
        features = self._extractor.extract(fake_record)
        ml_proba = self._learner.predict_proba(features)

        # Adjust confidence based on ML prediction
        if ml_proba >= ML_HIGH_PROBA:
            signal.confidence = min(1.0, signal.confidence + ML_CONFIDENCE_BOOST)
            self._ml_stats["boosted"] += 1
        elif ml_proba <= ML_LOW_PROBA:
            signal.confidence -= ML_CONFIDENCE_PENALTY
            if signal.confidence < self.min_confidence:
                self._ml_stats["skipped"] += 1
                return None
            self._ml_stats["penalized"] += 1
        else:
            self._ml_stats["passed"] += 1

        return signal


def run_ml_backtest(
    pair: str,
    tf: str,
    learner: ConfidenceLearner,
    extractor: ConfluenceFeatureExtractor,
) -> dict:
    """Run backtest with ML-adjusted confidence."""
    bars = load_bars(pair, tf)
    spread = get_spread_for_pair(pair)

    config = BacktestConfig(
        starting_balance=DEFAULT_CONFIG["starting_balance"],
        spread_pips=spread,
        commission_per_lot=DEFAULT_CONFIG["commission_per_lot"],
        pair=pair,
        min_confidence=DEFAULT_CONFIG["min_confidence"],
    )

    strategy = MLFilteredTTSStrategy(
        symbol=pair,
        learner=learner,
        extractor=extractor,
        min_confidence=DEFAULT_CONFIG["min_confidence"],
        min_quality_score=DEFAULT_CONFIG["min_quality_score"],
    )

    engine = MultiStrategyBacktestEngine(config, [strategy])
    result = engine.run_all_strategies(bars)
    metrics = result[strategy.name].metrics

    return {
        "trades": metrics.trades,
        "metrics": metrics,
        "ml_stats": strategy._ml_stats,
    }


# ── Reporting ──────────────────────────────────────────────────────────


def print_comparison(baseline: dict, ml_result: dict, pair: str):
    bm = baseline
    mm = ml_result
    ms = ml_result["ml_stats"]

    bp = bm.get("total_pnl", 0)
    mp = mm.get("total_pnl", 0)
    bwr = bm.get("win_rate", 0)
    mwr = mm.get("win_rate", 0)
    btc = bm.get("total_trades", 0)
    mtc = mm.get("total_trades", 0)
    mdd = mm.get("max_drawdown_pct", 0)
    mpf = mm.get("profit_factor", 0)

    print(f"\n  {'Metric':<25} {'Baseline':>12} {'ML-Adjusted':>12} {'Delta':>12}")
    print(f"  {'─' * 65}")
    print(f"  {'Total P&L':<25} ${bp:>10.2f} ${mp:>10.2f} ${mp - bp:>+10.2f}")
    print(f"  {'Win Rate':<25} {bwr:>11.1%} {mwr:>11.1%} {mwr - bwr:>+11.1%}")
    print(f"  {'Total Trades':<25} {btc:>12} {mtc:>12} {mtc - btc:>+12}")
    print(f"  {'Max Drawdown %':<25} {'N/A':>12} {mdd:>11.2f}% {'N/A':>12}")
    print(f"  {'Profit Factor':<25} {'N/A':>12} {mpf:>12.2f} {'N/A':>12}")

    print(
        f"\n  ML filter stats: boosted={ms['boosted']}, penalized={ms['penalized']}, skipped={ms['skipped']}, passed={ms['passed']}"  # noqa: E501
    )


# ── Main ───────────────────────────────────────────────────────────────


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    extractor = ConfluenceFeatureExtractor()

    print("=" * 70)
    print("  ML Confidence Pipeline — Confluence Feature Learning")
    print(f"  Timeframe: {TIMEFRAME} | Pairs: {PAIRS}")
    print("=" * 70)

    # Phase 1: Collect trades (rule-based)
    print("\n── Phase 1: Collecting rule-based trades ──")
    all_trades: dict[str, list[SimulatedTrade]] = {}
    baselines: dict[str, dict] = {}

    for pair in PAIRS:
        print(f"\n  {pair}:")
        try:
            trades = collect_trades(pair, TIMEFRAME)
            all_trades[pair] = trades
            baselines[pair] = {"trades": trades, "metrics": None}

            # Quick summary
            wins = sum(1 for t in trades if t.outcome == TradeOutcome.WIN)
            losses = sum(1 for t in trades if t.outcome == TradeOutcome.LOSS)
            total_pnl = sum(t.profit_loss for t in trades)
            wr = wins / len(trades) * 100 if trades else 0
            print(f"    Trades: {len(trades)} | W: {wins} L: {losses} | WR: {wr:.1f}% | P&L: ${total_pnl:.2f}")
        except FileNotFoundError as e:
            print(f"    SKIP: {e}")
        except Exception as e:
            print(f"    ERROR: {e}")

    if not all_trades:
        print("\n  No trades collected. Exiting.")
        return

    # Phase 2: Train learners
    print("\n── Phase 2: Training ML models ──")
    learners = train_learners(all_trades, TIMEFRAME)

    if not learners:
        print("\n  No models trained (insufficient trades). Exiting.")
        return

    # Phase 3: ML-adjusted backtest + comparison
    print("\n── Phase 3: ML-adjusted backtest ──")
    report = {
        "timestamp": timestamp,
        "timeframe": TIMEFRAME,
        "config": {
            "ml_high_proba": ML_HIGH_PROBA,
            "ml_low_proba": ML_LOW_PROBA,
            "ml_boost": ML_CONFIDENCE_BOOST,
            "ml_penalty": ML_CONFIDENCE_PENALTY,
            "min_confidence": DEFAULT_CONFIG["min_confidence"],
            "min_quality_score": DEFAULT_CONFIG["min_quality_score"],
        },
        "per_pair": {},
    }

    summary_rows = []

    for pair in learners:
        learner = learners[pair]
        print(f"\n  {pair} — Learned Weights:")
        for name, imp in sorted(learner.learned_weights.items(), key=lambda x: -x[1]):
            bar = "█" * int(imp * 100)
            print(f"    {name:<30s} {imp:.4f} {bar}")

        print(f"\n  {pair} — Running ML-adjusted backtest...")
        try:
            ml_result = run_ml_backtest(pair, TIMEFRAME, learner, extractor)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue

        baseline_trades = all_trades[pair]
        # Compute baseline metrics from trades directly
        wins_b = sum(1 for t in baseline_trades if t.outcome == TradeOutcome.WIN)
        losses_b = sum(1 for t in baseline_trades if t.outcome == TradeOutcome.LOSS)
        pnl_b = sum(t.profit_loss for t in baseline_trades)

        baseline_summary = {
            "total_trades": len(baseline_trades),
            "wins": wins_b,
            "losses": losses_b,
            "win_rate": wins_b / len(baseline_trades) if baseline_trades else 0,
            "total_pnl": pnl_b,
        }

        ml_trades = ml_result["metrics"].trades
        wins_m = sum(1 for t in ml_trades if t.outcome == TradeOutcome.WIN)
        losses_m = sum(1 for t in ml_trades if t.outcome == TradeOutcome.LOSS)
        pnl_m = sum(t.profit_loss for t in ml_trades)

        ml_summary = {
            "total_trades": len(ml_trades),
            "wins": wins_m,
            "losses": losses_m,
            "win_rate": wins_m / len(ml_trades) if ml_trades else 0,
            "total_pnl": pnl_m,
            "max_drawdown_pct": ml_result["metrics"].max_drawdown_pct,
            "profit_factor": ml_result["metrics"].profit_factor,
            "ml_stats": ml_result["ml_stats"],
        }

        print_comparison(baseline_summary, ml_summary, pair)

        report["per_pair"][pair] = {
            "baseline": baseline_summary,
            "ml_adjusted": ml_summary,
            "learned_weights": learner.learned_weights,
            "top_features": learner.get_top_features(5),
        }

        improved = pnl_m > pnl_b
        wr_delta = ml_summary["win_rate"] - baseline_summary["win_rate"]
        summary_rows.append(
            {
                "pair": pair,
                "baseline_pnl": pnl_b,
                "ml_pnl": pnl_m,
                "delta": pnl_m - pnl_b,
                "baseline_wr": baseline_summary["win_rate"],
                "ml_wr": ml_summary["win_rate"],
                "wr_delta": wr_delta,
                "improved": improved,
            }
        )

    # Save report
    report_path = PROJECT_ROOT / "reports" / "ml_confidence" / f"ml_{TIMEFRAME}_{timestamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report saved: {report_path}")

    # Final summary
    print(f"\n{'═' * 70}")
    print("  SUMMARY")
    print(f"{'═' * 70}")
    print(
        f"  {'Pair':<10} {'Base P&L':>12} {'ML P&L':>12} {'Delta':>12} {'Base WR':>10} {'ML WR':>10} {'Δ WR':>8} {'Status':<8}"  # noqa: E501
    )
    print(f"  {'─' * 85}")

    for row in summary_rows:
        status = "✅ BETTER" if row["improved"] else "❌ WORSE"
        if abs(row["delta"]) < 5:
            status = "➖ FLAT"
        print(
            f"  {row['pair']:<10} ${row['baseline_pnl']:>10.2f} ${row['ml_pnl']:>10.2f} "
            f"${row['delta']:>+10.2f} {row['baseline_wr']:>9.1%} {row['ml_wr']:>9.1%} "
            f"{row['wr_delta']:>+7.1%} {status}"
        )

    improved_count = sum(1 for r in summary_rows if r["improved"])
    print(f"\n  {improved_count}/{len(summary_rows)} pairs improved with ML adjustment")

    # Recommendations
    print(f"\n{'═' * 70}")
    print("  RECOMMENDATIONS")
    print(f"{'═' * 70}")

    if improved_count == 0:
        print("  • ML layer did not improve any pair. Consider:")
        print("    - Collecting more trade data (current min: 20, aim for 50+)")
        print("    - Tuning ML thresholds (high/low proba boundaries)")
        print("    - Adding more features (numeric: ATR, volatility, session timing)")
    elif improved_count <= len(summary_rows) // 2:
        print("  • ML improved some pairs but not most. Consider:")
        print("    - Per-pair threshold tuning")
        print("    - Investigating which features are noisy per pair")
    else:
        print("  • ML improved most pairs. Next steps:")
        print("    - Integrate into production signal pipeline")
        print("    - Walk-forward validation of ML layer")
        print("    - Monitor for overfitting with new data")

    print("  • All learned weights are saved in the report JSON for review.")


if __name__ == "__main__":
    main()
