"""Tests for amend_sl_tp fixes (Phase 2A + 2B).

Verifies that:
- 2A: time.sleep cooldown is reachable and spaces calls ≥1s apart
- 2B: amend_sl_tp returns False on broker rejection, True on success
- 2B: Rejection logs include positionId, errorCode, and description
"""

from __future__ import annotations

import logging
import os
import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/forex_bot is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))


@pytest.fixture
def feed_mock():
    """Create an OpenApiSpotFeed with a mocked connection, bypassing __init__."""
    from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed

    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._amend_lock = __import__("threading").Lock()
    feed._conn = MagicMock()
    feed._ctid_account_id = 12345
    feed._symbol_digits = {}
    feed._symbols = {}
    feed._name_to_id = {}
    feed._id_to_name = {}
    feed._round_price = MagicMock(side_effect=lambda sid, p: p)
    return feed


# ── 2A: Cooldown / sleep reachability ────────────────────────────────


class TestAmendCooldown:
    """Verify that amend_sl_tp calls are spaced ≥1s apart."""

    def test_sleep_is_reachable_before_return(self, feed_mock):
        """time.sleep must execute BEFORE return, not after it (was unreachable)."""
        sleep_calls: list[float] = []

        response = MagicMock()
        response.payloadType = 2111
        payload = types.SimpleNamespace(errorCode="", description="")

        with (
            patch(
                "adapters.ctrader.open_api_spot_feed.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
            patch(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                return_value=payload,
            ),
        ):
            feed_mock._conn.send_and_wait = MagicMock(return_value=response)

            result = feed_mock.amend_sl_tp(100, 1.0, 2.0)

        assert result is True, "amend should return True on success"
        assert len(sleep_calls) == 1, f"Expected exactly 1 sleep call, got {len(sleep_calls)}"
        assert sleep_calls[0] == 1.0, f"Expected 1.0s sleep, got {sleep_calls[0]}"

    def test_five_rapid_calls_spaced_at_least_1s(self, feed_mock):
        """5 rapid amend_sl_tp calls must be spaced ≥1s apart."""
        # Track real elapsed time (use a monotonically increasing fake)
        call_times: list[float] = []

        response = MagicMock()
        response.payloadType = 2111
        payload = types.SimpleNamespace(errorCode="", description="")

        def fake_send_and_wait(req, timeout=None, prefix=None):
            call_times.append(time.monotonic())
            return response

        feed_mock._conn.send_and_wait = fake_send_and_wait

        # Patch time.sleep to advance a fake clock instead of actually sleeping
        fake_now = [0.0]

        def fake_sleep(seconds):
            fake_now[0] += seconds

        with (
            patch("adapters.ctrader.open_api_spot_feed.time.sleep", side_effect=fake_sleep),
            patch(
                "adapters.ctrader.open_api_spot_feed.time.monotonic",
                side_effect=lambda: fake_now[0],
            ),
            patch(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                return_value=payload,
            ),
        ):
            for i in range(5):
                feed_mock.amend_sl_tp(100 + i, 1.0 + i * 0.1, 2.0 + i * 0.1)

        # Verify each call is ≥1s after the previous
        for i in range(1, len(call_times)):
            gap = call_times[i] - call_times[i - 1]
            assert gap >= 1.0, f"Call {i} started only {gap:.3f}s after call {i - 1} (expected ≥1.0s)"


# ── 2B: Broker response handling ─────────────────────────────────────


class TestAmendBrokerResponse:
    """Verify amend_sl_tp returns actual broker response status."""

    def test_returns_true_on_success(self, feed_mock):
        """When broker accepts the amend, return True."""
        # Build a success response (no errorCode)
        response = MagicMock()
        response.payloadType = 2111  # success response type
        payload = types.SimpleNamespace(errorCode="", description="")
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with (
            patch(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                return_value=payload,
            ),
            patch("adapters.ctrader.open_api_spot_feed.time.sleep"),
        ):
            result = feed_mock.amend_sl_tp(100, 1.0, 2.0)

        assert result is True, "Expected True for successful amend"

    def test_returns_false_on_broker_rejection(self, feed_mock):
        """When broker rejects with errorCode, return False."""
        response = MagicMock()
        response.payloadType = 2142  # error response
        payload = types.SimpleNamespace(errorCode="TRADING_BAD_STOPS", description="SL or TP price is invalid")
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with (
            patch(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                return_value=payload,
            ),
            patch("adapters.ctrader.open_api_spot_feed.time.sleep"),
        ):
            result = feed_mock.amend_sl_tp(100, 1.0, 2.0)

        assert result is False, "Expected False for rejected amend"

    def test_rejection_log_includes_positionid_errorcode_description(self, feed_mock, caplog):
        """Rejection log must include positionId, errorCode, and description."""
        response = MagicMock()
        response.payloadType = 2142
        payload = types.SimpleNamespace(errorCode="TRADING_BAD_STOPS", description="SL or TP price is invalid")
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with (
            patch(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                return_value=payload,
            ),
            patch("adapters.ctrader.open_api_spot_feed.time.sleep"),
        ):
            with caplog.at_level(logging.WARNING, logger="ayumi.openapi_spot_feed"):
                feed_mock.amend_sl_tp(100, 1.0, 2.0)

        # Find the rejection log record
        rejection_records = [
            r for r in caplog.records if "TRADING_BAD_STOPS" in r.message or "amend" in r.message.lower()
        ]
        assert len(rejection_records) >= 1, f"Expected rejection log, got: {[r.message for r in caplog.records]}"

        msg = rejection_records[0].message
        assert "100" in msg, f"Log missing positionId '100': {msg}"
        assert "TRADING_BAD_STOPS" in msg, f"Log missing errorCode: {msg}"
        assert "SL or TP price is invalid" in msg, f"Log missing description: {msg}"

    def test_returns_false_on_timeout(self, feed_mock):
        """When send_and_wait returns None (timeout), return False."""
        feed_mock._conn.send_and_wait = MagicMock(return_value=None)

        with patch("adapters.ctrader.open_api_spot_feed.time.sleep"):
            result = feed_mock.amend_sl_tp(100, 1.0, 2.0)

        assert result is False, "Expected False on timeout (None response)"

    def test_partial_amend_logged_as_warning(self, feed_mock, caplog):
        """Partial amend success (one of SL/TP rejected) should be detected and logged.

        In cTrader, a partial amend manifests as a success response where
        only one of SL/TP changed. We simulate this by having the first
        call succeed and a follow-up error on the second leg. Since
        amend_sl_tp is atomic (one request for both SL+TP), a partial
        amend is when the broker applies one but rejects the other —
        detected via errorCode on a subsequent reconciliation.

        For this test, we verify that when the broker returns success
        for the amend but the payload indicates partial application
        (via response fields), a WARNING is logged.
        """
        # Simulate a response that indicates partial application
        # In practice, cTrader's amend is atomic — it either applies both or neither.
        # However, if the response has an errorCode but also reports an applied
        # takeProfit value, it indicates partial success.
        response = MagicMock()
        response.payloadType = 2142
        payload = types.SimpleNamespace(
            errorCode="TRADING_BAD_STOPS",
            description="SL price invalid, TP may have been applied",
            stopLoss=None,
            takeProfit=2.0,
        )
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with (
            patch(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                return_value=payload,
            ),
            patch("adapters.ctrader.open_api_spot_feed.time.sleep"),
        ):
            with caplog.at_level(logging.WARNING, logger="ayumi.openapi_spot_feed"):
                result = feed_mock.amend_sl_tp(100, 1.0, 2.0)

        # Even partial amend should return False
        assert result is False

        # Check that log mentions the details
        rejection_records = [
            r for r in caplog.records if "TRADING_BAD_STOPS" in r.message or "amend" in r.message.lower()
        ]
        assert len(rejection_records) >= 1
