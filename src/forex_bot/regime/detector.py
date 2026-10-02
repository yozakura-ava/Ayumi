"""Market regime detector based on ATR percentile and ADX.

Classifies each bar into one of four regimes and provides conditional
allocation guidance for strategy selection and risk sizing.

Regimes
-------
- **Trending**  — ADX > 25, market is directional.
- **Choppy**    — ADX < 20, market is range-bound.
- **Volatile**  — ATR percentile > 80, risk is elevated.
- **Quiet**     — ATR percentile < 20, opportunity is limited.

Priority when regimes overlap (e.g. high ADX + high ATR):

    VOLATILE > QUIET > TRENDING > CHOPPY

This ensures risk management takes precedence over opportunity.

Public API
----------
- :class ``Regime`` — enum of the four regimes
- :class ``RegimeConfig`` — tunable thresholds
- :class ``RegimeAllocation`` — allocation guidance per regime
- :data ``DEFAULT_ALLOCATIONS`` — mapping of regime → allocation
- :class ``RegimeDetector`` — main detector class

Usage
-----
::

    from regime.detector import RegimeDetector, Regime

    detector = RegimeDetector()
    regimes = detector.detect(highs, lows, closes)   # pd.Series of Regime
    current = detector.detect_current(highs, lows, closes)  # last Regime
    alloc = detector.get_allocation(current)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

import numpy as np
import pandas as pd
from indicators import adx, atr_percentile

from .bbw_classifier import bbw_percentile

# ── Regime enum ────────────────────────────────────────────────────────────


class Regime(str, Enum):
    """Market regime classification."""

    TRENDING = "trending"
    CHOPPY = "choppy"
    VOLATILE = "volatile"
    QUIET = "quiet"


# ── Configuration ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegimeConfig:
    """Tunable thresholds for regime detection.

    Parameters
    ----------
    adx_period : int
        Lookback for ADX calculation.  Default 14.
    atr_period : int
        Lookback for ATR calculation.  Default 14.
    atr_lookback : int
        Rolling window for ATR percentile ranking.  Default 50.
    trending_adx : float
        ADX above this signals a trending regime.  Default 25.0.
    choppy_adx : float
        ADX below this signals a choppy regime.  Default 20.0.
    volatile_atr_pct : float
        ATR percentile (0–1 scale) above this signals volatile.  Default 0.80.
    quiet_atr_pct : float
        ATR percentile (0–1 scale) below this signals quiet.  Default 0.20.
    mtf_confirmation : str
        Higher-timeframe (H1/H4) ADX confirmation mode.

        ``"none"`` (default) — single-TF only, fully backward-compatible.
        ``"soft"`` — when H1 ADX is below ``h1_adx_threshold``, raise the
        effective trending ADX threshold by 3 points for that bar (stricter
        trending classification).
        ``"hard"`` — TRENDING requires base ADX > ``trending_adx`` AND
        H1 ADX > ``h1_adx_threshold`` AND H4 ADX > ``h4_adx_threshold``.
        The neutral-zone slope-based lean is overridden to CHOPPY whenever
        H1 ADX < ``h1_adx_threshold``.
    h1_adx_threshold : float
        H1 ADX threshold used in soft/hard confirmation.  Default 22.0.
    h4_adx_threshold : float
        H4 ADX threshold used in hard confirmation.  Default 20.0.
    bbw_confirmation : str
        Bollinger Band Width secondary confirmation mode for the
        ``VOLATILE`` regime.

        ``"none"`` (default) — single-signal (ATR-only), fully
        backward-compatible.  ``bbw_percentile`` is not computed at all.
        ``"soft"`` — ``VOLATILE`` fires when **either** ``atr_pct`` or
        ``bbw_pct`` exceeds its threshold (broader VOLATILE coverage).
        ``"hard"`` — ``VOLATILE`` fires only when **both** ``atr_pct`` and
        ``bbw_pct`` exceed their thresholds (stricter VOLATILE filter).
        When ``bbw_pct`` is ``NaN`` (warm-up) the detector falls back to
        the ATR-only rule at those bars.
    bbw_period : int
        Bollinger-band SMA / std lookback for ``bbw_percentile``.
        Default 20.
    bbw_lookback : int
        Rolling window for the BBW percentile rank.  Default 50.
    bbw_volatile_pct : float
        BBW percentile (0–1 scale) above which a bar is considered
        ``VOLATILE`` by the BBW half of the confirmation rule.  Default
        0.80.
    """

    adx_period: int = 14
    atr_period: int = 14
    atr_lookback: int = 50
    trending_adx: float = 25.0
    choppy_adx: float = 20.0
    volatile_atr_pct: float = 0.80
    quiet_atr_pct: float = 0.20
    mtf_confirmation: str = "none"
    h1_adx_threshold: float = 22.0
    h4_adx_threshold: float = 20.0
    bbw_confirmation: str = "none"
    bbw_period: int = 20
    bbw_lookback: int = 50
    bbw_volatile_pct: float = 0.80

    def __post_init__(self) -> None:
        mode = str(self.mtf_confirmation).lower()
        if mode not in {"none", "soft", "hard"}:
            raise ValueError(f"mtf_confirmation must be one of 'none', 'soft', 'hard' (got {self.mtf_confirmation!r})")
        # Normalise so equality comparisons do not depend on case.
        object.__setattr__(self, "mtf_confirmation", mode)

        bbw_mode = str(self.bbw_confirmation).lower()
        if bbw_mode not in {"none", "soft", "hard"}:
            raise ValueError(f"bbw_confirmation must be one of 'none', 'soft', 'hard' (got {self.bbw_confirmation!r})")
        # Normalise so equality comparisons do not depend on case.
        object.__setattr__(self, "bbw_confirmation", bbw_mode)


# ── Allocation guidance ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegimeAllocation:
    """Conditional allocation rules for a given regime.

    Attributes
    ----------
    regime : Regime
        The regime this allocation applies to.
    size_multiplier : float
        Fraction of base position size to use (0–1).
    max_concurrent_positions : int
        Maximum simultaneous positions allowed.
    stop_multiplier : float
        Multiplier on base stop distance (e.g. 1.5 = wider stops).
    preferred_strategy_types : list[str]
        Strategy archetypes that perform best in this regime.
    """

    regime: Regime
    size_multiplier: float
    max_concurrent_positions: int
    stop_multiplier: float
    preferred_strategy_types: list[str] = field(default_factory=list)


DEFAULT_ALLOCATIONS: dict[Regime, RegimeAllocation] = {
    Regime.TRENDING: RegimeAllocation(
        regime=Regime.TRENDING,
        size_multiplier=1.0,
        max_concurrent_positions=3,
        stop_multiplier=1.0,
        preferred_strategy_types=["donchian", "breakout", "momentum"],
    ),
    Regime.CHOPPY: RegimeAllocation(
        regime=Regime.CHOPPY,
        size_multiplier=0.75,
        max_concurrent_positions=2,
        stop_multiplier=0.75,
        preferred_strategy_types=["mean_reversion", "bb_rsi", "range"],
    ),
    Regime.VOLATILE: RegimeAllocation(
        regime=Regime.VOLATILE,
        size_multiplier=0.5,
        max_concurrent_positions=1,
        stop_multiplier=1.5,
        preferred_strategy_types=[],
    ),
    Regime.QUIET: RegimeAllocation(
        regime=Regime.QUIET,
        size_multiplier=0.5,
        max_concurrent_positions=1,
        stop_multiplier=1.0,
        preferred_strategy_types=[],
    ),
}


# ── Detector ───────────────────────────────────────────────────────────────


class RegimeDetector:
    """Classify market bars into regimes using ADX and ATR percentile.

    The detector is stateless beyond its configuration — call
    :meth:`detect` with OHLC sequences and receive a ``pd.Series``
    of :class:`Regime` values aligned to the input index.

    Parameters
    ----------
    config : RegimeConfig | None
        Tunable thresholds.  Uses :class:`RegimeConfig` defaults if None.
    allocations : dict[Regime, RegimeAllocation] | None
        Override the default allocation table.
    """

    def __init__(
        self,
        config: RegimeConfig | None = None,
        allocations: dict[Regime, RegimeAllocation] | None = None,
    ) -> None:
        self.config = config or RegimeConfig()
        self._allocations = allocations or DEFAULT_ALLOCATIONS

    # ── Core detection ──────────────────────────────────────────────────

    def detect(
        self,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
    ) -> pd.Series:
        """Classify each bar into a :class:`Regime`.

        Parameters
        ----------
        highs, lows, closes : Sequence[float]
            OHLC price series of equal length.  The price series is assumed
            to be at the detector's base timeframe (M15 in production use).
            When ``cfg.mtf_confirmation`` is ``"soft"`` or ``"hard"`` the
            detector aggregates the base bars up to H1 (4 bars) and H4 (16
            bars) by **bar count** — not by timestamp — to derive
            higher-timeframe ADX for confirmation.

        Returns
        -------
        pd.Series
            Series of :class:`Regime` values, same index/length as input.
            Leading bars will be ``NaN`` until indicators warm up.  When
            ``mtf_confirmation`` is enabled, the warm-up extends past the
            base-TF warm-up by the additional HTF aggregation warm-up
            (H1 ADX needs ~116 base bars before computing; H4 ADX needs
            ~464 base bars).  Bars whose HTF ADX is still NaN fall back to
            base-TF-only classification (no MTF penalty applied).
        """
        cfg = self.config

        adx_series = adx(highs, lows, closes, cfg.adx_period)
        atr_pct = atr_percentile(
            highs,
            lows,
            closes,
            cfg.atr_period,
            cfg.atr_lookback,
        )

        # ── BBW secondary signal (only when confirmation enabled) ──────
        bbw_pct: pd.Series | None = None
        if cfg.bbw_confirmation != "none":
            bbw_pct = bbw_percentile(
                highs,
                lows,
                closes,
                cfg.bbw_period,
                cfg.bbw_lookback,
            )

        n = len(closes)
        result = pd.Series([np.nan] * n, index=pd.RangeIndex(n), dtype=object)

        if n == 0:
            return result

        c = pd.Series(closes, dtype=float)
        result.index = c.index

        # ── Multi-TF ADX (only when confirmation enabled) ──────────────
        h1_adx: pd.Series | None = None
        h4_adx: pd.Series | None = None
        if cfg.mtf_confirmation != "none":
            h1_adx, h4_adx = self._compute_htf_adx(highs, lows, closes, n)

        # ── Effective trending ADX threshold (per-bar) ──────────────────
        # soft: raise by +3 at bars where H1 ADX disagrees (below threshold).
        # hard: require H1 AND H4 confirmation — bars without it cannot be
        #       trending (raise the trending threshold to +inf for those
        #       bars).  The hard-mode override of the neutral-zone slope
        #       lean to CHOPPY when H1 disagrees is applied below after
        #       the masks are built.
        if cfg.mtf_confirmation == "soft" and h1_adx is not None:
            h1_disagrees = ((h1_adx < cfg.h1_adx_threshold) & h1_adx.notna()).reindex(result.index, fill_value=False)
            trending_thr_bar: np.ndarray = np.where(
                h1_disagrees.to_numpy(),
                cfg.trending_adx + 3.0,
                cfg.trending_adx,
            )
        elif cfg.mtf_confirmation == "hard" and h1_adx is not None and h4_adx is not None:
            # Bars where H1 OR H4 disagree → trending requires an
            # impossible threshold.  Bars where both confirm → standard
            # threshold.
            h1_disagrees = ((h1_adx < cfg.h1_adx_threshold) & h1_adx.notna()).reindex(result.index, fill_value=False)
            h4_disagrees = ((h4_adx < cfg.h4_adx_threshold) & h4_adx.notna()).reindex(result.index, fill_value=False)
            hard_invalid = (h1_disagrees | h4_disagrees).to_numpy()
            trending_thr_bar = np.where(
                hard_invalid,
                np.inf,
                cfg.trending_adx,
            )
        else:
            trending_thr_bar = np.full(n, cfg.trending_adx)

        # Priority 1: Volatile (ATR percentile above threshold, optionally
        # confirmed by the BBW secondary signal).  The BBW half of the
        # rule is only consulted when ``cfg.bbw_confirmation != "none"``
        # — ``none`` (the default) preserves byte-identical output to
        # the pre-BBW detector.  In ``soft`` mode the rule is broadened
        # (OR); in ``hard`` mode it is tightened (AND).  When ``bbw_pct``
        # is ``NaN`` (warm-up before ``bbw_period + bbw_lookback`` bars)
        # the detector falls back to the ATR-only rule at those bars.
        if cfg.bbw_confirmation == "soft" and bbw_pct is not None:
            # NaN in ``bbw_pct`` is treated as False by the OR — exactly
            # what the spec requires for soft-mode warm-up.
            volatile_mask = (atr_pct > cfg.volatile_atr_pct) | (bbw_pct > cfg.bbw_volatile_pct)
        elif cfg.bbw_confirmation == "hard" and bbw_pct is not None:
            # For hard mode, a NaN ``bbw_pct`` would make the AND return
            # False (NaN propagates) — that would mark warm-up bars as
            # not-VOLATILE, which is the OPPOSITE of "fall back to
            # ATR-only".  Replace NaN with +inf so the AND degrades
            # cleanly to ``atr_pct > cfg.volatile_atr_pct`` at those
            # bars (inf > threshold is always True).
            bbw_safe = bbw_pct.fillna(np.inf)
            volatile_mask = (atr_pct > cfg.volatile_atr_pct) & (bbw_safe > cfg.bbw_volatile_pct)
        else:
            volatile_mask = atr_pct > cfg.volatile_atr_pct

        # Priority 2: Quiet (ATR percentile below threshold)
        quiet_mask = atr_pct < cfg.quiet_atr_pct

        # Priority 3: Trending (ADX above per-bar threshold, not volatile/quiet)
        trending_mask = (adx_series.to_numpy() > trending_thr_bar) & ~volatile_mask & ~quiet_mask

        # Priority 4: Choppy (ADX below threshold, not volatile/quiet/trending)
        choppy_mask = (adx_series < cfg.choppy_adx) & ~volatile_mask & ~quiet_mask & ~trending_mask

        # Neutral zone (choppy_adx ≤ ADX ≤ trending_adx, normal ATR):
        # lean trending if ADX is rising, choppy if falling.  The upper
        # bound uses the per-bar threshold so that hard-mode bars whose
        # threshold is +inf are not classified into the neutral zone
        # either way (they fall through to choppy / via the override).
        adx_slope = adx_series.diff()
        neutral_in_zone = (
            (adx_series >= cfg.choppy_adx) & (adx_series.to_numpy() <= trending_thr_bar) & ~volatile_mask & ~quiet_mask
        )
        neutral_trending = neutral_in_zone & (adx_slope > 0)
        neutral_choppy = neutral_in_zone & (adx_slope <= 0)

        # Hard-mode override of the neutral-zone slope-based lean: when
        # EITHER H1 or H4 ADX is below threshold (and known), force
        # CHOPPY regardless of whether ADX is rising or falling on the
        # base TF.  The trending threshold is already raised to +inf at
        # bars where either timeframe disagrees (see above), so the
        # trending_mask itself cannot fire there — but the neutral-zone
        # slope-based lean can still flip such bars to TRENDING when
        # H1 confirms and only H4 disagrees.  This override closes that
        # H4-only leakage.
        if cfg.mtf_confirmation == "hard" and h1_adx is not None and h4_adx is not None:
            h1_below = ((h1_adx < cfg.h1_adx_threshold) & h1_adx.notna()).reindex(result.index, fill_value=False)
            h4_below = ((h4_adx < cfg.h4_adx_threshold) & h4_adx.notna()).reindex(result.index, fill_value=False)
            htf_disagrees = h1_below | h4_below
            hard_force_choppy = neutral_in_zone & htf_disagrees
            neutral_choppy = neutral_choppy | hard_force_choppy
            neutral_trending = neutral_trending & ~htf_disagrees

        result[volatile_mask] = Regime.VOLATILE
        result[quiet_mask] = Regime.QUIET
        result[trending_mask] = Regime.TRENDING
        result[choppy_mask] = Regime.CHOPPY
        result[neutral_trending] = Regime.TRENDING
        result[neutral_choppy] = Regime.CHOPPY

        return result

    # ── Multi-timeframe ADX helpers ────────────────────────────────────

    def _compute_htf_adx(
        self,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        n: int,
    ) -> tuple[pd.Series, pd.Series]:
        """Aggregate base-TF bars to H1/H4 by bar count and compute ADX.

        Aggregation is by **bar count** (4 base bars → 1 H1, 16 → 1 H4)
        rather than by elapsed timestamp so that gaps (weekends, holidays,
        missing data) are handled by the same arithmetic without needing
        an explicit calendar — they simply produce a stretched/contracted
        H1/H4 view of the price action.

        Returns ``(h1_adx, h4_adx)`` as :class:`pandas.Series` aligned to
        ``RangeIndex(n)``.  HTF ADX values are broadcast across the
        ``period_bars`` base-TF bars in each group and forward-filled so
        the per-bar read of HTF ADX is consistent across the entire
        aggregation period.  Leading bars (before the HTF ADX warm-up)
        remain ``NaN``; :meth:`detect` treats those bars as base-TF only.
        """
        cfg = self.config
        idx = pd.RangeIndex(n)
        empty_h1 = pd.Series(np.nan, index=idx, dtype=float)
        empty_h4 = pd.Series(np.nan, index=idx, dtype=float)

        # Need at least 2*adx_period + 1 base bars for the M15 ADX warm-up
        # before HTF aggregation is meaningful.  Below this, return NaN
        # series so detect() falls back to base-TF only.
        if n < 2 * cfg.adx_period + 1:
            return empty_h1, empty_h4

        highs_arr = np.asarray(highs, dtype=float)
        lows_arr = np.asarray(lows, dtype=float)
        closes_arr = np.asarray(closes, dtype=float)

        def _aggregate(
            period_bars: int,
        ) -> tuple[list[float], list[float], list[float]]:
            n_agg = n // period_bars
            if n_agg == 0:
                return [], [], []
            h_agg: list[float] = []
            l_agg: list[float] = []
            c_agg: list[float] = []
            for j in range(n_agg):
                start = j * period_bars
                end = start + period_bars
                h_agg.append(float(np.max(highs_arr[start:end])))
                l_agg.append(float(np.min(lows_arr[start:end])))
                c_agg.append(float(closes_arr[end - 1]))
            return h_agg, l_agg, c_agg

        def _broadcast_to_m15(series_agg: "pd.Series | None", period_bars: int) -> pd.Series:
            if series_agg is None or len(series_agg) == 0:
                return pd.Series(np.nan, index=idx, dtype=float)
            result = pd.Series(np.nan, index=idx, dtype=float)
            for j in range(len(series_agg)):
                start = j * period_bars
                end = min(start + period_bars, n)
                result.iloc[start:end] = series_agg.iloc[j]
            # Forward-fill any leading NaN cells (rare with bar-count
            # aggregation; defensive against edge cases like zero
            # period_bars so split() errors are not raised).
            return result.ffill()

        # H1 = 4 base bars, H4 = 16 base bars
        h1_h, h1_l, h1_c = _aggregate(4)
        h1_adx_agg = adx(h1_h, h1_l, h1_c, cfg.adx_period) if h1_h else None
        h1_adx = _broadcast_to_m15(h1_adx_agg, 4)

        h4_h, h4_l, h4_c = _aggregate(16)
        h4_adx_agg = adx(h4_h, h4_l, h4_c, cfg.adx_period) if h4_h else None
        h4_adx = _broadcast_to_m15(h4_adx_agg, 16)

        return h1_adx, h4_adx

    def detect_current(
        self,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
    ) -> Regime:
        """Return the regime of the most recent completed bar.

        Falls back to :attr:`Regime.QUIET` if no bars have enough
        data for indicator warm-up.
        """
        series = self.detect(highs, lows, closes)
        valid = series.dropna()
        if valid.empty:
            return Regime.QUIET
        return Regime(valid.iloc[-1])

    # ── Allocation ──────────────────────────────────────────────────────

    def get_allocation(self, regime: Regime) -> RegimeAllocation:
        """Return the allocation guidance for *regime*.

        Falls back to the QUIET allocation if *regime* is unknown.
        """
        return self._allocations.get(regime, DEFAULT_ALLOCATIONS[Regime.QUIET])

    def get_current_allocation(
        self,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
    ) -> RegimeAllocation:
        """Detect the current regime and return its allocation guidance."""
        return self.get_allocation(self.detect_current(highs, lows, closes))


__all__ = [
    "Regime",
    "RegimeConfig",
    "RegimeAllocation",
    "DEFAULT_ALLOCATIONS",
    "RegimeDetector",
]
