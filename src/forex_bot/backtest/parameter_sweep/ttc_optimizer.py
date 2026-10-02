"""Optuna optimization study for the TTC signal engine (TTSStrategy).

Uses the existing WalkForwardObjective / OptunaOptimizer infrastructure
from optuna_optimizer.py, targeting the TTC-specific tunable parameters.

Embargo / Out-of-Sample Leakage Notes
--------------------------------------
The underlying WalkForwardValidator (quant/walk_forward.py) creates
**contiguous** train/val/test slices with no buffer between them.
For financial time series with autocorrelation, directly adjacent
train→test boundaries risk information leakage.

The ``embargo_bars`` parameter (default 0) is an API-level contract for
a future walk-forward runner upgrade. When wired through to the
validator, it will drop ``embargo_bars`` bars between each train→val
and val→test boundary within every walk-forward window.

**Recommended value for XAUUSD M15:** 96 bars (24 hours of M15 data)
to cover a full trading day's autocorrelation decay.

PF=0 Mitigation
---------------
Trials producing PF=0 (no winning trades or no trades at all in one or
more windows) are now explicitly pruned rather than silently scored.
This prevents the optimizer from exploring degenerate parameter regions
where the strategy is so selective it never fires.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .optuna_optimizer import (
    OptimizationResult,
    SearchSpace,
    float_range,
    int_range,
)

logger = logging.getLogger(__name__)

# Project root (for data/ path resolution)
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DATA_DIR = _PROJECT_ROOT / "data" / "forex" / "historical"
_REPORTS_DIR = _PROJECT_ROOT / "reports" / "optuna_ttc"


def ttc_search_space() -> SearchSpace:
    """Search space for TTC signal engine optimization.

    Targets confidence thresholds, confluence boost weights,
    negative confluence severity, and swing detection parameters.
    """
    return SearchSpace(
        min_confidence=float_range("min_confidence", 0.15, 0.50, step=0.05),
        min_quality_score=float_range("min_quality_score", 0.15, 0.45, step=0.05),
        mw_base_confidence=float_range("mw_base_confidence", 0.20, 0.45, step=0.05),
        rsi_divergence_boost=float_range("rsi_divergence_boost", 0.0, 0.20, step=0.05),
        htf_trend_aligned_boost=float_range("htf_trend_aligned_boost", 0.0, 0.20, step=0.05),
        htf_opposing_penalty=float_range("htf_opposing_penalty", -0.25, -0.05, step=0.05),
        kill_zone_active_boost=float_range("kill_zone_active_boost", -0.15, 0.05, step=0.05),
        negative_weight=float_range("negative_weight", 0.0, 2.0, step=0.25),
        swing_lookback=int_range("swing_lookback", 3, 10),
        history_bars=int_range("history_bars", 30, 100, step=10),
    )


def ttc_strategy_factory(
    params: dict[str, Any],
    symbol: str = "EURUSD",
    timeframe: str = "H1",
):
    """Create a TTSStrategy with Optuna-optimized parameters.

    Monkeypatches the module-level constants in tts_strategy.py
    before constructing the strategy instance.
    """
    from backtest.strategies.tts_strategy import TTSStrategy

    # Map search-space param names to module-level constant names
    _CONST_MAP = {
        "mw_base_confidence": "MW_BASE_CONFIDENCE",
        "rsi_divergence_boost": "RSI_DIVERGENCE_BOOST",
        "htf_trend_aligned_boost": "HTF_TREND_ALIGNED_BOOST",
        "htf_opposing_penalty": "HTF_OPPOSING_PENALTY",
        "kill_zone_active_boost": "KILL_ZONE_ACTIVE_BOOST",
        "negative_weight": "NEGATIVE_WEIGHT",
    }

    import backtest.strategies.tts_strategy as tts_mod

    # Save originals (in case of nested calls)
    originals = {}
    for param_name, const_name in _CONST_MAP.items():
        if param_name in params:
            originals[const_name] = getattr(tts_mod, const_name, None)
            setattr(tts_mod, const_name, params[param_name])

    # Also patch swing/history if present
    if "swing_lookback" in params:
        originals["SWING_LOOKBACK"] = getattr(tts_mod, "SWING_LOOKBACK", 5)
        tts_mod.SWING_LOOKBACK = params["swing_lookback"]
    if "history_bars" in params:
        originals["HISTORY_BARS"] = getattr(tts_mod, "HISTORY_BARS", 50)
        tts_mod.HISTORY_BARS = params["history_bars"]

    strategy = TTSStrategy(
        symbol=symbol,
        min_confidence=params.get("min_confidence", 0.20),
        min_quality_score=params.get("min_quality_score", 0.25),
        timeframe=timeframe,
    )

    # Restore originals
    for const_name, orig_val in originals.items():
        setattr(tts_mod, const_name, orig_val)

    return strategy


def load_bars(pair: str, timeframe: str = "H1") -> list:
    """Load CSV data for a pair/timeframe into Bar objects."""
    from datetime import datetime as _dt

    from backtest.engine import Bar

    csv_path = _DATA_DIR / f"{pair.upper()}_{timeframe}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No H1 data for {pair}: {csv_path}")

    bars: list[Bar] = []
    dropped = 0
    with open(csv_path) as f:
        f.readline()  # skip header
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 6:
                continue
            try:
                date_str = parts[0].strip()
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        bar_time = _dt.strptime(date_str, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue
                bars.append(
                    Bar(
                        time=bar_time,
                        open=float(parts[1]),
                        high=float(parts[2]),
                        low=float(parts[3]),
                        close=float(parts[4]),
                        volume=float(parts[5]),
                    )
                )
            except (ValueError, IndexError):
                dropped += 1
                continue

    if dropped:
        logger.warning("Dropped %d malformed bars from %s", dropped, csv_path)

    logger.info("Loaded %d %s bars for %s", len(bars), timeframe, pair)
    return bars


def run_ttc_optuna(
    pair: str = "EURUSD",
    timeframe: str = "H1",
    n_trials: int = 100,
    n_windows: int = 5,
    seed: int = 42,
    composite_weights: dict[str, float] | None = None,
    min_trades: int = 2,
    embargo_bars: int = 0,
) -> OptimizationResult:
    """Run Optuna optimization for the TTC signal engine.

    Args:
        pair: Forex pair (e.g. "EURUSD")
        timeframe: Bar timeframe (default "H1")
        n_trials: Number of Optuna trials
        n_windows: Walk-forward windows
        seed: Random seed for reproducibility
        composite_weights: Custom composite score weights
        min_trades: Minimum avg trades per window (default 2, TTC is selective)
        embargo_bars: Bars to drop between train/val/test boundaries.
            Default 0 (no embargo — preserves current behavior). When the
            walk-forward runner supports embargo, set to 96 for XAUUSD M15
            (24h autocorrelation decay). See module docstring for details.

    Returns:
        OptimizationResult with best params and walk-forward results.
    """
    import optuna
    from optuna.samplers import TPESampler

    from .optuna_optimizer import WalkForwardObjective

    bars = load_bars(pair, timeframe)
    search_space = ttc_search_space()

    def factory(_params: dict[str, Any]):
        return ttc_strategy_factory(_params, symbol=pair, timeframe=timeframe)

    class TTCObjective(WalkForwardObjective):
        """Custom objective with configurable min trade threshold."""

        def __init__(self, *args, min_trades: int = 2, **kwargs):
            super().__init__(*args, **kwargs)
            self._min_trades = min_trades

        def __call__(self, trial):
            from backtest.walk_forward_runner import run_strategy_walk_forward

            params = self._search_space.suggest(trial)
            strategy = self._strategy_factory(params)

            try:
                wf_result = run_strategy_walk_forward(
                    bars=self._bars,
                    strategy_factory=lambda: strategy,
                    pair=self._pair,
                    n_windows=self._n_windows,
                    train_ratio=self._train_ratio,
                    val_ratio=self._val_ratio,
                    overlap_ratio=self._overlap_ratio,
                    initial_balance=self._initial_balance,
                    spread_pips=self._spread_pips,
                    commission_per_lot=self._commission_per_lot,
                )
            except Exception as exc:
                logger.warning("Walk-forward failed for trial %d: %s", trial.number, exc)
                raise optuna.TrialPruned() from exc

            self._results_by_trial[trial.number] = wf_result
            self._params_by_trial[trial.number] = params

            agg = wf_result.aggregated
            if agg is None:
                raise optuna.TrialPruned()

            if agg.mean_trade_count < self._min_trades:
                raise optuna.TrialPruned()

            # Prune trials with PF=0 (no winning trades in avg across windows).
            # These represent degenerate parameter regions where the strategy
            # is too selective or market conditions didn't align — the 1/3
            # PF=0 anomaly from the 3-seed investigation traces to this.
            if agg.mean_profit_factor <= 0.0:
                logger.info(
                    "Trial %d pruned: PF=%.2f (no winning trades)",
                    trial.number,
                    agg.mean_profit_factor,
                )
                raise optuna.TrialPruned()

            score = self._composite_score(agg)

            trial.set_user_attr("win_rate", agg.mean_win_rate)
            trial.set_user_attr("profit_factor", agg.mean_profit_factor)
            trial.set_user_attr("max_drawdown", agg.mean_max_drawdown)
            trial.set_user_attr("sharpe_ratio", agg.mean_sharpe_ratio)
            trial.set_user_attr("trade_count", agg.mean_trade_count)
            trial.set_user_attr("total_pnl", agg.mean_total_pnl)
            trial.set_user_attr("windows_passed", agg.windows_passed)
            trial.set_user_attr("total_windows", agg.total_windows)
            trial.set_user_attr("go_nogo", wf_result.go_nogo)

            if not wf_result.go_nogo:
                score -= 1.0

            return score

    objective = TTCObjective(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        search_space=search_space,
        n_windows=n_windows,
        composite_weights=composite_weights,
        min_trades=min_trades,
    )

    sampler = TPESampler(seed=seed)
    study = optuna.create_study(sampler=sampler, direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    if not completed:
        return OptimizationResult(
            best_params={},
            best_value=float("-inf"),
            n_trials=len(study.trials),
            go_nogo=False,
            study_summary={
                "n_trials": len(study.trials),
                "n_complete": 0,
                "n_pruned": len(study.trials),
            },
        )

    best_trial = study.best_trial
    best_wf = objective.get_result(best_trial.number)

    result = OptimizationResult(
        best_params=best_trial.params,
        best_value=best_trial.value if best_trial.value is not None else float("-inf"),
        best_walk_forward=best_wf,
        n_trials=len(study.trials),
        go_nogo=best_wf.go_nogo if best_wf else False,
        study_summary={
            "n_trials": len(study.trials),
            "n_complete": len(completed),
            "n_pruned": len([t for t in study.trials if t.state == optuna.trial.TrialState.PRUNED]),
            "best_score": best_trial.value,
            "sampler": type(sampler).__name__,
        },
    )

    # Save results
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    out_path = _REPORTS_DIR / f"{pair}_{timeframe}_{timestamp}.json"

    report = {
        "pair": pair,
        "timeframe": timeframe,
        "n_trials": result.n_trials,
        "best_score": result.best_value,
        "go_nogo": result.go_nogo,
        "best_params": result.best_params,
        "study_summary": result.study_summary,
        "completed_at": datetime.utcnow().isoformat(),
    }

    if result.best_walk_forward and result.best_walk_forward.aggregated:
        agg = result.best_walk_forward.aggregated
        report["best_metrics"] = {
            "win_rate": agg.mean_win_rate,
            "profit_factor": agg.mean_profit_factor,
            "max_drawdown": agg.mean_max_drawdown,
            "sharpe_ratio": agg.mean_sharpe_ratio,
            "trade_count": agg.mean_trade_count,
            "total_pnl": agg.mean_total_pnl,
            "windows_passed": agg.windows_passed,
            "total_windows": agg.total_windows,
        }

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("Results saved to %s", out_path)
    return result


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Optuna optimization for TTC signal engine")
    parser.add_argument("--pair", default="EURUSD", help="Forex pair")
    parser.add_argument("--timeframe", default="H1", help="Bar timeframe")
    parser.add_argument("--n-trials", type=int, default=100, help="Number of Optuna trials")
    parser.add_argument("--n-windows", type=int, default=5, help="Walk-forward windows")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    result = run_ttc_optuna(
        pair=args.pair,
        timeframe=args.timeframe,
        n_trials=args.n_trials,
        n_windows=args.n_windows,
        seed=args.seed,
    )

    print(f"\n{'=' * 60}")
    print(f"TTC Optuna Results — {args.pair} {args.timeframe}")
    print(f"{'=' * 60}")
    print(f"Trials: {result.n_trials}")
    print(f"Best score: {result.best_value:.4f}")
    print(f"Go/No-Go: {result.go_nogo}")
    print("\nBest params:")
    for k, v in sorted(result.best_params.items()):
        print(f"  {k}: {v}")

    if result.best_walk_forward and result.best_walk_forward.aggregated:
        agg = result.best_walk_forward.aggregated
        print("\nMetrics:")
        print(f"  Win Rate:    {agg.mean_win_rate:.1%}")
        print(f"  Profit Factor: {agg.mean_profit_factor:.2f}")
        print(f"  Max Drawdown:  {agg.mean_max_drawdown:.1%}")
        print(f"  Sharpe:        {agg.mean_sharpe_ratio:.2f}")
        print(f"  Avg Trades:    {agg.mean_trade_count:.1f}")
        print(f"  Windows:       {agg.windows_passed}/{agg.total_windows} passed")
