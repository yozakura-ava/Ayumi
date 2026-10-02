"""Amalgamation backtest engine and signal combination system.

Refactored to compose with canonical ``engine.base.EngineCore`` and
``engine.mixins.ProgressiveSLMixin``, eliminating ~300 lines of
duplicated infrastructure code.

The amalgamation-specific concerns retained here are:

* ``AmalgamationConfig``     — voting method, confidence, confluence
* ``ComponentExtractor``     — strategy profiling and extraction
* ``AmalgamationEngine``     — pure signal combination (no engine infra)
* ``AmalgamatedBacktestEngine`` — backtest runner using amalgamation logic
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.config import BacktestConfig, BacktestMetrics
from core.pip import PipCalculator
from core.types import (
    Bar,
    ExitReason,
    MarketState,
    SessionType,
    SimulatedTrade,
    StrategySignal,
    TradeDirection,
    TradeOutcome,
)
from engine.base import EngineCore, determine_session
from engine.mixins import ProgressiveSLMixin
from signal_engine.risk_sizer import ConfidencePositionSizer

from .strategies import ISignalStrategy

# ────────────────────────────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────────────────────────────


class VotingMethod(Enum):
    VOTE = "vote"
    WEIGHTED = "weighted"
    CONFLUENCE = "confluence"


class ConfidenceMethod(Enum):
    MEAN = "mean"
    WEIGHTED = "weighted"
    CONFLUENCE = "confluence"


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

ICT_SMC_COMPONENT_TYPES = [
    "order_block",
    "fair_value_gap",
    "liquidity_sweep",
    "market_structure",
    "premium_discount",
    "h4_context",
]

INDICATOR_STRATEGY_PATTERNS = [
    "MA Crossover",
    "RSI Divergence",
    "Momentum ROC",
    "Bollinger Band",
    "S/R Breakout",
]


# ────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────


@dataclass
class AmalgamationConfig:
    voting_method: VotingMethod = VotingMethod.WEIGHTED
    confidence_method: ConfidenceMethod = ConfidenceMethod.WEIGHTED
    min_combined_confidence: float = 0.50
    min_confluence: int = 2
    strategy_weights: dict[str, float] = field(default_factory=dict)
    confluence_bonus: float = 0.10
    session_filter_enabled: bool = True
    regime_filter_enabled: bool = False
    allowed_sessions: list[SessionType] = field(
        default_factory=lambda: [
            SessionType.LONDON,
            SessionType.NY_AM,
            SessionType.NY_PM,
        ]
    )
    ict_smc_only: bool = True

    def get_weight(self, strategy_name: str) -> float:
        return self.strategy_weights.get(strategy_name, 1.0)

    def is_indicator_strategy(self, strategy_name: str) -> bool:
        for pattern in INDICATOR_STRATEGY_PATTERNS:
            if pattern.lower() in strategy_name.lower():
                return True
        return False


@dataclass
class ComponentProfile:
    name: str
    component_type: str
    sub_components: list[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    strategy_name: str
    direction: TradeDirection | None
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rationale: str
    weight: float = 1.0


# ────────────────────────────────────────────────────────────────────
# Component extractor (unchanged — pure logic, no engine infra)
# ────────────────────────────────────────────────────────────────────


class ComponentExtractor:
    COMPONENT_TYPES = [
        "signal_generator",
        "exit_condition",
        "confidence_modifier",
        "risk_adjustment",
    ]

    def extract(self, strategy: ISignalStrategy, state: MarketState) -> ExtractionResult | None:
        signal = strategy.evaluate(state)
        if signal is None:
            return None

        return ExtractionResult(
            strategy_name=strategy.name,
            direction=signal.direction,
            confidence=signal.confidence,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            rationale=signal.rationale,
        )

    def profile_strategy(self, strategy: ISignalStrategy) -> ComponentProfile:
        name = strategy.name
        if "ICT" in name or "SMC" in name or "Confluence" in name:
            return ComponentProfile(
                name=name,
                component_type="ict_smc_confluence",
                sub_components=ICT_SMC_COMPONENT_TYPES,
            )
        return ComponentProfile(name=name, component_type="unknown")


# ────────────────────────────────────────────────────────────────────
# Amalgamation engine — pure signal combiner (no engine infrastructure)
# ────────────────────────────────────────────────────────────────────


class AmalgamationEngine:
    """Pure signal-combination logic (voting, confidence, level aggregation).

    This is not a backtest engine — it has no trade management, balance
    tracking, or metrics.  It is used by ``AmalgamatedBacktestEngine``
    to combine multiple strategy signals before opening trades.
    """

    def __init__(self, config: AmalgamationConfig):
        self.config = config

    def combine(self, signals: list[StrategySignal], state: MarketState) -> StrategySignal | None:
        if not signals:
            return None

        filtered = self._apply_meta_filters(signals, state)
        if len(filtered) < self.config.min_confluence:
            return None

        direction = self._vote_direction(filtered)
        if direction is None or direction == TradeDirection.NEUTRAL:
            return None

        confidence = self._compute_confidence(filtered, direction)
        if confidence < self.config.min_combined_confidence:
            return None

        direction_signals = [s for s in filtered if s.direction == direction]
        if not direction_signals:
            return None

        entry, sl, tp1, tp2, tp3 = self._aggregate_levels(direction_signals, direction)
        rationale = self._build_rationale(direction_signals, direction, confidence)

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )

    def _apply_meta_filters(self, signals: list[StrategySignal], state: MarketState) -> list[StrategySignal]:
        filtered = signals

        if self.config.session_filter_enabled:
            filtered = [s for s in filtered if state.current_session in self.config.allowed_sessions]

        return filtered

    def _vote_direction(self, signals: list[StrategySignal]) -> TradeDirection:
        long_signals = [s for s in signals if s.direction == TradeDirection.LONG]
        short_signals = [s for s in signals if s.direction == TradeDirection.SHORT]

        if not long_signals and not short_signals:
            return TradeDirection.NEUTRAL

        if self.config.voting_method == VotingMethod.VOTE:
            long_count = len(long_signals)
            short_count = len(short_signals)
            if long_count > short_count:
                return TradeDirection.LONG
            elif short_count > long_count:
                return TradeDirection.SHORT
            return TradeDirection.NEUTRAL

        elif self.config.voting_method == VotingMethod.WEIGHTED:
            long_weight = sum(
                self.config.get_weight(s.rationale.split(":")[0].strip() if ":" in s.rationale else "") * s.confidence
                for s in long_signals
            )
            short_weight = sum(
                self.config.get_weight(s.rationale.split(":")[0].strip() if ":" in s.rationale else "") * s.confidence
                for s in short_signals
            )
            if long_weight > short_weight:
                return TradeDirection.LONG
            elif short_weight > long_weight:
                return TradeDirection.SHORT
            return TradeDirection.NEUTRAL

        else:
            long_conf = sum(s.confidence for s in long_signals) / max(1, len(long_signals))
            short_conf = sum(s.confidence for s in short_signals) / max(1, len(short_signals))
            if long_conf > short_conf:
                return TradeDirection.LONG
            elif short_conf > long_conf:
                return TradeDirection.SHORT
            return TradeDirection.NEUTRAL

    def _compute_confidence(self, signals: list[StrategySignal], direction: TradeDirection) -> float:
        direction_signals = [s for s in signals if s.direction == direction]

        if self.config.confidence_method == ConfidenceMethod.MEAN:
            return sum(s.confidence for s in direction_signals) / len(direction_signals)

        elif self.config.confidence_method == ConfidenceMethod.WEIGHTED:
            total_weight = sum(self.config.get_weight("default") for _ in direction_signals)
            weighted_sum = sum(s.confidence * self.config.get_weight("default") for s in direction_signals)
            return weighted_sum / total_weight if total_weight > 0 else 0.0

        else:
            base = sum(s.confidence for s in direction_signals) / len(direction_signals)
            bonus = 1.0 + self.config.confluence_bonus * (len(direction_signals) - 1)
            return min(0.99, base * bonus)

    def _aggregate_levels(
        self, signals: list[StrategySignal], direction: TradeDirection
    ) -> tuple[float, float, float, float, float]:
        if not signals:
            return (0, 0, 0, 0, 0)

        entry = sum(s.entry_price for s in signals) / len(signals)

        if direction == TradeDirection.LONG:
            sl = max(s.stop_loss for s in signals)
        else:
            sl = min(s.stop_loss for s in signals)

        tp1 = sum(s.take_profit_1 for s in signals) / len(signals)
        tp2 = sum(s.take_profit_2 for s in signals) / len(signals)
        tp3 = sum(s.take_profit_3 for s in signals) / len(signals)

        return (entry, sl, tp1, tp2, tp3)

    def _build_rationale(
        self,
        signals: list[StrategySignal],
        direction: TradeDirection,
        confidence: float,
    ) -> str:
        names = []
        for s in signals:
            parts = s.rationale.split(":")
            name = parts[0].strip() if parts else "Unknown"
            names.append(name)

        dir_label = "LONG" if direction == TradeDirection.LONG else "SHORT"
        return f"Amalgamated {dir_label} (conf={confidence:.2f}): {', '.join(names)}"


# ────────────────────────────────────────────────────────────────────
# AmalgamatedBacktestEngine — composes EngineCore + ProgressiveSLMixin
# ────────────────────────────────────────────────────────────────────


class AmalgamatedBacktestEngine(EngineCore, ProgressiveSLMixin):
    """Backtest engine using amalgamation-based signal combination.

    Composes ``EngineCore`` (trade lifecycle, metrics, daily tracking)
    and ``ProgressiveSLMixin`` (progressive SL, trade-exit checks) from
    the canonical engine package, using ``AmalgamationEngine`` for
    signal combination and ``ConfidencePositionSizer`` for risk sizing.
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategies: list[ISignalStrategy],
        amalgamation_config: AmalgamationConfig | None = None,
        risk_sizer: ConfidencePositionSizer | None = None,
    ):
        EngineCore.__init__(self, config)
        ProgressiveSLMixin.__init__(self, config)
        self.amalgamation = amalgamation_config if amalgamation_config else AmalgamationConfig()
        self.risk_sizer = risk_sizer or ConfidencePositionSizer(account_size=config.starting_balance)
        self.extractor = ComponentExtractor()
        if self.amalgamation.ict_smc_only:
            filtered = [s for s in strategies if not self.amalgamation.is_indicator_strategy(s.name)]
            if filtered:
                self.strategies = filtered
            else:
                from .ict_smc.strategy_adapter import ICTSMCStrategy

                self.strategies = [ICTSMCStrategy()]
                if self.amalgamation.min_confluence > 1:
                    self.amalgamation.min_confluence = 1
        else:
            self.strategies = strategies

    # ── public API ──────────────────────────────────────────────────

    def run(self, bars: list[Bar]) -> BacktestMetrics:
        if len(bars) < self.config.min_bars_before_signal:
            raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

        self._reset()
        trades: list[SimulatedTrade] = []
        equity_curve: list[float] = [self.balance]
        open_trades: list[SimulatedTrade] = []
        rejected_signals = 0

        engine = AmalgamationEngine(self.amalgamation)

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            self._check_open_trades(open_trades, bar, i, trades, equity_curve)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bar.time),
                )

                all_signals: list[StrategySignal] = []
                for strategy in self.strategies:
                    extraction = self.extractor.extract(strategy, state)
                    if (
                        extraction is not None
                        and extraction.direction is not None
                        and extraction.direction != TradeDirection.NEUTRAL
                    ):
                        dir_val: TradeDirection = extraction.direction
                        all_signals.append(
                            StrategySignal(
                                direction=dir_val,
                                confidence=extraction.confidence,
                                entry_price=extraction.entry_price,
                                stop_loss=extraction.stop_loss,
                                take_profit_1=extraction.take_profit_1,
                                take_profit_2=extraction.take_profit_2,
                                take_profit_3=extraction.take_profit_3,
                                rationale=extraction.rationale,
                            )
                        )

                if all_signals:
                    combined = engine.combine(all_signals, state)
                    if combined is not None:
                        trade = self._open_trade_confidence(combined, bar, i)
                        if trade is not None:
                            open_trades.append(trade)
                        else:
                            rejected_signals += 1
                    else:
                        rejected_signals += 1

            equity_curve.append(self.balance)

        self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, trades)
        self.rejected_signals = rejected_signals
        return self._calculate_metrics(trades, equity_curve)

    def run_individual_and_combined(self, bars: list[Bar]) -> tuple[dict[str, BacktestMetrics], BacktestMetrics]:
        from .multi_strategy_engine import MultiStrategyBacktestEngine

        multi_engine = MultiStrategyBacktestEngine(self.config, self.strategies)
        individual_results = multi_engine.run_all_strategies(bars)
        individual_metrics = {name: result.metrics for name, result in individual_results.items()}

        combined_metrics = self.run(bars)

        return individual_metrics, combined_metrics

    # ── confidence-based trade opening ──────────────────────────────

    def _open_trade_confidence(self, signal: StrategySignal, bar: Bar, bar_index: int) -> SimulatedTrade | None:
        """Open trade with ConfidencePositionSizer.

        Uses ``ConfidencePositionSizer`` (unlike ``EngineCore._open_trade``
        which uses fixed ``risk_per_trade_pct``).
        """
        risk = abs(signal.entry_price - signal.stop_loss)
        if risk == 0:
            return None

        pip_value = PipCalculator.pip_value(signal.entry_price)
        stop_pips = abs(signal.entry_price - signal.stop_loss) / pip_value

        risk_amount = self.risk_sizer.get_risk_amount(signal.confidence)
        lot_size = self.risk_sizer.get_lot_size(signal.confidence, stop_pips, pip_value)
        if lot_size <= 0:
            return None

        spread_cost = (self.config.spread_pips or 0) * pip_value
        effective_entry = (
            signal.entry_price + spread_cost
            if signal.direction == TradeDirection.LONG
            else signal.entry_price - spread_cost
        )
        adjusted_risk = abs(effective_entry - signal.stop_loss)
        if adjusted_risk == 0:
            return None

        lot_size = risk_amount / adjusted_risk
        margin_required = lot_size * effective_entry / self.config.leverage
        if margin_required > self.balance:
            return None

        max_lot_size = self.balance * self.config.leverage / effective_entry
        lot_size = min(lot_size, max_lot_size)

        return SimulatedTrade(
            entry_bar_index=bar_index,
            exit_bar_index=-1,
            direction=signal.direction,
            entry_price=effective_entry,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            exit_price=0.0,
            lot_size=lot_size,
            risk_amount=risk_amount,
            pips=0.0,
            profit_loss=0.0,
            outcome=TradeOutcome.OPEN,
            exit_reason=ExitReason.STOP_LOSS,
            entry_time=bar.time,
            exit_time=bar.time,
            confidence_score=signal.confidence,
            confluence_count=1,
            rationale=signal.rationale,
        )

    # ── amalgamation-specific close-all ─────────────────────────────

    def _close_all_open_trades(
        self,
        open_trades: list[SimulatedTrade],
        bar_index: int,
        exit_time,
        closed_trades: list[SimulatedTrade],
    ) -> list[SimulatedTrade]:
        """Close all remaining open trades at last available price."""
        if closed_trades:
            exit_price = closed_trades[-1].exit_price
        elif open_trades:
            exit_price = open_trades[0].entry_price
        else:
            exit_price = self.config.starting_balance

        closed: list[SimulatedTrade] = []
        for trade in open_trades:
            self._close_trade(trade, bar_index, exit_time, exit_price, ExitReason.END_OF_DATA)
            closed_trades.append(trade)
            closed.append(trade)
        open_trades.clear()
        return closed
