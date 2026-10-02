"""cTrader Open API Spot Feed — orchestrator composing extracted modules.

Slim orchestrator that composes:
- ``CTraderConnection`` — TCP connect/disconnect, reconnection, health monitoring
- ``CTraderAuth`` — authentication (app + account level) [archived; re-exported via credential_store/token_lifecycle]
- ``BarBuilder`` — tick → OHLCV aggregation (used by ForwardTestEngine)

This module handles:
- Subscription management (symbol resolution, spot subscriptions)
- Tick routing (spot events → Tick objects → callbacks)
- Order execution (new, amend, cancel, close, reconcile)
- Token refresh lifecycle (proactive + reactive)
- Kill switch integration

Symbol name normalization (canonical format):
    Strip '/' and '_' characters, uppercase. Example: "EUR/USD" → "EURUSD".

Historical context (preserved for grep-ability and design intent):

BQ-1327 — dual-token-manager race condition fix. The orchestrator coordinates
a single ``TokenManager`` instance (see ``self._token_mgr``) and serializes
all token refreshes through ``self._refresh_lock``. Before this fix, the
auth shim and the spot feed each ran their own token refresh loop, racing on
the same refresh token and clobbering ``.env``. The fix consolidates refresh
into this orchestrator and exposes ``_handle_auth_failure`` /
``_check_circuit_breaker`` for the single-state auth-error escalation path.

BQ-1329 — concurrent-session conflict (second-app strategy). cTrader enforces
a single-session rule (one live TCP session per OpenAPI app). To run both
the live spot feed (``OpenApiSpotFeed``) and historical bar requests
(``CTraderOpenApiClient``) without one evicting the other, they authenticate
with separate OpenAPI apps: the spot feed uses ``CTRADER_OPENAPI_CLIENT_ID``
/ ``CTRADER_OPENAPI_CLIENT_SECRET`` (primary), while the historical client
uses ``CTRADER_TRADE_APP_ID`` / ``CTRADER_TRADE_SECRET`` (secondary). A fully
shared TCP socket was deferred because it requires invasive changes to the
archived feed implementation; the second-app approach is the Phase-5
pragmatic fallback.
"""

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAmendOrderReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOACancelOrderReq,
    ProtoOAClosePositionReq,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOAUnsubscribeSpotsReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOATimeInForce,
    ProtoOATradeSide,
)
from ctrader_open_api.protobuf import Protobuf
from twisted.internet import reactor

from .auth_error_types import get_policy
from .connection import CTraderConnection
from .connection_state import ConnectionState, ConnectionStateManager
from .environment import (
    _infer_environment,
    log_startup_environment,
    validate_endpoint_environment,
)
from .execution_permission import ExecutionPermissionPolicy
from .market_data_feed import SymbolInfo, Tick
from .models import (
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TradeDirection,
)
from .token_lifecycle import TokenLifecycle
from .token_manager import TokenManager
from .volume_calculator import VolumeCalculator

logger = logging.getLogger("ayumi.openapi_spot_feed")

# Payload type constants
_APP_AUTH_RES_PAYLOAD_TYPE = 2101
_ACCT_AUTH_RES_PAYLOAD_TYPE = 2103
_ERROR_RES_PAYLOAD_TYPE = 2142
_EXECUTION_EVENT_PAYLOAD_TYPES = {2126, 2151}
_ORDER_ERROR_EVENT_PAYLOAD_TYPE = 2132
# 30s — cTrader demo broker has been observed responding in ~25s.
# The previous 20s base (+5s margin = 25s total) was exactly at the
# broker's response boundary, causing every order to time out.
# 30s base (+5s margin = 35s total) gives 10s of headroom.
_ORDER_TIMEOUT_SEC = 30.0
_AMEND_TIMEOUT_SEC = 30.0  # SL/TP amends are not time-critical — use a longer timeout
_RECONCILE_TIMEOUT_SEC = 10.0

# Late-fill registry TTL: how long after timeout/disconnect we keep
# order entries available for matching late-arriving execution events.
_LATE_FILL_TTL_SEC = 120.0

# Terminal ProtoOAExecutionType set (per vendored OpenApiModelMessages_pb2).
# Only these etypes should pop the pending order, set the event, and fire
# the terminal callbacks.  Everything else (ORDER_ACCEPTED, ORDER_REPLACED,
# ORDER_PARTIAL_FILL, ORDER_CANCEL_REJECTED, SWAP, DEPOSIT_WITHDRAW,
# BONUS_DEPOSIT_WITHDRAW) is informational — log only, do NOT pop the
# pending order, do NOT fire on_order_filled, do NOT set the event. This
# is the fix for card ce6de98d where the old pop-any-match logic caused
# ACCEPT(2) to fire on_order_filled on the ACCEPT payload, then the real
# FILLED(3) to rescue via the late_fill_registry producing a duplicate
# on_order_filled.
_TERMINAL_EXEC_TYPES: frozenset[int] = frozenset(
    {
        ProtoOAExecutionType.ORDER_FILLED,  # 3
        ProtoOAExecutionType.ORDER_CANCELLED,  # 5
        ProtoOAExecutionType.ORDER_REJECTED,  # 7
        ProtoOAExecutionType.ORDER_EXPIRED,  # 6
    }
)
# Progress (non-terminal, non-callback) ProtoOAExecutionType set (card
# 8ad140c5 finding #2 — sprint reina-2026-08-18-106).
#
# Why ORDER_PARTIAL_FILL(11) is PROGRESS, not terminal (designed by Reina,
# do NOT blind-conform to the initial scope {3,11,5,7,6}):
#
#   cTrader emits ORDER_PARTIAL_FILL(11) during the lifetime of an order
#   that fills in multiple chunks (e.g. iceberg / liquidity-driven
#   slicing on large market orders). The very next event for the same
#   clientOrderId is usually another ORDER_PARTIAL_FILL(11) or — at the
#   end — ORDER_FILLED(3).  If we treated 11 as terminal we would:
#
#     (a) fire on_order_filled prematurely with a PARTIAL executionPrice,
#         causing downstream strategy code to mark the trade closed
#         while the broker is still working the remainder;
#     (b) close the pending entry, so the real terminal ORDER_FILLED(3)
#         that arrives next would either fail to match or be classified
#         as a duplicate (the _fill_cb_fired dedupe would suppress the
#         real fill callback, leaving the trade unaccounted).
#
#   Treating 11 as PROGRESS is the correct semantics: update executed
#   volume / price on the order object when present in the payload,
#   log informationally, fire NO callback, keep the pending entry open
#   until ORDER_FILLED(3) (or one of the other terminal events).
#
#   The 11-then-stall path still flows into the existing INDETERMINATE
#   timeout (set by new_order() when event.wait expires) — unchanged.
#
#   Operators get visibility via the informational log line that
#   mentions partial fill volume / price when present.
_PROGRESS_EXEC_TYPES: frozenset[int] = frozenset(
    {
        ProtoOAExecutionType.ORDER_PARTIAL_FILL,  # 11
    }
)
# OrderStatus values that downstream consumers treat as terminal rejections
# from the spot feed's perspective. Used by indeterminate_timeout path
# decision logic in new_order() — see card ce6de98d fix (B).
_TERMINAL_REJECT_STATUSES = frozenset({OrderStatus.REJECTED, OrderStatus.CANCELLED})

# Reason string for indeterminate timeout (card ce6de98d fix B).
# Distinct from the legacy "timeout_awaiting_event" so downstream consumers
# (forward_test_engine._classify_live_order_outcome) can separate a true
# awaiting-ack state (signals_sent / signals_indeterminate) from a real
# broker failure (signals_failed_live).
_INDETERMINATE_TIMEOUT_REASON = "indeterminate_awaiting_event"

# Re-exported from connection.py for backward compatibility
from .connection import (
    _HEARTBEAT_DEGRADED_SEC,
    _HEARTBEAT_RECONNECT_SEC,
)
from .market_hours import is_forex_market_closed

# Stale tick thresholds (seconds during market hours)
_STALE_TICK_WARN_SEC = 60.0
_STALE_TICK_FREEZE_SEC = 120.0

# Pre-emptive reconnect thresholds (2026-07-17, revised 2026-07-17)
# The connection.py health monitor degrades at 35s and reconnects at 60s.
# We trigger a proactive reconnect at 30s — closer to the degraded window
# but still before it opens — so orders never queue against an unresponsive
# connection.  The original 20s threshold caused a race condition: during
# the 35-second event.wait() in new_order(), heartbeat silence could reach
# 20s under normal conditions (busy market, server-side processing delay),
# triggering a reconnect that tore down the connection while the order
# response was still in-flight, causing every order to time out with
# "timeout_awaiting_event" (card d88336dc root cause).
_PRE_EMPTIVE_RECONNECT_SEC = 30.0
_PRE_EMPTIVE_POLL_SEC = 5.0

# Error tier classification per BQ-1382 §6
_ERROR_TIERS = {
    # Tier 1: Transient — auto-reconnect
    "CH_OAUTH_TOKEN_EXPIRED": "transient",
    "CH_INVALID_TOKEN": "transient",
    "ALREADY_LOGGED_IN": "transient",
    "SESSION_EXPIRED": "transient",
    # Tier 2: Rate/Resource — back off
    "SERVER_BUSY": "rate_resource",
    "RATE_LIMIT_REACHED": "rate_resource",
    "CH_ACCOUNT_NOT_LOGGED_IN": "transient",
    # Tier 3: Critical — alert and halt
    "CH_PERMISSION_DENIED": "critical",
    "CH_SERVER_SECURITY_NOT_PASSED": "critical",
}


def _normalize_symbol_name(name: str) -> str:
    return name.replace("/", "").replace("_", "").upper()


def _lots_to_units(lots: float) -> int:
    """Deprecated — use VolumeCalculator.lots_to_volume() instead.

    Kept as a backward-compat alias for any external callers that haven't
    migrated yet. All internal call sites now go through VolumeCalculator.
    """
    return int(round(lots * 100_000))


