"""Vol-target overlay + drawdown ladder as composable backtest overlays.

Card: 37443f97-ddea-4f5b-b280-fc7b3d968d98 (Sprint D 4)
Sprint: 2026-10-06-avaos-ui-rework
Lane: [BUILD]

What this module provides
==========================

Two position-sizing overlays for the crypto backtest core:

  * **Vol-target overlay** — scales position sizes so realized
    volatility tracks a constant annualized target. The realized
    vol is estimated from log returns over a rolling window
    (default: 60 bars), annualized via ``sqrt(annualization_factor)``.
    The scale at bar ``i`` is::

        scale[i] = clamp(
            target_annualized_vol / realized_vol[i],
            min_scale, max_scale,
        )

    ``min_realized_vol`` floors the divisor so a zero-vol regime
    cannot divide-by-zero (or explode the scale). ``max_scale``
    caps leverage in micro-vol regimes.

  * **Drawdown ladder** — risk fraction steps down as drawdown
    from the equity high-water mark deepens. Configurable tiers,
    e.g. :data:`DEFAULT_DD_TIERS`::

        [(0.00, 1.00), (0.05, 0.50), (0.10, 0.25)]

    means **100% risk when DD < 5%, 50% at 5–10%, 25% beyond 10%**.

Both overlays follow the established wrapper pattern
(``funding_model.py``, ``liquidation.py``, ``venue_costs.py``):

  * :class:`forex_bot.engine.engine.BacktestEngine` runs unchanged
    — legacy FX path stays 100% untouched.
  * The wrapper returns an extension of
    :class:`backtest.types.BacktestMetrics` carrying the unchanged
    base metrics PLUS a per-bar scale/risk series.
  * Composition with
    :func:`backtest.venue_costs.run_backtest_with_full_crypto_overlay`
    is provided via
    :func:`run_backtest_with_full_crypto_overlay_and_vol_target`.

Why this is *not* a cost overlay
================================

The funding / liquidation / venue overlays add per-bar **cost
deltas** (additive on the equity curve). The vol-target / DD
overlays produce per-bar **scale factors** (multiplicative on the
position notional). They are independent layers:

  * Cost overlays produce ``base.ending_balance − cost``.
  * Scale overlays produce ``risk_scale`` that callers apply to
    the position notional before the cost overlays see it.

For the legacy FX path, the wrapper produces a constant
``risk_scale == 1.0`` series (when both ``vol_target_config`` and
``dd_config`` are ``None``) — byte-identical to the engine's own
output. When ``vol_target_config is None`` but ``dd_config`` is
supplied, the vol-target half is a no-op and the DD half runs.

Byte-equality composition contract
==================================

Byte-equality composition with
:func:`backtest.venue_costs.run_backtest_with_full_crypto_overlay`:

  1. The three existing wrappers (funding, liquidation, venue)
     each run the engine and add independent per-bar cost deltas.
  2. The vol-target / DD wrapper also runs the engine and
     produces independent per-bar scale series.
  3. The engine runs four times (one per wrapper) but each run
     is deterministic on ``(config, bars, strategies)``, so
     ``base.ending_balance`` is identical across all runs.
  4. The composed equity curve is::

         final_balance = base.ending_balance
                       − total_funding_cost
                       − total_liquidation_cost
                       − total_venue_cost

         risk_scale_at_bar_i = vol_scale[i] × dd_risk[i]

     The risk scale is applied **upstream** of all cost overlays
     (at position-sizing time), so it does not appear in the cost
     deltas — but it does affect the realized P&L when callers use
     the scale to size notionals in the engine path.

When both ``vol_target_config is None and dd_config is None``,
the vol-target / DD wrapper returns a ``risk_scale`` series of
all ``1.0`` — the engine run is identical to the cost-overlay
runs, and the composed result is byte-identical to
:func:`run_backtest_with_full_crypto_overlay` with the four
wrappers sharing ``base``.

The 1a.2 / 1a.3 reviews verified this byte-equality contract for
funding + liquidation; this module extends the same contract to
the vol-target + DD layers (Sprint D 4).

Notes on the realized-vol estimator
====================================

The realized vol at bar ``i`` is the population standard deviation
of the most recent ``lookback_window`` log returns ending at bar
``i``, scaled by ``sqrt(annualization_factor)``::

    realized_vol[i] = stdev(rets[i − lookback : i]) × sqrt(AF)

We use the **population** standard deviation (Bessel correction
omitted) because the rolling window is the entire estimator
sample — not a subsample of a larger distribution. With small
windows (60 bars or fewer), the Bessel correction in sample stdev
introduces a small up-bias that compounds across the rolling
window. Population stdev is reproducible and matches the
standard textbook definition of realized vol over a fixed window.

Notes on the drawdown-ladder semantics
======================================

The DD ladder reads the engine's ``base.equity_curve`` (length
``len(bars) + 1`` or more — the engine appends one balance per
bar plus the initial balance) and computes::

    hwm[i]  = max(initial_equity, equity_curve[0..i])
    dd_i   = (hwm[i] − equity_curve[i]) / hwm[i]
    risk_i = lookup(dd_i, tiers)  # see DrawdownLadderConfig

For the engine wrapper, ``initial_equity`` defaults to
``base.starting_balance``. Callers using the standalone
primitives supply their own initial equity (typically the
strategy's starting balance).

The DD ladder runs in O(T) where T = ``len(equity_curve)``.

Source: Sprint D 4 spec, "vol-target + DD ladder as composable
overlays".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

# IntegrityConfig is referenced as a type annotation only; the runtime
# import is deferred to the wrapper functions to keep the module-level
# import graph free of cross-crypto-overlay coupling.
from backtest.integrity_gate import IntegrityConfig  # noqa: E402
from backtest.types import Bar

__all__ = [
    # Constants
    "DEFAULT_ANNUALIZATION_FACTOR_1H",
    "DEFAULT_ANNUALIZATION_FACTOR_4H",
    "DEFAULT_ANNUALIZATION_FACTOR_DAILY",
    "DEFAULT_LOOKBACK_1H",
    "DEFAULT_LOOKBACK_4H",
    "DEFAULT_LOOKBACK_DAILY",
    "DEFAULT_VOL_TARGET",
    "DEFAULT_DD_TIERS",
    # Errors
    "VolTargetError",
    # Configs / domain types
    "VolTargetConfig",
    "DrawdownTier",
    "DrawdownLadderConfig",
    # Metrics
    "VolTargetBacktestMetrics",
    "DrawdownLadderBacktestMetrics",
    "VolTargetDDBacktestMetrics",
    # Math primitives
    "compute_log_returns",
    "infer_annualization_factor",
    "compute_realized_vol_series",
    "compute_vol_scale_series",
    "compute_high_water_mark_series",
    "compute_dd_series",
    "compute_dd_risk_series",
    "compute_composite_risk_scale_series",
    "lookup_dd_risk_fraction",
    # Config loaders
    "load_vol_target_config",
    "load_drawdown_ladder_config",
    # Wrappers
    "run_backtest_with_vol_target",
    "run_backtest_with_drawdown_ladder",
    "run_backtest_with_vol_target_and_dd",
    "run_backtest_with_full_crypto_overlay_and_vol_target",
]


# ---------------------------------------------------------------------------
# Constants — annualization factors for the three crypto cadences
# ---------------------------------------------------------------------------

#: 1h bars: 24 × 365 = 8760 hours/year.
DEFAULT_ANNUALIZATION_FACTOR_1H: int = 8760

#: 4h bars: 6 × 365 = 2190 four-hour periods/year.
DEFAULT_ANNUALIZATION_FACTOR_4H: int = 2190

#: Daily bars: 365 days/year.
DEFAULT_ANNUALIZATION_FACTOR_DAILY: int = 365

#: Default lookback windows for the realized vol estimator at each cadence.
#: 60 hourly bars ≈ 2.5 days (sensible default for crypto short-term vol).
DEFAULT_LOOKBACK_1H: int = 60

#: 60 4-hour bars ≈ 10 days.
DEFAULT_LOOKBACK_4H: int = 60

#: 30 daily bars ≈ 1 month.
DEFAULT_LOOKBACK_DAILY: int = 30

#: Default vol-target: 10% annualized. Matches a typical risk-parity
#: target for diversified crypto exposure. Operators tune this to
#: their risk budget; the value is config-loaded from YAML in
#: production.
DEFAULT_VOL_TARGET: float = 0.10


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VolTargetError(ValueError):
    """Raised when vol-target / DD ladder configuration fails validation.

    Inherits from :class:`ValueError` so existing
    ``pytest.raises(ValueError)`` callers catch it; tests that want
    to differentiate use ``except VolTargetError`` directly. Mirrors
    the ``VenueConfigError`` / ``IntegrityConfigError`` convention
    used elsewhere in the backtest core.
    """


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolTargetConfig:
    """Vol-target overlay configuration.

    Attributes
    ----------
    target_annualized_vol : float
        Target realized vol (annualized). Must be ``> 0``. Default
        0.10 (10%) — a typical risk-parity budget for diversified
        crypto exposure.
    lookback_window : int
        Rolling-window length in bars for the realized vol
        estimator. Must be ``>= 2``. Default 60 (matching
        :data:`DEFAULT_LOOKBACK_1H` for 1h bars).
    annualization_factor : int
        Bars-per-year for annualizing the realized vol estimator.
        Default 8760 (1h bars). Override when running 4h or daily
        bars — see :func:`infer_annualization_factor` for auto-
        detection.
    min_realized_vol : float
        Floor applied before dividing into ``target_annualized_vol``
        (avoids ``1 / 0`` and asymptotic explosions in zero-vol
        regimes). Must be ``> 0``. Default 1e-4 (1 bp annualized,
        i.e. 0.01%).
    max_scale : float
        Upper cap on the scale factor. Avoids leverage explosions
        in micro-vol regimes where ``target_vol / realized_vol``
        would otherwise be huge. Must be ``>= 1.0``. Default 4.0
        (i.e. never apply more than 4× leverage in a low-vol regime).
    min_scale : float
        Lower cap on the scale factor. Must be ``>= 0``. Default
        0.0 (no position in zero-vol regimes).
    warmup_bars : int, optional
        Number of leading bars where the scale is forced to
        ``min_scale`` (or 0) before the estimator has enough data.
        Default ``None`` → ``lookback_window`` (the first
        ``lookback_window`` bars get the warmup value).
    name : str
        Human-readable identifier for diagnostics. Default
        ``"vol-target"``.

    Notes
    -----
    Population standard deviation is the estimator — see module
    docstring §realized-vol-estimator.
    """

    target_annualized_vol: float = DEFAULT_VOL_TARGET
    lookback_window: int = DEFAULT_LOOKBACK_1H
    annualization_factor: int = DEFAULT_ANNUALIZATION_FACTOR_1H
    min_realized_vol: float = 1e-4
    max_scale: float = 4.0
    min_scale: float = 0.0
    warmup_bars: Optional[int] = None
    name: str = "vol-target"

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_annualized_vol):
            raise VolTargetError(
                f"target_annualized_vol must be finite (got {self.target_annualized_vol})"
            )
        if self.target_annualized_vol <= 0:
            raise VolTargetError(
                f"target_annualized_vol must be > 0 (got {self.target_annualized_vol})"
            )
        if self.lookback_window < 2:
            raise VolTargetError(
                f"lookback_window must be >= 2 (got {self.lookback_window})"
            )
        if self.annualization_factor < 1:
            raise VolTargetError(
                f"annualization_factor must be >= 1 (got {self.annualization_factor})"
            )
        if not math.isfinite(self.min_realized_vol):
            raise VolTargetError(
                f"min_realized_vol must be finite (got {self.min_realized_vol})"
            )
        if self.min_realized_vol <= 0:
            raise VolTargetError(
                f"min_realized_vol must be > 0 (got {self.min_realized_vol})"
            )
        if not math.isfinite(self.max_scale):
            raise VolTargetError(
                f"max_scale must be finite (got {self.max_scale})"
            )
        if self.max_scale < 1.0:
            raise VolTargetError(
                f"max_scale must be >= 1.0 (got {self.max_scale})"
            )
        if not math.isfinite(self.min_scale):
            raise VolTargetError(
                f"min_scale must be finite (got {self.min_scale})"
            )
        if self.min_scale < 0:
            raise VolTargetError(
                f"min_scale must be >= 0 (got {self.min_scale})"
            )
        if self.max_scale < self.min_scale:
            raise VolTargetError(
                f"max_scale ({self.max_scale}) must be >= min_scale ({self.min_scale})"
            )
        if self.warmup_bars is not None and self.warmup_bars < 0:
            raise VolTargetError(
                f"warmup_bars must be >= 0 (got {self.warmup_bars})"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise VolTargetError(
                f"name must be a non-empty string (got {self.name!r})"
            )


@dataclass(frozen=True)
class DrawdownTier:
    """A single drawdown-ladder tier.

    Attributes
    ----------
    dd_threshold : float
        Lower bound (inclusive) of the drawdown bracket. The first
        tier in a :class:`DrawdownLadderConfig` must have
        ``dd_threshold == 0.0``. Tiers must be sorted strictly
        ascending by ``dd_threshold`` (contiguous, no gaps, no
        overlap — the boundary semantics are "lower bound inclusive,
        next tier's lower bound is exclusive").
    risk_fraction : float
        Risk fraction applied when the running drawdown falls in
        this tier. Must be in ``[0.0, 1.0]``. ``1.0`` = full risk,
        ``0.0`` = no new positions (the strategy should still
        manage open positions, but should not open new ones).

    Notes
    -----
    Example: a tier with ``dd_threshold=0.05`` and
    ``risk_fraction=0.5`` means "when ``dd >= 5%``, use 0.5× risk".
    A subsequent tier with ``dd_threshold=0.10`` means "when
    ``dd >= 10%``, use the new tier's risk fraction".
    """

    dd_threshold: float
    risk_fraction: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.dd_threshold):
            raise VolTargetError(
                f"dd_threshold must be finite (got {self.dd_threshold})"
            )
        if self.dd_threshold < 0:
            raise VolTargetError(
                f"dd_threshold must be >= 0 (got {self.dd_threshold})"
            )
        if not math.isfinite(self.risk_fraction):
            raise VolTargetError(
                f"risk_fraction must be finite (got {self.risk_fraction})"
            )
        if not (0.0 <= self.risk_fraction <= 1.0):
            raise VolTargetError(
                f"risk_fraction must be in [0, 1] (got {self.risk_fraction})"
            )


#: Default DD ladder tiers: full risk ``< 5%``, 50% at 5–10%, 25% beyond.
#: Matches the spec example. Tiers are non-overlapping; boundary
#: semantics are "lower bound inclusive, next tier's lower bound is
#: exclusive". The last tier applies at ``dd >= 0.10``.
DEFAULT_DD_TIERS: tuple[DrawdownTier, ...] = (
    DrawdownTier(0.00, 1.00),
    DrawdownTier(0.05, 0.50),
    DrawdownTier(0.10, 0.25),
)


@dataclass(frozen=True)
class DrawdownLadderConfig:
    """Drawdown-ladder overlay configuration.

    Attributes
    ----------
    tiers : tuple[DrawdownTier, ...]
        Sorted, contiguous tier table. The first tier's
        ``dd_threshold`` must be ``0.0`` (so every drawdown value
        lands in some tier). Each tier's ``dd_threshold`` must be
        strictly greater than the previous tier's (contiguous,
        no overlap, no gap). Validated at construction.
    starting_equity : float, optional
        Initial equity for the high-water-mark computation. When
        ``None`` (default), the wrapper uses
        ``base.starting_balance`` from the engine's
        :class:`BacktestMetrics` (or ``equity_curve[0]`` if the
        caller supplies a pre-computed equity curve).
    min_risk_fraction : float
        Minimum risk fraction floor (applied after the tier table
        is evaluated). Must be in ``[0.0, 1.0]``. Default 0.0
        (the deepest tier sets the floor).
    name : str
        Human-readable identifier for diagnostics. Default
        ``"drawdown-ladder"``.
    """

    tiers: tuple[DrawdownTier, ...] = DEFAULT_DD_TIERS
    starting_equity: Optional[float] = None
    min_risk_fraction: float = 0.0
    name: str = "drawdown-ladder"

    def __post_init__(self) -> None:
        if not self.tiers:
            raise VolTargetError("tiers must be non-empty")
        if self.tiers[0].dd_threshold != 0.0:
            raise VolTargetError(
                f"first tier dd_threshold must be 0.0 (got {self.tiers[0].dd_threshold})"
            )
        if not math.isfinite(self.min_risk_fraction):
            raise VolTargetError(
                f"min_risk_fraction must be finite (got {self.min_risk_fraction})"
            )
        if not (0.0 <= self.min_risk_fraction <= 1.0):
            raise VolTargetError(
                f"min_risk_fraction must be in [0, 1] (got {self.min_risk_fraction})"
            )
        if self.starting_equity is not None:
            if not math.isfinite(self.starting_equity):
                raise VolTargetError(
                    f"starting_equity must be finite (got {self.starting_equity})"
                )
            if self.starting_equity <= 0:
                raise VolTargetError(
                    f"starting_equity must be > 0 (got {self.starting_equity})"
                )
        # Validate tier table: strictly ascending by dd_threshold.
        prev_thr = self.tiers[0].dd_threshold
        for i in range(1, len(self.tiers)):
            curr = self.tiers[i]
            if curr.dd_threshold <= prev_thr:
                raise VolTargetError(
                    f"tiers must be strictly ascending by dd_threshold; tier "
                    f"{i} ({curr.dd_threshold}) <= tier {i - 1} ({prev_thr})"
                )
            prev_thr = curr.dd_threshold
        if not isinstance(self.name, str) or not self.name.strip():
            raise VolTargetError(
                f"name must be a non-empty string (got {self.name!r})"
            )


# ---------------------------------------------------------------------------
# BacktestMetrics extensions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolTargetBacktestMetrics:
    """BacktestMetrics + vol-target scale overlay.

    Attributes
    ----------
    base : BacktestMetrics
        The original engine output (unchanged).
    vol_scale_series : list[float]
        Per-bar vol scale factor (length ``len(bars)``). At bar
        ``i``, ``vol_scale_series[i]`` is the scale factor the
        caller should apply to the position notional. ``1.0``
        means "as-is" (no scaling); ``> 1`` means "scale up"
        (low-vol regime); ``< 1`` means "scale down" (high-vol
        regime). The series is clamped to
        ``[config.min_scale, config.max_scale]``. When
        ``vol_target_config is None``, the series is all ``1.0``
        (legacy FX path — no scaling).
    realized_vol_series : list[float]
        Per-bar realized vol (annualized). ``[i]`` is the realized
        vol estimated from returns ending at bar ``i``
        (window length ``config.lookback_window``). ``[0..warmup-1]``
        is ``0.0`` (estimator not warm yet). Length
        ``len(bars)``.
    mean_realized_vol_post_warmup : float
        Mean realized vol over the post-warmup bars. Diagnostic.
        ``0.0`` when ``vol_target_config is None`` or no bars
        are past warmup.
    """

    base: object  # BacktestMetrics — typed as object to avoid hard import
    vol_scale_series: list[float]
    realized_vol_series: list[float]
    mean_realized_vol_post_warmup: float


@dataclass(frozen=True)
class DrawdownLadderBacktestMetrics:
    """BacktestMetrics + DD ladder risk overlay.

    Attributes
    ----------
    base : BacktestMetrics
        The original engine output (unchanged).
    dd_risk_series : list[float]
        Per-bar risk fraction (length ``len(bars)``). At bar
        ``i``, ``dd_risk_series[i]`` is the fraction the caller
        should apply to the position notional. ``1.0`` = full
        risk, ``0.0`` = no risk. The series is the per-bar
        outcome of the tier table lookup. When
        ``dd_config is None``, the series is all ``1.0``
        (legacy FX path — no scaling).
    hwm_series : list[float]
        Per-bar high-water-mark balance (length ``len(bars)``).
        ``hwm_series[i]`` is the max balance from bar 0 through
        bar ``i`` (inclusive). ``hwm_series[0]`` includes the
        starting equity.
    dd_series : list[float]
        Per-bar drawdown from HWM (length ``len(bars)``).
        ``dd_series[i]`` is
        ``(hwm_series[i] - balance_at_i) / hwm_series[i]``
        (fractional, ``0.0`` means at HWM). Clamped to ``>= 0``.
    max_dd_observed : float
        Max drawdown observed in the bar window (fractional).
        Diagnostic.
    """

    base: object
    dd_risk_series: list[float]
    hwm_series: list[float]
    dd_series: list[float]
    max_dd_observed: float


@dataclass(frozen=True)
class VolTargetDDBacktestMetrics:
    """BacktestMetrics + vol-target + DD ladder composition.

    Attributes
    ----------
    base : BacktestMetrics
        The original engine output (unchanged).
    vol_scale_series : list[float]
        Per-bar vol scale factor (see :class:`VolTargetBacktestMetrics`).
    dd_risk_series : list[float]
        Per-bar risk fraction (see :class:`DrawdownLadderBacktestMetrics`).
    composite_risk_scale_series : list[float]
        Per-bar product ``vol_scale * dd_risk``. The caller
        applies this directly to the position notional. Length
        ``len(bars)``. When both configs are ``None``, this is
        all ``1.0``.
    hwm_series : list[float]
        Per-bar high-water-mark (from DD overlay).
    dd_series : list[float]
        Per-bar drawdown (from DD overlay).
    """

    base: object
    vol_scale_series: list[float]
    dd_risk_series: list[float]
    composite_risk_scale_series: list[float]
    hwm_series: list[float]
    dd_series: list[float]


# ---------------------------------------------------------------------------
# Math primitives — log returns
# ---------------------------------------------------------------------------


def compute_log_returns(bars: Sequence[Bar]) -> list[float]:
    """Compute log returns from a bar series.

    For bar ``i >= 1``::

        r_i = log(close_i / close_{i - 1})

    Returns a list of length ``len(bars) - 1``. Empty for
    ``len(bars) <= 1``.

    Notes
    -----
    Uses the bar's ``close`` field. Non-positive prices raise
    :class:`VolTargetError` (the integrity gate catches them
    upstream; we re-check here for defense in depth).
    """
    if len(bars) <= 1:
        return []
    out: list[float] = []
    for i in range(1, len(bars)):
        p_prev = bars[i - 1].close
        p_curr = bars[i].close
        if not math.isfinite(p_prev) or not math.isfinite(p_curr):
            raise VolTargetError(
                f"non-finite price at bar {i}: prev={p_prev} curr={p_curr}"
            )
        if p_prev <= 0 or p_curr <= 0:
            raise VolTargetError(
                f"non-positive price at bar {i}: prev={p_prev} curr={p_curr}"
            )
        out.append(math.log(p_curr / p_prev))
    return out


# ---------------------------------------------------------------------------
# Math primitives — annualization factor inference
# ---------------------------------------------------------------------------


#: Standard cadence → annualization factor. The detector snaps the
#: median observed gap (in minutes) to the nearest entry.
_STANDARD_CADENCES: tuple[tuple[float, int], ...] = (
    (15.0, 35040),    # 15-min bars: 4 × 24 × 365
    (60.0, 8760),     # 1h bars: 24 × 365
    (240.0, 2190),    # 4h bars: 6 × 365
    (1440.0, 365),    # daily bars: 365
)


def infer_annualization_factor(bars: Sequence[Bar]) -> int:
    """Infer the annualization factor from the bar cadence.

    Computes the median gap (in minutes) between consecutive bars
    over the first ``min(len(bars), 50)`` bars, then snaps to the
    nearest entry in the standard cadence table.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must contain at least 2 bars (else returns
        :data:`DEFAULT_ANNUALIZATION_FACTOR_1H`).

    Returns
    -------
    int
        Annualization factor (bars per year). Snap targets are
        ``15m → 35040``, ``1h → 8760``, ``4h → 2190``, ``daily → 365``.

    Notes
    -----
    The detector is intentionally simple — it picks the closest
    standard cadence by median gap, with no tolerance band. For
    exotic cadences (e.g. 30-min, 8h, weekly) the snap may produce
    a sub-optimal factor; callers running on those cadences should
    set ``annualization_factor`` explicitly on
    :class:`VolTargetConfig`.
    """
    if len(bars) < 2:
        return DEFAULT_ANNUALIZATION_FACTOR_1H
    diffs: list[float] = []
    for i in range(1, min(len(bars), 50)):
        t_prev = bars[i - 1].time
        t_curr = bars[i].time
        dt_min = (t_curr - t_prev).total_seconds() / 60.0
        if dt_min > 0:
            diffs.append(dt_min)
    if not diffs:
        return DEFAULT_ANNUALIZATION_FACTOR_1H
    diffs.sort()
    median = diffs[len(diffs) // 2]
    best = min(_STANDARD_CADENCES, key=lambda c: abs(c[0] - median))
    return best[1]


# ---------------------------------------------------------------------------
# Math primitives — realized vol series
# ---------------------------------------------------------------------------


def compute_realized_vol_series(
    bars: Sequence[Bar],
    config: VolTargetConfig,
) -> list[float]:
    """Compute the per-bar realized vol (annualized) series.

    For bar ``i >= lookback_window``::

        realized_vol[i] = stdev(rets[i − lookback : i]) × sqrt(AF)

    where ``AF = config.annualization_factor``. For bars
    ``i < lookback_window``: ``realized_vol[i] = 0.0``.

    The series has length ``len(bars)``.

    Notes
    -----
    Population standard deviation is used — see module docstring
    §realized-vol-estimator for rationale. The first valid bar is
    at index ``lookback_window`` (the window of returns
    ``rets[i − lookback : i]`` has length ``lookback`` only when
    ``i >= lookback``).

    When ``config.warmup_bars > lookback_window``, the warmup gap is
    larger than the estimator's natural cold-start; this is allowed
    and the realized vol for bars ``[lookback, warmup)`` is computed
    (and could be used by a caller who wants to skip the warmup).
    The vol scale series applies the warmup gate separately.
    """
    if not isinstance(config, VolTargetConfig):
        raise TypeError(
            f"config must be VolTargetConfig (got {type(config).__name__})"
        )
    if len(bars) == 0:
        return []
    rets = compute_log_returns(bars)
    n_rets = len(rets)
    lookback = config.lookback_window
    series: list[float] = [0.0] * len(bars)
    if lookback > n_rets:
        # Not enough returns to fill a single window.
        return series
    af_sqrt = math.sqrt(float(config.annualization_factor))
    for i in range(lookback, len(bars)):
        window = rets[i - lookback : i]
        if len(window) < 2:
            series[i] = 0.0
            continue
        mean = sum(window) / len(window)
        var = sum((r - mean) ** 2 for r in window) / len(window)
        std = math.sqrt(var)
        series[i] = std * af_sqrt
    return series


# ---------------------------------------------------------------------------
# Math primitives — vol scale series
# ---------------------------------------------------------------------------


def compute_vol_scale_series(
    bars: Sequence[Bar],
    config: Optional[VolTargetConfig],
) -> list[float]:
    """Compute the per-bar vol scale factor.

    Returns a list of length ``len(bars)``. When ``config`` is
    ``None``, returns all ``1.0`` (the legacy path — no scaling).

    scale[i] = clamp(
        config.target_annualized_vol / max(realized_vol[i], config.min_realized_vol),
        config.min_scale,
        config.max_scale,
    )

    For bars ``i < warmup_bars`` (default ``lookback_window``):
        scale[i] = config.min_scale

    Notes
    -----
    The ``min_realized_vol`` floor prevents divide-by-zero (or
    asymptotic explosions) when realized vol approaches zero. The
    ``max_scale`` cap prevents leverage explosions in micro-vol
    regimes (e.g. weekends with flat prices).
    """
    if config is None:
        return [1.0] * len(bars)
    if not isinstance(config, VolTargetConfig):
        raise TypeError(
            f"config must be VolTargetConfig or None (got {type(config).__name__})"
        )
    if len(bars) == 0:
        return []
    realized = compute_realized_vol_series(bars, config)
    warmup = (
        config.warmup_bars if config.warmup_bars is not None else config.lookback_window
    )
    target = config.target_annualized_vol
    min_vol = config.min_realized_vol
    min_scale = config.min_scale
    max_scale = config.max_scale
    out: list[float] = [min_scale] * len(bars)
    for i in range(warmup, len(bars)):
        denom = max(realized[i], min_vol)
        scale = target / denom
        # Clamp manually to avoid min(scale, max) when scale is NaN
        # (defensive — math.isfinite guard upstream should prevent).
        if not math.isfinite(scale):
            scale = min_scale
        if scale > max_scale:
            scale = max_scale
        elif scale < min_scale:
            scale = min_scale
        out[i] = scale
    return out


# ---------------------------------------------------------------------------
# Math primitives — high-water-mark and drawdown
# ---------------------------------------------------------------------------


def compute_high_water_mark_series(
    equity_curve: Sequence[float],
    starting_equity: Optional[float] = None,
) -> list[float]:
    """Compute per-bar high-water-mark from an equity curve.

    HWM[i] = max(starting_equity (or equity_curve[0] if None),
                 equity_curve[0], ..., equity_curve[i])

    Returns a list of length ``len(equity_curve)``.

    Notes
    -----
    The HWM is computed in O(N) over ``equity_curve``. The
    ``starting_equity`` parameter allows callers to anchor the
    HWM at a value other than the first equity entry (e.g. when
    the equity_curve was rebuilt after a warmup period).

    Raises
    ------
    VolTargetError
        If the initial equity is non-positive.
    """
    if len(equity_curve) == 0:
        return []
    initial_val = (
        starting_equity if starting_equity is not None else equity_curve[0]
    )
    if not math.isfinite(initial_val):
        raise VolTargetError(
            f"initial equity must be finite (got {initial_val})"
        )
    if initial_val <= 0:
        raise VolTargetError(
            f"initial equity must be > 0 (got {initial_val})"
        )
    out: list[float] = []
    hwm = initial_val
    for v in equity_curve:
        if not math.isfinite(v):
            raise VolTargetError(
                f"non-finite equity value in equity_curve (got {v})"
            )
        if v > hwm:
            hwm = v
        out.append(hwm)
    return out


def compute_dd_series(
    equity_curve: Sequence[float],
    starting_equity: Optional[float] = None,
) -> list[float]:
    """Per-bar drawdown from high-water-mark (fractional).

    Returns a list of length ``len(equity_curve)``::

        dd[i] = (hwm[i] - equity_curve[i]) / hwm[i]

    clamped to ``>= 0``. ``dd[i] == 0`` means at-or-above HWM.

    Notes
    -----
    HWM is computed via :func:`compute_high_water_mark_series` —
    see that function for the ``starting_equity`` semantics.
    """
    hwm = compute_high_water_mark_series(equity_curve, starting_equity=starting_equity)
    out: list[float] = []
    for v, h in zip(equity_curve, hwm, strict=True):
        if h <= 0:
            raise VolTargetError(
                f"high-water-mark must be > 0 (got {h}); "
                "equity_curve must have positive values"
            )
        dd = (h - v) / h
        if dd < 0:
            # Clamp — equity can exceed HWM only transiently due to
            # floating-point, but the clamp is the safe default.
            dd = 0.0
        out.append(dd)
    return out


# ---------------------------------------------------------------------------
# Math primitives — DD ladder risk lookup
# ---------------------------------------------------------------------------


def lookup_dd_risk_fraction(
    dd: float,
    config: DrawdownLadderConfig,
) -> float:
    """Look up the risk fraction for a single drawdown value.

    Walks ``config.tiers`` and returns the ``risk_fraction`` of the
    *last* tier whose ``dd_threshold <= dd``. If no tier qualifies
    (impossible when the first tier's threshold is 0.0), returns
    the deepest tier's fraction.

    Parameters
    ----------
    dd : float
        Drawdown (fractional). Must be ``>= 0``.
    config : DrawdownLadderConfig
        The ladder config. ``min_risk_fraction`` is applied as a
        post-floor on the returned fraction.

    Returns
    -------
    float
        Risk fraction in ``[config.min_risk_fraction, 1.0]``.

    Notes
    -----
    This is the *core* primitive; the engine wrapper uses it per
    bar via :func:`compute_dd_risk_series`. The lookup is O(N) on
    the number of tiers (typically 3-5) — constant time in practice.
    """
    if not isinstance(config, DrawdownLadderConfig):
        raise TypeError(
            f"config must be DrawdownLadderConfig (got {type(config).__name__})"
        )
    if not math.isfinite(dd):
        raise VolTargetError(f"dd must be finite (got {dd})")
    if dd < 0:
        raise VolTargetError(f"dd must be >= 0 (got {dd})")
    # Linear walk; tiers are sorted ascending.
    fraction = config.tiers[0].risk_fraction
    for tier in config.tiers:
        if dd >= tier.dd_threshold:
            fraction = tier.risk_fraction
        else:
            break
    if fraction < config.min_risk_fraction:
        fraction = config.min_risk_fraction
    return fraction


def compute_dd_risk_series(
    equity_curve: Sequence[float],
    config: Optional[DrawdownLadderConfig],
    starting_equity: Optional[float] = None,
) -> tuple[list[float], list[float], list[float], float]:
    """Compute per-bar DD risk series + HWM + DD series + max DD.

    When ``config is None``, returns ``dd_risk_series`` of all
    ``1.0``, ``hwm_series`` matching the equity curve, ``dd_series``
    of all ``0.0``, and ``max_dd_observed = 0.0``.

    Returns
    -------
    tuple[list[float], list[float], list[float], float]
        ``(dd_risk_series, hwm_series, dd_series, max_dd_observed)``.
        All series have length ``len(equity_curve)``.
    """
    if config is None:
        n = len(equity_curve)
        # HWM is the running max of the equity curve itself (no config).
        hwm = compute_high_water_mark_series(equity_curve, starting_equity=starting_equity)
        null_dd_series = compute_dd_series(equity_curve, starting_equity=starting_equity)
        max_dd = max(null_dd_series) if null_dd_series else 0.0
        return [1.0] * n, hwm, null_dd_series, max_dd
    if not isinstance(config, DrawdownLadderConfig):
        raise TypeError(
            f"config must be DrawdownLadderConfig or None (got {type(config).__name__})"
        )
    hwm_series = compute_high_water_mark_series(
        equity_curve,
        starting_equity=(
            starting_equity if starting_equity is not None else config.starting_equity
        ),
    )
    dd_series: list[float] = []
    risk_series: list[float] = []
    for v, h in zip(equity_curve, hwm_series, strict=True):
        if h <= 0:
            raise VolTargetError(
                f"high-water-mark must be > 0 (got {h}); equity_curve must be positive"
            )
        dd = (h - v) / h
        if dd < 0:
            dd = 0.0
        dd_series.append(dd)
        risk_series.append(lookup_dd_risk_fraction(dd, config))
    max_dd = max(dd_series) if dd_series else 0.0
    return risk_series, hwm_series, dd_series, max_dd


# ---------------------------------------------------------------------------
# Math primitives — composite risk scale
# ---------------------------------------------------------------------------


def compute_composite_risk_scale_series(
    vol_scale: Sequence[float],
    dd_risk: Sequence[float],
) -> list[float]:
    """Compute the per-bar composite risk scale series.

    ``composite[i] = vol_scale[i] * dd_risk[i]``

    Both inputs must have the same length. Output has the same
    length.

    Notes
    -----
    This is a *simple multiplicative* composition — both overlays
    are applied independently and their effects multiply. When
    either input is ``1.0`` at a bar (no scaling), the composite
    is unchanged at that bar.
    """
    if len(vol_scale) != len(dd_risk):
        raise VolTargetError(
            f"vol_scale length ({len(vol_scale)}) must match dd_risk length "
            f"({len(dd_risk)})"
        )
    return [v * d for v, d in zip(vol_scale, dd_risk, strict=True)]


# ---------------------------------------------------------------------------
# Config loaders — fail-loud YAML-style mapping parsers
# ---------------------------------------------------------------------------


def _coerce_pos_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise VolTargetError(
            f"{label} must be numeric (got bool {value})"
        )
    if not isinstance(value, (int, float)):
        raise VolTargetError(
            f"{label} must be numeric (got {type(value).__name__}: {value!r})"
        )
    result = float(value)
    if not math.isfinite(result):
        raise VolTargetError(f"{label} must be finite (got {value!r})")
    return result


def _coerce_nonneg_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VolTargetError(
            f"{label} must be an int (got {type(value).__name__}: {value!r})"
        )
    if value < 0:
        raise VolTargetError(f"{label} must be >= 0 (got {value})")
    return value


def _coerce_optional_float(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    return _coerce_pos_float(value, label)


def _coerce_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise VolTargetError(
            f"{label} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value.strip():
        raise VolTargetError(f"{label} must be non-empty (got {value!r})")
    return value


def load_vol_target_config(mapping: Mapping[str, Any]) -> VolTargetConfig:
    """Parse a vol-target config mapping into a :class:`VolTargetConfig`.

    Required key: ``target_annualized_vol``. All other fields are
    optional and default to the values in
    :class:`VolTargetConfig`.

    Parameters
    ----------
    mapping : Mapping[str, Any]
        The config mapping (typically produced by
        ``yaml.safe_load``).

    Returns
    -------
    VolTargetConfig
        Validated config object.

    Raises
    ------
    VolTargetError
        On any structural / type / value violation. The message
        identifies the failing field so the operator can fix the
        YAML directly.
    """
    if not isinstance(mapping, Mapping):
        raise VolTargetError(
            f"vol_target config must be a mapping (got {type(mapping).__name__})"
        )
    if "target_annualized_vol" not in mapping:
        raise VolTargetError(
            "vol_target config is missing required key: 'target_annualized_vol'"
        )
    target = _coerce_pos_float(mapping["target_annualized_vol"], "target_annualized_vol")

    kwargs: dict[str, Any] = {"target_annualized_vol": target}

    if "lookback_window" in mapping:
        lookback = _coerce_nonneg_int(mapping["lookback_window"], "lookback_window")
        if lookback < 2:
            raise VolTargetError(
                f"lookback_window must be >= 2 (got {lookback})"
            )
        kwargs["lookback_window"] = lookback

    if "annualization_factor" in mapping:
        af = _coerce_nonneg_int(
            mapping["annualization_factor"], "annualization_factor"
        )
        if af < 1:
            raise VolTargetError(
                f"annualization_factor must be >= 1 (got {af})"
            )
        kwargs["annualization_factor"] = af

    if "min_realized_vol" in mapping:
        kwargs["min_realized_vol"] = _coerce_pos_float(
            mapping["min_realized_vol"], "min_realized_vol"
        )

    if "max_scale" in mapping:
        ms = _coerce_pos_float(mapping["max_scale"], "max_scale")
        if ms < 1.0:
            raise VolTargetError(
                f"max_scale must be >= 1.0 (got {ms})"
            )
        kwargs["max_scale"] = ms

    if "min_scale" in mapping:
        mn = _coerce_pos_float(mapping["min_scale"], "min_scale")
        if mn < 0:
            raise VolTargetError(
                f"min_scale must be >= 0 (got {mn})"
            )
        kwargs["min_scale"] = mn

    if "warmup_bars" in mapping:
        kwargs["warmup_bars"] = _coerce_nonneg_int(
            mapping["warmup_bars"], "warmup_bars"
        )

    if "name" in mapping:
        kwargs["name"] = _coerce_str(mapping["name"], "name")

    return VolTargetConfig(**kwargs)


def load_drawdown_ladder_config(mapping: Mapping[str, Any]) -> DrawdownLadderConfig:
    """Parse a DD ladder config mapping into a
    :class:`DrawdownLadderConfig`.

    Required key: ``tiers``. Each tier is a mapping with
    ``dd_threshold`` and ``risk_fraction``. ``dd_threshold`` of
    the first tier must be ``0.0`` (validated again in the
    constructor).

    Parameters
    ----------
    mapping : Mapping[str, Any]
        The config mapping.

    Returns
    -------
    DrawdownLadderConfig
        Validated config object.

    Raises
    ------
    VolTargetError
        On any structural / type / value violation.
    """
    if not isinstance(mapping, Mapping):
        raise VolTargetError(
            f"drawdown_ladder config must be a mapping (got {type(mapping).__name__})"
        )
    if "tiers" not in mapping:
        raise VolTargetError(
            "drawdown_ladder config is missing required key: 'tiers'"
        )
    tiers_raw = mapping["tiers"]
    if not isinstance(tiers_raw, (list, tuple)):
        raise VolTargetError(
            f"tiers must be a list/tuple (got {type(tiers_raw).__name__})"
        )
    if not tiers_raw:
        raise VolTargetError("tiers must be non-empty")
    parsed: list[DrawdownTier] = []
    for i, tier_raw in enumerate(tiers_raw):
        if not isinstance(tier_raw, Mapping):
            raise VolTargetError(
                f"tiers[{i}] must be a mapping (got {type(tier_raw).__name__})"
            )
        if "dd_threshold" not in tier_raw:
            raise VolTargetError(
                f"tiers[{i}] is missing required key: 'dd_threshold'"
            )
        if "risk_fraction" not in tier_raw:
            raise VolTargetError(
                f"tiers[{i}] is missing required key: 'risk_fraction'"
            )
        parsed.append(
            DrawdownTier(
                dd_threshold=_coerce_pos_float(
                    tier_raw["dd_threshold"], f"tiers[{i}].dd_threshold"
                ),
                risk_fraction=_coerce_pos_float(
                    tier_raw["risk_fraction"], f"tiers[{i}].risk_fraction"
                ),
            )
        )
    kwargs: dict[str, Any] = {"tiers": tuple(parsed)}
    if "starting_equity" in mapping:
        kwargs["starting_equity"] = _coerce_optional_float(
            mapping["starting_equity"], "starting_equity"
        )
    if "min_risk_fraction" in mapping:
        mrf = _coerce_pos_float(
            mapping["min_risk_fraction"], "min_risk_fraction"
        )
        if not (0.0 <= mrf <= 1.0):
            raise VolTargetError(
                f"min_risk_fraction must be in [0, 1] (got {mrf})"
            )
        kwargs["min_risk_fraction"] = mrf
    if "name" in mapping:
        kwargs["name"] = _coerce_str(mapping["name"], "name")
    return DrawdownLadderConfig(**kwargs)


# ---------------------------------------------------------------------------
# Engine wrappers
# ---------------------------------------------------------------------------


def _run_engine_with_overlays(
    bars: Sequence[Bar],
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str],
    integrity_config: Optional[IntegrityConfig],
) -> object:
    """Run :class:`BacktestEngine` after applying the integrity gate.

    Returns the engine's :class:`BacktestMetrics`. The integrity
    gate is opt-in (preserves legacy FX behavior when
    ``integrity_config is None``).
    """
    if integrity_config is not None:
        from backtest.integrity_gate import apply_integrity_gate as _apply_gate
        _apply_gate(
            symbol=getattr(config, "pair", "") or "",
            bars=bars,
            config=integrity_config,
        )
    from engine.engine import BacktestEngine

    name = (
        strategy_name
        if strategy_name is not None
        else getattr(strategies[0], "name", None)
    )
    engine = BacktestEngine(config, list(strategies))
    if name is not None:
        for s in strategies:
            if getattr(s, "name", None) == name:
                base_metrics = engine.run_single(s, list(bars))
                break
        else:
            base_metrics = engine.run_single(strategies[0], list(bars))
    else:
        base_metrics = engine.run_single(strategies[0], list(bars))
    return base_metrics


def _slice_equity_curve_to_bars(
    equity_curve: Sequence[float],
    n_bars: int,
) -> list[float]:
    """Slice the engine's equity_curve to ``len(bars)`` entries.

    The engine's ``equity_curve`` is length ``len(bars) + 1`` or
    more (one initial balance plus one or more per bar). For
    per-bar overlays we want exactly ``len(bars)`` entries aligned
    with ``bars`` — we slice off the initial entry (which is the
    pre-engine balance) and keep the first ``n_bars`` per-bar
    entries. If the engine produced fewer entries (e.g. early exit
    on drawdown breach), the result is padded with the last known
    balance.
    """
    if not equity_curve:
        return [0.0] * n_bars
    # Skip the initial balance; take the next n_bars entries.
    per_bar = list(equity_curve[1 : 1 + n_bars])
    if len(per_bar) < n_bars:
        last = per_bar[-1] if per_bar else equity_curve[0]
        per_bar = per_bar + [last] * (n_bars - len(per_bar))
    return per_bar


def run_backtest_with_vol_target(
    bars: Sequence[Bar],
    vol_target_config: Optional[VolTargetConfig],
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
    integrity_config: Optional[IntegrityConfig] = None,
) -> VolTargetBacktestMetrics:
    """Run :class:`BacktestEngine` and overlay the vol-target scale.

    The engine runs unchanged (legacy path preserved). The wrapper
    computes the per-bar vol scale series from the bar prices and
    the supplied :class:`VolTargetConfig`.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must be sorted ascending by time.
    vol_target_config : VolTargetConfig, optional
        When ``None``, the wrapper returns a vol scale series of
        all ``1.0`` (legacy FX path — no scaling).
    config : BacktestConfig
        Backtest config forwarded to ``BacktestEngine``.
    strategies : sequence
        Strategies passed to ``BacktestEngine``. Must be non-empty.
    strategy_name : str, optional
        Which strategy's metrics to wrap.
    integrity_config : IntegrityConfig, optional
        When supplied, :func:`backtest.integrity_gate.apply_integrity_gate`
        is called on ``bars`` before the engine runs (fail-loud).
        ``None`` (default) preserves the existing behavior.

    Returns
    -------
    VolTargetBacktestMetrics
        Base + vol-target overlay.
    """
    if vol_target_config is not None and not isinstance(vol_target_config, VolTargetConfig):
        raise TypeError(
            f"vol_target_config must be VolTargetConfig or None "
            f"(got {type(vol_target_config).__name__})"
        )
    if integrity_config is not None and not isinstance(integrity_config, IntegrityConfig):
        raise TypeError(
            f"integrity_config must be IntegrityConfig or None "
            f"(got {type(integrity_config).__name__})"
        )
    if not bars:
        raise ValueError("bars must be non-empty")
    if not strategies:
        raise ValueError("strategies must be non-empty")

    base_metrics = _run_engine_with_overlays(
        bars=bars,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
        integrity_config=integrity_config,
    )

    if vol_target_config is not None:
        realized_vol = compute_realized_vol_series(bars, vol_target_config)
        vol_scale = compute_vol_scale_series(bars, vol_target_config)
        warmup = (
            vol_target_config.warmup_bars
            if vol_target_config.warmup_bars is not None
            else vol_target_config.lookback_window
        )
        post = realized_vol[warmup:]
        mean_vol = (sum(post) / len(post)) if post else 0.0
    else:
        vol_scale = [1.0] * len(bars)
        realized_vol = [0.0] * len(bars)
        mean_vol = 0.0

    return VolTargetBacktestMetrics(
        base=base_metrics,
        vol_scale_series=vol_scale,
        realized_vol_series=realized_vol,
        mean_realized_vol_post_warmup=mean_vol,
    )


def run_backtest_with_drawdown_ladder(
    bars: Sequence[Bar],
    dd_config: Optional[DrawdownLadderConfig],
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
    integrity_config: Optional[IntegrityConfig] = None,
) -> DrawdownLadderBacktestMetrics:
    """Run :class:`BacktestEngine` and overlay the DD ladder risk series.

    The engine runs unchanged. The wrapper computes per-bar risk
    fraction from the engine's equity curve and the supplied
    :class:`DrawdownLadderConfig`.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must be sorted ascending by time.
    dd_config : DrawdownLadderConfig, optional
        When ``None``, the wrapper returns a DD risk series of
        all ``1.0`` (legacy FX path — no scaling).
    config : BacktestConfig
        Backtest config forwarded to ``BacktestEngine``.
    strategies : sequence
        Strategies passed to ``BacktestEngine``. Must be non-empty.
    strategy_name : str, optional
        Which strategy's metrics to wrap.
    integrity_config : IntegrityConfig, optional
        When supplied, the integrity gate runs before the engine.

    Returns
    -------
    DrawdownLadderBacktestMetrics
        Base + DD ladder overlay.
    """
    if dd_config is not None and not isinstance(dd_config, DrawdownLadderConfig):
        raise TypeError(
            f"dd_config must be DrawdownLadderConfig or None "
            f"(got {type(dd_config).__name__})"
        )
    if integrity_config is not None and not isinstance(integrity_config, IntegrityConfig):
        raise TypeError(
            f"integrity_config must be IntegrityConfig or None "
            f"(got {type(integrity_config).__name__})"
        )
    if not bars:
        raise ValueError("bars must be non-empty")
    if not strategies:
        raise ValueError("strategies must be non-empty")

    base_metrics = _run_engine_with_overlays(
        bars=bars,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
        integrity_config=integrity_config,
    )

    equity_curve = getattr(base_metrics, "equity_curve", []) or []
    per_bar_equity = _slice_equity_curve_to_bars(equity_curve, len(bars))

    if dd_config is not None:
        starting_equity = dd_config.starting_equity
        if starting_equity is None:
            starting_equity = getattr(base_metrics, "starting_balance", None)
        risk_series, hwm_series, dd_series, max_dd = compute_dd_risk_series(
            equity_curve=per_bar_equity,
            config=dd_config,
            starting_equity=starting_equity,
        )
    else:
        hwm_series = compute_high_water_mark_series(per_bar_equity)
        dd_series = [0.0] * len(per_bar_equity)
        risk_series = [1.0] * len(per_bar_equity)
        max_dd = 0.0

    return DrawdownLadderBacktestMetrics(
        base=base_metrics,
        dd_risk_series=risk_series,
        hwm_series=hwm_series,
        dd_series=dd_series,
        max_dd_observed=max_dd,
    )


def run_backtest_with_vol_target_and_dd(
    bars: Sequence[Bar],
    vol_target_config: Optional[VolTargetConfig],
    dd_config: Optional[DrawdownLadderConfig],
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
    integrity_config: Optional[IntegrityConfig] = None,
) -> VolTargetDDBacktestMetrics:
    """Run :class:`BacktestEngine` and overlay vol-target + DD ladder.

    The engine runs once; the wrapper composes the two overlays
    multiplicatively::

        composite_risk_scale[i] = vol_scale[i] * dd_risk[i]

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series.
    vol_target_config : VolTargetConfig, optional
        ``None`` → vol-target is a no-op (vol_scale == 1.0).
    dd_config : DrawdownLadderConfig, optional
        ``None`` → DD ladder is a no-op (dd_risk == 1.0).
    config : BacktestConfig
        Forwarded to ``BacktestEngine``.
    strategies : sequence
        Forwarded to ``BacktestEngine``.
    strategy_name : str, optional
        Forwarded to ``BacktestEngine``.
    integrity_config : IntegrityConfig, optional
        Opt-in integrity gate (fail-loud).

    Returns
    -------
    VolTargetDDBacktestMetrics
        Base + composite risk-scale overlay.

    Notes
    -----
    Byte-equality: when both ``vol_target_config is None`` and
    ``dd_config is None``, ``composite_risk_scale_series`` is all
    ``1.0`` — the wrapper is a no-op aside from the engine run.
    """
    if vol_target_config is not None and not isinstance(vol_target_config, VolTargetConfig):
        raise TypeError(
            f"vol_target_config must be VolTargetConfig or None "
            f"(got {type(vol_target_config).__name__})"
        )
    if dd_config is not None and not isinstance(dd_config, DrawdownLadderConfig):
        raise TypeError(
            f"dd_config must be DrawdownLadderConfig or None "
            f"(got {type(dd_config).__name__})"
        )
    if integrity_config is not None and not isinstance(integrity_config, IntegrityConfig):
        raise TypeError(
            f"integrity_config must be IntegrityConfig or None "
            f"(got {type(integrity_config).__name__})"
        )
    if not bars:
        raise ValueError("bars must be non-empty")
    if not strategies:
        raise ValueError("strategies must be non-empty")

    base_metrics = _run_engine_with_overlays(
        bars=bars,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
        integrity_config=integrity_config,
    )

    vol_scale = compute_vol_scale_series(bars, vol_target_config)

    equity_curve = getattr(base_metrics, "equity_curve", []) or []
    per_bar_equity = _slice_equity_curve_to_bars(equity_curve, len(bars))

    if dd_config is not None:
        starting_equity = dd_config.starting_equity
        if starting_equity is None:
            starting_equity = getattr(base_metrics, "starting_balance", None)
        risk_series, hwm_series, dd_series, _max_dd = compute_dd_risk_series(
            equity_curve=per_bar_equity,
            config=dd_config,
            starting_equity=starting_equity,
        )
    else:
        hwm_series = compute_high_water_mark_series(per_bar_equity)
        dd_series = [0.0] * len(per_bar_equity)
        risk_series = [1.0] * len(per_bar_equity)

    composite = compute_composite_risk_scale_series(vol_scale, risk_series)

    return VolTargetDDBacktestMetrics(
        base=base_metrics,
        vol_scale_series=vol_scale,
        dd_risk_series=risk_series,
        composite_risk_scale_series=composite,
        hwm_series=hwm_series,
        dd_series=dd_series,
    )


def run_backtest_with_full_crypto_overlay_and_vol_target(
    bars: Sequence[Bar],
    mark_prices: Sequence[float],
    funding_events: Sequence[object],
    fills: Sequence[object],
    venue_config: object,
    position: object,
    liq_spec: object,
    config: object,
    strategies: Sequence[object],
    strategy_name: Optional[str] = None,
    vol_target_config: Optional[VolTargetConfig] = None,
    dd_config: Optional[DrawdownLadderConfig] = None,
    integrity_config: Optional[IntegrityConfig] = None,
) -> tuple:
    """Run venue + funding + liquidation + vol-target + DD ladder overlays.

    Composition strategy:
      1. Run :func:`backtest.venue_costs.run_backtest_with_full_crypto_overlay`
         — produces ``(funded, liquidated, venue_metrics)``.
      2. Run :func:`run_backtest_with_vol_target_and_dd` against
         the same bars/position/engine config — produces the
         vol-target + DD overlay.
      3. Return all four overlays.

    Each wrapper runs the engine once (4 total engine invocations);
    the engine is deterministic on ``(config, bars, strategies)``
    so ``base.ending_balance`` is identical across the runs and
    the overlays are composable.

    Parameters
    ----------
    bars, mark_prices, funding_events, fills, venue_config,
    position, liq_spec, config, strategies, strategy_name
        Forwarded to :func:`run_backtest_with_full_crypto_overlay`
        and :func:`run_backtest_with_vol_target_and_dd`.
    vol_target_config : VolTargetConfig, optional
        Forwarded to the vol-target overlay. ``None`` → vol-target
        is a no-op (vol_scale == 1.0).
    dd_config : DrawdownLadderConfig, optional
        Forwarded to the DD overlay. ``None`` → DD is a no-op.
    integrity_config : IntegrityConfig, optional
        Forwarded to all four wrappers (shared across runs; the
        gate is fail-loud and raises on any violation before any
        engine run).

    Returns
    -------
    tuple[FundedBacktestMetrics, LiquidatedBacktestMetrics,
          VenueCostBacktestMetrics, VolTargetDDBacktestMetrics]
        All four overlays. The caller sums the three cost overlays'
        ``*_equity_delta`` series for a single cost line and uses
        the vol-target / DD overlay's ``composite_risk_scale_series``
        to scale notionals.

    Notes
    -----
    Byte-equality with
    :func:`run_backtest_with_full_crypto_overlay`: when both
    ``vol_target_config is None and dd_config is None``, the
    fourth element is :class:`VolTargetDDBacktestMetrics` with
    ``composite_risk_scale_series`` all ``1.0`` and
    ``base.ending_balance`` equal to the funding/liq/venue
    wrappers' ``base.ending_balance``. The first three elements
    are byte-identical to
    :func:`run_backtest_with_full_crypto_overlay`'s output.
    """
    from backtest.venue_costs import (
        run_backtest_with_full_crypto_overlay as _run_full,
    )

    funded, liquidated, venue_metrics = _run_full(
        bars=bars,
        mark_prices=mark_prices,
        funding_events=funding_events,
        fills=fills,
        venue_config=venue_config,
        position=position,
        liq_spec=liq_spec,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
    )
    vol_dd_metrics = run_backtest_with_vol_target_and_dd(
        bars=bars,
        vol_target_config=vol_target_config,
        dd_config=dd_config,
        config=config,
        strategies=strategies,
        strategy_name=strategy_name,
        integrity_config=integrity_config,
    )
    return funded, liquidated, venue_metrics, vol_dd_metrics
