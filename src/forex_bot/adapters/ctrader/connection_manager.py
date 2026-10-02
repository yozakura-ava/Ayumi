"""Unified connection manager for cTrader dual-connection architecture.

Owns both the market data (spot feed) and trade execution connections,
providing a single health gate (SplitBrainGate) and unified metrics.

Concurrent session lifecycle (BQ-1329):
-----------------------------------------
When operating the spot feed + historical-data client simultaneously,
only one metrics/health thread set is needed.  Call ``stop()`` before
dropping the manager to terminate the background metrics emitter and avoid
thread leaks in tests or short-lived processes.

BQ-716 Phase 1.
"""

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from .connection_state import ConnectionState, ConnectionStateManager
from .credential_store import CredentialStore
from .error_classifier import ErrorTier, classify_error
from .reconnect_strategy import ReconnectDecision, ReconnectStrategy
from .token_lifecycle import TokenLifecycle

# BQ-1330a: Auth retry constants
AUTH_RETRY_MAX_ATTEMPTS = 3
AUTH_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# Auth failure escalation: after this many consecutive auth failures across
# reconnect cycles, force a full reconnect (new TCP + fresh auth) instead of
# looping on the same connection.  Prevents the "stuck in FAILED" spiral
# where auth errors climb to 20+ without recovery (2026-07-03 incident).
AUTH_FULL_RECONNECT_THRESHOLD = 8


# ── Decision context ──────────────────────────────────────────────────────────


@dataclass
class ConnectionStateSnapshot:
    """Connection state to embed in trading decision records.

    Captures a point-in-time view of both connections for post-hoc analysis.
    """

    market_data_state: str  # ConnectionState value
    trade_execution_state: str  # ConnectionState value
    fully_operational: bool
    is_tradeable: bool
    timestamp: str  # ISO-8601


logger = logging.getLogger("ayumi.connection_manager")


# ── Data types ────────────────────────────────────────────────────────────────


class ConnectionRole(Enum):
    MARKET_DATA = "market_data"
    TRADE_EXECUTION = "trade_execution"


@dataclass
class ConnectionHealth:
    """Health snapshot for a single connection."""

    role: ConnectionRole
    state: ConnectionState
    uptime_pct_1h: float = 0.0
    uptime_pct_24h: float = 0.0
    reconnect_count_1h: int = 0
    reconnect_count_24h: int = 0
    time_in_degraded_1h: float = 0.0
    last_state_change: Optional[datetime] = None
    last_state_change_reason: str = ""


@dataclass
class DualConnectionHealth:
    """Health snapshot for both connections."""

    market_data: ConnectionHealth
    trade_execution: ConnectionHealth
    fully_operational: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Metrics ───────────────────────────────────────────────────────────────────


@dataclass
class _StateTransition:
    """Record of a state transition for metrics."""

    timestamp: float  # monotonic
    old_state: ConnectionState
    new_state: ConnectionState
    reason: str


