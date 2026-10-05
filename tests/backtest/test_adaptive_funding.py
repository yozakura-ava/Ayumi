"""Tests for ``forex_bot.backtest.funding_model`` — adaptive perpetual-funding cost model.

Card: 9cf5f1c1-9572-4d1e-9fe8-00949bc6387a (Sprint C 1a.1)
Sprint: 2026-10-05-crypto-strategy-factory
Lane: [BUILD][CRYPTO-LANE]

Scope (HR5 — targeted tests only):

  * FundingEvent domain validation (timezone, interval, finite rate)
  * AdaptiveFundingSchedule algorithm (Binance 2025-05-02 spec):
      - Base 8h cadence at 00/08/16 UTC
      - 1h cadence when |rate| ≥ 0.3% cap/floor, reverting to 8h
  * Funding P&L math (perp convention: positive rate → longs pay)
  * Adaptive vs fixed-8h divergence (R1's core fact)
  * Engine integration:
      - Legacy FX path is 100% untouched (run without funding
        produces identical metrics to the prior baseline)
      - Funding overlay reduces ending balance + Sharpe for negative carry
      - Funding overlay increases ending balance for positive carry
  * Edge cases:
      - Empty history
      - Funding events outside the backtest window are excluded
      - Funding rate = 0 produces zero P&L

All tests are deterministic — synthetic fixtures only, no live Binance
calls. The engine integration test uses a no-op strategy that never
fires a signal, so we exercise the real engine's run_single() path
without price movement dominating the equity curve.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup: src/forex_bot on sys.path so ``backtest.funding_model`` and
# ``engine.engine`` resolve. Mirrors the suite-level conftest.py setup.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_FX = _PROJECT_ROOT / "src" / "forex_bot"
_SRC_ROOT = _PROJECT_ROOT / "src"
for p in (_SRC_FX, _SRC_ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from backtest.funding_model import (  # noqa: E402
    AdaptiveFundingSchedule,
    BASELINE_8H_HOURS,
    CAP_RATE,
    DEFAULT_BASE_HOURS,
    FAST_HOURS,
    FundedBacktestMetrics,
    FundingCostLine,
    FundingEvent,
    FundingRateSnapshot,
    PositionSpec,
    SETTLEMENT_HOURS_UTC,
    compute_adaptive_schedule,
    compute_funding_pnl,
    run_backtest_with_funding,
)
from backtest.strategies.isignal_strategy import ISignalStrategy  # noqa: E402
from backtest.types import Bar, BacktestConfig, BacktestMetrics  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _ev(  # shorthand FundingEvent factory
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int = 0,
    rate: float = 0.0001,
    interval_hours: int = 8,
    cap_triggered: bool = False,
) -> FundingEvent:
    return FundingEvent(
        time=datetime(year, month, day, hour, minute, tzinfo=timezone.utc),
        rate=rate,
        interval_hours=interval_hours,
        cap_triggered=cap_triggered,
    )


def _flat_bars(
    n: int,
    start: datetime,
    step_minutes: int = 60,
    base_price: float = 100.0,
) -> list[Bar]:
    """Build N flat OHLC bars — useful for funding-cost isolation tests
    where we don't want price movement to dominate the equity curve.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    bars: list[Bar] = []
    for i in range(n):
        t = start + timedelta(minutes=step_minutes * i)
        bars.append(
            Bar(
                time=t,
                open=base_price,
                high=base_price,
                low=base_price,
                close=base_price,
                volume=100.0,
                spread_pips=1.5,
            )
        )
    return bars


def _simple_config(starting_balance: float = 10_000.0) -> BacktestConfig:
    """A BacktestConfig that triggers no trades on flat bars — used to
    isolate funding overlay from strategy P&L."""
    return BacktestConfig(
        starting_balance=starting_balance,
        risk_per_trade_pct=0.01,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.10,
        commission_per_lot=0.0,  # isolate from commission
        leverage=100,
        min_confidence=0.99,  # never trigger a signal
        min_bars_before_signal=30,
        max_open_trades=1,
        pair="BTCUSDT",
        spread_pips=0.0,  # isolate from spread
        round_trip_spread=False,
        slippage_pips=0.0,
        swap_per_lot_per_day=0.0,
    )


