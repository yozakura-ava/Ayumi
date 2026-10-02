"""Rolling correlation matrix for multi-instrument portfolios.

Computes Pearson correlation between instrument returns using a rolling
window.  Designed to feed into :class:`risk.correlation_sizer.CorrelationAwareSizer`
but is usable standalone for any portfolio analytics.

Historical returns can be supplied directly (dict of symbol -> list of
returns) or loaded from OHLC bar data.

Dual-window support (SRB-AYUMI-001):
    :meth:`compute_multi_window` returns correlation matrices at both
    30-day and 90-day rolling windows in a single call, enabling the
    pair-selection policy to detect short-term and structural correlations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CorrelationMatrix:
    """Rolling Pearson correlation matrix across instruments.

    Parameters
    ----------
    window : int
        Number of return observations to use for the rolling window
        (default 30 — intended as 30 *daily* bars).
    """

    window: int = 30

    # Internal: symbol -> list of period returns (latest at the end)
    _returns: dict[str, list[float]] = field(default_factory=dict, repr=False)
    # Cached matrix after :meth:`compute`
    _matrix: Optional[dict[str, dict[str, float]]] = field(default=None, repr=False)

    # ------------------------------------------------------------------ #
    # Data ingestion
    # ------------------------------------------------------------------ #
    def add_returns(self, symbol: str, returns: list[float]) -> None:
        """Replace the return series for *symbol*."""
        self._returns[symbol] = list(returns)
        self._matrix = None  # invalidate cache

    def add_bar_data(self, symbol: str, closes: list[float]) -> None:
        """Compute simple period returns from a close-price series and store them."""
        if len(closes) < 2:
            self._returns[symbol] = []
            self._matrix = None
            return
        rets: list[float] = []
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            cur = closes[i]
            if prev > 0:
                rets.append((cur - prev) / prev)
            else:
                rets.append(0.0)
        self._returns[symbol] = rets
        self._matrix = None

    # ------------------------------------------------------------------ #
    # Computation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pearson(a: list[float], b: list[float]) -> float:
        """Pearson correlation between two series, truncated to the shorter."""
        n = min(len(a), len(b))
        if n < 2:
            return 0.0
        aa = np.array(a[-n:], dtype=float)
        bb = np.array(b[-n:], dtype=float)
        std_a = aa.std()
        std_b = bb.std()
        if std_a == 0.0 or std_b == 0.0:
            return 0.0
        mean_a = aa.mean()
        mean_b = bb.mean()
        cov = float(np.mean((aa - mean_a) * (bb - mean_b)))
        return cov / (std_a * std_b)

    def compute(self) -> dict[str, dict[str, float]]:
        """Build the full N×N correlation matrix.

        Each entry ``matrix[s1][s2]`` is the Pearson correlation over the
        rolling window.  Only the trailing *window* returns are used.
        """
        symbols = sorted(self._returns.keys())
        result: dict[str, dict[str, float]] = {}
        for s1 in symbols:
            result[s1] = {}
            for s2 in symbols:
                if s1 == s2:
                    result[s1][s2] = 1.0
                else:
                    r1 = self._returns[s1][-self.window :] if self.window else self._returns[s1]
                    r2 = self._returns[s2][-self.window :] if self.window else self._returns[s2]
                    result[s1][s2] = self._pearson(r1, r2)
        self._matrix = result
        return result

    def compute_multi_window(
        self,
        windows: list[int] | None = None,
    ) -> dict[int, dict[str, dict[str, float]]]:
        """Build correlation matrices at multiple rolling windows.

        Parameters
        ----------
        windows
            Rolling window sizes in observations.  Default ``[30, 90]``
            per SRB-AYUMI-001 (30d short-term + 90d structural).

        Returns
        -------
        dict mapping each window size to its N×N correlation matrix.
        """
        if windows is None:
            windows = [30, 90]

        results: dict[int, dict[str, dict[str, float]]] = {}
        original_window = self.window
        for w in windows:
            self.window = w
            results[w] = self.compute()
        # Restore original window and cached matrix
        self.window = original_window
        if original_window in results:
            self._matrix = results[original_window]
        else:
            self.compute()
        return results

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #
    def get_correlation(self, symbol_a: str, symbol_b: str) -> float:
        """Return cached correlation between two symbols.

        Returns 0.0 if either symbol is unknown (graceful missing data).
        """
        if self._matrix is None:
            self.compute()
        assert self._matrix is not None  # noqa: S101 — invariant after lazy init; `compute()` either raises or populates `_matrix`, intentionally silenced under `python -O`
        if symbol_a not in self._matrix or symbol_b not in self._matrix:
            return 0.0
        return self._matrix[symbol_a][symbol_b]

    @property
    def symbols(self) -> list[str]:
        """Symbols currently tracked."""
        return sorted(self._returns.keys())
