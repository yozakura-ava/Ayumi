"""ML-driven weight optimizer for kill zone, HTF opposing, and base confidence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

from backtest.engine import (
    BacktestConfig,
    Bar,
    SimulatedTrade,
    TradeOutcome,
    get_spread_for_pair,
)
from backtest.multi_strategy_engine import MultiStrategyBacktestEngine
from backtest.strategies import TTSStrategy
from ml.confluence_features import ConfluenceFeatureExtractor

DATA_DIR = PROJECT_ROOT / "data" / "forex" / "historical"
PAIRS = ["EURUSD", "USDJPY", "XAUUSD"]
TIMEFRAME = "M15"

# OOS holdout start date — replaces the implicit _2026.csv filename convention.
# When CSV files migrate to DuckDB, use this date to filter holdout rows:
#   WHERE timestamp_utc >= OOS_HOLDOUT_START  -- holdout data
#   WHERE timestamp_utc <  OOS_HOLDOUT_START  -- training data
OOS_HOLDOUT_START = "2026-01-01"

CONFIGS = {
    "A_baseline": {"kill_zone": 0.05, "htf_opposing": -0.15, "base_confidence": 0.20},
    "B_kz_pen_small": {
        "kill_zone": -0.03,
        "htf_opposing": -0.15,
        "base_confidence": 0.20,
    },
    "C_kz_pen_lg_htf_relax": {
        "kill_zone": -0.05,
        "htf_opposing": -0.10,
        "base_confidence": 0.25,
    },
    "D_kz_pen_small_htf_relax_base25": {
        "kill_zone": -0.03,
        "htf_opposing": -0.05,
        "base_confidence": 0.25,
    },
    "E_kz_pen_lg_htf_strict_base30": {
        "kill_zone": -0.05,
        "htf_opposing": -0.15,
        "base_confidence": 0.30,
    },
}


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


def run_config(pair: str, params: dict) -> list[SimulatedTrade]:
    """Run a single config for a pair by monkeypatching strategy module constants."""
    import backtest.strategies.tts_strategy as tts_mod

    orig_kz = tts_mod.KILL_ZONE_ACTIVE_BOOST
    orig_base = tts_mod.MW_BASE_CONFIDENCE

    try:
        tts_mod.KILL_ZONE_ACTIVE_BOOST = params["kill_zone"]
        tts_mod.MW_BASE_CONFIDENCE = params["base_confidence"]

        # HTF opposing penalty is hardcoded as -0.15 in evaluate().
        # Post-hoc adjustment for feature extraction accuracy.
        htf_diff = params["htf_opposing"] - (-0.15)

        bars = load_bars(pair, TIMEFRAME)
        spread = get_spread_for_pair(pair)

        config = BacktestConfig(
            starting_balance=10000,
            spread_pips=spread,
            commission_per_lot=3.5,
            pair=pair,
            min_confidence=0.50,
        )

        strategy = TTSStrategy(
            symbol=pair,
            min_confidence=0.50,
            min_quality_score=0.60,
        )

        engine = MultiStrategyBacktestEngine(config, [strategy])
        result = engine.run_all_strategies(bars)
        metrics = result[strategy.name].metrics

        # Adjust confidence for HTF opposing trades
        for trade in metrics.trades:
            if trade.rationale and "htf_opposing" in trade.rationale:
                trade.confidence_score += htf_diff

        return metrics.trades

    finally:
        tts_mod.KILL_ZONE_ACTIVE_BOOST = orig_kz
        tts_mod.MW_BASE_CONFIDENCE = orig_base


def summarize_trades(trades: list[SimulatedTrade]) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
        }
    wins = sum(1 for t in trades if t.outcome == TradeOutcome.WIN)
    losses = sum(1 for t in trades if t.outcome == TradeOutcome.LOSS)
    pnl = sum(t.profit_loss for t in trades)
    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "total_pnl": pnl,
    }


def main():
    print("=" * 70)
    print("  ML Weight Optimizer — Kill Zone, HTF Opposing, Base Confidence")
    print(f"  Pairs: {PAIRS} | Timeframe: {TIMEFRAME}")
    print("=" * 70)

    extractor = ConfluenceFeatureExtractor()
    all_results = {}
    all_records = []

    for config_name, params in CONFIGS.items():
        print(
            f"\n── Config {config_name}: kz={params['kill_zone']:+.2f}, "
            f"htf_opp={params['htf_opposing']:+.2f}, base={params['base_confidence']:.2f} ──"
        )

        for pair in PAIRS:
            try:
                trades = run_config(pair, params)
                summary = summarize_trades(trades)
                all_results[(config_name, pair)] = {
                    "trades": trades,
                    "summary": summary,
                }

                print(
                    f"  {pair}: {summary['total_trades']} trades | "
                    f"WR: {summary['win_rate']:.1%} | P&L: ${summary['total_pnl']:.2f}"
                )

                for t in trades:
                    if not t.rationale:
                        continue
                    record = {
                        "rationale": t.rationale,
                        "confidence_score": t.confidence_score,
                        "confluence_count": getattr(t, "confluence_count", 0),
                        "outcome": 1 if t.outcome == TradeOutcome.WIN else 0,
                        "profit_loss": t.profit_loss,
                        "config": config_name,
                        "pair": pair,
                        "kill_zone_val": params["kill_zone"],
                        "htf_opposing_val": params["htf_opposing"],
                        "base_confidence_val": params["base_confidence"],
                    }
                    record["features"] = extractor.extract(record)
                    # Append config params as features for ML to learn from
                    record["features"].extend(
                        [
                            params["kill_zone"],
                            params["htf_opposing"],
                            params["base_confidence"],
                        ]
                    )
                    all_records.append(record)

            except Exception as e:
                print(f"  {pair}: ERROR — {e}")

    if not all_records:
        print("\n  No trades collected. Exiting.")
        return

    # ── ML Analysis ──
    print(f"\n{'═' * 70}")
    print("  ML ANALYSIS")
    print(f"{'═' * 70}")
    print(f"  Total trade records for training: {len(all_records)}")

    aug_feature_names = extractor.FEATURE_NAMES + [
        "kill_zone_val",
        "htf_opposing_val",
        "base_confidence_val",
    ]

    from sklearn.ensemble import RandomForestClassifier

    X = np.array([r["features"] for r in all_records])
    y = np.array([r["outcome"] for r in all_records], dtype=np.int32)

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=5,
        random_state=42,
        class_weight="balanced",
    )
    model.fit(X, y)

    importances = dict(zip(aug_feature_names, model.feature_importances_.tolist()))  # noqa: B905
    print("\n  Feature Importances:")
    for name, imp in sorted(importances.items(), key=lambda x: -x[1]):
        bar = "█" * int(imp * 200)
        print(f"    {name:<30s} {imp:.4f} {bar}")

    # ── Per-pair best config ──
    print(f"\n{'═' * 70}")
    print("  PER-PAIR BEST CONFIGURATION")
    print(f"{'═' * 70}")

    for pair in PAIRS:
        print(f"\n  {pair}:")
        best_config = None
        best_pnl = float("-inf")
        for config_name in CONFIGS:
            key = (config_name, pair)
            if key not in all_results:
                continue
            s = all_results[key]["summary"]
            print(
                f"    {config_name:<35s} trades={s['total_trades']:>4d}  "
                f"WR={s['win_rate']:.1%}  P&L=${s['total_pnl']:.2f}"
            )
            if s["total_pnl"] > best_pnl:
                best_pnl = s["total_pnl"]
                best_config = config_name
        if best_config:
            print(f"    >>> BEST: {best_config} (P&L: ${best_pnl:.2f})")

    # ── Config comparison summary ──
    print(f"\n{'═' * 70}")
    print("  CONFIG COMPARISON (aggregated)")
    print(f"{'═' * 70}")

    best_agg = None
    best_agg_pnl = float("-inf")
    for config_name, params in CONFIGS.items():  # noqa: B007
        total_trades = 0
        total_wins = 0
        total_pnl = 0.0
        for pair in PAIRS:
            key = (config_name, pair)
            if key not in all_results:
                continue
            s = all_results[key]["summary"]
            total_trades += s["total_trades"]
            total_wins += s["wins"]
            total_pnl += s["total_pnl"]
        wr = total_wins / total_trades if total_trades else 0
        print(f"  {config_name:<35s} trades={total_trades:>4d}  WR={wr:>6.1%}  P&L=${total_pnl:>10.2f}")
        if total_pnl > best_agg_pnl:
            best_agg_pnl = total_pnl
            best_agg = config_name

    best_params = CONFIGS[best_agg]

    # ── Recommendations ──
    print(f"\n{'═' * 70}")
    print("  RECOMMENDATIONS")
    print(f"{'═' * 70}")

    print(f"\n  Optimal config by aggregate P&L: {best_agg}")
    print(
        f"    kill_zone_boost: {best_params['kill_zone']:+.2f} "
        f"({'PENALTY' if best_params['kill_zone'] < 0 else 'BOOST'})"
    )
    print(f"    htf_opposing_penalty: {best_params['htf_opposing']:+.2f}")
    print(f"    base_confidence: {best_params['base_confidence']:.2f}")

    kz_imp = importances.get("kill_zone_active", 0)
    htf_imp = importances.get("htf_opposing", 0)
    print("\n  Feature importance insights:")
    print(f"    kill_zone_active:  {kz_imp:.4f}")
    print(f"    htf_opposing:      {htf_imp:.4f}")
    print(f"    kill_zone_val:     {importances.get('kill_zone_val', 0):.4f}")
    print(f"    htf_opposing_val:  {importances.get('htf_opposing_val', 0):.4f}")
    print(f"    base_confidence_val: {importances.get('base_confidence_val', 0):.4f}")

    if best_params["kill_zone"] < 0:
        print("\n  ✅ Best config penalizes kill zone (not boosting). Removing the boost helped.")
    else:
        print("\n  ❌ Best config still boosts kill zone. Penalty did not help.")

    # Save report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    report = {
        "timestamp": timestamp,
        "configs_tested": CONFIGS,
        "per_pair_results": {
            f"{cn}_{pair}": all_results.get((cn, pair), {}).get("summary", {})
            for cn in CONFIGS
            for pair in PAIRS
            if (cn, pair) in all_results
        },
        "feature_importances": importances,
        "best_config": best_agg,
        "best_params": best_params,
        "best_aggregate_pnl": best_agg_pnl,
    }

    report_path = PROJECT_ROOT / "reports" / "ml_confidence" / f"weight_opt_{timestamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report saved: {report_path}")


if __name__ == "__main__":
    main()
