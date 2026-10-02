from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import optuna
from backtest.engine import Bar
from optuna.samplers import TPESampler
from quant.walk_forward import (
    AggregatedMetrics,
    WalkForwardResults,
)

logger = logging.getLogger(__name__)

StrategyFactory = Callable[[dict[str, Any]], Any]


class SearchSpace:
    def __init__(self, **kwargs):
        self._specs: dict[str, dict[str, Any]] = {}
        for name, spec in kwargs.items():
            if isinstance(spec, dict) and "type" in spec:
                self._specs[name] = spec
            else:
                raise ValueError(
                    f"Invalid search space spec for '{name}'. Use suggest_{type}(name, low, high, ...) format."
                )

    def suggest(self, trial: optuna.Trial, prefix: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {}
        for name, spec in self._specs.items():
            key = f"{prefix}{name}" if prefix else name
            suggest_type = spec["type"]
            if suggest_type == "int":
                params[name] = trial.suggest_int(
                    key,
                    spec["low"],
                    spec["high"],
                    step=spec.get("step", 1),
                    log=spec.get("log", False),
                )
            elif suggest_type == "float":
                params[name] = trial.suggest_float(
                    key,
                    spec["low"],
                    spec["high"],
                    step=spec.get("step"),
                    log=spec.get("log", False),
                )
            elif suggest_type == "categorical":
                params[name] = trial.suggest_categorical(key, spec["choices"])
            else:
                raise ValueError(f"Unknown suggest type: {suggest_type}")
        return params

    @property
    def param_names(self) -> list[str]:
        return sorted(self._specs.keys())


def int_range(name: str, low: int, high: int, step: int = 1) -> dict[str, Any]:
    return {"type": "int", "low": low, "high": high, "step": step}


def float_range(
    name: str,
    low: float,
    high: float,
    step: float | None = None,
    log: bool = False,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": "float", "low": low, "high": high, "log": log}
    if step is not None:
        spec["step"] = step
    return spec


def categorical(name: str, choices: list[Any]) -> dict[str, Any]:
    return {"type": "categorical", "choices": choices}


def session_range_mr_search_space() -> SearchSpace:
    return SearchSpace(
        atr_period=int_range("atr_period", 8, 30),
        atr_sl_multiplier=float_range("atr_sl_multiplier", 0.5, 3.0, step=0.1),
        atr_tp_multiplier=float_range("atr_tp_multiplier", 1.0, 4.0, step=0.1),
        rsi_period=int_range("rsi_period", 8, 28),
        rsi_long_level=float_range("rsi_long_level", 20.0, 40.0, step=1.0),
        rsi_short_level=float_range("rsi_short_level", 60.0, 80.0, step=1.0),
        session_range_min_pips=float_range("session_range_min_pips", 10.0, 50.0, step=5.0),
        entry_near_extreme_pips=float_range("entry_near_extreme_pips", 5.0, 30.0, step=1.0),
        hard_cap_sl_pips=float_range("hard_cap_sl_pips", 15.0, 50.0, step=5.0),
        tp1_rr=float_range("tp1_rr", 0.5, 2.0, step=0.1),
        tp2_rr=float_range("tp2_rr", 1.0, 3.0, step=0.1),
        ema_trend_period=int_range("ema_trend_period", 20, 100),
        session_range_sl_fraction=float_range("session_range_sl_fraction", 0.3, 0.9, step=0.05),
    )


@dataclass
class OptimizationResult:
    best_params: dict[str, Any]
    best_value: float
    best_walk_forward: WalkForwardResults | None = None
    n_trials: int = 0
    go_nogo: bool = False
    study_summary: dict[str, Any] = field(default_factory=dict)


class WalkForwardObjective:
    def __init__(
        self,
        bars: list[Bar],
        strategy_factory: StrategyFactory,
        pair: str,
        search_space: SearchSpace,
        n_windows: int = 5,
        train_ratio: float = 0.7,
        val_ratio: float = 0.15,
        overlap_ratio: float = 0.2,
        initial_balance: float = 10000,
        spread_pips: float | None = None,
        commission_per_lot: float | None = None,
        composite_weights: dict[str, float] | None = None,
    ):
        self._bars = bars
        self._strategy_factory = strategy_factory
        self._pair = pair
        self._search_space = search_space
        self._n_windows = n_windows
        self._train_ratio = train_ratio
        self._val_ratio = val_ratio
        self._overlap_ratio = overlap_ratio
        self._initial_balance = initial_balance
        self._spread_pips = spread_pips
        self._commission_per_lot = commission_per_lot
        if composite_weights is not None:
            total = sum(composite_weights.values())
            if abs(total - 1.0) > 1e-9:
                raise ValueError(f"Custom composite_weights must sum to 1.0, got {total:.4f}")
        self._composite_weights = composite_weights or {
            "win_rate": 0.30,
            "profit_factor": 0.30,
            "max_drawdown": 0.25,
            "sharpe_ratio": 0.15,
        }
        self._results_by_trial: dict[int, WalkForwardResults] = {}
        self._params_by_trial: dict[int, dict[str, Any]] = {}

    def __call__(self, trial: optuna.Trial) -> float:
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

        if agg.mean_trade_count < 5:
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

    def _composite_score(self, agg: AggregatedMetrics) -> float:
        w = self._composite_weights
        score = 0.0
        score += w.get("win_rate", 0.0) * agg.mean_win_rate
        score += w.get("profit_factor", 0.0) * min(agg.mean_profit_factor, 5.0) / 5.0
        score += w.get("max_drawdown", 0.0) * (1.0 - min(agg.mean_max_drawdown, 0.15) / 0.15)
        score += w.get("sharpe_ratio", 0.0) * min(max(agg.mean_sharpe_ratio, 0.0), 3.0) / 3.0
        return score

    def get_result(self, trial_number: int) -> WalkForwardResults | None:
        return self._results_by_trial.get(trial_number)

    def get_params(self, trial_number: int) -> dict[str, Any] | None:
        return self._params_by_trial.get(trial_number)


class OptunaOptimizer:
    def __init__(
        self,
        bars: list[Bar],
        strategy_factory: StrategyFactory,
        pair: str,
        search_space: SearchSpace,
        n_trials: int = 100,
        n_windows: int = 5,
        train_ratio: float = 0.7,
        val_ratio: float = 0.15,
        overlap_ratio: float = 0.2,
        initial_balance: float = 10000,
        spread_pips: float | None = None,
        commission_per_lot: float | None = None,
        composite_weights: dict[str, float] | None = None,
        sampler: optuna.samplers.BaseSampler | None = None,
        seed: int | None = 42,
        direction: str = "maximize",
    ):
        self._bars = bars
        self._strategy_factory = strategy_factory
        self._pair = pair
        self._search_space = search_space
        self._n_trials = n_trials
        self._n_windows = n_windows
        self._train_ratio = train_ratio
        self._val_ratio = val_ratio
        self._overlap_ratio = overlap_ratio
        self._initial_balance = initial_balance
        self._spread_pips = spread_pips
        self._commission_per_lot = commission_per_lot
        self._composite_weights = composite_weights
        self._seed = seed
        self._direction = direction

        self._sampler = sampler or TPESampler(seed=seed)

        self._objective = WalkForwardObjective(
            bars=bars,
            strategy_factory=strategy_factory,
            pair=pair,
            search_space=search_space,
            n_windows=n_windows,
            train_ratio=train_ratio,
            val_ratio=val_ratio,
            overlap_ratio=overlap_ratio,
            initial_balance=initial_balance,
            spread_pips=spread_pips,
            commission_per_lot=commission_per_lot,
            composite_weights=composite_weights,
        )

    def optimize(self) -> OptimizationResult:
        study = optuna.create_study(
            sampler=self._sampler,
            direction=self._direction,
        )

        study.optimize(self._objective, n_trials=self._n_trials, show_progress_bar=False)

        completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if not completed:
            return OptimizationResult(
                best_params={},
                best_value=float("-inf") if self._direction == "maximize" else float("inf"),
                n_trials=len(study.trials),
                go_nogo=False,
                study_summary={
                    "n_trials": len(study.trials),
                    "n_complete": 0,
                    "n_pruned": len([t for t in study.trials if t.state == optuna.trial.TrialState.PRUNED]),
                    "sampler": type(self._sampler).__name__,
                },
            )

        best_trial = study.best_trial
        best_wf = self._objective.get_result(best_trial.number)

        summary: dict[str, Any] = {
            "n_trials": len(study.trials),
            "n_complete": len(completed),
            "n_pruned": len([t for t in study.trials if t.state == optuna.trial.TrialState.PRUNED]),
            "best_score": best_trial.value,
            "sampler": type(self._sampler).__name__,
        }

        return OptimizationResult(
            best_params=best_trial.params,
            best_value=best_trial.value if best_trial.value is not None else float("-inf"),
            best_walk_forward=best_wf,
            n_trials=len(study.trials),
            go_nogo=best_wf.go_nogo if best_wf else False,
            study_summary=summary,
        )

    @property
    def objective(self) -> WalkForwardObjective:
        return self._objective
