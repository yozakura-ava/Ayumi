from dataclasses import dataclass, field
from datetime import datetime

from ..engine import Bar, ExitReason, StrategySignal, TradeDirection
from .config import TradeManagementConfig
from .exit_refinement import ExitRefiner, ExitRefinerState
from .partial_exit import PartialExitAction, PartialExitManager, TierState
from .session_filter import SessionFilter, SessionFilterResult
from .trailing_stop import TrailingStopManager, TrailingStopState


class TradeAction:
    OPEN = "open"
    CLOSE_FULL = "close_full"
    CLOSE_PARTIAL = "close_partial"
    MODIFY_SL = "modify_sl"
    NO_ACTION = "no_action"


@dataclass
class ManagedTrade:
    entry_bar_index: int
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    original_stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    lot_size: float
    entry_time: datetime
    confidence_score: float
    rationale: str

    current_sl: float = 0.0
    bars_held: int = 0
    partial_closes: list = field(default_factory=list)
    remaining_pct: float = 1.0
    tier_state: TierState | None = None
    trailing_state: TrailingStopState | None = None
    exit_refiner_state: ExitRefinerState | None = None
    is_closed: bool = False
    exit_price: float = 0.0
    exit_reason: ExitReason | None = None
    realized_pnl: float = 0.0
    partial_realized_pnl: float = 0.0

    def __post_init__(self):
        if self.current_sl == 0.0:
            self.current_sl = self.stop_loss


@dataclass
class ManagementResult:
    action: str
    exit_price: float = 0.0
    close_pct: float = 0.0
    new_sl: float = 0.0
    reason: ExitReason | None = None
    message: str = ""


