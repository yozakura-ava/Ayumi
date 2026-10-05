"""Adaptive perpetual-funding cost model for crypto backtests.

Card: 9cf5f1c1-9572-4d1e-9fe8-00949bc6387a (Sprint C 1a.1)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

What this module provides
==========================

A funding cost model for the crypto backtest core that charges realized
perpetual funding at the actual settlement timestamps produced by
Binance's *adaptive settlement frequency* (effective 2025-05-02):

  * **Base cadence**: 8-hour settlements at 00:00, 08:00, 16:00 UTC.
  * **Adaptive switch**: when the funding rate reaches the ±0.3% cap /
    floor, the venue shortens the *next* interval to 1 hour; once the
    cap/floor is not reached, the cadence reverts to 8 hours.
  * **Cap / floor**: ±0.003 (i.e. 0.3%) per Binance USDⓈ-M perps.

The cost line is applied as a recurring P&L charge — *not* on a fixed
8-hour grid, which factually understates carry and biases Sharpe
upward (Satsuki R1, §1.1 of
``docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md``).

Integration contract
====================

The model is intentionally decoupled from
:class:`forex_bot.engine.engine.BacktestEngine`. The engine still runs
unchanged; :func:`run_backtest_with_funding` wraps the engine, computes
the funding schedule from the realized rate series, and returns a
:class:`FundedBacktestMetrics` extension of
:class:`backtest.types.BacktestMetrics` carrying:

  * the unchanged ``base`` engine metrics (legacy path preserved),
  * the ``funding_events`` actually applied (with bar indices),
  * the ``total_funding_cost`` as a single number,
  * a per-bar ``funded_equity_delta`` series (funding adjustments,
    length ``len(bars)``) that downstream consumers can overlay onto
    any per-bar equity curve they construct,
  * the ``funded_ending_balance`` after subtracting funding P&L from
    the engine's reported ending balance.

This keeps the existing forex path 100% untouched — callers that don't
opt into funding see identical behaviour.

Funding economics (perp convention)
===================================

  * Positive funding rate → longs pay shorts.
  * Negative funding rate → shorts pay longs.
  * Per-event dollar P&L for a *long* with notional ``N`` and rate ``r``:
    ``pnl = -N * r``
  * Per-event dollar P&L for a *short* with notional ``N`` and rate ``r``:
    ``pnl = +N * r``

Source: Binance USDⓈ-M funding rate docs (see research doc §5 source list).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Optional, Protocol, Sequence, Union

from backtest.types import BacktestConfig, BacktestMetrics, Bar

__all__ = [
    "FundingEvent",
    "AdaptiveFundingSchedule",
    "PositionSpec",
    "FundingCostLine",
    "FundedBacktestMetrics",
    "compute_adaptive_schedule",
    "compute_funding_pnl",
    "run_backtest_with_funding",
    "BASELINE_8H_HOURS",
    "CAP_RATE",
    "DEFAULT_BASE_HOURS",
    "FAST_HOURS",
    "SETTLEMENT_HOURS_UTC",
]


# ---------------------------------------------------------------------------
# Constants — Binance USDⓈ-M adaptive funding spec
# ---------------------------------------------------------------------------

#: Default settlement cadence when no cap/floor is hit (Binance spec).
DEFAULT_BASE_HOURS: int = 8

#: Adaptive interval used after a cap/floor trigger (Binance spec).
FAST_HOURS: int = 1

#: Hardcoded 8h baseline; 4h is reserved for future venue configs but
#: not used by Binance USDⓈ-M post-2025-05-02. Kept here so the
#: schedule is auditable against the spec wording ("8h/4h base").
BASELINE_8H_HOURS: int = 8

#: ±0.3% cap/floor threshold per Binance spec.
CAP_RATE: float = 0.003

#: Default settlement hours UTC for the 8h grid (00, 08, 16).
SETTLEMENT_HOURS_UTC: tuple[int, ...] = (0, 8, 16)


# ---------------------------------------------------------------------------
# Duck-typed strategy interface (used by the engine wrapper only)
# ---------------------------------------------------------------------------


class _IStrategyLike(Protocol):
    """Structural type for the strategies list.

    The real engine accepts any object exposing ``name`` + ``evaluate``
    (see ``backtest.strategies.isignal_strategy.ISignalStrategy``); we
    type the parameter as a Protocol to avoid an import cycle.
    """

    name: str

    def evaluate(self, state: object) -> object: ...


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingEvent:
    """A single realized funding settlement.

    Attributes
    ----------
    time : datetime
        Settlement time in UTC. Must be timezone-aware.
    rate : float
        Signed funding rate as a decimal (e.g. ``0.0001`` = 1 bp).
        Positive = longs pay shorts; negative = shorts pay longs.
    interval_hours : int
        Cadence used to reach *this* event from the prior one (8 by
        default; 1 if the previous settlement's rate hit ±0.3% and
        forced adaptive 1h cadence).
    cap_triggered : bool
        ``True`` iff the rate at *this* settlement hit ``±CAP_RATE``,
        meaning the *next* interval will be 1h rather than 8h.

    Notes
    -----
    The :class:`AdaptiveFundingSchedule` derives ``interval_hours`` and
    ``cap_triggered`` from the realized rate series; constructing a
    :class:`FundingEvent` directly is supported for tests and synthetic
    fixtures but is not the intended API in production.
    """

    time: datetime
    rate: float
    interval_hours: int
    cap_triggered: bool

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValueError(
                f"FundingEvent.time must be timezone-aware (got naive {self.time!r})"
            )
        if self.time.tzinfo != timezone.utc:
            # Normalize so downstream arithmetic is consistent.
            object.__setattr__(self, "time", self.time.astimezone(timezone.utc))
        if self.interval_hours not in (1, 4, 8):
            raise ValueError(
                f"interval_hours must be 1, 4, or 8 (got {self.interval_hours})"
            )
        if not math.isfinite(self.rate):
            raise ValueError(f"rate must be finite (got {self.rate!r})")


# Backwards-compatible alias — Satsuki R1 brief and other research uses
# ``FundingRateSnapshot`` (the adapter type). Accept either.
FundingRateSnapshotLike = Union["FundingEvent", "FundingRateSnapshot"]


@dataclass(frozen=True)
class FundingRateSnapshot:
    """Duck-typed adapter snapshot shape.

    Mirrors ``data.crypto_adapter.FundingRateSnapshot`` without importing
    it (to avoid a circular dep backtest → data → backtest). The
    adapter exposes ``symbol``, ``time``, ``funding_rate``, and
    ``mark_price``. We only consume ``time`` and ``funding_rate``.
    """

    symbol: str
    time: datetime
    funding_rate: float
    mark_price: Optional[float] = None


class _SnapshotLike(Protocol):
    """Structural type for duck-typed funding snapshot inputs.

    Matches ``FundingRateSnapshot`` (``backtest.types``),
    ``data.crypto_adapter.FundingRateSnapshot``, and any other object
    that exposes a settlement ``time`` + a signed ``funding_rate``.
    """

    time: datetime
    funding_rate: float


def _to_event_like(obj: object) -> tuple[datetime, float]:
    """Coerce FundingEvent / FundingRateSnapshot / duck-typed → (time, rate)."""
    if isinstance(obj, FundingEvent):
        return obj.time, obj.rate
    if isinstance(obj, FundingRateSnapshot):
        return obj.time, float(obj.funding_rate)
    # Generic duck-typed object: read attributes by name. Cast to the
    # structural protocol so mypy understands the access; the runtime
    # semantics rely on the caller passing an object that exposes
    # ``time`` + ``funding_rate`` (e.g. ``data.crypto_adapter.FundingRateSnapshot``).
    snap: _SnapshotLike = obj  # type: ignore[assignment]
    return snap.time, float(snap.funding_rate)


# ---------------------------------------------------------------------------
# Adaptive funding schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptiveFundingSchedule:
    """Result of :func:`compute_adaptive_schedule`.

    Attributes
    ----------
    events : tuple[FundingEvent, ...]
        Sorted, ascending by time.
    total_events : int
        ``len(events)`` — convenience for callers that want a count.
    cap_events : int
        Number of events where ``cap_triggered is True`` — a sanity
        hook for tests and runtime observability.
    adaptive_count : int
        Number of events at 1h intervals (i.e. cadence deviated from
        8h). When this is non-zero the schedule differs from the
        fixed-8h grid that factually understates carry.
    """

    events: tuple[FundingEvent, ...]
    total_events: int = field(init=False)
    cap_events: int = field(init=False)
    adaptive_count: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_events", len(self.events))
        object.__setattr__(
            self,
            "cap_events",
            sum(1 for e in self.events if e.cap_triggered),
        )
        object.__setattr__(
            self,
            "adaptive_count",
            sum(1 for e in self.events if e.interval_hours != BASELINE_8H_HOURS),
        )

    def total_dollar_charge(self, notional_usd: float, direction: Literal["long", "short"]) -> float:
        """Sum of dollar P&L across all events for a fixed notional/direction.

        Convenience for spot-check tests; production callers use
        :func:`compute_funding_pnl` directly with per-event overlays.
        """
        if notional_usd <= 0:
            raise ValueError(f"notional_usd must be positive (got {notional_usd})")
        if direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short' (got {direction!r})")
        sign = -1.0 if direction == "long" else 1.0
        return sum(sign * notional_usd * e.rate for e in self.events)


def _normalize_input_events(
    inputs: Iterable[object],
) -> list[tuple[datetime, float]]:
    """Coerce an iterable of FundingEvent / snapshots to (time, rate) pairs.

    The list is sorted ascending by time. Naive datetimes are rejected
    (FundingEvent.__post_init__ rejects them too; we reject here so
    generic-snapshot callers get the same treatment).
    """
    pairs: list[tuple[datetime, float]] = []
    for obj in inputs:
        t, r = _to_event_like(obj)
        if t.tzinfo is None:
            raise ValueError(f"Naive datetime in funding input: {t!r}")
        if t.tzinfo != timezone.utc:
            t = t.astimezone(timezone.utc)
        if not math.isfinite(r):
            raise ValueError(f"Non-finite funding rate in input: {r!r}")
        pairs.append((t, r))
    pairs.sort(key=lambda p: p[0])
    return pairs


def _floor_to_settlement(t: datetime) -> datetime:
    """Round ``t`` *down* to the nearest 8h settlement (00/08/16 UTC).

    This is purely an anchor helper for empty-input windows; for windows
    with at least one realized event, the schedule anchors at the first
    event time (not snapped).
    """
    if t.tzinfo is None or t.tzinfo != timezone.utc:
        t = t.astimezone(timezone.utc)
    # Pick the most recent settlement hour <= t.hour.
    h = t.hour
    if h < 8:
        settle_h = 0
    elif h < 16:
        settle_h = 8
    else:
        settle_h = 16
    return t.replace(hour=settle_h, minute=0, second=0, microsecond=0)


def compute_adaptive_schedule(
    inputs: Iterable[object],
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    cap_rate: float = CAP_RATE,
    base_hours: int = DEFAULT_BASE_HOURS,
) -> AdaptiveFundingSchedule:
    """Build the adaptive settlement schedule from a realized rate series.

    Parameters
    ----------
    inputs : iterable
        Funding events or duck-typed snapshots (any object exposing
        ``time`` + ``funding_rate``). Times must be timezone-aware.
    start : datetime, optional
        Lower bound (inclusive). When ``None`` and inputs is non-empty,
        the first input's time is used. When ``None`` and inputs is
        empty, ``end`` must also be supplied (else empty schedule).
    end : datetime, optional
        Upper bound (inclusive). When ``None`` and inputs is non-empty,
        the last input's time is used. When ``None`` and inputs is
        empty, ``start`` must be supplied (else empty schedule).
    cap_rate : float
        Magnitude that triggers adaptive cadence (default ``CAP_RATE``
        = 0.003 per Binance spec).
    base_hours : int
        Base cadence (default ``8``). Kept parameterized so future
        venues with 4h or other bases can reuse the algorithm.

    Returns
    -------
    AdaptiveFundingSchedule
        Frozen result with computed ``events`` tuple and observability
        counters (``total_events``, ``cap_events``, ``adaptive_count``).

    Algorithm
    ---------
    1. Build a sorted (time → rate) lookup from the input series.
    2. Anchor at ``start`` (snapped down to the 8h settlement grid
       when start is supplied; or at the first input time when start
       is omitted).
    3. At each step: emit a FundingEvent at the current anchor with the
       rate that matches this time (or 0.0 if missing).
    4. Set ``cap_triggered = |rate| >= cap_rate`` at this settlement.
    5. Advance the anchor by 1h if the PREVIOUS settlement's rate hit
       the cap, else by ``base_hours``.
    6. Stop once the anchor exceeds ``end``.

    This matches the spec language: "shifting to 1-hour when the
    funding rate reaches the ±0.3% cap/floor, reverting when the cap
    is not reached" — each settlement's rate determines the *next*
    interval.

    Notes
    -----
    The schedule is computed in **O(k)** where *k* is the number of
    generated events; the input series is read in sorted order. Empty
    input + no ``start``/``end`` → empty schedule (no synthetic
    events). Empty input + both bounds supplied → all-zero-rate events
    on the 8h grid (this is the engine's contract: a backtest window
    with no realized funding data still sees the 8h grid as a
    lower-bound cost line).
    """
    if cap_rate <= 0:
        raise ValueError(f"cap_rate must be positive (got {cap_rate})")
    if base_hours not in (1, 4, 8):
        raise ValueError(f"base_hours must be 1, 4, or 8 (got {base_hours})")

    lookup = _normalize_input_events(inputs)
    if not lookup and start is None and end is None:
        return AdaptiveFundingSchedule(())

    # Resolve start/end to timezone-aware datetimes (never None past this point).
    resolved_start = start if start is not None else (lookup[0][0] if lookup else end)
    resolved_end = end if end is not None else (lookup[-1][0] if lookup else resolved_start)
    if resolved_start is None or resolved_end is None:
        # Unreachable: the early-return above handles inputs=[] when both
        # bounds are None, and at least one of lookup or start/end is
        # supplied. Defensive guard for mypy narrowing.
        raise ValueError("start and end must be resolved to datetimes")
    start = resolved_start
    end = resolved_end

    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None:
        raise ValueError("end must be timezone-aware")
    if end < start:
        raise ValueError(f"end ({end}) < start ({start})")

    rate_at: dict[datetime, float] = {t: r for t, r in lookup}
    # Snap start down to the 8h settlement grid so the schedule begins
    # at a real Binance settlement, never at an arbitrary intra-hour.
    anchor = _floor_to_settlement(start)
    # Edge case: if start is exactly at a settlement, anchor == start;
    # if start is e.g. 03:00, anchor snaps to 00:00. In both cases the
    # first emitted event is on the 8h grid.

    events: list[FundingEvent] = []
    cap_prev = False  # cap state carried from the PREVIOUS settlement

    while anchor <= end:
        # Rate at this anchor (0 if not in the realized series).
        rate = rate_at.get(anchor, 0.0)
        cap_now = abs(rate) >= cap_rate
        # interval_hours recorded on THIS event = the distance from the
        # previous settlement to this one. Default to base_hours for the
        # first event (no preceding settlement).
        interval_hours = FAST_HOURS if cap_prev else base_hours
        # The advance to the NEXT event is governed by THIS event's cap:
        # if this settlement hit ±0.3%, the next interval is 1h.
        next_interval_hours = FAST_HOURS if cap_now else base_hours
        events.append(
            FundingEvent(
                time=anchor,
                rate=rate,
                interval_hours=interval_hours,
                cap_triggered=cap_now,
            )
        )
        cap_prev = cap_now
        anchor = anchor + timedelta(hours=next_interval_hours)

    return AdaptiveFundingSchedule(tuple(events))


# ---------------------------------------------------------------------------
# Per-position funding P&L
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSpec:
    """The position to charge funding against.

    Attributes
    ----------
    notional_usd : float
        Position notional in USD (or USD-equivalent quote currency).
        For ``lot_size`` units of a base asset priced at ``entry_price``
        (USD per unit), notional = ``lot_size * entry_price``.
    direction : {'long', 'short'}
        Trade direction. Long pays funding when ``rate > 0``; short
        receives. The opposite when ``rate < 0``.
    """

    notional_usd: float
    direction: Literal["long", "short"]

    def __post_init__(self) -> None:
        if self.notional_usd <= 0:
            raise ValueError(f"notional_usd must be positive (got {self.notional_usd})")
        if self.direction not in ("long", "short"):
            raise ValueError(
                f"direction must be 'long' or 'short' (got {self.direction!r})"
            )


@dataclass(frozen=True)
class FundingCostLine:
    """A single funding-charge line item.

    Attributes
    ----------
    time : datetime
        Settlement time (UTC).
    rate : float
        Realized funding rate at this settlement.
    pnl : float
        Dollar P&L applied to the position (negative = paid; positive
        = received).
    interval_hours : int
        Cadence used to reach this event (1 if previous was capped).
    bar_index : int
        Index of the bar in the supplied bar list at which this
        funding event lands (i.e. the first bar whose time is >= the
        settlement time). ``-1`` if the event is past the bar window
        (excluded from the funded equity series).
    """

    time: datetime
    rate: float
    pnl: float
    interval_hours: int
    bar_index: int


def compute_funding_pnl(
    schedule: AdaptiveFundingSchedule,
    position: PositionSpec,
    bars: Optional[Sequence[Bar]] = None,
) -> list[FundingCostLine]:
    """Compute the per-event funding P&L for a fixed position spec.

    Returns a list of :class:`FundingCostLine` aligned with the
    schedule. The sum of ``pnl`` across the list is the total funding
    contribution to P&L for the period.

    For a *long* with notional ``N`` and rate ``r``:
        pnl = -N * r   (positive rate → pay; negative rate → receive)

    For a *short* with notional ``N`` and rate ``r``:
        pnl = +N * r   (positive rate → receive; negative rate → pay)

    Parameters
    ----------
    schedule : AdaptiveFundingSchedule
        The settlement schedule (built via ``compute_adaptive_schedule``).
    position : PositionSpec
        Notional + direction for the funding calculation.
    bars : sequence[Bar], optional
        When supplied, each FundingCostLine is annotated with the bar
        index where the funding event lands (first bar whose time >=
        the settlement time). When ``None``, ``bar_index`` is ``-1``.

    Notes
    -----
    Events whose settlement time exceeds the last bar's time get
    ``bar_index = -1`` and are excluded from any per-bar overlay.
    """
    if not isinstance(schedule, AdaptiveFundingSchedule):
        raise TypeError(
            f"schedule must be AdaptiveFundingSchedule (got {type(schedule).__name__})"
        )
    sign = -1.0 if position.direction == "long" else 1.0
    out: list[FundingCostLine] = []
    for ev in schedule.events:
        pnl = sign * position.notional_usd * ev.rate
        bar_idx = -1
        if bars is not None and len(bars) > 0:
            bar_idx = _find_bar_index(bars, ev.time)
            if bar_idx >= len(bars):
                bar_idx = -1
        out.append(
            FundingCostLine(
                time=ev.time,
                rate=ev.rate,
                pnl=pnl,
                interval_hours=ev.interval_hours,
                bar_index=bar_idx,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Backtest integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundedBacktestMetrics:
    """BacktestMetrics + funding overlay (R1 deliverable shape).

    Adds four fields to :class:`backtest.types.BacktestMetrics` without
    mutating the original dataclass (which other engines also
    construct).

    Attributes
    ----------
    base : BacktestMetrics
        The original engine output (unchanged).
    funding_events : tuple[FundingCostLine, ...]
        Per-event funding P&L applied (in schedule order).
    total_funding_cost : float
        Sum of ``pnl`` across ``funding_events`` (negative = net paid;
        positive = net received).
    funded_equity_delta : list[float]
        Per-bar funding deltas, length ``len(bars)``. ``[i]`` is the
        cumulative funding P&L charged at bar ``i`` (only nonzero at
        bars where a funding event lands). Downstream consumers can
        add this to their own per-bar equity curve.
    funded_ending_balance : float
        ``base.ending_balance + total_funding_cost``.
    """

    base: BacktestMetrics
    funding_events: tuple[FundingCostLine, ...]
    total_funding_cost: float
    funded_equity_delta: list[float]
    funded_ending_balance: float


def _find_bar_index(bars: Sequence[Bar], event_time: datetime) -> int:
    """Find the first bar whose time is >= event_time.

    Returns ``len(bars)`` if no such bar exists (i.e. event is past the
    backtest window — funding for that event is not realized inside
    the window and is therefore excluded from ``funded_equity_delta``).
    """
    for i, bar in enumerate(bars):
        bt = bar.time
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        else:
            bt = bt.astimezone(timezone.utc)
        if bt >= event_time:
            return i
    return len(bars)


def run_backtest_with_funding(
    bars: Sequence[Bar],
    funding_events: Sequence[object],
    position: PositionSpec,
    config: BacktestConfig,
    strategies: Sequence[_IStrategyLike],
    strategy_name: Optional[str] = None,
) -> FundedBacktestMetrics:
    """Run :class:`BacktestEngine` and overlay adaptive funding P&L.

    Parameters
    ----------
    bars : sequence[Bar]
        Bar series. Must be sorted ascending by time.
    funding_events : sequence
        Realized funding series. Accepts
        :class:`backtest.funding_model.FundingEvent`,
        :class:`backtest.funding_model.FundingRateSnapshot`, or any
        duck-typed object exposing ``time`` + ``funding_rate``.
    position : PositionSpec
        The position to charge funding against. The engine currently
        runs single-strategy; ``position`` is therefore a flat
        notional/direction spec applied to every funding event.
    config : BacktestConfig
        Backtest configuration (forwarded to ``BacktestEngine``).
        ``config.funding_events`` is *not* consulted by the engine —
        this wrapper computes funding independently and overlays it.
    strategies : sequence
        Strategies passed to ``BacktestEngine``. Must be non-empty.
    strategy_name : str, optional
        Which strategy's metrics to wrap. Defaults to the first
        strategy in the list.

    Returns
    -------
    FundedBacktestMetrics
        Base + funding overlay. Funding events past the bar window are
        excluded (they didn't realize inside the backtest period).

    Notes
    -----
    The funding P&L is applied at the bar whose time is >= the funding
    event's settlement time. This is a simplification (the engine
    doesn't expose per-bar position state here) — the wrapper assumes
    the position was open for the entire window. For strategies that
    close positions intra-window, callers should pre-filter
    ``funding_events`` to only those settlements when the position was
    held.

    This contract is documented; downstream consumers in Sprint C 1a.2
    (per-position tracking) can refine it without changing the public
    shape of :class:`FundedBacktestMetrics`.
    """
    if not isinstance(position, PositionSpec):
        raise TypeError(
            f"position must be PositionSpec (got {type(position).__name__})"
        )
    if not isinstance(config, BacktestConfig):
        raise TypeError(
            f"config must be BacktestConfig (got {type(config).__name__})"
        )
    if not bars:
        raise ValueError("bars must be non-empty")
    if not strategies:
        raise ValueError("strategies must be non-empty")

    # Lazy import: the engine imports heavy deps; we don't want to
    # import them just to validate args.
    from engine.engine import BacktestEngine

    name = strategy_name if strategy_name is not None else getattr(strategies[0], "name", None)
    engine = BacktestEngine(config, list(strategies))
    # The real engine has ``run_single`` (per-strategy) and ``run_all``
    # (multi-strategy). Run each strategy independently and pick the
    # one we were asked to wrap.
    if name is not None:
        for s in strategies:
            if getattr(s, "name", None) == name:
                base_metrics = engine.run_single(s, list(bars))
                break
        else:
            # Strategy name not found → fall back to the first one.
            base_metrics = engine.run_single(strategies[0], list(bars))
    else:
        base_metrics = engine.run_single(strategies[0], list(bars))

    schedule = compute_adaptive_schedule(
        funding_events,
        start=bars[0].time,
        end=bars[-1].time,
    )
    lines = compute_funding_pnl(schedule, position, bars=bars)

    # Build per-bar funding delta series. Length = len(bars). Each entry
    # is the sum of funding P&L charged at that bar.
    deltas = [0.0] * len(bars)
    for line in lines:
        if 0 <= line.bar_index < len(bars):
            deltas[line.bar_index] += line.pnl

    total_funding = sum(line.pnl for line in lines)

    return FundedBacktestMetrics(
        base=base_metrics,
        funding_events=tuple(lines),
        total_funding_cost=total_funding,
        funded_equity_delta=deltas,
        funded_ending_balance=base_metrics.ending_balance + total_funding,
    )