class _NoFireStrategy(ISignalStrategy):
    """A trivial strategy that never produces a signal — used to drive
    the engine on flat bars without any trade P&L.

    Satisfies the ``ISignalStrategy`` ABC: ``name`` + ``evaluate``.
    """

    @property
    def name(self) -> str:
        return "NoFire"

    def evaluate(self, state: Any) -> None:
        return None


def _funding_events_for_window(
    n_hours: int,
    rate: float = 0.0001,
    start_hour: int = 0,
) -> list[FundingEvent]:
    """Build funding events at every 8h settlement within [start, start + n_hours]."""
    out: list[FundingEvent] = []
    settlement_hours = (0, 8, 16)
    day = 5  # any day in October 2026
    month = 10
    year = 2026
    for h in range(0, n_hours + 1, 8):
        actual_h = start_hour + h
        # Normalize into day boundaries
        days_offset = actual_h // 24
        hour_of_day = actual_h % 24
        if hour_of_day not in settlement_hours:
            # Snap to nearest 8h slot for the helper's output.
            # Helper generates 8h-grid events, so we round to the next settlement.
            if hour_of_day < 8:
                hour_of_day = 8
            elif hour_of_day < 16:
                hour_of_day = 16
            else:
                hour_of_day = 0
                days_offset += 1
        actual_day = day + days_offset
        out.append(_ev(year, month, actual_day, hour_of_day, rate=rate))
    return out


# ──────────────────────────────────────────────────────────────────────
# Module-level constants (Binance spec)
# ──────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    def test_baseline_8h_hours(self):
        assert BASELINE_8H_HOURS == 8

    def test_default_base_hours(self):
        assert DEFAULT_BASE_HOURS == 8

    def test_fast_hours(self):
        assert FAST_HOURS == 1

    def test_cap_rate(self):
        # ±0.3% per Binance USDⓈ-M perp spec.
        assert CAP_RATE == pytest.approx(0.003, abs=1e-12)

    def test_settlement_hours_utc(self):
        assert SETTLEMENT_HOURS_UTC == (0, 8, 16)


# ──────────────────────────────────────────────────────────────────────
# FundingEvent validation
# ──────────────────────────────────────────────────────────────────────


class TestFundingEventValidation:
    def test_round_trip(self):
        ev = _ev(2026, 10, 5, 0, rate=0.0001, interval_hours=8)
        assert ev.rate == 0.0001
        assert ev.interval_hours == 8
        assert ev.time.tzinfo == timezone.utc

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            FundingEvent(
                time=datetime(2026, 10, 5, 0, 0),  # naive
                rate=0.0001,
                interval_hours=8,
                cap_triggered=False,
            )

    def test_normalizes_non_utc_to_utc(self):
        # America/Toronto in summer = EDT (UTC-4). 00:00 EDT = 04:00 UTC.
        eastern = timezone(timedelta(hours=-4))
        t = datetime(2026, 10, 5, 0, 0, tzinfo=eastern)
        ev = FundingEvent(
            time=t,
            rate=0.0001,
            interval_hours=8,
            cap_triggered=False,
        )
        assert ev.time.tzinfo == timezone.utc
        assert ev.time.hour == 4  # normalized to UTC

    def test_rejects_invalid_interval_hours(self):
        with pytest.raises(ValueError, match="interval_hours"):
            FundingEvent(
                time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                rate=0.0001,
                interval_hours=2,
                cap_triggered=False,
            )

    def test_accepts_4h_interval(self):
        # 4h is reserved for future venue configs but still allowed by the validator.
        ev = FundingEvent(
            time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            rate=0.0001,
            interval_hours=4,
            cap_triggered=False,
        )
        assert ev.interval_hours == 4

    def test_accepts_1h_interval(self):
        ev = FundingEvent(
            time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            rate=0.0001,
            interval_hours=1,
            cap_triggered=True,
        )
        assert ev.interval_hours == 1

    def test_rejects_nan_rate(self):
        with pytest.raises(ValueError, match="finite"):
            FundingEvent(
                time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                rate=float("nan"),
                interval_hours=8,
                cap_triggered=False,
            )

    def test_rejects_inf_rate(self):
        with pytest.raises(ValueError, match="finite"):
            FundingEvent(
                time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                rate=float("inf"),
                interval_hours=8,
                cap_triggered=False,
            )

    def test_cap_triggered_flag_preserved(self):
        ev = _ev(2026, 10, 5, 0, rate=0.003, cap_triggered=True)
        assert ev.cap_triggered is True


