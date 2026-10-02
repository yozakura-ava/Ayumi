"""CTraderConnection — TCP connection management for cTrader OpenAPI.

Handles TCP connect/disconnect, protobuf message send/receive,
reconnection with exponential backoff, and health monitoring.

This module is extracted from open_api_spot_feed.py to separate
connection concerns from business logic (subscriptions, tick handling,
order execution).
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from typing import Callable, Optional

from ctrader_open_api import Client, TcpProtocol
from twisted.internet import reactor

from .connection_state import ConnectionState, ConnectionStateManager
from .market_hours import is_forex_market_closed
from .reactor_manager import ReactorManager

logger = logging.getLogger("ayumi.ctrader_connection")

# ── Reconnection defaults ──────────────────────────────────────────────────

_DEFAULT_CONNECT_TIMEOUT = 15
_DEFAULT_SEND_TIMEOUT = 10
_MAX_RECONNECT_ATTEMPTS = 5
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_MAX_SEC = 60.0

# ── Health monitoring ──────────────────────────────────────────────────────

_HEARTBEAT_DEGRADED_SEC = 35.0
_HEARTBEAT_RECONNECT_SEC = 60.0
_HEALTH_CHECK_INTERVAL = 10.0


class CTraderConnection:
    """Manages TCP connection lifecycle to cTrader OpenAPI.

    Responsibilities:
        - TCP connect/disconnect via ctrader_open_api Client
        - Protobuf message send/receive with Deferred pattern
        - Reconnection with exponential backoff + jitter
        - Health monitoring (heartbeat staleness detection)
        - Connection state machine integration

    Does NOT handle:
        - Authentication (delegated to CTraderAuth)
        - Subscriptions
        - Tick/bar processing
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        state_manager: Optional[ConnectionStateManager] = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        max_reconnect_attempts: int = _MAX_RECONNECT_ATTEMPTS,
        backoff_base: float = _BACKOFF_BASE_SEC,
        backoff_max: float = _BACKOFF_MAX_SEC,
        health_check_interval: float = _HEALTH_CHECK_INTERVAL,
        heartbeat_timeout_sec: float = _HEARTBEAT_RECONNECT_SEC,
    ):
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._max_reconnect_attempts = max_reconnect_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._health_check_interval = health_check_interval
        self._heartbeat_timeout_sec = heartbeat_timeout_sec

        # State
        self._state_mgr = state_manager or ConnectionStateManager(name="connection")
        self._reactor_manager = ReactorManager()
        self._client: Optional[Client] = None
        self._connected = threading.Event()
        self._connected_at: Optional[float] = None
        self._running = False

        # Health tracking
        self._last_heartbeat_recv: float = time.monotonic()
        self._health_timer: Optional[threading.Timer] = None
        self._stop_event = threading.Event()

        # Reconnection tracking
        self._reconnect_count: int = 0
        self._backoff_current: float = backoff_base

        # Callbacks
        self._on_connected_callbacks: list[Callable] = []
        self._on_disconnected_callbacks: list[Callable] = []
        self._on_feed_dead_callbacks: list[Callable] = []
        self._on_message_callback: Optional[Callable] = None

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def uptime_seconds(self) -> float:
        if self._connected_at is None:
            return 0.0
        return time.monotonic() - self._connected_at

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def state_manager(self) -> ConnectionStateManager:
        return self._state_mgr

    @property
    def client(self) -> Optional[Client]:
        return self._client

    # ── Callback registration ──────────────────────────────────────────────

    def on_connected(self, callback: Callable) -> None:
        self._on_connected_callbacks.append(callback)

    def on_disconnected(self, callback: Callable) -> None:
        self._on_disconnected_callbacks.append(callback)

    def on_feed_dead(self, callback: Callable) -> None:
        """Register callback for when reconnect attempts are exhausted."""
        self._on_feed_dead_callbacks.append(callback)

    def set_message_callback(self, callback: Callable) -> None:
        """Set the callback for incoming protobuf messages."""
        self._on_message_callback = callback

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Establish TCP connection to cTrader API host.

        Returns True if connection succeeds within connect_timeout.
        """
        self._connected.clear()
        self._stop_event.clear()
        self._state_mgr.transition_to(
            ConnectionState.CONNECTING,
            reason="tcp_connect_initiated",
        )

        self._client = Client(
            self._host,
            self._port,
            TcpProtocol,
        )
        self._client.setConnectedCallback(self._handle_connected)
        self._client.setDisconnectedCallback(self._handle_disconnected)

        self._reactor_manager.ensure_running()
        reactor.callFromThread(self._client.startService)

        if not self._connected.wait(timeout=self._connect_timeout):
            logger.error("Connection timeout to %s:%d", self._host, self._port)
            return False
        return True

    def disconnect(self) -> None:
        """Disconnect from cTrader API host."""
        self._running = False
        self._stop_event.set()

        if self._health_timer is not None:
            self._health_timer.cancel()
            self._health_timer = None

        if self._client:
            try:
                reactor.callFromThread(self._client.stopService)
            except Exception:  # noqa: S110 — fire-and-forget stopService during explicit disconnect; client already gone
                pass
            self._client = None

        self._connected.clear()
        self._state_mgr.transition_to(
            ConnectionState.DISCONNECTED,
            reason="explicit_disconnect",
        )

    # ── Message I/O ────────────────────────────────────────────────────────

    def send_and_wait(
        self,
        message,
        timeout: float = _DEFAULT_SEND_TIMEOUT,
        *,
        prefix: str = "conn",
        client_msg_id: str | None = None,
    ):
        """Send a protobuf message and wait for response via Deferred.

        Returns the response protobuf, or None on timeout/error.
        """
        if self._client is None:
            logger.error("Cannot send: not connected")
            return None

        event = threading.Event()
        result = [None]
        msg_id = client_msg_id or f"{prefix}_{uuid.uuid4().hex}"

        def on_success(proto_res):
            result[0] = proto_res
            event.set()

        def on_error(failure):
            logger.error("Send-and-wait failed: %s", failure)
            event.set()

        def do_send():
            d = self._client.send(
                message,
                clientMsgId=msg_id,
                responseTimeoutInSeconds=timeout,
            )
            d.addCallbacks(on_success, on_error)

        reactor.callFromThread(do_send)

        if not event.wait(timeout=timeout + 5):
            logger.error("Send-and-wait timeout")
            return None
        return result[0]

    def send(self, message) -> None:
        """Fire-and-forget send via reactor thread."""
        if self._client is None:
            return
        try:
            d = self._client.send(message)
            d.addErrback(lambda f: logger.debug("Send errback: %s", f))
        except Exception as exc:
            logger.debug("Send failed: %s", exc)

    # ── Health monitoring ──────────────────────────────────────────────────

    def start_health_monitor(self) -> None:
        """Start the periodic health check timer."""
        self._running = True
        self._schedule_health_check()

    def stop_health_monitor(self) -> None:
        """Stop the health check timer."""
        self._running = False
        self._stop_event.set()
        if self._health_timer is not None:
            self._health_timer.cancel()
            self._health_timer = None

    def notify_heartbeat(self) -> None:
        """Call when any message is received (updates heartbeat timestamp)."""
        self._last_heartbeat_recv = time.monotonic()

    # ── Internal callbacks ─────────────────────────────────────────────────

    def _handle_connected(self, client) -> None:
        """Internal: TCP connection established."""
        logger.info("Connected to %s:%d", self._host, self._port)
        self._connected_at = time.monotonic()
        self._connected.set()
        self._last_heartbeat_recv = time.monotonic()
        self._state_mgr.transition_to(
            ConnectionState.CONNECTED,
            reason="tcp_connected",
        )

        for cb in self._on_connected_callbacks:
            try:
                cb(self)
            except Exception as exc:
                logger.error("on_connected callback error: %s", exc)

    def _handle_disconnected(self, client, reason) -> None:
        """Internal: TCP connection lost."""
        logger.warning("Disconnected from %s:%d: %s", self._host, self._port, reason)
        self._connected.clear()
        self._state_mgr.transition_to(
            ConnectionState.RECONNECTING,
            reason=f"disconnected: {reason}",
        )

        for cb in self._on_disconnected_callbacks:
            try:
                cb(self, reason)
            except Exception as exc:
                logger.error("on_disconnected callback error: %s", exc)

    # ── Reconnection with exponential backoff ──────────────────────────────

    def attempt_reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff + jitter.

        Returns True if reconnect succeeds, False if attempts exhausted.
        After max_reconnect_attempts failures, emits "feed_dead" event.
        """
        self._reconnect_count += 1

        if self._reconnect_count > self._max_reconnect_attempts:
            logger.critical(
                "Reconnect exhausted: %d attempts (max=%d) — emitting feed_dead",
                self._reconnect_count,
                self._max_reconnect_attempts,
            )
            self._state_mgr.transition_to(
                ConnectionState.FAILED,
                reason="reconnect_exhausted",
            )
            for cb in self._on_feed_dead_callbacks:
                try:
                    cb(self)
                except Exception as exc:
                    logger.error("feed_dead callback error: %s", exc)
            return False

        # Exponential backoff with jitter
        backoff = min(
            self._backoff_base * (2 ** (self._reconnect_count - 1)),
            self._backoff_max,
        )
        jitter = random.uniform(0, backoff * 0.25)  # noqa: S311 — non-cryptographic jitter on reconnection backoff timer
        delay = backoff + jitter
        logger.info(
            "Reconnect attempt %d/%d in %.1fs (backoff=%.1fs)",
            self._reconnect_count,
            self._max_reconnect_attempts,
            delay,
            backoff,
        )
        time.sleep(delay)

        # Disconnect old client
        if self._client:
            try:
                reactor.callFromThread(self._client.stopService)
            except Exception:  # noqa: S110 — fire-and-forget stopService during reconnect; old client about to be replaced
                pass
            self._client = None

        self._connected.clear()
        success = self.connect()

        if success:
            self._reconnect_count = 0
            self._backoff_current = self._backoff_base
            logger.info("Reconnection successful")
        else:
            self._backoff_current = min(self._backoff_current * 2, self._backoff_max)

        return success

    def reset_reconnect(self) -> None:
        """Reset reconnect state after a successful connection."""
        self._reconnect_count = 0
        self._backoff_current = self._backoff_base

    # ── Health check internals ─────────────────────────────────────────────

    def _schedule_health_check(self) -> None:
        if not self._running:
            return
        self._health_timer = threading.Timer(
            self._health_check_interval,
            self._health_check_loop,
        )
        self._health_timer.daemon = True
        self._health_timer.start()

    def _health_check_loop(self) -> None:
        if not self._running:
            return
        try:
            # Send heartbeat to server (fire-and-forget)
            if self._running and self._client and self.is_connected:
                try:
                    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                        ProtoHeartbeatEvent,
                    )
                    from twisted.internet import reactor

                    reactor.callFromThread(self.send, ProtoHeartbeatEvent())
                except Exception:  # noqa: S110 — fire-and-forget heartbeat send; don't let send failure crash health check
                    pass
            self._check_heartbeat()
        except Exception as exc:
            logger.error("Health check error: %s", exc, exc_info=True)
        self._schedule_health_check()

    def _check_heartbeat(self) -> None:
        """Check heartbeat staleness and trigger reconnect if needed."""
        current_state = self._state_mgr.state
        if current_state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.FAILED,
            ConnectionState.APP_AUTHENTICATING,
            ConnectionState.ACCT_AUTHENTICATING,
        ):
            return

        elapsed = time.monotonic() - self._last_heartbeat_recv

        # During forex market close (Fri 21:55 UTC - Sun 21:00 UTC), no ticks arrive.
        # Don't trigger heartbeat-based reconnects when the market is closed.
        if is_forex_market_closed():
            return

        if elapsed >= self._heartbeat_timeout_sec:
            logger.warning(
                "No heartbeat for %.1fs (threshold: %.0fs) — triggering reconnect",
                elapsed,
                self._heartbeat_timeout_sec,
            )
            self._state_mgr.transition_to(
                ConnectionState.RECONNECTING,
                reason=f"heartbeat_timeout:{elapsed:.0f}s",
            )
        elif elapsed >= _HEARTBEAT_DEGRADED_SEC:
            logger.warning(
                "No heartbeat for %.1fs — DEGRADED",
                elapsed,
            )
            self._state_mgr.transition_to(
                ConnectionState.DEGRADED,
                reason=f"heartbeat_stale:{elapsed:.0f}s",
            )
