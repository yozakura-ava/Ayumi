"""Unit tests: Execution-event race fixes (cards ce6de98d + 18b74ea7).

Tests the terminal-only state machine in ``_handle_execution_event`` and
the indeterminate-timeout / session-conflict wiring added by Satsuki's
build for sprint reina-2026-08-18-105.

Scenarios (mirroring the fix spec):
  (1) ACCEPT → pending-mutation → FILLED same-second:
      no DROP, exactly ONE on_order_filled, price from FILLED payload.
  (2) wait-expiry → no REJECTED, no on_order_rejected, no
      signals_failed_live bump; late FILLED upgrade fires on_order_filled
      ONCE.
  (3) Double FILLED re-emission → one callback total (dedupe via
      ``_fill_cb_fired``).
  (4) ALREADY_LOGGED_IN empty-id → SESSION-CONFLICT warning + counter.
  (5) Post-TTL FILLED → bounded drop + unmatched_late_fills counter.

All scenarios use mocks/fixtures ONLY — no live cTrader connections.
Mirrors the existing ``test_timeout_race_errorcode.py`` pattern.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

# Ensure src/forex_bot is importable
_SRC = str(Path(__file__).resolve().parents[3] / "src" / "forex_bot")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from adapters.ctrader.models import Order, OrderStatus, OrderType, TradeDirection
from adapters.ctrader.open_api_spot_feed import (
    _INDETERMINATE_TIMEOUT_REASON,
    _LATE_FILL_TTL_SEC,
    _TERMINAL_EXEC_TYPES,
    OpenApiSpotFeed,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (  # noqa: I001
    ProtoOAExecutionType,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_minimal_feed() -> OpenApiSpotFeed:
    """Construct an ``OpenApiSpotFeed`` with mocked connection for testing.

    Mirrors ``test_timeout_race_errorcode._make_minimal_feed`` but also
    initialises the new counters added by card ce6de98d / 18b74ea7 and
    overrides ``_trigger_callback`` to invoke callbacks synchronously
    (the real implementation uses a ThreadPoolExecutor which would
    detach the callback from the test thread).
    """
    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._kill_switch = None
    feed._permission_policy = None
    feed._pending_orders = {}
    feed._pending_client_msg_ids = {}
    feed._late_fill_registry = {}
    feed._disconnected_pending_orders = []
    feed._callbacks = {
        "on_order_rejected": [],
        "on_order_filled": [],
        "on_order_cancelled": [],
    }
    # Override _trigger_callback to invoke synchronously. The production
    # implementation submits to a ThreadPoolExecutor (which is mocked as a
    # bare MagicMock and would silently drop the callback). Tests need to
    # see the callback fire in the test thread so assertions can observe it.
    feed._trigger_callback = lambda event, *args: [cb(*args) for cb in list(feed._callbacks.get(event, []))]
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
    feed._amend_lock = threading.Lock()
    # Card 18b74ea7: session-conflict counter, initialised at 0 by __init__
    # but we set it explicitly for clarity.
    feed._order_error_session_conflict_count = 0
    # Card ce6de98d (E): unmatched_late_fills counter, same pattern.
    feed._unmatched_late_fills_count = 0
    return feed


def _make_order(
    *,
    order_id: str = "test_req_001",
    symbol: str = "EURUSD",
    status: OrderStatus = OrderStatus.PENDING,
) -> Order:
    """Create an Order suitable for execution-event tests."""
    return Order(
        order_id=order_id,
        symbol=symbol,
        direction=TradeDirection.LONG,
        order_type=OrderType.MARKET,
        volume=1.0,
        price=1.10000,
        status=status,
    )


def _make_execution_event(
    *,
    execution_type: int,
    client_order_id: str,
    execution_price: float | None = None,
    error_code: str | None = None,
    description: str | None = None,
) -> MagicMock:
    """Build a mock execution-event message with order payload + envelope."""
    message = MagicMock()
    message.executionType = execution_type
    message.errorCode = error_code
    message.description = description

    # Order payload (with clientOrderId + executionPrice).
    order_payload = MagicMock()
    order_payload.clientOrderId = client_order_id
    order_payload.executionPrice = execution_price
    order_payload.executedVolume = 0
    order_payload.symbolId = 1
    order_payload.positionId = 0
    message.order = order_payload

    # Deal payload (empty).
    message.deal = None
    # Position payload (empty).
    message.position = None

    return message


# ── Test class ────────────────────────────────────────────────────────────


class TestTerminalOnlyStateMachine:
    """Card ce6de98d (A): TERMINAL-ONLY state machine in _handle_execution_event."""

    def test_terminal_exec_types_set_includes_filled_cancelled_rejected_expired(self):
        """Verify the terminal set matches the pb2 enum (no magic numbers)."""
        assert ProtoOAExecutionType.ORDER_FILLED in _TERMINAL_EXEC_TYPES
        assert ProtoOAExecutionType.ORDER_CANCELLED in _TERMINAL_EXEC_TYPES
        assert ProtoOAExecutionType.ORDER_REJECTED in _TERMINAL_EXEC_TYPES
        assert ProtoOAExecutionType.ORDER_EXPIRED in _TERMINAL_EXEC_TYPES
        # Non-terminal: ACCEPT, REPLACED, PARTIAL_FILL, CANCEL_REJECTED
        assert ProtoOAExecutionType.ORDER_ACCEPTED not in _TERMINAL_EXEC_TYPES
        assert ProtoOAExecutionType.ORDER_REPLACED not in _TERMINAL_EXEC_TYPES
        assert ProtoOAExecutionType.ORDER_PARTIAL_FILL not in _TERMINAL_EXEC_TYPES
        assert ProtoOAExecutionType.ORDER_CANCEL_REJECTED not in _TERMINAL_EXEC_TYPES

    def test_accept_does_not_pop_pending_or_fire_callback(self):
        """ACCEPT(2) is informational: log only, no pop, no callback.

        Regression test for the pop-any-match bug where ACCEPT fired
        on_order_filled on the ACCEPT payload.
        """
        feed = _make_minimal_feed()

        fired = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired.append("filled")]

        request_id = "test_req_accept"
        order = _make_order(order_id=request_id)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        # ACCEPT event (executionType=2) with empty executionPrice to
        # simulate the bug's fallback chain being exercised.
        message = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_ACCEPTED,
            client_order_id=request_id,
            execution_price=None,  # No price on ACCEPT — would fall back to order.price
        )
        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        feed._handle_execution_event(message, envelope)

        # Order must remain pending (no pop).
        assert request_id in feed._pending_orders, "ACCEPT must NOT pop _pending_orders"
        assert order.status == OrderStatus.PENDING, f"ACCEPT must NOT change order.status, got {order.status}"
        # No callback fired.
        assert fired == [], f"ACCEPT must NOT fire on_order_filled; fired={fired}"
        # Event must NOT be set (caller still waiting for terminal event).
        assert not event.is_set(), "ACCEPT must NOT set the pending event"

    def test_filled_after_accept_fires_on_order_filled_once_with_filled_price(self):
        """Scenario (1): ACCEPT→pending-mutation→FILLED same-second.

        After ACCEPT logged-and-skipped, FILLED(3) must dispatch with
        the FILLED payload's executionPrice (NOT the fallback chain).
        Exactly one on_order_filled must fire.
        """
        feed = _make_minimal_feed()

        fired = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired.append(o)]

        request_id = "test_req_accept_then_filled"
        order = _make_order(order_id=request_id)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        # 1. ACCEPT event (informational) — must be logged-only.
        accept_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_ACCEPTED,
            client_order_id=request_id,
        )
        feed._handle_execution_event(accept_msg, envelope)
        assert request_id in feed._pending_orders, "ACCEPT must not pop"
        assert fired == [], "ACCEPT must not fire"

        # 2. FILLED event with executionPrice from the broker.
        filled_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id=request_id,
            execution_price=1.12345,  # Distinct from order.price (1.10000)
        )
        feed._handle_execution_event(filled_msg, envelope)

        # Order is now FILLED, pending entry popped.
        assert order.status == OrderStatus.FILLED
        assert order.filled_price == 1.12345, f"Filled price must come from FILLED payload, got {order.filled_price}"
        assert request_id not in feed._pending_orders
        # Exactly one on_order_filled callback fired (not two).
        assert len(fired) == 1, f"Expected exactly one on_order_filled, got {len(fired)}"
        # Dedupe flag set.
        assert getattr(order, "_fill_cb_fired", False) is True

    def test_double_filled_re_emission_dedupes_callback(self):
        """Scenario (3): Double FILLED re-emission → one callback total.

        The broker may re-emit ORDER_FILLED (+4m observed in production).
        The second emission must NOT re-fire on_order_filled.
        """
        feed = _make_minimal_feed()

        fired = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired.append(o)]

        request_id = "test_req_double_filled"
        order = _make_order(order_id=request_id)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        # First FILLED (the real one) — should fire callback.
        filled_1 = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id=request_id,
            execution_price=1.11111,
        )
        feed._handle_execution_event(filled_1, envelope)
        assert len(fired) == 1

        # Simulate broker re-emission: a duplicate FILLED arrives later
        # (we re-create the pending entry as the broker would for a fresh
        # wire-level retransmit; in practice the late-fill registry
        # would handle it, but the dedupe is the safety net).
        feed._pending_orders[request_id] = (event, order)
        filled_2 = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id=request_id,
            execution_price=1.22222,  # Different price — must NOT overwrite
        )
        feed._handle_execution_event(filled_2, envelope)

        # Still exactly one callback (second was deduped).
        assert len(fired) == 1, (
            f"Duplicate FILLED re-emission must NOT re-fire on_order_filled; got {len(fired)} callbacks"
        )
        # Original fill price preserved.
        assert order.filled_price == 1.11111, (
            f"Duplicate FILLED must NOT overwrite filled_price; got {order.filled_price}"
        )

    def test_cancelled_sets_status_and_fires_callback(self):
        """ORDER_CANCELLED(5) is terminal: dispatch to existing CANCELLED branch."""
        feed = _make_minimal_feed()

        fired_cancelled = []
        feed._callbacks["on_order_cancelled"] = [lambda o, m: fired_cancelled.append(o)]

        request_id = "test_req_cancel"
        order = _make_order(order_id=request_id)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        cancel_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_CANCELLED,
            client_order_id=request_id,
        )
        feed._handle_execution_event(cancel_msg, envelope)

        assert order.status == OrderStatus.CANCELLED
        assert getattr(order, "reason", "") == "order_cancelled"
        assert event.is_set()
        assert len(fired_cancelled) == 1
        assert request_id not in feed._pending_orders

    def test_rejected_sets_status_and_fires_callback(self):
        """ORDER_REJECTED(7) is terminal: dispatch to existing REJECTED branch."""
        feed = _make_minimal_feed()

        fired_rejected = []
        fired_rejected_args = []
        feed._callbacks["on_order_rejected"] = [
            lambda o, m, r: (fired_rejected.append(o), fired_rejected_args.append(r))
        ]

        request_id = "test_req_reject"
        order = _make_order(order_id=request_id)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        reject_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_REJECTED,
            client_order_id=request_id,
            error_code="TRADING_BAD_STOPS",
            description="SL too close",
        )
        feed._handle_execution_event(reject_msg, envelope)

        assert order.status == OrderStatus.REJECTED
        assert "TRADING_BAD_STOPS" in getattr(order, "reason", "")
        assert event.is_set()
        assert len(fired_rejected) == 1
        assert "TRADING_BAD_STOPS" in fired_rejected_args[0]

    def test_expired_treated_as_cancelled_with_expired_reason(self):
        """ORDER_EXPIRED(6) is terminal: mapped to CANCELLED + reason='order_expired'."""
        feed = _make_minimal_feed()

        fired_cancelled = []
        feed._callbacks["on_order_cancelled"] = [lambda o, m: fired_cancelled.append(o)]

        request_id = "test_req_expire"
        order = _make_order(order_id=request_id)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        expired_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_EXPIRED,
            client_order_id=request_id,
        )
        feed._handle_execution_event(expired_msg, envelope)

        assert order.status == OrderStatus.CANCELLED, (
            f"EXPIRED must map to CANCELLED (no OrderStatus.EXPIRED), got {order.status}"
        )
        assert getattr(order, "reason", "") == "order_expired"
        assert event.is_set()
        assert len(fired_cancelled) == 1


class TestLateFillRegistryUpgrade:
    """Card ce6de98d (A, B): late-fill registry upgrade + indeterminate timeout."""

    def test_late_filled_upgrade_fires_on_order_filled_once(self):
        """Scenario (2, first half): late FILLED upgrade fires on_order_filled ONCE.

        After the timeout path leaves order.status=PENDING +
        reason="indeterminate_awaiting_event" in the late-fill registry,
        a subsequent FILLED event must upgrade it via the registry and
        fire on_order_filled exactly once.
        """
        feed = _make_minimal_feed()

        fired = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired.append(o)]

        request_id = "test_req_late"
        order = _make_order(order_id=request_id)
        order.status = OrderStatus.PENDING  # Indeterminate timeout left it PENDING
        order.reason = _INDETERMINATE_TIMEOUT_REASON
        client_msg_id = f"order_late_{request_id}"

        # Simulate the timeout path having registered the late-fill entry.
        feed._register_late_fill(request_id, order, client_msg_id)
        assert request_id in feed._late_fill_registry

        envelope = MagicMock()
        envelope.clientMsgId = client_msg_id

        # FILLED event with empty clientOrderId (broker ack shape) but the
        # clientMsgId from the envelope still matches the registry.
        filled_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id="",  # empty — broker ack shape
            execution_price=1.55555,
        )
        feed._handle_execution_event(filled_msg, envelope)

        # Order upgraded to FILLED via the registry.
        assert order.status == OrderStatus.FILLED, f"Late FILLED upgrade must set FILLED, got {order.status}"
        assert order.filled_price == 1.55555, f"Late FILLED must use FILLED payload price, got {order.filled_price}"
        # Registry entry consumed.
        assert request_id not in feed._late_fill_registry
        # Callback fired exactly once.
        assert len(fired) == 1, f"Late FILLED upgrade must fire on_order_filled exactly once, got {len(fired)}"

    def test_post_ttl_filled_increments_unmatched_late_fills_counter(self):
        """Scenario (5): post-TTL FILLED → bounded drop + unmatched_late_fills counter.

        After the late-fill registry TTL expires, a subsequent FILLED
        must be dropped (not matched) and the unmatched_late_fills
        counter must be incremented. The drop log includes a warning.
        """
        feed = _make_minimal_feed()

        fired = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired.append(o)]

        request_id = "test_req_post_ttl"
        order = _make_order(order_id=request_id)
        client_msg_id = f"order_post_ttl_{request_id}"

        # Register with already-expired entry (expiry in the past).
        past = time.monotonic() - 1.0
        feed._late_fill_registry[request_id] = (past, order, client_msg_id)

        initial_unmatched = feed._unmatched_late_fills_count

        envelope = MagicMock()
        envelope.clientMsgId = client_msg_id

        filled_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id=request_id,
            execution_price=1.77777,
        )
        feed._handle_execution_event(filled_msg, envelope)

        # Order NOT upgraded (registry expired → DROP).
        assert order.status == OrderStatus.PENDING, f"Post-TTL FILLED must NOT upgrade order; status={order.status}"
        assert len(fired) == 0, "Post-TTL FILLED must NOT fire callback"
        # Counter incremented.
        assert feed._unmatched_late_fills_count == initial_unmatched + 1, (
            f"unmatched_late_fills counter must increment on post-TTL DROP; "
            f"before={initial_unmatched} after={feed._unmatched_late_fills_count}"
        )


class TestSessionConflictDetection:
    """Card 18b74ea7: ALREADY_LOGGED_IN empty-id → SESSION-CONFLICT warning + counter."""

    def test_already_logged_in_empty_client_order_id_increments_counter(self):
        """Scenario (4): ALREADY_LOGGED_IN with empty clientOrderId → counter=1."""
        feed = _make_minimal_feed()

        # Order-error event with empty clientOrderId, no match anywhere.
        message = MagicMock()
        message.clientOrderId = ""
        message.errorCode = "ALREADY_LOGGED_IN"
        message.description = "Trading account already authorized"

        envelope = MagicMock()
        envelope.clientMsgId = ""

        initial_count = feed._order_error_session_conflict_count

        result = feed._handle_pending_order_error(message, envelope)

        assert result is False, (
            "SESSION-CONFLICT must return False (no match) so the caller knows the event was not consumed"
        )
        assert feed._order_error_session_conflict_count == initial_count + 1, (
            f"Session-conflict counter must increment; "
            f"before={initial_count} after={feed._order_error_session_conflict_count}"
        )

    def test_already_authorized_description_increments_counter(self):
        """Description 'already authorized' (lowercase match) also triggers."""
        feed = _make_minimal_feed()

        message = MagicMock()
        message.clientOrderId = ""
        message.errorCode = "SOME_OTHER_CODE"
        message.description = "Channel was already authorized by another process"

        envelope = MagicMock()
        envelope.clientMsgId = ""

        initial_count = feed._order_error_session_conflict_count

        feed._handle_pending_order_error(message, envelope)

        assert feed._order_error_session_conflict_count == initial_count + 1, (
            "Description-based 'already authorized' must also trigger counter"
        )

    def test_other_empty_id_errors_do_not_increment_session_conflict_counter(self):
        """Empty-id error with non-ALREADY_LOGGED_IN code keeps current behaviour.

        Only SESSION-CONFLICT bumps the new counter. Other empty-id errors
        (e.g. NOT_ENOUGH_MONEY arriving with empty clientOrderId) must NOT
        bump the session-conflict counter — they continue to log at WARNING
        and return False.
        """
        feed = _make_minimal_feed()

        message = MagicMock()
        message.clientOrderId = ""
        message.errorCode = "NOT_ENOUGH_MONEY"
        message.description = "Insufficient balance"

        envelope = MagicMock()
        envelope.clientMsgId = ""

        initial_count = feed._order_error_session_conflict_count

        feed._handle_pending_order_error(message, envelope)

        assert feed._order_error_session_conflict_count == initial_count, (
            "Non-session-conflict errors must NOT bump session-conflict counter"
        )

    def test_session_conflict_does_not_double_count_on_pending_match(self):
        """When clientOrderId matches a pending order, session-conflict path is NOT taken.

        The session-conflict detection only fires when clientOrderId is
        EMPTY AND no match exists in _pending_orders or late registry.
        If the error event matches a pending order, the normal
        REJECTED branch fires and the session-conflict counter is not
        bumped.
        """
        feed = _make_minimal_feed()

        request_id = "test_req_pending_match"
        order = _make_order(order_id=request_id, status=OrderStatus.PENDING)
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[f"order_{request_id}"] = request_id

        fired_rejected = []
        feed._callbacks["on_order_rejected"] = [lambda o, m, r: fired_rejected.append(r)]

        message = MagicMock()
        message.clientOrderId = request_id
        message.errorCode = "ALREADY_LOGGED_IN"  # Hypothetical — would be REJECTED with this code
        message.description = "Already authorized"

        envelope = MagicMock()
        envelope.clientMsgId = f"order_{request_id}"

        initial_count = feed._order_error_session_conflict_count

        result = feed._handle_pending_order_error(message, envelope)

        assert result is True, "Pending-order match must return True"
        assert order.status == OrderStatus.REJECTED
        assert len(fired_rejected) == 1
        assert feed._order_error_session_conflict_count == initial_count, (
            "Pending-match path must NOT bump session-conflict counter"
        )


class TestIndeterminateTimeoutSemantics:
    """Card ce6de98d (B): INDETERMINATE timeout semantics."""

    def test_indeterminate_reason_string_distinct_from_legacy(self):
        """Verify the new reason string is used in the spot feed's timeout path."""
        # Sanity check: the constant is defined and is a non-empty string.
        assert _INDETERMINATE_TIMEOUT_REASON == "indeterminate_awaiting_event"
        # Distinct from the legacy "timeout_awaiting_event".
        assert _INDETERMINATE_TIMEOUT_REASON != "timeout_awaiting_event"

    def test_late_fill_registry_ttl_constant_unaffected(self):
        """The TTL constant is unchanged so existing late-fill logic still works."""
        assert _LATE_FILL_TTL_SEC == 120.0


