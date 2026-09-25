"""End-to-end test: new_order() timeout → INDETERMINATE outcome + late-fill registered.

Card 8ad140c5 finding #4 — sprint reina-2026-08-18-106 (MEDIUM fix).

Background
----------
Before card ce6de98d, a timeout in ``new_order()`` marked the order as
``REJECTED`` and fired ``on_order_rejected``. The engine translated that
into ``signals_failed_live += 1`` — even when the broker had actually
filled the order and the execution event just hadn't arrived yet. 4 of 4
observed production fills were unaccounted (broker +$301.36, health
showed signals=9 trades=0).

Card ce6de98d (B) fixed this by introducing
``_INDETERMINATE_TIMEOUT_REASON``:

  * On ``event.wait(timeout + 5)`` expiry, the order is registered in
    ``_late_fill_registry`` and tagged ``reason = reason = "indeterminate_awaiting_event"``.
  * The order stays at ``status = PENDING`` (non-terminal).
  * Late broker events arriving within the TTL window upgrade the
    status to FILLED (execution event) or REJECTED (real errorCode).
  * No ``on_order_rejected`` is fired for the timeout itself.

This test exercises that full path end-to-end with a mocked reactor and
connection — no live broker. It is the missing regression test for
finding #4 (no e2e timeout→INDETERMINATE test existed).

What this test asserts
----------------------

1. ``new_order()`` returns an Order whose ``status`` is ``PENDING``
   (NOT REJECTED) and whose ``reason`` is
   ``"indeterminate_awaiting_event"`` after the local ``event.wait``
   expires with no broker event having fired the event.

2. The order is registered in ``_late_fill_registry`` under its
   ``request_id`` (so late execution events can still match).

3. No ``on_order_rejected`` callback fires for the timeout itself —
   the order is still in flight.

4. The ``signals_indeterminate`` counter surface point remains intact
   (``ForwardTestEngine._signals_indeterminate`` increment is exercised
   by integration tests; this unit test verifies the spot-feed side).

What this test does NOT cover
-----------------------------

* Live broker integration (mocked here).
* Full integration with ``ForwardTestEngine`` (covered by
  ``test_live_outcome_classify.py``).
* The grace-poll window that catches late broker error events arriving
  during the 500ms window after the timeout (covered by
  ``test_timeout_race_errorcode.py``).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/forex-bot is importable (mirrors sibling tests' pattern).
_SRC = str(Path(__file__).resolve().parents[3] / "src" / "forex-bot")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from adapters.ctrader.models import Order, OrderStatus, OrderType, TradeDirection
from adapters.ctrader.open_api_spot_feed import (
    _INDETERMINATE_TIMEOUT_REASON,
    _LATE_FILL_TTL_SEC,
    _TERMINAL_REJECT_STATUSES,
    OpenApiSpotFeed,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOATradeSide,
)


def _make_minimal_feed() -> OpenApiSpotFeed:
    """Construct an OpenApiSpotFeed with mocked connection for testing.

    Mirrors ``test_timeout_race_errorcode._make_minimal_feed`` but adds
    the late-fill registry + counters required for the
    timeout→INDETERMINATE flow, and the attributes required to call
    ``new_order()`` end-to-end.
    """
    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._kill_switch = None
    feed._permission_policy = None
    feed._ctid_account_id = 123456  # Required by new_order() for the ProtoOA request.
    feed._client_id = "test_client_id"
    feed._client_secret = "test_client_secret"
    feed._access_token = "test_access_token"
    feed._refresh_token = ""
    feed._host = "test.ctraderapi.com"
    feed._port = 5035
    feed._token_mgr = MagicMock()
    feed._token_lifecycle = None
    feed._pending_orders = {}
    feed._pending_client_msg_ids = {}
    feed._late_fill_registry = {}
    feed._unmatched_late_fills_count = 0
    feed._disconnected_pending_orders = []
    feed._callbacks = {
        "on_order_rejected": [],
        "on_order_filled": [],
        "on_order_cancelled": [],
    }
    # Override _trigger_callback to invoke synchronously. The production
    # implementation submits to a ThreadPoolExecutor; tests need to see
    # callbacks fire in the test thread so assertions can observe them.
    feed._trigger_callback = (
        lambda event, *args: [cb(*args) for cb in list(feed._callbacks.get(event, []))]
    )
    feed._callback_executor = MagicMock()
    feed._state_mgr = MagicMock()
    feed._state_mgr.is_operational = True
    feed._conn = MagicMock()
    feed._conn.client = MagicMock()
    feed._symbols = {}
    feed._symbol_digits = {}
    feed._volume_calc = MagicMock()
    feed._volume_calc.volume_to_lots.return_value = 1.0
    feed._volume_calc.lots_to_volume.return_value = 100000
    feed._id_to_name = {1: "EURUSD"}
    feed._name_to_id = {"EURUSD": 1}
    feed._amend_lock = threading.Lock()
    feed._subscribed_symbol_ids = set()
    return feed


def _patch_reactor_inline(monkeypatch_or_patch_target):
    """Patch reactor.callFromThread to run the function synchronously.

    new_order() submits its do_send closure to reactor.callFromThread. In
    tests, we want that closure to run immediately in the calling thread
    so the event.wait(timeout) below can observe the timeout race
    cleanly. Without this patch, the closure would queue to the real
    Twisted reactor (not running under pytest) and new_order() would
    block until pytest's overall test timeout.

    Accepts either a pytest monkeypatch fixture or a unittest.mock.patch
    context manager — passed via a small adapter to keep this test
    self-contained without forcing a pytest-only dependency at the
    module level.
    """
    import adapters.ctrader.open_api_spot_feed as spot_feed_module

    def inline_call_from_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    if hasattr(monkeypatch_or_patch_target, "setattr"):
        # pytest monkeypatch fixture
        monkeypatch_or_patch_target.setattr(
            spot_feed_module.reactor, "callFromThread", inline_call_from_thread
        )
    else:
        # unittest.mock.patch — caller is expected to have entered the
        # context manager already; this function is a no-op in that case.
        pass


class TestNewOrderTimeoutIndeterminate:
    """End-to-end timeout → INDETERMINATE flow for new_order()."""

    def test_new_order_timeout_produces_indeterminate_status(self):
        """Scenario (1): new_order() with no broker response.

        The local ``event.wait(timeout + 5)`` expires without the
        deferred firing. Expected:

          * ``order.status == PENDING`` (NOT REJECTED).
          * ``order.reason == "indeterminate_awaiting_event"``.
          * The order is in ``_late_fill_registry``.
          * No ``on_order_rejected`` callback fired.
        """
        feed = _make_minimal_feed()

        # Capture callbacks synchronously.
        rejected_calls = []
        filled_calls = []
        cancelled_calls = []
        feed._callbacks["on_order_rejected"] = [lambda *a: rejected_calls.append(a)]
        feed._callbacks["on_order_filled"] = [lambda *a: filled_calls.append(a)]
        feed._callbacks["on_order_cancelled"] = [lambda *a: cancelled_calls.append(a)]
        feed._trigger_callback = lambda event, *args: (
            rejected_calls.extend([args] if event == "on_order_rejected" else []),
            filled_calls.extend([args] if event == "on_order_filled" else []),
            cancelled_calls.extend([args] if event == "on_order_cancelled" else []),
        )

        # Patch reactor.callFromThread to run inline so we don't need a
        # real Twisted reactor in tests. The do_send closure in
        # new_order() will execute synchronously.
        with patch(
            "adapters.ctrader.open_api_spot_feed.reactor.callFromThread",
            side_effect=lambda fn, *a, **kw: fn(*a, **kw),
        ):
            # Use a very short timeout so the test runs in <2s.
            # event.wait(timeout + 5) = event.wait(5.5) when timeout=0.5.
            # We patch the deferred's responseTimeoutInSeconds indirectly
            # via the timeout parameter (it propagates to the deferred
            # timeout, but the local wait uses timeout + 5).
            t0 = time.monotonic()
            order = feed.new_order(
                1,
                ProtoOATradeSide.BUY,
                100000,
                timeout=0.5,
            )
            elapsed = time.monotonic() - t0

        # The order should NOT be REJECTED — indeterminate outcome only
        # sets the reason + comment.
        assert order.status == OrderStatus.PENDING, (
            f"Expected PENDING (still-in-flight), got {order.status.value!r} "
            f"(INDETERMINATE means status stays PENDING for late upgrade)"
        )
        # Reason / comment carry the INDETERMINATE marker.
        assert order.reason == _INDETERMINATE_TIMEOUT_REASON, (
            f"Expected reason={_INDETERMINATE_TIMEOUT_REASON!r}, got {order.reason!r}"
        )
        assert order.comment == _INDETERMINATE_TIMEOUT_REASON

        # The order is registered in the late-fill registry under its
        # request_id so late broker events can match.
        assert len(feed._late_fill_registry) == 1, (
            f"Expected 1 entry in _late_fill_registry, got "
            f"{len(feed._late_fill_registry)}"
        )
        registry_entry = next(iter(feed._late_fill_registry.values()))
        # (expiry_monotonic, Order, client_msg_id)
        assert len(registry_entry) == 3, "Late-fill entry must carry (expiry, order, cmsg_id)"
        registry_order, registry_cmsg_id = registry_entry[1], registry_entry[2]
        assert registry_order is order, "Late-fill registry should hold the timed-out order"
        assert registry_cmsg_id, "Late-fill registry entry should carry the client_msg_id"

        # No rejection callback fired — the order is still in flight.
        assert len(rejected_calls) == 0, (
            f"Expected 0 on_order_rejected callbacks (INDETERMINATE is "
            f"not a terminal rejection), got {len(rejected_calls)}: "
            f"{rejected_calls!r}"
        )
        assert len(filled_calls) == 0, "No fill callbacks expected for a pure timeout"
        assert len(cancelled_calls) == 0, "No cancel callbacks expected for a pure timeout"

        # Sanity: the test actually waited for the timeout (i.e. the
        # event.wait was not bypassed). elapsed should be at least
        # timeout + 5 = 5.5s minus a small epsilon. Allow generous slack.
        assert elapsed >= 5.0, (
            f"new_order() returned in {elapsed:.2f}s — expected at least "
            f"5s (timeout + 5 grace). Test may have been bypassed."
        )

    def test_indeterminate_reason_is_distinct_from_terminal_reject(self):
        """Scenario (2): the INDETERMINATE marker must NOT be a terminal reject.

        The ``_TERMINAL_REJECT_STATUSES`` set drives the late-fill
        callback path's classification logic. If
        ``_INDETERMINATE_TIMEOUT_REASON`` were ever (mis-)classified as
        a terminal reject, the live-fill callback would still treat the
        order as failed and double-count the trade. Asserting the
        constant's value is distinct from the OrderStatus enum members
        in ``_TERMINAL_REJECT_STATUSES`` makes the contract explicit.
        """
        # The reason is a free-form string; the terminal-reject set is
        # the OrderStatus enum. They must NOT collide (e.g. via a
        # refactor that maps reason → status and shortcuts terminal
        # classification).
        from adapters.ctrader.protocols import OrderStatus as OS

        # The string reason must not equal any OrderStatus value (which
        # would mean downstream classification mis-routes it).
        for terminal_status in _TERMINAL_REJECT_STATUSES:
            assert _INDETERMINATE_TIMEOUT_REASON != terminal_status.value, (
                f"INDETERMINATE_TIMEOUT_REASON ({_INDETERMINATE_TIMEOUT_REASON!r}) "
                f"must NOT collide with terminal-reject status "
                f"{terminal_status.value!r}"
            )
        # And must not be the FILLED marker either (clearly distinct).
        assert _INDETERMINATE_TIMEOUT_REASON != OS.FILLED.value

    def test_late_fill_registry_ttl_is_configured(self):
        """Scenario (3): late-fill TTL must be > 0 to allow late match.

        The registry must keep entries alive long enough for late broker
        events to match. A TTL of 0 (or negative) would make the
        timeout→INDETERMINATE path useless — late events would always
        fall into the unmatched_late_fills DROP branch.
        """
        assert _LATE_FILL_TTL_SEC > 0, (
            f"_LATE_FILL_TTL_SEC must be > 0 for late-fill matching; "
            f"got {_LATE_FILL_TTL_SEC}"
        )
        # Sanity bound: TTL should be in a sensible operational range
        # (not too short that it never matches, not too long that the
        # registry grows unbounded).
        assert 1.0 <= _LATE_FILL_TTL_SEC <= 600.0, (
            f"_LATE_FILL_TTL_SEC={_LATE_FILL_TTL_SEC} outside expected "
            f"operational range [1.0, 600.0]"
        )

    def test_pending_order_then_late_terminal_event_resolves_cleanly(self):
        """Scenario (4): simulate the full timeout → late-fill upgrade path.

        1. Register a pending order manually.
        2. Simulate the timeout by NOT firing the event AND registering
           the late-fill entry (mirroring what new_order's timeout path
           does).
        3. Send a late terminal execution event (FILLED) for the order.
        4. Verify the late-fill registry is consumed atomically and
           the on_filled callback fires with FILLED (the INDETERMINATE
           upgrade to FILLED).

        Note: the result is delivered via callback (on_filled), NOT via
        ``get_result()`` after consume — because the atomic terminal
        consume pops ``_results`` to prevent a subsequent ORDER_ERROR
        from overwriting a confirmed terminal status (Card 8ad140c5
        HIGH fix).

        This is the late-arrival happy-path counterpart to the pure
        timeout test in scenario (1). Together they prove the timeout
        path is reversible: an order that timed out can still resolve
        cleanly when the broker event arrives within the TTL window.
        """
        from adapters.ctrader.execution_event_handler import ExecutionEventHandler

        filled_calls = []

        def capture_filled(result):
            filled_calls.append(result)

        handler = ExecutionEventHandler(on_filled=capture_filled)
        client_msg_id = "order_test_lateupgrade_001"
        client_order_id = "ctid_trader_test_lateupgrade_001"

        # Step 1: register pending.
        handler.register_pending(client_msg_id, client_order_id)

        # Step 2: simulate the timeout — register late-fill.
        handler.register_late_fill(client_msg_id, client_order_id)

        # Both _pending and _late_fill should now have entries.
        assert client_msg_id in handler._pending
        assert client_msg_id in handler._late_fill
        assert client_order_id in handler._client_order_ids
        assert client_order_id in handler._late_fill_by_coid

        # Step 3: send a late terminal FILLED execution event.
        # Build a minimal mock message with executionType=ORDER_FILLED(3)
        # and an order payload carrying the clientOrderId.
        msg = MagicMock()
        msg.payloadType = 2126
        msg.executionType = 3  # ORDER_FILLED
        order_payload = MagicMock()
        order_payload.clientOrderId = client_order_id
        order_payload.orderId = "ctid_order_42"
        order_payload.executionPrice = 1.25000
        order_payload.executedVolume = 100000
        msg.order = order_payload

        handler.on_execution_event(msg)

        # Step 4a: verify the on_filled callback fired with FILLED status.
        # The result is delivered via callback, NOT get_result() (atomic
        # consume clears _results to prevent ORDER_ERROR overwrite).
        #
        # NOTE: compare against ``.value`` (string) instead of the enum
        # member directly because ``ExecutionEventHandler`` and
        # ``OpenApiSpotFeed`` each import ``OrderStatus`` from a
        # different module (protocols vs models). The two enums have
        # identical string values but are NOT the same Python class, so
        # ``protocols.OrderStatus.FILLED == models.OrderStatus.FILLED``
        # is False. ``.value`` comparison is robust to that.
        assert len(filled_calls) == 1, (
            f"Expected exactly 1 on_filled callback after late terminal "
            f"event, got {len(filled_calls)}"
        )
        result = filled_calls[0]
        assert result.status.value == "filled", (
            f"Expected FILLED status, got {result.status.value!r}"
        )

        # Step 4b: atomic terminal consumption (Card 8ad140c5 #1, HIGH).
        # All five structures must be empty for this order.
        assert client_msg_id not in handler._pending, (
            "_pending should have been atomically consumed on terminal"
        )
        assert client_msg_id not in handler._results, (
            "_results should have been atomically consumed on terminal"
        )
        assert client_order_id not in handler._client_order_ids, (
            "_client_order_ids should have been atomically consumed on terminal"
        )
        assert client_msg_id not in handler._late_fill, (
            "_late_fill should have been atomically consumed on terminal"
        )
        assert client_order_id not in handler._late_fill_by_coid, (
            "_late_fill_by_coid should have been atomically consumed on terminal"
        )

        # Unmatched counter not bumped — this was a clean match, not
        # an expired entry lookup.
        assert handler._unmatched_late_fills_count == 0, (
            f"Expected unmatched_late_fills=0 for clean late-match, "
            f"got {handler._unmatched_late_fills_count}"
        )

    def test_unmatched_late_fills_counter_bumps_on_ttl_expired_lookup(self):
        """Scenario (5): late event for an EXPIRED late-fill entry bumps counter.

        Card 8ad140c5 finding #3 (counter blind path). Before the fix,
        the counter only fired when the lookup was via client_msg_id
        iteration; an expired entry matched only by clientOrderId would
        silently drop without bumping the counter.

        Post-fix: the counter bumps exactly once when the lookup
        observes an expired entry (via either key).

        Scenario setup:
        * A late-fill entry exists (the post-timeout broker event window)
        * Its TTL has been FORCED to expire (now in the past)
        * The pending entry was already cleaned up by the caller
          (``cleanup_pending`` invoked post-resolution of a sibling event,
          which pre-fix left _late_fill entries alive but cleared the
          pending map \u2014 the classic orphan-late-fill scenario).
        * A new broker event arrives for the same clientOrderId. The
          match fails (TTL expired, entry popped by lazy cleanup) and the
          counter bumps.
        """
        from adapters.ctrader.execution_event_handler import ExecutionEventHandler

        handler = ExecutionEventHandler()
        client_msg_id = "order_test_expired_001"
        client_order_id = "ctid_trader_test_expired_001"

        # Step 1: register late-fill only \u2014 NO pending entry. This models
        # the post-cleanup state where cleanup_pending() was invoked on
        # a sibling order, leaving the late-fill entry alive (pre-fix
        # bug pattern: cleanup_pending didn't pop _late_fill).
        handler.register_late_fill(client_msg_id, client_order_id)

        # Step 2: force the late-fill entry to expire by overwriting its
        # expiry with a value in the past. Snapshot the dict first, then
        # release the lock before mutating — ``threading.Lock`` is not
        # re-entrant so we must release before any handler method
        # acquires it again.
        import time as _time

        expired_snapshot = {}
        with handler._lock:
            for cmid, entry in handler._late_fill.items():
                expired_snapshot[cmid] = (_time.monotonic() - 1.0, entry[1])
        with handler._lock:
            for cmid, new_entry in expired_snapshot.items():
                handler._late_fill[cmid] = new_entry

        # Pre-conditions: no pending entry, no _client_order_ids entry
        # (simulating post-cleanup_pending state), but late-fill registry
        # still has the (expired) entry.
        assert client_msg_id not in handler._pending
        assert client_order_id not in handler._client_order_ids
        assert client_msg_id in handler._late_fill
        assert client_order_id in handler._late_fill_by_coid

        # Step 3: send a late FILLED event for the same clientOrderId.
        # _match_by_client_order_id fails (no _client_order_ids entry),
        # _check_late_fill is called and observes the expired entry
        # (the pre-check before lookup saw it in _late_fill_by_coid,
        # but _check_late_fill returns None because it popped the
        # expired entry during lazy cleanup).
        msg = MagicMock()
        msg.payloadType = 2126
        msg.executionType = 3  # ORDER_FILLED
        order_payload = MagicMock()
        order_payload.clientOrderId = client_order_id
        order_payload.orderId = "ctid_order_expired"
        msg.order = order_payload

        handler.on_execution_event(msg)

        # Counter bumped exactly once (the post-fix blind-path coverage).
        assert handler._unmatched_late_fills_count == 1, (
            f"Expected unmatched_late_fills=1 after expired-entry "
            f"lookup, got {handler._unmatched_late_fills_count}"
        )

        # Late-fill registry cleaned up by lazy cleanup of the expired entry.
        assert client_msg_id not in handler._late_fill, (
            "Expired late-fill entry should have been popped by lazy cleanup"
        )
        assert client_order_id not in handler._late_fill_by_coid, (
            "Expired late-fill_by_coid entry should have been popped by "
            "lazy cleanup"
        )