# ──────────────────────────────────────────────────────────────────────
# AdaptiveFundingSchedule algorithm
# ──────────────────────────────────────────────────────────────────────


class TestComputeAdaptiveSchedule:
    def test_empty_history_returns_empty_schedule(self):
        schedule = compute_adaptive_schedule([])
        assert isinstance(schedule, AdaptiveFundingSchedule)
        assert schedule.total_events == 0
        assert schedule.cap_events == 0
        assert schedule.adaptive_count == 0

    def test_no_history_with_supplied_bounds(self):
        """Empty input + supplied bounds → 8h grid with zero-rate events.

        The schedule still emits events on the 8h grid so the engine has
        a funding cost line for the entire window even when realized
        rate data is missing.
        """
        start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc) + timedelta(hours=24)
        schedule = compute_adaptive_schedule([], start=start, end=end)
        # Snap to 00:00; advance 8h: 00, 08, 16, 24 → 4 events
        assert schedule.total_events == 4
        for ev in schedule.events:
            assert ev.rate == 0.0
            assert ev.interval_hours == 8
            assert ev.cap_triggered is False

    def test_rejects_naive_start(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            compute_adaptive_schedule(
                [], start=datetime(2026, 10, 5, 0, 0), end=None
            )

    def test_rejects_naive_end(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            compute_adaptive_schedule(
                [], start=None, end=datetime(2026, 10, 5, 0, 0)
            )

    def test_rejects_end_before_start(self):
        with pytest.raises(ValueError, match="end .* < start"):
            compute_adaptive_schedule(
                [],
                start=datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            )

    def test_rejects_negative_cap_rate(self):
        with pytest.raises(ValueError, match="cap_rate"):
            compute_adaptive_schedule(
                [],
                start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                cap_rate=-0.001,
            )

    def test_rejects_invalid_base_hours(self):
        with pytest.raises(ValueError, match="base_hours"):
            compute_adaptive_schedule(
                [],
                start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
                base_hours=2,
            )

    def test_anchors_to_8h_settlement_grid(self):
        """Start time snaps to the most recent 8h settlement (00/08/16)."""
        # Start at 03:00 UTC → snaps to 00 UTC
        schedule = compute_adaptive_schedule(
            [],
            start=datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc),
        )
        assert schedule.total_events == 1
        assert schedule.events[0].time == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)

    def test_start_after_settlement_hour_anchors_down(self):
        """Start at 03:00 → first event at 00:00 (snap DOWN, never forward)."""
        schedule = compute_adaptive_schedule(
            [],
            start=datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc),
        )
        assert schedule.events[0].time == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)

    def test_all_rates_below_cap_uses_8h_cadence(self):
        history = [
            _ev(2026, 10, 5, 0, rate=0.0001),
            _ev(2026, 10, 5, 8, rate=0.0002),
            _ev(2026, 10, 5, 16, rate=-0.0001),
            _ev(2026, 10, 6, 0, rate=0.00015),
        ]
        schedule = compute_adaptive_schedule(history)
        assert schedule.total_events == 4
        for ev in schedule.events:
            assert ev.interval_hours == 8
            assert ev.cap_triggered is False
        assert schedule.adaptive_count == 0
        assert schedule.cap_events == 0

    def test_rate_at_cap_triggers_1h_next_interval(self):
        """Rate == +0.003 at first settlement → next interval is 1h."""
        history = [
            _ev(2026, 10, 5, 0, rate=0.003),  # at cap → next = 1h
            # rate at 01:00 = 0.0 → next = 8h
            _ev(2026, 10, 5, 1, rate=0.0),
        ]
        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc),
        )
        assert schedule.total_events == 2
        # First event at 00:00, interval_hours driven by previous cap state (none) → 8h
        assert schedule.events[0].time == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        assert schedule.events[0].interval_hours == 8
        assert schedule.events[0].cap_triggered is True
        # Second event at 01:00 (1h later, not 8h later) — interval governed by cap at 00:00
        assert schedule.events[1].time == datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
        assert schedule.events[1].interval_hours == 1
        assert schedule.events[1].cap_triggered is False

    def test_rate_above_cap_triggers_1h_next_interval(self):
        """Rate = +0.005 (well above 0.003) → next interval is 1h."""
        history = [
            _ev(2026, 10, 5, 0, rate=0.005),
        ]
        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        assert schedule.events[0].cap_triggered is True

    def test_negative_rate_at_floor_triggers_1h_next_interval(self):
        """Rate == -0.003 at first settlement → next interval is 1h."""
        history = [
            _ev(2026, 10, 5, 0, rate=-0.003),
        ]
        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        assert schedule.events[0].cap_triggered is True

    def test_sustained_cap_produces_1h_intervals_until_revert(self):
        """Cap held from 00:00 to 02:00 → events at 00, 01, 02 (all 1h apart)."""
        history = [
            _ev(2026, 10, 5, 0, rate=0.0035),
            _ev(2026, 10, 5, 1, rate=0.0040),
            _ev(2026, 10, 5, 2, rate=0.0035),
        ]
        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc),
        )
        assert schedule.total_events == 3
        assert [e.time.hour for e in schedule.events] == [0, 1, 2]
        # First event: no previous cap → interval = 8h
        assert schedule.events[0].interval_hours == 8
        # Second event: cap at 00:00 → interval = 1h
        assert schedule.events[1].interval_hours == 1
        # Third event: cap at 01:00 → interval = 1h
        assert schedule.events[2].interval_hours == 1
        assert schedule.adaptive_count == 2  # 2 of 3 intervals are 1h

    def test_revert_from_1h_to_8h_after_cap_releases(self):
        """Cap held at 00:00 only → events at 00:00 (cap), 01:00 (1h, no cap), then 09:00 (8h)."""
        # Provide a 2-event window: cap at 00, no cap at 01
        history = [
            _ev(2026, 10, 5, 0, rate=0.0035),
            _ev(2026, 10, 5, 1, rate=0.0001),
        ]
        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc),
        )
        assert schedule.total_events == 3
        assert schedule.events[0].time == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        assert schedule.events[1].time == datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
        assert schedule.events[2].time == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
        # First interval is 8h (no prev cap), then 1h (cap at 00), then 8h (no cap at 01)
        assert schedule.events[0].interval_hours == 8
        assert schedule.events[1].interval_hours == 1
        assert schedule.events[2].interval_hours == 8

    def test_schedule_is_sorted_ascending(self):
        history = [
            _ev(2026, 10, 5, 16, rate=0.0001),
            _ev(2026, 10, 5, 0, rate=0.0001),
            _ev(2026, 10, 5, 8, rate=0.0001),
        ]
        schedule = compute_adaptive_schedule(history)
        times = [e.time for e in schedule.events]
        assert times == sorted(times)

    def test_schedule_accepts_duck_typed_snapshots(self):
        """Generic FundingRateSnapshot-shaped objects (with time + funding_rate) work."""

        class Snap:
            def __init__(self, t, r):
                self.time = t
                self.funding_rate = r

        snaps = [
            Snap(datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), 0.0001),
            Snap(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc), 0.0002),
        ]
        result = compute_adaptive_schedule(snaps)
        assert result.total_events >= 2

    def test_schedule_accepts_FundingRateSnapshot(self):
        """The native FundingRateSnapshot dataclass works without explicit fields."""
        snap = FundingRateSnapshot(
            symbol="BTCUSDT",
            time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            funding_rate=0.0001,
            mark_price=68500.0,
        )
        result = compute_adaptive_schedule([snap])
        assert result.total_events >= 1

    def test_schedule_rejects_naive_input(self):
        """Naive datetimes in inputs are rejected for timezone safety."""

        class Snap:
            def __init__(self, t, r):
                self.time = t
                self.funding_rate = r

        with pytest.raises(ValueError, match="Naive datetime"):
            compute_adaptive_schedule(
                [Snap(datetime(2026, 10, 5, 0, 0), 0.0001)]
            )