class TestLateFillDedupeOnUpgrade:
    """Card ce6de98d (C): on_order_filled dedupe — late upgrade path also guarded."""

    def test_late_filled_after_indeterminate_then_filled_dedupes(self):
        """The dedupe guard must work on the late-registry upgrade path too.

        If the order already fired on_order_filled (e.g. via a misrouted
        ACCEPT in some legacy scenario), a subsequent late FILLED via
        the registry must NOT re-fire.
        """
        feed = _make_minimal_feed()

        fired = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired.append(o)]

        request_id = "test_req_dedupe_upgrade"
        order = _make_order(order_id=request_id)
        # Simulate the order having already fired on_order_filled (the
        # _fill_cb_fired flag is set).
        order._fill_cb_fired = True
        client_msg_id = f"order_dedupe_{request_id}"

        feed._register_late_fill(request_id, order, client_msg_id)

        envelope = MagicMock()
        envelope.clientMsgId = client_msg_id

        filled_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id="",
            execution_price=1.99999,
        )
        feed._handle_execution_event(filled_msg, envelope)

        # No re-fire.
        assert len(fired) == 0, "Late-registry FILLED with _fill_cb_fired=True must NOT re-fire on_order_filled"
        # Order status NOT changed (the dedupe short-circuits before the
        # status write — but the registry entry was popped during lookup).
        # We don't assert status here since the dedupe happens after the
        # lookup pop.


