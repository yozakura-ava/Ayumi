from __future__ import annotations

from enum import StrEnum

from core.pip import PipCalculator
from core.types import Bar, ExitReason, SimulatedTrade, StrategySignal, TradeDirection


class CombineMethod(StrEnum):
    WEIGHTED = "weighted"
    VOTED = "voted"
    BEST = "best"


class ProgressiveSLMixin:
    def __init__(self, config):
        self.config = config

    def _init_trade_tracking(self, trade: SimulatedTrade) -> None:
        if not hasattr(trade, "_sl_moved_to_be"):
            trade._sl_moved_to_be = False
            trade._sl_moved_to_tp1 = False

    def _progressive_sl_update(self, trade: SimulatedTrade, bar: Bar) -> None:
        self._init_trade_tracking(trade)
        pip_size = PipCalculator.pip_value(trade.entry_price)

        if trade.direction == TradeDirection.LONG:
            if bar.high >= trade.take_profit_2 and not trade._sl_moved_to_tp1:
                trade.stop_loss = trade.take_profit_1
                trade._sl_moved_to_tp1 = True
                trade._sl_moved_to_be = True
            elif bar.high >= trade.take_profit_1 and not trade._sl_moved_to_be:
                trade.stop_loss = max(trade.stop_loss, trade.entry_price + pip_size)
                trade._sl_moved_to_be = True
        else:
            if bar.low <= trade.take_profit_2 and not trade._sl_moved_to_tp1:
                trade.stop_loss = trade.take_profit_1
                trade._sl_moved_to_tp1 = True
                trade._sl_moved_to_be = True
            elif bar.low <= trade.take_profit_1 and not trade._sl_moved_to_be:
                trade.stop_loss = min(trade.stop_loss, trade.entry_price - pip_size)
                trade._sl_moved_to_be = True

    def _check_trade_exit(self, trade: SimulatedTrade, bar: Bar) -> tuple[bool, float, ExitReason]:
        if trade.direction == TradeDirection.LONG:
            if bar.low <= trade.stop_loss:
                return (True, trade.stop_loss, ExitReason.STOP_LOSS)
            if bar.high >= trade.take_profit_3:
                return (True, trade.take_profit_3, ExitReason.TAKE_PROFIT_3)
            if bar.high >= trade.take_profit_2:
                return (True, trade.take_profit_2, ExitReason.TAKE_PROFIT_2)
            if bar.high >= trade.take_profit_1:
                return (True, trade.take_profit_1, ExitReason.TAKE_PROFIT_1)
        else:
            if bar.high >= trade.stop_loss:
                return (True, trade.stop_loss, ExitReason.STOP_LOSS)
            if bar.low <= trade.take_profit_3:
                return (True, trade.take_profit_3, ExitReason.TAKE_PROFIT_3)
            if bar.low <= trade.take_profit_2:
                return (True, trade.take_profit_2, ExitReason.TAKE_PROFIT_2)
            if bar.low <= trade.take_profit_1:
                return (True, trade.take_profit_1, ExitReason.TAKE_PROFIT_1)
        return (False, 0.0, ExitReason.STOP_LOSS)

    def _check_open_trades(
        self,
        open_trades: list[SimulatedTrade],
        bar: Bar,
        bar_index: int,
        closed_trades: list[SimulatedTrade],
        equity_curve: list[float],
    ) -> None:
        to_close: list[SimulatedTrade] = []
        for trade in open_trades:
            self._progressive_sl_update(trade, bar)
            hit, exit_price, reason = self._check_trade_exit(trade, bar)
            if hit:
                self._close_trade(trade, bar_index, bar.time, exit_price, reason)
                closed_trades.append(trade)
                to_close.append(trade)
                equity_curve.append(self.balance)
        for t in to_close:
            open_trades.remove(t)


class CombinedSignalMixin:
    def _combine_signals(
        self,
        signals: list[StrategySignal],
        method: CombineMethod = CombineMethod.WEIGHTED,
        min_confidence: float = 0.50,
    ) -> StrategySignal | None:
        if not signals:
            return None

        long_signals = [s for s in signals if s.direction == TradeDirection.LONG]
        short_signals = [s for s in signals if s.direction == TradeDirection.SHORT]

        if not long_signals and not short_signals:
            return None

        if method == CombineMethod.WEIGHTED:
            return self._combine_weighted(long_signals, short_signals, min_confidence)
        elif method == CombineMethod.VOTED:
            return self._combine_voted(long_signals, short_signals, min_confidence)
        elif method == CombineMethod.BEST:
            return self._combine_best(signals, min_confidence)
        return None

    def _combine_weighted(
        self,
        long_signals: list[StrategySignal],
        short_signals: list[StrategySignal],
        min_confidence: float,
    ) -> StrategySignal | None:
        long_conf = sum(s.confidence for s in long_signals) / len(long_signals) if long_signals else 0.0
        short_conf = sum(s.confidence for s in short_signals) / len(short_signals) if short_signals else 0.0

        if long_conf > short_conf and long_conf >= min_confidence:
            return self._aggregate_direction(long_signals, TradeDirection.LONG, long_conf)
        elif short_conf > long_conf and short_conf >= min_confidence:
            return self._aggregate_direction(short_signals, TradeDirection.SHORT, short_conf)
        return None

    def _combine_voted(
        self,
        long_signals: list[StrategySignal],
        short_signals: list[StrategySignal],
        min_confidence: float,
    ) -> StrategySignal | None:
        if len(long_signals) > len(short_signals):
            return self._aggregate_direction(long_signals, TradeDirection.LONG, min_confidence)
        elif len(short_signals) > len(long_signals):
            return self._aggregate_direction(short_signals, TradeDirection.SHORT, min_confidence)
        return None

    def _combine_best(
        self,
        signals: list[StrategySignal],
        min_confidence: float,
    ) -> StrategySignal | None:
        best = max(signals, key=lambda s: s.confidence)
        if best.confidence >= min_confidence:
            return best
        return None

    @staticmethod
    def _aggregate_direction(
        signals: list[StrategySignal],
        direction: TradeDirection,
        confidence: float,
    ) -> StrategySignal:
        entry = sum(s.entry_price for s in signals) / len(signals)
        if direction == TradeDirection.LONG:
            sl = max(s.stop_loss for s in signals)
        else:
            sl = min(s.stop_loss for s in signals)
        tp1 = sum(s.take_profit_1 for s in signals) / len(signals)
        tp2 = sum(s.take_profit_2 for s in signals) / len(signals)
        tp3 = sum(s.take_profit_3 for s in signals) / len(signals)
        rationale = f"Combined {len(signals)} signals ({direction.value}, conf={confidence:.2f})"
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
