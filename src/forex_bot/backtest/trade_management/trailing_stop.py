from dataclasses import dataclass

from ..engine import Bar, TradeDirection
from .config import TrailingStopConfig, TrailingStopMethod


@dataclass
class TrailingStopState:
    is_active: bool = False
    current_sl: float = 0.0
    bars_since_entry: int = 0
    sar_af: float = 0.02
    sar_ep: float = 0.0
    is_long: bool = True
    highest_price: float = 0.0
    lowest_price: float = 0.0
    original_sl: float = 0.0


@dataclass
class TrailingStopResult:
    triggered: bool = False
    exit_price: float = 0.0
    new_sl: float = 0.0
    sl_updated: bool = False


class TrailingStopManager:
    def __init__(self, config: TrailingStopConfig):
        self.config = config

    def create_state(self, direction: TradeDirection, entry_price: float, stop_loss: float) -> TrailingStopState:
        return TrailingStopState(
            current_sl=stop_loss,
            original_sl=stop_loss,
            is_long=direction == TradeDirection.LONG,
            highest_price=entry_price,
            lowest_price=entry_price,
        )

    def evaluate(
        self,
        bar: Bar,
        state: TrailingStopState,
        direction: TradeDirection,
        atr: float,
        entry_price: float,
    ) -> TrailingStopResult:
        if not self.config.enabled or not state.is_active:
            return TrailingStopResult()

        state.bars_since_entry += 1
        result = TrailingStopResult()

        if self.config.method == TrailingStopMethod.ATR:
            result = self._atr_trail(bar, state, direction, atr)
        elif self.config.method == TrailingStopMethod.STEP:
            result = self._step_trail(bar, state, direction)
        elif self.config.method == TrailingStopMethod.TIME:
            result = self._time_trail(bar, state, direction, atr, entry_price)
        elif self.config.method == TrailingStopMethod.PARABOLIC_SAR:
            result = self._sar_trail(bar, state, direction)

        if result.sl_updated:
            state.current_sl = result.new_sl

        return result

    def _atr_trail(
        self, bar: Bar, state: TrailingStopState, direction: TradeDirection, atr: float
    ) -> TrailingStopResult:
        trail_distance = atr * self.config.atr_multiplier

        if direction == TradeDirection.LONG:
            state.highest_price = max(state.highest_price, bar.high)
            new_sl = state.highest_price - trail_distance

            if bar.low <= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            if new_sl > state.current_sl:
                return TrailingStopResult(new_sl=new_sl, sl_updated=True)
        else:
            state.lowest_price = min(state.lowest_price, bar.low)
            new_sl = state.lowest_price + trail_distance

            if bar.high >= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            if new_sl < state.current_sl:
                return TrailingStopResult(new_sl=new_sl, sl_updated=True)

        return TrailingStopResult()

    def _step_trail(self, bar: Bar, state: TrailingStopState, direction: TradeDirection) -> TrailingStopResult:
        pip_value = self._get_pip_value(bar.close)
        step_distance = self.config.step_pips * pip_value

        if direction == TradeDirection.LONG:
            if bar.low <= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            state.highest_price = max(state.highest_price, bar.high)
            new_sl = state.highest_price - step_distance

            if new_sl > state.current_sl:
                return TrailingStopResult(new_sl=new_sl, sl_updated=True)
        else:
            if bar.high >= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            state.lowest_price = min(state.lowest_price, bar.low)
            new_sl = state.lowest_price + step_distance

            if new_sl < state.current_sl:
                return TrailingStopResult(new_sl=new_sl, sl_updated=True)

        return TrailingStopResult()

    def _time_trail(
        self,
        bar: Bar,
        state: TrailingStopState,
        direction: TradeDirection,
        atr: float,
        entry_price: float,
    ) -> TrailingStopResult:
        if state.bars_since_entry < self.config.time_tighten_bars:
            return TrailingStopResult()

        tightened_distance = atr * self.config.atr_multiplier * self.config.time_tighten_pct

        if direction == TradeDirection.LONG:
            if bar.low <= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            state.highest_price = max(state.highest_price, bar.high)
            normal_sl = state.highest_price - atr * self.config.atr_multiplier
            new_sl = max(normal_sl, state.highest_price - tightened_distance)
            new_sl = max(new_sl, entry_price)

            if new_sl > state.current_sl:
                return TrailingStopResult(new_sl=new_sl, sl_updated=True)
        else:
            if bar.high >= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            state.lowest_price = min(state.lowest_price, bar.low)
            normal_sl = state.lowest_price + atr * self.config.atr_multiplier
            new_sl = min(normal_sl, state.lowest_price + tightened_distance)
            new_sl = min(new_sl, entry_price)

            if new_sl < state.current_sl:
                return TrailingStopResult(new_sl=new_sl, sl_updated=True)

        return TrailingStopResult()

    def _sar_trail(self, bar: Bar, state: TrailingStopState, direction: TradeDirection) -> TrailingStopResult:
        state.sar_af = self.config.sar_af_start
        state.sar_ep = bar.close

        if direction == TradeDirection.LONG:
            if bar.low <= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            if bar.high > state.sar_ep:
                state.sar_ep = bar.high
                state.sar_af = min(state.sar_af + self.config.sar_af_increment, self.config.sar_af_max)

            sar = state.sar_ep - state.sar_af * (state.sar_ep - state.current_sl)
            sar = max(sar, state.lowest_price if hasattr(state, "_prev_low") else bar.low)

            if sar > state.current_sl:
                return TrailingStopResult(new_sl=sar, sl_updated=True)
        else:
            if bar.high >= state.current_sl:
                return TrailingStopResult(triggered=True, exit_price=state.current_sl)

            if bar.low < state.sar_ep:
                state.sar_ep = bar.low
                state.sar_af = min(state.sar_af + self.config.sar_af_increment, self.config.sar_af_max)

            sar = state.sar_ep + state.sar_af * (state.current_sl - state.sar_ep)
            sar = min(sar, state.highest_price if hasattr(state, "_prev_high") else bar.high)

            if sar < state.current_sl:
                return TrailingStopResult(new_sl=sar, sl_updated=True)

        return TrailingStopResult()

    @staticmethod
    def _get_pip_value(price: float) -> float:
        if price >= 50:
            return 0.01
        elif price >= 1:
            return 0.0001
        return 0.00000001
