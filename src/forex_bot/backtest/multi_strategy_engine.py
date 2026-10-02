"""Multi-strategy backtest engine with Kelly Criterion overlay.

Refactored to compose with canonical ``engine.base.EngineCore`` and
``engine.mixins.ProgressiveSLMixin``, eliminating ~400 lines of
duplicated infrastructure code (trade management, daily tracking,
metrics, Sharpe calculation, etc.).

The multi-strategy-specific concerns retained here are:

* ``MultiStrategyConfig``        — weights, confidence threshold
* ``KellyConfig`` / Kelly overlay — diminish lot size based on edge
* ``run_all_strategies()``        — per-strategy results with last_signal
* ``run_combined_strategies()``   — combined run with individual results
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import BacktestConfig, BacktestMetrics
from core.pip import PipCalculator
from core.types import (
    Bar,
    ExitReason,
    MarketState,
    SimulatedTrade,
    StrategySignal,
    TradeDirection,
    TradeOutcome,
)
from engine.base import EngineCore, determine_session
from engine.mixins import ProgressiveSLMixin
from quant.position_sizing import kelly_criterion
from signal_engine.risk_sizer import ConfidencePositionSizer

from .strategies import ISignalStrategy

# ────────────────────────────────────────────────────────────────────
# Configuration dataclasses
# ────────────────────────────────────────────────────────────────────


@dataclass
class KellyConfig:
    """Kelly Criterion overlay for backtest position sizing.

    In combined-strategy mode the blended edge from multiple signals is used
    to compute a Half-Kelly fraction which acts as a conservative diminisher
    on the lot size produced by the confidence sizer.  Kelly never increases
    position size — it only maintains or reduces it.
    """

    enabled: bool = True
    min_trades: int = 20  # minimum closed trades before Kelly activates
    rolling_window: int = 50  # recent trades used for edge estimation


@dataclass
class MultiStrategyConfig:
    weights: list[float] | None = None
    min_combined_confidence: float = 0.50
    use_confluence_scoring: bool = True

    def __post_init__(self):
        if self.weights is None:
            self.weights = [1.0, 1.0, 1.0, 1.0, 1.0]


@dataclass
class StrategyBacktestResult:
    strategy_name: str
    metrics: BacktestMetrics
    last_signal: StrategySignal | None


# ────────────────────────────────────────────────────────────────────
# Engine — composes EngineCore + ProgressiveSLMixin
# ────────────────────────────────────────────────────────────────────


class MultiStrategyBacktestEngine(EngineCore, ProgressiveSLMixin):
    """Multi-strategy backtest engine with Kelly overlay.

    Composes ``EngineCore`` (trade lifecycle, metrics, daily tracking)
    and ``ProgressiveSLMixin`` (progressive SL, trade-exit checks) from
    the canonical engine package, adding only multi-strategy signal
    combination and Kelly-based position diminishing.
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategies: list[ISignalStrategy],
        multi_config: MultiStrategyConfig | None = None,
        risk_sizer: ConfidencePositionSizer | None = None,
        kelly_config: KellyConfig | None = None,
    ):
        EngineCore.__init__(self, config)
        ProgressiveSLMixin.__init__(self, config)
        self.strategies = strategies
        self.multi_config = multi_config or MultiStrategyConfig()
        self.risk_sizer = risk_sizer or ConfidencePositionSizer(account_size=config.starting_balance)
        self._kelly_config = kelly_config or KellyConfig()
        self._kelly_closed_trades: list[SimulatedTrade] = []
        self._kelly_skips = 0

    # ── public API ──────────────────────────────────────────────────

    def run_all_strategies(self, bars: list[Bar]) -> dict[str, StrategyBacktestResult]:
        """Run each strategy independently, returning per-strategy results."""
        results: dict[str, StrategyBacktestResult] = {}
        for strategy in self.strategies:
            result = self._run_single_strategy(strategy, bars)
            results[strategy.name] = result
        return results

    def run_combined_strategies(
        self, strategies: list[ISignalStrategy], bars: list[Bar]
    ) -> tuple[dict[str, StrategyBacktestResult], BacktestMetrics]:
        """Run strategies with signal combination, also returning individual results."""
        individual = self.run_all_strategies(bars)

        self._reset()
        self._kelly_closed_trades = []
        self._kelly_skips = 0

        trades: list[SimulatedTrade] = []
        equity_curve: list[float] = [self.balance]
        open_trades: list[SimulatedTrade] = []

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            self._check_open_trades_kelly(open_trades, bar, i, trades, equity_curve)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bar.time),
                )

                all_signals: list[StrategySignal] = []
                for strategy in strategies:
                    signal = strategy.evaluate(state)
                    if signal is not None:
                        all_signals.append(signal)

                if all_signals:
                    combined = self._combine_signals(all_signals)
                    if combined is not None and combined.confidence >= self.multi_config.min_combined_confidence:
                        trade = self._open_trade_kelly(combined, bar, i)
                        if trade is not None:
                            open_trades.append(trade)

            equity_curve.append(self.balance)

        trades.extend(self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close))
        combined_metrics = self._calculate_metrics(trades, equity_curve)
        return individual, combined_metrics

    # ── single-strategy runner (private) ────────────────────────────

    def _run_single_strategy(self, strategy: ISignalStrategy, bars: list[Bar]) -> StrategyBacktestResult:
        if len(bars) < self.config.min_bars_before_signal:
            raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

        self._reset()
        self._kelly_closed_trades = []
        self._kelly_skips = 0

        trades: list[SimulatedTrade] = []
        equity_curve: list[float] = [self.balance]
        open_trades: list[SimulatedTrade] = []
        last_signal: StrategySignal | None = None

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            self._check_open_trades_kelly(open_trades, bar, i, trades, equity_curve)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bar.time),
                )

                signal = strategy.evaluate(state)
                if signal is not None and signal.confidence >= self.config.min_confidence:
                    trade = self._open_trade_kelly(signal, bar, i)
                    if trade is not None:
                        open_trades.append(trade)
                        last_signal = signal

            equity_curve.append(self.balance)

        trades.extend(self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close))
        metrics = self._calculate_metrics(trades, equity_curve)
        return StrategyBacktestResult(strategy_name=strategy.name, metrics=metrics, last_signal=last_signal)

    # ── signal combination (multi-strategy specific) ────────────────

    def _combine_signals(self, signals: list[StrategySignal]) -> StrategySignal | None:
        if len(signals) == 0:
            return None

        long_signals = [s for s in signals if s.direction == TradeDirection.LONG]
        short_signals = [s for s in signals if s.direction == TradeDirection.SHORT]

        long_conf = sum(s.confidence for s in long_signals) / max(1, len(long_signals))
        short_conf = sum(s.confidence for s in short_signals) / max(1, len(short_signals))

        if long_conf > short_conf and long_conf >= self.multi_config.min_combined_confidence:
            direction = TradeDirection.LONG
            confidence = long_conf
            entry = sum(s.entry_price for s in long_signals) / len(long_signals)
            sl = max(s.stop_loss for s in long_signals)
            tp1 = sum(s.take_profit_1 for s in long_signals) / len(long_signals)
            tp2 = sum(s.take_profit_2 for s in long_signals) / len(long_signals)
            tp3 = sum(s.take_profit_3 for s in long_signals) / len(long_signals)
        elif short_conf > long_conf and short_conf >= self.multi_config.min_combined_confidence:
            direction = TradeDirection.SHORT
            confidence = short_conf
            entry = sum(s.entry_price for s in short_signals) / len(short_signals)
            sl = min(s.stop_loss for s in short_signals)
            tp1 = sum(s.take_profit_1 for s in short_signals) / len(short_signals)
            tp2 = sum(s.take_profit_2 for s in short_signals) / len(short_signals)
            tp3 = sum(s.take_profit_3 for s in short_signals) / len(short_signals)
        else:
            return None

        rationale = f"Combined {len(signals)} signals: {len(long_signals)} long, {len(short_signals)} short"

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

    # ── Kelly-aware trade opening ───────────────────────────────────

    def _compute_kelly_multiplier(self, closed_trades: list[SimulatedTrade]) -> float:
        """Compute a Kelly-based position multiplier from recent closed trades.

        Returns a value in [0.0, 1.0] where 1.0 means full confidence size
        (strong edge) and 0.0 means no edge — skip the trade.
        """
        recent = closed_trades[-self._kelly_config.rolling_window :]
        wins = [t for t in recent if t.outcome == TradeOutcome.WIN]
        losses = [t for t in recent if t.outcome == TradeOutcome.LOSS]

        n = len(recent)
        win_rate = len(wins) / n if n > 0 else 0.0
        avg_win = sum(t.profit_loss for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(t.profit_loss for t in losses) / len(losses)) if losses else 0.0

        if avg_win <= 0.0 or win_rate <= 0.0:
            return 0.0
        kelly_frac = kelly_criterion(win_rate, avg_win, avg_loss)
        if kelly_frac <= 0.0:
            return 0.0
        # Normalize: kelly_criterion returns [0.0, 0.5].  Map to [0.0, 1.0].
        return min(kelly_frac / 0.5, 1.0)

    def _open_trade_kelly(self, signal: StrategySignal, bar: Bar, bar_index: int) -> SimulatedTrade | None:
        """Open a trade with confidence-based sizing + Kelly overlay.

        Uses ``ConfidencePositionSizer`` (unlike ``EngineCore._open_trade``
        which uses fixed ``risk_per_trade_pct``) and applies Kelly-based
        position diminishing when enough trades have been recorded.
        """
        risk = abs(signal.entry_price - signal.stop_loss)
        if risk == 0:
            return None

        pip_value = PipCalculator.pip_value(signal.entry_price)
        stop_pips = risk / pip_value

        vol_multiplier = 0.5 if signal.is_volatile else 1.0
        risk_amount = self.risk_sizer.get_risk_amount(signal.confidence) * vol_multiplier
        lot_size = self.risk_sizer.get_lot_size(signal.confidence, stop_pips, pip_value)
        if lot_size <= 0:
            return None

        if getattr(self.config, "round_trip_spread", True):
            effective_entry = signal.entry_price
            adjusted_risk = risk
        else:
            spread_cost = self.config.spread_pips * pip_value
            slippage_cost = self.config.slippage_pips * pip_value
            total_cost = spread_cost + slippage_cost
            effective_entry = (
                signal.entry_price + total_cost
                if signal.direction == TradeDirection.LONG
                else signal.entry_price - total_cost
            )
            adjusted_risk = abs(effective_entry - signal.stop_loss)

        if adjusted_risk == 0:
            return None

        lot_size = risk_amount / adjusted_risk
        margin_required = lot_size * effective_entry / self.config.leverage
        if margin_required > self.balance:
            return None

        # Kelly overlay — diminish lot size based on estimated edge
        if self._kelly_config.enabled and len(self._kelly_closed_trades) >= self._kelly_config.min_trades:
            kelly_mult = self._compute_kelly_multiplier(self._kelly_closed_trades)
            if kelly_mult <= 0.0:
                self._kelly_skips += 1
                return None  # Kelly says no edge, skip trade
            lot_size *= kelly_mult

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

    # ── Kelly-aware trade checking ──────────────────────────────────

    def _check_open_trades_kelly(
        self,
        open_trades: list[SimulatedTrade],
        bar: Bar,
        bar_index: int,
        closed_trades: list[SimulatedTrade],
        equity_curve: list[float],
    ) -> None:
        """Check open trades for exits, tracking Kelly closed trades.

        Delegates to ``ProgressiveSLMixin._check_open_trades`` for the
        actual SL/exit logic, then records closed trades for Kelly.
        """
        trades_before = len(closed_trades)
        self._check_open_trades(open_trades, bar, bar_index, closed_trades, equity_curve)
        # Track newly closed trades for Kelly overlay
        for t in closed_trades[trades_before:]:
            self._kelly_closed_trades.append(t)