class ConnectionMetrics:
    """Tracks per-connection metrics: uptime, reconnect count, degraded time."""

    def __init__(self, role: ConnectionRole, window_hours: int = 24):
        self._role = role
        self._lock = threading.Lock()
        self._transitions: deque[_StateTransition] = deque(maxlen=10000)
        self._start_time = time.monotonic()
        self._current_state: ConnectionState = ConnectionState.DISCONNECTED

    def record_transition(
        self,
        old_state: ConnectionState,
        new_state: ConnectionState,
        reason: str,
    ) -> None:
        with self._lock:
            self._transitions.append(
                _StateTransition(
                    timestamp=time.monotonic(),
                    old_state=old_state,
                    new_state=new_state,
                    reason=reason,
                )
            )
            self._current_state = new_state

    def get_health(self, state_mgr: ConnectionStateManager) -> ConnectionHealth:
        """Compute health metrics from transition history."""
        now = time.monotonic()
        window_1h = now - 3600
        window_24h = now - 86400

        with self._lock:
            transitions_1h = [t for t in self._transitions if t.timestamp >= window_1h]
            transitions_24h = [t for t in self._transitions if t.timestamp >= window_24h]

            # Reconnect count = transitions into RECONNECTING
            reconnects_1h = sum(1 for t in transitions_1h if t.new_state == ConnectionState.RECONNECTING)
            reconnects_24h = sum(1 for t in transitions_24h if t.new_state == ConnectionState.RECONNECTING)

            # Time in DEGRADED state (approximate from transitions)
            degraded_time_1h = 0.0
            degraded_start = None
            for t in transitions_1h:
                if t.new_state == ConnectionState.DEGRADED:
                    degraded_start = t.timestamp
                elif degraded_start is not None and t.new_state != ConnectionState.DEGRADED:
                    degraded_time_1h += t.timestamp - degraded_start
                    degraded_start = None
            # If still degraded, count to now
            if degraded_start is not None:
                degraded_time_1h += now - degraded_start

            # Uptime: time in AUTHENTICATED / DEGRADED / total time
            # Simple approximation: ratio of AUTHENTICATED+DEGRADED transitions
            good_states = {ConnectionState.AUTHENTICATED, ConnectionState.DEGRADED}
            uptime_1h = self._calc_uptime(transitions_1h, window_1h, now, good_states)
            uptime_24h = self._calc_uptime(transitions_24h, window_24h, now, good_states)

            # Last transition
            last_ts = None
            last_reason = ""
            if self._transitions:
                last = self._transitions[-1]
                last_ts = datetime.fromtimestamp(last.timestamp, tz=timezone.utc)
                last_reason = last.reason

        return ConnectionHealth(
            role=self._role,
            state=state_mgr.state,
            uptime_pct_1h=round(uptime_1h * 100, 2),
            uptime_pct_24h=round(uptime_24h * 100, 2),
            reconnect_count_1h=reconnects_1h,
            reconnect_count_24h=reconnects_24h,
            time_in_degraded_1h=round(degraded_time_1h, 2),
            last_state_change=last_ts,
            last_state_change_reason=last_reason,
        )

    def _calc_uptime(
        self,
        transitions: list[_StateTransition],
        window_start: float,
        now: float,
        good_states: set[ConnectionState],
    ) -> float:
        """Calculate uptime as fraction of time in good states."""
        if not transitions:
            # No transitions in window — assume current state for entire window
            if self._current_state in good_states:
                return 1.0
            return 0.0

        good_time = 0.0
        prev_time = window_start
        prev_state = transitions[0].old_state if transitions else self._current_state

        for t in transitions:
            if prev_state in good_states:
                good_time += t.timestamp - prev_time
            prev_time = t.timestamp
            prev_state = t.new_state

        # Count from last transition to now
        if prev_state in good_states:
            good_time += now - prev_time

        total = now - window_start
        return min(1.0, good_time / total) if total > 0 else 0.0


# ── Connection Manager ────────────────────────────────────────────────────────