class TradeManager:
    def __init__(self, config: TradeManagementConfig | None = None):
        self.config = config or TradeManagementConfig()
        self._partial_exit = PartialExitManager(
            enabled=self.config.partial_exit.enabled,
            tiers=self.config.partial_exit.tiers,
            final_trail=self.config.partial_exit.final_trail,
        )
        self._trailing_stop = TrailingStopManager(self.config.trailing_stop)
        self._session_filter = SessionFilter(
            enabled=self.config.session_filter.enabled,
            allow_entry_sessions=self.config.session_filter.allow_entry_sessions,
            hold_through_sessions=self.config.session_filter.hold_through_sessions,
            weekend_close_hour_utc=self.config.session_filter.weekend_close_hour_utc,
            weekend_close_minute_utc=self.config.session_filter.weekend_close_minute_utc,
            news_buffer_bars=self.config.session_filter.news_buffer_bars,
            news_buffer_on_entry=self.config.session_filter.news_buffer_on_entry,
        )
        self._exit_refiner = ExitRefiner(
            enabled=self.config.exit_refinement.enabled,
            max_bars_to_tp1=self.config.exit_refinement.max_bars_to_tp1,
            momentum_exit_enabled=self.config.exit_refinement.momentum_exit_enabled,
            momentum_lookback=self.config.exit_refinement.momentum_lookback,
            momentum_reversal_threshold=self.config.exit_refinement.momentum_reversal_threshold,
            spread_filter_enabled=self.config.exit_refinement.spread_filter_enabled,
            max_spread_atr_pct=self.config.exit_refinement.max_spread_atr_pct,
        )

    def check_entry_allowed(
        self,
        bar: Bar,
        signal: StrategySignal,
        atr: float,
        spread_pips: float = 0.5,
        pair: str = "EURUSD",
    ) -> SessionFilterResult:
        session_result = self._session_filter.check_entry(bar, pair)
        if not session_result.allow_entry:
            return session_result

        if not self._exit_refiner.check_entry_spread(bar, atr, spread_pips):
            return SessionFilterResult(
                allow_entry=False,
                reason=f"Spread too wide: {spread_pips} pips exceeds {self.config.exit_refinement.max_spread_atr_pct:.0%} of ATR",  # noqa: E501
            )

        return SessionFilterResult(allow_entry=True)

    def open_trade(self, bar_index: int, bar: Bar, signal: StrategySignal, lot_size: float) -> ManagedTrade:
        trade = ManagedTrade(
            entry_bar_index=bar_index,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            original_stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            lot_size=lot_size,
            entry_time=bar.time,
            confidence_score=signal.confidence,
            rationale=signal.rationale,
            tier_state=self._partial_exit.create_state(),
            trailing_state=self._trailing_stop.create_state(signal.direction, signal.entry_price, signal.stop_loss),
            exit_refiner_state=self._exit_refiner.create_state(),
        )
        return trade

    def on_bar(
        self,
        trade: ManagedTrade,
        bar: Bar,
        bar_index: int,
        atr: float,
        recent_bars: list[Bar] | None = None,
    ) -> ManagementResult:
        if trade.is_closed:
            return ManagementResult(action=TradeAction.NO_ACTION)

        trade.bars_held = bar_index - trade.entry_bar_index

        session_hold = self._session_filter.check_hold(bar, trade.entry_time, trade.direction)
        if session_hold.force_close:
            trade.is_closed = True
            trade.exit_price = bar.close
            trade.exit_reason = ExitReason.END_OF_DATA
            return ManagementResult(
                action=TradeAction.CLOSE_FULL,
                exit_price=bar.close,
                reason=ExitReason.END_OF_DATA,
                message=session_hold.reason,
            )

        if self._check_stop_loss(trade, bar):
            trade.is_closed = True
            trade.exit_price = trade.current_sl
            trade.exit_reason = ExitReason.STOP_LOSS
            return ManagementResult(
                action=TradeAction.CLOSE_FULL,
                exit_price=trade.current_sl,
                reason=ExitReason.STOP_LOSS,
                message="Stop loss hit",
            )

        if trade.tier_state is None or trade.trailing_state is None or trade.exit_refiner_state is None:
            return ManagementResult(action=TradeAction.NO_ACTION)

        tier_state = trade.tier_state
        trailing_state = trade.trailing_state
        exit_refiner_state = trade.exit_refiner_state

        partial_result = self._partial_exit.evaluate(
            bar,
            tier_state,
            trade.direction,
            trade.entry_price,
            trade.stop_loss,
            trade.take_profit_1,
            trade.take_profit_2,
            trade.take_profit_3,
            trade.current_sl,
        )

        if partial_result.action == PartialExitAction.FULL_CLOSE:
            trade.is_closed = True
            trade.exit_price = partial_result.exit_price
            trade.exit_reason = partial_result.reason
            trade.remaining_pct = 0.0
            return ManagementResult(
                action=TradeAction.CLOSE_FULL,
                exit_price=partial_result.exit_price,
                close_pct=1.0,
                reason=partial_result.reason,
                message=f"Full close at {partial_result.reason.value}",
            )

        if partial_result.action == PartialExitAction.PARTIAL_CLOSE:
            trade.partial_closes.append(
                {
                    "bar_index": bar_index,
                    "price": partial_result.exit_price,
                    "pct": partial_result.close_pct,
                    "reason": partial_result.reason.value,
                }
            )
            trade.remaining_pct = tier_state.remaining_pct

            if partial_result.tier_reached.value >= 1:
                exit_refiner_state.tp1_hit = True

            result = ManagementResult(
                action=TradeAction.CLOSE_PARTIAL,
                exit_price=partial_result.exit_price,
                close_pct=partial_result.close_pct,
                reason=partial_result.reason,
                message=f"Partial close {partial_result.close_pct:.0%} at {partial_result.reason.value}",
            )

            if partial_result.new_sl is not None:
                trade.current_sl = partial_result.new_sl
                result.action = TradeAction.CLOSE_PARTIAL
                result.new_sl = partial_result.new_sl

            return result

        if partial_result.action == PartialExitAction.ENABLE_TRAIL:
            trailing_state.is_active = True

        if trailing_state.is_active and partial_result.tier_reached.value >= self.config.trailing_stop.only_after_tier:
            trail_result = self._trailing_stop.evaluate(bar, trailing_state, trade.direction, atr, trade.entry_price)
            if trail_result.triggered:
                trade.is_closed = True
                trade.exit_price = trail_result.exit_price
                trade.exit_reason = ExitReason.STOP_LOSS
                return ManagementResult(
                    action=TradeAction.CLOSE_FULL,
                    exit_price=trail_result.exit_price,
                    reason=ExitReason.STOP_LOSS,
                    message="Trailing stop triggered",
                )
            if trail_result.sl_updated:
                trade.current_sl = trail_result.new_sl
                return ManagementResult(
                    action=TradeAction.MODIFY_SL,
                    new_sl=trail_result.new_sl,
                    message=f"Trailing SL updated to {trail_result.new_sl:.5f}",
                )

        refiner_result = self._exit_refiner.on_bar(bar, exit_refiner_state, trade.direction, atr, recent_bars)
        if refiner_result.should_exit:
            trade.is_closed = True
            trade.exit_price = refiner_result.exit_price
            trade.exit_reason = refiner_result.reason
            return ManagementResult(
                action=TradeAction.CLOSE_FULL,
                exit_price=refiner_result.exit_price,
                reason=refiner_result.reason,
                message=refiner_result.message,
            )

        return ManagementResult(action=TradeAction.NO_ACTION)

    def _check_stop_loss(self, trade: ManagedTrade, bar: Bar) -> bool:
        if trade.direction == TradeDirection.LONG:
            return bar.low <= trade.current_sl
        else:
            return bar.high >= trade.current_sl
