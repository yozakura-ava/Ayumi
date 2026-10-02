"""ATRFilter — volatility gate using Average True Range.

Rejects signals when volatility is outside an acceptable band.
Too-low ATR → no momentum (chop).
Too-high ATR → dangerous, wide stops likely.

Priority: 20 (runs after cheap trend check).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ATRConfig:
    atr_min: float = 0.0008  # ~8 pips on GBPUSD
    atr_max: float = 0.0060  # ~60 pips — storm filter
    pip_value: float = 0.0001  # 1 pip for 4-digit pairs


class ATRFilter:
    """Gate signals based on ATR volatility band."""

    priority: int = 20

    def __init__(self, config: Optional[ATRConfig] = None) -> None:
        self._config = config or ATRConfig()

    @property
    def name(self) -> str:
        return "atr"

    def evaluate(self, atr_value: float, pip_value: Optional[float] = None) -> bool:
        """Return True if ATR is within the acceptable volatility band.

        Args:
            atr_value: Current ATR (raw price units, e.g. 0.00120).
            pip_value: Override pip size for the instrument.
        """
        pv = pip_value if pip_value is not None else self._config.pip_value
        atr_pips = atr_value / pv if pv > 0 else atr_value
        min_pips = self._config.atr_min / pv if pv > 0 else self._config.atr_min
        max_pips = self._config.atr_max / pv if pv > 0 else self._config.atr_max

        if atr_pips < min_pips:
            logger.debug(
                "ATRFilter REJECT: atr=%.5f (%.1f pips) below min %.1f pips",
                atr_value,
                atr_pips,
                min_pips,
            )
            return False

        if atr_pips > max_pips:
            logger.debug(
                "ATRFilter REJECT: atr=%.5f (%.1f pips) above max %.1f pips",
                atr_value,
                atr_pips,
                max_pips,
            )
            return False

        return True
