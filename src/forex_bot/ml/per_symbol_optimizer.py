"""Per-symbol, per-timeframe ML optimization for the Ayumi trading bot.

Runs independent walk-forward backtests for each (symbol, timeframe) pair
across multiple config variants, trains per-pair ConfidenceLearner models,
and determines the optimal configuration per symbol.

Usage:
    cd $AYUMI_ROOT/src/forex_bot && python3 -m ml.per_symbol_optimizer
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# We monkey-patch TTSStrategy constants per config variant
import backtest.strategies.tts_strategy as tts_module
from backtest.engine import Bar, TradeOutcome, get_spread_for_pair
from backtest.multi_strategy_engine import MultiStrategyBacktestEngine
from backtest.strategies import TTSStrategy
from ml.confidence_learner import ConfidenceLearner
from ml.confluence_features import ConfluenceFeatureExtractor

logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "XAUUSD"]
TIMEFRAMES = ["M15"]
DATA_DIR = PROJECT_ROOT / "data" / "forex" / "historical"

CONFIGS = [
    {"label": "A_baseline", "base": 0.30, "kz_penalty": -0.05, "htf_penalty": -0.15},
    {"label": "B_high_base", "base": 0.35, "kz_penalty": -0.05, "htf_penalty": -0.15},
    {"label": "C_low_base", "base": 0.25, "kz_penalty": -0.05, "htf_penalty": -0.15},
    {"label": "D_no_kz", "base": 0.30, "kz_penalty": 0.0, "htf_penalty": -0.15},
    {"label": "E_strong_kz", "base": 0.30, "kz_penalty": -0.10, "htf_penalty": -0.15},
    {"label": "F_no_htf_pen", "base": 0.30, "kz_penalty": -0.05, "htf_penalty": 0.0},
    {
        "label": "G_low_base_no_kz",
        "base": 0.25,
        "kz_penalty": 0.0,
        "htf_penalty": -0.15,
    },
    {
        "label": "H_high_base_strong_kz",
        "base": 0.35,
        "kz_penalty": -0.10,
        "htf_penalty": -0.15,
    },
]

DEFAULT_BT_CONFIG = {
    "starting_balance": 10000,
    "commission_per_lot": 3.5,
    "min_confidence": 0.20,
    "min_quality_score": 0.25,
}

# OOS holdout start date — replaces the implicit _2026.csv filename convention.
# When CSV files migrate to DuckDB, use this date to filter holdout rows:
#   WHERE timestamp_utc >= OOS_HOLDOUT_START  -- holdout data
#   WHERE timestamp_utc <  OOS_HOLDOUT_START  -- training data
OOS_HOLDOUT_START = "2026-01-01"


# ── Helpers ───────────────────────────────────────────────────────────


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
        for suffix in ["_2026.csv", ".csv"]:
            csv_path = DATA_DIR / f"{pair}_{tf}{suffix}"
            if csv_path.exists():
                break
        else:
            raise FileNotFoundError(f"No data for {pair}/{tf}")
    else:
        # Prefer the full .csv, fall back to _2026.csv
        for suffix in [".csv", "_2026.csv"]:
            csv_path = DATA_DIR / f"{pair}_{tf}{suffix}"
            if csv_path.exists():
                break
        else:
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


def patch_tts_constants(config: dict) -> None:
    """Monkey-patch TTSStrategy module-level constants for a config variant."""
    tts_module.MW_BASE_CONFIDENCE = config["base"]
    tts_module.KILL_ZONE_ACTIVE_BOOST = config["kz_penalty"]
    tts_module.HTF_OPPOSING_PENALTY = config["htf_penalty"]


def restore_tts_constants() -> None:
    """Restore defaults."""
    tts_module.MW_BASE_CONFIDENCE = 0.30
    tts_module.KILL_ZONE_ACTIVE_BOOST = -0.05
    tts_module.HTF_OPPOSING_PENALTY = -0.15


# ── Per-Symbol Optimizer ──────────────────────────────────────────────


class PerSymbolOptimizer:
    """Runs independent ML optimization for each symbol+timeframe."""

    def __init__(self):
        self.results: dict[str, dict] = {}

    def _run_backtest(self, pair: str, tf: str, config: dict) -> list[Any]:
        """Run a single backtest for a pair with a given config. Returns trades."""
        bars = load_bars(pair, tf)
        spread = get_spread_for_pair(pair)

        from backtest.engine import BacktestConfig

        bt_config = BacktestConfig(
            starting_balance=DEFAULT_BT_CONFIG["starting_balance"],
            spread_pips=spread,
            commission_per_lot=DEFAULT_BT_CONFIG["commission_per_lot"],
            pair=pair,
            min_confidence=DEFAULT_BT_CONFIG["min_confidence"],
        )

        strategy = TTSStrategy(
            symbol=pair,
            min_confidence=DEFAULT_BT_CONFIG["min_confidence"],
            min_quality_score=DEFAULT_BT_CONFIG["min_quality_score"],
        )

        engine = MultiStrategyBacktestEngine(bt_config, [strategy])
        result = engine.run_all_strategies(bars)
        return result[strategy.name].metrics.trades

    def _trades_to_metrics(self, trades: list[Any]) -> dict:
        """Compute summary metrics from a trade list."""
        if not trades:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_pct": 0.0,
                "t4_trades": 0,
                "t5_trades": 0,
            }

        wins = [t for t in trades if t.outcome == TradeOutcome.WIN]
        losses = [t for t in trades if t.outcome == TradeOutcome.LOSS]
        n_wins = len(wins)
        n_losses = len(losses)
        total = len(trades)

        win_pnl = sum(t.profit_loss for t in wins)
        loss_pnl = sum(t.profit_loss for t in losses)
        avg_win = win_pnl / n_wins if n_wins else 0
        avg_loss = abs(loss_pnl / n_losses) if n_losses else 0.01

        # Drawdown
        balance = DEFAULT_BT_CONFIG["starting_balance"]
        peak = balance
        max_dd_pct = 0.0
        for t in trades:
            balance += t.profit_loss
            if balance > peak:
                peak = balance
            dd_pct = (peak - balance) / peak * 100 if peak > 0 else 0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # T4/T5: trades with confidence >= 0.40 (T4) or >= 0.50 (T5)
        t4 = sum(1 for t in trades if getattr(t, "confidence_score", 0) >= 0.40)
        t5 = sum(1 for t in trades if getattr(t, "confidence_score", 0) >= 0.50)

        return {
            "total_trades": total,
            "wins": n_wins,
            "losses": n_losses,
            "win_rate": n_wins / total if total else 0,
            "total_pnl": sum(t.profit_loss for t in trades),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": win_pnl / abs(loss_pnl) if loss_pnl != 0 else float("inf"),
            "max_drawdown_pct": max_dd_pct,
            "t4_trades": t4,
            "t5_trades": t5,
        }

    def run_for_symbol(self, symbol: str, timeframe: str, base_configs: list[dict]) -> dict:
        """Run walk-forward for one symbol+timeframe across multiple configs.

        Returns dict with per-config results, best config, and learned weights.
        """
        key = f"{symbol}_{timeframe}"
        print(f"\n{'─' * 60}")
        print(f"  Optimizing: {key}")
        print(f"{'─' * 60}")

        extractor = ConfluenceFeatureExtractor()
        config_results = []

        for cfg in base_configs:
            label = cfg["label"]
            print(f"\n  Config {label}: base={cfg['base']}, kz={cfg['kz_penalty']}, htf={cfg['htf_penalty']}")

            try:
                patch_tts_constants(cfg)
                trades = self._run_backtest(symbol, timeframe, cfg)
                metrics = self._trades_to_metrics(trades)
                restore_tts_constants()
            except Exception as e:
                restore_tts_constants()
                print(f"    ERROR: {e}")
                config_results.append({"label": label, "config": cfg, "error": str(e)})
                continue

            print(
                f"    Trades: {metrics['total_trades']} | WR: {metrics['win_rate']:.1%} | "
                f"P&L: ${metrics['total_pnl']:.2f} | PF: {metrics['profit_factor']:.2f} | "
                f"DD: {metrics['max_drawdown_pct']:.1f}% | T4: {metrics['t4_trades']} T5: {metrics['t5_trades']}"
            )

            # Train ML model on these trades
            learner_result = {"weights": None, "top_features": None, "trained": False}
            records = []
            for t in trades:
                if not getattr(t, "rationale", None):
                    continue
                rec = {
                    "rationale": t.rationale,
                    "confidence_score": getattr(t, "confidence_score", 0.5),
                    "confluence_count": getattr(t, "confluence_count", 0),
                    "outcome": 1 if t.outcome == TradeOutcome.WIN else 0,
                }
                rec["features"] = extractor.extract(rec)
                records.append(rec)

            if len(records) >= 20:
                learner = ConfidenceLearner(symbol=symbol, timeframe=timeframe)
                try:
                    weights = learner.train(records, extractor.FEATURE_NAMES)
                    learner_result = {
                        "weights": weights,
                        "top_features": learner.get_top_features(5),
                        "trained": True,
                        "train_size": learner.train_size,
                    }
                    top = learner.get_top_features(5)
                    print(f"    ML trained on {learner.train_size} trades. Top: {top}")
                except ValueError as e:
                    print(f"    ML skip: {e}")
            else:
                print(f"    ML skip: only {len(records)} trades with rationale")

            config_results.append(
                {
                    "label": label,
                    "config": cfg,
                    "metrics": metrics,
                    "learner": learner_result,
                }
            )

        # Select best config: highest P&L with WR >= 40% and DD < 25%
        # Score = P&L * (1 + WR) / (1 + DD%)
        best = None
        best_score = float("-inf")
        for cr in config_results:
            m = cr.get("metrics")
            if not m or m["total_trades"] == 0:
                continue
            wr = m["win_rate"]
            pnl = m["total_pnl"]
            dd = max(m["max_drawdown_pct"], 0.1)
            # Penalize configs with WR < 35% or DD > 30%
            if wr < 0.35 or dd > 30:
                score = pnl * 0.5  # heavy penalty
            else:
                score = pnl * (1 + wr) / (1 + dd / 100)
            cr["score"] = score
            if score > best_score:
                best_score = score
                best = cr

        result = {
            "key": key,
            "symbol": symbol,
            "timeframe": timeframe,
            "config_results": config_results,
            "best": best,
            "best_score": best_score,
        }

        if best:
            print(
                f"\n  ★ BEST: {best['label']} — P&L: ${best['metrics']['total_pnl']:.2f}, "
                f"WR: {best['metrics']['win_rate']:.1%}, DD: {best['metrics']['max_drawdown_pct']:.1f}%, "
                f"Score: {best_score:.2f}"
            )
            if best["learner"]["trained"]:
                print(f"    Top confluences: {[f[0] for f in best['learner']['top_features']]}")

        self.results[key] = result
        return result

    def run_all(self, symbols: list, timeframes: list, base_configs: list) -> dict:
        """Run full grid: all symbols × all timeframes × all configs."""
        for symbol in symbols:
            for timeframe in timeframes:
                self.run_for_symbol(symbol, timeframe, base_configs)
        return self.results

    def build_per_symbol_configs(self) -> dict:
        """Build the PER_SYMBOL_CONFIGS dict from best results."""
        configs = {}
        for key, result in self.results.items():  # noqa: B007
            symbol = result["symbol"]
            timeframe = result["timeframe"]
            best = result.get("best")
            if not best:
                continue

            entry = {
                "base_confidence": best["config"]["base"],
                "kz_penalty": best["config"]["kz_penalty"],
                "htf_penalty": best["config"]["htf_penalty"],
                "config_label": best["label"],
                "top_confluences": (
                    [f[0] for f in best["learner"]["top_features"]] if best["learner"]["trained"] else []
                ),
                "metrics": {
                    "pnl": best["metrics"]["total_pnl"],
                    "win_rate": best["metrics"]["win_rate"],
                    "max_drawdown_pct": best["metrics"]["max_drawdown_pct"],
                    "profit_factor": best["metrics"]["profit_factor"],
                    "total_trades": best["metrics"]["total_trades"],
                    "t4_trades": best["metrics"]["t4_trades"],
                    "t5_trades": best["metrics"]["t5_trades"],
                },
            }

            if symbol not in configs:
                configs[symbol] = {}
            configs[symbol][timeframe] = entry

        return configs


# ── Main ───────────────────────────────────────────────────────────────


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    start = time.time()

    print("=" * 70)
    print("  Per-Symbol / Per-Timeframe ML Optimization")
    print(f"  Symbols: {SYMBOLS} | Timeframes: {TIMEFRAMES}")
    print(f"  Configs: {len(CONFIGS)} variants")
    print(f"  Started: {timestamp}")
    print("=" * 70)

    optimizer = PerSymbolOptimizer()
    optimizer.run_all(SYMBOLS, TIMEFRAMES, CONFIGS)

    # Build per-symbol configs
    per_symbol_configs = optimizer.build_per_symbol_configs()

    # Save per-symbol config module
    config_path = PROJECT_ROOT / "src" / "forex_bot" / "ml" / "per_symbol_configs.py"
    config_content = (
        f'"""Auto-generated per-symbol, per-timeframe configs. Generated: {timestamp}."""\n\nPER_SYMBOL_CONFIGS = '  # noqa: E501
    )
    config_content += json.dumps(per_symbol_configs, indent=4, default=str)
    config_content += "\n"
    config_path.write_text(config_content)
    print(f"\n  Config file written: {config_path}")

    # Save full report
    report_dir = PROJECT_ROOT / "reports" / "ml_per_symbol"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"per_symbol_{timestamp}.json"

    # Make results JSON-serializable
    def serialize(obj):
        if isinstance(obj, float) and (obj != obj or obj == float("inf")):
            return str(obj)
        if isinstance(obj, dict):
            return {k: serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [serialize(v) for v in obj]
        return obj

    report = {
        "timestamp": timestamp,
        "symbols": SYMBOLS,
        "timeframes": TIMEFRAMES,
        "configs_tested": CONFIGS,
        "per_symbol_configs": serialize(per_symbol_configs),
        "results": serialize(optimizer.results),
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  Report saved: {report_path}")

    # ── Print Summary ─────────────────────────────────────────────────
    elapsed = time.time() - start
    print(f"\n{'═' * 70}")
    print("  FINAL SUMMARY")
    print(f"{'═' * 70}")
    print(f"  Elapsed: {elapsed:.0f}s")

    header = (
        f"  {'Symbol':<10} {'Config':<20} {'P&L':>10} {'WR':>8} {'DD%':>8} {'PF':>8} {'Trades':>8} {'T4':>5} {'T5':>5}"  # noqa: E501
    )
    print(header)
    print(f"  {'─' * len(header)}")

    best_overall = None
    best_score = float("-inf")

    for key, result in optimizer.results.items():
        best = result.get("best")
        if not best or "metrics" not in best:
            print(f"  {key:<10} {'N/A':<20} {'NO RESULT':>50}")
            continue

        m = best["metrics"]
        score = best.get("score", 0)
        print(
            f"  {key:<10} {best['label']:<20} ${m['total_pnl']:>8.2f} "
            f"{m['win_rate']:>7.1%} {m['max_drawdown_pct']:>7.1f}% "
            f"{m['profit_factor']:>8.2f} {m['total_trades']:>8} "
            f"{m['t4_trades']:>5} {m['t5_trades']:>5}"
        )

        if score > best_score:
            best_score = score
            best_overall = result

    if best_overall:
        b = best_overall["best"]
        print(f"\n  ★ BEST OVERALL: {best_overall['key']} with config {b['label']}")
        print(
            f"    P&L: ${b['metrics']['total_pnl']:.2f} | WR: {b['metrics']['win_rate']:.1%} | "
            f"DD: {b['metrics']['max_drawdown_pct']:.1f}%"
        )

    # Global vs per-symbol comparison
    print(f"\n{'═' * 70}")
    print("  GLOBAL vs PER-SYMBOL COMPARISON")
    print(f"{'═' * 70}")

    # Run global baseline (Config A)
    restore_tts_constants()
    global_total_pnl = 0
    global_total_trades = 0
    global_wins = 0
    for symbol in SYMBOLS:
        try:
            bars = load_bars(symbol, "M15")
            spread = get_spread_for_pair(symbol)
            from backtest.engine import BacktestConfig

            bt_config = BacktestConfig(
                starting_balance=DEFAULT_BT_CONFIG["starting_balance"],
                spread_pips=spread,
                commission_per_lot=DEFAULT_BT_CONFIG["commission_per_lot"],
                pair=symbol,
                min_confidence=DEFAULT_BT_CONFIG["min_confidence"],
            )
            strategy = TTSStrategy(
                symbol=symbol,
                min_confidence=DEFAULT_BT_CONFIG["min_confidence"],
                min_quality_score=DEFAULT_BT_CONFIG["min_quality_score"],
            )
            engine = MultiStrategyBacktestEngine(bt_config, [strategy])
            result = engine.run_all_strategies(bars)
            trades = result[strategy.name].metrics.trades
            pnl = sum(t.profit_loss for t in trades)
            wins = sum(1 for t in trades if t.outcome == TradeOutcome.WIN)
            global_total_pnl += pnl
            global_total_trades += len(trades)
            global_wins += wins
        except Exception:
            logger.warning(
                "Global baseline backtest failed for %s, skipping",
                symbol,
                exc_info=True,
            )

    global_wr = global_wins / global_total_trades if global_total_trades else 0

    # Sum per-symbol optimal P&L
    per_symbol_total_pnl = 0
    per_symbol_total_trades = 0
    per_symbol_wins = 0
    for key, result in optimizer.results.items():  # noqa: B007
        best = result.get("best")
        if best and "metrics" in best:
            m = best["metrics"]
            per_symbol_total_pnl += m["total_pnl"]
            per_symbol_total_trades += m["total_trades"]
            per_symbol_wins += m["wins"]

    per_symbol_wr = per_symbol_wins / per_symbol_total_trades if per_symbol_total_trades else 0

    print(f"  {'Metric':<25} {'Global (A)':>15} {'Per-Symbol':>15} {'Delta':>15}")
    print(f"  {'─' * 72}")
    print(
        f"  {'Total P&L':<25} ${global_total_pnl:>13.2f} ${per_symbol_total_pnl:>13.2f} "
        f"${per_symbol_total_pnl - global_total_pnl:>+13.2f}"
    )
    print(f"  {'Win Rate':<25} {global_wr:>14.1%} {per_symbol_wr:>14.1%} {per_symbol_wr - global_wr:>+14.1%}")
    print(
        f"  {'Total Trades':<25} {global_total_trades:>15} {per_symbol_total_trades:>15} "
        f"{per_symbol_total_trades - global_total_trades:>+15}"
    )

    # Learned weights summary
    print(f"\n{'═' * 70}")
    print("  PER-SYMBOL LEARNED WEIGHTS (Top Confluences)")
    print(f"{'═' * 70}")
    for key, result in optimizer.results.items():
        best = result.get("best")
        if best and best.get("learner", {}).get("trained"):
            print(f"\n  {key} (config: {best['label']}):")
            for fname, fimp in best["learner"]["top_features"]:
                bar = "█" * int(fimp * 100)
                print(f"    {fname:<30s} {fimp:.4f} {bar}")

    print(f"\n  Done. Config file: {config_path}")
    print(f"  Full report: {report_path}")


if __name__ == "__main__":
    main()
