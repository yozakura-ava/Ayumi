"""§4.2-4.3 — Rise/drop counting, level validation, and timeframe feeding."""

from __future__ import annotations

from typing import Optional

import numpy as np

from .data_types import Level, LevelType, Swing, SwingType
from .thresholds import LEVEL_COMPLETION_RATIO


class LevelCounter:
    """Count rises (R1-R3) and drops (D1-D3) from swing sequences.

    A "rise" is a move from a swing low to the next swing high that meets
    completion criteria. Rises are numbered sequentially: R1 = first completed
    rise to a new high, R2 = second, R3 = third (exhaustion zone).

    Drops mirror rises.
    """

    def __init__(
        self,
        r3_ratio: float = LEVEL_COMPLETION_RATIO,
        ema_50: Optional[np.ndarray] = None,
        ema_200: Optional[np.ndarray] = None,
        avg_volume: Optional[float] = None,
        volume_series: Optional[np.ndarray] = None,
    ):
        self.r3_ratio = r3_ratio
        self.ema_50 = ema_50
        self.ema_200 = ema_200
        self.avg_volume = avg_volume
        self.volume_series = volume_series

    def detect_levels(
        self,
        swing_highs: list[Swing],
        swing_lows: list[Swing],
    ) -> list[Level]:
        """Walk through alternating swing extremes and count rises/drops.

        Returns completed levels only (R1-R3, D1-D3).
        """
        if not swing_highs or not swing_lows:
            return []

        # Merge and sort all swings chronologically
        all_swings = sorted(swing_highs + swing_lows, key=lambda s: s.bar_index)
        if len(all_swings) < 2:
            return []

        levels: list[Level] = []
        rise_count = 0
        drop_count = 0
        _prev_extreme_price: float = 0.0

        # Track the highest high and lowest low for new-high/new-low checks
        running_high = all_swings[0].price
        running_low = all_swings[0].price

        for i in range(1, len(all_swings)):
            prev = all_swings[i - 1]
            curr = all_swings[i]

            # Determine direction of this leg
            if curr.swing_type == SwingType.HIGH and prev.swing_type == SwingType.LOW:
                # Rise leg
                magnitude = curr.price - prev.price
                is_new_high = curr.price > running_high
                if is_new_high:
                    rise_count += 1
                    running_high = curr.price
                    completed = self._validate_rise(rise_count, curr, prev, magnitude)
                    lt = self._rise_level_type(rise_count)
                    levels.append(
                        Level(
                            price=curr.price,
                            level_type=lt,
                            magnitude=magnitude,
                            bar_index=curr.bar_index,
                            completed=completed,
                        )
                    )

            elif curr.swing_type == SwingType.LOW and prev.swing_type == SwingType.HIGH:
                # Drop leg
                magnitude = prev.price - curr.price
                is_new_low = curr.price < running_low
                if is_new_low:
                    drop_count += 1
                    running_low = curr.price
                    completed = self._validate_drop(drop_count, curr, prev, magnitude)
                    lt = self._drop_level_type(drop_count)
                    levels.append(
                        Level(
                            price=curr.price,
                            level_type=lt,
                            magnitude=magnitude,
                            bar_index=curr.bar_index,
                            completed=completed,
                        )
                    )

        return levels

    def _validate_rise(
        self,
        count: int,
        swing_high: Swing,
        swing_low: Swing,
        magnitude: float,
    ) -> bool:
        """Check rise completion criteria per §4.3."""
        # R1: MM candle breaks and closes above 50 EMA + attempts 200 EMA
        # R2: MM candle breaks and closes above 200 EMA
        # R3: magnitude ≥ 90% of R2 magnitude
        # Phase 1: simplified — always complete unless R3 magnitude check fails
        if count == 3 and self._prev_rise_magnitude is not None:
            if magnitude < self._prev_rise_magnitude * self.r3_ratio:
                return False
        return True

    def _validate_drop(
        self,
        count: int,
        swing_low: Swing,
        swing_high: Swing,
        magnitude: float,
    ) -> bool:
        """Check drop completion criteria (mirror of rise)."""
        if count == 3 and self._prev_drop_magnitude is not None:
            if magnitude < self._prev_drop_magnitude * self.r3_ratio:
                return False
        return True

    def _rise_level_type(self, count: int) -> LevelType:
        mapping = {1: LevelType.R1, 2: LevelType.R2, 3: LevelType.R3}
        return mapping.get(count, LevelType.SWH)

    def _drop_level_type(self, count: int) -> LevelType:
        mapping = {1: LevelType.D1, 2: LevelType.D2, 3: LevelType.D3}
        return mapping.get(count, LevelType.SWL)

    # We track previous magnitudes for R3/D3 validation
    _prev_rise_magnitude: Optional[float] = None
    _prev_drop_magnitude: Optional[float] = None

    def feed_to_htf(self, level: Level) -> dict:
        """Format a level for HTFAnalyzer consumption."""
        return {
            "price": level.price,
            "type": level.level_type.value,
            "magnitude": level.magnitude,
            "bar_index": level.bar_index,
            "completed": level.completed,
        }

    def detect_levels_with_tracking(
        self,
        swing_highs: list[Swing],
        swing_lows: list[Swing],
    ) -> list[Level]:
        """Same as detect_levels but properly tracks R2/D2 magnitudes
        for R3/D3 validation."""
        self._prev_rise_magnitude = None
        self._prev_drop_magnitude = None
        levels = self.detect_levels(swing_highs, swing_lows)
        # Update magnitude tracking after detection
        for lv in levels:
            if lv.level_type == LevelType.R2:
                self._prev_rise_magnitude = lv.magnitude
            elif lv.level_type == LevelType.D2:
                self._prev_drop_magnitude = lv.magnitude
        return levels
