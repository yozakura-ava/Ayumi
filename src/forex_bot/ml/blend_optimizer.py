"""Strategy Blend Optimizer — uses Optuna to find the best strategy combination."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import optuna
from backtest.blend_backtest import BacktestConfig, BlendBacktest
from ml.signal_provider import HistoricalSignalProvider

if TYPE_CHECKING:
    from strategies.registry import StrategyRegistry


@dataclass
class BlendOptConfig:
    """Configuration for blend optimization."""

    starting_balance: float = 10000.0
    risk_per_trade_pct: float = 0.005
    max_concurrent: int = 8
    min_trades_for_score: int = 10
    cpu_limit_percent: float = 15.0
    n_trials: int = 100
    timeout_seconds: int = 600


@dataclass
class BlendConfig:
    """A specific strategy blend configuration."""

    active_strategies: dict[str, bool]
    strategy_weights: dict[str, float]
    allowed_symbols: dict[str, list[str]]
    sniper_threshold: float
    swarm_threshold: float


@dataclass
class BlendResult:
    """Result of a blend optimization run."""

    best_config: BlendConfig
    best_score: float
    best_win_rate: float
    best_total_trades: int
    best_max_drawdown_pct: float
    study: optuna.Study


def softmax_weights(weights: dict[str, float]) -> dict[str, float]:
    """Normalize weights using softmax so they sum to 1.0."""
    if not weights:
        return {}
    w = np.array(list(weights.values()))
    e = np.exp(w - np.max(w))
    s = e / e.sum()
    return dict(zip(weights.keys(), s.tolist()))  # noqa: B905


class StrategyBlendOptimizer:
    """Search over strategy combinations to find the best blend."""

    def __init__(self, registry: StrategyRegistry, config: BlendOptConfig) -> None:
        self._registry = registry
        self._config = config
        self._all_strategies = registry.get_all()
        self._signal_provider = HistoricalSignalProvider(registry)
        self._best_result: BlendResult | None = None
        self._trial_start: float = 0.0
        self._cpu_budget_seconds: float = 0.0
        self._cpu_used_seconds: float = 0.0

    @staticmethod
    def _compute_score(
        win_rate: float,
        total_trades: int,
        max_dd_pct: float,
        gross_profit: float = 0.0,
        gross_loss: float = 0.0,
        num_active_strategies: int = 1,
    ) -> float:
        """Score: profit_factor * win_rate * sqrt(trades) * dd_factor * strategy_penalty."""
        if total_trades < 1:
            return 0.0
        dd_factor = 1.0 / (1.0 + max_dd_pct / 100.0)
        profit_factor = gross_profit / max(gross_loss, 1.0)
        strategy_penalty = 1.0 / (1.0 + 0.1 * (num_active_strategies - 1))
        return profit_factor * win_rate * math.sqrt(total_trades) * dd_factor * strategy_penalty

    def objective(self, trial: optuna.Trial) -> float:
        """Optuna objective function."""
        # CPU metering
        if self._cpu_budget_seconds > 0 and self._cpu_used_seconds >= self._cpu_budget_seconds:
            raise optuna.TrialPruned("CPU budget exceeded")

        trial_start = time.monotonic()

        active: dict[str, bool] = {}
        weights: dict[str, float] = {}
        allowed_symbols: dict[str, list[str]] = {}
        all_symbols: set[str] = set()

        for strat in self._all_strategies:
            sid = strat.strategy_id
            is_active = trial.suggest_categorical(f"active_{sid}", [True, False])
            active[sid] = is_active

            if is_active:
                w = trial.suggest_float(f"weight_{sid}", 0.1, 1.0)
                weights[sid] = w
                # Per-strategy symbol selection
                chosen = trial.suggest_categorical(
                    f"symbols_{sid}",
                    [",".join(strat.symbols), ",".join(strat.symbols[:1])]
                    if len(strat.symbols) > 1
                    else [strat.symbols[0]],
                )
                allowed_symbols[sid] = chosen.split(",")
                all_symbols.update(sid.upper() for sid in allowed_symbols[sid])

        # At least one strategy must be active
        if not any(active.values()):
            return 0.0

        # Thresholds
        sniper_threshold = trial.suggest_float("sniper_threshold", 0.55, 0.85)
        swarm_threshold = trial.suggest_float("swarm_threshold", 0.30, 0.55)

        # Get signals from historical provider
        blend_cfg = BlendConfig(
            active_strategies=active,
            strategy_weights=weights,
            allowed_symbols=allowed_symbols,
            sniper_threshold=sniper_threshold,
            swarm_threshold=swarm_threshold,
        )
        signals = self._signal_provider.get_signals_for_blend(blend_cfg)

        if not signals:
            return 0.0

        # Run blend backtest
        bt_config = BacktestConfig(
            starting_balance=self._config.starting_balance,
            risk_per_trade_pct=self._config.risk_per_trade_pct,
            max_sniper=self._config.max_concurrent,
            max_swarm=self._config.max_concurrent,
        )
        bt = BlendBacktest(bt_config)
        result = bt.run(signals)

        # CPU metering update
        self._cpu_used_seconds += time.monotonic() - trial_start

        # Score
        if result.total_trades < self._config.min_trades_for_score:
            return 0.0

        num_active = sum(1 for v in active.values() if v)
        score = self._compute_score(
            result.win_rate,
            result.total_trades,
            result.max_drawdown_pct,
            gross_profit=result.gross_profit,
            gross_loss=result.gross_loss,
            num_active_strategies=num_active,
        )
        return score

    def optimize(self, n_trials: int | None = None, timeout: int | None = None) -> BlendResult:
        """Run optimization."""
        # Pre-generate signals for all strategies
        self._signal_provider.generate_signals("EURUSD", "2025-01-01", "2025-03-31")

        n_trials = n_trials or self._config.n_trials
        timeout = timeout or self._config.timeout_seconds

        # CPU budget: cpu_limit_percent of timeout
        self._cpu_budget_seconds = (self._config.cpu_limit_percent / 100.0) * timeout
        self._cpu_used_seconds = 0.0

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(direction="maximize")
        study.optimize(self.objective, n_trials=n_trials, timeout=timeout)

        trial = study.best_trial
        best_config = self._trial_to_blend_config(trial)

        # Re-run best to get metrics
        blend_cfg = BlendConfig(
            active_strategies=best_config.active_strategies,
            strategy_weights=softmax_weights(best_config.strategy_weights),
            allowed_symbols=best_config.allowed_symbols,
            sniper_threshold=best_config.sniper_threshold,
            swarm_threshold=best_config.swarm_threshold,
        )
        signals = self._signal_provider.get_signals_for_blend(blend_cfg)
        bt_config = BacktestConfig(
            starting_balance=self._config.starting_balance,
            risk_per_trade_pct=self._config.risk_per_trade_pct,
        )
        bt = BlendBacktest(bt_config)
        result = bt.run(signals)

        self._best_result = BlendResult(
            best_config=best_config,
            best_score=trial.value if trial.value else 0.0,
            best_win_rate=result.win_rate,
            best_total_trades=result.total_trades,
            best_max_drawdown_pct=result.max_drawdown_pct,
            study=study,
        )
        return self._best_result

    def get_best_blend(self) -> BlendConfig:
        """Return the best blend configuration found."""
        if self._best_result is None:
            raise RuntimeError("No optimization run yet. Call optimize() first.")
        return self._best_result.best_config

    def _trial_to_blend_config(self, trial: optuna.trial.FrozenTrial) -> BlendConfig:
        """Convert an Optuna trial to a BlendConfig."""
        active: dict[str, bool] = {}
        weights: dict[str, float] = {}
        allowed_symbols: dict[str, list[str]] = {}

        for strat in self._all_strategies:
            sid = strat.strategy_id
            is_active = trial.params.get(f"active_{sid}", False)
            active[sid] = is_active

            if is_active:
                weights[sid] = round(trial.params.get(f"weight_{sid}", 0.5), 3)
                sym_str = trial.params.get(f"symbols_{sid}", ",".join(strat.symbols))
                allowed_symbols[sid] = sym_str.split(",")

        return BlendConfig(
            active_strategies=active,
            strategy_weights=weights,
            allowed_symbols=allowed_symbols,
            sniper_threshold=round(trial.params.get("sniper_threshold", 0.70), 3),
            swarm_threshold=round(trial.params.get("swarm_threshold", 0.40), 3),
        )
