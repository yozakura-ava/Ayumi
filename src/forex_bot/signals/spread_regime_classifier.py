"""Spread Regime Classifier.

Classifies the current spread into one of four regimes (TIGHT, NORMAL,
WIDE, EXTREME) using a rolling window of recent spread observations,
and exposes the per-regime confidence penalty used by the signal
engine to dampen signal confidence under elevated spread conditions.

Public surface
--------------
- :class:`SpreadRegime` — enum of regimes.
- :class:`SpreadRegimeClassifier` — rolling-window classifier.
- :data:`_DEFAULT_PENALTIES` — per-regime confidence penalty map.
- :data:`TIMEFRAME_WINDOW` — rolling-window size per timeframe.
- :data:`DEFAULT_WINDOW` — default window size when none is supplied.

The classifier is stateless across instances: each classifier owns its
own rolling window. Thread-safety is not provided — callers should
serialize updates per instance.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Mapping, Optional


class SpreadRegime(Enum):
    """Spread regime bucket."""

    TIGHT = "tight"
    NORMAL = "normal"
    WIDE = "wide"
    EXTREME = "extreme"


# Per-regime confidence multiplier. Values in [0.5, 1.0] per acceptance
# criteria; TIGHT keeps full confidence, EXTREME halves it.
_DEFAULT_PENALTIES: Mapping[SpreadRegime, float] = {
    SpreadRegime.TIGHT: 1.00,
    SpreadRegime.NORMAL: 0.85,
    SpreadRegime.WIDE: 0.70,
    SpreadRegime.EXTREME: 0.50,
}


# Rolling-window size per timeframe. Larger windows smooth more, smaller
# windows react faster to regime changes.
TIMEFRAME_WINDOW: Mapping[str, int] = {
    "M1": 30,
    "M5": 50,
    "M15": 80,
    "M30": 100,
    "H1": 150,
    "H4": 200,
    "D1": 250,
}

# Default window when the caller does not specify one.
DEFAULT_WINDOW: int = 50


class SpreadRegimeClassifier:
    """Classify spread observations into regimes using a rolling window.

    The classifier maintains a FIFO list of the most recent spread values
    up to ``window``. On every :meth:`classify` call, percentiles of the
    rolling distribution define the regime thresholds:

    - TIGHT  : below the 25th percentile
    - NORMAL : at or above p25, below p75
    - WIDE   : at or above p75, below p90
    - EXTREME: at or above p90

    When the window is empty, the classifier returns ``NORMAL`` and the
    full-confidence penalty, since there is no evidence to deviate from
    baseline conditions.
    """

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        if window <= 0:
            raise ValueError(f"window must be positive, got {window!r}")
        self.window: int = int(window)
        self.spreads: list[float] = []

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def update(self, bar: object) -> None:
        """Append a spread observation to the rolling window.

        ``bar`` may be any object exposing a numeric ``spread`` attribute
        (e.g. an OHLC bar, a ``SimpleNamespace``, or a dataclass). The
        observed value is appended; if the window is full, the oldest
        value is dropped to maintain a fixed size.
        """
        spread = getattr(bar, "spread", None)
        if spread is None:
            raise TypeError("bar must expose a 'spread' attribute (numeric spread value)")
        value = float(spread)
        if value < 0:
            raise ValueError(f"spread must be non-negative, got {value!r}")
        self.spreads.append(value)
        if len(self.spreads) > self.window:
            # FIFO trim — keep only the most recent ``window`` values.
            self.spreads.pop(0)

    def reset(self) -> None:
        """Clear the rolling window."""
        self.spreads.clear()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------
    def classify(self, spread_pips: float) -> SpreadRegime:
        """Classify ``spread_pips`` against the rolling distribution.

        Returns ``SpreadRegime.NORMAL`` when the window is empty so that
        callers without history get a sensible default.
        """
        if not self.spreads:
            return SpreadRegime.NORMAL

        sorted_spreads = sorted(self.spreads)
        p25 = self._percentile(sorted_spreads, 25)
        p75 = self._percentile(sorted_spreads, 75)
        p90 = self._percentile(sorted_spreads, 90)

        if spread_pips < p25:
            return SpreadRegime.TIGHT
        if spread_pips < p75:
            return SpreadRegime.NORMAL
        if spread_pips < p90:
            return SpreadRegime.WIDE
        return SpreadRegime.EXTREME

    def confidence_penalty(
        self,
        regime: Optional[SpreadRegime] = None,
        spread_pips: Optional[float] = None,
    ) -> float:
        """Return the confidence penalty for ``regime``.

        Exactly one of ``regime`` or ``spread_pips`` must be provided.
        When ``spread_pips`` is given, the regime is computed via
        :meth:`classify` and that regime's penalty is returned. The
        returned value is always within ``[0.5, 1.0]`` per the
        acceptance criteria.
        """
        if regime is None and spread_pips is None:
            raise TypeError("confidence_penalty requires either 'regime' or 'spread_pips'")
        if regime is None:
            # type-checkers: assert narrows spread_pips after the early-exit TypeError
            assert spread_pips is not None  # noqa: S101 — silenced under python -O
            regime = self.classify(spread_pips)
        return _DEFAULT_PENALTIES[regime]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _percentile(sorted_data: Iterable[float], pct: float) -> float:
        """Linear-interpolation percentile on a pre-sorted iterable.

        ``pct`` is in the closed interval ``[0, 100]``. Returns 0.0 for
        an empty input so callers can branch cleanly.
        """
        data = list(sorted_data)
        if not data:
            return 0.0
        if len(data) == 1:
            return data[0]
        if pct <= 0:
            return data[0]
        if pct >= 100:
            return data[-1]

        k = (len(data) - 1) * (pct / 100.0)
        f = int(k)
        c = min(f + 1, len(data) - 1)
        if f == c:
            return data[f]
        # Linear interpolation between data[f] and data[c].
        lower = data[f] * (c - k)
        upper = data[c] * (k - f)
        return lower + upper


__all__ = [
    "SpreadRegime",
    "SpreadRegimeClassifier",
    "_DEFAULT_PENALTIES",
    "TIMEFRAME_WINDOW",
    "DEFAULT_WINDOW",
]
