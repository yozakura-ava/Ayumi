from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from .config import (
    QuantConfig,
    SizingMode,
)
from .correlation import CorrelationTracker
from .correlation import Position as CorrelationPosition
from .markov_regime import MarkovRegimeFilter
from .position_sizing import (
    DynamicSizingConfig,
    dynamic_sizing,
    fixed_fractional,
    kelly_criterion,
)
from .regime import (
    combined_regime as calc_combined_regime,
)
from .regime import (
    trend_regime as calc_trend_regime,
)
from .regime import (
    volatility_regime as calc_volatility_regime,
)
from .vaps import VAPSConfig, vaps_multiply

if TYPE_CHECKING:
    from backtest.engine import Bar, MarketState
    from backtest.strategies import ISignalStrategy

    from .portfolio import PortfolioSignal, StrategyPortfolio

logger = logging.getLogger(__name__)


class TradeAction(Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    RESIZE = "resize"


@dataclass(frozen=True)
class TradeDecision:
    action: TradeAction
    lot_size: float | None = None
    regime_confidence: float = 0.0
    correlation_warnings: tuple[str, ...] = ()
    sizing_mode: str = ""
    reject_reason: str = ""


@dataclass(frozen=True)
class ValidationResult:
    go_nogo: bool
    walk_forward_passed: bool
    aggregated_metrics: Any = None
    per_window_metrics: tuple = ()
    details: str = ""


@dataclass
class PortfolioState:
    balance: float = 100_000.0
    open_positions: dict[str, float] = field(default_factory=dict)
    win_streak: int = 0
    loss_streak: int = 0
    recent_pnl: float = 0.0
    total_wins: int = 0
    total_losses: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0


class QuantPipeline:
    def __init__(self, config: QuantConfig):
        self._config = config
        self._portfolio = PortfolioState()

        self._corr_tracker: CorrelationTracker | None = None
        if config.correlation.enabled:
            self._corr_tracker = CorrelationTracker(
                pairs=list(config.correlation.pairs),
                window=config.correlation.window,
                threshold=config.correlation.threshold,
            )

        # Bounded deques: regime functions only need the last
        # ``lookback`` elements.  Using ``deque(maxlen=...)`` prevents
        # unbounded growth over multi-year backtests or long-running
        # live sessions.
        _max_lookback = (
            max(
                config.regime.atr_lookback,
                config.regime.adx_period + 1,
                50,
            )
            * 2
        )
        self._atr_history: deque[float] = deque(maxlen=_max_lookback)
        self._high_history: deque[float] = deque(maxlen=_max_lookback)
        self._low_history: deque[float] = deque(maxlen=_max_lookback)
        self._close_history: deque[float] = deque(maxlen=_max_lookback)

        self._strategy_portfolio: StrategyPortfolio | None = None

        # Markov-regime filter (AYUAA-401 Phase 2). It is an additive
        # sizing layer: it multiplies the base lot by a persistence
        # factor but never overrides the base Kelly/FF calculation.
        # Cold start (insufficient history) returns neutral 1.0x so the
        # filter never *reduces* exposure before it has evidence.
        self._markov_filter: MarkovRegimeFilter | None = None
        self._markov_enabled: bool = bool(config.markov.enabled)
        if self._markov_enabled:
            self._markov_filter = MarkovRegimeFilter(
                states=list(config.markov.states),
                min_history=config.markov.min_history,
            )

        # The last classified 6-state regime label, used to form the
        # (prev, current) transition when ``update_bars`` is called.
        self._last_markov_state: str | None = None

    def reset_markov_filter(self) -> None:
        """Reset the Markov filter and associated state.

        Call this when switching symbols or restarting a backtest to
        prevent cross-pair transition contamination.
        """
        if self._markov_filter is not None:
            self._markov_filter.reset()
        self._last_markov_state = None

    def pre_trade_check(
        self,
        signal_symbol: str,
        entry_price: float,
        stop_loss: float,
        bar_time: datetime | None = None,
    ) -> TradeDecision:
        regime_confidence = 1.0
        correlation_warnings: list[str] = []
        reject_reason = ""

        if self._config.regime.enabled:
            regime_confidence = self._check_regime(bar_time)
            if regime_confidence < self._config.regime.min_confidence:
                reject_reason = (
                    f"Regime confidence {regime_confidence:.2f} below minimum {self._config.regime.min_confidence:.2f}"
                )
                return TradeDecision(
                    action=TradeAction.REJECT,
                    regime_confidence=regime_confidence,
                    reject_reason=reject_reason,
                )

        if self._config.correlation.enabled and self._corr_tracker is not None and self._corr_tracker.is_initialized:
            positions = [
                CorrelationPosition(
                    symbol=sym,
                    exposure=lot,
                )
                for sym, lot in self._portfolio.open_positions.items()
            ]
            if positions:
                correlation_warnings = self._corr_tracker.check_exposure(positions)

        lot_size: float | None = None
        sizing_mode = ""
        if self._config.position_sizing.enabled and stop_loss != 0.0:
            base_lot = self._calculate_base_lot(entry_price, stop_loss)
            if base_lot <= 0:
                reject_reason = "Position sizing returned zero lots"
                return TradeDecision(
                    action=TradeAction.REJECT,
                    regime_confidence=regime_confidence,
                    correlation_warnings=tuple(correlation_warnings),
                    reject_reason=reject_reason,
                )

            lot_size = self._apply_sizing_mode(base_lot)
            sizing_mode = self._config.position_sizing.mode.value

        if lot_size is not None and lot_size != base_lot:
            action = TradeAction.RESIZE
        else:
            action = TradeAction.ACCEPT

        return TradeDecision(
            action=action,
            lot_size=lot_size,
            regime_confidence=regime_confidence,
            correlation_warnings=tuple(correlation_warnings),
            sizing_mode=sizing_mode,
        )

    def validate_strategy(
        self,
        strategy: ISignalStrategy,
        bars: list[Bar],
    ) -> ValidationResult:
        if not self._config.walk_forward.enabled:
            return ValidationResult(
                go_nogo=True,
                walk_forward_passed=True,
                details="Walk-forward validation disabled in config",
            )

        from .walk_forward import run_strategy as run_walk_forward

        results = run_walk_forward(
            strategy=strategy,
            bars=bars,
            n_windows=self._config.walk_forward.n_windows,
            train_ratio=self._config.walk_forward.train_ratio,
            val_ratio=self._config.walk_forward.val_ratio,
            overlap_ratio=self._config.walk_forward.overlap_ratio,
        )

        return ValidationResult(
            go_nogo=results.go_nogo,
            walk_forward_passed=results.go_nogo,
            aggregated_metrics=results.aggregated,
            per_window_metrics=tuple(results.per_window),
            details=f"Walk-forward: {sum(1 for m in results.per_window if m.passed_go_nogo)}/{len(results.per_window)} windows passed",  # noqa: E501
        )

    def on_trade_closed(
        self,
        pnl: float,
    ) -> None:
        if pnl > 0:
            self._portfolio.win_streak += 1
            self._portfolio.loss_streak = 0
            self._portfolio.total_wins += 1
            self._portfolio.avg_win = (
                self._portfolio.avg_win * (self._portfolio.total_wins - 1) + pnl
            ) / self._portfolio.total_wins
        elif pnl < 0:
            self._portfolio.loss_streak += 1
            self._portfolio.win_streak = 0
            self._portfolio.total_losses += 1
            self._portfolio.avg_loss = (
                self._portfolio.avg_loss * (self._portfolio.total_losses - 1) + abs(pnl)
            ) / self._portfolio.total_losses

        self._portfolio.recent_pnl = pnl

    def update_bars(
        self,
        high: float,
        low: float,
        close: float,
        atr: float,
    ) -> None:
        self._atr_history.append(atr)
        self._high_history.append(high)
        self._low_history.append(low)
        self._close_history.append(close)

        # Phase 2: feed the resulting transition into the Markov filter
        # if it is enabled. We classify the current bar using the same
        # volatility + trend regime functions the pipeline already uses
        # for ``_check_regime``, then merge into the 6-state space the
        # filter is built over (extreme→high, neutral→ranging).
        if self._markov_filter is not None:
            current_state = self._get_current_markov_state()
            if current_state is not None and self._last_markov_state is not None:
                try:
                    self._markov_filter.observe(self._last_markov_state, current_state)
                except ValueError:
                    logger.warning(
                        "Markov filter rejected state %r — likely regime.py output changed. Skipping transition.",
                        current_state,
                    )
            self._last_markov_state = current_state

    def update_correlation_prices(self, prices: dict[str, float]) -> None:
        if self._corr_tracker is not None:
            self._corr_tracker.update(prices)

    @property
    def portfolio(self) -> PortfolioState:
        return self._portfolio

    @portfolio.setter
    def portfolio(self, value: PortfolioState) -> None:
        self._portfolio = value

    @property
    def strategy_portfolio(self) -> StrategyPortfolio | None:
        return self._strategy_portfolio

    def attach_portfolio(self, portfolio: StrategyPortfolio) -> None:
        self._strategy_portfolio = portfolio

    def evaluate_portfolio(
        self,
        market_states: dict[str, MarketState],
    ) -> list[PortfolioSignal]:
        if self._strategy_portfolio is None:
            return []
        from .portfolio import PortfolioSignal as PS

        raw_signals = self._strategy_portfolio.evaluate_all(market_states)
        result: list[PortfolioSignal] = []
        for ps in raw_signals:
            allocation = None
            for a in self._strategy_portfolio.config.allocations:
                if a.strategy_name == ps.strategy_name and a.symbol == ps.symbol:
                    allocation = a
                    break
            if allocation is None:
                continue
            decision = self.pre_trade_check(
                signal_symbol=ps.symbol,
                entry_price=ps.signal.entry_price,
                stop_loss=ps.signal.stop_loss,
            )
            if decision.action == TradeAction.REJECT:
                continue

            lot_size = self._strategy_portfolio.calculate_position_size(ps.signal, allocation)
            if decision.lot_size is not None:
                lot_size = min(lot_size, decision.lot_size)

            result.append(
                PS(
                    strategy_name=ps.strategy_name,
                    symbol=ps.symbol,
                    signal=ps.signal,
                    weight=ps.weight,
                    adjusted_lot_size=lot_size,
                )
            )
        return result

    def _check_regime(self, bar_time: datetime | None = None) -> float:
        if bar_time is None:
            logger.warning("_check_regime called without bar_time, using fabricated noon Monday")
        vol_result = calc_volatility_regime(
            list(self._atr_history),
            lookback=self._config.regime.atr_lookback,
        )
        trend_result = calc_trend_regime(
            list(self._high_history),
            list(self._low_history),
            list(self._close_history),
            adx_period=self._config.regime.adx_period,
        )

        from .regime import session_regime as calc_session_regime

        hour = bar_time.hour if bar_time else 12
        day_of_week = bar_time.weekday() if bar_time else 0
        session_result = calc_session_regime(hour, day_of_week)

        combined = calc_combined_regime(vol_result, trend_result, session_result)
        return combined.confidence

    def _calculate_base_lot(self, entry_price: float, stop_loss: float) -> float:
        sizing_cfg = self._config.position_sizing
        if sizing_cfg.mode == SizingMode.KELLY and self._portfolio.total_wins + self._portfolio.total_losses > 0:
            total_trades = self._portfolio.total_wins + self._portfolio.total_losses
            win_rate = self._portfolio.total_wins / total_trades
            avg_win = self._portfolio.avg_win if self._portfolio.avg_win > 0 else 1.0
            avg_loss = self._portfolio.avg_loss if self._portfolio.avg_loss > 0 else 1.0
            kelly_frac = kelly_criterion(win_rate, avg_win, avg_loss)
            if kelly_frac <= 0:
                return fixed_fractional(
                    self._portfolio.balance,
                    sizing_cfg.risk_pct,
                    entry_price,
                    stop_loss,
                )
            risk_amount = self._portfolio.balance * kelly_frac
            stop_distance = abs(entry_price - stop_loss)
            if stop_distance == 0:
                return 0.0
            return risk_amount / (stop_distance * 100_000)

        return fixed_fractional(
            self._portfolio.balance,
            sizing_cfg.risk_pct,
            entry_price,
            stop_loss,
        )

    def _apply_sizing_mode(self, base_lot: float) -> float:
        sizing_cfg = self._config.position_sizing

        if sizing_cfg.mode == SizingMode.VOLATILITY_ADAPTIVE:
            vaps_config = VAPSConfig(
                lookback=sizing_cfg.vaps_lookback,
                low_multiplier=sizing_cfg.vaps_low_multiplier,
                normal_multiplier=sizing_cfg.vaps_normal_multiplier,
                high_multiplier=sizing_cfg.vaps_high_multiplier,
                extreme_multiplier=sizing_cfg.vaps_extreme_multiplier,
                min_multiplier=sizing_cfg.vaps_min_multiplier,
                max_multiplier=sizing_cfg.vaps_max_multiplier,
            )
            adapted_lot, _regime, _pct = vaps_multiply(base_lot, list(self._atr_history), config=vaps_config)
            return adapted_lot

        if sizing_cfg.mode == SizingMode.DYNAMIC:
            dyn_config = DynamicSizingConfig(
                min_multiplier=sizing_cfg.dynamic_min_multiplier,
                max_multiplier=sizing_cfg.dynamic_max_multiplier,
                loss_reduction=sizing_cfg.dynamic_loss_reduction,
                win_increase=sizing_cfg.dynamic_win_increase,
                max_streak_impact=sizing_cfg.dynamic_max_streak_impact,
            )
            return dynamic_sizing(
                base_size=base_lot,
                recent_pnl=self._portfolio.recent_pnl,
                win_streak=self._portfolio.win_streak,
                loss_streak=self._portfolio.loss_streak,
                config=dyn_config,
            )

        if sizing_cfg.mode == SizingMode.MARKOV_ADAPTIVE:
            # Cold start: filter is disabled or not yet trained. The
            # contract is "no modulation" — return the base lot
            # untouched so the filter never *reduces* exposure before
            # it has any evidence to act on.
            if self._markov_filter is None or not self._markov_filter.is_ready():
                return base_lot
            current_state = self._get_current_markov_state()
            if current_state is None:
                return base_lot
            multiplier = self._markov_filter.size_multiplier(current_state)
            # The Markov filter's internal hard clamp is
            # [0.5, 1.3], but we re-clamp against the configured
            # bounds so a future config change to make the layer
            # more conservative always wins.
            configured_min = self._config.markov.min_multiplier
            configured_max = self._config.markov.max_multiplier
            multiplier = max(configured_min, min(configured_max, multiplier))
            return base_lot * multiplier

        return base_lot

    def _get_current_markov_state(self) -> str | None:
        """Classify the current bar into one of the 6 Markov states.

        Returns ``None`` until there is enough history for
        ``volatility_regime`` and ``trend_regime`` to produce a
        meaningful classification. The merge rules are:

        * ``extreme`` volatility → ``high`` (collapse the 4-state
          volatility into the 3-state lattice the Markov filter
          expects).
        * ``neutral`` trend → ``ranging`` (same collapse for the
          3-state trend space into the 2-state lattice).

        The result is a ``"{vol}_{trend}"`` label, e.g. ``"normal_ranging"``.
        """
        if len(self._atr_history) < 50:
            return None
        if (
            len(self._high_history) < self._config.regime.adx_period + 1
            or len(self._low_history) < self._config.regime.adx_period + 1
            or len(self._close_history) < self._config.regime.adx_period + 1
        ):
            return None

        vol_r = calc_volatility_regime(
            list(self._atr_history),
            lookback=self._config.regime.atr_lookback,
        )
        trend_r = calc_trend_regime(
            list(self._high_history),
            list(self._low_history),
            list(self._close_history),
            adx_period=self._config.regime.adx_period,
        )

        # Merge to the 6-state space the Markov filter is built over.
        vol_str = vol_r.regime.value
        if vol_str == "extreme":
            vol_str = "high"
        trend_str = trend_r.direction.value
        if trend_str == "neutral":
            trend_str = "ranging"

        return f"{vol_str}_{trend_str}"
