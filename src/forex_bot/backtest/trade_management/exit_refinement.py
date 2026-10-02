from dataclasses import dataclass

from ..engine import Bar, ExitReason, TradeDirection


@dataclass
class ExitRefinerState:
    bars_since_entry: int = 0
    tp1_hit: bool = False
    momentum_history: list[float] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.momentum_history is None:
            self.momentum_history = []


@dataclass
class ExitRefinerResult:
    should_exit: bool = False
    exit_price: float = 0.0
    reason: ExitReason | None = None
    message: str = ""


class ExitRefiner:
    def __init__(
        self,
        enabled: bool = True,
        max_bars_to_tp1: int = 48,
        momentum_exit_enabled: bool = True,
        momentum_lookback: int = 5,
        momentum_reversal_threshold: float = 0.6,
        spread_filter_enabled: bool = True,
        max_spread_atr_pct: float = 0.15,
    ):
        self.enabled = enabled
        self.max_bars_to_tp1 = max_bars_to_tp1
        self.momentum_exit_enabled = momentum_exit_enabled
        self.momentum_lookback = momentum_lookback
        self.momentum_reversal_threshold = momentum_reversal_threshold
        self.spread_filter_enabled = spread_filter_enabled
        self.max_spread_atr_pct = max_spread_atr_pct

    def create_state(self) -> ExitRefinerState:
        return ExitRefinerState()

    def on_bar(
        self,
        bar: Bar,
        state: ExitRefinerState,
        direction: TradeDirection,
        atr: float,
        recent_bars: list[Bar] | None = None,
    ) -> ExitRefinerResult:
        if not self.enabled:
            return ExitRefinerResult()

        state.bars_since_entry += 1
        momentum = self._calculate_momentum(bar, recent_bars)
        state.momentum_history.append(momentum)

        if self.momentum_exit_enabled and not state.tp1_hit:
            result = self._check_momentum_reversal(state, direction, bar, momentum)
            if result.should_exit:
                return result

        if not state.tp1_hit and state.bars_since_entry >= self.max_bars_to_tp1:
            if direction == TradeDirection.LONG:
                exit_price = bar.close
            else:
                exit_price = bar.close
            return ExitRefinerResult(
                should_exit=True,
                exit_price=exit_price,
                reason=ExitReason.STOP_LOSS,
                message=f"Time stop: {state.bars_since_entry} bars without TP1",
            )

        return ExitRefinerResult()

    def check_entry_spread(self, bar: Bar, atr: float, spread_pips: float = 0.5) -> bool:
        if not self.enabled or not self.spread_filter_enabled:
            return True

        if atr <= 0:
            return True

        pip_value = self._get_pip_value(bar.close)
        spread_price = spread_pips * pip_value
        spread_atr_ratio = spread_price / atr

        return spread_atr_ratio <= self.max_spread_atr_pct

    def _check_momentum_reversal(
        self,
        state: ExitRefinerState,
        direction: TradeDirection,
        bar: Bar,
        current_momentum: float,
    ) -> ExitRefinerResult:
        if len(state.momentum_history) < self.momentum_lookback:
            return ExitRefinerResult()

        recent = state.momentum_history[-self.momentum_lookback :]
        avg_momentum = sum(recent) / len(recent)

        is_reversing = False
        if direction == TradeDirection.LONG and avg_momentum < -self.momentum_reversal_threshold:
            is_reversing = True
        elif direction == TradeDirection.SHORT and avg_momentum > self.momentum_reversal_threshold:
            is_reversing = True

        if is_reversing:
            return ExitRefinerResult(
                should_exit=True,
                exit_price=bar.close,
                reason=ExitReason.SIGNAL_FLIP,
                message=f"Momentum reversal: avg={avg_momentum:.3f} against {direction.value}",
            )

        return ExitRefinerResult()

    def _calculate_momentum(self, bar: Bar, recent_bars: list[Bar] | None = None) -> float:
        if recent_bars and len(recent_bars) >= 2:
            return bar.close - recent_bars[-1].close
        return bar.close - bar.open

    @staticmethod
    def _get_pip_value(price: float) -> float:
        if price >= 50:
            return 0.01
        elif price >= 1:
            return 0.0001
        return 0.00000001
