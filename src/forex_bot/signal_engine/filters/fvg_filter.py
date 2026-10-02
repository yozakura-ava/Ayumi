"""FVGFilter — Fair Value Gap detection filter.

A Fair Value Gap is a three-bar imbalance where the high of bar[i-2]
and the low of bar[i] don't overlap, leaving a "gap" in price action.
FVGs act as magnets for future price — signals in the direction of
a recent FVG get a pass, signals against it are rejected.

Priority: 30 (runs after trend + volatility checks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FVGConfig:
    max_bars_lookback: int = 20
    min_gap_pips: float = 1.0
    pip_value: float = 0.0001


@dataclass
class FVGResult:
    found: bool
    direction: str  # "bullish", "bearish", "none"
    gap_high: float = 0.0
    gap_low: float = 0.0
    bar_index: int = -1


class FVGFilter:
    """Detect Fair Value Gaps and gate signals accordingly."""

    priority: int = 30

    def __init__(self, config: Optional[FVGConfig] = None) -> None:
        self._config = config or FVGConfig()

    @property
    def name(self) -> str:
        return "fvg"

    def detect_fvg(self, highs: list[float], lows: list[float], closes: list[float]) -> FVGResult:
        """Scan the last N bars for a Fair Value Gap.

        A bullish FVG: low[i] > high[i-2] (gap up).
        A bearish FVG: high[i] < low[i-2] (gap down).
        """
        n = len(highs)
        if n < 3:
            return FVGResult(found=False, direction="none")

        lookback = min(self._config.max_bars_lookback, n - 2)
        pip = self._config.pip_value
        min_gap = self._config.min_gap_pips * pip

        # Scan from most recent backwards
        for i in range(n - 1, lookback - 1, -1):
            # Bullish FVG: current low > high two bars ago
            gap = lows[i] - highs[i - 2]
            if gap > min_gap:
                return FVGResult(
                    found=True,
                    direction="bullish",
                    gap_high=lows[i],
                    gap_low=highs[i - 2],
                    bar_index=i,
                )

            # Bearish FVG: current high < low two bars ago
            gap = lows[i - 2] - highs[i]
            if gap > min_gap:
                return FVGResult(
                    found=True,
                    direction="bearish",
                    gap_high=lows[i - 2],
                    gap_low=highs[i],
                    bar_index=i,
                )

        return FVGResult(found=False, direction="none")

    def evaluate(
        self,
        signal_direction: str,
        highs: list[float],
        lows: list[float],
        closes: list[float],
    ) -> bool:
        """Return True if the signal direction aligns with a recent FVG.

        If no FVG is found, the filter passes (no opinion).
        """
        result = self.detect_fvg(highs, lows, closes)
        if not result.found:
            return True  # No FVG → no opinion, allow

        direction = signal_direction.upper()
        if direction == "LONG" and result.direction == "bullish":
            return True
        if direction == "SHORT" and result.direction == "bearish":
            return True

        logger.debug(
            "FVGFilter REJECT: dir=%s but fvg=%s (gap_high=%.5f gap_low=%.5f)",
            direction,
            result.direction,
            result.gap_high,
            result.gap_low,
        )
        return False
