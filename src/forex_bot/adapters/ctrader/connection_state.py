"""Shared connection state machine for cTrader Open API connections.

Provides a thread-safe state machine that both the spot feed and trade
client report to. This gives a unified view of connection health across
all cTrader connections.

State transition rules (from research doc section 2):
    - AUTHENTICATED → DEGRADED: heartbeat not received within 15s
    - DEGRADED → RECONNECTING: heartbeat timeout > 30s or send/recv error
    - DEGRADED → AUTHENTICATED: recovered
    - Any → RECONNECTING: on connection error
    - RECONNECTING → CONNECTING: after backoff delay
    - RECONNECTING → FAILED: after max retries (10)
    - FAILED → DISCONNECTED: via explicit reset
    - FAILED → CONNECTING / RECONNECTING / CONNECTED / APP_AUTHENTICATING:
      recovery transitions (added 2026-07-03: transient auth bursts no longer
      trap the connection in FAILED state)
"""

import logging
import threading
from collections.abc import Callable
from enum import Enum
from typing import Optional

logger = logging.getLogger("ayumi.connection_state")


class ConnectionState(Enum):
    """Connection lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    APP_AUTHENTICATING = "app_authenticating"
    ACCT_AUTHENTICATING = "acct_authenticating"
    AUTHENTICATED = "authenticated"
    SUSPENDED = "suspended"
    SUBSCRIBING = "subscribing"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


# ── Transition table ─────────────────────────────────────────────────────────
# Maps (current_state, target_state) → True if the transition is valid.
# Keys not present are invalid transitions that will be rejected.

_VALID_TRANSITIONS: dict[tuple[ConnectionState, ConnectionState], bool] = {
    # Initial connect sequence
    (ConnectionState.DISCONNECTED, ConnectionState.CONNECTING): True,
    (ConnectionState.CONNECTING, ConnectionState.CONNECTED): True,
    (ConnectionState.CONNECTING, ConnectionState.FAILED): True,
    (ConnectionState.CONNECTING, ConnectionState.DISCONNECTED): True,
    (ConnectionState.CONNECTED, ConnectionState.APP_AUTHENTICATING): True,
    (ConnectionState.CONNECTED, ConnectionState.DISCONNECTED): True,
    (ConnectionState.CONNECTED, ConnectionState.RECONNECTING): True,
    (ConnectionState.APP_AUTHENTICATING, ConnectionState.ACCT_AUTHENTICATING): True,
    (ConnectionState.APP_AUTHENTICATING, ConnectionState.FAILED): True,
    (ConnectionState.APP_AUTHENTICATING, ConnectionState.RECONNECTING): True,
    (ConnectionState.ACCT_AUTHENTICATING, ConnectionState.AUTHENTICATED): True,
    (ConnectionState.ACCT_AUTHENTICATING, ConnectionState.FAILED): True,
    (ConnectionState.ACCT_AUTHENTICATING, ConnectionState.RECONNECTING): True,
    # Operational transitions
    (ConnectionState.AUTHENTICATED, ConnectionState.DEGRADED): True,
    (ConnectionState.AUTHENTICATED, ConnectionState.RECONNECTING): True,
    (ConnectionState.AUTHENTICATED, ConnectionState.DISCONNECTED): True,
    (ConnectionState.AUTHENTICATED, ConnectionState.FAILED): True,
    (ConnectionState.AUTHENTICATED, ConnectionState.SUSPENDED): True,
    (ConnectionState.AUTHENTICATED, ConnectionState.SUBSCRIBING): True,
    # Suspended transitions
    (ConnectionState.SUSPENDED, ConnectionState.CONNECTING): True,
    (ConnectionState.SUSPENDED, ConnectionState.DISCONNECTED): True,
    # Subscription replay transitions
    (ConnectionState.SUBSCRIBING, ConnectionState.AUTHENTICATED): True,
    (ConnectionState.SUBSCRIBING, ConnectionState.RECONNECTING): True,
    # Degraded transitions
    (ConnectionState.DEGRADED, ConnectionState.AUTHENTICATED): True,
    (ConnectionState.DEGRADED, ConnectionState.RECONNECTING): True,
    (ConnectionState.DEGRADED, ConnectionState.DISCONNECTED): True,
    (ConnectionState.DEGRADED, ConnectionState.FAILED): True,
    # Reconnecting transitions
    (ConnectionState.RECONNECTING, ConnectionState.CONNECTING): True,
    (ConnectionState.RECONNECTING, ConnectionState.CONNECTED): True,
    (ConnectionState.RECONNECTING, ConnectionState.APP_AUTHENTICATING): True,
    (ConnectionState.RECONNECTING, ConnectionState.FAILED): True,
    (ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED): True,
    # Failed transitions — allow recovery paths so transient auth bursts
    # don't permanently trap the connection (BQ: failed-state-sticky bug).
    # The FAILED state must permit re-entry into the normal reconnect/auth
    # lifecycle; without these, TCP reconnects but state transitions are
    # rejected, causing the connection to fight itself.
    (ConnectionState.FAILED, ConnectionState.DISCONNECTED): True,
    (ConnectionState.FAILED, ConnectionState.CONNECTING): True,  # manual retry
    (ConnectionState.FAILED, ConnectionState.RECONNECTING): True,  # recovery
    (ConnectionState.FAILED, ConnectionState.CONNECTED): True,  # TCP reconnected
    (ConnectionState.FAILED, ConnectionState.APP_AUTHENTICATING): True,  # re-auth
    # Allow self-transitions for idempotent calls (no-op)
    (ConnectionState.DISCONNECTED, ConnectionState.DISCONNECTED): True,
    (ConnectionState.CONNECTING, ConnectionState.CONNECTING): True,
    (ConnectionState.CONNECTED, ConnectionState.CONNECTED): True,
    (ConnectionState.AUTHENTICATED, ConnectionState.AUTHENTICATED): True,
    (ConnectionState.SUSPENDED, ConnectionState.SUSPENDED): True,
    (ConnectionState.SUBSCRIBING, ConnectionState.SUBSCRIBING): True,
    (ConnectionState.DEGRADED, ConnectionState.DEGRADED): True,
    (ConnectionState.RECONNECTING, ConnectionState.RECONNECTING): True,
    (ConnectionState.FAILED, ConnectionState.FAILED): True,
}


def is_valid_transition(
    current: ConnectionState,
    target: ConnectionState,
) -> bool:
    """Check whether a state transition is allowed."""
    return _VALID_TRANSITIONS.get((current, target), False)


class ConnectionStateManager:
    """Thread-safe connection state machine.

    Tracks the state of a single connection (spot feed or trade client)
    and emits callbacks on state transitions.

    Multiple managers can be created — one per connection — to give a
    unified health view across all cTrader connections.
    """

    def __init__(self, name: str = "connection"):
        self._name = name
        self._state = ConnectionState.DISCONNECTED
        self._lock = threading.Lock()
        self._callbacks: list[Callable] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> ConnectionState:
        """Current state (lock-free read of an enum attribute is safe in CPython)."""
        return self._state

    @property
    def is_operational(self) -> bool:
        """True when AUTHENTICATED or DEGRADED (can still send requests)."""
        return self._state in (ConnectionState.AUTHENTICATED, ConnectionState.DEGRADED)

    @property
    def is_authenticated(self) -> bool:
        return self._state == ConnectionState.AUTHENTICATED

    @property
    def is_failed(self) -> bool:
        return self._state == ConnectionState.FAILED

    def transition_to(
        self,
        new_state: ConnectionState,
        reason: str = "",
        metadata: Optional[dict] = None,
    ) -> bool:
        """Attempt a state transition.

        Returns True if the transition was applied, False if it was rejected.
        Callbacks are fired only on actual transitions (not self-transitions).
        """
        with self._lock:
            old_state = self._state

            if not is_valid_transition(old_state, new_state):
                logger.warning(
                    "[%s] Rejected state transition: %s → %s (reason: %s)",
                    self._name,
                    old_state.value,
                    new_state.value,
                    reason,
                )
                return False

            self._state = new_state
            callbacks = list(self._callbacks)

        # Fire callbacks outside the lock to prevent deadlocks
        if old_state != new_state:
            logger.info(
                "[%s] State transition: %s → %s (reason: %s)",
                self._name,
                old_state.value,
                new_state.value,
                reason,
            )
            for callback in callbacks:
                try:
                    callback(old_state, new_state, reason, metadata or {})
                except Exception as exc:
                    logger.error(
                        "[%s] State change callback error: %s",
                        self._name,
                        exc,
                    )
        return True

    def on_state_change(
        self,
        callback: Callable[[ConnectionState, ConnectionState, str, dict], None],
    ) -> None:
        """Register a callback for state transitions.

        Callback signature: (old_state, new_state, reason, metadata)
        """
        with self._lock:
            self._callbacks.append(callback)

    def reset(self) -> None:
        """Force-reset to DISCONNECTED (only allowed from FAILED)."""
        self.transition_to(ConnectionState.DISCONNECTED, reason="manual_reset")