class ConnectionManager:
    """Owns both cTrader connections and provides unified health view.

    SplitBrainGate: is_fully_operational() returns True only when BOTH
    connections are in AUTHENTICATED state. Enforced at API level —
    trading operations should check before executing.

    Usage::

        mgr = ConnectionManager()
        mgr.register("market_data", spot_feed_state_mgr)
        mgr.register("trade_execution", trade_client_state_mgr)

        if mgr.is_fully_operational:
            # Safe to trade
            ...
        else:
            # Wait or handle degraded state
            ...
    """

    def __init__(
        self,
        metrics_log_path: str | Path | None = None,
        emit_interval: float = 60.0,
    ):
        self._lock = (
            threading.RLock()
        )  # BQ-1329: RLock needed because get_decision_context() calls properties that also acquire lock  # noqa: E501
        self._connections: dict[ConnectionRole, ConnectionStateManager] = {}
        self._metrics: dict[ConnectionRole, ConnectionMetrics] = {}
        self._callbacks: list[callable] = []
        self._metrics_log_path = Path(metrics_log_path) if metrics_log_path else None
        self._emit_interval = emit_interval
        self._error_tier_counts = {
            ErrorTier.TIER_1_TRANSIENT: 0,
            ErrorTier.TIER_2_BACKOFF: 0,
            ErrorTier.TIER_3A_OPERATION: 0,
            ErrorTier.TIER_3B_SYSTEM: 0,
        }
        self._stop_event = threading.Event()
        self._metrics_thread: Optional[threading.Thread] = None
        if self._metrics_log_path is not None:
            self._metrics_thread = threading.Thread(
                target=self._metrics_loop,
                name="ctrader-connection-metrics",
                daemon=True,
            )
            self._metrics_thread.start()

        # Connection reliability wiring (BQ-716)
        self._watchdog = None  # ConnectionWatchdog — lazy import to avoid circular
        self._token_lifecycle: Optional[TokenLifecycle] = None
        self._cred_store: Optional[CredentialStore] = None
        self._reconnect_strategy = ReconnectStrategy()
        self._auth_token: Optional[str] = None
        self._reconnect_attempt = 0

        # Auth failure escalation tracking (2026-07-03: failed-state-sticky bug)
        self._consecutive_auth_failures = 0

    def stop(self) -> None:
        """Stop background threads and release resources.

        BQ-1329: Idempotent teardown for the metrics emitter thread.
        Safe to call multiple times.
        """
        self._stop_event.set()
        if self._metrics_thread is not None:
            if self._metrics_thread.is_alive() and self._metrics_thread is not threading.current_thread():
                self._metrics_thread.join(timeout=2.0)
            self._metrics_thread = None
        logger.info("[ConnectionManager] Stopped")

    def __enter__(self) -> "ConnectionManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def register(
        self,
        role: str | ConnectionRole,
        state_manager: ConnectionStateManager,
    ) -> None:
        """Register a connection's state manager."""
        if isinstance(role, str):
            role = ConnectionRole(role)

        with self._lock:
            self._connections[role] = state_manager
            self._metrics[role] = ConnectionMetrics(role)

        # Subscribe to state changes
        state_manager.on_state_change(lambda old, new, reason, meta, r=role: self._on_state_change(r, old, new, reason))
        logger.info("[ConnectionManager] Registered %s connection", role.value)

    def unregister(self, role: str | ConnectionRole) -> None:
        """Unregister a connection."""
        if isinstance(role, str):
            role = ConnectionRole(role)
        with self._lock:
            self._connections.pop(role, None)
            self._metrics.pop(role, None)

    # ─── SplitBrainGate ────────────────────────────────────────────────────────

    @property
    def is_fully_operational(self) -> bool:
        """True only when ALL connections are AUTHENTICATED.

        This is the SplitBrainGate — trading operations MUST check this
        before executing. A connection in DEGRADED state is NOT sufficient
        for safe trading (data may be stale).

        For read-only operations (display, monitoring), individual connection
        states can be checked directly.
        """
        with self._lock:
            if not self._connections:
                return False
            return all(mgr.state == ConnectionState.AUTHENTICATED for mgr in self._connections.values())

    @property
    def is_tradeable(self) -> bool:
        """True when trade execution is AUTHENTICATED and market data is at least DEGRADED.

        More lenient than is_fully_operational — allows trading with slightly
        stale prices (DEGRADED data connection) but still requires trade
        connection to be solid.
        """
        with self._lock:
            trade = self._connections.get(ConnectionRole.TRADE_EXECUTION)
            market = self._connections.get(ConnectionRole.MARKET_DATA)

            if trade is None:
                return False

            trade_ok = trade.state == ConnectionState.AUTHENTICATED
            market_ok = market is not None and market.state in (
                ConnectionState.AUTHENTICATED,
                ConnectionState.DEGRADED,
            )

            return trade_ok and market_ok

    @property
    def is_data_available(self) -> bool:
        """True when market data connection is operational."""
        with self._lock:
            market = self._connections.get(ConnectionRole.MARKET_DATA)
            return market is not None and market.is_operational

    def observe_error(self, tier: ErrorTier) -> None:
        """Record an error tier for metrics emission."""
        with self._lock:
            self._error_tier_counts[tier] = self._error_tier_counts.get(tier, 0) + 1

    # ─── Health & Metrics ──────────────────────────────────────────────────────

    def get_health(self) -> DualConnectionHealth:
        """Get health snapshot for both connections."""
        with self._lock:
            market_mgr = self._connections.get(ConnectionRole.MARKET_DATA)
            trade_mgr = self._connections.get(ConnectionRole.TRADE_EXECUTION)
            market_metrics = self._metrics.get(ConnectionRole.MARKET_DATA)
            trade_metrics = self._metrics.get(ConnectionRole.TRADE_EXECUTION)

        market_health = ConnectionHealth(role=ConnectionRole.MARKET_DATA, state=ConnectionState.DISCONNECTED)
        trade_health = ConnectionHealth(role=ConnectionRole.TRADE_EXECUTION, state=ConnectionState.DISCONNECTED)

        if market_mgr and market_metrics:
            market_health = market_metrics.get_health(market_mgr)
        if trade_mgr and trade_metrics:
            trade_health = trade_metrics.get_health(trade_mgr)

        return DualConnectionHealth(
            market_data=market_health,
            trade_execution=trade_health,
            fully_operational=self.is_fully_operational,
        )

    def get_connection_state(self, role: str | ConnectionRole) -> ConnectionState:
        """Get state of a specific connection."""
        if isinstance(role, str):
            role = ConnectionRole(role)
        with self._lock:
            mgr = self._connections.get(role)
            return mgr.state if mgr else ConnectionState.DISCONNECTED

    # ─── State change handling ─────────────────────────────────────────────────

    def _on_state_change(
        self,
        role: ConnectionRole,
        old_state: ConnectionState,
        new_state: ConnectionState,
        reason: str,
    ) -> None:
        """Handle state changes from registered connections."""
        with self._lock:
            metrics = self._metrics.get(role)

        if metrics:
            metrics.record_transition(old_state, new_state, reason)

        # Log significant transitions
        if new_state in (ConnectionState.FAILED, ConnectionState.RECONNECTING):
            logger.warning(
                "[ConnectionManager] %s: %s → %s (reason: %s)",
                role.value,
                old_state.value,
                new_state.value,
                reason,
            )
        elif old_state != new_state:
            logger.info(
                "[ConnectionManager] %s: %s → %s (reason: %s)",
                role.value,
                old_state.value,
                new_state.value,
                reason,
            )

        # Fire callbacks
        health = self.get_health()
        for callback in self._callbacks:
            try:
                callback(health, role, old_state, new_state, reason)
            except Exception as exc:
                logger.error("[ConnectionManager] Callback error: %s", exc)

    def on_state_change(self, callback: callable) -> None:
        """Register a callback for any connection state change.

        Callback signature:
            (health: DualConnectionHealth, role: ConnectionRole,
             old_state: ConnectionState, new_state: ConnectionState, reason: str)
        """
        self._callbacks.append(callback)

    def emit_metrics(self) -> dict:
        """Write a structured metrics snapshot to the configured JSONL log."""
        payload = self._build_metrics_payload()
        if self._metrics_log_path is None:
            return payload

        self._metrics_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._metrics_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return payload

    def flush_metrics(self) -> dict:
        """Force an immediate metrics emission."""
        return self.emit_metrics()

    def _build_metrics_payload(self) -> dict:
        health = self.get_health()
        with self._lock:
            error_tier_counts = dict(self._error_tier_counts)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "connection_metrics",
            "market_data": {
                "state": health.market_data.state.value,
                "uptime_1h": health.market_data.uptime_pct_1h,
                "reconnects_1h": health.market_data.reconnect_count_1h,
                "degraded_time_1h": health.market_data.time_in_degraded_1h,
            },
            "trade_execution": {
                "state": health.trade_execution.state.value,
                "uptime_1h": health.trade_execution.uptime_pct_1h,
                "reconnects_1h": health.trade_execution.reconnect_count_1h,
                "degraded_time_1h": health.trade_execution.time_in_degraded_1h,
            },
            "fully_operational": health.fully_operational,
            "error_tiers": {
                "t1": error_tier_counts.get(ErrorTier.TIER_1_TRANSIENT, 0),
                "t2": error_tier_counts.get(ErrorTier.TIER_2_BACKOFF, 0),
                "t3a": error_tier_counts.get(ErrorTier.TIER_3A_OPERATION, 0),
                "t3b": error_tier_counts.get(ErrorTier.TIER_3B_SYSTEM, 0),
            },
        }

    def _metrics_loop(self) -> None:
        while not self._stop_event.wait(self._emit_interval):
            try:
                self.emit_metrics()
            except Exception as exc:
                logger.error("[ConnectionManager] Metrics emission error: %s", exc)

    # ─── Decision context ──────────────────────────────────────────────────────

    def get_decision_context(self) -> "ConnectionStateSnapshot":
        """Get current connection state for embedding in trading decisions.

        Called by the trading engine before every decision.
        The snapshot is included in the decision record for post-hoc analysis.
        """
        with self._lock:
            market_mgr = self._connections.get(ConnectionRole.MARKET_DATA)
            trade_mgr = self._connections.get(ConnectionRole.TRADE_EXECUTION)
            market_state = market_mgr.state if market_mgr else ConnectionState.DISCONNECTED
            trade_state = trade_mgr.state if trade_mgr else ConnectionState.DISCONNECTED
            fully_op = self.is_fully_operational
            tradeable = self.is_tradeable

        return ConnectionStateSnapshot(
            market_data_state=market_state.value,
            trade_execution_state=trade_state.value,
            fully_operational=fully_op,
            is_tradeable=tradeable,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ─── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Human-readable summary of connection states."""
        health = self.get_health()
        lines = [
            f"ConnectionManager: fully_operational={health.fully_operational}",
            f"  Market Data:     {health.market_data.state.value} "
            f"(uptime 1h: {health.market_data.uptime_pct_1h}%, "
            f"reconnects: {health.market_data.reconnect_count_1h})",
            f"  Trade Execution: {health.trade_execution.state.value} "
            f"(uptime 1h: {health.trade_execution.uptime_pct_1h}%, "
            f"reconnects: {health.trade_execution.reconnect_count_1h})",
        ]
        return "\n".join(lines)

    # ─── Connection reliability (BQ-716) ───────────────────────────────────────

    def start_watchdog(
        self,
        *,
        degraded_threshold: float = 30.0,
        failed_threshold: float = 90.0,
        poll_interval: float = 5.0,
    ) -> None:
        """Start the heartbeat watchdog for all registered connections.

        Creates a ``ConnectionWatchdog``, registers all current connections,
        and starts the daemon thread.  Idempotent — calling again while running
        is a no-op.

        Args:
            degraded_threshold: Seconds of silence before DEGRADED.
            failed_threshold: Seconds of silence before FAILED.
            poll_interval: Watchdog poll interval in seconds.
        """
        # Lazy import to avoid circular dependency (watchdog imports ConnectionManager)
        from .connection_watchdog import ConnectionWatchdog

        if self._watchdog is not None and self._watchdog.is_running:
            logger.debug("[ConnectionManager] Watchdog already running")
            return

        self._watchdog = ConnectionWatchdog(
            self,
            degraded_threshold=degraded_threshold,
            failed_threshold=failed_threshold,
            poll_interval=poll_interval,
        )

        # Register all existing connections with the watchdog
        with self._lock:
            for role, state_mgr in self._connections.items():
                self._watchdog.register(role, state_mgr)

        self._watchdog.start()
        logger.info("[ConnectionManager] Watchdog started")

    def stop_watchdog(self) -> None:
        """Stop the heartbeat watchdog (idempotent)."""
        if self._watchdog is not None:
            self._watchdog.stop()
            logger.info("[ConnectionManager] Watchdog stopped")

    def handle_disconnect(self, role: str | ConnectionRole) -> None:
        """Handle a connection that has transitioned to FAILED.

        Called by the watchdog when heartbeat silence exceeds the FAILED
        threshold.  The state transition to FAILED is already handled by the
        watchdog calling ``state_mgr.transition_to()`` directly; this method
        provides the hook for additional reconnect / alert logic.
        """
        if isinstance(role, str):
            role = ConnectionRole(role)

        logger.warning(
            "[ConnectionManager] %s disconnected — evaluating reconnect strategy",
            role.value,
        )

    def handle_token_refresh(self, access_token: str) -> None:
        """Update internal auth state after a successful token refresh.

        Args:
            access_token: The refreshed access token string.
        """
        self._auth_token = access_token
        logger.info(
            "[ConnectionManager] Auth token updated (access=%s…)",
            access_token[:8] if access_token else "????????",
        )

    def refresh_oauth_if_needed(
        self,
        credentials_path: str | Path | None = None,
    ) -> None:
        """Check and refresh the OAuth token if needed.

        Uses :class:`TokenLifecycle` to ensure the token is valid,
        refreshing if within the 5-day buffer before expiry.

        Args:
            credentials_path: Unused — kept for API compatibility.
                              Tokens come from .env via CredentialStore.
        """
        oauth_logger = logging.getLogger("ayumi.connection.oauth")

        if self._cred_store is None:
            self._cred_store = CredentialStore(".env")
        if self._token_lifecycle is None:
            self._token_lifecycle = TokenLifecycle(self._cred_store)

        try:
            token = self._token_lifecycle.ensure_valid()
            self.handle_token_refresh(token)
            oauth_logger.debug(
                "[OAuth] Token is current (access=%s…)",
                token[:8],
            )
        except Exception as exc:
            oauth_logger.warning("[OAuth] Token refresh failed: %s", exc)

    def decide_reconnect(
        self,
        error: Exception,
        attempt: int | None = None,
    ) -> ReconnectDecision:
        """Classify an error and decide reconnection strategy.

        Maps the exception to a cTrader error code, classifies it into a
        recovery tier, and uses :class:`ReconnectStrategy` to decide whether
        to retry, skip, or halt.

        Args:
            error: The exception that caused the connection failure.
            attempt: Override the attempt counter.  If ``None``, uses and
                increments the internal counter.

        Returns:
            :class:`ReconnectDecision` with action and sleep duration.
        """
        if attempt is None:
            self._reconnect_attempt += 1
            attempt = self._reconnect_attempt

        error_code = self._exception_to_error_code(error)
        error_msg = str(error) or type(error).__name__

        classified = classify_error(error_code, error_msg)
        decision = self._reconnect_strategy.decide(classified, attempt=attempt)

        # Record the error tier for metrics
        self.observe_error(classified.tier)

        return decision

    @staticmethod
    def _exception_to_error_code(error: Exception) -> str:
        """Map a Python exception to a cTrader error code string.

        Checks for explicit ``code`` / ``error_code`` attributes first,
        then falls back to type-name pattern matching.
        """
        # Check for explicit code attribute
        code = getattr(error, "code", None) or getattr(error, "error_code", None)
        if code:
            return str(code).upper()

        exc_name = type(error).__name__.upper()

        _DIRECT = {
            "TIMEOUTERROR": "HEARTBEAT_TIMEOUT",
            "CONNECTIONERROR": "CONNECTION_LOST",
            "CONNECTIONRESETERROR": "TCP_RESET",
            "CONNECTIONREFUSEDERROR": "CONNECTION_LOST",
            "CONNECTIONABORTEDERROR": "TCP_RESET",
            "OSERROR": "CONNECTION_LOST",
        }

        if exc_name in _DIRECT:
            return _DIRECT[exc_name]

        # Pattern matching on exception name
        if "AUTH" in exc_name or "TOKEN" in exc_name or "PERMISSION" in exc_name:
            return "AUTH_EXPIRED"
        if "TIMEOUT" in exc_name:
            return "REQUEST_TIMEOUT"
        if "CONNECTION" in exc_name or "SOCKET" in exc_name:
            return "CONNECTION_LOST"

        return "UNKNOWN"

    def reset_reconnect_state(self) -> None:
        """Reset the reconnect attempt counter and jitter state.

        Call after a successful reconnection to reset backoff.
        """
        self._reconnect_attempt = 0
        self._consecutive_auth_failures = 0
        self._reconnect_strategy.reset()

    # ─── Auth failure escalation (2026-07-03) ─────────────────────────────

    def record_auth_success(self) -> None:
        """Reset the consecutive auth failure counter after successful auth.

        Call this whenever any connection successfully authenticates.
        Ensures transient auth bursts don't permanently poison the state.
        """
        if self._consecutive_auth_failures > 0:
            logger.info(
                "[ConnectionManager] Auth success — resetting failure counter (was %d)",
                self._consecutive_auth_failures,
            )
        self._consecutive_auth_failures = 0

    def record_auth_failure(self) -> bool:
        """Track consecutive auth failures and decide if full reconnect is needed.

        Returns ``True`` when the failure threshold is reached, signalling
        that the caller should do a full reconnect (tear down TCP, create new
        connection, fresh auth) instead of looping on the same connection.

        The counter resets automatically when the threshold is hit, so a
        single threshold breach triggers one full-reconnect signal.

        Returns:
            ``True`` if full reconnect is recommended, ``False`` otherwise.
        """
        self._consecutive_auth_failures += 1

        if self._consecutive_auth_failures >= AUTH_FULL_RECONNECT_THRESHOLD:
            logger.warning(
                "[ConnectionManager] Auth failure threshold reached (%d/%d) — recommending full reconnect",
                self._consecutive_auth_failures,
                AUTH_FULL_RECONNECT_THRESHOLD,
            )
            self._consecutive_auth_failures = 0
            return True

        logger.debug(
            "[ConnectionManager] Auth failure %d/%d",
            self._consecutive_auth_failures,
            AUTH_FULL_RECONNECT_THRESHOLD,
        )
        return False

    # ─── Auth retry (BQ-1330a) ──────────────────────────────────────────────

    def authenticate_with_retry(
        self,
        auth_fn: callable,
        *,
        max_attempts: int = AUTH_RETRY_MAX_ATTEMPTS,
        backoff_seconds: tuple[float, ...] = AUTH_RETRY_BACKOFF_SECONDS,
    ) -> bool:
        """Attempt authentication with retry on failure.

        BQ-1330a: Wraps any auth callable so the engine FSM survives
        transient auth failures instead of permanently dying.  The existing
        FSM states are NOT changed — this method simply retries the callable
        and lets the caller handle state transitions.

        After ``max_attempts`` failures, returns ``False`` so the caller can
        decide whether to halt.  The engine dying after all retries are
        exhausted is acceptable per spec.

        Args:
            auth_fn: Callable that returns ``True`` on success, ``False`` or
                raises on failure.
            max_attempts: Maximum number of attempts (default 3).
            backoff_seconds: Exponential backoff schedule in seconds,
                applied between attempts (default 1s, 2s, 4s).

        Returns:
            ``True`` if authentication eventually succeeded,
            ``False`` if all attempts were exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = auth_fn()
                if result:
                    if attempt > 1:
                        logger.info(
                            "[ConnectionManager] Auth succeeded on attempt %d/%d",
                            attempt,
                            max_attempts,
                        )
                    self.reset_reconnect_state()
                    self.record_auth_success()
                    return True
                # auth_fn returned False — treat as failure
                logger.warning(
                    "[ConnectionManager] Auth attempt %d/%d returned False",
                    attempt,
                    max_attempts,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[ConnectionManager] Auth attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    exc,
                )

            # Sleep before next attempt (skip on last attempt)
            if attempt < max_attempts:
                sleep_idx = min(attempt - 1, len(backoff_seconds) - 1)
                sleep_time = backoff_seconds[sleep_idx]
                logger.info(
                    "[ConnectionManager] Retrying auth in %.1fs (attempt %d/%d)",
                    sleep_time,
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(sleep_time)

        logger.error(
            "[ConnectionManager] Auth failed after %d attempts%s",
            max_attempts,
            f": {last_error}" if last_error else "",
        )
        return False
