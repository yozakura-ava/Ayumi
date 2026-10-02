"""Unit tests: Pre-emptive reconnect guard — prevents reconnect during in-flight orders.

Tests the root cause fix for card d88336dc:
- The pre-emptive reconnect monitor (_PRE_EMPTIVE_RECONNECT_SEC) must NOT
  trigger a reconnect while there are pending orders in _pending_orders.
- The original 20s threshold caused a race: during new_order()'s 35s
  event.wait(), heartbeat silence could reach 20s, triggering reconnect,
  tearing down the connection, and causing the order response to be lost
  → timeout_awaiting_event for every order.

AC4 from card d88336dc: Test that reproduces the timeout condition and
validates the fix.
"""

import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/forex_bot is importable
_SRC = str(Path(__file__).resolve().parents[3] / "src" / "forex_bot")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from adapters.ctrader.models import Order, OrderStatus, OrderType, TradeDirection
from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed


def _make_minimal_feed():
    """Create an OpenApiSpotFeed with mocked connection for testing."""
    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._kill_switch = None
    feed._permission_policy = None
    feed._pending_orders = {}
    feed._pending_client_msg_ids = {}
    feed._callbacks = {
        "on_order_rejected": [],
        "on_order_filled": [],
        "on_order_cancelled": [],
    }
    feed._state_mgr = MagicMock()
    feed._state_mgr.is_authenticated = True
    feed._conn = MagicMock()
    feed._conn._last_heartbeat_recv = time.monotonic() - 40  # stale heartbeat
    feed._conn._reconnect_count = 0
    feed._symbols = {}
    feed._symbol_digits = {}
    feed._volume_calc = MagicMock()
    feed._id_to_name = {1: "EURUSD"}
    feed._reauth_in_progress = threading.Event()
    feed._preemptive_in_progress = threading.Event()
    feed._preemptive_stop = threading.Event()
    feed._preemptive_thread = None
    feed._running = True
    return feed


class TestPreemptiveReconnectGuard:
    """Verify the pre-emptive reconnect monitor respects pending orders."""

    def test_reconnect_skipped_when_orders_pending(self):
        """AC4: Reconnect must NOT fire when _pending_orders is non-empty.

        This is the exact race condition that caused 172 timeout_awaiting_event
        errors and 0 live fills (card d88336dc). The fix adds a guard in
        _check_preemptive_reconnect() that returns early if there are
        in-flight orders.
        """
        feed = _make_minimal_feed()

        # Simulate a pending order (as new_order() would create)
        request_id = uuid.uuid4().hex
        order = Order(
            order_id=request_id,
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            order_type=OrderType.MARKET,
            volume=1.0,
            status=OrderStatus.PENDING,
        )
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)

        # Heartbeat is 40s stale — well past the 30s threshold
        # The reconnect SHOULD be skipped because orders are pending
        trigger_mock = MagicMock()
        feed._trigger_preemptive_reconnect = trigger_mock

        # Also patch is_forex_market_closed to return False (market open)
        with patch(
            "adapters.ctrader.open_api_spot_feed.is_forex_market_closed",
            return_value=False,
        ):
            feed._check_preemptive_reconnect()

        # Verify reconnect was NOT triggered
        (
            trigger_mock.assert_not_called(),
            (
                "Pre-emptive reconnect fired while orders are pending — "
                "this is the root cause of timeout_awaiting_event (card d88336dc)"
            ),
        )

    def test_reconnect_fires_when_no_orders_pending(self):
        """Reconnect SHOULD fire when no orders are pending and heartbeat is stale."""
        feed = _make_minimal_feed()

        # No pending orders
        assert len(feed._pending_orders) == 0

        trigger_mock = MagicMock()
        feed._trigger_preemptive_reconnect = trigger_mock

        with patch(
            "adapters.ctrader.open_api_spot_feed.is_forex_market_closed",
            return_value=False,
        ):
            feed._check_preemptive_reconnect()

        # Reconnect SHOULD fire — no orders to disrupt
        trigger_mock.assert_called_once()
        call_args = trigger_mock.call_args
        assert call_args[0][0] >= 30.0, f"Expected silence >= 30s, got {call_args[0][0]}"

    def test_reconnect_skipped_when_reauth_in_progress(self):
        """Reconnect must not fire if re-auth is already in progress."""
        feed = _make_minimal_feed()
        feed._reauth_in_progress.set()

        trigger_mock = MagicMock()
        feed._trigger_preemptive_reconnect = trigger_mock

        with patch(
            "adapters.ctrader.open_api_spot_feed.is_forex_market_closed",
            return_value=False,
        ):
            feed._check_preemptive_reconnect()

        trigger_mock.assert_not_called()

    def test_reconnect_skipped_when_not_authenticated(self):
        """Reconnect must not fire if connection is not authenticated."""
        feed = _make_minimal_feed()
        feed._state_mgr.is_authenticated = False

        trigger_mock = MagicMock()
        feed._trigger_preemptive_reconnect = trigger_mock

        with patch(
            "adapters.ctrader.open_api_spot_feed.is_forex_market_closed",
            return_value=False,
        ):
            feed._check_preemptive_reconnect()

        trigger_mock.assert_not_called()

    def test_threshold_is_30_seconds(self):
        """Verify the pre-emptive reconnect threshold is 30s, not 20s.

        The original 20s threshold was too aggressive — it triggered during
        normal order processing where heartbeat gaps of 20s are common.
        30s gives orders enough time to complete (35s event.wait) while
        still catching genuinely dead connections before the 35s degraded
        window.
        """
        from adapters.ctrader.open_api_spot_feed import _PRE_EMPTIVE_RECONNECT_SEC

        assert _PRE_EMPTIVE_RECONNECT_SEC == 30.0, (
            f"Expected threshold 30.0s, got {_PRE_EMPTIVE_RECONNECT_SEC}. "
            f"The 20s threshold caused the timeout race (card d88336dc)."
        )

    def test_multiple_pending_orders_all_block_reconnect(self):
        """Multiple in-flight orders should all block reconnect."""
        feed = _make_minimal_feed()

        # Add 3 pending orders
        for i in range(3):
            request_id = f"test_req_{i}"
            order = Order(
                order_id=request_id,
                symbol="EURUSD",
                direction=TradeDirection.LONG,
                order_type=OrderType.MARKET,
                volume=1.0,
                status=OrderStatus.PENDING,
            )
            event = threading.Event()
            feed._pending_orders[request_id] = (event, order)

        trigger_mock = MagicMock()
        feed._trigger_preemptive_reconnect = trigger_mock

        with patch(
            "adapters.ctrader.open_api_spot_feed.is_forex_market_closed",
            return_value=False,
        ):
            feed._check_preemptive_reconnect()

        trigger_mock.assert_not_called()
