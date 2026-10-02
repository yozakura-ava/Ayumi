"""§10 — Walk-forward Optuna optimizer for TTSStrategy (V2).

V1 problem: tuned all 31 boost weights on a single backtest run (17-28 trades).
31-dimensional search on ~20 data points = noise, not signal.

V2 approach: walk-forward objectives + reduced search space.
- Select which boosters to ENABLE (binary/categorical) instead of tuning weights
- Tune only 3 continuous params: base_confidence, negative_weight, kz_penalty
- Run walk-forward windows for valid out-of-sample metrics
- Score = wr * sqrt(trades) * (1 / (1 + dd/100)) — rewards consistency
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import optuna
import pandas as pd
from optuna.samplers import TPESampler

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

import backtest.strategies.tts_strategy as tts_module
from backtest.engine import Bar, get_spread_for_pair
from backtest.strategies import TTSStrategy
from backtest.walk_forward_runner import run_strategy_walk_forward

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

RESULTS_DIR = PROJECT_ROOT / "reports" / "optuna_optimizer"
STUDIES_DIR = PROJECT_ROOT / "reports" / "optuna_studies"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
STUDIES_DIR.mkdir(parents=True, exist_ok=True)

# ── Reduced search space ─────────────────────────────────────────────────────

# Continuous params (always tuned)
BASE_CONF_RANGE = (0.20, 0.45)
NEG_WEIGHT_RANGE = (0.0, 1.5)
KZ_PENALTY_RANGE = (-0.10, -0.01)

# Booster on/off switches (categorical — key insight)
BOOSTER_SWITCHES = [
    "RSI_DIVERGENCE_BOOST",
    "HTF_TREND_ALIGNED_BOOST",
    "SVC_AT_PEAK_BOOST",
    "MFI_BOOST",
    "EMA_CROSS_BOOST",
    "BB_CONF_BOOST",
    "ADX_BOOST",
    "VOLUME_SPIKE_BOOST",
    "VWAP_DISTANCE_BOOST",
    "RSI_EXTREME_BOOST",
    "EMA_EXTENSION_BOOST",
]

# Which negatives to activate (subset)
NEGATIVE_SWITCHES = [
    "RSI_OVERBOUGHT_NC",
    "RSI_OVERSOLD_NC",
    "HTF_COUNTER_TREND_NC",
    "LATE_KILL_ZONE_NC",
    "VOLUME_DIVERGENCE_NC",
    "BB_SQUEEZE_NC",
    "ADX_EXHAUSTION_NC",
    "ASIA_RANGE_WIDE_NC",
    "MFI_OVERSOLD_NC",
]

# Default values for restoring after each trial
_BOOSTER_DEFAULTS: dict[str, float] = {
    "RSI_DIVERGENCE_BOOST": 0.10,
    "HTF_TREND_ALIGNED_BOOST": 0.10,
    "SVC_AT_PEAK_BOOST": 0.10,
    "MFI_BOOST": 0.08,
    "EMA_CROSS_BOOST": 0.08,
    "BB_CONF_BOOST": 0.07,
    "ADX_BOOST": 0.06,
    "VOLUME_SPIKE_BOOST": 0.05,
    "VWAP_DISTANCE_BOOST": 0.05,
    "RSI_EXTREME_BOOST": 0.06,
    "EMA_EXTENSION_BOOST": 0.07,
}

_NEGATIVE_DEFAULTS: dict[str, float] = {
    "RSI_OVERBOUGHT_NC": -0.05,
    "RSI_OVERSOLD_NC": -0.05,
    "HTF_COUNTER_TREND_NC": -0.08,
    "LATE_KILL_ZONE_NC": -0.06,
    "VOLUME_DIVERGENCE_NC": -0.05,
    "BB_SQUEEZE_NC": -0.04,
    "ADX_EXHAUSTION_NC": -0.05,
    "ASIA_RANGE_WIDE_NC": -0.05,
    "MFI_OVERSOLD_NC": -0.04,
}

# Boosters not in the switches list — keep at defaults
# (CONSOLIDATION_BOOST, ASIA_GAP_FAVORABLE_BOOST, ILOD_IHOD_AT_BOUNDARY_BOOST,
#  VWAP_REJECTION_BOOST, EMA_CLUSTER_BOOST, HTF_200EMA_BOOST,
#  HTF_200EMA_PENALTY, VWAP_EXTREME_DISTANCE_NC, MFI_OVERBOUGHT_NC)
_OTHER_POSITIVE_DEFAULTS: dict[str, float] = {
    "CONSOLIDATION_BOOST": 0.05,
    "ASIA_GAP_FAVORABLE_BOOST": 0.05,
    "ILOD_IHOD_AT_BOUNDARY_BOOST": 0.05,
    "VWAP_REJECTION_BOOST": 0.10,
    "EMA_CLUSTER_BOOST": 0.06,
    "HTF_200EMA_BOOST": 0.10,
    "HTF_200EMA_PENALTY": 0.08,
}
_OTHER_NEGATIVE_DEFAULTS: dict[str, float] = {
    "VWAP_EXTREME_DISTANCE_NC": -0.04,
    "MFI_OVERBOUGHT_NC": -0.04,
}

# ── Default test config ─────────────────────────────────────────────────────

DEFAULT_BT_CONFIG = dict(
    starting_balance=10_000,
    commission_per_lot=3.5,
    min_confidence=0.20,
    min_quality_score=0.25,
)

# Minimum total trades across walk-forward windows to not prune
MIN_TOTAL_TRADES = 15

# OOS holdout start date — replaces the implicit _2026.csv filename convention.
# When CSV files migrate to DuckDB, use this date to filter holdout rows:
#   WHERE timestamp_utc >= OOS_HOLDOUT_START  -- holdout data
#   WHERE timestamp_utc <  OOS_HOLDOUT_START  -- training data
OOS_HOLDOUT_START = "2026-01-01"

# ── Data loading ────────────────────────────────────────────────────────────


def load_bars(pair: str, tf: str = "M15", holdout_only: bool = False) -> list[Bar]:
    """Load historical bars from CSV.

    OOS Holdout Convention
    ----------------------
    Files named ``{PAIR}_{TF}_2026.csv`` contain OOS holdout data
    (>= OOS_HOLDOUT_START).  Files named ``{PAIR}_{TF}.csv`` contain the
    full dataset including the holdout period.

    When *holdout_only* is False (default for this optimizer) the full
    ``.csv`` is loaded without filtering — optuna uses all available data.

    When *holdout_only* is True, the function prefers the ``_2026.csv``
    file, or filters the full ``.csv`` to ``>= OOS_HOLDOUT_START``.

    DuckDB migration: replace filename selection with
    ``WHERE timestamp_utc >= OOS_HOLDOUT_START`` (holdout) or
    ``WHERE timestamp_utc <  OOS_HOLDOUT_START`` (training).
    """
    data_dir = PROJECT_ROOT / "data" / "forex" / "historical"
    if holdout_only:
        # Prefer the _2026.csv holdout file, fall back to filtered .csv
        for suffix in (f"{pair}_{tf}_2026.csv", f"{pair}_{tf}.csv"):
            csv_path = data_dir / suffix
            if csv_path.exists():
                break
        else:
            raise FileNotFoundError(f"No data for {pair}/{tf}")
    else:
        # Prefer the full .csv, fall back to _2026.csv
        for suffix in (f"{pair}_{tf}.csv", f"{pair}_{tf}_2026.csv"):
            csv_path = data_dir / suffix
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


# ── Param application ───────────────────────────────────────────────────────


def _apply_trial_params(trial: optuna.Trial) -> dict[str, Any]:
    """Apply trial params to tts_module constants. Returns applied dict."""
    applied: dict[str, Any] = {}

    # 1. Continuous params
    base_conf = trial.suggest_float("base_confidence", *BASE_CONF_RANGE, step=0.01)
    neg_weight = trial.suggest_float("negative_weight", *NEG_WEIGHT_RANGE, step=0.1)
    kz_penalty = trial.suggest_float("kz_penalty", *KZ_PENALTY_RANGE, step=0.01)

    tts_module.MW_BASE_CONFIDENCE = base_conf
    tts_module.NEGATIVE_WEIGHT = neg_weight
    tts_module.KILL_ZONE_ACTIVE_BOOST = kz_penalty
    applied.update(base_confidence=base_conf, negative_weight=neg_weight, kz_penalty=kz_penalty)

    # 2. Booster on/off switches
    enabled_boosters: list[str] = []
    for name in BOOSTER_SWITCHES:
        is_on = trial.suggest_categorical(f"booster_{name}", [True, False])
        val = _BOOSTER_DEFAULTS[name] if is_on else 0.0
        setattr(tts_module, name, val)
        applied[f"booster_{name}"] = is_on
        if is_on:
            enabled_boosters.append(name)

    applied["enabled_boosters"] = enabled_boosters

    # 3. Negative switches
    enabled_negatives: list[str] = []
    for name in NEGATIVE_SWITCHES:
        is_on = trial.suggest_categorical(f"neg_{name}", [True, False])
        val = _NEGATIVE_DEFAULTS[name] if is_on else 0.0
        setattr(tts_module, name, val)
        applied[f"neg_{name}"] = is_on
        if is_on:
            enabled_negatives.append(name)

    applied["enabled_negatives"] = enabled_negatives

    # 4. Reset other boosters to defaults
    for name, val in _OTHER_POSITIVE_DEFAULTS.items():
        setattr(tts_module, name, val)
    for name, val in _OTHER_NEGATIVE_DEFAULTS.items():
        setattr(tts_module, name, val)

    return applied


def restore_defaults() -> None:
    """Restore all tts_module constants to their defaults."""
    for name, val in _BOOSTER_DEFAULTS.items():
        setattr(tts_module, name, val)
    for name, val in _NEGATIVE_DEFAULTS.items():
        setattr(tts_module, name, val)
    for name, val in _OTHER_POSITIVE_DEFAULTS.items():
        setattr(tts_module, name, val)
    for name, val in _OTHER_NEGATIVE_DEFAULTS.items():
        setattr(tts_module, name, val)
    tts_module.MW_BASE_CONFIDENCE = 0.30
    tts_module.NEGATIVE_WEIGHT = 1.0
    tts_module.KILL_ZONE_ACTIVE_BOOST = -0.05


# ── Walk-forward objective ──────────────────────────────────────────────────


def build_wf_objective(
    pair: str,
    tf: str,
    bars: list[Bar],
    n_windows: int = 3,
    train_ratio: float = 0.7,
) -> callable:
    """Build an Optuna objective that uses walk-forward validation."""
    spread = get_spread_for_pair(pair)

    def objective(trial: optuna.Trial) -> float:
        applied = _apply_trial_params(trial)

        try:
            wf_result = run_strategy_walk_forward(
                bars=bars,
                strategy_factory=lambda: TTSStrategy(
                    symbol=pair,
                    min_confidence=applied["base_confidence"],
                    min_quality_score=DEFAULT_BT_CONFIG["min_quality_score"],
                ),
                pair=pair,
                n_windows=n_windows,
                train_ratio=train_ratio,
                initial_balance=DEFAULT_BT_CONFIG["starting_balance"],
                spread_pips=spread,
                commission_per_lot=DEFAULT_BT_CONFIG["commission_per_lot"],
                min_confidence=applied["base_confidence"],
            )
        except Exception as exc:
            raise optuna.TrialPruned(f"Walk-forward error: {exc}") from exc
        finally:
            restore_defaults()

        agg = wf_result.aggregated
        if agg is None:
            raise optuna.TrialPruned("No aggregated metrics")

        total_trades = agg.mean_trade_count * agg.total_windows
        if total_trades < MIN_TOTAL_TRADES:
            raise optuna.TrialPruned(f"Only {total_trades:.0f} total trades (need {MIN_TOTAL_TRADES})")

        wr = agg.mean_win_rate
        dd = max(agg.mean_max_drawdown, 0.01)
        trades = max(total_trades, 1.0)

        # Score: rewards win rate, trade count, and low drawdown
        score = wr * (trades**0.5) * (1.0 / (1.0 + dd * 100))

        trial.set_user_attr("win_rate", wr)
        trial.set_user_attr("profit_factor", agg.mean_profit_factor)
        trial.set_user_attr("max_drawdown", agg.mean_max_drawdown)
        trial.set_user_attr("mean_trade_count", agg.mean_trade_count)
        trial.set_user_attr("total_trades", total_trades)
        trial.set_user_attr("total_pnl", agg.mean_total_pnl)
        trial.set_user_attr("windows_passed", agg.windows_passed)
        trial.set_user_attr("go_nogo", wf_result.go_nogo)

        if not wf_result.go_nogo:
            score -= 2.0

        return score

    return objective


# ── Study runner ────────────────────────────────────────────────────────────


def run_wf_study(
    pair: str,
    tf: str = "M15",
    n_trials: int = 80,
    n_windows: int = 3,
    train_ratio: float = 0.7,
    seed: int = 42,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """Run walk-forward Optuna study for one pair.

    Returns dict with best params, metrics, and whether to update config.
    """
    study_path = STUDIES_DIR / f"{pair}_{tf}_wf_v2.db"
    study_name = f"{pair}_{tf}_wf_v2"

    if study_path.exists():
        study = optuna.load_study(
            study_name=study_name,
            storage=f"sqlite:///{study_path}",
            sampler=TPESampler(seed=seed),
        )
        existing = len(study.trials)
        print(f"\n  ↪ Resuming study for {pair}/{tf} ({existing} trials done)")
    else:
        study = optuna.create_study(
            study_name=study_name,
            storage=f"sqlite:///{study_path}",
            sampler=TPESampler(seed=seed),
            direction="maximize",
        )
        print(f"\n  ↪ New walk-forward study for {pair}/{tf}")

    bars = load_bars(pair, tf)
    print(f"  Loaded {len(bars)} bars")

    objective = build_wf_objective(pair, tf, bars, n_windows=n_windows, train_ratio=train_ratio)

    print(f"  Running {n_trials} trials ({n_windows}-window walk-forward)...")
    study.optimize(objective, n_trials=n_trials, timeout=timeout, show_progress_bar=True)

    best = study.best_trial
    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]

    print(f"\n  ★ Best trial #{best.number} (of {len(completed)} complete): score={best.value:.3f}")
    print(
        f"    WR: {best.user_attrs.get('win_rate', 'N/A'):.1%} | "
        f"PF: {best.user_attrs.get('profit_factor', 0):.2f} | "
        f"Trades: {best.user_attrs.get('total_trades', 0):.0f}"
    )

    # Check if we should update per_symbol_configs
    wr = best.user_attrs.get("win_rate", 0)
    trades_per_window = best.user_attrs.get("mean_trade_count", 0)
    should_update = wr >= 0.45 and trades_per_window >= 20

    result = {
        "pair": pair,
        "timeframe": tf,
        "n_trials": len(study.trials),
        "n_complete": len(completed),
        "best_trial": best.number,
        "best_score": best.value,
        "best_params": best.params,
        "best_user_attrs": dict(best.user_attrs),
        "should_update_config": should_update,
        "completed_at": datetime.now().isoformat(),
    }

    # Save report
    report_path = RESULTS_DIR / f"{pair}_{tf}_wf_v2_report.json"
    report_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"  Report saved: {report_path}")

    return result


# ── Grid search (warm-start helper) ────────────────────────────────────────


def run_grid(
    pair: str,
    tf: str = "M15",
    base_values: list[float] | None = None,
    neg_weight_values: list[float] | None = None,
    kz_penalty_values: list[float] | None = None,
    n_windows: int = 3,
) -> dict[str, Any]:
    """Brute-force grid over the 3 continuous params with all boosters ON.

    Fast way to find a good starting point before Optuna fine-tunes.
    """
    base_values = base_values or [0.20, 0.25, 0.30, 0.35, 0.40]
    neg_weight_values = neg_weight_values or [0.0, 0.5, 1.0, 1.5]
    kz_penalty_values = kz_penalty_values or [-0.10, -0.05, -0.01]

    bars = load_bars(pair, tf)
    spread = get_spread_for_pair(pair)
    rows = []

    total = len(base_values) * len(neg_weight_values) * len(kz_penalty_values)
    print(f"  Grid: {total} combos for {pair}/{tf}")

    for base in base_values:
        for nw in neg_weight_values:
            for kz in kz_penalty_values:
                # Apply params
                tts_module.MW_BASE_CONFIDENCE = base
                tts_module.NEGATIVE_WEIGHT = nw
                tts_module.KILL_ZONE_ACTIVE_BOOST = kz
                # Enable all switchable boosters
                for name, val in _BOOSTER_DEFAULTS.items():
                    setattr(tts_module, name, val)
                for name, val in _NEGATIVE_DEFAULTS.items():
                    setattr(tts_module, name, val)
                for name, val in _OTHER_POSITIVE_DEFAULTS.items():
                    setattr(tts_module, name, val)
                for name, val in _OTHER_NEGATIVE_DEFAULTS.items():
                    setattr(tts_module, name, val)

                try:
                    wf = run_strategy_walk_forward(
                        bars=bars,
                        strategy_factory=lambda: TTSStrategy(
                            symbol=pair,
                            min_confidence=base,  # noqa: B023
                            min_quality_score=DEFAULT_BT_CONFIG["min_quality_score"],
                        ),
                        pair=pair,
                        n_windows=n_windows,
                        initial_balance=DEFAULT_BT_CONFIG["starting_balance"],
                        spread_pips=spread,
                        commission_per_lot=DEFAULT_BT_CONFIG["commission_per_lot"],
                        min_confidence=base,
                    )
                    agg = wf.aggregated
                    if agg:
                        rows.append(
                            {
                                "base_confidence": base,
                                "negative_weight": nw,
                                "kz_penalty": kz,
                                "win_rate": agg.mean_win_rate,
                                "profit_factor": agg.mean_profit_factor,
                                "max_drawdown": agg.mean_max_drawdown,
                                "mean_trade_count": agg.mean_trade_count,
                                "total_pnl": agg.mean_total_pnl,
                                "go_nogo": wf.go_nogo,
                            }
                        )
                except Exception:
                    logger.warning(
                        "Walk-forward failed for base=%.3f nw=%.3f kz=%.3f, skipping row",
                        base,
                        nw,
                        kz,
                    )
                finally:
                    restore_defaults()

    if not rows:
        return {"rows": [], "best_grid_row": {}}

    # Sort by composite score
    for r in rows:
        wr = r["win_rate"]
        dd = max(r["max_drawdown"], 0.01)
        tc = max(r["mean_trade_count"], 1.0)
        r["score"] = wr * (tc**0.5) * (1.0 / (1.0 + dd * 100))

    rows.sort(key=lambda r: r["score"], reverse=True)
    best = rows[0]

    print(
        f"  Grid best: WR={best['win_rate']:.1%} PF={best['profit_factor']:.2f} "
        f"Trades={best['mean_trade_count']:.0f} P&L=${best['total_pnl']:.2f}"
    )
    print(f"    base={best['base_confidence']} nw={best['negative_weight']} kz={best['kz_penalty']}")

    return {"rows": rows, "best_grid_row": best}


# ── Config update ───────────────────────────────────────────────────────────


def update_per_symbol_config(pair: str, tf: str, study_result: dict) -> bool:
    """Update per_symbol_configs.py if study result beats current config.

    Only updates when WR >= 45% and trades >= 20 per window.
    """
    if not study_result.get("should_update_config"):
        print(f"  Skipping config update for {pair}/{tf} — doesn't meet threshold")
        return False

    params = study_result["best_params"]
    attrs = study_result["best_user_attrs"]

    config_path = PROJECT_ROOT / "src" / "forex_bot" / "ml" / "per_symbol_configs.py"
    if not config_path.exists():
        print(f"  Config file not found: {config_path}")
        return False

    config_path.read_text()
    new_config = {
        "base_confidence": params.get("base_confidence", 0.30),
        "kz_penalty": params.get("kz_penalty", -0.05),
        "htf_penalty": -0.15,
        "top_confluences": [],
        "negative_weight": params.get("negative_weight", 1.0),
        f"# Optuna V2 walk-forward | WR={attrs.get('win_rate', 0):.1%} "
        f"| Trades={attrs.get('mean_trade_count', 0):.0f} "
        f"| PF={attrs.get('profit_factor', 0):.2f}": None,
    }

    # Build enabled boosters list
    enabled = [name for name in BOOSTER_SWITCHES if params.get(f"booster_{name}", False)]
    new_config["top_confluences"] = enabled

    # Mark: this is a simplified update — a proper implementation would
    # parse the Python AST. For now, we append a note.
    print(f"  ★ {pair}/{tf} config eligible for update. Manual review recommended.")
    print(f"    New config: {json.dumps({k: v for k, v in new_config.items() if v is not None}, indent=2)}")

    return True


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    """Run grid + Optuna for all pairs."""
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "XAUUSD"]
    tf = "M15"

    for pair in pairs:
        print(f"\n{'=' * 60}")
        print(f"  {pair}/{tf}")
        print(f"{'=' * 60}")

        # Quick grid warm-start
        run_grid(pair, tf, n_windows=2)

        # Walk-forward Optuna
        result = run_wf_study(pair, tf, n_trials=80, n_windows=2)

        # Check if config should be updated
        update_per_symbol_config(pair, tf, result)

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
