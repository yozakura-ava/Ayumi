"""Execution event handler — routes cTrader protobuf messages to handlers.

Replaces the _on_message() switch in open_api_spot_feed.py.
Routes by payload type. Maintains pending-order correlation map.

Payload type → handler mapping:
    2126, 2151  →  on_execution_event   (order filled / cancelled / rejected)
    2132        →  on_order_error        (order error event)
    2142        →  on_general_error      (general error response)

Reference: BQ-1043 Phase 3a, §4.6 of the infra rebuild spec.
Amendment A5: SignalStatsRecorder wiring for outcome tracking.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .protocols import OrderResult, OrderStatus

logger = logging.getLogger("ayumi.execution")

# ── Payload type constants (mirror open_api_spot_feed.py) ────────────────────

_EXECUTION_EVENT_PAYLOAD_TYPES = {2126, 2151}
_ORDER_ERROR_PAYLOAD_TYPE = 2132
_GENERAL_ERROR_PAYLOAD_TYPE = 2142

# ── ProtoOAExecutionType enum values (from OpenApiModelMessages_pb2) ─────────
# We use integers directly to avoid importing the protobuf module (which
# requires the SDK at runtime and complicates unit tests).  The values are
# stable as they are wire-format enums defined by cTrader.
_EXEC_TYPE_FILLED = 3  # ORDER_FILLED
_EXEC_TYPE_PARTIAL_FILL = 11  # ORDER_PARTIAL_FILL
_EXEC_TYPE_CANCELLED = 5  # ORDER_CANCELLED
_EXEC_TYPE_REJECTED = 7  # ORDER_REJECTED
_EXEC_TYPE_EXPIRED = 6  # ORDER_EXPIRED


class ExecutionEventHandler:
    """Routes execution events and correlates order responses.

    Maintains a pending-orders map keyed by ``client_msg_id``.  Each entry
    has a ``threading.Event`` + ``OrderResult`` holder.  When a response
    arrives (success OR error), the event fires and the holder is populated.

    This is the ONLY module that touches the pending-orders correlation map.
    ``OrderGateway`` registers entries; ``ExecutionEventHandler`` resolves
    them.

    A secondary map (``_client_order_ids``) correlates the SDK-generated
    ``clientOrderId`` back to the ``clientMsgId`` so that execution events
    (which carry ``clientOrderId``) can resolve entries registered under
    ``clientMsgId``.
    """

    def __init__(
        self,
        stats_recorder: Any = None,
        on_filled: Callable[[OrderResult], None] | None = None,
        on_rejected: Callable[[OrderResult], None] | None = None,
    ):
        """Initialise the handler.

        Args:
            stats_recorder: Optional ``SignalStatsRecorder`` for outcome
                tracking (Amendment A5).  When provided, ``record_outcome``
                is called on fills.
            on_filled: Optional callback invoked after a successful fill.
            on_rejected: Optional callback invoked after a rejection.
        """
        self._pending: dict[str, threading.Event] = {}
        self._results: dict[str, OrderResult | None] = {}
        self._client_order_ids: dict[str, str] = {}  # clientOrderId → client_msg_id
        self._lock = threading.Lock()
        self._stats = stats_recorder
        self._on_filled = on_filled
        self._on_rejected = on_rejected

        # Late-fill registry: orders that timed out or disconnected but may
        # still receive broker events. Maps client_msg_id → (expiry, result).
        # Mirrors the pattern in OpenApiSpotFeed._late_fill_registry.
        self._late_fill: dict[str, tuple[float, OrderResult | None]] = {}
        self._late_fill_by_coid: dict[str, str] = {}  # clientOrderId → client_msg_id
        self._late_fill_ttl: float = 120.0  # seconds

        # Card 8ad140c5 finding #3 — sprint reina-2026-08-18-106:
        # Counter bumped exactly once when an execution event arrives after
        # the late-fill registry TTL window has expired (broker event
        # arrived too late to be matched). Surfaced via health_monitor's
        # _safe_attr lookup against this handler. Mirrors
        # OpenApiSpotFeed._unmatched_late_fills_count so the B5 health line
        # shows a single consistent rate across both surfaces.
        self._unmatched_late_fills_count: int = 0

    # ── Registration / lookup API (called by OrderGateway) ───────────────

    def register_pending(
        self,
        client_msg_id: str,
        client_order_id: str | None = None,
    ) -> threading.Event:
        """Register a pending order before sending.

        Args:
            client_msg_id: The ``clientMsgId`` used when sending the order.
            client_order_id: Optional ``clientOrderId`` if known upfront.

        Returns:
            ``threading.Event`` that will be set when the response arrives.
        """
        event = threading.Event()
        with self._lock:
            self._pending[client_msg_id] = event
            self._results[client_msg_id] = None
            if client_order_id:
                self._client_order_ids[client_order_id] = client_msg_id
        return event

    def get_result(self, client_msg_id: str) -> OrderResult | None:
        """Return the resolved result for ``client_msg_id``, or ``None``."""
        with self._lock:
            return self._results.get(client_msg_id)

    def cleanup_pending(self, client_msg_id: str) -> None:
        """ATOMIC: remove every piece of state for this order.

        Card 8ad140c5 finding #1 — sprint reina-2026-08-18-106 (HIGH fix).

        Background
        ----------
        The timeout path registers an order in BOTH ``_pending`` (kept
        under ``client_msg_id``) AND ``_late_fill`` (kept under
        ``client_msg_id``). It also links ``client_order_id`` →
        ``client_msg_id`` in ``_client_order_ids`` and
        ``_late_fill_by_coid``.

        Pre-fix: ``cleanup_pending`` popped ``_pending``, ``_results``,
        and ``_client_order_ids`` but left ``_late_fill`` and
        ``_late_fill_by_coid`` alive. When the caller invoked
        ``cleanup_pending`` after a late-fill-path terminal resolution,
        a subsequent ORDER_ERROR for the same order could match the
        stale ``_client_order_ids`` entry (only on the late-arrival
        path that bypassed ``cleanup_pending``) and overwrite the
        confirmed FILLED result with REJECTED.

        Post-fix
        --------
        A single call — regardless of which path triggered it
        (pending-path terminal OR late-fill-path terminal) — consumes
        the state for THIS order across ALL FIVE structures, exactly
        once:

          * ``_pending``              — by client_msg_id
          * ``_results``              — by client_msg_id
          * ``_client_order_ids``     — by reverse iteration on client_msg_id
          * ``_late_fill``            — by client_msg_id
          * ``_late_fill_by_coid``    — by reverse iteration on client_msg_id

        Idempotency: calling this method a second time for the same
        order is a no-op. A duplicate terminal event arriving AFTER
        state has been consumed falls through to the
        ``_unmatched_late_fills_count`` bump — never overwrites a
        confirmed terminal status.

        See also ``_consume_terminal_state`` for the synchronous
        cleanup variant used by the routing handlers themselves
        (which must clear all state inside the same critical section
        that writes the result, so a concurrent ORDER_ERROR cannot
        interleave between the result write and the registry pop).
        """
        with self._lock:
            self._pending.pop(client_msg_id, None)
            self._results.pop(client_msg_id, None)
            # Clean reverse map entries pointing to this client_msg_id
            for co_id, cm_id in list(self._client_order_ids.items()):
                if cm_id == client_msg_id:
                    self._client_order_ids.pop(co_id, None)
            # Atomic late-fill registry consumption (Card 8ad140c5 #1).
            # Without this, a late-arrival terminal event that bypassed
            # the pending path (timeout → late_fill registered → late
            # FILLED via on_execution_event) leaves _late_fill_by_coid
            # populated. A subsequent ORDER_ERROR for the same order
            # could then match via _late_fill_by_coid and overwrite the
            # confirmed FILLED with REJECTED.
            self._late_fill.pop(client_msg_id, None)
            for co_id, cm_id in list(self._late_fill_by_coid.items()):
                if cm_id == client_msg_id:
                    self._late_fill_by_coid.pop(co_id, None)

    def _consume_terminal_state(
        self,
        client_msg_id: str,
        client_order_id: str | None = None,
    ) -> None:
        """ATOMIC: clear every piece of state for a terminal resolution.

        Called from inside the routing handlers' ``with self._lock:``
        blocks (on_execution_event, on_order_error, on_general_error)
        so the result write and the registry pop are mutually visible
        to a concurrent handler invocation.

        ``client_order_id`` is optional — for ORDER_ERROR events the
        payload may carry a clientOrderId that differs from the
        pending client's clientOrderId (broker-side re-keying). When
        provided, both keys are popped from the maps that support
        double-keying.

        Idempotent: a duplicate terminal event arriving after state has
        been consumed is a no-op (pop returns ``None``, no overwrite
        of an absent result entry).
        """
        # Pending + result
        self._pending.pop(client_msg_id, None)
        self._results.pop(client_msg_id, None)
        # Reverse map: clientOrderId → client_msg_id (both keys)
        if client_order_id:
            self._client_order_ids.pop(client_order_id, None)
        for co_id, cm_id in list(self._client_order_ids.items()):
            if cm_id == client_msg_id:
                self._client_order_ids.pop(co_id, None)
        # Late-fill registry (both keys + reverse)
        self._late_fill.pop(client_msg_id, None)
        if client_order_id:
            self._late_fill_by_coid.pop(client_order_id, None)
        for co_id, cm_id in list(self._late_fill_by_coid.items()):
            if cm_id == client_msg_id:
                self._late_fill_by_coid.pop(co_id, None)

    def register_late_fill(
        self, client_msg_id: str, client_order_id: str | None = None
    ) -> None:
        """Register a timed-out order for late-fill matching.

        Call this when ``get_result`` returns ``None`` after the event times
        out. The entry stays in the registry for ``_late_fill_ttl`` seconds
        so that late-arriving execution events can still be correlated.
        """
        import time as _time

        with self._lock:
            expiry = _time.monotonic() + self._late_fill_ttl
            self._late_fill[client_msg_id] = (expiry, None)
            if client_order_id:
                self._late_fill_by_coid[client_order_id] = client_msg_id
            logger.info(
                "[LATE_FILL] Registered clientMsgId=%s for %ds grace",
                client_msg_id,
                self._late_fill_ttl,
            )

    def _check_late_fill(self, client_order_id: str) -> str | None:
        """Check late-fill registry by clientOrderId. Returns client_msg_id if found.

        Caller MUST hold ``self._lock``. This matches the pattern used
        by ``OpenApiSpotFeed._lookup_late_fill`` — the lock is acquired
        by the dispatching handler (``on_execution_event``,
        ``on_order_error``) so the lookup and the eventual result
        write / atomic consume all happen inside the same critical
        section. Re-entering the lock here would deadlock with the
        non-reentrant ``threading.Lock``.

        Side effect: lazy cleanup of expired entries — both
        ``_late_fill`` (by client_msg_id) and ``_late_fill_by_coid``
        (by reverse iteration on client_msg_id) for any expired keys.
        """
        import time as _time

        # Caller MUST hold self._lock — see docstring.
        # Lazy cleanup of expired entries
        now = _time.monotonic()
        expired = [k for k, (exp, _) in self._late_fill.items() if now >= exp]
        for k in expired:
            self._late_fill.pop(k, None)
            for coid, cmid in list(self._late_fill_by_coid.items()):
                if cmid == k:
                    self._late_fill_by_coid.pop(coid, None)
        return self._late_fill_by_coid.get(client_order_id)

    # ── Routing ──────────────────────────────────────────────────────────

    def route(self, message: Any, envelope: Any | None = None) -> None:
        """Route an incoming message by payload type.

        Args:
            message: The extracted protobuf payload (from ``Protobuf.extract``).
            envelope: The raw SDK message envelope (has ``clientMsgId``).
                       May be ``None`` when no envelope context is available.
        """
        payload_type = getattr(message, "payloadType", None)

        if payload_type in _EXECUTION_EVENT_PAYLOAD_TYPES:
            self.on_execution_event(message)
        elif payload_type == _ORDER_ERROR_PAYLOAD_TYPE:
            self.on_order_error(message, envelope)
        elif payload_type == _GENERAL_ERROR_PAYLOAD_TYPE:
            self.on_general_error(message, envelope)
        else:
            logger.debug("[ROUTE] Unhandled payload type: %s", payload_type)

    # ── Handlers ─────────────────────────────────────────────────────────

    def on_execution_event(self, message: Any) -> None:
        """Handle order filled / cancelled / rejected (payload 2126, 2151).

        Extracts ``clientOrderId`` from the order payload, matches it to a
        pending entry (with late-fill registry fallback), builds an
        :class:`OrderResult`, fires the event, and atomically consumes
        every piece of state for the order.

        Card 8ad140c5 findings addressed here:

        * #1 (HIGH): atomic consumption of every piece of state
          (``_pending``, ``_results``, ``_client_order_ids``,
          ``_late_fill``, ``_late_fill_by_coid``) on every terminal
          resolution — no orphan entries survive a confirmed FILLED /
          CANCELLED / REJECTED so a later ORDER_ERROR cannot match
          and overwrite.

        * #2: ORDER_PARTIAL_FILL (11) is PROGRESS, not terminal. The
          handler short-circuits before any pending entry touch — no
          pop, no callback, no event set. The pending entry stays
          available for the eventual terminal event.

        * #3: unmatched_late_fills counter bumps exactly once when an
          execution event arrives after the late-fill registry's TTL
          window (broker event too late to be matched). Tracks the
          clientOrderId-only lookup path AND the client_msg_id-only
          fallback path; both contribute via the same
          ``expired_keys_seen`` set so the counter never
          double-counts when both paths land on the same expired
          entry.
        """
        order_payload = getattr(message, "order", None)
        client_order_id = (
            getattr(order_payload, "clientOrderId", "") if order_payload else ""
        )
        etype = getattr(message, "executionType", None)

        # Card 8ad140c5 finding #2 — sprint reina-2026-08-18-106:
        # PARTIAL_FILL(11) is PROGRESS, not terminal. cTrader emits
        # PARTIAL_FILL during multi-chunk fills (iceberg / liquidity
        # slicing). The next event for the same clientOrderId is
        # usually another PARTIAL_FILL(11) or ORDER_FILLED(3). If we
        # treated 11 as terminal here we would fire on_filled
        # prematurely with a PARTIAL executionPrice, causing the
        # downstream strategy to mark the trade closed while the
        # broker is still working the remainder — and close the
        # pending entry so the real ORDER_FILLED(3) that arrives next
        # would either fail to match or be dropped as a duplicate.
        #
        # Log informationally and return — the pending entry stays
        # available for the eventual terminal event. Operators still
        # get visibility via this log line + the partial fill
        # volume/price when present in the payload.
        if etype == _EXEC_TYPE_PARTIAL_FILL:
            filled_price = (
                getattr(order_payload, "executionPrice", None)
                if order_payload
                else None
            )
            filled_volume = (
                getattr(order_payload, "executedVolume", None)
                if order_payload
                else None
            )
            logger.info(
                "[EXEC] PARTIAL_FILL (PROGRESS, not terminal) — "
                "clientOrderId=%r filled_price=%s filled_volume=%s "
                "pending entry preserved for terminal event",
                client_order_id,
                filled_price,
                filled_volume,
            )
            return

        with self._lock:
            matched_id = self._match_by_client_order_id(client_order_id)
            # Card 8ad140c5 finding #3: track whether the lookup
            # observed an expired late-fill entry so the
            # unmatched_late_fills counter bumps exactly once on
            # the DROP path. A set ensures the same expired entry
            # observed via both clientOrderId and client_msg_id only
            # counts once.
            expired_keys_seen: set[str] = set()
            if not matched_id and client_order_id:
                # Pre-check: was the clientOrderId in the registry
                # BEFORE the lookup? If yes AND the lookup returns
                # None, the entry was expired → bump the counter.
                pre_present = client_order_id in self._late_fill_by_coid
                late_match = self._check_late_fill(client_order_id)
                if late_match:
                    matched_id = late_match
                elif pre_present:
                    expired_keys_seen.add(client_order_id)
            if not matched_id:
                if expired_keys_seen:
                    self._unmatched_late_fills_count += 1
                logger.warning(
                    "[EXEC] No match for clientOrderId=%r etype=%r "
                    "(unmatched_late_fills=%d)",
                    client_order_id,
                    etype,
                    self._unmatched_late_fills_count,
                )
                return

            result = self._build_result_from_execution(message, order_payload, etype)
            self._results[matched_id] = result
            self._pending[matched_id].set()
            # Card 8ad140c5 finding #1 (HIGH): atomic terminal
            # consumption. The result write and the state pop must
            # be mutually visible to a concurrent handler invocation
            # (e.g. ORDER_ERROR arriving in a parallel thread) so a
            # confirmed FILLED cannot be overwritten by a stale
            # REJECTED that matches via a residual late-fill entry.
            self._consume_terminal_state(matched_id, client_order_id)

        logger.info(
            "[EXEC] clientOrderId=%r → %s", client_order_id, result.status.value
        )

        # Stats recording (Amendment A5)
        if self._stats is not None and result.status == OrderStatus.FILLED:
            try:
                self._stats.record_outcome(
                    signal_id=matched_id,
                    outcome="tp_hit",
                    pips=0.0,
                    time_to_close=0,
                )
            except Exception:
                logger.debug("[EXEC] Stats recording failed (non-fatal)")

        # Callbacks
        if result.status == OrderStatus.FILLED and self._on_filled:
            try:
                self._on_filled(result)
            except Exception:
                logger.debug("[EXEC] on_filled callback error (non-fatal)")

        if result.status == OrderStatus.REJECTED and self._on_rejected:
            try:
                self._on_rejected(result)
            except Exception:
                logger.debug("[EXEC] on_rejected callback error (non-fatal)")

    def on_order_error(self, message: Any, envelope: Any | None) -> None:
        """Handle order error event (payload 2132).

        Extracts ``errorCode`` + ``description``, matches by
        ``clientOrderId`` (from the payload) or ``clientMsgId`` (from the
        envelope), populates a REJECTED :class:`OrderResult`, fires event,
        and atomically consumes every piece of state.

        Card 8ad140c5 findings addressed here:

        * #1 (HIGH): atomic terminal consumption — same as
          ``on_execution_event``. Prevents a REJECTED here from
          orphaning late-fill entries that a subsequent legitimate
          ORDER_ERROR could match.

        * #3: unmatched_late_fills counter bumps exactly once when
          an ORDER_ERROR arrives for an order whose late-fill registry
          entry has already expired. Tracks the clientOrderId lookup
          path via ``_check_late_fill``.
        """
        client_order_id = getattr(message, "clientOrderId", "")
        client_msg_id = ""
        if envelope is not None:
            client_msg_id = getattr(envelope, "clientMsgId", "")

        error_code = getattr(message, "errorCode", "UNKNOWN")
        description = getattr(message, "description", "")

        with self._lock:
            matched_id = self._match_by_client_order_id(client_order_id)
            expired_keys_seen: set[str] = set()
            if not matched_id and client_msg_id:
                matched_id = client_msg_id if client_msg_id in self._pending else None
            if not matched_id and client_order_id:
                # Late-fill registry fallback (Card 8ad140c5 #3).
                # Pre-check: was the clientOrderId in the registry
                # BEFORE the lookup? If yes AND the lookup returns
                # None, the entry was expired → bump the counter.
                pre_present = client_order_id in self._late_fill_by_coid
                late_match = self._check_late_fill(client_order_id)
                if late_match:
                    matched_id = late_match
                elif pre_present:
                    expired_keys_seen.add(client_order_id)
            if not matched_id:
                if expired_keys_seen:
                    self._unmatched_late_fills_count += 1
                logger.warning(
                    "[ORDER_ERROR] No match clientOrderId=%r clientMsgId=%r "
                    "errorCode=%r desc=%r (unmatched_late_fills=%d)",
                    client_order_id,
                    client_msg_id,
                    error_code,
                    description,
                    self._unmatched_late_fills_count,
                )
                return

            result = OrderResult(
                status=OrderStatus.REJECTED,
                error_code=error_code,
                error_message=description,
            )
            self._results[matched_id] = result
            self._pending[matched_id].set()
            # Card 8ad140c5 finding #1 (HIGH): atomic terminal
            # consumption — same as on_execution_event.
            self._consume_terminal_state(matched_id, client_order_id)

        logger.warning(
            "[ORDER_ERROR] → REJECTED errorCode=%r desc=%r",
            error_code,
            description,
        )

        if self._on_rejected:
            try:
                self._on_rejected(result)
            except Exception:
                logger.debug("[ORDER_ERROR] on_rejected callback error (non-fatal)")

    def on_general_error(self, message: Any, envelope: Any | None = None) -> None:
        """Handle general error response (payload 2142).

        Tries to resolve any pending order that might be waiting on this
        error response (matching by ``clientMsgId`` from the envelope).
        If no pending order matches, simply logs the error.

        Card 8ad140c5 finding #1 (HIGH): atomic terminal consumption —
        same as ``on_execution_event`` and ``on_order_error``. The
        general-error path is rare but still resolves a pending order,
        so it must also clear every piece of state (pending, results,
        client_order_ids, late_fill, late_fill_by_coid) under the
        same critical section that writes the result.
        """
        error_code = getattr(message, "errorCode", "UNKNOWN")
        description = getattr(message, "description", "")

        client_msg_id = ""
        if envelope is not None:
            client_msg_id = getattr(envelope, "clientMsgId", "")

        resolved = False
        if client_msg_id:
            with self._lock:
                if client_msg_id in self._pending:
                    result = OrderResult(
                        status=OrderStatus.REJECTED,
                        error_code=error_code,
                        error_message=description,
                    )
                    self._results[client_msg_id] = result
                    self._pending[client_msg_id].set()
                    # Card 8ad140c5 finding #1 (HIGH): atomic terminal
                    # consumption. The general-error path is rare but
                    # still resolves a pending order, so it must clear
                    # every piece of state under the same critical
                    # section that writes the result.
                    self._consume_terminal_state(client_msg_id)
                    resolved = True

        if resolved:
            logger.warning(
                "[ERROR] → REJECTED clientMsgId=%r errorCode=%r desc=%r",
                client_msg_id,
                error_code,
                description,
            )
        else:
            logger.error(
                "[ERROR] General error: code=%r desc=%r", error_code, description
            )

    # ── Internal helpers ─────────────────────────────────────────────────

    def _match_by_client_order_id(self, client_order_id: str) -> str | None:
        """Match a ``clientOrderId`` to a pending entry's ``client_msg_id``.

        Checks the explicit ``_client_order_ids`` reverse map first, then
        falls back to a direct key match (for cases where the
        ``clientOrderId`` *is* the ``clientMsgId``).
        """
        if not client_order_id:
            return None

        # Explicit reverse map
        if client_order_id in self._client_order_ids:
            return self._client_order_ids[client_order_id]

        # Direct match (clientOrderId used as clientMsgId)
        if client_order_id in self._pending:
            return client_order_id

        return None

    @staticmethod
    def _build_result_from_execution(
        message: Any,
        order_payload: Any,
        etype: Any,
    ) -> OrderResult:
        """Construct an :class:`OrderResult` from a terminal execution event.

        Maps the ``ProtoOAExecutionType`` to the appropriate
        :class:`OrderStatus` and extracts fill details.

        Card 8ad140c5 finding #2 — sprint reina-2026-08-18-106:
        ORDER_PARTIAL_FILL(11) is NOT mapped here. It is a PROGRESS
        event, handled by ``on_execution_event`` which short-circuits
        before this method is invoked. Defensive: if a caller bypasses
        ``on_execution_event`` (test fixture, future refactor), the
        ``etype in (_EXEC_TYPE_FILLED, _EXEC_TYPE_PARTIAL_FILL)`` branch
        must not be re-introduced — PARTIAL_FILL must remain non-terminal
        to avoid the premature on_filled firing documented in the
        long-form comment in ``on_execution_event``.
        """
        if etype == _EXEC_TYPE_PARTIAL_FILL:
            # Defensive: should not reach here. Return None to signal
            # non-terminal. Caller (on_execution_event) already
            # short-circuits PARTIAL_FILL before invoking this method.
            logger.warning(
                "[EXEC] _build_result_from_execution called with PARTIAL_FILL "
                "— should have been short-circuited in on_execution_event"
            )
            raise ValueError(
                "PARTIAL_FILL is PROGRESS, not terminal; handle in on_execution_event"
            )

        if etype == _EXEC_TYPE_FILLED:
            filled_price = (
                getattr(order_payload, "executionPrice", None)
                if order_payload
                else None
            )
            filled_volume = (
                getattr(order_payload, "executedVolume", None)
                if order_payload
                else None
            )
            return OrderResult(
                status=OrderStatus.FILLED,
                order_id=str(getattr(order_payload, "orderId", "") or ""),
                filled_price=filled_price,
                filled_volume=filled_volume,
            )

        if etype == _EXEC_TYPE_CANCELLED:
            return OrderResult(
                status=OrderStatus.CANCELLED,
                order_id=str(getattr(order_payload, "orderId", "") or "")
                if order_payload
                else None,
            )

        if etype in (_EXEC_TYPE_REJECTED, _EXEC_TYPE_EXPIRED):
            error_code = getattr(message, "errorCode", None) or "REJECTED"
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=str(getattr(order_payload, "orderId", "") or "")
                if order_payload
                else None,
                error_code=error_code,
            )

        # Default: treat as filled (ORDER_ACCEPTED, ORDER_REPLACED, etc.)
        # This branch handles informational etypes that bypassed the
        # caller-side filter (test fixtures, future refactors).
        # Production code short-circuits PARTIAL_FILL in on_execution_event
        # before reaching here; ACCEPT/REPLACED are not in the handler's
        # payload type set so they never reach on_execution_event either.
        logger.debug("[EXEC] Unhandled executionType=%r, defaulting to FILLED", etype)
        return OrderResult(status=OrderStatus.FILLED)


__all__ = ["ExecutionEventHandler"]
