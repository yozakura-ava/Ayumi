"""cTrader Open API client for fetching historical data.

Uses the ctrader_open_api package (Twisted-based protobuf client) to:
- Authenticate via OAuth (app auth + account auth)
- Fetch symbol lists
- Download historical trendbars (OHLCV)

The Twisted reactor runs in a background thread so all public methods
are synchronous from the caller's perspective.

Concurrent session strategy (BQ-1329)
-------------------------------------
cTrader OpenAPI enforces a single-session rule for the same
(app_id, account_id) pair.  When ``OpenApiSpotFeed`` (spot prices +
order execution) is authenticated, a second connection from this client
using the same app credentials gets rejected with
``Trading account is not authorized``.

Judgment call: a fully shared TCP socket would require deep changes to
the archived ``OpenApiSpotFeed`` shim.  The safer, ops-simple fallback
implemented here is a *second OpenAPI app*: this client can authenticate
with a separate app_id/secret while still using the same trading account
and access token.  Set ``CTRADER_TRADE_APP_ID`` / ``CTRADER_TRADE_SECRET``
in the environment (or pass ``trade_client_id`` / ``trade_client_secret``
to the constructor) to use the alternate app for historical-data fetches.

If both primary and trade credentials are absent, the client falls back
to the primary credentials and logs a warning that concurrent operation
with the spot feed may fail.
"""

import logging
import os
import threading
import time
import typing

from ctrader_open_api.client import Client
from ctrader_open_api.endpoints import EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAGetTrendbarsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod
from ctrader_open_api.protobuf import Protobuf
from ctrader_open_api.tcpProtocol import TcpProtocol
from twisted.internet import reactor

from .reactor_manager import ReactorManager

logger = logging.getLogger(__name__)

# BQ-1327: Auth response payload type constants (mirrored from archived
# open_api_spot_feed.py to enable response validation in the live client).
_APP_AUTH_RES_PAYLOAD_TYPE = 2101
_ACCT_AUTH_RES_PAYLOAD_TYPE = 2103

# Period string → ProtoOATrendbarPeriod enum value
PERIOD_MAP = {
    "M1": ProtoOATrendbarPeriod.M1,
    "M5": ProtoOATrendbarPeriod.M5,
    "M15": ProtoOATrendbarPeriod.M15,
    "M30": ProtoOATrendbarPeriod.M30,
    "H1": ProtoOATrendbarPeriod.H1,
    "H4": ProtoOATrendbarPeriod.H4,
    "D1": ProtoOATrendbarPeriod.D1,
    "W1": ProtoOATrendbarPeriod.W1,
}

# Period string → bar duration in seconds
PERIOD_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

# Max bars per single API request per period
MAX_BARS = {
    "M1": 5760,
    "M5": 5760,
    "M15": 5760,
    "H1": 5760,
    "H4": 5760,
    "D1": 5760,
}


