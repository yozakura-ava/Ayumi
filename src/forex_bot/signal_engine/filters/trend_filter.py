"""TrendFilter — checks trend alignment via EMA relationship.

A long signal is allowed when EMA(fast) > EMA(slow) (bullish structure).
A short signal is allowed when EMA(fast) < EMA(slow) (bearish structure).
If the signal direction fights the trend, the filter rejects it.

Priority: 10 (runs first — cheap EMA check).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TrendConfig:
    ema_fast_period: int = 9
    ema_slow_period: int = 21
    tolerance_pips: float = 0.0  # dead-zone around crossover


class TrendFilter:
    """Gate signals based on EMA trend alignment."""

    priority: int = 10

    def __init__(self, config: Optional[TrendConfig] = None) -> None:
        self._config = config or TrendConfig()

    @property
    def name(self) -> str:
        return "trend"

    def evaluate(
        self,
        signal_direction: str,
        ema_fast: float,
        ema_slow: float,
        tolerance: Optional[float] = None,
    ) -> bool:
        """Return True if the signal direction aligns with the EMA trend.

        Args:
            signal_direction: "LONG" or "SHORT"
            ema_fast: Current fast EMA value.
            ema_slow: Current slow EMA value.
            tolerance: Override dead-zone (defaults to config tolerance).
        """
        tol = tolerance if tolerance is not None else self._config.tolerance_pips
        direction = signal_direction.upper()

        spread = ema_fast - ema_slow

        # Within dead-zone → trend is unclear, allow the signal through
        if abs(spread) <= tol:
            return True

        if direction == "LONG":
            passed = spread > 0  # bullish: fast above slow
        elif direction == "SHORT":
            passed = spread < 0  # bearish: fast below slow
        else:
            logger.warning("Unknown direction %r, passing through", signal_direction)
            return True

        if not passed:
            logger.debug(
                "TrendFilter REJECT: dir=%s ema_fast=%.5f ema_slow=%.5f spread=%.5f",
                direction,
                ema_fast,
                ema_slow,
                spread,
            )
        return passed