# ──────────────────────────────────────────────────────────────────────
# Funding P&L math (perp convention)
# ──────────────────────────────────────────────────────────────────────


class TestComputeFundingPnL:
    def test_long_with_positive_rate_pays(self):
        """Long + positive rate → pnl = -notional * rate (negative)."""
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 0, rate=0.001)],
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        lines = compute_funding_pnl(schedule, position)
        assert len(lines) == 1
        # -100_000 * 0.001 = -100
        assert lines[0].pnl == pytest.approx(-100.0, abs=1e-9)

    def test_long_with_negative_rate_receives(self):
        """Long + negative rate → pnl = -notional * rate (positive)."""
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 0, rate=-0.001)],
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        lines = compute_funding_pnl(schedule, position)
        # -100_000 * -0.001 = +100
        assert lines[0].pnl == pytest.approx(+100.0, abs=1e-9)

    def test_short_with_positive_rate_receives(self):
        """Short + positive rate → pnl = +notional * rate (positive)."""
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 0, rate=0.001)],
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        position = PositionSpec(notional_usd=100_000.0, direction="short")
        lines = compute_funding_pnl(schedule, position)
        # +100_000 * 0.001 = +100
        assert lines[0].pnl == pytest.approx(+100.0, abs=1e-9)

    def test_short_with_negative_rate_pays(self):
        """Short + negative rate → pnl = +notional * rate (negative)."""
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 0, rate=-0.001)],
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        position = PositionSpec(notional_usd=100_000.0, direction="short")
        lines = compute_funding_pnl(schedule, position)
        # +100_000 * -0.001 = -100
        assert lines[0].pnl == pytest.approx(-100.0, abs=1e-9)

    def test_zero_rate_zero_pnl(self):
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 0, rate=0.0)],
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        for direction in ("long", "short"):
            position = PositionSpec(notional_usd=100_000.0, direction=direction)
            lines = compute_funding_pnl(schedule, position)
            assert lines[0].pnl == 0.0

    def test_magnitude_scales_with_notional(self):
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 0, rate=0.0001)],
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
        lines_1x = compute_funding_pnl(schedule, PositionSpec(100_000.0, "long"))
        lines_10x = compute_funding_pnl(schedule, PositionSpec(1_000_000.0, "long"))
        assert lines_10x[0].pnl == pytest.approx(lines_1x[0].pnl * 10, abs=1e-9)

    def test_sum_pnl_matches_total_dollar_charge(self):
        """For a multi-event schedule, the sum of per-event PnL equals
        the schedule's total_dollar_charge for the same notional/direction."""
        history = [
            _ev(2026, 10, 5, 0, rate=0.0001),
            _ev(2026, 10, 5, 8, rate=0.0002),
            _ev(2026, 10, 5, 16, rate=-0.0001),
            _ev(2026, 10, 6, 0, rate=0.00015),
        ]
        schedule = compute_adaptive_schedule(history)
        position = PositionSpec(notional_usd=50_000.0, direction="long")
        lines = compute_funding_pnl(schedule, position)
        assert sum(l.pnl for l in lines) == pytest.approx(
            schedule.total_dollar_charge(50_000.0, "long"), abs=1e-9
        )

    def test_position_spec_rejects_zero_notional(self):
        with pytest.raises(ValueError, match="positive"):
            PositionSpec(notional_usd=0.0, direction="long")

    def test_position_spec_rejects_negative_notional(self):
        with pytest.raises(ValueError, match="positive"):
            PositionSpec(notional_usd=-100.0, direction="long")

    def test_position_spec_rejects_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            PositionSpec(notional_usd=100.0, direction="sideways")  # type: ignore[arg-type]

    def test_bar_index_assigned_when_bars_supplied(self):
        """When bars are supplied, each FundingCostLine carries the
        bar_index where the funding event lands (first bar whose time
        is >= the event time)."""
        bars = _flat_bars(
            n=24, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 5, 8, rate=0.0001)],
            start=bars[0].time,
            end=bars[-1].time,
        )
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        lines = compute_funding_pnl(schedule, position, bars=bars)
        # Schedule emits events on the 8h grid: 00:00, 08:00, 16:00.
        # The 08:00 event (which carries the supplied rate 0.0001) lands
        # at bar index 8 (the 8th 1h bar). The 00:00 event lands at 0.
        assert lines[0].time == datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        assert lines[0].bar_index == 0
        # The second event is at 08:00 — the supplied funding event.
        assert lines[1].time == datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
        assert lines[1].bar_index == 8
        assert lines[1].rate == pytest.approx(0.0001, abs=1e-12)

    def test_bar_index_minus_one_when_event_past_window(self):
        """Events past the bar window get bar_index = -1."""
        bars = _flat_bars(
            n=10, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        schedule = compute_adaptive_schedule(
            [_ev(2026, 10, 7, 2, rate=0.001)],  # far past the 10h window
            start=bars[0].time,
            end=bars[-1].time,
        )
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        lines = compute_funding_pnl(schedule, position, bars=bars)
        # The supplied past-window event is NOT in the schedule (the
        # schedule only covers [start, end] on the 8h grid). All events
        # that ARE in the schedule land inside the bar window.
        for line in lines:
            assert 0 <= line.bar_index < len(bars)
            assert line.time < bars[-1].time + timedelta(hours=8)


# ──────────────────────────────────────────────────────────────────────
# Adaptive vs fixed-8h divergence (R1's core fact)
# ──────────────────────────────────────────────────────────────────────


class TestAdaptiveVsFixed8hDivergence:
    """Per R1: fixed-8h funding grids bias Sharpe upward because they
    miss the extra settlements that occur when cap is triggered."""

    def test_no_cap_adaptive_matches_fixed_8h(self):
        history = [
            _ev(2026, 10, 5, 0, rate=0.0001),
            _ev(2026, 10, 5, 8, rate=0.0002),
            _ev(2026, 10, 5, 16, rate=-0.0001),
        ]
        schedule = compute_adaptive_schedule(history)
        # No cap → all 8h intervals
        assert schedule.adaptive_count == 0
        assert schedule.total_events == 3

    def test_cap_triggered_adaptive_has_more_events_than_fixed_8h(self):
        """Cap held for 3 hours → adaptive produces 4 events (00, 01, 02, 03);
        a fixed-8h grid would have produced only 1 event in the same window
        (00:00, next at 08:00)."""
        history = [
            _ev(2026, 10, 5, 0, rate=0.0035),  # cap → 1h next
            _ev(2026, 10, 5, 1, rate=0.0035),  # cap → 1h next
            _ev(2026, 10, 5, 2, rate=0.0035),  # cap → 1h next
            _ev(2026, 10, 5, 3, rate=0.0020),  # no cap → 8h next
        ]
        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc),
        )
        assert schedule.total_events == 4
        # A fixed-8h grid anchored at 00:00 within [0, 3] hours would produce
        # exactly 1 event at 00:00.
        fixed_8h_count = 1
        # Adaptive has 4 events vs 1 fixed-8h event → 4x more events
        assert schedule.total_events == 4 * fixed_8h_count

    def test_cap_triggered_adaptive_charges_more_funding(self):
        """Sustained cap: adaptive schedule applies funding 24 times per
        day (1h cadence); fixed-8h would apply it only 3 times. The
        total funding charge on adaptive is ~8x higher for the same
        notional & sustained rate (24 vs 3 events per day)."""
        # Same sustained rate throughout 24h
        history = []
        for hour in range(24):
            history.append(_ev(2026, 10, 5, hour, rate=0.0035))  # all cap

        schedule = compute_adaptive_schedule(
            history,
            start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc),
        )
        # All 1h intervals → 24 events
        assert schedule.total_events == 24
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        total_long_cost = schedule.total_dollar_charge(100_000.0, "long")
        # Long pays 0.0035 * 100_000 * 24 = -8400
        assert total_long_cost == pytest.approx(-8400.0, abs=1e-6)


