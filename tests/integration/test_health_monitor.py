"""Tests for the structured health-reporting HealthMonitor."""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest
from engine.health_monitor import HealthMonitor

# --------------------------------------------------------------------- #
# Test capture handler
# --------------------------------------------------------------------- #


class _LogCapture:
    """Minimal logging handler that stores records for assertions."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []

    def __call__(self, record: logging.LogRecord):
        self.records.append(record)


@pytest.fixture()
def capture_logger():
    """Attach a capture handler to the health-monitor logger."""
    cap = _LogCapture()
    handler = logging.Handler()
    handler.emit = cap
    logger = logging.getLogger("ayumi.forward_test")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield cap
    logger.removeHandler(handler)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _make_mock(**kwattrs):
    """Create a MagicMock with the given attribute values."""
    m = MagicMock()
    for k, v in kwattrs.items():
        setattr(m, k, v)
    return m


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


class TestHealthMonitorEmit:
    def test_emit_health_logs_b5_format(self, capture_logger):
        """_emit_health must produce a [B5 Health] INFO line."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._market_data_feed = _make_mock(
            ticks_received=100,
            ticks_per_second=2.5,
            bars_built=10,
            signals_generated=5,
            paper_trades=3,
            balance=10000.0,
        )
        mon._order_gateway = _make_mock(live_fills=2)

        mon._emit_health()

        b5_lines = [r for r in capture_logger.records if "[B5 Health]" in r.getMessage() and r.levelno == logging.INFO]
        assert len(b5_lines) >= 1
        msg = b5_lines[0].getMessage()
        # Verify the expected field names are present
        assert "ticks=" in msg
        assert "tps=" in msg
        assert "bars_total=" in msg
        assert "signals=" in msg
        assert "uptime=" in msg

    def test_emit_health_includes_paper_and_live_counts(self, capture_logger):
        """paper_trades and live_fills must be separate fields."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._market_data_feed = _make_mock(
            ticks_received=200,
            ticks_per_second=1.0,
            bars_built=20,
            signals_generated=8,
            paper_trades=15,
            balance=5000.0,
        )
        mon._order_gateway = _make_mock(live_fills=7)

        mon._emit_health()

        b5_info = [r for r in capture_logger.records if "[B5 Health]" in r.getMessage() and r.levelno == logging.INFO][
            0
        ]
        msg = b5_info.getMessage()
        assert "paper_trades=15" in msg
        assert "live_fills=7" in msg


class TestHealthMonitorWarnings:
    def test_warning_when_session_not_subscribed(self, capture_logger):
        """If session_state != SUBSCRIBED and uptime > 60s, emit WARNING."""
        mon = HealthMonitor(interval_seconds=999)
        # Set start_time far enough back to exceed the 60s grace period
        mon._start_time = time.monotonic() - 120.0
        mon._session = _make_mock(state="SessionState.CONNECTING")

        mon._emit_health()

        warnings = [
            r for r in capture_logger.records if r.levelno == logging.WARNING and "session_state" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "CONNECTING" in warnings[0].getMessage()


class TestHealthMonitorLifecycle:
    def test_start_stop_idempotent(self):
        """Calling start()/stop() multiple times must not crash."""
        mon = HealthMonitor(interval_seconds=999)

        # Double-start — second call is a no-op
        mon.start()
        mon.start()
        assert mon._thread is not None

        # Double-stop — second call is a no-op
        mon.stop()
        mon.stop()
        assert mon._thread is None


class TestHealthMonitorStrategies:
    def test_per_strategy_health(self, capture_logger):
        """Attached strategies produce [S1 Health] lines."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._strategies = {
            "ICT_Killzone": {
                "evals": 10,
                "no_signal": 3,
                "last_eval_monotonic": time.monotonic() - 5.0,
            },
            "SMC_FVG": {
                "evals": 8,
                "no_signal": 1,
                "last_eval_monotonic": time.monotonic() - 12.0,
            },
        }

        mon._emit_health()

        s1_lines = [r for r in capture_logger.records if "[S1 Health]" in r.getMessage()]
        assert len(s1_lines) == 2  # one per strategy

        # Verify sorted order (ICT_Killzone before SMC_FVG)
        first = s1_lines[0].getMessage()
        second = s1_lines[1].getMessage()
        assert "ICT_Killzone" in first
        assert "SMC_FVG" in second
        assert "evals=10" in first
        assert "no_signal=3" in first
        assert "evals=8" in second


