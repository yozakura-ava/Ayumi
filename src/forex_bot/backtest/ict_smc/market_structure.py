from __future__ import annotations

from ..engine import TradeDirection
from .models import ICTMarketState, StructureBreak, SwingPoint


class MarketStructureAnalyzer:
    def __init__(self, swing_lookback: int = 5, bos_threshold: float = 0.5):
        self._swing_lookback = swing_lookback
        self._bos_threshold = bos_threshold

    def analyze(self, state: ICTMarketState):
        self._detect_swing_points(state)
        self._detect_structure_breaks(state)
        self._determine_bias(state)
        self._calculate_atr(state)

    def _detect_swing_points(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < self._swing_lookback * 2 + 1:
            return

        state.swing_highs.clear()
        state.swing_lows.clear()

        for i in range(self._swing_lookback, len(bars) - self._swing_lookback):
            is_swing_high = True
            is_swing_low = True

            for j in range(1, self._swing_lookback + 1):
                if bars[i].high <= bars[i - j].high or bars[i].high <= bars[i + j].high:
                    is_swing_high = False
                if bars[i].low >= bars[i - j].low or bars[i].low >= bars[i + j].low:
                    is_swing_low = False

            if is_swing_high:
                state.swing_highs.append(SwingPoint(index=i, price=bars[i].high, is_high=True, time=bars[i].time))

            if is_swing_low:
                state.swing_lows.append(SwingPoint(index=i, price=bars[i].low, is_high=False, time=bars[i].time))

    def _detect_structure_breaks(self, state: ICTMarketState):
        state.structure_breaks.clear()

        if len(state.swing_highs) < 2 or len(state.swing_lows) < 2:
            return

        for i in range(1, len(state.swing_highs)):
            if state.swing_highs[i].price > state.swing_highs[i - 1].price:
                threshold = state.swing_highs[i - 1].price * (1 - self._bos_threshold / 100)
                if state.swing_highs[i].price > threshold:
                    is_choch = state.structure_bias == TradeDirection.SHORT
                    state.structure_breaks.append(
                        StructureBreak(
                            time=state.swing_highs[i].time,
                            direction=TradeDirection.LONG,
                            break_level=state.swing_highs[i - 1].price,
                            is_choch=is_choch,
                            break_strength=min(
                                1.0,
                                (state.swing_highs[i].price - state.swing_highs[i - 1].price)
                                / state.swing_highs[i - 1].price
                                * 100
                                / self._bos_threshold,
                            ),
                        )
                    )

        for i in range(1, len(state.swing_lows)):
            if state.swing_lows[i].price < state.swing_lows[i - 1].price:
                threshold = state.swing_lows[i - 1].price * (1 + self._bos_threshold / 100)
                if state.swing_lows[i].price < threshold:
                    is_choch = state.structure_bias == TradeDirection.LONG
                    state.structure_breaks.append(
                        StructureBreak(
                            time=state.swing_lows[i].time,
                            direction=TradeDirection.SHORT,
                            break_level=state.swing_lows[i - 1].price,
                            is_choch=is_choch,
                            break_strength=min(
                                1.0,
                                (state.swing_lows[i - 1].price - state.swing_lows[i].price)
                                / state.swing_lows[i - 1].price
                                * 100
                                / self._bos_threshold,
                            ),
                        )
                    )

    def _determine_bias(self, state: ICTMarketState):
        if not state.structure_breaks:
            state.structure_bias = TradeDirection.NEUTRAL
            return

        recent_breaks = sorted(state.structure_breaks, key=lambda sb: sb.time, reverse=True)[:5]

        bullish_strength = sum(sb.break_strength for sb in recent_breaks if sb.direction == TradeDirection.LONG)
        bearish_strength = sum(sb.break_strength for sb in recent_breaks if sb.direction == TradeDirection.SHORT)

        last_break = recent_breaks[0]
        last_break_weight = 2.0
        if last_break.direction == TradeDirection.LONG:
            bullish_strength += last_break_weight
        else:
            bearish_strength += last_break_weight

        if bullish_strength > bearish_strength * 1.3:
            state.structure_bias = TradeDirection.LONG
        elif bearish_strength > bullish_strength * 1.3:
            state.structure_bias = TradeDirection.SHORT
        else:
            state.structure_bias = TradeDirection.NEUTRAL

    def _calculate_atr(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < 15:
            state.atr = 0.0
            return

        period = min(14, len(bars) - 1)
        atr_sum = 0.0
        for i in range(len(bars) - period, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    abs(bars[i].high - bars[i - 1].close),
                    abs(bars[i].low - bars[i - 1].close),
                )
                atr_sum += tr
        state.atr = atr_sum / period

    def is_bullish_structure(self, state: ICTMarketState) -> bool:
        return state.structure_bias == TradeDirection.LONG

    def is_bearish_structure(self, state: ICTMarketState) -> bool:
        return state.structure_bias == TradeDirection.SHORT

    def get_structure_strength(self, state: ICTMarketState) -> float:
        if not state.structure_breaks:
            return 0.0
        recent = sorted(state.structure_breaks, key=lambda sb: sb.time, reverse=True)[:3]
        return sum(1 for sb in recent if sb.direction == state.structure_bias) / len(recent)

    def is_strong_trend(self, state: ICTMarketState) -> bool:
        if state.structure_bias == TradeDirection.NEUTRAL:
            return False
        if not state.structure_breaks:
            return False

        recent_breaks = sorted(state.structure_breaks, key=lambda sb: sb.time, reverse=True)[:5]
        last_break = recent_breaks[0]
        if last_break.direction != state.structure_bias:
            return False

        last_break_age = (state.latest_bar.time - last_break.time).total_seconds() / 60
        if last_break_age > 960:
            return False

        aligned_count = sum(1 for sb in recent_breaks if sb.direction == state.structure_bias)
        aligned_ratio = aligned_count / len(recent_breaks)
        return aligned_ratio >= 0.4