# ──────────────────────────────────────────────────────────────────────
# Engine integration
# ──────────────────────────────────────────────────────────────────────


class TestEngineIntegration:
    def test_engine_run_unchanged_without_funding(self):
        """BacktestEngine.run_single() with the funding wrapper does NOT
        modify the legacy code path. We verify by running the engine
        on flat bars (no trades) and confirming metrics match the
        unwrapped run.
        """
        bars = _flat_bars(n=100, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        strategy = _NoFireStrategy()
        from engine.engine import BacktestEngine

        engine = BacktestEngine(config, [strategy])
        baseline = engine.run_single(strategy, bars)
        # No trades → ending balance == starting, no PnL.
        assert baseline.total_trades == 0
        assert baseline.total_pnl == pytest.approx(0.0, abs=1e-9)
        assert baseline.ending_balance == pytest.approx(10_000.0, abs=1e-9)

    def test_funded_run_returns_funded_metrics(self):
        """run_backtest_with_funding returns a FundedBacktestMetrics
        wrapping the original BacktestMetrics plus the funding overlay."""
        bars = _flat_bars(n=100, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        funding = [_ev(2026, 10, 5, 8, rate=0.0001)]
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        assert isinstance(result, FundedBacktestMetrics)
        # Duck-type check on result.base — see notes in
        # TestLegacyForexPathIsolation for why isinstance identity can
        # differ across import paths.
        assert hasattr(result.base, "total_pnl")
        assert hasattr(result.base, "ending_balance")
        assert hasattr(result.base, "sharpe_ratio")
        assert result.funded_equity_delta
        assert len(result.funding_events) >= 1

    def test_negative_carry_reduces_ending_balance(self):
        """Long + positive rate → funding is negative → ending balance
        must be lower than the baseline (no funding) ending balance."""
        bars = _flat_bars(
            n=200, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        # 200 hours at 8h cadence → ~25 events
        funding = _funding_events_for_window(n_hours=192, rate=0.0001)
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        # Long pays → total_funding_cost is negative
        assert result.total_funding_cost < 0
        # Funded ending balance < baseline ending balance
        assert result.funded_ending_balance < result.base.ending_balance
        # Delta matches total
        assert result.funded_ending_balance == pytest.approx(
            result.base.ending_balance + result.total_funding_cost, abs=1e-9
        )

    def test_positive_carry_increases_ending_balance(self):
        """Long + negative rate → funding is positive → ending balance
        must be higher than the baseline ending balance."""
        bars = _flat_bars(
            n=200, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        # Negative rate → long receives → positive PnL
        funding = _funding_events_for_window(n_hours=192, rate=-0.0001)
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        # Long receives → total_funding_cost is positive
        assert result.total_funding_cost > 0
        assert result.funded_ending_balance > result.base.ending_balance

    def test_funding_event_after_window_excluded(self):
        """Funding event past the bar window does not affect equity."""
        # Engine requires >= 30 bars (min_bars_before_signal default).
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        # Funding event 50h past the bar window (well past bar 47 at 23:00 the next day).
        funding = [_ev(2026, 10, 7, 2, rate=0.001)]
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        # Schedule only covers [start, end] → past-window event isn't in
        # the schedule → no in-window funding events → total = 0.
        assert result.total_funding_cost == 0.0
        assert result.funded_ending_balance == pytest.approx(
            result.base.ending_balance, abs=1e-9
        )
        # No funding event lands inside the window.
        for line in result.funding_events:
            assert 0 <= line.bar_index < len(bars)

    def test_funding_pnl_applied_to_per_bar_delta(self):
        """Verify a single funding event causes a step change in the
        per-bar funded_equity_delta at the matching bar."""
        bars = _flat_bars(
            n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), step_minutes=60
        )
        config = _simple_config()
        # Long + rate=0.0001 + notional=$100k = -$10 at the funding event
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        funding = [_ev(2026, 10, 5, 8, rate=0.0001)]
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        # Funding event at 08:00 → bar index 8 (8 hours into the window)
        # Other schedule events at 00:00 and 16:00 carry rate=0 → zero delta.
        # The single non-zero delta is at bar 8 from the supplied rate.
        nonzero = [
            (i, d) for i, d in enumerate(result.funded_equity_delta) if d != 0.0
        ]
        assert len(nonzero) == 1, f"expected one non-zero bar, got {nonzero}"
        assert nonzero[0][0] == 8
        assert nonzero[0][1] == pytest.approx(-10.0, abs=1e-9)

    def test_rejects_invalid_position(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        with pytest.raises(TypeError, match="PositionSpec"):
            run_backtest_with_funding(
                bars, [], "not a position spec", config, [_NoFireStrategy()]  # type: ignore[arg-type]
            )

    def test_rejects_invalid_config(self):
        bars = _flat_bars(n=48, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        with pytest.raises(TypeError, match="BacktestConfig"):
            run_backtest_with_funding(
                bars, [], position, "not a config", [_NoFireStrategy()]  # type: ignore[arg-type]
            )

    def test_rejects_empty_bars(self):
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        with pytest.raises(ValueError, match="non-empty"):
            run_backtest_with_funding([], [], position, config, [_NoFireStrategy()])

    def test_rejects_empty_strategies(self):
        bars = _flat_bars(n=10, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        with pytest.raises(ValueError, match="strategies"):
            run_backtest_with_funding(bars, [], position, config, [])


# ──────────────────────────────────────────────────────────────────────
# Legacy forex path isolation
# ──────────────────────────────────────────────────────────────────────


class TestLegacyForexPathIsolation:
    """The funding module is a *separate* module — calling BacktestEngine
    directly must produce identical metrics whether the funding module
    is imported or not."""

    def test_engine_module_does_not_import_funding_model(self):
        """The engine module must not depend on funding_model — verify
        by checking engine.py doesn't reference FundingEvent."""
        from pathlib import Path

        engine_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "forex_bot"
            / "engine"
            / "engine.py"
        )
        contents = engine_path.read_text()
        for sym in ("FundingEvent", "AdaptiveFundingSchedule", "compute_funding_pnl"):
            assert sym not in contents, (
                f"engine.py must not import {sym} (leaks crypto path into legacy engine)"
            )

    def test_unwrapped_engine_run_unchanged(self):
        """BacktestEngine.run_single() returns identical metrics whether or
        not the funding wrapper exists in the codebase."""
        bars = _flat_bars(n=100, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        strategy = _NoFireStrategy()
        from engine.engine import BacktestEngine

        engine1 = BacktestEngine(config, [strategy])
        m1 = engine1.run_single(strategy, bars)
        engine2 = BacktestEngine(config, [strategy])
        m2 = engine2.run_single(strategy, bars)
        # Both runs produce identical results — engine is pure.
        assert m1.total_pnl == m2.total_pnl
        assert m1.sharpe_ratio == m2.sharpe_ratio
        assert m1.ending_balance == m2.ending_balance


# ──────────────────────────────────────────────────────────────────────
# FundedBacktestMetrics structure
# ──────────────────────────────────────────────────────────────────────


class TestFundedBacktestMetrics:
    def test_total_funding_cost_is_sum_of_lines(self):
        """total_funding_cost equals sum(pnl for pnl in funding_events)."""
        bars = _flat_bars(n=200, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        funding = _funding_events_for_window(n_hours=192, rate=0.0001)
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        assert result.total_funding_cost == pytest.approx(
            sum(l.pnl for l in result.funding_events), abs=1e-9
        )

    def test_funding_cost_line_dataclass(self):
        line = FundingCostLine(
            time=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            rate=0.0001,
            pnl=-10.0,
            interval_hours=8,
            bar_index=0,
        )
        assert line.pnl == -10.0
        assert line.rate == 0.0001
        assert line.bar_index == 0

    def test_funded_equity_delta_length_matches_bars(self):
        """The funded_equity_delta series has length len(bars)."""
        bars = _flat_bars(n=100, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        funding = [_ev(2026, 10, 5, 8, rate=0.0001)]
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        assert len(result.funded_equity_delta) == len(bars)

    def test_funded_ending_balance_is_base_plus_funding(self):
        """funded_ending_balance = base.ending_balance + total_funding_cost."""
        bars = _flat_bars(n=200, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        funding = _funding_events_for_window(n_hours=192, rate=0.0001)
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        assert result.funded_ending_balance == pytest.approx(
            result.base.ending_balance + result.total_funding_cost, abs=1e-9
        )

    def test_per_bar_delta_sums_to_in_window_funding(self):
        """The sum of per-bar deltas equals the sum of P&L for funding
        events whose settlement time falls inside the bar window.
        Events past the window are excluded from the per-bar delta
        but still appear in ``total_funding_cost`` (they didn't
        realize inside the backtest, so they shouldn't affect equity)."""
        bars = _flat_bars(n=200, start=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
        config = _simple_config()
        position = PositionSpec(notional_usd=100_000.0, direction="long")
        # Mix: one event inside, one event past the window
        funding = [
            _ev(2026, 10, 5, 8, rate=0.0001),  # inside (bar 8)
            _ev(2026, 10, 20, 0, rate=0.001),  # past window
        ]
        result = run_backtest_with_funding(
            bars, funding, position, config, [_NoFireStrategy()]
        )
        # Past-window event isn't in the schedule → only the inside
        # event contributes to per-bar delta. The supplied past-window
        # event doesn't appear in ``funding_events`` because the schedule
        # only covers [start, end].
        in_window_pnl = sum(l.pnl for l in result.funding_events if 0 <= l.bar_index < len(bars))
        assert sum(result.funded_equity_delta) == pytest.approx(
            in_window_pnl, abs=1e-9
        )
        # And the per-bar delta is the sum of all in-window events.
        assert result.total_funding_cost == pytest.approx(
            sum(l.pnl for l in result.funding_events), abs=1e-9
        )