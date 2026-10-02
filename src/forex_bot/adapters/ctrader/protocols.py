"""Interface contracts (Protocol/ABC) for the cTrader integration layer.

These protocols define the narrow boundary between the domain layer
(strategies, signal engine, forward test engine) and the cTrader
infrastructure layer (session, market data feed, order gateway).

The concrete implementations will satisfy these protocols structurally
(duck typing via ``Protocol``), enabling easy testing with mocks and
clean dependency inversion.

Reference: BQ-1043 Phase 0.5 — Interface Contracts (Amendment A2)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# ── Enums ────────────────────────────────────────────────────────────────────


class OrderStatus(Enum):
    """Lifecycle status for an order."""

    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TradeSide(Enum):
    """Direction of a trade."""

    BUY = "buy"
    SELL = "sell"


class PositionStatus(Enum):
    """Lifecycle status for a position."""

    OPEN = "open"
    CLOSED = "closed"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"


class SessionState(Enum):
    """Connection state machine values."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    SUBSCRIBED = "subscribed"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tick:
    """A single bid/ask update for a symbol."""

    symbol: str
    bid: float
    ask: float
    timestamp: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Bar:
    """An OHLCV bar for a symbol/timeframe."""

    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime
    is_closed: bool = False


@dataclass(frozen=True)
class OrderResult:
    """Result of an order operation.

    ``status`` is explicitly FILLED, REJECTED, or TIMEOUT — never
    PENDING masquerading as success.
    """

    status: OrderStatus
    order_id: str | None = None
    filled_price: float | None = None
    filled_volume: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    execution_time_ms: int | None = None


@dataclass(frozen=True)
class Position:
    """A trading position (open or historical)."""

    position_id: str
    symbol: str
    direction: str
    volume: float
    entry_price: float
    current_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    pnl: float = 0.0
    status: PositionStatus = PositionStatus.OPEN


# ── Protocols ────────────────────────────────────────────────────────────────


@runtime_checkable
class MarketFeedProtocol(Protocol):
    """Market data feed interface.

    Provides tick streaming, bar access, symbol resolution, and
    subscription management.  The forward test engine depends on
    this interface rather than a concrete implementation.
    """

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, auto_subscribe: list[str] | None = None) -> bool:
        """Start the feed and optionally auto-subscribe to symbols."""
        ...

    def stop(self) -> None:
        """Stop the feed and disconnect."""
        ...

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True when the feed loop is active."""
        ...

    @property
    def is_connected(self) -> bool:
        """True when the underlying transport is connected + authenticated."""
        ...

    @property
    def ticks_received(self) -> int:
        """Total ticks received since start."""
        ...

    @property
    def bars_built(self) -> int:
        """Total bars built since start."""
        ...

    # ── Subscriptions ──────────────────────────────────────────────────────

    def subscribe(self, symbol_name: str) -> bool:
        """Subscribe to spot events for *symbol_name*."""
        ...

    def unsubscribe(self, symbol_name: str) -> bool:
        """Unsubscribe from spot events for *symbol_name*."""
        ...

    # ── Market Data Access ─────────────────────────────────────────────────

    def get_tick(self, symbol_name: str) -> Tick | None:
        """Get the latest tick for *symbol_name*, or ``None``."""
        ...

    def get_all_ticks(self) -> dict[str, Tick]:
        """Return a snapshot of all current ticks keyed by normalized symbol."""
        ...

    def get_spread(self, symbol_name: str) -> float | None:
        """Current spread for *symbol_name*, or ``None`` if no tick."""
        ...

    # ── Symbol Resolution ──────────────────────────────────────────────────

    def resolve_symbol_id(self, name: str) -> int:
        """Resolve a symbol name to its cTrader numeric ID.

        Raises ``ValueError`` if the symbol is unknown.
        """
        ...

    # ── Historical Data ────────────────────────────────────────────────────

    def fetch_trendbars(self, symbol: str, period_minutes: int, count: int) -> list[Bar]:
        """Fetch historical bars (trendbars) from cTrader."""
        ...

    # ── Callbacks ──────────────────────────────────────────────────────────

    def on_tick(self, callback: Callable[[Tick], None]) -> None:
        """Register a callback invoked on every incoming tick."""
        ...

    def register_callback(self, event_name: str, fn: Callable) -> None:
        """Register a callback for a named event.

        Events: ``on_order_filled``, ``on_order_rejected``,
        ``on_order_cancelled``, ``on_reconnected``.
        """
        ...

    # ── Health ─────────────────────────────────────────────────────────────

    def get_health(self) -> dict[str, Any]:
        """Return a health/status dictionary for diagnostics."""
        ...


@runtime_checkable
class OrderGatewayProtocol(Protocol):
    """Order execution interface.

    Sends orders to cTrader and correlates execution events.
    The domain layer (forward test engine, paper trader) calls
    these methods to place and manage trades.
    """

    def send_market_order(
        self,
        symbol_id: int,
        side: TradeSide,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "",
    ) -> OrderResult:
        """Send a market order. Returns when filled, rejected, or timed out."""
        ...

    def cancel_order(self, order_id: int) -> bool:
        """Cancel a pending order. Returns ``True`` on success."""
        ...

    def amend_position(
        self,
        position_id: int,
        sl: float | None = None,
        tp: float | None = None,
    ) -> bool:
        """Amend stop-loss / take-profit on an open position."""
        ...

    def close_position(self, position_id: int, volume: float) -> bool:
        """Partially or fully close an open position."""
        ...

    def reconcile(self) -> list[Position]:
        """Fetch current open positions from cTrader for reconciliation."""
        ...


@runtime_checkable
class SessionProtocol(Protocol):
    """Connection session interface.

    Manages the TCP connection lifecycle: connect → authenticate →
    subscribe → disconnect, with clean state transitions.
    """

    @property
    def is_operational(self) -> bool:
        """``True`` when connected, authenticated, and subscribed."""
        ...

    @property
    def state(self) -> SessionState:
        """Current connection state."""
        ...

    def connect(self) -> bool:
        """Establish TCP + app auth + account auth.

        Returns ``True`` on success, ``False`` on failure.
        Fails fast with a logged reason on any error.
        """
        ...

    def disconnect(self) -> None:
        """Cleanly disconnect from cTrader."""
        ...

    def send(self, message: Any, client_msg_id: str, timeout: float = 30.0) -> Any:
        """Send a protobuf message and wait for the response.

        Returns the response payload or raises ``TimeoutError``.
        """
        ...