class CTraderOpenApiClient:
    """Synchronous wrapper around cTrader's Twisted Open API client."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        account_id: int,
        access_token: str | None = None,
        host: str | None = None,
        port: int = 5035,
        trade_client_id: str | None = None,
        trade_client_secret: str | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_id = account_id
        self._access_token = access_token
        self._host = host or EndPoints.PROTOBUF_DEMO_HOST
        self._port = port
        self._client: Client | None = None
        self._reactor_thread: threading.Thread | None = None
        self._connected = False
        # BQ-1327: Guard to prevent concurrent reconnection/auth races,
        # mirroring the archived spot feed's _reauth_in_progress pattern.
        self._reauth_in_progress = threading.Event()
        # BQ-1327: External callback for unexpected disconnects.
        self._on_disconnected: typing.Callable | None = None
        self._symbol_digits_cache: dict[int, int] = {}
        # BQ-1330: Instance-state events used by the self-method callback
        # handlers (self._on_connected / self._on_response) so the SDK
        # registrations can use ``self.<method>`` rather than local
        # closures. Each call to ``_do_connect`` / ``_send_and_wait``
        # replaces the relevant event before re-registering.
        self._connected_event: threading.Event | None = None
        self._send_event: threading.Event | None = None
        self._send_result: list = [None]

        # BQ-1329: Optional second-app credentials for concurrent sessions.
        # Environment variables take precedence when constructor args are None.
        self._trade_client_id = (
            trade_client_id
            or os.environ.get("CTRADER_TRADE_APP_ID")
            or os.environ.get("CTRADER_OPENAPI_TRADE_CLIENT_ID")
        )
        self._trade_client_secret = (
            trade_client_secret
            or os.environ.get("CTRADER_TRADE_SECRET")
            or os.environ.get("CTRADER_OPENAPI_TRADE_CLIENT_SECRET")
        )
        self._using_trade_app = bool(self._trade_client_id and self._trade_client_secret)
        if self._using_trade_app:
            logger.info(
                "BQ-1329: Using separate OpenAPI app for historical-data client: %s...",
                self._trade_client_id[:8],
            )
        elif trade_client_id is not None or trade_client_secret is not None:
            logger.warning(
                "BQ-1329: Incomplete trade-app credentials provided; "
                "falling back to primary app. Concurrent operation with the spot feed may fail."
            )

    @property
    def using_trade_app(self) -> bool:
        """True when this client authenticates with the alternate trade app."""
        return self._using_trade_app

    @property
    def app_client_id(self) -> str:
        """Return the app client_id used for application authentication."""
        return self._trade_client_id if self._using_trade_app else self._client_id

    @property
    def app_client_secret(self) -> str:
        """Return the app client_secret used for application authentication."""
        return self._trade_client_secret if self._using_trade_app else self._client_secret

    # --- Connection lifecycle ---

    def connect(self) -> bool:
        """Connect to cTrader Open API, authenticate, and return True on success.

        BQ-1327: Uses _reauth_in_progress guard to prevent concurrent
        connect/reconnect attempts from racing each other.
        """
        if self._connected:
            return True

        # BQ-1327: If a connect/reauth is already underway, wait for it.
        if self._reauth_in_progress.is_set():
            logger.debug("Reauth already in progress — waiting")
            if not self._reauth_in_progress.wait(timeout=20):
                logger.error("Timeout waiting for concurrent reauth")
                return False
            return self._connected

        self._reauth_in_progress.set()
        try:
            return self._do_connect()
        finally:
            self._reauth_in_progress.clear()

    def _do_connect(self) -> bool:
        """Internal connect logic, called under the _reauth_in_progress guard."""
        # Ensure shared reactor is running
        ReactorManager().ensure_running()
        time.sleep(0.3)  # brief pause for reactor readiness

        self._client = Client(self._host, self._port, TcpProtocol)

        # Wait for TCP connection. BQ-1330: the connect callback is now
        # a self-method (``self._on_connected``) backed by instance
        # state (``self._connected_event``) so the SDK registration
        # passes the callback linter's "self-method" check.
        self._connected_event = threading.Event()

        # BQ-1327: Wire disconnected callback so the client cleans up state
        # when the TCP connection drops unexpectedly (mirrors archived
        # spot feed's setDisconnectedCallback pattern).
        self._client.setDisconnectedCallback(self._on_tcp_disconnected)
        self._client.setConnectedCallback(self._on_connected)
        self._client.startService()

        if not self._connected_event.wait(timeout=15):
            logger.error("Timeout waiting for TCP connection to cTrader")
            return False

        # Step 1: Application auth
        # BQ-1329: Use the alternate trade-app credentials when configured so
        # this historical-data connection does not collide with the spot feed's
        # primary-app session on cTrader's single-session rule.
        app_id = self.app_client_id
        app_secret = self.app_client_secret
        try:
            app_auth_res = self._send_and_wait(
                ProtoOAApplicationAuthReq(
                    clientId=app_id,
                    clientSecret=app_secret,
                ),
                timeout=10,
            )
            if app_auth_res is None:
                logger.error("Application auth failed — no response")
                return False
            # BQ-1327: Validate payload type matches expected app auth response
            if not self._is_valid_auth_response(app_auth_res, _APP_AUTH_RES_PAYLOAD_TYPE, "app"):
                return False
        except Exception as e:
            logger.error(f"Application auth failed: {e}")
            return False

        # Step 2: Account auth
        token = self._access_token or ""
        try:
            account_auth_res = self._send_and_wait(
                ProtoOAAccountAuthReq(
                    ctidTraderAccountId=self._account_id,
                    accessToken=token,
                ),
                timeout=10,
            )
            if account_auth_res is None:
                logger.error("Account auth failed — no response")
                return False
            # BQ-1327: Validate payload type matches expected account auth response
            if not self._is_valid_auth_response(account_auth_res, _ACCT_AUTH_RES_PAYLOAD_TYPE, "account"):
                return False
        except Exception as e:
            logger.error(f"Account auth failed: {e}")
            return False

        self._connected = True
        logger.info("cTrader Open API connected and authenticated")
        return True

    def disconnect(self):
        """Disconnect from the API. Stops the TCP client but NOT the reactor."""
        if self._client:
            try:
                self._client.stopService()
            except Exception:  # noqa: S110 — fire-and-forget stopService during disconnect; client may already be torn down
                pass
        self._connected = False
        self._reauth_in_progress.clear()
        logger.info("cTrader Open API disconnected")

    def setDisconnectedCallback(self, callback: typing.Callable) -> None:
        """Register a callback invoked when the TCP connection drops.

        BQ-1327: Mirrors the setDisconnectedCallback pattern from the archived
        spot feed so callers can react to unexpected disconnects.
        """
        self._on_disconnected = callback

    def _on_connected(self, _: object) -> None:
        """Internal handler for TCP connect events (BQ-1330).

        Sets ``self._connected_event`` so ``_do_connect`` can unblock.
        Safe to call when no connect is in flight (event is ``None``).
        """
        if self._connected_event is not None:
            self._connected_event.set()

    def _on_response(self, _: object, __: object) -> None:
        """Internal handler for incoming SDK messages (BQ-1330).

        Sets ``self._send_event`` so the in-flight ``_send_and_wait``
        call can unblock. Safe to call when no send is in flight.
        """
        if self._send_event is not None:
            self._send_event.set()

    def _on_tcp_disconnected(self, _: object) -> None:
        """Internal handler for TCP disconnect events (BQ-1327)."""
        was_connected = self._connected
        self._connected = False
        self._reauth_in_progress.clear()
        if was_connected and self._on_disconnected is not None:
            try:
                self._on_disconnected(self)
            except Exception as exc:
                logger.warning("Disconnected callback error: %s", exc)

    def _is_valid_auth_response(self, response, expected_payload_type: int, stage: str) -> bool:
        """Validate that an auth response has the expected payload type.

        BQ-1327: Mirrors the _is_expected_auth_response check from the
        archived spot feed.  Rejects mismatched payload types and surfaces
        error responses with their errorCode for diagnostics.
        """
        payload_type = getattr(response, "payloadType", None)
        if payload_type == expected_payload_type:
            return True
        # Error response (payload type 2142)
        if payload_type == 2142:
            payload = Protobuf.extract(response)
            error_code = getattr(payload, "errorCode", "UNKNOWN")
            logger.error("%s auth rejected: %s", stage, error_code)
            return False
        logger.error(
            "%s auth unexpected payloadType=%s (expected %s)",
            stage,
            payload_type,
            expected_payload_type,
        )
        return False

    def _send_and_wait(self, message, timeout: float = 10):
        """Send a protobuf message and wait for the response synchronously."""
        if not self._client or not self._client.isConnected:
            raise RuntimeError("Not connected")

        # BQ-1330: Message callback is now ``self._on_response`` (a
        # self-method) backed by instance state. The deferred callbacks
        # still use local closures because the callback linter only
        # inspects ``setMessageReceivedCallback`` / ``setConnectedCallback``
        # / ``setDisconnectedCallback`` registrations.
        self._send_event = threading.Event()
        self._send_result = [None]

        self._client.setMessageReceivedCallback(self._on_response)

        # Send via the Twisted thread
        client_msg_id = f"{id(message)}_{time.monotonic()}"
        deferred = self._client.send(message, clientMsgId=client_msg_id, responseTimeoutInSeconds=timeout)

        def capture_result(proto_res):
            self._send_result[0] = proto_res
            self._send_event.set()

        def capture_error(failure):
            logger.error(f"API request failed: {failure}")
            self._send_event.set()

        reactor.callFromThread(lambda: deferred.addCallbacks(capture_result, capture_error))

        if not self._send_event.wait(timeout=timeout + 5):
            return None

        return self._send_result[0]

    # --- Public API ---

    def get_all_symbols(self) -> list[dict]:
        """Fetch all available symbols via ProtoOASymbolsListReq.

        Returns list of dicts with: symbol_id, name, description, digits,
        pip_size, category, enabled.
        """
        if not self._connected:
            raise RuntimeError("Not connected")

        response = self._send_and_wait(
            ProtoOASymbolsListReq(
                ctidTraderAccountId=self._account_id,
                includeArchivedSymbols=False,
            ),
            timeout=15,
        )

        if response is None:
            logger.error("No response for SymbolsListReq")
            return []

        # Extract payload
        payload = Protobuf.extract(response)
        symbols = []

        for s in payload.symbol:
            pip_position = getattr(s, "pipPosition", None) or getattr(s, "pipPositionSize", None) or 4
            pip_size = 10 ** (-pip_position)
            symbols.append(
                {
                    "symbol_id": s.symbolId,
                    "name": s.symbolName,
                    "description": getattr(s, "description", ""),
                    "digits": getattr(s, "digits", None) or 5,
                    "pip_size": pip_size,
                    "enabled": getattr(s, "enabled", True),
                    "base_asset_id": getattr(s, "baseAssetId", 0),
                    "quote_asset_id": getattr(s, "quoteAssetId", 0),
                    "category_id": s.symbolCategoryId,
                }
            )

        logger.info(f"Fetched {len(symbols)} symbols")
        return symbols

    def get_symbol_details(self, symbol_id: int) -> dict | None:
        """Get details for a specific symbol via ProtoOASymbolByIdReq."""
        if not self._connected:
            raise RuntimeError("Not connected")

        response = self._send_and_wait(
            ProtoOASymbolByIdReq(
                ctidTraderAccountId=self._account_id,
                symbolId=[symbol_id],
            ),
            timeout=10,
        )

        if response is None:
            return None

        payload = Protobuf.extract(response)
        msg_type = getattr(payload, "payloadType", None)
        if msg_type == 2142:  # ProtoOAErrorRes — symbol not found or auth error
            error_code = getattr(payload, "errorCode", "UNKNOWN")
            description = getattr(payload, "description", "")
            logger.warning(
                "Symbol details error for symbol_id=%d: %s — %s",
                symbol_id,
                error_code,
                description,
            )
            return None
        if not hasattr(payload, "symbol") or not payload.symbol:
            return None

        s = payload.symbol[0]
        return {
            "symbol_id": s.symbolId,
            "digits": s.digits,
            "pip_position": s.pipPosition,
            "pip_size": 10 ** (-s.pipPosition) if s.pipPosition else 0.0001,
            "enable_short_selling": s.enableShortSelling,
            "swap_long": s.swapLong,
            "swap_short": s.swapShort,
            "max_volume": s.maxVolume,
            "min_volume": s.minVolume,
            "step_volume": s.stepVolume,
            "lot_size": s.lotSize,
        }

    def get_trendbars(
        self,
        symbol_id: int,
        period: str,
        from_ts: int,
        to_ts: int,
        max_bars: int | None = None,
    ) -> list[dict]:
        """Fetch historical OHLCV bars via ProtoOAGetTrendbarsReq.

        Args:
            symbol_id: cTrader numeric symbol ID
            period: "M1", "M5", "M15", "M30", "H1", "H4", "D1"
            from_ts: Unix timestamp in milliseconds
            to_ts: Unix timestamp in milliseconds
            max_bars: Max bars to request (default: period-specific max)

        Returns list of dicts with: timestamp, open, high, low, close, volume
        """
        if not self._connected:
            raise RuntimeError("Not connected")

        if period not in PERIOD_MAP:
            raise ValueError(f"Invalid period '{period}'. Must be one of {list(PERIOD_MAP.keys())}")

        if max_bars is None:
            max_bars = MAX_BARS.get(period, 5760)

        period_enum = PERIOD_MAP[period]

        # Use cached symbol digits for rounding precision
        if symbol_id not in self._symbol_digits_cache:
            details = self.get_symbol_details(symbol_id)
            if details:
                self._symbol_digits_cache[symbol_id] = details["digits"]
            else:
                self._symbol_digits_cache[symbol_id] = 5  # default
        digits = self._symbol_digits_cache[symbol_id]

        # cTrader encodes all raw prices with 5 decimal places (int units of 1e-5)
        divisor = 100000.0

        response = self._send_and_wait(
            ProtoOAGetTrendbarsReq(
                ctidTraderAccountId=self._account_id,
                fromTimestamp=from_ts,
                toTimestamp=to_ts,
                period=period_enum,
                symbolId=symbol_id,
                count=max_bars,
            ),
            timeout=30,
        )

        if response is None:
            logger.warning(f"No trendbars response for symbol {symbol_id}")
            return []

        payload = Protobuf.extract(response)
        trendbars = getattr(payload, "trendbar", None)
        if not trendbars:
            logger.warning(f"No trendbar data in response for symbol {symbol_id}")
            return []

        bars = []
        for tb in trendbars:
            # cTrader returns relative prices:
            # low is the base, divided by 100000
            # open/high/close are low + delta, also divided by 100000
            low_raw = tb.low
            open_raw = low_raw + tb.deltaOpen
            high_raw = low_raw + tb.deltaHigh
            close_raw = low_raw + tb.deltaClose

            # divisor is set above (always 100000.0 for cTrader raw encoding)

            bars.append(
                {
                    "timestamp": tb.utcTimestampInMinutes * 60 * 1000,  # to ms
                    "open": round(open_raw / divisor, digits),
                    "high": round(high_raw / divisor, digits),
                    "low": round(low_raw / divisor, digits),
                    "close": round(close_raw / divisor, digits),
                    "volume": tb.volume,
                }
            )

        return bars


def calculate_chunks(
    period: str,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, int]]:
    """Calculate chunk boundaries for a date range.

    Returns list of (from_ms, to_ms) tuples, each within the max bars
    constraint for the given period.
    """
    if period not in PERIOD_SECONDS:
        raise ValueError(f"Invalid period '{period}'")

    period_sec = PERIOD_SECONDS[period]
    max_bars = MAX_BARS.get(period, 5760)
    # Max timespan per chunk in milliseconds
    chunk_ms = max_bars * period_sec * 1000

    chunks = []
    current = start_ms
    while current < end_ms:
        chunk_end = min(current + chunk_ms, end_ms)
        chunks.append((current, chunk_end))
        current = chunk_end

    return chunks
