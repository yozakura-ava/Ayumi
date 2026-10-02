"""cTrader session — TCP connection lifecycle with reactor bridge.

Lifecycle: DISCONNECTED → CONNECTING → AUTHENTICATING → CONNECTED → SUBSCRIBED
On error: → RECONNECTING → (back to CONNECTING)

The reactor bridge pattern (Amendment A1):
- Caller thread: session.send(msg) → reactor.callFromThread(do_send) → event.wait(30s)
- Reactor thread: do_send → d = client.send(msg) → d.addCallback(on_success) → d.addErrback(on_error)
- BOTH callback and errback call event.set()

Reference: BQ-1043 Phase 2a, Amendment A1 (Reactor Bridge Pattern)
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from ctrader_open_api import Client, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOASubscribeSpotsReq,
)
from ctrader_open_api.protobuf import Protobuf
from twisted.internet import reactor

from .credential_store import CredentialStore
from .protocols import SessionState
from .reactor_manager import ReactorManager
from .token_lifecycle import TokenLifecycle

logger = logging.getLogger("ayumi.session")

# ── Payload type constants ─────────────────────────────────────────────────

_APP_AUTH_RES = 2101
_ACCT_AUTH_RES = 2103

# ── Exceptions ─────────────────────────────────────────────────────────────


class SendError(Exception):
    """Raised when a reactor-bridged send fails (errback fires)."""


class SessionAuthError(Exception):
    """Raised when app or account authentication fails."""


# ── cTraderSession ─────────────────────────────────────────────────────────


class cTraderSession:
    """Manages a single TCP connection to cTrader.

    Owns the TCP socket and the reactor bridge. Does NOT handle
    order execution logic, market data processing, or health monitoring.

    Responsibilities:
        - TCP connect/disconnect
        - App auth + account auth
        - Reactor bridge for thread-safe send/receive
        - Message routing to registered handlers

    Lifecycle::
        DISCONNECTED → CONNECTING → AUTHENTICATING → CONNECTED → SUBSCRIBED
    """

    def __init__(
        self,
        token_lifecycle: TokenLifecycle,
        credential_store: CredentialStore,
        host: str = "demo.ctraderapi.com",
        port: int = 5035,
    ):
        self._token_lifecycle = token_lifecycle
        self._credential_store = credential_store
        self._host = host
        self._port = port

        # SDK client — created on connect
        self._client: Optional[Client] = None

        # State machine
        self._state = SessionState.DISCONNECTED
        self._state_lock = threading.Lock()

        # TCP connection synchronisation
        self._tcp_connected = threading.Event()

        # Message handlers: payload_type → list of handlers
        self._handlers: dict[int, list[Callable]] = {}

        # Account ID from credential store
        creds = credential_store.get()
        self._account_id: int = creds.account_id

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        """Current session state."""
        return self._state

    @property
    def is_operational(self) -> bool:
        """True when CONNECTED or SUBSCRIBED."""
        return self._state in (SessionState.CONNECTED, SessionState.SUBSCRIBED)

    @property
    def client(self) -> Optional[Client]:
        """Underlying SDK client (for modules that need it)."""
        return self._client

    # ── State machine ──────────────────────────────────────────────────────

    def _set_state(self, new_state: SessionState) -> None:
        with self._state_lock:
            old = self._state
            self._state = new_state
        logger.info("State: %s → %s", old.value, new_state.value)

    # ── Connection lifecycle ───────────────────────────────────────────────

    def connect(self) -> bool:
        """TCP connect + app auth + account auth.

        Returns True on success, False on failure.
        Fails fast on any error with a logged reason.
        """
        try:
            return self._do_connect()
        except Exception as exc:
            logger.error("Connection failed: %s", exc, exc_info=True)
            self._set_state(SessionState.FAILED)
            return False

    def _do_connect(self) -> bool:
        # ── Step 1: TCP connect ────────────────────────────────────────────
        self._set_state(SessionState.CONNECTING)
        self._tcp_connected.clear()

        self._client = Client(self._host, self._port, TcpProtocol)
        self._client.setConnectedCallback(self._on_tcp_connected)
        self._client.setDisconnectedCallback(self._on_tcp_disconnected)

        ReactorManager().ensure_running()
        if not reactor.running:
            raise RuntimeError("reactor must be running before connect")

        reactor.callFromThread(self._client.startService)

        if not self._tcp_connected.wait(timeout=15):
            logger.error("TCP connect timeout to %s:%d", self._host, self._port)
            self._set_state(SessionState.FAILED)
            return False

        logger.info("TCP connected to %s:%d", self._host, self._port)

        # ── Step 2: App auth ───────────────────────────────────────────────
        self._set_state(SessionState.AUTHENTICATING)

        creds = self._credential_store.get()
        app_req = ProtoOAApplicationAuthReq(
            clientId=creds.client_id,
            clientSecret=creds.client_secret,
        )
        app_res = self.send(app_req, "app_auth", timeout=15)
        if app_res is None:
            self._set_state(SessionState.FAILED)
            raise SessionAuthError("App auth timed out")
        if not self._is_auth_response(app_res, _APP_AUTH_RES, "app"):
            self._set_state(SessionState.FAILED)
            raise SessionAuthError("App auth rejected")

        logger.info("App auth successful")

        # ── Step 3: Account auth (with valid token) ────────────────────────
        access_token = self._token_lifecycle.ensure_valid()

        acct_req = ProtoOAAccountAuthReq(
            ctidTraderAccountId=self._account_id,
            accessToken=access_token,
        )
        acct_res = self.send(acct_req, "acct_auth", timeout=15)
        if acct_res is None:
            self._set_state(SessionState.FAILED)
            raise SessionAuthError("Account auth timed out")
        if not self._is_auth_response(acct_res, _ACCT_AUTH_RES, "account"):
            self._set_state(SessionState.FAILED)
            raise SessionAuthError("Account auth rejected")

        logger.info("Account auth successful (account_id=%d)", self._account_id)

        # ── Register message callback ──────────────────────────────────────
        self._client.setMessageReceivedCallback(self._on_message)

        self._set_state(SessionState.CONNECTED)
        return True

    def subscribe_market_data(self, symbol_ids: list[int]) -> bool:
        """Subscribe to spot events for the given symbol IDs.

        Returns True on success, False on failure.
        """
        if not self.is_operational:
            logger.error(
                "Cannot subscribe: session not operational (state=%s)",
                self._state.value,
            )
            return False

        if not symbol_ids:
            logger.warning("subscribe_market_data called with empty symbol_ids")
            return False

        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = self._account_id
        for sid in symbol_ids:
            req.symbolId.append(sid)
        req.subscribeToSpotTimestamp = True

        res = self.send(req, "subscribe_spots", timeout=15)
        if res is None:
            logger.error("Subscribe market data timed out")
            return False

        self._set_state(SessionState.SUBSCRIBED)
        logger.info("Subscribed to %d symbols", len(symbol_ids))
        return True

    # ── Reactor bridge send ────────────────────────────────────────────────

    def send(
        self,
        message: Any,
        client_msg_id: str,
        timeout: float = 30.0,
    ) -> Any:
        """Send a protobuf message via the reactor bridge and wait for response.

        Dispatches ``client.send()`` to the reactor thread, then blocks the
        caller until the deferred fires (success or error) or the timeout
        expires.

        Args:
            message: Protobuf request message.
            client_msg_id: Unique ID for request correlation.
            timeout: Seconds to wait before returning None.

        Returns:
            Response protobuf on success, None on timeout.

        Raises:
            SendError: If the deferred errback fires.
        """
        if self._client is None:
            raise SendError("No client — not connected")

        event = threading.Event()
        result_holder: list[Any] = [None]
        error_holder: list[str | None] = [None]

        def do_send() -> None:
            try:
                d = self._client.send(
                    message,
                    clientMsgId=client_msg_id,
                    responseTimeoutInSeconds=timeout,
                )

                def on_success(res: Any) -> None:
                    result_holder[0] = res
                    event.set()

                def on_error(failure: Any) -> None:
                    logger.warning("Send failed (msg_id=%s): %s", client_msg_id, failure)
                    error_holder[0] = str(failure)
                    event.set()  # MUST fire on BOTH paths

                d.addCallbacks(on_success, on_error)
            except Exception as exc:
                error_holder[0] = str(exc)
                event.set()

        reactor.callFromThread(do_send)

        if not event.wait(timeout=timeout):
            logger.warning("Send timed out (msg_id=%s, timeout=%.1fs)", client_msg_id, timeout)
            return None

        if error_holder[0] is not None:
            raise SendError(error_holder[0])
        return result_holder[0]

    # ── Message routing ────────────────────────────────────────────────────

    def register_message_handler(
        self,
        payload_type: int,
        handler: Callable,
    ) -> None:
        """Register a handler for a specific payload type.

        Multiple handlers may be registered for the same payload type;
        all will be called on receipt.

        Used by ExecutionEventHandler, MarketDataFeed, etc.
        """
        self._handlers.setdefault(payload_type, []).append(handler)

    def _on_message(self, client: Any, message: Any) -> None:
        """Route incoming messages to registered handlers by payload type.

        This method is called by the SDK on the reactor thread.
        """
        payload_type = message.payloadType
        handlers = self._handlers.get(payload_type, [])
        for handler in handlers:
            try:
                handler(message)
            except Exception:
                logger.exception(
                    "Handler %s raised for payloadType=%d",
                    getattr(handler, "__name__", handler),
                    payload_type,
                )

    # ── Callbacks from SDK ─────────────────────────────────────────────────

    def _on_tcp_connected(self, _: Any = None) -> None:
        """Called by SDK when TCP connection is established."""
        self._tcp_connected.set()

    def _on_tcp_disconnected(self, _: Any = None, reason: Any = None) -> None:
        """Called by SDK when TCP connection is lost."""
        logger.warning("TCP disconnected: %s", reason)
        self._tcp_connected.clear()
        if self._state != SessionState.DISCONNECTED:
            self._set_state(SessionState.RECONNECTING)

    # ── Disconnect ─────────────────────────────────────────────────────────

    def disconnect(self) -> None:
        """Cleanly disconnect from cTrader.

        Stops the SDK client service and resets state.
        """
        if self._client is not None:
            try:
                ReactorManager().ensure_running()
                reactor.callFromThread(self._client.stopService)
            except Exception:
                logger.debug("Error stopping client service", exc_info=True)
            self._client = None

        self._tcp_connected.clear()
        self._set_state(SessionState.DISCONNECTED)
        logger.info("Session disconnected")

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _is_auth_response(response: Any, expected_type: int, stage: str) -> bool:
        """Check whether an auth response is a success.

        Auth responses have no explicit ``errorCode`` field on success.
        If ``errorCode`` is present and non-empty, auth failed.
        """
        if response is None:
            return False
        try:
            payload = Protobuf.extract(response)
        except Exception:
            logger.error("%s auth: failed to extract payload", stage)
            return False

        error_code = getattr(payload, "errorCode", None)
        if error_code:
            logger.error("%s auth rejected: errorCode=%s", stage, error_code)
            return False
        return True
