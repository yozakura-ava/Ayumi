"""Bollinger Band Width (BBW) volatility classifier.

Provides a secondary volatility signal for the :class:`~regime.detector.RegimeDetector`
to disambiguate the ``VOLATILE`` regime.  ATR percentile under-detects
``VOLATILE`` in instruments like XAUUSD where the per-bar price range is
large but the *relative* band width (price volatility relative to the
prevailing level) gives a cleaner volatility signature.

Public API
----------
- :func ``bbw_percentile`` — BBW percentile rank within a rolling window.

Design contract
---------------
``bbw_percentile`` follows the same vectorised ``pd.Series`` pattern as
``indicators.atr_percentile`` (min-max normalisation inside a rolling
window).  The detector integrates the result as an additive secondary
signal — never as a replacement for the ATR percentile — so the
``none`` confirmation mode preserves byte-identical output to the
pre-BBW detector.

Usage
-----
::

    from regime.bbw_classifier import bbw_percentile

    bbw_pct = bbw_percentile(highs, lows, closes)   # pd.Series in [0, 1]
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from indicators import bollinger_bands

# Default guard against zero/flat denominators.  Chosen small enough not
# to perturb any realistic forex/gold price (XAUUSD trades at ~$1,800 —
# 1e-10 is 18 orders of magnitude smaller) but large enough to keep the
# downstream division finite.
_FLAT_DENOM_GUARD = 1e-10


def bbw_percentile(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    bbw_period: int = 20,
    bbw_lookback: int = 50,
) -> pd.Series:
    """Bollinger Band Width percentile rank within a rolling window.

    Parameters
    ----------
    highs, lows, closes : Sequence[float]
        OHLC price series of equal length.  ``highs``/``lows`` are
        accepted for API parity with ``indicators.atr_percentile`` but
        only the close series is consumed (Bollinger Bands are computed
        from close only).
    bbw_period : int
        Lookback for the Bollinger-band SMA / standard-deviation.
        Default 20 (matches the canonical ``bollinger_bands`` default).
    bbw_lookback : int
        Rolling window over which the BBW percentile rank is computed.
        Default 50 (matches the canonical ``atr_lookback`` default).

    Returns
    -------
    pd.Series
        Series of BBW percentile ranks in [0, 1], aligned to the input
        close index.  Leading bars (warm-up, before both ``bbw_period``
        and ``bbw_lookback`` are satisfied) are ``NaN``.

    Notes
    -----
    Raw ``BBW = (Upper - Lower) / Middle``.  Upper / lower bands use
    ``bbw_period`` SMA ± 2 std of close.  The percentile rank is the
    min-max normalisation

        (BBW - rolling_min(BBW, bbw_lookback))
        / (rolling_max(BBW, bbw_lookback) - rolling_min(BBW, bbw_lookback))

    applied inside a rolling window of size ``bbw_lookback`` — identical
    in shape to ``indicators.atr_percentile``.  When the rolling range
    is zero (every value in the window is the same — flat prices after
    normalisation) the result is ``NaN``; the detector treats those bars
    as base-TF only when ``bbw_confirmation != "none"``.

    Edge cases
    ~~~~~~~~~~
    - ``len(closes) < bbw_period`` → all-NaN series.
    - Flat prices (rolling ``std == 0``) → raw BBW is zero by construction
      (upper == lower == mid), the percentile rank is well-defined
      (NaN from the ``rng.replace(0, np.nan)`` guard only fires when
      *every* BBW value in the window is identical — i.e. truly flat).
    - Zero / negative mid price → guard division with
      ``max(|mid|, 1e-10)`` to keep the raw BBW finite.
    """
    del highs, lows  # accepted for API parity with atr_percentile

    if bbw_period <= 0:
        raise ValueError(f"bbw_period must be > 0 (got {bbw_period!r})")
    if bbw_lookback <= 0:
        raise ValueError(f"bbw_lookback must be > 0 (got {bbw_lookback!r})")

    c = pd.Series(closes, dtype=float)
    n = len(c)

    # Warm-up: not enough bars for even one BBW value.
    if n < bbw_period:
        return pd.Series(np.nan, index=c.index, dtype=float)

    # Bollinger Bands: SMA ± 2 std over ``bbw_period``.
    upper, mid, lower = bollinger_bands(c, period=bbw_period, num_std=2.0)

    # Guard division: protect against zero/negative mid (defensive — no
    # real forex pair trades at zero or below).  ``max(|mid|, 1e-10)``
    # mirrors the spec's ``max(mean, 1e-10)`` guard.  The resulting
    # raw_bbw may briefly be very large for ``|mid| < 1e-10`` inputs;
    # the downstream ``.replace([inf, -inf], nan)`` cleans those up.
    mid_safe = mid.where(mid.abs() >= _FLAT_DENOM_GUARD, _FLAT_DENOM_GUARD)
    raw_bbw = (upper - lower) / mid_safe
    # Bollinger Bands are symmetric around mid, so (upper - lower) is
    # always ≥ 0; the abs() guard above is purely defensive.
    raw_bbw = raw_bbw.replace([np.inf, -np.inf], np.nan)

    # Percentile rank: min-max normalisation inside the rolling window.
    # Identical in shape to ``indicators.atr_percentile``.
    rolling_min = raw_bbw.rolling(
        window=bbw_lookback,
        min_periods=bbw_lookback,
    ).min()
    rolling_max = raw_bbw.rolling(
        window=bbw_lookback,
        min_periods=bbw_lookback,
    ).max()
    rng = rolling_max - rolling_min
    return (raw_bbw - rolling_min) / rng.replace(0, np.nan)


__all__ = ["bbw_percentile"]