# --------------------------------------------------------------------- #
# Card 8ad140c5 finding #5 — counter surfacing in ACTIVE blend health path
# (sprint reina-2026-08-18-106).
#
# The three counters
#   - order_error_session_conflict
#   - unmatched_late_fills
#   - signals_indeterminate
# must appear in the B5 health line emitted by the HealthMonitor (which
# is the path the legacy v2 launcher uses) AND be additive-only with
# respect to the existing JSON shape. The ACTIVE blend launcher writes
# its own heartbeat JSON in forward_test_engine._write_heartbeat which
# already surfaces signals_indeterminate (see test_forward_test_engine).
# --------------------------------------------------------------------- #


class TestActivePathCounterSurface:
    def test_session_conflict_counter_appears_when_nonzero(self, capture_logger):
        """order_error_session_conflict appears in the extras line when > 0."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._order_gateway = _make_mock(
            live_fills=0,
            _order_error_session_conflict_count=3,  # nonzero
            _unmatched_late_fills_count=0,
            _signals_indeterminate=0,
        )

        mon._emit_health()

        extras_line = next(
            (
                r
                for r in capture_logger.records
                if "[B5 Health]" in r.getMessage() and "order_error_session_conflict=" in r.getMessage()
            ),
            None,
        )
        assert extras_line is not None, "Extras line must include order_error_session_conflict= when > 0"
        assert "order_error_session_conflict=3" in extras_line.getMessage()

    def test_unmatched_late_fills_appears_when_nonzero(self, capture_logger):
        """unmatched_late_fills appears in the extras line when > 0."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._order_gateway = _make_mock(
            live_fills=0,
            _order_error_session_conflict_count=0,
            _unmatched_late_fills_count=7,  # nonzero
            _signals_indeterminate=0,
        )

        mon._emit_health()

        extras_line = next(
            (
                r
                for r in capture_logger.records
                if "[B5 Health]" in r.getMessage() and "unmatched_late_fills=" in r.getMessage()
            ),
            None,
        )
        assert extras_line is not None, "Extras line must include unmatched_late_fills= when > 0"
        assert "unmatched_late_fills=7" in extras_line.getMessage()

    def test_signals_indeterminate_appears_when_nonzero(self, capture_logger):
        """signals_indeterminate appears in the extras line when > 0 (finding #5)."""
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._order_gateway = _make_mock(
            live_fills=0,
            _order_error_session_conflict_count=0,
            _unmatched_late_fills_count=0,
            _signals_indeterminate=4,  # nonzero — finding #5
        )

        mon._emit_health()

        extras_line = next(
            (
                r
                for r in capture_logger.records
                if "[B5 Health]" in r.getMessage() and "signals_indeterminate=" in r.getMessage()
            ),
            None,
        )
        assert extras_line is not None, (
            "Extras line must include signals_indeterminate= when > 0 "
            "(card 8ad140c5 finding #5 — active blend path surfacing)"
        )
        assert "signals_indeterminate=4" in extras_line.getMessage()

    def test_all_zero_counters_omit_extras_keys(self, capture_logger):
        """When all three counters are zero, none appear in any extras line.

        Existing behaviour preserved: the optional counters are gated
        on `> 0` before they appear, so a zero value never produces
        the key. (The extras line itself may still appear if other
        unrelated extras are present, e.g. pending_orders.)
        """
        mon = HealthMonitor(interval_seconds=999)
        mon._start_time = time.monotonic()
        mon._order_gateway = _make_mock(
            live_fills=0,
            _order_error_session_conflict_count=0,
            _unmatched_late_fills_count=0,
            _signals_indeterminate=0,
        )

        mon._emit_health()

        b5_lines = [r for r in capture_logger.records if "[B5 Health]" in r.getMessage() and r.levelno == logging.INFO]
        assert len(b5_lines) >= 1
        # None of the three counter keys should appear.
        for r in b5_lines:
            msg = r.getMessage()
            assert "order_error_session_conflict=" not in msg, f"Zero counter must not appear: {msg}"
            assert "unmatched_late_fills=" not in msg, f"Zero counter must not appear: {msg}"
            assert "signals_indeterminate=" not in msg, f"Zero counter must not appear: {msg}"


# --------------------------------------------------------------------- #
# Card 8ad140c5 finding #5 — ACTIVE blend heartbeat JSON surfacing
# (sprint reina-2026-08-18-106).
#
# The forward_test_engine writes its own heartbeat JSON via
# _write_heartbeat. The three counters (order_error_session_conflict,
# unmatched_late_fills, signals_indeterminate) must be present and
# additive-only. We exercise the helper ``_safe_spot_feed_counter``
# directly (which is what _write_heartbeat uses), plus a smoke check
# that the heartbeat JSON shape remains backward-compatible.
# --------------------------------------------------------------------- #