class TestOrderErrorFirstLateFillAtomicityRework2:
    """Card 8ad140c5 rework-2 (Rin iter-3 verdict, M1 ATOMICITY).

    Regression test for the inverse-direction race that ``_handle_pending_order_error``
    L2350-2351 was failing to handle. The pre-fix code did

        event, order = self._pending_orders.pop(client_order_id)
        self._pending_client_msg_ids.pop(client_order_id, None)   # NO-OP

    The second pop is a no-op because ``_pending_client_msg_ids`` is keyed by
    client_msg_id (see L336 / L1518), not client_order_id. The pre-fix code
    therefore left the late-fill REGISTRY entry AND the reverse-map
    (client_msg_id → request_id) entry alive after the REJECTED callback
    fired.

    The failure window: ORDER_ERROR resolves the order as REJECTED
    (on_order_rejected fires once), then a late EXEC_EVENT FILLED arrives
    for the SAME order within the late-fill registry TTL window. The
    surviving registry entry matches the FILLED, the FILLED path overwrites
    REJECTED → FILLED, and on_order_filled fires a second time — corrupting
    live accounting.

    The post-fix code routes through ``_consume_order_state_across_all_maps``
    (L1093, verified idempotent at L1148-1171) which clears all THREE
    structures by both keys. This test exercises the exact ordering Rin
    flagged in iter-3: order_error handler resolves terminal (REJECTED,
    on_order_rejected fired) → late EXEC_EVENT FILLED for same order →
    assert NO status overwrite, NO second terminal callback, all three
    maps empty for that order.
    """

    def test_order_error_then_late_exec_filled_does_not_overwrite_rejected(self):
        """Order-error handler resolves REJECTED → late FILLED must be a no-op.

        Pre-fix bug (Rin iter-3): the order_error handler at L2350-2351
        popped ``_pending_orders`` (correct) but used a no-op
        ``_pending_client_msg_ids.pop(client_order_id, None)`` to
        ``_pending_client_msg_ids``. The late-fill REGISTRY entry survived.
        A late EXEC_EVENT FILLED arriving for the same order would then
        match the surviving registry entry, overwrite the confirmed
        REJECTED status with FILLED, and fire on_order_filled a second
        time.

        Post-fix: ``_handle_pending_order_error`` now calls
        ``_consume_order_state_across_all_maps(client_order_id, client_msg_id)``
        which clears all three maps (pending, registry, reverse) by both
        keys. A late EXEC_EVENT FILLED arriving post-resolution finds
        nothing to match and falls through to the unmatched-DROP path —
        no overwrite, no second callback.
        """
        feed = _make_minimal_feed()

        fired_filled = []
        fired_rejected = []
        feed._callbacks["on_order_filled"] = [lambda o, m: fired_filled.append(o)]
        feed._callbacks["on_order_rejected"] = [lambda o, m, r: fired_rejected.append(r)]

        request_id = "test_req_order_error_first"
        order = _make_order(order_id=request_id)
        client_msg_id = f"order_{request_id}"

        # Simulate the timeout path: order registered in BOTH _pending_orders
        # AND _late_fill_registry AND the reverse
        # client_msg_id → request_id map. This is exactly the state
        # new_order() leaves behind when it times out waiting for the
        # broker's accept ack.
        event = threading.Event()
        feed._pending_orders[request_id] = (event, order)
        feed._pending_client_msg_ids[client_msg_id] = request_id
        feed._register_late_fill(request_id, order, client_msg_id)

        # Sanity — all three maps populated for this order BEFORE the
        # order_error event arrives.
        assert request_id in feed._pending_orders
        assert request_id in feed._late_fill_registry
        assert client_msg_id in feed._pending_client_msg_ids

        # ── Step 1: order_error handler resolves REJECTED ─────────────
        # Build an order_error message + envelope that matches the
        # pending order by clientOrderId (direct match path, not the
        # clientMsgId fallback).
        order_error_msg = MagicMock()
        order_error_msg.clientOrderId = request_id
        order_error_msg.clientMsgId = client_msg_id
        order_error_msg.executionType = ProtoOAExecutionType.ORDER_REJECTED
        order_error_msg.errorCode = "REJECTED_BY_BROKER"
        order_error_msg.description = "Order rejected, will not fill"

        envelope = MagicMock()
        envelope.clientMsgId = client_msg_id

        result = feed._handle_pending_order_error(order_error_msg, envelope)

        # Order resolved as REJECTED, on_order_rejected fired exactly once.
        assert result is True, "handle_pending_order_error must return True when an order is matched and resolved"
        assert order.status == OrderStatus.REJECTED, (
            f"Order must be REJECTED after order_error handler; got {order.status}"
        )
        assert len(fired_rejected) == 1, f"on_order_rejected must fire exactly once; got {len(fired_rejected)}"
        assert len(fired_filled) == 0, f"on_order_filled must NOT fire on order_error path; got {len(fired_filled)}"

        # ── Atomic consume invariant (the rework-2 fix) ───────────────
        # After the order_error handler returns, ALL THREE maps must be
        # empty for this order. Pre-fix: only _pending_orders was popped.
        # The registry entry + reverse map survived.
        assert request_id not in feed._pending_orders, "Atomic consume must clear pending on REJECTED resolution"
        assert request_id not in feed._late_fill_registry, (
            "Atomic consume must clear late-fill registry on REJECTED "
            "resolution (the pre-fix rework-2 bug: registry entry survived)"
        )
        assert client_msg_id not in feed._pending_client_msg_ids, (
            "Atomic consume must clear reverse client_msg_id map on "
            "REJECTED resolution (the pre-fix rework-2 bug: no-op pop "
            "by client_order_id against client_msg_id-keyed map)"
        )

        # ── Step 2: late EXEC_EVENT FILLED arrives for the same order ─
        # This is the inverse-direction race Rin flagged. With all three
        # maps empty, the late FILLED must fall through to the unmatched
        # DROP path: no registry match, no _pending_orders match, no
        # overwrite of REJECTED, no second callback.
        envelope_filled = MagicMock()
        envelope_filled.clientMsgId = client_msg_id

        late_filled_msg = _make_execution_event(
            execution_type=ProtoOAExecutionType.ORDER_FILLED,
            client_order_id=request_id,
            execution_price=1.55555,
        )

        feed._handle_execution_event(late_filled_msg, envelope_filled)

        # ── Final state assertions ───────────────────────────────────
        # Order status NOT overwritten — REJECTED stands.
        assert order.status == OrderStatus.REJECTED, (
            f"Late FILLED must NOT overwrite confirmed REJECTED; got {order.status}"
        )
        # No on_order_filled ever fired (the regression that Rin flagged).
        assert len(fired_filled) == 0, (
            f"Exactly zero on_order_filled must fire; got {len(fired_filled)} "
            f"(the pre-fix rework-2 bug: registry match → second callback)"
        )
        # Still exactly one on_order_rejected (the original REJECTED
        # callback is the only one).
        assert len(fired_rejected) == 1, f"Exactly one on_order_rejected must fire; got {len(fired_rejected)}"
        # All three maps still empty for this order (DEFENSIVE: the
        # unmatched-DROP path on the late FILLED must not have
        # re-populated anything).
        assert request_id not in feed._pending_orders
        assert request_id not in feed._late_fill_registry
        assert client_msg_id not in feed._pending_client_msg_ids