class OpenApiSpotFeed:
    """Live spot price feed via cTrader Open API.

    Composes CTraderConnection (TCP), routes ticks to callbacks,
    and manages order execution.
    """

    # Class-level default so mock instances (created via __new__) have
    # _permission_policy = None even when __init__ is bypassed.
    _permission_policy: Optional["ExecutionPermissionPolicy"] = None

    def __init__(
        self,
        ctid_account_id: int,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str | None = None,
        host: str = "live.ctraderapi.com",
        port: int = 5035,
        token_lifecycle: Optional["TokenLifecycle"] = None,
    ):
        self._ctid_account_id = ctid_account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._refresh_token = refresh_token or ""
        self._host = host
        self._port = port

        # TokenManager — DEPRECATED for OAuth operations.
        # OAuth refresh is owned by TokenLifecycle (self._token_lifecycle).
        # Do not add new OAuth calls here.
        self._token_mgr = TokenManager(
            token_path=Path(__file__).resolve().parents[3] / "data" / "token_state.json",
            env_path=Path(__file__).resolve().parents[3] / ".env",
        )

        # TokenLifecycle — the ONLY OAuth refresh owner.
        # If not passed by caller, lazily construct from .env.
        self._token_lifecycle: Optional[TokenLifecycle] = token_lifecycle

        # Connection (extracted module)
        self._state_mgr = ConnectionStateManager(name="spot_feed")
        self._conn = CTraderConnection(
            host,
            port,
            state_manager=self._state_mgr,
        )
        self._conn.on_connected(self._on_conn_connected)
        self._conn.on_disconnected(self._on_conn_disconnected)
        self._conn.on_feed_dead(self._on_conn_feed_dead)

        # State
        self._running: bool = False
        self._authed: threading.Event = threading.Event()
        self._app_authed: threading.Event = threading.Event()
        self._reauth_in_progress = threading.Event()
        self._lock = threading.Lock()
        # Serializes amend_sl_tp calls so multiple simultaneous fills don't
        # overwhelm the cTrader connection with back-to-back requests.
        self._amend_lock = threading.Lock()

        # Symbol metadata
        self._symbols: dict[int, SymbolInfo] = {}
        self._name_to_id: dict[str, int] = {}
        self._id_to_name: dict[int, str] = {}
        self._symbol_digits: dict[int, int] = {}

        # Subscriptions
        self._subscribed_symbol_ids: set[int] = set()

        # Tick data
        self._ticks: dict[str, Tick] = {}
        self._ticks_by_id: dict[int, Tick] = {}
        self._tick_callbacks: list[Callable[[Tick], None]] = []
        self._tick_counts: dict[str, int] = {}
        self._last_tick_recv_monotonic: float = time.monotonic()

        # Token refresh state
        self._token_expires_at: float | None = None
        self._refresh_timer: threading.Timer | None = None
        self._refresh_in_progress = False
        self._refresh_lock = threading.Lock()
        self._auth_error_count: int = 0
        self._auth_circuit_open: bool = False
        self._last_reactive_refresh_time: float = 0.0
        self._last_successful_auth_time: float = 0.0

        # Card 1e32c408 (session-conflict guard, sprint reina-2026-08-21-024):
        # Single-session mutex shared between the token-refresh-driven
        # re-auth (``_refresh_token_and_reauth._do_refresh_offthread``) and
        # the reconnect-driven re-auth (``_reconnect_restore``). Prevents
        # concurrent re-auth sends on the same cTrader channel — without
        # this lock, a reactive AUTH_EXPIRED refresh and a proactive
        # refresh could interleave and produce duplicate account-auth
        # requests, which the broker rejects with ALREADY_LOGGED_IN. The
        # token-Lifecycle level (``_refresh_lock``) protects the OAuth
        # HTTP call itself; this lock is at the spot-feed layer and
        # covers the *broker side* of the auth cycle. Non-blocking
        # acquire keeps the proactive timer from stalling — if another
        # re-auth is already in flight, the caller skips (the holder
        # will finish or the next cycle will retry).
        self._session_reauth_lock = threading.Lock()

        # Reconnect stabilization delay — cTrader demo server needs time to
        # accept auth after TCP connect (market-open race, see card 23cb1091).
        self._reconnect_stabilization_delay: float = float(os.environ.get("CTRADER_RECONNECT_DELAY", "3.0"))

        # Reconnection state
        self._connected_at: float | None = None
        self._disconnect_at: float | None = None
        self._on_reconnected_callbacks: list[Callable[[float], None]] = []

        # Kill switch
        self._kill_switch: Optional[object] = None
        self._permission_policy: Optional[ExecutionPermissionPolicy] = None

        # Order execution
        self._pending_orders: dict[str, tuple[threading.Event, Order]] = {}
        self._pending_client_msg_ids: dict[str, str] = {}
        self._disconnected_pending_orders: list[Order] = []

        # Late-fill registry: orders that timed out or were disconnected
        # but may still receive execution events from the broker.
        # Maps request_id → (expiry_monotonic, Order, client_msg_id)
        self._late_fill_registry: dict[str, tuple[float, Order, str]] = {}

        # Pre-emptive reconnect monitor
        self._preemptive_thread: Optional[threading.Thread] = None
        self._preemptive_stop = threading.Event()
        self._preemptive_in_progress = threading.Event()
        self._callbacks: dict[str, list[Callable]] = {
            "on_order_filled": [],
            "on_order_rejected": [],
            "on_order_cancelled": [],
        }

        # Card 18b74ea7: Session-conflict counter — incremented when an
        # ORDER_ERROR event arrives with empty clientOrderId AND errorCode
        # == ALREADY_LOGGED_IN (or description contains "already authorized").
        # Surfaced via health_monitor's _safe_attr lookup against the spot
        # feed (order_gateway role). The watchdog process and the forward
        # test both authenticate to the same trading account via the same
        # OpenAPI app, which causes the server to return ALREADY_LOGGED_IN
        # on the second auth attempt. Previously this error was silently
        # dropped because clientOrderId was empty (no pending-order match)
        # — invisible to all counters. Now it is logged as a SESSION-CONFLICT
        # warning and the counter is bumped so operators can see the
        # double-authorization pattern in the B5 health line.
        self._order_error_session_conflict_count: int = 0

        # Card ce6de98d (E): unmatched_late_fills counter — incremented
        # when an execution event arrives for a late-fill registry entry
        # whose TTL has already expired. The DROP warning was already in
        # place; the counter is additive so operators can see how often
        # broker events arrive after the registry's 120s grace window.
        # Surfaced via health_monitor._safe_attr against the spot feed.
        self._unmatched_late_fills_count: int = 0

        # Card ce6de98d (C): Per-order fill-callback dedupe flag. Set to True
        # after the FIRST on_order_filled callback has fired for the order.
        # Prevents duplicate on_order_filled when (a) ACCEPT(2) was
        # previously misrouted to FILLED via the old pop-any-match bug, or
        # (b) the broker re-emits FILLED after TTL expiry on the late path.
        # Stored as a setattr on the Order object itself (rather than a
        # separate map) so the dedupe flag travels with the order through
        # the late_fill_registry path.
        # The attribute name "_fill_cb_fired" is checked before invoking
        # on_order_filled — see _handle_execution_event.
        self._callback_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="openapi-spot-callback",
        )

        # Volume conversion (per-symbol, replaces hardcoded 100_000)
        self._volume_calc = VolumeCalculator(self._symbols)

    # ── Properties ─────────────────────────────────────────────────────────

    def set_kill_switch(self, kill_switch) -> None:
        self._kill_switch = kill_switch

    def set_permission_policy(self, policy: ExecutionPermissionPolicy) -> None:
        self._permission_policy = policy

    def validate_wiring(self) -> None:
        """Validate that required production dependencies are wired.

        Called by ForwardTestEngine after construction, NOT in __init__.
        This allows tests to construct OpenApiSpotFeed without a full
        TokenLifecycle while ensuring production paths can't forget it.
        """
        if self._token_lifecycle is None:
            raise RuntimeError(
                "OpenApiSpotFeed.validate_wiring(): token_lifecycle is None. "
                "Production runtime requires a TokenLifecycle instance for OAuth refresh delegation. "
                "Pass token_lifecycle=<TokenLifecycle> when constructing for live/demo use."
            )
        # Enable token refresh — TokenLifecycle ships with _refresh_disabled=True
        # as a conservative default. Production runtime must override this to
        # allow proactive and reactive OAuth refresh. Without this, the access
        # token expires and every subsequent request gets
        # account_authorization_fault (INVALID_REQUEST: not authorized).
        if getattr(self._token_lifecycle, "_refresh_disabled", False):
            self._token_lifecycle._refresh_disabled = False
            logger.info("[Startup] token refresh ENABLED (was disabled by default)")
        # Start proactive refresh timer now that refresh is enabled.
        if hasattr(self._token_lifecycle, "start_proactive_timer"):
            try:
                self._token_lifecycle.start_proactive_timer()
            except Exception as exc:
                logger.warning("[Startup] proactive refresh timer failed: %s", exc)
        logger.info(
            "[Startup] refresh_owner=TokenLifecycle wired=%s refresh_enabled=True",
            self._token_lifecycle is not None,
        )

    def on_reconnected(self, callback: Callable[[float], None]) -> None:
        self._on_reconnected_callbacks.append(callback)

    @property
    def state_manager(self) -> ConnectionStateManager:
        return self._state_mgr

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._state_mgr.is_authenticated

    @property
    def is_paper_mode(self) -> bool:
        return False

    @property
    def symbols(self) -> dict[int, SymbolInfo]:
        return dict(self._symbols)

    @property
    def name_to_id(self) -> dict[str, int]:
        return dict(self._name_to_id)

    @property
    def connection(self):
        """Expose the underlying CTraderConnection for account queries (balance, positions)."""
        return self._conn

    @property
    def ctid_account_id(self) -> int:
        """Expose the cTrader account ID for account-state queries."""
        return self._ctid_account_id

    @property
    def ticks(self) -> dict[str, Tick]:
        with self._lock:
            return dict(self._ticks)

    @property
    def tick_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._tick_counts)

    def lots_to_volume(self, symbol_id: int, lots: float) -> int:
        """Public accessor for VolumeCalculator — used by Task 4 consumers."""
        return self._volume_calc.lots_to_volume(symbol_id, lots)

    def get_health(self) -> dict:
        now = time.monotonic()
        heartbeat_age = now - self._conn._last_heartbeat_recv if self._conn._last_heartbeat_recv else None
        return {
            "auth_circuit_open": self._auth_circuit_open,
            "auth_error_count": self._auth_error_count,
            "refresh_in_progress": self._refresh_in_progress,
            "connected": self._conn.is_connected,
            "authed": self._authed.is_set(),
            "state": self._state_mgr.state.value,
            "is_operational": self._state_mgr.is_operational,
            "last_tick_age": now - self._last_tick_recv_monotonic,
            "last_heartbeat_age": heartbeat_age if heartbeat_age is not None else 999,
        }

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, auto_subscribe: list[str] | None = None) -> bool:
        if self._running:
            return True

        # Environment validation — cross-check endpoint vs configured environment.
        _env = _infer_environment(self._host)
        try:
            validate_endpoint_environment(self._host, _env)
        except ValueError as e:
            logger.critical("%s", e)
            raise  # Fail closed — do not connect

        log_startup_environment(
            env=_env,
            host=self._host,
            account_id=str(self._ctid_account_id),
            kill_switch_active=True,  # kill switch is always "active" conceptually
            kill_switch_mode="freeze",
        )

        logger.info(
            "[Startup Diagnostics] environment=%s endpoint=%s account=%s "
            "refresh_owner=%s execution_mode=%s kill_switch=%s",
            _env.value,
            self._host,
            self._ctid_account_id,
            "TokenLifecycle" if self._token_lifecycle is not None else "NONE",
            "live" if getattr(self, "_is_live", False) else "demo",
            "preserved",  # don't read kill switch state here — just note it's checked
        )

        # Token validation — placeholder check only.
        # TokenLifecycle.ensure_valid() (called by ForwardTestEngine) handles
        # OAuth refresh. We keep a lightweight placeholder guard here.
        _PLACEHOLDER_VALUES = {
            "***",
            "new-access",
            "new-refresh",
            "",
            "none",
            "null",
            "todo",
            "changeme",
        }
        if self._access_token.lower() in _PLACEHOLDER_VALUES:
            logger.critical("STARTUP ABORTED: Access token is a placeholder")
            return False
        if self._refresh_token.lower() in _PLACEHOLDER_VALUES:
            logger.critical("STARTUP ABORTED: Refresh token is a placeholder")
            return False

        if getattr(self._callback_executor, "_shutdown", False):
            self._callback_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="openapi-spot-callback",
            )

        # Connect via CTraderConnection
        if not self._conn.connect():
            return False

        # Authenticate
        if not self._auth():
            return False

        # Fetch symbols
        if not self._fetch_symbol_list():
            logger.warning("Symbol list API failed, using static fallback")
            self._populate_static_symbols()

        self._running = True
        self._conn.start_health_monitor()

        if auto_subscribe:
            for symbol_name in auto_subscribe:
                symbol_id = self._resolve_name_to_id(symbol_name)
                if symbol_id is None:
                    logger.warning("Cannot resolve '%s' — skipping", symbol_name)
                    continue
                if symbol_id not in self._symbol_digits:
                    self._fetch_symbol_details(symbol_id)
            for symbol_name in auto_subscribe:
                if not self.subscribe(symbol_name):
                    logger.warning("Failed to auto-subscribe to %s", symbol_name)

        logger.info(
            "OpenApiSpotFeed started: account=%d symbols=%d",
            self._ctid_account_id,
            len(self._symbols),
        )
        self._start_preemptive_monitor()
        return True

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_preemptive_monitor()
        self._conn.stop_health_monitor()
        self._conn.disconnect()

        for symbol_id in list(self._subscribed_symbol_ids):
            self._unsubscribe_by_id(symbol_id)

        for _, (event, order) in list(self._pending_orders.items()):
            order.status = OrderStatus.PENDING
            order.comment = order.comment or "connection_lost_during_order"
            order.reason = "connection_lost_during_order"
            event.set()
        self._pending_orders.clear()
        self._pending_client_msg_ids.clear()
        self._late_fill_registry.clear()
        self._callback_executor.shutdown(wait=False, cancel_futures=True)

        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None

        logger.info("OpenApiSpotFeed stopped")

    # ── Pre-emptive reconnect monitor ──────────────────────────────────────

    def _start_preemptive_monitor(self) -> None:
        """Start a daemon thread that monitors heartbeat staleness.

        When heartbeat silence exceeds ``_PRE_EMPTIVE_RECONNECT_SEC``, a
        proactive reconnect is triggered *before* the connection enters the
        DEGRADED window (35s in connection.py).  This prevents orders from
        queuing against an unresponsive connection.
        """
        if self._preemptive_thread is not None and self._preemptive_thread.is_alive():
            return
        self._preemptive_stop.clear()
        self._preemptive_thread = threading.Thread(
            target=self._preemptive_loop,
            name="ctrader-preemptive-reconnect",
            daemon=True,
        )
        self._preemptive_thread.start()
        logger.info(
            "[Preemptive] Monitor started (threshold=%ss, poll=%ss)",
            _PRE_EMPTIVE_RECONNECT_SEC,
            _PRE_EMPTIVE_POLL_SEC,
        )

    def _stop_preemptive_monitor(self) -> None:
        """Stop the pre-emptive reconnect monitor thread."""
        self._preemptive_stop.set()
        thread = self._preemptive_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_PRE_EMPTIVE_POLL_SEC * 2)
        self._preemptive_thread = None

    def _preemptive_loop(self) -> None:
        """Main pre-emptive reconnect monitor loop."""
        while not self._preemptive_stop.wait(_PRE_EMPTIVE_POLL_SEC):
            try:
                self._check_preemptive_reconnect()
            except Exception as exc:
                logger.error("[Preemptive] Monitor cycle error: %s", exc)

    def _check_preemptive_reconnect(self) -> None:
        """Check heartbeat staleness and trigger pre-emptive reconnect if needed."""
        # Skip if connection is not in AUTHENTICATED state
        if not self._state_mgr.is_authenticated:
            return
        # Skip if a reconnect or re-auth is already in progress
        if self._reauth_in_progress.is_set():
            return
        if self._preemptive_in_progress.is_set():
            return
        # Skip during market close (no ticks expected)
        if is_forex_market_closed():
            return
        # CRITICAL: Never trigger a reconnect while orders are in-flight.
        # new_order() blocks on event.wait(timeout+5) for the broker's
        # execution event.  A reconnect tears down the TCP connection,
        # causing the pending order response to be lost and every in-flight
        # order to time out with "timeout_awaiting_event".
        # This was the root cause of card d88336dc: 172 timeouts, 0 fills.
        if self._pending_orders:
            logger.debug(
                "[Preemptive] Skipping reconnect — %d order(s) in-flight",
                len(self._pending_orders),
            )
            return
        # Check heartbeat age
        last_hb = self._conn._last_heartbeat_recv
        if last_hb is None:
            return
        silence = time.monotonic() - last_hb
        if silence < _PRE_EMPTIVE_RECONNECT_SEC:
            return
        # Pre-emptive reconnect threshold exceeded
        logger.warning(
            "[Preemptive] Heartbeat silence %.1fs >= %.1fs — triggering proactive reconnect",
            silence,
            _PRE_EMPTIVE_RECONNECT_SEC,
        )
        self._trigger_preemptive_reconnect(silence)

    def _trigger_preemptive_reconnect(self, silence_sec: float) -> None:
        """Force a proactive reconnect before the degraded window opens.

        Runs ``CTraderConnection.attempt_reconnect()`` in a background thread
        because that method includes a blocking ``time.sleep()`` for backoff.
        On success, :meth:`_on_conn_connected` fires and spawns
        :meth:`_reconnect_restore` to re-authenticate.
        """
        # Atomically claim the reconnect slot to prevent double-firing
        if not self._preemptive_in_progress.is_set():
            self._preemptive_in_progress.set()
        else:
            return

        # Also set reauth flag so _on_conn_connected knows to re-auth
        if not self._reauth_in_progress.is_set():
            self._reauth_in_progress.set()

        # Check if connection is already mid-reconnect
        if self._conn._reconnect_count > 0:
            logger.info(
                "[Preemptive] Connection already attempting reconnect (count=%d) — skipping",
                self._conn._reconnect_count,
            )
            self._preemptive_in_progress.clear()
            return

        def _do_reconnect():
            try:
                logger.info("[Preemptive] Initiating reconnect (silence=%.1fs)", silence_sec)
                success = self._conn.attempt_reconnect()
                if success:
                    logger.info("[Preemptive] Reconnect succeeded — re-auth will follow")
                else:
                    logger.error("[Preemptive] Reconnect failed — connection health monitor will retry")
            except Exception as exc:
                logger.error("[Preemptive] Reconnect exception: %s", exc, exc_info=True)
            finally:
                self._preemptive_in_progress.clear()

        t = threading.Thread(
            target=_do_reconnect,
            name="ctrader-preemptive-reconnect-action",
            daemon=True,
        )
        t.start()

    # ── Connection callbacks (from CTraderConnection) ──────────────────────

    def _on_conn_connected(self, conn: CTraderConnection) -> None:
        self._connected_at = time.monotonic()
        # On reconnect (not initial), spawn re-auth thread
        if self._running and not self._reauth_in_progress.is_set():
            self._reauth_in_progress.set()
            t = threading.Thread(target=self._reconnect_restore, daemon=True)
            t.start()

    def _on_conn_disconnected(self, conn: CTraderConnection, reason) -> None:
        self._authed.clear()
        self._app_authed.clear()
        self._reauth_in_progress.clear()
        self._disconnect_at = time.monotonic()

        # Move pending orders to late-fill registry (not just
        # _disconnected_pending_orders) so that execution events arriving
        # after the disconnect can still be matched.  Previously, clearing
        # _pending_orders here caused all late fills to be dropped with
        # "[EXEC_EVENT] DROP — not in pending_orders (keys=[])".
        for req_id, (event, order) in list(self._pending_orders.items()):
            # Find the client_msg_id for this request_id
            cmsg_id = None
            for cm_id, rid in list(self._pending_client_msg_ids.items()):
                if rid == req_id:
                    cmsg_id = cm_id
                    break
            self._register_late_fill(req_id, order, cmsg_id or "")
            order.status = OrderStatus.PENDING
            order.comment = order.comment or "connection_lost_during_order"
            order.reason = "connection_lost_during_order"
            event.set()
            self._disconnected_pending_orders.append(order)
        self._pending_orders.clear()
        self._pending_client_msg_ids.clear()

    def _on_conn_feed_dead(self, conn: CTraderConnection) -> None:
        """CTraderConnection exhausted reconnect attempts — escalate."""
        if is_forex_market_closed():
            logger.debug("Feed dead during market close — not activating kill switch")
            return
        logger.critical("Feed declared dead by CTraderConnection — activating kill switch")
        self._activate_kill_switch_freeze("feed_dead:reconnect_exhausted")

    # ── Authentication ─────────────────────────────────────────────────────

    def _auth(self) -> bool:
        if self._reauth_in_progress.is_set():
            if self._authed.wait(timeout=20):
                return True
            if self._app_authed.wait(timeout=15):
                pass  # fall through to account auth
            else:
                return False

        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,
            ProtoOAApplicationAuthReq,
        )

        # App auth
        self._state_mgr.transition_to(ConnectionState.APP_AUTHENTICATING, reason="app_auth_sending")
        app_res = self._conn.send_and_wait(
            ProtoOAApplicationAuthReq(clientId=self._client_id, clientSecret=self._client_secret),
            timeout=10,
        )
        if app_res is None or not self._is_expected_auth_response(app_res, _APP_AUTH_RES_PAYLOAD_TYPE, "app"):
            self._handle_auth_failure("initial_app_auth")
            return False
        self._app_authed.set()

        # Account auth
        self._state_mgr.transition_to(ConnectionState.ACCT_AUTHENTICATING, reason="acct_auth_sending")
        acct_res = self._conn.send_and_wait(
            ProtoOAAccountAuthReq(
                ctidTraderAccountId=self._ctid_account_id,
                accessToken=self._access_token,
            ),
            timeout=10,
        )
        if acct_res is None or not self._is_expected_auth_response(acct_res, _ACCT_AUTH_RES_PAYLOAD_TYPE, "account"):
            self._handle_auth_failure("initial_acct_auth")
            return False
        self._authed.set()
        self._auth_error_count = 0
        self._auth_circuit_open = False
        self._last_successful_auth_time = time.monotonic()

        # Track token expiry
        payload = Protobuf.extract(acct_res)
        expires_in = getattr(payload, "expiresIn", None) or 86400
        if expires_in and expires_in > 0:
            self._token_expires_at = time.monotonic() + expires_in
            self._schedule_proactive_refresh(expires_in)
        # Token tracking is handled by TokenLifecycle/CredentialStore.
        # The old self._token_mgr.track_token() call is removed (BQ-1327 no-op).

        self._state_mgr.transition_to(ConnectionState.AUTHENTICATED, reason="initial_auth_complete")
        self._set_message_callback()
        self._auto_clear_kill_switch()
        return True

    def _is_expected_auth_response(self, response, expected_payload_type: int, stage: str) -> bool:
        payload_type = getattr(response, "payloadType", None)
        if payload_type == expected_payload_type:
            return True
        if payload_type == _ERROR_RES_PAYLOAD_TYPE:
            payload = Protobuf.extract(response)
            error_code = getattr(payload, "errorCode", "UNKNOWN")
            if error_code == "ALREADY_LOGGED_IN":
                # ALREADY_LOGGED_IN is expected during reconnect: the server
                # still considers the previous session active. We accept the
                # response when either (a) the local app-auth flag is set
                # (cold re-auth where the prior session just dropped) or
                # (b) a reconnect-driven re-auth is in progress. Without
                # the reauth check, `_on_conn_disconnected` clears
                # `_app_authed` before the new auth attempt, and treating
                # ALREADY_LOGGED_IN as a failure cascades into the auth
                # circuit breaker (card ecbecd01).
                if self._app_authed.is_set() or self._reauth_in_progress.is_set():
                    return True
            logger.error("%s auth rejected: %s", stage, error_code)
            return False
        return False

    def _set_message_callback(self) -> None:
        client = self._conn.client
        if client:
            client.setMessageReceivedCallback(self._on_message)

    def _auto_clear_kill_switch(self) -> None:
        if self._kill_switch is not None:
            try:
                if self._kill_switch.is_active:
                    self._kill_switch.deactivate(reason="auto_cleared_on_successful_auth")
            except Exception:  # noqa: S110 — best-effort kill-switch auto-clear; failure is logged upstream and does not block auth success
                pass

    # ── Message routing ────────────────────────────────────────────────────

    def _on_message(self, client, message):
        msg_type = message.payloadType
        self._conn.notify_heartbeat()

        # Diagnostic: log all non-heartbeat message types
        if msg_type not in (2131,):
            logger.debug(
                "[MSG] payloadType=%s pending_orders=%d",
                msg_type,
                len(self._pending_orders),
            )

        if msg_type == 2101:
            self._app_authed.set()
        elif msg_type == 2131:
            self._handle_spot_event(Protobuf.extract(message))
        elif msg_type in _EXECUTION_EVENT_PAYLOAD_TYPES:
            self._handle_execution_event(Protobuf.extract(message), message)
        elif msg_type == _ORDER_ERROR_EVENT_PAYLOAD_TYPE:
            self._handle_order_error_event(Protobuf.extract(message), message)
        elif msg_type == 2142:
            payload = Protobuf.extract(message)
            if not self._handle_pending_order_error(payload, message):
                self._handle_error(payload, message)
        elif msg_type == 2103:
            self._authed.set()
            self._auth_error_count = 0
            self._auth_circuit_open = False
            self._last_successful_auth_time = time.monotonic()
        elif msg_type in (2128, 2130):
            pass

    def _handle_spot_event(self, message) -> None:
        if message is None:
            return
        symbol_id = message.symbolId
        raw_bid, raw_ask = message.bid, message.ask
        if raw_bid == 0 and raw_ask == 0:
            return

        # cTrader encodes ALL raw prices (bid/ask + trendbar OHLC) at
        # 5-decimal precision internally, regardless of the symbol's display
        # digits. Any symbol with display digits < 5 (e.g. USDJPY digits=3,
        # XAUUSD digits=2) must use tick_digits=5 to avoid price inflation.
        # Universal fix: always force 5 when digits < 5, no symbol-name
        # heuristic needed (supersedes the prior JPY-only guard).
        digits = self._symbol_digits.get(symbol_id, 5)
        if symbol_id not in self._symbol_digits:
            logger.warning("Tick decode: no digits for symbol_id=%s, using default 5", symbol_id)
        tick_digits = 5 if digits < 5 else digits
        divisor = 10**tick_digits
        bid, ask = raw_bid / divisor, raw_ask / divisor

        if raw_bid == 0 or raw_ask == 0:
            last = self._ticks_by_id.get(symbol_id)
            if last:
                bid = last.bid if raw_bid == 0 else bid
                ask = last.ask if raw_ask == 0 else ask
            else:
                return

        if bid >= ask or (ask - bid) > ((bid + ask) / 2) * 0.01:
            return

        ts_raw = message.timestamp / 1000
        timestamp = datetime.fromtimestamp(
            ts_raw if ts_raw > 0 else datetime.now(timezone.utc).timestamp(),
            tz=timezone.utc,
        )
        tick = Tick(symbol_id=symbol_id, bid=bid, ask=ask, timestamp=timestamp)

        self._last_tick_recv_monotonic = time.monotonic()
        broker_name = self._id_to_name.get(symbol_id, str(symbol_id))
        normalized = _normalize_symbol_name(broker_name)

        with self._lock:
            self._ticks[normalized] = tick
            self._ticks_by_id[symbol_id] = tick
            self._tick_counts[normalized] = self._tick_counts.get(normalized, 0) + 1

        for cb in self._tick_callbacks:
            try:
                cb(tick)
            except Exception as exc:
                logger.error("Tick callback error: %s", exc)

    # ── Subscription ───────────────────────────────────────────────────────

    def subscribe(self, symbol_name: str) -> bool:
        symbol_id = self._resolve_name_to_id(symbol_name)
        return self._subscribe_by_id(symbol_id) if symbol_id else False

    def unsubscribe(self, symbol_name: str) -> bool:
        symbol_id = self._resolve_name_to_id(symbol_name)
        return self._unsubscribe_by_id(symbol_id) if symbol_id else False

    def on_tick(self, callback: Callable[[Tick], None]):
        self._tick_callbacks.append(callback)

    def register_callback(self, event_name: str, fn: Callable) -> None:
        # Dedupe: if the same function is already registered for this event,
        # don't append a second copy. Without this, multiple orders sharing
        # the same callback function cause the callback to fire repeatedly
        # for each subsequent registration.
        existing = self._callbacks.setdefault(event_name, [])
        if fn not in existing:
            existing.append(fn)

    def _trigger_callback(self, event_name: str, *args) -> None:
        for cb in list(self._callbacks.get(event_name, [])):
            try:
                self._callback_executor.submit(cb, *args)
            except RuntimeError:
                pass

    # ── Late-fill registry ───────────────────────────────────────────────

    def _register_late_fill(self, request_id: str, order: Order, client_msg_id: str) -> None:
        """Register a timed-out or disconnected order for late-fill matching.

        Keeps the order available in ``_late_fill_registry`` for
        ``_LATE_FILL_TTL_SEC`` seconds so that execution events arriving
        after the timeout/disconnect can still be correlated.
        """
        expiry = time.monotonic() + _LATE_FILL_TTL_SEC
        self._late_fill_registry[request_id] = (expiry, order, client_msg_id)
        logger.info(
            "[LATE_FILL] Registered request_id=%s for %ds grace (reason=%s)",
            request_id,
            _LATE_FILL_TTL_SEC,
            getattr(order, "reason", "unknown"),
        )

    def _lookup_late_fill(self, request_id: str) -> Order | None:
        """Check the late-fill registry for a timed-out order.

        Returns the ``Order`` if found and not expired, else ``None``.
        Cleans up expired entries as a side effect.
        """
        if not self._late_fill_registry:
            return None
        # Lazy cleanup of expired entries
        now = time.monotonic()
        expired = [rid for rid, (exp, _, _) in self._late_fill_registry.items() if now >= exp]
        for rid in expired:
            self._late_fill_registry.pop(rid, None)
        entry = self._late_fill_registry.get(request_id)
        if entry is None:
            return None
        _, order, _ = entry
        return order

    def _resolve_late_fill(self, request_id: str) -> tuple[threading.Event, Order] | None:
        """Pop a late-fill entry and return a synthetic (Event, Order) pair.

        This is used when a late execution event matches a timed-out order.
        The order is removed from the registry and its status is updated.
        """
        entry = self._late_fill_registry.pop(request_id, None)
        if entry is None:
            return None
        _, order, _ = entry
        event = threading.Event()  # Synthetic event — caller already has the result
        event.set()
        return event, order

    def _consume_order_state_across_all_maps(
        self,
        client_order_id: str,
        client_msg_id: str,
    ) -> None:
        """ATOMIC: remove every piece of state for this order across ALL maps.

        Card 8ad140c5 finding #1 — sprint reina-2026-08-18-106 (HIGH fix).

        Background
        ----------
        new_order() timeout path registers an order in BOTH ``_pending_orders``
        (kept under clientOrderId) AND ``_late_fill_registry`` (kept under
        request_id, carrying the client_msg_id). It also links the
        ``client_msg_id`` → ``request_id`` map ``_pending_client_msg_ids``.

        Pre-fix: when a terminal event matched via the pending path, the
        spot feed popped ``_pending_orders`` but left the registry entry
        alive.  A subsequent ORDER_ERROR / ORDER_REJECTED arriving within
        the TTL window would then match the registry entry via the
        clientMsgId fallback, overwrite the order's confirmed FILLED
        status with REJECTED, and fire a second ``on_order_rejected``
        callback — corrupting the live accounting. The mirror image
        (terminal via registry path, then a duplicate pending-path event)
        was equally broken.

        Post-fix
        ---------
        A single terminal resolution — regardless of which path matched
        (pending-path or registry-path) — must consume the state for
        THIS order across ALL THREE structures, exactly once:

          * ``_pending_orders``   — by client_order_id (request_id)
          * ``_late_fill_registry`` — by client_order_id AND/OR by iterating
            on client_msg_id (whichever holds it)
          * ``_pending_client_msg_ids`` — by client_msg_id (reverse map)
            AND by client_order_id (legacy double-key)

        Idempotency: calling this method a second time for the same order
        is a no-op (no exceptions, no overwrites). A duplicate terminal
        event arriving AFTER state has been consumed is treated as a
        reverse-ordering duplicate and falls through to the
        ``_fill_cb_fired`` dedupe / unmatched-DROP paths — never
        overwrites a confirmed terminal status.

        Ordering guarantees
        -------------------
        * pending-path → terminal:  pops pending + registry + reverse maps
        * registry-path → terminal:  pops registry + pending + reverse maps
        * Both orderings leave ALL THREE structures empty for this order
          on return.
        """
        # 1. Pending path — pop by clientOrderId if present.
        if client_order_id:
            self._pending_orders.pop(client_order_id, None)
            # _pending_client_msg_ids is double-keyed historically
            # (some call sites use client_msg_id as the value, others
            # use client_order_id directly). Pop both to be safe.
            self._pending_client_msg_ids.pop(client_order_id, None)

        # 2. Late-fill registry — pop by clientOrderId if present.
        # Defensive: ``_late_fill_registry`` was added after the legacy
        # ``_handle_pending_order_error`` test fixtures were written, so
        # some legacy fixtures (e.g. ``test_timeout_race_errorcode.py``)
        # do not initialise it. The helper's contract is "clear all 3
        # structures by both keys" — if a structure doesn't exist, the
        # consume is trivially empty for that structure. ``getattr``
        # with a default empty dict preserves idempotency for both
        # the initialised and un-initialised cases (no-op when
        # absent, no-op when already popped).
        late_registry = getattr(self, "_late_fill_registry", None)
        popped_via_cid = False
        if late_registry is not None and client_order_id and client_order_id in late_registry:
            late_registry.pop(client_order_id, None)
            popped_via_cid = True

        # 3. Late-fill registry — pop by iterating client_msg_id (the
        #    timeout path registers request_id as the key, but the
        #    registry value carries client_msg_id for envelope matching).
        if late_registry is not None and client_msg_id:
            for rid, (_, _, cmid) in list(late_registry.items()):
                if cmid == client_msg_id:
                    late_registry.pop(rid, None)
                    break

        # 4. Reverse map — pop by client_msg_id (the new_order path
        #    inserts client_msg_id → request_id).
        if client_msg_id:
            self._pending_client_msg_ids.pop(client_msg_id, None)

        # Single log line so operators can trace atomic consumption.
        # Cheap enough to keep on the hot path; matches the existing
        # log-volume profile of the spot feed.
        logger.info(
            "[EXEC_EVENT] atomic-consume: clientOrderId=%r clientMsgId=%r "
            "registry_popped_via_cid=%s (pending=%d registry=%d reverse=%d)",
            client_order_id,
            client_msg_id,
            popped_via_cid,
            len(self._pending_orders),
            len(late_registry) if late_registry is not None else 0,
            len(self._pending_client_msg_ids),
        )

    def get_tick(self, symbol_name: str) -> Optional[Tick]:
        with self._lock:
            return self._ticks.get(_normalize_symbol_name(symbol_name))

    def get_tick_by_id(self, symbol_id: int) -> Optional[Tick]:
        with self._lock:
            return self._ticks_by_id.get(symbol_id)

    def get_all_ticks(self) -> dict[str, Tick]:
        with self._lock:
            return dict(self._ticks)

    def get_spread(self, symbol_name: str) -> Optional[float]:
        tick = self.get_tick(symbol_name)
        return tick.spread if tick else None

    def _subscribe_by_id(self, symbol_id: int) -> bool:
        if symbol_id not in self._symbol_digits:
            self._fetch_symbol_details(symbol_id)
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.symbolId.append(symbol_id)
        req.subscribeToSpotTimestamp = True
        reactor.callFromThread(self._conn.send, req)
        self._subscribed_symbol_ids.add(symbol_id)
        return True

    def _unsubscribe_by_id(self, symbol_id: int) -> bool:
        if symbol_id not in self._subscribed_symbol_ids:
            return True
        req = ProtoOAUnsubscribeSpotsReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.symbolId.append(symbol_id)
        try:
            reactor.callFromThread(self._conn.send, req)
        except Exception:  # noqa: S110 — fire-and-forget unsubscribe; tx queue teardown is non-critical during cleanup
            pass
        self._subscribed_symbol_ids.discard(symbol_id)
        return True

    # ── Symbol management ──────────────────────────────────────────────────

    def _resolve_name_to_id(self, name: str) -> Optional[int]:
        return self._name_to_id.get(_normalize_symbol_name(name))

    def resolve_symbol_id(self, name: str) -> int:
        sid = self._resolve_name_to_id(name)
        if sid is None:
            raise ValueError(f"Symbol '{name}' not found. Known: {sorted(self._name_to_id.keys())}")
        return sid

    def _fetch_symbol_list(self) -> bool:
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._ctid_account_id
        response = self._conn.send_and_wait(req, timeout=15)
        if response is None:
            return False
        payload = Protobuf.extract(response)
        if payload is None or not hasattr(payload, "symbol"):
            return False
        for sym in payload.symbol:
            self._id_to_name[sym.symbolId] = sym.symbolName
            self._name_to_id[_normalize_symbol_name(sym.symbolName)] = sym.symbolId
        return len(self._name_to_id) > 0

    def _fetch_symbol_details(self, symbol_id: int) -> bool:
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.symbolId.append(symbol_id)
        response = self._conn.send_and_wait(req, timeout=10)
        if response is None:
            return False
        payload = Protobuf.extract(response)
        if payload is None or not payload.symbol:
            return False
        sym = payload.symbol[0]
        self._symbols[symbol_id] = SymbolInfo(
            symbol_id=symbol_id,
            name=self._id_to_name.get(symbol_id, str(symbol_id)),
            pip_size=10 ** (-sym.digits),
            digits=sym.digits,
            lot_size=sym.lotSize if sym.lotSize else 100_000,
            min_volume=sym.minVolume if sym.minVolume else 0,
            max_volume=sym.maxVolume if sym.maxVolume else 0,
            step_volume=sym.stepVolume if sym.stepVolume else 1,
        )
        self._symbol_digits[symbol_id] = sym.digits
        return True

    def _populate_static_symbols(self):
        for sid, (name, digits) in {
            1: ("EURUSD", 5),
            2: ("GBPUSD", 5),
            4: ("USDJPY", 3),
        }.items():
            self._symbols[sid] = SymbolInfo(sid, name, 10 ** (-digits), digits, lot_size=100_000)
            self._symbol_digits[sid] = digits
            self._id_to_name[sid] = name
            self._name_to_id[_normalize_symbol_name(name)] = sid

    # ── Trendbars / Historical Bars ────────────────────────────────────────

    def fetch_historical_bars(self, symbol: str, timeframe: str, count: int) -> list:
        """Fetch historical bars through the existing authenticated connection.

        Single-connection replacement for CTraderOpenApiClient.get_trendbars().
        Uses the same TCP session that powers spot ticks and order execution.

        Args:
            symbol: Normalized symbol name, e.g. "GBPUSD"
            timeframe: "M15", "H1", etc. (same codes as CTraderOpenApiClient)
            count: Number of bars to fetch

        Returns list of Bar objects.
        """
        _TF_MAP = {
            "M1": 1,
            "M5": 5,
            "M15": 15,
            "M30": 30,
            "H1": 60,
            "H4": 240,
            "D1": 1440,
        }
        period_minutes = _TF_MAP.get(timeframe.upper())
        if period_minutes is None:
            raise ValueError(f"Unknown timeframe '{timeframe}'. Valid: {list(_TF_MAP.keys())}")
        return self.fetch_trendbars(symbol, period_minutes, count)

    def fetch_trendbars(self, symbol: str, period_minutes: int, count: int) -> list:
        from backtest.engine import Bar
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsReq

        if not self._conn.is_connected:
            return []
        symbol_id = self._resolve_name_to_id(symbol)
        if symbol_id is None:
            return []

        PERIOD_MAP = {
            1: 1,
            2: 2,
            3: 3,
            4: 4,
            5: 5,
            10: 6,
            15: 7,
            30: 8,
            60: 9,
            240: 10,
            720: 11,
            1440: 12,
            10080: 13,
        }
        period_enum = PERIOD_MAP.get(period_minutes)
        if period_enum is None:
            return []

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.symbolId = symbol_id
        req.period = period_enum
        req.count = count
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        req.toTimestamp = now_ms
        req.fromTimestamp = now_ms - (count * period_minutes * 60 * 1000)

        response = self._conn.send_and_wait(req, timeout=15)
        if response is None:
            return []
        payload = Protobuf.extract(response)
        if payload is None:
            return []

        # Determine the correct divisor for trendbar protobuf values.
        # cTrader encodes JPY-pair trendbar OHLC at 5-digit precision
        # internally, even though the symbol's digits field reports 3.
        # Using 10**3 for a 10**5-encoded payload leaves prices 100x inflated.
        # Non-JPY 5-digit symbols (EURUSD/GBPUSD) are unaffected since
        # digits=5 matches the protobuf encoding.
        symbol_name = self._symbol_name_for_id(symbol_id)
        digits = self._symbol_digits.get(symbol_id, 5)
        is_jpy = "JPY" in symbol_name.upper()
        tb_digits = 5 if digits < 5 else digits
        d = float(10**tb_digits)

        bars = []
        for tb in getattr(payload, "trendbar", []):
            bar_time = datetime.fromtimestamp(getattr(tb, "utcTimestampInMinutes", 0) * 60, tz=timezone.utc)
            low_raw = getattr(tb, "low", 0)
            high_decoded = round((low_raw + getattr(tb, "deltaHigh", 0)) / d, 5)

            # Magnitude sanity guard for JPY pairs
            if is_jpy and high_decoded > 1000:
                logger.warning(
                    "JPY pair %s trendbar high=%.5f exceeds 1000 after decode "
                    "(raw low=%d, divisor=%.0f, digits=%d) — possible precision mismatch",
                    symbol_name,
                    high_decoded,
                    low_raw,
                    d,
                    digits,
                )

            bars.append(
                Bar(
                    time=bar_time,
                    open=round((low_raw + getattr(tb, "deltaOpen", 0)) / d, 5),
                    high=high_decoded,
                    low=round(low_raw / d, 5),
                    close=round((low_raw + getattr(tb, "deltaClose", 0)) / d, 5),
                    volume=getattr(tb, "volume", 0),
                )
            )
        return bars

    # ── Order execution ────────────────────────────────────────────────────

    def _symbol_name_for_id(self, symbol_id: int) -> str:
        return _normalize_symbol_name(self._id_to_name.get(symbol_id, str(symbol_id)))

    def _round_price(self, symbol_id, value):
        """Round a price value to the symbol's allowed decimal places.

        cTrader rejects orders whose SL/TP/limit/stop prices exceed the
        symbol's digit precision (e.g. 5 digits for EURUSD, 3 for USDJPY).
        This prevents INVALID_REQUEST rejections from floating-point noise.
        """
        if value is None:
            return None
        digits = self._symbol_digits.get(symbol_id, 5)
        return round(float(value), digits)

    def new_order(
        self,
        symbol_id,
        side,
        volume,
        *,
        order_type=ProtoOAOrderType.MARKET,
        price=None,
        sl=None,
        tp=None,
        time_in_force=ProtoOATimeInForce.GOOD_TILL_CANCEL,
        comment="",
        timeout=_ORDER_TIMEOUT_SEC,
    ) -> Order:
        # P5A: defense-in-depth permission check. Must block before any broker
        # mutation or reactor dispatch.
        if self._permission_policy is not None:
            allowed, reason = self._permission_policy.can_send_order()
            if not allowed:
                logger.warning("Order blocked by permission policy: %s", reason)
                request_id = uuid.uuid4().hex
                order = Order(
                    order_id=request_id,
                    symbol=self._symbol_name_for_id(symbol_id),
                    direction=TradeDirection.LONG if side == ProtoOATradeSide.BUY else TradeDirection.SHORT,
                    order_type={
                        ProtoOAOrderType.LIMIT: OrderType.LIMIT,
                        ProtoOAOrderType.STOP: OrderType.STOP,
                    }.get(order_type, OrderType.MARKET),
                    volume=self._volume_calc.volume_to_lots(symbol_id, volume),
                    price=price,
                    stop_loss=sl,
                    take_profit=tp,
                    status=OrderStatus.REJECTED,
                    comment=comment,
                )
                order.reason = reason
                return order

        request_id = uuid.uuid4().hex
        order = Order(
            order_id=request_id,
            symbol=self._symbol_name_for_id(symbol_id),
            direction=TradeDirection.LONG if side == ProtoOATradeSide.BUY else TradeDirection.SHORT,
            order_type={
                ProtoOAOrderType.LIMIT: OrderType.LIMIT,
                ProtoOAOrderType.STOP: OrderType.STOP,
            }.get(order_type, OrderType.MARKET),
            volume=self._volume_calc.volume_to_lots(symbol_id, volume),
            price=price,
            stop_loss=sl,
            take_profit=tp,
            status=OrderStatus.PENDING,
            comment=comment,
        )
        if not self._state_mgr.is_operational:
            order.reason = "not_connected"
            return order

        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.symbolId = symbol_id
        req.orderType = order_type
        req.tradeSide = side
        req.volume = volume
        req.timeInForce = time_in_force
        req.clientOrderId = request_id
        price = self._round_price(symbol_id, price)
        sl = self._round_price(symbol_id, sl)
        tp = self._round_price(symbol_id, tp)
        if order_type == ProtoOAOrderType.LIMIT and price is not None:
            req.limitPrice = price
        elif order_type == ProtoOAOrderType.STOP and price is not None:
            req.stopPrice = price
        if sl is not None:
            req.stopLoss = sl
        if tp is not None:
            req.takeProfit = tp
        # cTrader enforces a maximum comment length (100 chars). Truncate
        # to prevent ORDER_ERROR rejections when signal.rationale is long.
        if comment:
            req.comment = comment[:100]

        event = threading.Event()
        client_msg_id = f"order_{uuid.uuid4().hex}"
        self._pending_orders[request_id] = (event, order)
        self._pending_client_msg_ids[client_msg_id] = request_id

        client = self._conn.client
        if client is None:
            order.status = OrderStatus.REJECTED
            order.comment = "not_connected"
            return order

        def do_send():
            d = client.send(req, clientMsgId=client_msg_id, responseTimeoutInSeconds=timeout)

            def on_error(failure):
                logger.warning("Order send deferred error: %s", failure)
                # The error event handler (_handle_pending_order_error) may
                # have already set order status via a broker error event.
                # If not, mark the order as rejected here so the caller gets
                # a terminal status instead of a dangling PENDING.
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.REJECTED
                    order.reason = "deferred_error"
                    order.comment = "deferred_error"
                    self._trigger_callback(
                        "on_order_rejected",
                        order,
                        None,
                        "deferred_error",
                    )
                else:
                    # Order already processed by _handle_pending_order_error
                    # with a real broker errorCode. Log the deferred error
                    # for diagnostics but don't overwrite the real reason.
                    logger.info(
                        "Deferred error fired after broker event already handled "
                        "order=%s (current reason=%s) — not overwriting",
                        request_id,
                        getattr(order, "reason", ""),
                    )
                # Set the event so the calling thread doesn't block further.
                event.set()

            d.addCallbacks(lambda _: None, on_error)

        reactor.callFromThread(do_send)

        # Use timeout + 5 to give the deferred's responseTimeoutInSeconds
        # callback (on_error) a chance to fire before we fall through to
        # the local timeout path.  Without this margin, event.wait expires
        # at the same instant as the deferred timeout, creating a race
        # where on_error never gets to run before the local timeout
        # handler marks the order as "timeout_awaiting_event".
        if not event.wait(timeout=timeout + 5):
            # Both the deferred timeout AND the local wait expired — the
            # reactor thread is likely stuck or massively delayed.
            # Move to late-fill registry so execution events arriving later
            # can still be matched and the order status corrected from
            # INDETERMINATE → FILLED (or → REJECTED with real errorCode).
            # The registry entry has a TTL of _LATE_FILL_TTL_SEC (120s) to
            # prevent unbounded growth.
            self._register_late_fill(request_id, order, client_msg_id)
            # Card ce6de98d (B): INDETERMINATE timeout semantics.
            # Previously this path marked order.status = REJECTED and fired
            # on_order_rejected, which the engine's late-fill callback
            # translated into signals_failed_live += 1. With the bug fixed,
            # 4 of 4 fills today are unaccounted because the broker had
            # already filled the orders but the deferred timeout fired
            # before the execution event arrived — every fill was
            # incorrectly counted as a failure (broker +$301.36, health
            # shows signals=9 trades=0).
            #
            # New behaviour: leave order.status = PENDING (non-terminal,
            # downstream treats as still-awaiting-ack) and tag reason =
            # "indeterminate_awaiting_event". Late-fill events arriving
            # via the registry will upgrade to FILLED with the real
            # execution price; late error events via
            # _handle_pending_order_error will upgrade to REJECTED with
            # the real errorCode. We do NOT fire on_order_rejected here —
            # that would mark a still-pending order as failed prematurely
            # and double-count fills that arrive via the late path.
            #
            # Don't overwrite if _handle_pending_order_error already set a
            # real broker errorCode (race won by the broker event handler).
            if order.status == OrderStatus.PENDING:
                order.reason = _INDETERMINATE_TIMEOUT_REASON
                order.comment = _INDETERMINATE_TIMEOUT_REASON
                # No on_order_rejected — the order is still in flight.
        # AC2/AC3: Brief grace period for late-arriving broker error events.
        # When the deferred timeout fires just before a broker rejection
        # arrives, the order.reason is "deferred_error" instead of the real
        # errorCode (e.g. TRADING_BAD_STOPS). Poll briefly (up to 500ms) to
        # let the reactor thread process the 2142 error event and update
        # the order via _handle_pending_order_error.
        # Card ce6de98d (B): also include _INDETERMINATE_TIMEOUT_REASON so
        # the grace poll can be exited when the broker finally sends the
        # real error event for a timed-out order.
        _PRE_GRACE_REASONS = (
            "deferred_error",
            "timeout_awaiting_event",
            _INDETERMINATE_TIMEOUT_REASON,
        )
        if getattr(order, "reason", "") in _PRE_GRACE_REASONS:
            _GRACE_POLL_SEC = 0.5
            _GRACE_INTERVAL = 0.05
            grace_end = time.monotonic() + _GRACE_POLL_SEC
            while time.monotonic() < grace_end:
                if getattr(order, "reason", "") not in _PRE_GRACE_REASONS:
                    logger.info(
                        "Late broker error received during grace poll: order=%s reason=%s",
                        request_id,
                        getattr(order, "reason", ""),
                    )
                    break
                time.sleep(_GRACE_INTERVAL)
        return order

    def send_order(
        self,
        symbol,
        direction,
        order_type,
        volume,
        price=None,
        stop_loss=None,
        take_profit=None,
        comment="",
    ) -> Order:
        symbol_id = self.resolve_symbol_id(symbol)
        side = ProtoOATradeSide.BUY if direction == TradeDirection.LONG else ProtoOATradeSide.SELL
        proto_type = {
            OrderType.MARKET: ProtoOAOrderType.MARKET,
            OrderType.LIMIT: ProtoOAOrderType.LIMIT,
            OrderType.STOP: ProtoOAOrderType.STOP,
        }.get(order_type, ProtoOAOrderType.MARKET)
        return self.new_order(
            symbol_id,
            side,
            self._volume_calc.lots_to_volume(symbol_id, volume),
            order_type=proto_type,
            price=price,
            sl=stop_loss,
            tp=take_profit,
            comment=comment,
        )

    # ------------------------------------------------------------------
    # Phase 6: Broker-mutating methods — ALL must carry a policy guard.
    # Currently guarded: cancel_order, amend_order, amend_sl_tp,
    #                    close_position, new_order (P5A, above).
    # If you add a NEW method that sends a mutating request to the broker
    # (e.g. hedge_position, liquidate, partial_fill_amend), you MUST add:
    #     if self._permission_policy is not None:
    #         allowed, reason = self._permission_policy.can_<op>()
    #         if not allowed:
    #             logger.warning("<op> blocked by policy: %s", reason)
    #             return False
    # ------------------------------------------------------------------

    def cancel_order(self, order_id, *, timeout=_ORDER_TIMEOUT_SEC) -> bool:
        # Phase 6: policy gate for broker-mutating operations
        if self._permission_policy is not None:
            allowed, reason = self._permission_policy.can_cancel_order()
            if not allowed:
                logger.warning("cancel_order blocked by policy: %s", reason)
                return False
        req = ProtoOACancelOrderReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.orderId = order_id
        return self._conn.send_and_wait(req, timeout=timeout, prefix="order") is not None

    def amend_order(
        self,
        order_id,
        *,
        price=None,
        sl=None,
        tp=None,
        symbol_id=None,
        timeout=_ORDER_TIMEOUT_SEC,
    ) -> bool:
        # Phase 6: policy gate for broker-mutating operations
        if self._permission_policy is not None:
            allowed, reason = self._permission_policy.can_amend_order()
            if not allowed:
                logger.warning("amend_order blocked by policy: %s", reason)
                return False
        req = ProtoOAAmendOrderReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.orderId = order_id
        if symbol_id:
            price = self._round_price(symbol_id, price)
            sl = self._round_price(symbol_id, sl)
            tp = self._round_price(symbol_id, tp)
        if price is not None:
            req.limitPrice = price
        if sl is not None:
            req.stopLoss = sl
        if tp is not None:
            req.takeProfit = tp
        return self._conn.send_and_wait(req, timeout=timeout, prefix="order") is not None

    def amend_sl_tp(self, position_id, sl, tp, *, symbol_id=None, timeout=_AMEND_TIMEOUT_SEC) -> bool:
        # Platform constraint: ProtoOAAmendPositionSLTPReq only supports a single SL and single TP
        # per position. TP2/TP3 are managed in-software via Position dataclass + position_monitor
        # ratcheting. See docs/forex/tp-sl-chain-trace.md §F4.
        # Phase 6: policy gate for broker-mutating operations
        if self._permission_policy is not None:
            allowed, reason = self._permission_policy.can_amend_sl_tp()
            if not allowed:
                logger.warning("amend_sl_tp blocked by policy: %s", reason)
                return False
        # Wait-for-response amend: we need the actual broker outcome so callers
        # can detect rejection (e.g. TRADING_BAD_STOPS) and react. Serialization
        # via _amend_lock plus a 1s cooldown prevents back-to-back amend floods.
        with self._amend_lock:
            req = ProtoOAAmendPositionSLTPReq()
            req.ctidTraderAccountId = self._ctid_account_id
            req.positionId = position_id
            if symbol_id:
                sl = self._round_price(symbol_id, sl)
                tp = self._round_price(symbol_id, tp)
            req.stopLoss = sl
            req.takeProfit = tp

            res = self._conn.send_and_wait(req, timeout=timeout, prefix="amend")
            if res is None:
                logger.warning(
                    "Amend SL/TP timeout for position %s (sl=%s tp=%s): no response from broker",
                    position_id,
                    sl,
                    tp,
                )
                return False

            payload = Protobuf.extract(res) if hasattr(res, "payloadType") else res
            error_code = getattr(payload, "errorCode", None)
            description = getattr(payload, "description", "") or ""
            if error_code:
                # If the broker response also reports applied SL/TP values then
                # the amend may have partially succeeded (one side applied, the
                # other rejected). Log the partial state so operators can
                # reconcile manually.
                applied_sl = getattr(payload, "stopLoss", None)
                applied_tp = getattr(payload, "takeProfit", None)
                if applied_sl is not None or applied_tp is not None:
                    logger.warning(
                        "Partial amend SL/TP for position %s: errorCode=%r description=%r "
                        "requested sl=%s tp=%s applied sl=%s tp=%s",
                        position_id,
                        error_code,
                        description,
                        sl,
                        tp,
                        applied_sl,
                        applied_tp,
                    )
                else:
                    logger.warning(
                        "Amend SL/TP rejected for position %s: errorCode=%r description=%r sl=%s tp=%s",
                        position_id,
                        error_code,
                        description,
                        sl,
                        tp,
                    )
                return False

            # 1s cooldown so the next amend doesn't immediately re-saturate the
            # connection. Held inside the lock to serialize spacing.
            time.sleep(1.0)
            return True

    def close_position(
        self,
        position_id,
        volume,
        *,
        symbol_id: int | None = None,
        timeout: float = _ORDER_TIMEOUT_SEC,
    ) -> bool:
        """Close (fully or partially) an open position.

        ``volume`` accepts either of:

        * ``int``  — already-converted cTrader raw volume (lots × lot_size).
          Passed through unchanged. This is the historical contract.
        * ``float`` — lots. Converted to raw volume via the existing
          :class:`VolumeCalculator` using ``symbol_id``. ``symbol_id``
          is required when ``volume`` is a float (otherwise the
          per-symbol lot_size is unknown and we cannot convert).

        Args:
            position_id: cTrader position ID.
            volume: Volume to close, as either raw ``int`` (cents/units)
                or ``float`` lots.
            symbol_id: Symbol ID of the position. **Required** when
                ``volume`` is a ``float``; ignored when ``volume`` is
                an ``int``.
            timeout: Per-request timeout in seconds.

        Returns:
            ``True`` if cTrader acknowledged the close request, else ``False``.
        """
        # Phase 6: policy gate for broker-mutating operations
        if self._permission_policy is not None:
            allowed, reason = self._permission_policy.can_close_position()
            if not allowed:
                logger.warning("close_position blocked by policy: %s", reason)
                return False
        if isinstance(volume, float):
            if symbol_id is None:
                raise ValueError(
                    "close_position: symbol_id is required when volume is "
                    "a float (lots); pass the position's symbol_id to "
                    "convert lots → raw volume via the per-symbol lot_size."
                )
            volume = self._volume_calc.lots_to_volume(symbol_id, volume)
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._ctid_account_id
        req.positionId = position_id
        req.volume = volume
        return self._conn.send_and_wait(req, timeout=timeout, prefix="order") is not None

    def reconcile(self, timeout=_RECONCILE_TIMEOUT_SEC) -> list[Position]:
        if not self._state_mgr.is_operational:
            return []
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self._ctid_account_id
        res = self._conn.send_and_wait(req, timeout=timeout, prefix="qry")
        if res is None:
            return []
        payload = Protobuf.extract(res) if hasattr(res, "payloadType") else res
        positions = []
        for raw in getattr(payload, "position", []):
            try:
                td = getattr(raw, "tradeData", None)
                positions.append(
                    Position(
                        position_id=str(getattr(raw, "positionId", "")),
                        symbol=self._symbol_name_for_id(getattr(td, "symbolId", 0)),
                        direction=TradeDirection.LONG
                        if getattr(td, "tradeSide", 0) == ProtoOATradeSide.BUY
                        else TradeDirection.SHORT,
                        volume=self._volume_calc.volume_to_lots(
                            getattr(td, "symbolId", 0), int(getattr(td, "volume", 0))
                        ),
                        entry_price=getattr(raw, "price", 0.0),
                        current_price=getattr(raw, "price", 0.0),
                        stop_loss=getattr(raw, "stopLoss", None) or None,
                        take_profit=getattr(raw, "takeProfit", None) or None,
                        status=PositionStatus.OPEN,
                    )
                )
            except Exception as exc:
                logger.warning("Reconcile parse error: %s", exc)
        return positions

    # ── Execution event handlers ───────────────────────────────────────────

    def _handle_execution_event(self, message, envelope=None) -> None:
        """Route an inbound execution event (payload 2126 / 2151).

        Card ce6de98d (A) — TERMINAL-ONLY STATE MACHINE.

        The previous implementation popped ``_pending_orders`` for ANY
        execution type, then dispatched only on ``ORDER_CANCELLED`` /
        ``ORDER_REJECTED``, falling through to a FILLED path for every
        other etype. That meant ACCEPT(2) fired on_order_filled on the
        ACCEPT payload (wrong price fallback chain), and the real
        FILLED(3) later rescued via late_fill_registry producing a
        duplicate on_order_filled. Late re-emissions (+4m observed) then
        dropped because the pending entry was already popped.

        New behaviour:
        - Look up the order in ``_pending_orders`` (or fall back to
          late_fill_registry, as before).
        - If the etype is NOT in the terminal set (``_TERMINAL_EXEC_TYPES``)
          — e.g. ORDER_ACCEPTED(2), ORDER_REPLACED(4),
          ORDER_PARTIAL_FILL(11), ORDER_CANCEL_REJECTED(8),
          SWAP/DEPOSIT_WITHDRAW/BONUS_DEPOSIT_WITHDRAW — log the event as
          informational, DO NOT pop the pending entry, DO NOT fire any
          callback, DO NOT set the event. The pending order stays available
          for a future terminal event to match.
        - If the etype IS terminal, pop the pending entry and dispatch:
          ORDER_CANCELLED, ORDER_REJECTED, ORDER_EXPIRED (mapped to CANCELLED
          with reason="order_expired" since OrderStatus has no EXPIRED
          value), or ORDER_FILLED with the FILLED payload's executionPrice.
        - Card ce6de98d (C): on_order_filled dedupe. Before firing
          on_order_filled, check ``order._fill_cb_fired`` — if already True,
          log "DROP — duplicate FILLED re-emission" and return without
          firing. Applies to both the live path and the late-registry
          upgrade path. Duplicate FILLED re-emissions (e.g. +4m after the
          first fill) land in the unmatched handling WITHOUT re-firing the
          callback.
        """
        order_payload = getattr(message, "order", None)
        client_order_id = getattr(order_payload, "clientOrderId", "") if order_payload else ""
        etype = getattr(message, "executionType", None)
        client_msg_id = getattr(envelope, "clientMsgId", "") if envelope else ""
        logger.info(
            "[EXEC_EVENT] clientOrderId=%r execType=%s has_order=%s pending_keys=%s",
            client_order_id,
            etype,
            order_payload is not None,
            list(self._pending_orders.keys()) if self._pending_orders else "[]",
        )
        event: threading.Event
        order: Order
        # Card ce6de98d (A): RESOLVE THE ORDER WITHOUT POPPING.
        # The lookup now peeks at _pending_orders and the late-fill
        # registry WITHOUT removing the entries. The pop happens later,
        # only when we confirm the etype is terminal. For informational
        # etypes (ACCEPT, REPLACED, etc.) the entry remains available so
        # a subsequent terminal event can still match.
        from_late_registry = False
        # Card ce6de98d (E) / 8ad140c5 finding #3: track whether we encountered
        # (and skipped) an expired late-fill registry entry while looking up
        # the order — independent of WHICH key matched (clientOrderId OR
        # clientMsgId). We bump ``_unmatched_late_fills_count`` in the DROP
        # branch below so operators can see how often broker events arrive
        # after the registry's 120s grace window (additive metric).
        #
        # Pre-fix bug: the counter only fired via the clientMsgId iteration
        # path (``post_ttl_expired_seen`` was set inside the iteration loop
        # below). A post-TTL event matched ONLY by clientOrderId (no
        # clientMsgId) would not bump the counter, leaving operators blind
        # to a real class of broker late events.
        #
        # Post-fix: track expiry per-path AND aggregate. We use a set of
        # registry keys we observed as expired so we never double-count if
        # both paths land on the same expired entry, and so we still bump
        # exactly once when exactly one path saw expiry.
        expired_keys_seen: set[str] = set()
        if not client_order_id or client_order_id not in self._pending_orders:
            # Fallback: try matching by envelope clientMsgId (same pattern as
            # _handle_pending_order_error). When cTrader acks with empty
            # clientOrderId, the clientMsgId from the envelope is the only
            # way to correlate the fill back to the pending order.
            if client_msg_id:
                client_order_id = self._pending_client_msg_ids.get(client_msg_id, "")
            if not client_order_id or client_order_id not in self._pending_orders:
                # Late-fill registry check: order may have timed out or been
                # disconnected, but the broker execution event arrived later.
                # Match by clientOrderId or clientMsgId against the registry.
                late_order = None
                # Card 8ad140c5 finding #3: clientOrderId-only path must also
                # observe TTL expiry for the unmatched counter. _lookup_late_fill
                # silently pops expired entries as a side effect; we must
                # detect that THIS lookup found only expired entries (vs.
                # the registry being genuinely empty) so we can bump the
                # counter.
                if client_order_id:
                    pre_expiry = next(
                        iter(self._late_fill_registry.get(client_order_id, (None,))[0:1]),
                        None,
                    )
                    if pre_expiry is not None:
                        # Entry existed before lookup; check TTL.
                        exp_ts = pre_expiry
                        now_ts = time.monotonic()
                        if now_ts >= exp_ts:
                            expired_keys_seen.add(client_order_id)
                    late_order = self._lookup_late_fill(client_order_id)
                if late_order is None and client_msg_id:
                    # Card ce6de98d (E) / scenario (5): bounded late handling.
                    # Iterate the registry to find a client_msg_id match, but
                    # SKIP expired entries (TTL already passed) — they must
                    # be dropped with the unmatched_late_fills counter bump
                    # rather than re-fired as a fill. We do the TTL filter
                    # inline (rather than relying on _lookup_late_fill's lazy
                    # cleanup) because the per-msg-id iteration path doesn't
                    # get that cleanup applied automatically.
                    #
                    # Card 8ad140c5 finding #3: when this iteration sees
                    # an expired entry (matched by client_msg_id), remove
                    # it from the registry HERE so the registry does not
                    # accumulate dead entries across many post-TTL events.
                    # The expired_keys_seen set ensures the counter still
                    # bumps exactly once even if the same entry is observed
                    # by both the clientOrderId-only pre-check (above) and
                    # this client_msg_id iteration.
                    now = time.monotonic()
                    for rid, (exp, ord_, cmid) in list(self._late_fill_registry.items()):
                        if cmid != client_msg_id:
                            continue
                        if now >= exp:
                            # Entry expired before this event arrived —
                            # record for the counter bump at the end of
                            # this method (if we end up here in the DROP
                            # branch) AND pop from the registry so it
                            # does not linger across subsequent broker
                            # events.
                            expired_keys_seen.add(rid)
                            self._late_fill_registry.pop(rid, None)
                            continue
                        late_order = ord_
                        break
                if late_order is not None:
                    logger.info(
                        "[EXEC_EVENT] LATE-FILL MATCH — clientOrderId=%r found in late_fill_registry, "
                        "upgrading from reason=%s",
                        client_order_id,
                        getattr(late_order, "reason", ""),
                    )
                    order = late_order
                    # NOTE: flag `from_late_registry` is intentionally assigned but never read
                    # (F841 out-of-scope for this card; suppressed to satisfy ruff-exit-0 bar).
                    # Preserved in place so a future refactor that consumes this signal still sees
                    # the marker without re-introducing the assignment.
                    from_late_registry = True  # noqa: F841
                    # Synthetic event (already set) — the original caller already
                    # returned; no thread is waiting on this event.
                    event = threading.Event()
                    event.set()
                else:
                    error_code = getattr(message, "errorCode", "UNKNOWN")
                    description = getattr(message, "description", "")
                    # Card ce6de98d (E) / 8ad140c5 finding #3: bump the
                    # unmatched_late_fills counter exactly once if EITHER
                    # lookup path observed an expired entry. We use a set
                    # so the same expired entry observed via both keys
                    # only counts once (parity across both paths). This
                    # also covers the clientOrderId-only case that the
                    # pre-fix code missed.
                    if expired_keys_seen:
                        self._unmatched_late_fills_count += 1
                    logger.warning(
                        "[EXEC_EVENT] DROP — clientOrderId=%r not in pending_orders (keys=%s) "
                        "or late_fill_registry (size=%d) "
                        "errorCode=%r description=%r "
                        "(unmatched_late_fills=%d)",
                        client_order_id,
                        list(self._pending_orders.keys()) if self._pending_orders else "[]",
                        len(self._late_fill_registry),
                        error_code,
                        description,
                        self._unmatched_late_fills_count,
                    )
                    return
            else:
                # Peek: do NOT pop yet. The pop happens after we confirm
                # the etype is terminal (below).
                event, order = self._pending_orders[client_order_id]
        else:
            # Peek: do NOT pop yet.
            event, order = self._pending_orders[client_order_id]

        # Re-read etype from the message (defensive against re-binding).
        etype = getattr(message, "executionType", None)

        # Card ce6de98d (A): TERMINAL-ONLY STATE MACHINE.
        # Informational etypes (ACCEPT, REPLACED, SWAP, DEPOSIT_WITHDRAW,
        # BONUS_DEPOSIT_WITHDRAW, CANCEL_REJECTED, etc.) log only and skip
        # — DO NOT pop, DO NOT fire callbacks, DO NOT set the event. The
        # pending entry remains available for a future terminal event to
        # match.
        #
        # Card 8ad140c5 finding #2 — sprint reina-2026-08-18-106: ORDER_PARTIAL_FILL(11)
        # is PROGRESS, not terminal (designed by Reina). 11 is split out
        # into its own branch BEFORE the terminal-only check so the
        # terminal-set membership test stays clean. Rationale documented
        # inline at the _PROGRESS_EXEC_TYPES constant definition above.
        if etype in _PROGRESS_EXEC_TYPES:
            # PARTIAL_FILL(11) — update executed volume/price if present
            # on the payload, log informationally, NO callback, KEEP
            # pending entry open. A subsequent ORDER_FILLED(3) (or any
            # terminal event) will close out the order. 11-then-stall
            # flows into the existing INDETERMINATE timeout path
            # unchanged — new_order() still sets reason=
            # indeterminate_awaiting_event on event.wait expiry.
            _partial_volume = getattr(order_payload, "executedVolume", None) if order_payload else None
            _partial_price = getattr(order_payload, "executionPrice", None) if order_payload else None
            # Best-effort: update the order's volume in lots if the payload
            # carries executedVolume. We intentionally do NOT mutate
            # filled_price here — filled_price is reserved for the real
            # terminal FILLED event so downstream consumers can distinguish
            # a partial fill from a completed fill.
            if _partial_volume:
                ev_symbol_id = getattr(order_payload, "symbolId", None) if order_payload else None
                if ev_symbol_id is None or ev_symbol_id not in self._symbols:
                    ev_symbol_id = self._resolve_name_to_id(order.symbol) or 0
                try:
                    order.volume = self._volume_calc.volume_to_lots(ev_symbol_id, _partial_volume)
                except ValueError:
                    logger.info(
                        "[EXEC_EVENT] PARTIAL_FILL volume conversion skipped for "
                        "symbol_id=%s (raw=%s) — keeping order.volume=%s",
                        ev_symbol_id,
                        _partial_volume,
                        order.volume,
                    )
            logger.info(
                "[EXEC_EVENT] PROGRESS execType=%s (ORDER_PARTIAL_FILL) for "
                "clientOrderId=%r — executedVolume=%s executionPrice=%s. "
                "PROGRESS semantics (sprint 106, finding #2): NO callback, "
                "pending entry kept open, awaiting terminal ORDER_FILLED(3) "
                "/ ORDER_REJECTED(7) / ORDER_CANCELLED(5) / ORDER_EXPIRED(6).",
                etype,
                client_order_id,
                _partial_volume if _partial_volume is not None else "n/a",
                _partial_price if _partial_price is not None else "n/a",
            )
            return

        if etype not in _TERMINAL_EXEC_TYPES:
            logger.info(
                "[EXEC_EVENT] informational execType=%s for clientOrderId=%r "
                "— terminal-only state machine: no callback fired, pending entry remains open",
                etype,
                client_order_id,
            )
            return

        # From here on, etype IS terminal. Atomically consume ALL state for
        # this order across pending + late-fill registry + reverse map —
        # card 8ad140c5 finding #1 (sprint reina-2026-08-18-106).  This is
        # idempotent and order-independent: pending-path terminal events
        # AND registry-path terminal events both end up calling the same
        # consume helper, so a duplicate / reverse-ordering terminal
        # event arriving within the TTL window is a no-op (no overwrite,
        # no second callback).
        self._consume_order_state_across_all_maps(client_order_id, client_msg_id)

        if etype == ProtoOAExecutionType.ORDER_CANCELLED:
            order.status = OrderStatus.CANCELLED
            order.reason = "order_cancelled"
            event.set()
            self._trigger_callback("on_order_cancelled", order, message)
            return
        if etype == ProtoOAExecutionType.ORDER_REJECTED:
            reason = getattr(message, "errorCode", "") or "order_rejected"
            order.status = OrderStatus.REJECTED
            order.comment = reason
            order.reason = reason
            event.set()
            self._trigger_callback("on_order_rejected", order, message, reason)
            return
        if etype == ProtoOAExecutionType.ORDER_EXPIRED:
            # OrderStatus has no EXPIRED value — map to CANCELLED with
            # reason="order_expired" so downstream treats it as terminal
            # non-failure (similar to user-initiated cancel). The reason
            # string distinguishes GTD-expiry from a manual cancel.
            order.status = OrderStatus.CANCELLED
            order.reason = "order_expired"
            order.comment = "order_expired"
            event.set()
            self._trigger_callback("on_order_cancelled", order, message)
            return

        # ORDER_FILLED path.
        # Card ce6de98d (C): on_order_filled dedupe. If this order already
        # fired on_order_filled (e.g. the old bug misrouted ACCEPT(2) as
        # FILLED, or the broker re-emitted FILLED after TTL expiry), skip
        # the duplicate callback. Late duplicates land in the unmatched
        # handling (DROP log above) WITHOUT re-firing on_order_filled.
        if getattr(order, "_fill_cb_fired", False):
            logger.warning(
                "[EXEC_EVENT] DROP — duplicate FILLED re-emission for "
                "clientOrderId=%r (order_id=%r) — on_order_filled already "
                "fired, skipping to prevent double-count. "
                "Card ce6de98d (C) dedupe guard.",
                client_order_id,
                getattr(order, "order_id", ""),
            )
            return
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.utcnow()
        order.filled_price = (
            getattr(order_payload, "executionPrice", None)
            or getattr(getattr(message, "deal", None), "executionPrice", None)
            or getattr(getattr(message, "position", None), "price", None)
            or order.price
        )
        # Stamp cTrader positionId onto Order for downstream amend calls
        # Naked-position fix landed in commit 1ed0cdee (2026-07-06). cTrader
        # exposes the positionId in three places depending on event variant;
        # try each in priority order.
        _pos_source = (
            getattr(order_payload, "positionId", None)
            or getattr(getattr(message, "position", None), "positionId", None)
            or getattr(getattr(message, "deal", None), "positionId", None)
        )
        if _pos_source:
            try:
                order.position_id = int(_pos_source)
            except (TypeError, ValueError):
                pass
        ev = getattr(order_payload, "executedVolume", 0)
        if ev:
            ev_symbol_id = getattr(order_payload, "symbolId", None)
            if ev_symbol_id is None or ev_symbol_id not in self._symbols:
                ev_symbol_id = self._resolve_name_to_id(order.symbol) or 0
            try:
                order.volume = self._volume_calc.volume_to_lots(ev_symbol_id, ev)
            except ValueError:
                logger.warning(
                    "Execution event: cannot convert volume for symbol_id=%s, keeping order.volume=%s",
                    ev_symbol_id,
                    order.volume,
                )
        order.reason = "order_filled"
        # Card ce6de98d (C): mark the fill-callback as fired BEFORE firing
        # so a synchronous re-entry (unlikely but possible if a callback
        # itself triggers another execution event) cannot double-fire.
        order._fill_cb_fired = True
        event.set()
        self._trigger_callback("on_order_filled", order, message)

    def _handle_order_error_event(self, message, envelope) -> None:
        self._handle_pending_order_error(message, envelope)

    def _handle_pending_order_error(self, message, envelope) -> bool:
        client_order_id = getattr(message, "clientOrderId", "")
        client_msg_id = getattr(envelope, "clientMsgId", "")
        logger.info(
            "[ORDER_ERROR] clientOrderId=%r clientMsgId=%r pending_keys=%s",
            client_order_id,
            client_msg_id,
            list(self._pending_orders.keys()) if self._pending_orders else "[]",
        )
        if not client_order_id and client_msg_id:
            client_order_id = self._pending_client_msg_ids.get(client_msg_id, "")
        if not client_order_id or client_order_id not in self._pending_orders:
            # Late-fill registry check: error event for a timed-out/disconnected order
            late_order = None
            if client_order_id:
                late_order = self._lookup_late_fill(client_order_id)
                if late_order is not None:
                    self._late_fill_registry.pop(client_order_id, None)
            if late_order is None and client_msg_id:
                for rid, (_, ord_, cmid) in list(self._late_fill_registry.items()):
                    if cmid == client_msg_id:
                        self._late_fill_registry.pop(rid, None)
                        late_order = ord_
                        break
            if late_order is not None:
                error_code = getattr(message, "errorCode", "UNKNOWN")
                description = getattr(message, "description", "")
                reason = f"{error_code}: {description}".strip(": ")
                late_order.status = OrderStatus.REJECTED
                late_order.comment = reason
                late_order.reason = reason
                logger.info(
                    "[ORDER_ERROR] LATE-FILL MATCH — clientOrderId=%r errorCode=%r "
                    "updating order from late_fill_registry",
                    client_order_id,
                    error_code,
                )
                return True
            error_code = getattr(message, "errorCode", "UNKNOWN")
            description = getattr(message, "description", "")
            # Card 18b74ea7: SESSION-CONFLICT detection. When an ORDER_ERROR
            # event arrives with empty clientOrderId AND errorCode is
            # ALREADY_LOGGED_IN (or description contains "already
            # authorized"), the broker is reporting that another process is
            # already authenticated against the same trading account + OpenAPI
            # app. In the Ayumi deployment this pattern appears when the
            # ayumi watchdog (PID 688994) and the forward test (PID 3028751)
            # are both alive and the watchdog's health-check loop briefly
            # triggers a re-auth that the broker rejects.  Previously this
            # code path was a silent DROP — no counter, no log level above
            # WARNING, invisible to operators. Now we emit a distinct
            # SESSION-CONFLICT warning and bump the
            # ``order_error_session_conflict_count`` counter (surfaced via
            # health_monitor._safe_attr lookup against the spot feed as the
            # ``order_gateway``). Other empty-id errors keep their previous
            # behaviour.
            is_session_conflict = (
                error_code == "ALREADY_LOGGED_IN" or "already authorized" in (description or "").lower()
            )
            if is_session_conflict:
                self._order_error_session_conflict_count += 1
                logger.warning(
                    "[ORDER_ERROR] SESSION-CONFLICT — account/channel re-authorization detected: "
                    "clientOrderId=%r clientMsgId=%r errorCode=%r description=%r "
                    "(session_conflict_count=%d pending=%d late_registry=%d). "
                    "Card 18b74ea7: two processes authorizing the same cTrader "
                    "account; investigate watchdog + forward test overlap.",
                    client_order_id,
                    client_msg_id,
                    error_code,
                    description,
                    self._order_error_session_conflict_count,
                    len(self._pending_orders),
                    len(self._late_fill_registry),
                )
                # Return False to signal "not matched" so the caller knows
                # we did not consume the event, but the counter bump + log
                # make the conflict visible to operators. Other empty-id
                # errors continue to log at WARNING (no counter bump).
                return False
            logger.warning(
                "[ORDER_ERROR] DROP — no match for clientOrderId=%r clientMsgId=%r "
                "errorCode=%r description=%r (pending=%d, late_registry=%d)",
                client_order_id,
                client_msg_id,
                error_code,
                description,
                len(self._pending_orders),
                len(self._late_fill_registry),
            )
            return False
        event, order = self._pending_orders.pop(client_order_id)
        # Card 8ad140c5 rework-2 (Rin iter-3 verdict, M1 ATOMICITY):
        # route the order-error handler through the same atomic-consume
        # helper that ``_handle_execution_event`` uses for its terminal
        # branch. Pre-fix this line did
        #   ``self._pending_client_msg_ids.pop(client_order_id, None)``
        # which is a NO-OP — ``_pending_client_msg_ids`` is keyed by
        # client_msg_id (see L336 / L1518), not client_order_id. The
        # pre-fix code therefore left the late-fill REGISTRY entry AND
        # the reverse-map (client_msg_id → request_id) entry alive after
        # the REJECTED callback fired.
        #
        # Concrete failure mode (Rin iter-3 example):
        #   1. timeout path registers order in pending + registry + reverse map
        #   2. ORDER_ERROR arrives → REJECTED, on_order_rejected fired
        #   3. late EXEC_EVENT FILLED arrives for THE SAME order
        #   4. registry entry still alive → late registry match
        #   5. FILLED path overwrites REJECTED → FILLED
        #   6. on_order_filled fires a second time (double-count)
        #
        # Post-fix: ``_consume_order_state_across_all_maps`` (L1093,
        # verified idempotent at L1148-1171) clears all THREE structures
        # by both keys. Its internal ``_pending_orders.pop(client_order_id)``
        # is a harmless no-op here because we already popped above.
        self._consume_order_state_across_all_maps(client_order_id, client_msg_id)
        error_code = getattr(message, "errorCode", "UNKNOWN")
        description = getattr(message, "description", "")
        reason = f"{error_code}: {description}".strip(": ")
        # Guard: if the order was already marked REJECTED by the deferred
        # timeout (on_error) or the local event.wait timeout path, this is
        # a late-arriving broker error event. Update the order's reason to
        # the real errorCode so the caller/classifier sees it, but don't
        # trigger a second callback.
        already_rejected = order.status == OrderStatus.REJECTED
        order.status = OrderStatus.REJECTED
        order.comment = reason
        order.reason = reason
        logger.warning(
            "[ORDER_ERROR] MATCHED clientOrderId=%r errorCode=%r description=%r reason=%s%s",
            client_order_id,
            error_code,
            description,
            reason,
            " (late arrival — reason corrected, callback skipped)" if already_rejected else "",
        )
        event.set()
        if not already_rejected:
            self._trigger_callback("on_order_rejected", order, message, reason)
        return True

    # ── Error handling & token refresh ─────────────────────────────────────

    def _handle_error(self, message, envelope=None) -> None:
        error_code = getattr(message, "errorCode", "UNKNOWN")
        if error_code == "ALREADY_LOGGED_IN":
            self._authed.set()
            return
        if self._auth_circuit_open or self._refresh_in_progress:
            return
        # Centralized auth error classification
        description = getattr(message, "description", "")

        # Determine if this error originated from a non-order query call
        # (e.g. balance/position reconciliation). Query calls use
        # clientMsgId prefixed with trader_query_*, protoOaTrades_*,
        # or protoOaAccount_*. Kill switch should NOT activate for these.
        client_msg_id = getattr(envelope, "clientMsgId", "") if envelope else ""
        is_query_call = (
            client_msg_id.startswith("trader_query_")
            or client_msg_id.startswith("protoOaTrades_")
            or client_msg_id.startswith("protoOaAccount_")
            or client_msg_id.startswith("protoOaReconcile")
            or client_msg_id.startswith("protoOaSymbolBy")
            or client_msg_id.startswith("protoOaTrader_")
        )

        # Tier classification (BQ-1382 §6)
        tier = _ERROR_TIERS.get(error_code, "unknown")
        logger.warning(
            "cTrader error [%s] tier=%s: %s",
            error_code,
            tier,
            description,
        )

        # Tier 3: Critical — halt auto-reconnect for permission/security errors
        if tier == "critical":
            logger.critical(
                "Critical cTrader error [%s] — halting auto-reconnect: %s",
                error_code,
                description,
            )
            return  # Don't attempt reconnect for permission/security errors

        # Tier 2: Rate/Resource — extended backoff before reconnect
        if tier == "rate_resource":
            logger.warning(
                "Rate/resource error [%s] — applying 10s extended backoff",
                error_code,
            )
            time.sleep(10)

        fault_type, policy = get_policy(error_code, description)
        logger.warning(
            "Auth error classified: code=%s fault_type=%s tier=%s can_refresh=%s escalate=%s",
            error_code,
            fault_type.value,
            tier,
            policy.can_refresh,
            policy.requires_escalation,
        )
        # Description-based reclassification: INVALID_REQUEST with "not authorized"
        # in the description is classified as MALFORMED_REQUEST by error code
        # lookup, but the real issue is account authorization. Upgrade to
        # ACCOUNT_AUTHORIZATION_FAULT so the kill switch activates.
        if not policy.activate_kill_switch and "not authorized" in description.lower():
            # Card f37e7b74: emit an explicit, unmistakable error log line
            # naming the authorization fault when the server reports the
            # trading account is not authorized. This surfaces the actual
            # server-side fault in operator logs (rather than letting it
            # bubble up later as the misleading "ProtoOATraderRes missing
            # 'trader' field" schema complaint raised downstream by
            # account_state.get_balance). The exact phrasing "trading
            # account" is the canonical cTrader broker message emitted on
            # the trader/balance query path; we gate on both phrases to
            # avoid false positives from generic "not authorized" wording.
            desc_lower = description.lower()
            if "trading account" in desc_lower and "not authorized" in desc_lower:
                logger.error(
                    "[Account Auth] server reports not authorized — broker "
                    "intervention required: code=%s description='%s' "
                    "clientMsgId=%s",
                    error_code,
                    description,
                    client_msg_id,
                )
            if is_query_call:
                logger.warning(
                    "Query error contains 'not authorized' — kill switch NOT activated "
                    "(non-order call, clientMsgId=%s): code=%s desc='%s'",
                    client_msg_id,
                    error_code,
                    description,
                )
            else:
                from .auth_error_types import POLICIES, AuthFaultType

                reclassified = POLICIES.get(AuthFaultType.ACCOUNT_AUTHORIZATION_FAULT)
                if reclassified and reclassified.activate_kill_switch:
                    policy = reclassified
                    fault_type = AuthFaultType.ACCOUNT_AUTHORIZATION_FAULT
                    logger.warning(
                        "Auth error reclassified by description: code=%s desc='%s' → %s",
                        error_code,
                        description,
                        fault_type.value,
                    )

        if policy.activate_kill_switch:
            logger.error(
                "Kill switch activating due to %s fault (code=%s): %s",
                fault_type.value,
                error_code,
                description,
            )
            self._activate_kill_switch_freeze(f"auth_fault:{fault_type.value}:{error_code}")
        if policy.can_refresh:
            now = time.monotonic()
            if now - self._last_reactive_refresh_time < 60.0:
                return
            self._last_reactive_refresh_time = now
            self._refresh_token_and_reauth()
        elif policy.requires_escalation:
            logger.error(
                "Auth fault requires escalation: %s (%s) — failing closed",
                fault_type.value,
                error_code,
            )

    def _refresh_token_and_reauth(self, proactive: bool = False) -> None:
        """Delegate OAuth refresh to TokenLifecycle and re-auth on success.

        Runs the refresh in a background thread to avoid blocking the Twisted
        reactor. The re-auth send is dispatched back to the reactor thread
        via reactor.callFromThread().

        If no TokenLifecycle is wired or refresh is disabled, falls back to
        no-op (migration safety).

        Card 1e32c408 (session-conflict guard, sprint reina-2026-08-21-024):

        The pre-fix version unconditionally sent ``ProtoOAAccountAuthReq``
        after every token refresh, even when the channel was already
        authenticated. cTrader enforces a single-session rule per
        OpenAPI app per channel — re-authenticating an already-authenticated
        channel returns ``ALREADY_LOGGED_IN`` (``errorCode``,
        ``description='Trading account is already authorized in this
        channel'``). Because the re-auth was fire-and-forget
        (``reactor.callFromThread(self._conn.send, req)``), the response
        surfaced as an ``ORDER_ERROR`` event with empty ``clientOrderId``,
        which the existing 18b74ea7 SESSION-CONFLICT detector caught and
        logged but could not silence.

        2026-08-20T17:12:55Z live XAUUSD/SRMR+ forward test reproduced
        this once: a proactive token refresh at 17:12:53–54 fired a
        ``ProtoOAAccountAuthReq`` at 17:12:55 against the already-authenticated
        channel. Broker returned ``ALREADY_LOGGED_IN``; the warning fired,
        feed stayed healthy (one-off, no cascading failure).

        The fix has two parts:

        1. **Skip when already authenticated** — if the channel is
           authenticated (``_authed.is_set()`` and ``_conn.is_connected``),
           the existing channel binding to the OLD access token is still
           valid for the lifetime of the current connection. The new token
           is committed to ``self._access_token``, ``self._refresh_token``,
           and the on-disk credential store; it will be used on the next
           reconnect (when ``_reconnect_restore`` re-auths the channel
           with the fresh token). No broker round-trip, no
           ``ALREADY_LOGGED_IN``, no SESSION-CONFLICT warning.

        2. **Session mutex** — wraps the entire refresh+re-auth sequence
           in ``self._session_reauth_lock`` so that a reactive
           AUTH_EXPIRED-driven refresh and a proactive refresh cannot
           interleave their re-auth sends. ``_reconnect_restore`` acquires
           the same lock (see its docstring). The lock is non-blocking on
           acquisition: if another thread holds it, we skip the re-auth
           (the holder will complete it or the next refresh cycle will
           retry) rather than deadlock.

        The defensive SESSION-CONFLICT detector at
        ``_handle_order_error_event`` is preserved unchanged — it remains
        the safety net for any ALREADY_LOGGED_IN that does slip through
        (e.g., from an external process authenticating against the same
        account, card 18b74ea7).
        """
        if self._token_lifecycle is None:
            logger.warning("_refresh_token_and_reauth: no TokenLifecycle wired — skipping")
            return

        if getattr(self._token_lifecycle, "_refresh_disabled", False):
            logger.info(
                "_refresh_token_and_reauth: token refresh DISABLED — skipping (manual rotation required). proactive=%s",
                proactive,
            )
            return

        if not proactive and self._auth_circuit_open:
            return

        def _do_refresh_offthread():
            # Card 1e32c408 (session-conflict guard): acquire the session
            # mutex BEFORE doing the OAuth refresh + re-auth send. The
            # lock is also acquired by ``_reconnect_restore`` — see that
            # method's docstring for why. Non-blocking acquire: if a
            # reconnect-driven re-auth is already in flight, skip this
            # refresh (the holder will complete it; if it fails, the next
            # reactive / proactive cycle will retry).
            if not self._session_reauth_lock.acquire(blocking=False):
                logger.info(
                    "Session re-auth lock held — skipping this token "
                    "refresh cycle (another re-auth is in flight). proactive=%s",
                    proactive,
                )
                return
            try:
                new_token = self._token_lifecycle.force_refresh()
                self._access_token = new_token
                # Refresh the refresh token too
                creds = self._token_lifecycle._store.get()
                self._refresh_token = creds.refresh_token
                # Update local expiry tracking
                exp = self._token_lifecycle.expires_at
                if exp is not None:
                    self._token_expires_at = time.monotonic() + max(
                        (exp - datetime.now(timezone.utc)).total_seconds(), 60.0
                    )
                else:
                    self._token_expires_at = time.monotonic() + 86400
                # Card 1e32c408 (session-conflict guard): skip the
                # account re-auth when the channel is already
                # authenticated, OR when the underlying TCP connection
                # is gone. The cTrader OpenAPI single-session rule means
                # re-authenticating an already-authenticated channel is a
                # no-op that the broker rejects with ALREADY_LOGGED_IN.
                # Sending on a dead connection is pointless — the
                # reconnect loop will handle re-auth when it gets a fresh
                # TCP connection. The new access token is committed to
                # the credential store (above) and will be used on the
                # next reconnect — see the function-level docstring for
                # the full rationale.
                if not self._conn.is_connected:
                    logger.info(
                        "Token refreshed via TokenLifecycle delegation — "
                        "connection down, skipping re-auth (reconnect "
                        "loop owns auth when TCP is restored; "
                        "fresh token_age_s=%.1f, proactive=%s)",
                        self._token_lifecycle.token_age_s,
                        proactive,
                    )
                    self._auth_error_count = 0
                    return
                if self._authed.is_set():
                    logger.info(
                        "Token refreshed via TokenLifecycle delegation — "
                        "channel already authenticated, skipping re-auth "
                        "(new token will be used on next reconnect; "
                        "fresh token_age_s=%.1f, expires_at=%s, proactive=%s)",
                        self._token_lifecycle.token_age_s,
                        self._token_lifecycle.expires_at.isoformat() if self._token_lifecycle.expires_at else "None",
                        proactive,
                    )
                    self._auth_error_count = 0
                    return
                # Re-auth via reactor (channel is NOT authenticated and
                # the connection is live — the recovery path).
                from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                    ProtoOAAccountAuthReq,
                )

                req = ProtoOAAccountAuthReq()
                req.ctidTraderAccountId = self._ctid_account_id
                req.accessToken = new_token
                reactor.callFromThread(self._conn.send, req)
                self._auth_error_count = 0
                logger.info("Token refreshed via TokenLifecycle delegation")
            except Exception as exc:
                self._auth_error_count += 1
                self._check_circuit_breaker()
                logger.error("Token refresh delegation failed: %s", exc)
            finally:
                self._session_reauth_lock.release()

        threading.Thread(target=_do_refresh_offthread, daemon=True).start()

    def _handle_auth_failure(self, context: str) -> None:
        self._auth_error_count += 1
        self._check_circuit_breaker()

    def _check_circuit_breaker(self) -> None:
        if self._auth_error_count >= 5:
            self._auth_circuit_open = True
            self._state_mgr.transition_to(ConnectionState.FAILED, reason=f"auth_errors:{self._auth_error_count}")
            self._activate_kill_switch_freeze(f"auth_failure:{self._auth_error_count}")
        elif self._auth_error_count >= 3:
            self._state_mgr.transition_to(
                ConnectionState.DEGRADED,
                reason=f"auth_degraded:{self._auth_error_count}",
            )

    def _schedule_proactive_refresh(self, expires_in: int) -> None:
        if self._auth_circuit_open:
            return
        if getattr(self._token_lifecycle, "_refresh_disabled", False):
            logger.info("_schedule_proactive_refresh: skipped — token refresh DISABLED")
            return
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        refresh_in = max(expires_in * 0.8, 60.0)
        self._refresh_timer = threading.Timer(refresh_in, self._proactive_refresh_task)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

    def _proactive_refresh_task(self) -> None:
        if self._running:
            self._refresh_token_and_reauth(proactive=True)

    # ── Health-check compatibility wrappers ─────────────────────────────
    # These delegate to the data now owned by CTraderConnection so that
    # legacy tests and monitoring scripts keep working after the archived
    # refactor moved heartbeat/stale-tick state into CTraderConnection.

    def _check_heartbeat_health(self) -> None:
        """Compatibility wrapper — checks heartbeat health and transitions state.

        Heartbeat data lives in CTraderConnection._last_heartbeat_recv.
        This wrapper replicates the old health-check logic so tests and
        monitoring code that call it keep working.
        """
        if not self._state_mgr.is_operational:
            return  # Skip when DISCONNECTED/CONNECTING/RECONNECTING/FAILED

        now = time.monotonic()
        last_hb = self._conn._last_heartbeat_recv
        if last_hb is None:
            return
        age = now - last_hb

        if age >= _HEARTBEAT_RECONNECT_SEC:
            logger.warning("Heartbeat stale (%.1fs) → RECONNECTING", age)
            self._state_mgr.transition_to(
                ConnectionState.RECONNECTING,
                reason="heartbeat_reconnect",
            )
        elif age >= _HEARTBEAT_DEGRADED_SEC:
            logger.warning("Heartbeat degraded (%.1fs) → DEGRADED", age)
            self._state_mgr.transition_to(
                ConnectionState.DEGRADED,
                reason="heartbeat_degraded",
            )

    def _check_stale_ticks(self) -> None:
        """Compatibility wrapper — checks for stale ticks during market hours.

        Stale-tick detection was moved out of the feed during the archived
        refactor. This wrapper restores the check so tests and monitoring
        code keep working. Weekend and non-authenticated states are skipped.
        """
        if not self._state_mgr.is_authenticated and self._state_mgr.state != ConnectionState.DEGRADED:
            return  # Only check when AUTHENTICATED or DEGRADED

        now_dt = datetime.now(timezone.utc)
        if now_dt.weekday() >= 5:
            return  # Weekend — skip

        age = time.monotonic() - self._last_tick_recv_monotonic
        if age >= _STALE_TICK_FREEZE_SEC:
            logger.warning("Stale ticks (%.1fs) → activating kill switch freeze", age)
            self._activate_kill_switch_freeze(f"stale_ticks:{age:.0f}s")
        elif age >= _STALE_TICK_WARN_SEC:
            logger.warning("Stale ticks detected (%.1fs) — warning only", age)

    def _activate_kill_switch_freeze(self, reason: str) -> None:
        # Market-hours gating is handled by callers (e.g. _on_conn_feed_dead)
        # that check is_forex_market_closed() before invoking this method.
        # Auth-failure and other hard-error callers should fire unconditionally.
        if self._kill_switch is not None:
            try:
                self._kill_switch.activate_global_freeze(reason=reason, triggered_by="spot_feed")
            except Exception as exc:
                logger.error("Kill switch activation failed: %s", exc)

    # ── Reconnection ───────────────────────────────────────────────────────

    def _reconnect_restore(self) -> None:
        # Card 1e32c408 (session-conflict guard): acquire the session
        # mutex around the entire reconnect-driven re-auth cycle so a
        # token-refresh-driven re-auth cannot interleave. Non-blocking:
        # if a refresh is already in flight, skip this reconnect (the
        # holder will complete the auth; if it fails, the connection's
        # own reconnect loop will retry).
        if not self._session_reauth_lock.acquire(blocking=False):
            logger.info(
                "Session re-auth lock held — skipping reconnect-driven "
                "re-auth (token refresh in flight will handle it)"
            )
            self._reauth_in_progress.clear()
            return
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAAccountAuthReq,
                ProtoOAApplicationAuthReq,
            )

            if not self._conn.is_connected:
                return

            # Stabilization delay — cTrader demo server needs time to accept auth
            # after TCP connect (market-open race, see card 23cb1091).
            if self._reconnect_stabilization_delay > 0:
                logger.info(
                    "Reconnect stabilization delay: %.1fs before auth",
                    self._reconnect_stabilization_delay,
                )
                time.sleep(self._reconnect_stabilization_delay)

            self._state_mgr.transition_to(
                ConnectionState.APP_AUTHENTICATING,
                reason="reconnect_app_auth_sending",
            )
            # App auth with within-session retry (market-open race resilience)
            app_success = False
            for attempt in range(2):
                app_res = self._conn.send_and_wait(
                    ProtoOAApplicationAuthReq(clientId=self._client_id, clientSecret=self._client_secret),
                    timeout=10,
                )
                if app_res is not None and self._is_expected_auth_response(
                    app_res, _APP_AUTH_RES_PAYLOAD_TYPE, "reconnect_app"
                ):
                    app_success = True
                    break
                if attempt == 0:
                    logger.warning("Reconnect app auth attempt 1 failed — retrying in 10s (market-open race)")
                    time.sleep(10)

            if not app_success:
                self._handle_auth_failure("reconnect_app")
                return
            self._app_authed.set()

            self._state_mgr.transition_to(
                ConnectionState.ACCT_AUTHENTICATING,
                reason="reconnect_acct_auth_sending",
            )
            if not self._conn.is_connected:
                return

            acct_res = self._conn.send_and_wait(
                ProtoOAAccountAuthReq(
                    ctidTraderAccountId=self._ctid_account_id,
                    accessToken=self._access_token,
                ),
                timeout=10,
            )
            if acct_res is None or not self._is_expected_auth_response(
                acct_res, _ACCT_AUTH_RES_PAYLOAD_TYPE, "reconnect_acct"
            ):
                self._handle_auth_failure("reconnect_acct")
                return
            self._authed.set()
            self._set_message_callback()

            logger.info(
                "Re-subscribing to %d symbols after reconnect: %s",
                len(self._subscribed_symbol_ids),
                list(self._subscribed_symbol_ids),
            )
            for sid in list(self._subscribed_symbol_ids):
                self._subscribe_by_id(sid)

            self._state_mgr.transition_to(ConnectionState.AUTHENTICATED, reason="reconnect_complete")
            positions = self.reconcile()
            self._resolve_disconnected_orders(positions)
            self._fire_reconnect_callbacks()
        except Exception as exc:
            logger.error("Reconnect restore failed: %s", exc, exc_info=True)
        finally:
            self._reauth_in_progress.clear()
            # Card 1e32c408: release the session mutex. Use a guarded
            # release because the early-return path on lock contention
            # (above) does not own the lock.
            if self._session_reauth_lock.locked():
                self._session_reauth_lock.release()

    def _resolve_disconnected_orders(self, positions: list[Position]) -> None:
        if not self._disconnected_pending_orders:
            return
        available = list(positions)
        unresolved = []
        for order in self._disconnected_pending_orders:
            match = next(
                (
                    p
                    for p in available
                    if p.symbol == order.symbol
                    and p.direction == order.direction
                    and abs(p.volume - order.volume) < 0.000001
                ),
                None,
            )
            if match is None:
                unresolved.append(order)
                continue
            available.remove(match)
            order.status = OrderStatus.FILLED
            order.filled_at = datetime.utcnow()
            order.filled_price = match.entry_price
            order.reason = "resolved_by_reconcile"
            self._trigger_callback("on_order_filled", order, match)
        self._disconnected_pending_orders = unresolved

    def _fire_reconnect_callbacks(self) -> None:
        if self._disconnect_at is None:
            return
        outage = time.monotonic() - self._disconnect_at
        for cb in self._on_reconnected_callbacks:
            try:
                cb(outage)
            except Exception as exc:
                logger.error("Reconnect callback error: %s", exc)
        self._disconnect_at = None
