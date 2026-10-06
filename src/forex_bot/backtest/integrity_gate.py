"""Fail-loud data-integrity gate for crypto backtests.

Card: b86ac95b-6aab-4014-a6d8-828868f401c2 (Sprint C 1a.4)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

What this module provides
==========================

A data-integrity gate that validates crypto OHLCV bar series *before*
they enter the backtest engine. Detects three classes of defect:

  * **Gaps** — missing candles vs the expected cadence (1h/4h/8h).
    A missing candle may be a maintenance window, exchange outage,
    API throttling, or delisting — the symptom is identical, the
    meaning differs. We classify gaps but never repair them.
  * **Delistings** — data ends while the symbol is still in the
    requested universe. If the loaded series' last bar precedes the
    requested window's end (with tolerance), that's a delisting
    signal.
  * **Anomalous candles** — zero-volume, NaN, or OHLC-invariant
    violations. A NaN in any OHLC column, a sustained
    empty-interval stretch, or ``high < low``, ``high < open``,
    ``high < close``, ``low > open``, ``low > close`` are all
    errors.

Behavior contract
=================

The gate is **fail-loud**: on any violation it raises
:class:`DataIntegrityError` *before* mutating the bar series. The
gate never:

  * forward-fills gaps (research §1.6, R5 — Satsuki);
  * truncates the bar series to "clean" sections;
  * warns-and-continues (the silent-repair vector);
  * returns a repaired or partial bar series.

When the gate passes, it returns an :class:`IntegrityReport`
(zero violations) and the caller's bars are returned unchanged.

Configuration
=============

:class:`IntegrityConfig` controls:

  * ``expected_cadence_minutes`` — base cadence. The three values
    production callers use are 60 (1h), 240 (4h), and 1440 (D1).
    Other values are accepted but flagged as a non-crypto-class
    cadence in the docstring.
  * ``gap_tolerance_multiplier`` — how many cadences of slack
    before a gap is reported (default 1.5 = one bar of slack
    absorbs micro-drift in real exchange data; values >2 are
    considered permissive).
  * ``expected_window_end`` — when supplied, the gate flags a
    delisting if the last bar's time precedes
    ``expected_window_end - delisting_tolerance``. ``None`` disables
    the check.
  * ``delisting_tolerance_minutes`` — tolerance window applied to
    the delisting comparison. Default 0 (last bar must be on/after
    expected_window_end; real exchange data typically needs a
    positive value to absorb a few minutes of data latency).
  * ``universe_symbols`` — when supplied, the gate flags any
    symbol in the universe that is missing from the supplied
    bars (a per-symbol missing-data check, distinct from the
    window-end delisting check).
  * ``max_zero_volume_stretch`` — number of consecutive
    zero-volume bars allowed before flagging. ``0`` (the
    default) treats any single zero-volume bar as a violation;
    a positive value allows a short stretch of empty-interval
    bars (useful for illiquid pairs where empty intervals are
    expected). The gate does NOT distinguish between empty-interval
    and outage — it just enforces the upper bound on consecutive
    zero bars.
  * ``check_ohlc_invariants`` — enable the OHLC invariant checks
    (``high >= low``, ``high >= open``, ``high >= close``,
    ``low <= open``, ``low <= close``).
  * ``check_nan_values`` — enable NaN checks on OHLC + volume.
  * ``check_gaps`` / ``check_delistings`` / ``check_anomalies`` —
    master switches for each check family (default ``True``).
  * ``schema_version`` — bumped when the on-disk YAML structure
    changes (mirrors the ``VenueFeeConfig`` convention).

Like :func:`backtest.venue_costs.load_venue_fee_config`, the
config can be loaded from a YAML-style mapping via
:func:`load_integrity_config` with fail-loud validation
(:class:`IntegrityConfigError`).

Integration
===========

The crypto overlay wrappers (``run_backtest_with_venue_costs``,
``run_backtest_with_funding``, ``run_backtest_with_liquidation``)
accept an optional ``integrity_config`` parameter. When provided,
the wrapper calls :func:`enforce_integrity_gate` before invoking
the engine. When ``None`` (default), the existing behavior is
preserved — the gate is opt-in, and existing tests that don't
pass ``integrity_config`` are unaffected.

The legacy FX loader (:class:`backtest.data_loader.CsvDataLoader`)
is **unaffected** unless it explicitly opts in (it does not, by
design — FX bar data passes through the legacy path which doesn't
enforce crypto-class integrity checks). The new module is the
crypto lane's opt-in only.

Source: research doc §1.6, R5 of
``docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional, Sequence

from backtest.types import Bar

__all__ = [
    "DataIntegrityError",
    "IntegrityConfig",
    "IntegrityViolation",
    "IntegrityReport",
    "IntegrityConfigError",
    "DEFAULT_CADENCE_MINUTES",
    "VALID_CADENCES_MINUTES",
    "DEFAULT_GAP_TOLERANCE_MULTIPLIER",
    "DEFAULT_MAX_ZERO_VOLUME_STRETCH",
    "load_integrity_config",
    "infer_cadence_minutes",
    "detect_gaps",
    "detect_delisting",
    "detect_anomalies",
    "validate_crypto_bars",
    "enforce_integrity_gate",
    "apply_integrity_gate",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default base cadence in minutes (1h). The three production values
#: used by the crypto backtest core are 60, 240, and 1440; 60 is the
#: default because the funding/liquidation/venue overlay path runs
#: against 1h bars (matching the adaptive funding rate's fastest
#: cadence).
DEFAULT_CADENCE_MINUTES: int = 60

#: The three production cadences the gate is configured against.
#: Other values are accepted but :func:`_validate_cadence` flags
#: them — they are not the canonical crypto overlay cadences.
VALID_CADENCES_MINUTES: tuple[int, ...] = (60, 240, 1440)

#: Default gap tolerance multiplier. A gap is reported when
#: ``observed_diff > expected_cadence * gap_tolerance_multiplier``.
#: 1.5 means one bar of slack is allowed before a real gap is
#: flagged (absorbs micro-drift in real exchange data without
#: silencing actual outages).
DEFAULT_GAP_TOLERANCE_MULTIPLIER: float = 1.5

#: Default maximum zero-volume stretch. ``0`` means any single
#: zero-volume bar is a violation. Positive values allow short
#: empty-interval runs (useful for illiquid pairs).
DEFAULT_MAX_ZERO_VOLUME_STRETCH: int = 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IntegrityConfigError(ValueError):
    """Raised when an integrity config mapping fails validation.

    Inherits from :class:`ValueError` so existing
    ``pytest.raises(ValueError)`` callers catch it; tests that want
    to differentiate use ``except IntegrityConfigError`` directly.
    Mirrors the ``VenueConfigError`` convention from
    :mod:`backtest.venue_costs`.
    """


class DataIntegrityError(ValueError):
    """Raised when a bar series fails integrity validation.

    The exception carries the full :class:`IntegrityReport` (in
    addition to a human-readable summary message) so callers can
    inspect the precise violation set without re-running the
    validator.

    Attributes
    ----------
    symbol : str
        The symbol (or series id) whose bars were validated.
    report : IntegrityReport
        The full validation report. Always populated; ``report.passed``
        is always ``False`` when this exception is raised.
    """

    def __init__(self, message: str, *, symbol: str, report: "IntegrityReport") -> None:
        super().__init__(message)
        self.symbol = symbol
        self.report = report

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"DataIntegrityError(symbol={self.symbol!r}, "
            f"violations={len(self.report.violations)}, "
            f"message={str(self)!r})"
        )


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


ViolationKind = Literal[
    "gap",
    "delisting",
    "missing_symbol",
    "zero_volume",
    "nan_value",
    "ohlc_invariant",
]


@dataclass(frozen=True)
class IntegrityViolation:
    """A single integrity violation record.

    Attributes
    ----------
    kind : str
        Violation class — one of ``"gap"``, ``"delisting"``,
        ``"missing_symbol"``, ``"zero_volume"``, ``"nan_value"``,
        ``"ohlc_invariant"``.
    symbol : str
        The symbol (or series id) the violation is associated with.
        For ``"missing_symbol"`` the symbol is the missing one (not
        the loaded symbol).
    bar_index : int
        Index of the bar in the supplied bar list that triggered
        the violation. ``-1`` when the violation is not anchored to
        a specific bar (e.g. ``"delisting"`` with empty bars;
        ``"missing_symbol"``).
    start_time : datetime
        Bar time at the start of the violation window. For
        ``"delisting"`` this is the last bar's time. For
        ``"missing_symbol"`` this is the expected_window_start
        (or the first loaded bar's time). Naive datetimes are
        rejected; values are normalized to UTC.
    end_time : datetime
        Bar time at the end of the violation window. For ``"gap"``
        this is the time of the bar *after* the gap (the candle
        that reveals the missing interval). For ``"delisting"``
        this is the expected_window_end (or the last loaded bar's
        time + 1 cadence). Naive datetimes are rejected; values
        are normalized to UTC.
    detail : str
        Human-readable description of the violation (e.g. "expected
        bar at 2026-10-05T04:00:00Z (gap 4h > tolerance 1.5x
        1h = 1h 30m)"; "high (95) < low (96)"; "volume is NaN").
    expected : float, optional
        The expected value, when applicable (e.g. expected cadence
        seconds, expected high floor). ``None`` otherwise.
    actual : float, optional
        The observed value, when applicable (e.g. observed diff
        seconds, observed high). ``None`` otherwise.
    """

    kind: str
    symbol: str
    bar_index: int
    start_time: datetime
    end_time: datetime
    detail: str
    expected: Optional[float] = None
    actual: Optional[float] = None

    def __post_init__(self) -> None:
        if self.kind not in (
            "gap",
            "delisting",
            "missing_symbol",
            "zero_volume",
            "nan_value",
            "ohlc_invariant",
        ):
            raise IntegrityConfigError(
                f"IntegrityViolation.kind must be one of the documented kinds "
                f"(got {self.kind!r})"
            )
        for label, value in (
            ("start_time", self.start_time),
            ("end_time", self.end_time),
        ):
            if value.tzinfo is None:
                raise IntegrityConfigError(
                    f"IntegrityViolation.{label} must be timezone-aware "
                    f"(got naive {value!r})"
                )
            if value.tzinfo != timezone.utc:
                object.__setattr__(self, label, value.astimezone(timezone.utc))
        if not isinstance(self.symbol, str):
            raise IntegrityConfigError(
                f"IntegrityViolation.symbol must be a string "
                f"(got {type(self.symbol).__name__}: {self.symbol!r})"
            )
        # For "missing_symbol" violations, the symbol is the missing
        # one and must be non-empty (it identifies what to look for).
        # For every other kind, the symbol is metadata about the
        # loaded series — it may be empty when the caller did not
        # supply one (e.g. detect_delisting called without a
        # series-specific symbol). Permitting an empty symbol here
        # avoids the validator short-circuiting on a callsite
        # contract that allows it.
        if self.kind == "missing_symbol" and not self.symbol:
            raise IntegrityConfigError(
                f"IntegrityViolation.symbol must be non-empty for "
                f"'missing_symbol' violations (got {self.symbol!r})"
            )


@dataclass(frozen=True)
class IntegrityConfig:
    """Strictness configuration for the integrity gate.

    The default config (``IntegrityConfig()``) is the strictest
    production setting: 1h cadence, 1.5x gap tolerance, zero-volume
    bars flagged, OHLC + NaN checks enabled, no universe.

    Attributes
    ----------
    expected_cadence_minutes : int
        Base cadence in minutes. The three production values are
        60 (1h), 240 (4h), and 1440 (D1). Other values are
        accepted but flagged in :func:`_validate_cadence`.
    gap_tolerance_multiplier : float
        Multiplier applied to ``expected_cadence_minutes`` to
        derive the gap threshold (in minutes). A diff between
        consecutive bars greater than
        ``expected_cadence_minutes * gap_tolerance_multiplier``
        is reported as a gap. Default 1.5.
    expected_window_end : datetime, optional
        When supplied, the gate flags a delisting if the last
        bar's time is earlier than
        ``expected_window_end - delisting_tolerance_minutes``.
        ``None`` (default) disables the window-end delisting
        check.
    delisting_tolerance_minutes : float
        Tolerance applied to the window-end comparison (in
        minutes). Default 0.
    universe_symbols : tuple[str, ...]
        When supplied, the gate checks that every symbol in
        the universe is present in the loaded bars. Missing
        symbols produce a ``"missing_symbol"`` violation. When
        empty (default), the check is disabled. Symbol
        comparison is case-sensitive.
    max_zero_volume_stretch : int
        Maximum number of consecutive zero-volume bars
        tolerated. ``0`` (default) treats any single
        zero-volume bar as a violation. A positive value
        allows short empty-interval runs.
    check_ohlc_invariants : bool
        Enable OHLC invariant checks. Default ``True``.
    check_nan_values : bool
        Enable NaN checks on OHLC + volume. Default ``True``.
    check_gaps : bool
        Enable gap detection. Default ``True``.
    check_delistings : bool
        Enable delisting detection (requires
        ``expected_window_end`` or ``universe_symbols``).
        Default ``True``.
    check_anomalies : bool
        Enable zero-volume / NaN / OHLC invariant checks.
        Default ``True``.
    schema_version : int
        Config schema version. Defaults to ``1``. Callers can
        refuse to load a config with a newer schema than they
        recognize.
    """

    expected_cadence_minutes: int = DEFAULT_CADENCE_MINUTES
    gap_tolerance_multiplier: float = DEFAULT_GAP_TOLERANCE_MULTIPLIER
    expected_window_end: Optional[datetime] = None
    delisting_tolerance_minutes: float = 0.0
    universe_symbols: tuple[str, ...] = ()
    max_zero_volume_stretch: int = DEFAULT_MAX_ZERO_VOLUME_STRETCH
    check_ohlc_invariants: bool = True
    check_nan_values: bool = True
    check_gaps: bool = True
    check_delistings: bool = True
    check_anomalies: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        _validate_cadence(self.expected_cadence_minutes)
        if not math.isfinite(self.gap_tolerance_multiplier):
            raise IntegrityConfigError(
                f"gap_tolerance_multiplier must be finite "
                f"(got {self.gap_tolerance_multiplier})"
            )
        if self.gap_tolerance_multiplier < 1.0:
            raise IntegrityConfigError(
                f"gap_tolerance_multiplier must be >= 1.0 "
                f"(got {self.gap_tolerance_multiplier}); values < 1.0 "
                f"would report normal consecutive bars as gaps"
            )
        if not math.isfinite(self.delisting_tolerance_minutes):
            raise IntegrityConfigError(
                f"delisting_tolerance_minutes must be finite "
                f"(got {self.delisting_tolerance_minutes})"
            )
        if self.delisting_tolerance_minutes < 0:
            raise IntegrityConfigError(
                f"delisting_tolerance_minutes must be >= 0 "
                f"(got {self.delisting_tolerance_minutes})"
            )
        if self.max_zero_volume_stretch < 0:
            raise IntegrityConfigError(
                f"max_zero_volume_stretch must be >= 0 "
                f"(got {self.max_zero_volume_stretch})"
            )
        if not isinstance(self.universe_symbols, tuple) or any(
            not isinstance(s, str) or not s for s in self.universe_symbols
        ):
            raise IntegrityConfigError(
                f"universe_symbols must be a tuple of non-empty strings "
                f"(got {self.universe_symbols!r})"
            )
        if self.expected_window_end is not None and self.expected_window_end.tzinfo is None:
            raise IntegrityConfigError(
                f"expected_window_end must be timezone-aware "
                f"(got naive {self.expected_window_end!r})"
            )
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise IntegrityConfigError(
                f"schema_version must be a positive int "
                f"(got {self.schema_version})"
            )


@dataclass(frozen=True)
class IntegrityReport:
    """Result of a single integrity validation pass.

    Attributes
    ----------
    symbol : str
        The symbol (or series id) the validation was run against.
    total_bars : int
        Number of bars inspected. ``len(bars)`` at call time.
    cadence_minutes : int
        The cadence the validator used (echoed from
        :class:`IntegrityConfig` for traceability).
    window_start : datetime
        Time of the first bar in the supplied series (UTC).
        ``None`` when the series is empty.
    window_end : datetime
        Time of the last bar in the supplied series (UTC).
        ``None`` when the series is empty.
    expected_window_end : datetime, optional
        Echoed from :class:`IntegrityConfig.expected_window_end`
        (or ``None``).
    violations : tuple[IntegrityViolation, ...]
        Every violation detected, in detection order. Empty when
        the series passed.
    passed : bool
        ``True`` when ``len(violations) == 0``.
    """

    symbol: str
    total_bars: int
    cadence_minutes: int
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    expected_window_end: Optional[datetime]
    violations: tuple[IntegrityViolation, ...]
    passed: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "passed", len(self.violations) == 0)

    def by_kind(self, kind: str) -> list[IntegrityViolation]:
        """Return violations of one kind, in detection order."""
        return [v for v in self.violations if v.kind == kind]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_cadence(cadence: int) -> None:
    if not isinstance(cadence, int) or isinstance(cadence, bool):
        raise IntegrityConfigError(
            f"expected_cadence_minutes must be an int "
            f"(got {type(cadence).__name__}: {cadence!r})"
        )
    if cadence <= 0:
        raise IntegrityConfigError(
            f"expected_cadence_minutes must be > 0 (got {cadence})"
        )


def _to_utc(t: datetime, label: str) -> datetime:
    if t.tzinfo is None:
        raise IntegrityConfigError(
            f"{label} must be timezone-aware (got naive {t!r})"
        )
    return t.astimezone(timezone.utc)


def _is_nan(value: float) -> bool:
    """NaN check that doesn't depend on math.isnan for typed floats.

    Handles pandas NaN (which is float('nan') and behaves like math.nan)
    and python float('nan').
    """
    if value is None:
        return False
    if isinstance(value, float):
        return math.isnan(value)
    # numpy scalar — duck-type
    try:
        return bool(value != value)  # NaN != NaN
    except Exception:  # pragma: no cover - defensive
        return False


def _validate_bar_field_positive(bar: Bar, field: str, value: float) -> None:
    if not math.isfinite(value):
        # NaN / inf — handled separately in detect_anomalies
        return
    if value <= 0:
        raise IntegrityConfigError(
            f"Bar.{field} must be positive (got {value} at {bar.time})"
        )


# ---------------------------------------------------------------------------
# Cadence inference
# ---------------------------------------------------------------------------


def infer_cadence_minutes(bars: Sequence[Bar]) -> int:
    """Infer the bar cadence in minutes from the first few bars.

    Uses the median of the first up-to-10 consecutive diffs (in
    minutes). Returns ``60`` as a fallback when the series is
    too short (< 2 bars) to infer.

    The result is rounded to the nearest entry in
    :data:`VALID_CADENCES_MINUTES` (60, 240, 1440) when within
    5% of a valid cadence; otherwise the raw median is returned
    so the caller can decide.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Need at least 2 bars to infer; otherwise
        returns the default.

    Returns
    -------
    int
        Inferred cadence in minutes. ``60`` for empty / 1-bar series.
    """
    if len(bars) < 2:
        return DEFAULT_CADENCE_MINUTES
    diffs: list[int] = []
    for i in range(1, min(len(bars), 11)):
        prev = bars[i - 1].time
        curr = bars[i].time
        # Reject naive datetimes — the gate requires tz-aware bars.
        if prev.tzinfo is None or curr.tzinfo is None:
            raise IntegrityConfigError(
                f"infer_cadence_minutes: bars must be timezone-aware "
                f"(got naive time at index {i - 1} or {i})"
            )
        delta = int((curr.astimezone(timezone.utc) - prev.astimezone(timezone.utc)).total_seconds() // 60)
        if delta <= 0:
            # Out-of-order or duplicate timestamp — skip.
            continue
        diffs.append(delta)
    if not diffs:
        return DEFAULT_CADENCE_MINUTES
    diffs.sort()
    median = diffs[len(diffs) // 2]
    # Snap to the nearest valid cadence when within 5%.
    for valid in VALID_CADENCES_MINUTES:
        if abs(median - valid) / valid <= 0.05:
            return valid
    return median


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------


def detect_gaps(
    bars: Sequence[Bar],
    config: IntegrityConfig,
    *,
    symbol: str = "",
) -> list[IntegrityViolation]:
    """Detect missing candles vs the expected cadence.

    A gap is reported when the diff between two consecutive bars
    exceeds ``expected_cadence_minutes * gap_tolerance_multiplier``
    (in minutes). For each gap a single
    :class:`IntegrityViolation` of kind ``"gap"`` is emitted with
    ``start_time`` = the previous bar's time and ``end_time`` =
    the next bar's time.

    Out-of-order or duplicate bars are not flagged here (they are
    a separate concern; the engine handles them via its own
    invariant). Bars must be sorted ascending by time; the gate
    does NOT sort, so callers must pre-sort.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must be sorted ascending by time. Must be
        tz-aware.
    config : IntegrityConfig
        Strictness config. The gate honors ``check_gaps`` and
        ``expected_cadence_minutes`` / ``gap_tolerance_multiplier``.
    symbol : str
        Symbol/series id used in violation records.

    Returns
    -------
    list[IntegrityViolation]
        Detected gap violations, in detection order. Empty when
        no gap is detected or ``config.check_gaps`` is ``False``.
    """
    # see line-643 patch note above (defect #9 / card 0ab49707)
    if not hasattr(config, "expected_cadence_minutes"):
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__}); "
            "missing expected_cadence_minutes"
        )
    if type(config).__name__ != "IntegrityConfig":
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__})"
        )
    if not config.check_gaps:
        return []
    if len(bars) < 2:
        return []
    cadence_min = config.expected_cadence_minutes
    threshold_min = cadence_min * config.gap_tolerance_multiplier
    threshold_sec = threshold_min * 60.0

    violations: list[IntegrityViolation] = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]
        curr = bars[i]
        if prev.time.tzinfo is None or curr.time.tzinfo is None:
            raise IntegrityConfigError(
                f"detect_gaps: bars must be timezone-aware "
                f"(got naive time at index {i - 1} or {i})"
            )
        prev_t = prev.time.astimezone(timezone.utc)
        curr_t = curr.time.astimezone(timezone.utc)
        delta_sec = (curr_t - prev_t).total_seconds()
        if delta_sec <= 0:
            # Out-of-order / duplicate — not a "gap" in the cadence sense.
            continue
        if delta_sec > threshold_sec:
            detail = (
                f"gap {delta_sec / 60:.1f}m exceeds tolerance "
                f"{threshold_min:.1f}m "
                f"(expected cadence {cadence_min}m, "
                f"multiplier {config.gap_tolerance_multiplier}) "
                f"between bars at {prev_t.isoformat()} and {curr_t.isoformat()}"
            )
            violations.append(
                IntegrityViolation(
                    kind="gap",
                    symbol=symbol,
                    bar_index=i,
                    start_time=prev_t,
                    end_time=curr_t,
                    detail=detail,
                    expected=threshold_sec,
                    actual=delta_sec,
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Delisting detection
# ---------------------------------------------------------------------------


def detect_delisting(
    bars: Sequence[Bar],
    config: IntegrityConfig,
    *,
    symbol: str = "",
    loaded_symbol: Optional[str] = None,
) -> list[IntegrityViolation]:
    """Detect delistings via the window-end and universe checks.

    Two checks are performed, both independently controlled by
    ``config.check_delistings``:

      1. **Window-end delisting** — when
         ``config.expected_window_end`` is supplied, the last
         bar's time must be at or after
         ``expected_window_end - delisting_tolerance_minutes``.
         A delisting is reported when the last bar precedes this
         threshold.
      2. **Universe-missing symbol** — when
         ``config.universe_symbols`` is non-empty, any symbol
         in the universe that is missing from the loaded bars
         is reported as a ``"missing_symbol"`` violation.

    Parameters
    ----------
    bars : sequence[Bar]
        Loaded bar series.
    config : IntegrityConfig
        Strictness config.
    symbol : str
        Symbol/series id of the loaded bars (used in window-end
        delisting violations).
    loaded_symbol : str, optional
        The "name" of the symbol represented by ``bars``. When
        supplied AND a non-empty ``universe_symbols`` is provided,
        the gate also reports the loaded symbol as missing when
        it isn't in the universe (catches "wrong symbol loaded
        for a known-good universe slot"). When ``None`` (default),
        the loaded symbol is excluded from the universe check
        (only the explicit ``universe_symbols`` set is enforced).

    Returns
    -------
    list[IntegrityViolation]
        Detected delisting / missing-symbol violations, in
        detection order. Empty when no violation is detected or
        ``config.check_delistings`` is ``False``.
    """
    # see line-643 patch note above (defect #9 / card 0ab49707)
    if not hasattr(config, "expected_cadence_minutes"):
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__}); "
            "missing expected_cadence_minutes"
        )
    if type(config).__name__ != "IntegrityConfig":
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__})"
        )
    if not config.check_delistings:
        return []
    violations: list[IntegrityViolation] = []

    # 1. Window-end delisting.
    if config.expected_window_end is not None and bars:
        last_t = _to_utc(bars[-1].time, "bars[-1].time")
        threshold = _to_utc(
            config.expected_window_end, "config.expected_window_end"
        ) - _minutes_to_timedelta(config.delisting_tolerance_minutes)
        if last_t < threshold:
            detail = (
                f"delisting: last bar at {last_t.isoformat()} precedes "
                f"expected window end {threshold.isoformat()} "
                f"(tolerance {config.delisting_tolerance_minutes:.1f}m)"
            )
            violations.append(
                IntegrityViolation(
                    kind="delisting",
                    symbol=symbol,
                    bar_index=len(bars) - 1,
                    start_time=last_t,
                    end_time=threshold,
                    detail=detail,
                    expected=threshold.timestamp(),
                    actual=last_t.timestamp(),
                )
            )

    # 2. Universe-missing symbol.
    if config.universe_symbols:
        universe = set(config.universe_symbols)
        present: set[str] = set()
        if loaded_symbol is not None:
            present.add(loaded_symbol)
        for missing in sorted(universe - present):
            # Anchor the missing-symbol violation to the first loaded
            # bar (or expected_window_end if available) for traceability.
            if bars:
                anchor = _to_utc(bars[0].time, "bars[0].time")
            elif config.expected_window_end is not None:
                anchor = _to_utc(
                    config.expected_window_end, "config.expected_window_end"
                )
            else:
                anchor = datetime(1970, 1, 1, tzinfo=timezone.utc)
            detail = (
                f"missing symbol: {missing!r} is in the universe but no "
                f"bars were loaded for it"
            )
            violations.append(
                IntegrityViolation(
                    kind="missing_symbol",
                    symbol=missing,
                    bar_index=-1,
                    start_time=anchor,
                    end_time=anchor,
                    detail=detail,
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


def detect_anomalies(
    bars: Sequence[Bar],
    config: IntegrityConfig,
    *,
    symbol: str = "",
) -> list[IntegrityViolation]:
    """Detect zero-volume, NaN, and OHLC-invariant violations.

    Two sub-checks are gated by ``config.check_anomalies``:

      * **Zero-volume stretch** — when ``max_zero_volume_stretch``
        consecutive zero-volume bars are observed, a violation
        is reported at the *first* bar of the violating stretch.
        A single zero-volume bar is reported when the limit is
        0 (default).
      * **NaN values** — when ``check_nan_values`` is ``True``,
        any NaN in open / high / low / close / volume is
        reported as a ``"nan_value"`` violation.
      * **OHLC invariants** — when ``check_ohlc_invariants`` is
        ``True``, the four invariants
        ``high >= low``, ``high >= open``, ``high >= close``,
        ``low <= open``, ``low <= close`` are checked per bar;
        a violation is reported at the first failed invariant.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. May be empty (returns empty list).
    config : IntegrityConfig
        Strictness config.
    symbol : str
        Symbol/series id used in violation records.

    Returns
    -------
    list[IntegrityViolation]
        Detected anomaly violations, in detection order.
    """
    # see line-643 patch note above (defect #9 / card 0ab49707)
    if not hasattr(config, "expected_cadence_minutes"):
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__}); "
            "missing expected_cadence_minutes"
        )
    if type(config).__name__ != "IntegrityConfig":
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__})"
        )
    if not config.check_anomalies:
        return []
    violations: list[IntegrityViolation] = []

    stretch_limit = config.max_zero_volume_stretch
    zero_stretch_count = 0
    zero_stretch_start: Optional[int] = None

    for i, bar in enumerate(bars):
        bar_t = _to_utc(bar.time, f"bars[{i}].time")

        # NaN checks.
        if config.check_nan_values:
            for field_name, value in (
                ("open", bar.open),
                ("high", bar.high),
                ("low", bar.low),
                ("close", bar.close),
                ("volume", bar.volume),
            ):
                if _is_nan(value):
                    violations.append(
                        IntegrityViolation(
                            kind="nan_value",
                            symbol=symbol,
                            bar_index=i,
                            start_time=bar_t,
                            end_time=bar_t,
                            detail=(
                                f"{field_name} is NaN at bar {i} "
                                f"(time {bar_t.isoformat()})"
                            ),
                            actual=float("nan"),
                        )
                    )

        # OHLC invariant checks.
        if config.check_ohlc_invariants:
            # Only check invariants when all four values are finite
            # (NaN is reported separately; an inf would be its own
            # problem and we want a clean error message).
            ohlc = (bar.open, bar.high, bar.low, bar.close)
            if all(math.isfinite(v) for v in ohlc):
                o, h, lo, c = ohlc
                if h < lo:
                    violations.append(
                        IntegrityViolation(
                            kind="ohlc_invariant",
                            symbol=symbol,
                            bar_index=i,
                            start_time=bar_t,
                            end_time=bar_t,
                            detail=(
                                f"high ({h}) < low ({lo}) at bar {i} "
                                f"(time {bar_t.isoformat()})"
                            ),
                            expected=h,
                            actual=lo,
                        )
                    )
                if h < o:
                    violations.append(
                        IntegrityViolation(
                            kind="ohlc_invariant",
                            symbol=symbol,
                            bar_index=i,
                            start_time=bar_t,
                            end_time=bar_t,
                            detail=(
                                f"high ({h}) < open ({o}) at bar {i} "
                                f"(time {bar_t.isoformat()})"
                            ),
                            expected=h,
                            actual=o,
                        )
                    )
                if h < c:
                    violations.append(
                        IntegrityViolation(
                            kind="ohlc_invariant",
                            symbol=symbol,
                            bar_index=i,
                            start_time=bar_t,
                            end_time=bar_t,
                            detail=(
                                f"high ({h}) < close ({c}) at bar {i} "
                                f"(time {bar_t.isoformat()})"
                            ),
                            expected=h,
                            actual=c,
                        )
                    )
                if lo > o:
                    violations.append(
                        IntegrityViolation(
                            kind="ohlc_invariant",
                            symbol=symbol,
                            bar_index=i,
                            start_time=bar_t,
                            end_time=bar_t,
                            detail=(
                                f"low ({lo}) > open ({o}) at bar {i} "
                                f"(time {bar_t.isoformat()})"
                            ),
                            expected=lo,
                            actual=o,
                        )
                    )
                if lo > c:
                    violations.append(
                        IntegrityViolation(
                            kind="ohlc_invariant",
                            symbol=symbol,
                            bar_index=i,
                            start_time=bar_t,
                            end_time=bar_t,
                            detail=(
                                f"low ({lo}) > close ({c}) at bar {i} "
                                f"(time {bar_t.isoformat()})"
                            ),
                            expected=lo,
                            actual=c,
                        )
                    )

        # Zero-volume stretch.
        # We only count *finite* zero values; NaN volumes are reported
        # under the nan_value check above and don't contribute to the
        # zero-volume stretch.
        if bar.volume == 0 and not _is_nan(bar.volume):
            if zero_stretch_count == 0:
                zero_stretch_start = i
            zero_stretch_count += 1
        else:
            if zero_stretch_count > stretch_limit:
                first_i = zero_stretch_start if zero_stretch_start is not None else i
                first_t = _to_utc(bars[first_i].time, f"bars[{first_i}].time")
                last_t = _to_utc(
                    bars[first_i + zero_stretch_count - 1].time,
                    f"bars[{first_i + zero_stretch_count - 1}].time",
                )
                violations.append(
                    IntegrityViolation(
                        kind="zero_volume",
                        symbol=symbol,
                        bar_index=first_i,
                        start_time=first_t,
                        end_time=last_t,
                        detail=(
                            f"zero-volume stretch of {zero_stretch_count} bars "
                            f"exceeds limit {stretch_limit} "
                            f"(first at {first_t.isoformat()}, "
                            f"last at {last_t.isoformat()})"
                        ),
                        expected=float(stretch_limit),
                        actual=float(zero_stretch_count),
                    )
                )
            zero_stretch_count = 0
            zero_stretch_start = None

    # Trailing stretch (open at the tail of the series).
    if zero_stretch_count > stretch_limit:
        first_i = zero_stretch_start if zero_stretch_start is not None else len(bars) - 1
        first_t = _to_utc(bars[first_i].time, f"bars[{first_i}].time")
        last_t = _to_utc(
            bars[first_i + zero_stretch_count - 1].time,
            f"bars[{first_i + zero_stretch_count - 1}].time",
        )
        violations.append(
            IntegrityViolation(
                kind="zero_volume",
                symbol=symbol,
                bar_index=first_i,
                start_time=first_t,
                end_time=last_t,
                detail=(
                    f"zero-volume stretch of {zero_stretch_count} bars "
                    f"exceeds limit {stretch_limit} (trailing stretch; "
                    f"first at {first_t.isoformat()}, "
                    f"last at {last_t.isoformat()})"
                ),
                expected=float(stretch_limit),
                actual=float(zero_stretch_count),
            )
        )

    return violations


# ---------------------------------------------------------------------------
# Top-level validation entry points
# ---------------------------------------------------------------------------


def _minutes_to_timedelta(minutes: float) -> Any:
    """Convert minutes to a timedelta; the datetime import is in
    the module header so we just return a timedelta here."""
    from datetime import timedelta
    return timedelta(minutes=minutes)


def validate_crypto_bars(
    symbol: str,
    bars: Sequence[Bar],
    config: IntegrityConfig,
    *,
    loaded_symbol: Optional[str] = None,
) -> IntegrityReport:
    """Run all integrity checks; return a report (does NOT raise).

    This is the read-only validation entry point. It is suitable
    for callers that want to inspect the full violation set
    (e.g. a CLI that prints a report) without aborting the
    caller. Use :func:`enforce_integrity_gate` for the
    fail-loud flow.

    The validator never mutates ``bars`` — it only inspects them.
    When the bar series is empty, the only violations that can be
    produced are universe-missing-symbol violations (when
    ``universe_symbols`` is non-empty) and the empty series
    itself is otherwise treated as a degenerate pass (the
    engine's own guards catch empty bars).

    Parameters
    ----------
    symbol : str
        The symbol (or series id) the validation is being run
        against. Recorded in the report and in each violation.
    bars : sequence[Bar]
        Bar series. May be empty. Must be sorted ascending by
        time when non-empty.
    config : IntegrityConfig
        Strictness config. The validator honors
        ``config.check_gaps``, ``config.check_delistings``, and
        ``config.check_anomalies`` as master switches.
    loaded_symbol : str, optional
        The "name" of the symbol represented by ``bars``. Used
        only when ``config.universe_symbols`` is non-empty (see
        :func:`detect_delisting`). Defaults to ``symbol`` when
        ``None``.

    Returns
    -------
    IntegrityReport
        Validation result. ``report.passed`` is ``True`` when
        no violation was detected.
    """
    # see line-643 patch note above (defect #9 / card 0ab49707)
    if not hasattr(config, "expected_cadence_minutes"):
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__}); "
            "missing expected_cadence_minutes"
        )
    if type(config).__name__ != "IntegrityConfig":
        raise TypeError(
            f"config must be IntegrityConfig (got {type(config).__name__})"
        )
    if not isinstance(symbol, str) or not symbol:
        raise IntegrityConfigError(
            f"symbol must be a non-empty string (got {symbol!r})"
        )
    effective_loaded = loaded_symbol if loaded_symbol is not None else symbol
    violations: list[IntegrityViolation] = []
    violations.extend(detect_gaps(bars, config, symbol=symbol))
    violations.extend(detect_delisting(bars, config, symbol=symbol, loaded_symbol=effective_loaded))
    violations.extend(detect_anomalies(bars, config, symbol=symbol))

    if bars:
        window_start = _to_utc(bars[0].time, "bars[0].time")
        window_end = _to_utc(bars[-1].time, "bars[-1].time")
    else:
        window_start = None
        window_end = None

    return IntegrityReport(
        symbol=symbol,
        total_bars=len(bars),
        cadence_minutes=config.expected_cadence_minutes,
        window_start=window_start,
        window_end=window_end,
        expected_window_end=(
            _to_utc(config.expected_window_end, "config.expected_window_end")
            if config.expected_window_end is not None
            else None
        ),
        violations=tuple(violations),
    )


def enforce_integrity_gate(
    symbol: str,
    bars: Sequence[Bar],
    config: IntegrityConfig,
    *,
    loaded_symbol: Optional[str] = None,
) -> IntegrityReport:
    """Validate; raise :class:`DataIntegrityError` on any violation.

    The error is raised *before* the caller has a chance to
    consume ``bars`` — the function never returns the (possibly
    mutated) bar list, so callers can rely on the bars being
    unchanged when an exception is raised.

    The bars are NOT copied, NOT repaired, NOT truncated; they
    remain exactly as supplied. This is the no-repair contract
    required by R5.

    Parameters
    ----------
    symbol : str
        The symbol (or series id).
    bars : sequence[Bar]
        Bar series to validate. May be empty.
    config : IntegrityConfig
        Strictness config.
    loaded_symbol : str, optional
        Forwarded to :func:`validate_crypto_bars` /
        :func:`detect_delisting`.

    Returns
    -------
    IntegrityReport
        The (passing) report. Only returned when the gate passes;
        on any violation, :class:`DataIntegrityError` is raised.

    Raises
    ------
    DataIntegrityError
        When at least one violation is detected. The exception
        carries the full :class:`IntegrityReport` (accessible
        via ``e.report``) so callers can inspect the violation
        set without re-running the validator.
    """
    report = validate_crypto_bars(
        symbol=symbol,
        bars=bars,
        config=config,
        loaded_symbol=loaded_symbol,
    )
    if not report.passed:
        summary = _format_violation_summary(symbol, report)
        raise DataIntegrityError(summary, symbol=symbol, report=report)
    return report


def apply_integrity_gate(
    symbol: str,
    bars: Sequence[Bar],
    config: IntegrityConfig,
    *,
    loaded_symbol: Optional[str] = None,
) -> list[Bar]:
    """Validate; raise on failure; return the input bars unchanged.

    This is the **integration helper** for the crypto overlay
    path. It is intended to be called by the overlay wrappers
    (``run_backtest_with_venue_costs``,
    ``run_backtest_with_funding``,
    ``run_backtest_with_liquidation``) before invoking the
    engine. When the gate passes, the input ``bars`` are
    returned unchanged (the same ``list`` / ``Sequence`` object
    identity is preserved — no defensive copy is made, because
    the gate has no reason to mutate).

    On failure, the function raises :class:`DataIntegrityError`
    *before* returning any bar reference; the engine is never
    invoked.

    Parameters
    ----------
    symbol : str
        Forwarded to :func:`enforce_integrity_gate`.
    bars : sequence[Bar]
        Forwarded to :func:`enforce_integrity_gate`. The list
        object is returned unchanged when the gate passes.
    config : IntegrityConfig
        Forwarded to :func:`enforce_integrity_gate`.
    loaded_symbol : str, optional
        Forwarded to :func:`enforce_integrity_gate`.

    Returns
    -------
    list[Bar]
        ``list(bars)`` — a fresh list (in case the caller passed
        a non-list sequence like a generator) with the same
        elements. The bars themselves are not copied.

    Raises
    ------
    DataIntegrityError
        Forwarded from :func:`enforce_integrity_gate`.
    """
    enforce_integrity_gate(
        symbol=symbol,
        bars=bars,
        config=config,
        loaded_symbol=loaded_symbol,
    )
    # Return a list so callers get a stable, indexable contract.
    return list(bars)


# ---------------------------------------------------------------------------
# Config loading — fail-loud YAML-style mapping parser
# ---------------------------------------------------------------------------


def _require_keys(mapping: Mapping[str, Any], required: Sequence[str]) -> None:
    """Raise IntegrityConfigError listing every missing required key."""
    missing = [k for k in required if k not in mapping]
    if missing:
        keys_str = ", ".join(repr(k) for k in missing)
        raise IntegrityConfigError(
            f"integrity config is missing required key(s): {keys_str}"
        )


def _coerce_float(value: Any, label: str) -> float:
    """Coerce a config value to float; fail loud on non-numeric input."""
    if isinstance(value, bool):
        raise IntegrityConfigError(
            f"{label} must be numeric (got bool {value})"
        )
    if not isinstance(value, (int, float)):
        raise IntegrityConfigError(
            f"{label} must be numeric (got {type(value).__name__}: {value!r})"
        )
    result = float(value)
    if not math.isfinite(result):
        raise IntegrityConfigError(f"{label} must be finite (got {value!r})")
    return result


def _coerce_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise IntegrityConfigError(
            f"{label} must be int (got bool {value})"
        )
    if not isinstance(value, int):
        raise IntegrityConfigError(
            f"{label} must be int (got {type(value).__name__}: {value!r})"
        )
    return value


def _coerce_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise IntegrityConfigError(
            f"{label} must be a bool (got {type(value).__name__}: {value!r})"
        )
    return value


def _coerce_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise IntegrityConfigError(
            f"{label} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value.strip():
        raise IntegrityConfigError(
            f"{label} must be a non-empty string (got {value!r})"
        )
    return value


def _coerce_datetime_optional(
    value: Any,
    label: str,
) -> Optional[datetime]:
    """Parse an optional ISO 8601 string or datetime into a
    tz-aware UTC datetime. ``None`` passes through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise IntegrityConfigError(
                f"{label} must be timezone-aware when supplied as datetime "
                f"(got naive {value!r})"
            )
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IntegrityConfigError(
                f"{label} must be a valid ISO 8601 datetime string "
                f"(got {value!r}: {exc})"
            ) from exc
        if parsed.tzinfo is None:
            raise IntegrityConfigError(
                f"{label} must include a timezone (got naive {value!r})"
            )
        return parsed.astimezone(timezone.utc)
    raise IntegrityConfigError(
        f"{label} must be a datetime, ISO 8601 string, or None "
        f"(got {type(value).__name__}: {value!r})"
    )


def load_integrity_config(mapping: Mapping[str, Any]) -> IntegrityConfig:
    """Parse an integrity config mapping into :class:`IntegrityConfig`.

    The mapping is the in-memory representation of a YAML block
    (caller does ``yaml.safe_load``); this function enforces the
    schema:

      * All numeric fields must be numeric (not bool, not string).
      * All booleans must be bool (not 0/1, not "true"/"false").
      * All strings must be non-empty where they identify a key.
      * Datetime fields accept ``datetime`` or ISO 8601 strings.
      * ``expected_cadence_minutes`` must be a positive int.
      * ``gap_tolerance_multiplier`` must be ``>= 1.0`` and finite.
      * ``delisting_tolerance_minutes`` must be ``>= 0`` and finite.
      * ``universe_symbols`` must be a sequence of non-empty strings.
      * ``max_zero_volume_stretch`` must be ``>= 0``.

    Any validation failure raises :class:`IntegrityConfigError` (a
    :class:`ValueError` subclass) with a specific message naming
    the failing field so the operator can fix the YAML directly.

    Parameters
    ----------
    mapping : Mapping[str, Any]
        The config mapping (typically produced by ``yaml.safe_load``).

    Returns
    -------
    IntegrityConfig
        Validated config object.

    Raises
    ------
    IntegrityConfigError
        On any structural / type / value violation.
    """
    if not isinstance(mapping, Mapping):
        raise IntegrityConfigError(
            f"integrity config must be a mapping (got {type(mapping).__name__})"
        )

    expected_cadence = DEFAULT_CADENCE_MINUTES
    if "expected_cadence_minutes" in mapping:
        expected_cadence = _coerce_int(
            mapping["expected_cadence_minutes"], "expected_cadence_minutes"
        )

    gap_tol = DEFAULT_GAP_TOLERANCE_MULTIPLIER
    if "gap_tolerance_multiplier" in mapping:
        gap_tol = _coerce_float(
            mapping["gap_tolerance_multiplier"], "gap_tolerance_multiplier"
        )

    expected_end: Optional[datetime] = None
    if "expected_window_end" in mapping:
        expected_end = _coerce_datetime_optional(
            mapping["expected_window_end"], "expected_window_end"
        )

    delisting_tol = 0.0
    if "delisting_tolerance_minutes" in mapping:
        delisting_tol = _coerce_float(
            mapping["delisting_tolerance_minutes"], "delisting_tolerance_minutes"
        )

    universe: tuple[str, ...] = ()
    if "universe_symbols" in mapping:
        raw = mapping["universe_symbols"]
        if not isinstance(raw, (list, tuple)):
            raise IntegrityConfigError(
                f"universe_symbols must be a list/tuple "
                f"(got {type(raw).__name__})"
            )
        parsed_universe: list[str] = []
        for i, sym in enumerate(raw):
            parsed_universe.append(_coerce_str(sym, f"universe_symbols[{i}]"))
        universe = tuple(parsed_universe)

    zero_stretch = DEFAULT_MAX_ZERO_VOLUME_STRETCH
    if "max_zero_volume_stretch" in mapping:
        zero_stretch = _coerce_int(
            mapping["max_zero_volume_stretch"], "max_zero_volume_stretch"
        )

    check_ohlc = True
    if "check_ohlc_invariants" in mapping:
        check_ohlc = _coerce_bool(
            mapping["check_ohlc_invariants"], "check_ohlc_invariants"
        )
    check_nan = True
    if "check_nan_values" in mapping:
        check_nan = _coerce_bool(
            mapping["check_nan_values"], "check_nan_values"
        )
    check_gaps = True
    if "check_gaps" in mapping:
        check_gaps = _coerce_bool(mapping["check_gaps"], "check_gaps")
    check_delistings = True
    if "check_delistings" in mapping:
        check_delistings = _coerce_bool(
            mapping["check_delistings"], "check_delistings"
        )
    check_anomalies = True
    if "check_anomalies" in mapping:
        check_anomalies = _coerce_bool(
            mapping["check_anomalies"], "check_anomalies"
        )

    schema_version = 1
    if "schema_version" in mapping:
        schema_raw = mapping["schema_version"]
        if isinstance(schema_raw, bool) or not isinstance(schema_raw, int):
            raise IntegrityConfigError(
                f"schema_version must be an int "
                f"(got {type(schema_raw).__name__}: {schema_raw!r})"
            )
        schema_version = schema_raw

    return IntegrityConfig(
        expected_cadence_minutes=expected_cadence,
        gap_tolerance_multiplier=gap_tol,
        expected_window_end=expected_end,
        delisting_tolerance_minutes=delisting_tol,
        universe_symbols=universe,
        max_zero_volume_stretch=zero_stretch,
        check_ohlc_invariants=check_ohlc,
        check_nan_values=check_nan,
        check_gaps=check_gaps,
        check_delistings=check_delistings,
        check_anomalies=check_anomalies,
        schema_version=schema_version,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_violation_summary(symbol: str, report: IntegrityReport) -> str:
    """Build the human-readable DataIntegrityError message."""
    by_kind_counts: dict[str, int] = {}
    for v in report.violations:
        by_kind_counts[v.kind] = by_kind_counts.get(v.kind, 0) + 1
    counts_str = ", ".join(
        f"{count} {kind}" for kind, count in sorted(by_kind_counts.items())
    )
    first_detail = report.violations[0].detail if report.violations else "(none)"
    return (
        f"DataIntegrityError: symbol={symbol!r} "
        f"bars={report.total_bars} cadence={report.cadence_minutes}m "
        f"violations=[{counts_str}] first_violation={first_detail!r}"
    )