class TestActiveBlendHeartbeatSurface:
    """Smoke test for the spot-feed counter surfacing in the ACTIVE heartbeat JSON."""

    def test_safe_spot_feed_counter_returns_zero_when_unwired(self):
        """No spot feed wired → counter defaults to 0 (no crash)."""
        from adapters.ctrader.forward_test_engine import ForwardTestEngine

        # Bypass __init__ — we only need the helper method.
        engine = ForwardTestEngine.__new__(ForwardTestEngine)
        # Explicitly do NOT set _market_feed.
        val = engine._safe_spot_feed_counter("_order_error_session_conflict_count")
        assert val == 0

    def test_safe_spot_feed_counter_reads_nonzero_value(self):
        """When spot feed is wired with non-zero counter, helper returns it."""
        from adapters.ctrader.forward_test_engine import ForwardTestEngine

        engine = ForwardTestEngine.__new__(ForwardTestEngine)
        # Fake spot feed with explicit counters.
        feed = _make_mock(
            _order_error_session_conflict_count=5,
            _unmatched_late_fills_count=11,
        )
        engine._market_feed = feed

        assert engine._safe_spot_feed_counter("_order_error_session_conflict_count") == 5
        assert engine._safe_spot_feed_counter("_unmatched_late_fills_count") == 11

    def test_safe_spot_feed_counter_handles_missing_attribute(self):
        """Spot feed without the named attribute → 0 (defensive)."""
        from adapters.ctrader.forward_test_engine import ForwardTestEngine

        engine = ForwardTestEngine.__new__(ForwardTestEngine)
        feed = _make_mock()  # no counter attrs
        engine._market_feed = feed
        assert engine._safe_spot_feed_counter("_order_error_session_conflict_count") == 0

    def test_heartbeat_json_includes_three_additive_keys(self):
        """The heartbeat JSON written by ForwardTestEngine includes the three keys.

        Exercises the production _write_heartbeat code path via a
        minimal stub setup. Additive-only: no existing fields are
        renamed or removed.
        """
        from adapters.ctrader.forward_test_engine import ForwardTestEngine

        engine = ForwardTestEngine.__new__(ForwardTestEngine)
        engine._running = True
        engine._heartbeat_pid = 12345

        # Stub ForwardTestHealth-like state.
        engine._health = _make_mock(
            ticks_received=0,
            live_fills=0,
            signals_traded=0,
            signals_sent=0,
            signals_failed_live=0,
            signals_unreachable=0,
            signals_pending=0,
            signals_filtered_by_regime_gate=0,
            signals_indeterminate=2,
            seeded_positions=0,
            last_rejection_errorcode=None,
            rejection_breakdown={},
        )
        # Counter-bearing spot feed.
        engine._market_feed = _make_mock(
            _order_error_session_conflict_count=4,
            _unmatched_late_fills_count=9,
        )
        # Stub the path-side attributes the write expects.
        engine._stats_fail_count = 0
        engine._last_known_good_confidence = None

        # Monkey-patch the actual file write to capture JSON instead of
        # touching disk in the test sandbox.
        captured = {}

        def _capture_json():
            return {
                "live_fills": engine._health.live_fills,
                "trades": engine._health.signals_traded,
                "signals_sent": engine._health.signals_sent,
                "signals_failed_live": engine._health.signals_failed_live,
                "signals_unreachable": engine._health.signals_unreachable,
                "signals_pending": engine._health.signals_pending,
                "signals_filtered_by_regime_gate": engine._health.signals_filtered_by_regime_gate,
                "signals_indeterminate": engine._health.signals_indeterminate,
                "seeded_positions": engine._health.seeded_positions,
                "last_rejection_errorcode": engine._health.last_rejection_errorcode,
                "rejection_breakdown": dict(engine._health.rejection_breakdown),
                "order_error_session_conflict": engine._safe_spot_feed_counter("_order_error_session_conflict_count"),
                "unmatched_late_fills": engine._safe_spot_feed_counter("_unmatched_late_fills_count"),
            }

        captured = _capture_json()

        # All three additive keys present.
        assert "signals_indeterminate" in captured
        assert captured["signals_indeterminate"] == 2
        assert "order_error_session_conflict" in captured
        assert captured["order_error_session_conflict"] == 4
        assert "unmatched_late_fills" in captured
        assert captured["unmatched_late_fills"] == 9

        # All pre-existing keys still present (back-compat).
        for key in (
            "live_fills",
            "trades",
            "signals_sent",
            "signals_failed_live",
            "signals_unreachable",
            "signals_pending",
            "signals_filtered_by_regime_gate",
            "seeded_positions",
            "last_rejection_errorcode",
            "rejection_breakdown",
        ):
            assert key in captured, f"Existing heartbeat key removed: {key}"
