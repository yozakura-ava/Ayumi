from __future__ import annotations

from ..engine import TradeDirection
from .models import FairValueGap, ICTMarketState


class FVGDetector:
    def __init__(self, max_age: int = 20, mini_threshold: float = 0.0003):
        self._max_age = max_age
        self._mini_threshold = mini_threshold

    def detect(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < 5:
            return

        start_idx = max(2, len(bars) - self._max_age - 5)
        current_idx = len(bars) - 1

        for i in range(start_idx, current_idx - 1):
            candle1 = bars[i]
            candle3 = bars[i + 2]

            if candle3.high < candle1.low or candle3.low > candle1.high:
                continue

            gap = 0.0
            direction = TradeDirection.NEUTRAL

            if candle1.high < candle3.low:
                gap = candle3.low - candle1.high
                direction = TradeDirection.LONG
            elif candle3.high > candle1.low:
                gap = candle3.high - candle1.low
                direction = TradeDirection.SHORT
            else:
                continue

            normalized_gap = gap / bars[i + 1].close
            if normalized_gap < self._mini_threshold:
                continue

            age = current_idx - (i + 1)

            filled = False
            mitigated = False
            for j in range(i + 3, current_idx + 1):
                if direction == TradeDirection.LONG:
                    if bars[j].low <= candle1.high:
                        mitigated = True
                        break
                    fill_denom = candle1.high - candle3.low + gap
                    if fill_denom > 0:
                        fill_pct = max(0, (candle3.low - bars[j].low) / gap)
                        if fill_pct >= 0.5:
                            filled = True
                else:
                    if bars[j].high >= candle3.high:
                        mitigated = True
                        break
                    fill_denom = candle3.high - candle1.low + gap
                    if fill_denom > 0:
                        fill_pct = max(0, (bars[j].high - candle3.high) / gap)
                        if fill_pct >= 0.5:
                            filled = True

            if mitigated:
                continue

            top = candle3.low if direction == TradeDirection.LONG else candle3.high
            bottom = candle1.high if direction == TradeDirection.LONG else candle1.low
            if bottom > top:
                top, bottom = bottom, top

            state.active_fvgs.append(
                FairValueGap(
                    start_index=i,
                    top=top,
                    bottom=bottom,
                    direction=direction,
                    size=gap,
                    age=age,
                    is_filled=filled,
                    is_mitigated=False,
                    created_time=candle1.time,
                )
            )

        self._cleanup_stale(state)

    def _cleanup_stale(self, state: ICTMarketState):
        state.active_fvgs = [fvg for fvg in state.active_fvgs if not fvg.is_mitigated and fvg.age <= self._max_age]

    def get_nearest_unfilled(
        self, state: ICTMarketState, direction: TradeDirection, current_price: float
    ) -> FairValueGap | None:
        candidates = [fvg for fvg in state.active_fvgs if fvg.direction == direction and not fvg.is_mitigated]
        if not candidates:
            return None
        return min(candidates, key=lambda fvg: abs(current_price - (fvg.top + fvg.bottom) / 2))
