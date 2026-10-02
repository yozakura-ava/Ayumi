"""§3 — N-bar swing detection. Foundation for all pattern and level analysis."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .data_types import Swing, SwingType
from .thresholds import EQUAL_SWING_THRESHOLD


class SwingDetector:
    """Detect swing highs and lows using N-bar lookback windows."""

    def __init__(self, lookback: int = 5):
        self.lookback = lookback

    def detect_swings(
        self,
        highs: list[float] | np.ndarray | pd.Series,
        lows: list[float] | np.ndarray | pd.Series,
    ) -> tuple[list[Swing], list[Swing]]:
        """Identify swing highs and lows.

        A swing high: bar whose high exceeds all highs within `lookback` bars
        on each side. Swing lows are the mirror.

        Edge cases handled:
        - Equal highs/lows within 0.05% → merged into single swing
        - Inside bars (high < prev high, low > prev low) → skipped
        - Outside bars → evaluated normally
        """
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        n = len(highs)
        lb = self.lookback

        raw_highs: list[tuple[int, float]] = []
        raw_lows: list[tuple[int, float]] = []

        for i in range(lb, n - lb):
            is_sh = True
            is_sl = True
            # Inside bar check: if this bar is strictly inside the previous, skip
            if i > 0 and highs[i] < highs[i - 1] and lows[i] > lows[i - 1]:
                continue

            for j in range(1, lb + 1):
                if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                    is_sh = False
                if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                    is_sl = False

            if is_sh:
                raw_highs.append((i, float(highs[i])))
            if is_sl:
                raw_lows.append((i, float(lows[i])))

        swing_highs = self._merge_nearby(raw_highs)
        swing_lows = self._merge_nearby(raw_lows)

        return (
            [Swing(bi, p, SwingType.HIGH) for bi, p in swing_highs],
            [Swing(bi, p, SwingType.LOW) for bi, p in swing_lows],
        )

    def _merge_nearby(self, swings: list[tuple[int, float]]) -> list[tuple[int, float]]:
        """Merge consecutive swings within 0.05% of each other into one.

        Keeps the first (earliest) swing in each merged group.
        """
        if not swings:
            return []
        merged = [swings[0]]
        for idx, price in swings[1:]:
            prev_price = merged[-1][1]
            if abs(price - prev_price) / prev_price <= EQUAL_SWING_THRESHOLD:
                # Keep the first, discard this duplicate
                continue
            merged.append((idx, price))
        return merged

    def get_swing_series(self, df: pd.DataFrame, lookback: Optional[int] = None) -> pd.DataFrame:
        """Return a copy of df with swing_high and swing_low columns.

        Non-swing bars get NaN in those columns.
        """
        lb = lookback or self.lookback
        detector = SwingDetector(lookback=lb)
        swing_highs, swing_lows = detector.detect_swings(df["high"].values, df["low"].values)
        result = df.copy()
        result["swing_high"] = np.nan
        result["swing_low"] = np.nan
        for s in swing_highs:
            result.at[result.index[s.bar_index], "swing_high"] = s.price
        for s in swing_lows:
            result.at[result.index[s.bar_index], "swing_low"] = s.price
        return result
