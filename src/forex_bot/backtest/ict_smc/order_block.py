from __future__ import annotations

from ..engine import TradeDirection
from .models import ICTMarketState, OrderBlock


class OrderBlockDetector:
    def __init__(
        self,
        freshness_window: int = 5,
        min_body_ratio: float = 0.5,
        overlap_threshold: float = 0.7,
        lookback: int = 50,
    ):
        self._freshness_window = freshness_window
        self._min_body_ratio = min_body_ratio
        self._overlap_threshold = overlap_threshold
        self._lookback = lookback

    def detect(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < 10:
            return

        start_idx = max(0, len(bars) - self._lookback)
        current_idx = len(bars) - 1

        for i in range(start_idx, current_idx - 1):
            bar = bars[i]
            body_ratio = ICTMarketState.bar_body_ratio(bar)
            if body_ratio < self._min_body_ratio:
                continue
            bar_range = ICTMarketState.bar_range(bar)
            if bar_range == 0:
                continue

            age = current_idx - i
            if age > self._freshness_window:
                continue

            direction = TradeDirection.LONG if ICTMarketState.bar_is_bullish(bar) else TradeDirection.SHORT
            top = max(bar.open, bar.close)
            bottom = min(bar.open, bar.close)

            avg_body_start = max(0, len(bars) - 21)
            recent_bodies = [
                ICTMarketState.bar_body(b)
                for b in bars[avg_body_start : avg_body_start + 20]
                if ICTMarketState.bar_range(b) > 0
            ]
            avg_body = sum(recent_bodies) / len(recent_bodies) if recent_bodies else 0

            body_size = ICTMarketState.bar_body(bar)
            strength = 0.5
            if avg_body > 0:
                strength = min(1.0, 0.5 + (body_size / avg_body - 1.0) * 0.25)

            min_wick = min(ICTMarketState.bar_upper_wick(bar), ICTMarketState.bar_lower_wick(bar))
            wick_ratio = min_wick / bar_range
            if wick_ratio < 0.2:
                strength += 0.1

            if state.structure_bias == direction:
                strength += 0.15

            strength = min(1.0, strength)

            overlaps = any(
                ob.direction == direction
                and (
                    max(0, min(top, ob.top) - max(bottom, ob.bottom)) / min(ob.top - ob.bottom, top - bottom)
                    > self._overlap_threshold
                )
                if (ob.top - ob.bottom) > 0 and (top - bottom) > 0
                else False
                for ob in state.active_order_blocks
            )
            if overlaps:
                continue

            mitigated = False
            for j in range(i + 1, current_idx + 1):
                if direction == TradeDirection.LONG and bars[j].low < bottom:
                    mitigated = True
                    break
                if direction == TradeDirection.SHORT and bars[j].high > top:
                    mitigated = True
                    break

            if not mitigated:
                state.active_order_blocks.append(
                    OrderBlock(
                        start_index=i,
                        end_index=i,
                        top=top,
                        bottom=bottom,
                        direction=direction,
                        strength=strength,
                        is_mitigated=False,
                        age=age,
                        created_time=bar.time,
                        body_size=body_size,
                    )
                )

        self._cleanup_mitigated(state)

    def _cleanup_mitigated(self, state: ICTMarketState):
        bars = state.bars
        to_remove = []
        for ob in state.active_order_blocks:
            found = False
            for i in range(ob.start_index + 1, len(bars)):
                if ob.direction == TradeDirection.LONG and bars[i].low < ob.bottom:
                    found = True
                    break
                if ob.direction == TradeDirection.SHORT and bars[i].high > ob.top:
                    found = True
                    break
            if found or ob.age > self._freshness_window * 3:
                to_remove.append(ob)
        for ob in to_remove:
            state.active_order_blocks.remove(ob)

    def get_most_relevant(self, state: ICTMarketState, direction: TradeDirection) -> OrderBlock | None:
        candidates = [ob for ob in state.active_order_blocks if ob.direction == direction and not ob.is_mitigated]
        if not candidates:
            return None
        return max(candidates, key=lambda ob: (ob.strength, -ob.age))
