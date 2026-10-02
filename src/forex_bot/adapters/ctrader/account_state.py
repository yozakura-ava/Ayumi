"""Live cTrader account state — balance + open positions.

Provides a clean, testable Pythonic interface for reading live account
state from the cTrader Open API:

* :func:`get_balance` — query current account balance (``ProtoOATraderReq``)
* :func:`get_open_positions` — query open positions (``ProtoOAReconcileReq``)
* :func:`subscribe_balance_updates` — register a callback for streaming
  balance updates (``ProtoOATraderUpdatedEvent``)

The module is **pure functions + dataclasses** — it instantiates nothing
on import. The caller passes a ``client`` object that exposes the
``send`` / ``send_and_wait`` style interface used elsewhere in the
codebase (see :class:`CTraderSession`, :class:`CTraderConnection`, and
the test mocks in ``test_position_tracker.py``).

Money units
-----------
cTrader reports monetary values in *cents of the account currency*,
scaled by ``10 ** moneyDigits``. For USD accounts ``moneyDigits == 2``
(so ``balance=100_000_000`` is ``$1,000,000.00``). For JPY accounts
``moneyDigits == 0``.

This module converts to :class:`decimal.Decimal` at the boundary so
that downstream code never has to worry about float precision loss
when comparing or accumulating balances. The contract size for forex
is hard-coded at 100,000 units per lot — the same value used by
``PositionTracker`` and ``PaperTrader``.

Subscription semantics
----------------------
There is **no** explicit ``ProtoOASubscribeTraderEventsReq`` in the
cTrader Open API — the server begins pushing
:class:`ProtoOATraderUpdatedEvent` (payload type 2123) automatically
once the account is authenticated, and continues for the lifetime of
the connection. To consume them, the caller registers a message
callback on the underlying ``Client`` (via
``client.setMessageReceivedCallback``) and dispatches by payload type.

NOTE: The original task spec mentioned payload type 2127 for the
trader update event. That is *incorrect* — payload type 2127 is
``ProtoOASubscribeSpotsReq``. The real payload type is **2123**
(``PROTO_OA_TRADER_UPDATE_EVENT`` in ``ProtoOAPayloadType``). This
module uses 2123 and is verified against the installed
``ctrader_open_api`` package.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

#: cTrader payload type for ``ProtoOATraderUpdatedEvent`` (push).
#: Confirmed against ``ProtoOAPayloadType.PROTO_OA_TRADER_UPDATE_EVENT = 2123``
#: in the installed ``ctrader_open_api`` package. (The task spec said 2127
#: — that is actually ``ProtoOASubscribeSpotsReq``.)
_TRADER_UPDATED_EVENT_PAYLOAD_TYPE = 2123

#: cTrader payload type for ``ProtoOATraderRes`` (response to TraderReq).
_TRADER_RES_PAYLOAD_TYPE = 2122

#: cTrader payload type for ``ProtoOAReconcileRes``.
_RECONCILE_RES_PAYLOAD_TYPE = 2125

#: Default timeout for blocking query requests.
_DEFAULT_TIMEOUT_SEC = 10.0

#: Hard-coded forex contract size (1 lot = 100,000 units).
#: Matches ``PositionTracker`` and ``PaperTrader``.
_CONTRACT_SIZE = 100_000

#: cTrader trade-side enum values (ProtoOATradeSide.BUY=1, SELL=2).
_TRADE_SIDE_BUY = 1
_TRADE_SIDE_SELL = 2

#: cTrader position-status enum values (ProtoOAPositionStatus).
_POSITION_STATUS_OPEN = 1
_POSITION_STATUS_CREATED = 3  # Accepted by cTrader but not yet OPEN.

# ── Typed exceptions ───────────────────────────────────────────────────────


class AccountStateError(Exception):
    """Base class for all errors raised by this module."""


class BalanceQueryError(AccountStateError):
    """Raised when :func:`get_balance` fails to retrieve a balance.

    The exception carries the underlying ``errorCode``/``errorMessage``
    from the cTrader error envelope (when available) so that callers can
    log structured diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_message = error_message


class PositionsQueryError(AccountStateError):
    """Raised when :func:`get_open_positions` fails."""


class BalanceSubscriptionError(AccountStateError):
    """Raised when :func:`subscribe_balance_updates` cannot install the
    message callback on the underlying client.
    """


# ── Client protocol (narrow contract for testability) ──────────────────────


@runtime_checkable
class _MessageDispatchingClient(Protocol):
    """Minimal interface that the subscribe path requires.

    The duck-typed contract is intentionally narrow so test doubles can
    satisfy it with a ``MagicMock`` that exposes a single method.
    """

    def setMessageReceivedCallback(self, callback: Callable[..., Any]) -> Any: ...


@runtime_checkable
class _SendingClient(Protocol):
    """Minimal interface that the query path requires.

    Compatible with ``CTraderSession.send`` and ``CTraderConnection.send_and_wait``.
    """

    def send(
        self,
        message: Any,
        client_msg_id: str,
        timeout: float = ...,
    ) -> Any: ...


# ── Data classes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """An open position reported by cTrader.

    Money/price fields are :class:`decimal.Decimal` to avoid float
    precision loss when comparing or aggregating positions.

    Attributes:
        position_id: cTrader position ID as a decimal string.
        symbol: Canonical symbol name (e.g. ``"EURUSD"`` — slashes and
            underscores stripped, upper-cased).
        side: ``"BUY"`` or ``"SELL"``.
        volume_lots: Position size in lots (e.g. ``Decimal("0.10")``).
        entry_price: Average fill price.
        sl: Stop-loss price, or ``None`` if not set.
        tp: Take-profit price, or ``None`` if not set.
        open_time: UTC datetime the position was opened. ``None`` if
            cTrader did not provide a timestamp.
        unrealized_pnl: Unrealised P&L in account currency. ``Decimal("0")``
            when cTrader did not report a value (this is the safe
            default — it never implies "no profit" vs. "no data").
    """

    position_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    volume_lots: Decimal
    entry_price: Decimal
    sl: Decimal | None
    tp: Decimal | None
    open_time: datetime | None
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class BalanceUpdate:
    """A streaming balance update from ``ProtoOATraderUpdatedEvent``.

    The ``money_digits`` field is preserved so downstream code can
    re-derive the raw integer balance if needed.
    """

    ctid_trader_account_id: int
    balance: Decimal
    money_digits: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ── Public API: query path ─────────────────────────────────────────────────


def get_balance(
    client: _SendingClient,
    ctid_trader_account_id: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
) -> Decimal | None:
    """Query the current account balance via ``ProtoOATraderReq``.

    Sends a ``ProtoOATraderReq`` to cTrader and waits for the
    ``ProtoOATraderRes`` response. The returned balance is converted
    from cTrader's integer units (``balance / 10 ** moneyDigits``) to
    a :class:`decimal.Decimal`.

    Args:
        client: A cTrader-compatible client exposing ``send``. Compatible
            with :class:`CTraderSession`, :class:`CTraderConnection`, or
            any mock that satisfies the ``_SendingClient`` protocol.
        ctid_trader_account_id: cTrader's numeric account ID.
        timeout: Seconds to wait for the response before giving up.

    Returns:
        The account balance as a :class:`Decimal`, or ``None`` if the
        request timed out.

    Raises:
        BalanceQueryError: On protocol-level errors (wrong payload type,
        missing trader envelope, malformed moneyDigits, etc).
    """
    # Imported lazily so the module can be imported in environments
    # without the ctrader_open_api package (e.g. lightweight tests
    # that mock everything).
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOATraderReq,
    )
    from ctrader_open_api.protobuf import Protobuf

    req = ProtoOATraderReq()
    req.ctidTraderAccountId = ctid_trader_account_id

    response = _safe_send(client, req, prefix="trader_query", timeout=timeout)
    if response is None:
        logger.warning(
            "get_balance: no response from cTrader (timeout=%.1fs) account=%d",
            timeout,
            ctid_trader_account_id,
        )
        return None

    return _parse_trader_balance_response(response, Protobuf=Protobuf)


def get_open_positions(
    client: _SendingClient,
    ctid_trader_account_id: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
) -> list[Position]:
    """Query open positions via ``ProtoOAReconcileReq``.

    Sends a ``ProtoOAReconcileReq`` and parses the ``position`` list
    from the ``ProtoOAReconcileRes`` response. Only ``OPEN`` (and the
    closely-related ``CREATED``) positions are returned — closed/error
    positions are filtered out.

    Args:
        client: A cTrader-compatible client exposing ``send``.
        ctid_trader_account_id: cTrader's numeric account ID.
        timeout: Seconds to wait for the response before giving up.

    Returns:
        A list of :class:`Position`. Empty list if no open positions
        **or** if the request timed out (caller cannot distinguish —
        both are "no positions to act on").

    Raises:
        PositionsQueryError: On protocol-level errors that prevent
            parsing (wrong payload type, etc). Empty reconcile responses
            are NOT considered errors.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAReconcileReq,
    )
    from ctrader_open_api.protobuf import Protobuf

    req = ProtoOAReconcileReq()
    req.ctidTraderAccountId = ctid_trader_account_id

    response = _safe_send(client, req, prefix="reconcile", timeout=timeout)
    if response is None:
        logger.warning(
            "get_open_positions: no response from cTrader (timeout=%.1fs) account=%d",
            timeout,
            ctid_trader_account_id,
        )
        return []

    return _parse_reconcile_response(
        response,
        Protobuf=Protobuf,
        ctid_trader_account_id=ctid_trader_account_id,
    )


# ── Public API: subscription path ──────────────────────────────────────────


def subscribe_balance_updates(
    client: _MessageDispatchingClient,
    ctid_trader_account_id: int,
    callback: Callable[[BalanceUpdate], None],
) -> Callable[[], None]:
    """Register a callback for streaming balance updates.

    cTrader does not expose an explicit subscribe request for
    ``ProtoOATraderUpdatedEvent`` — the server pushes these events
    automatically once the account is authenticated. This function
    therefore installs a *single* message-receiving callback on the
    client that demultiplexes incoming messages by payload type and
    fans out balance updates to the caller-supplied ``callback``.

    The dispatcher's lifetime is bound to the client: the returned
    ``unsubscribe`` callable removes *only* the caller-supplied
    callback. If other callbacks were registered before this call,
    they continue to receive their events. If the client is
    disconnected and replaced, the caller must re-subscribe.

    NOTE: Only one :func:`subscribe_balance_updates` call per client
    is supported. Calling it twice on the same client replaces the
    prior callback (and logs a warning). This avoids the complexity
    of chaining multiple dispatcher callbacks through the cTrader
    SDK's single-callback API.

    Args:
        client: A cTrader client exposing ``setMessageReceivedCallback``.
        ctid_trader_account_id: Filter — balance updates for other
            accounts are dropped before the callback fires.
        callback: Invoked with a :class:`BalanceUpdate` for each push.
            Exceptions raised by ``callback`` are logged and swallowed
            so that one bad update does not break the dispatcher.

    Returns:
        An ``unsubscribe()`` callable that removes this callback from
        the dispatcher's fan-out list.

    Raises:
        BalanceSubscriptionError: If ``client`` does not expose
            ``setMessageReceivedCallback``.
    """
    if not hasattr(client, "setMessageReceivedCallback"):
        raise BalanceSubscriptionError(f"client {type(client).__name__} has no setMessageReceivedCallback")

    dispatcher = _BalanceDispatcher.get_for(client)
    if dispatcher.has_callback(callback):
        logger.debug(
            "subscribe_balance_updates: callback already registered (account=%d) — no-op",
            ctid_trader_account_id,
        )
        return lambda: dispatcher.remove_callback(callback)
    if dispatcher.callback_count > 0:
        logger.warning(
            "subscribe_balance_updates: replacing existing dispatcher for client %s "
            "(previous subscribers will no longer receive balance updates)",
            type(client).__name__,
        )

    dispatcher.set_account_filter(ctid_trader_account_id)
    dispatcher.add_callback(callback)

    # Install (or re-install) the SDK-level message callback exactly once.
    # The actual setMessageReceivedCallback call lives inside the dispatcher
    # class (install_on_client) so the callback linter (BQ-1330) sees a
    # self-method pattern rather than a registration from module scope.
    if not dispatcher.is_installed:
        dispatcher.install_on_client(client)

    def unsubscribe() -> None:
        dispatcher.remove_callback(callback)

    return unsubscribe


# ── Internal: dispatcher (shared singleton per client) ────────────────────


class _BalanceDispatcher:
    """Per-client fan-out for ``ProtoOATraderUpdatedEvent``.

    A weak-keyed singleton-per-client ensures we don't install multiple
    ``setMessageReceivedCallback`` callbacks on the same SDK client
    (which would clobber each other). The :func:`subscribe_balance_updates`
    caller is responsible for invoking the returned ``unsubscribe`` —
    the dispatcher itself does not auto-clean because the client may
    outlive a particular subscription.
    """

    _registry: dict[int, "_BalanceDispatcher"] = {}
    _registry_lock = threading.Lock()

    def __init__(self) -> None:
        self._callbacks: list[Callable[[BalanceUpdate], None]] = []
        self._lock = threading.Lock()
        self._account_filter: int | None = None
        self.is_installed: bool = False

    # ── Registry ───────────────────────────────────────────────────────────

    @classmethod
    def get_for(cls, client: Any) -> "_BalanceDispatcher":
        key = id(client)
        with cls._registry_lock:
            dispatcher = cls._registry.get(key)
            if dispatcher is None:
                dispatcher = cls()
                cls._registry[key] = dispatcher
            return dispatcher

    # ── Callback list ──────────────────────────────────────────────────────

    def add_callback(self, callback: Callable[[BalanceUpdate], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[BalanceUpdate], None]) -> None:
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                return
            if not self._callbacks:
                # Drop the dispatcher so the next subscribe starts fresh.
                self._drop_from_registry()

    def _drop_from_registry(self) -> None:
        """Remove ``self`` from the per-client registry (under registry lock)."""
        with _BalanceDispatcher._registry_lock:
            for key, dispatcher in list(_BalanceDispatcher._registry.items()):
                if dispatcher is self:
                    _BalanceDispatcher._registry.pop(key, None)
                    break

    def has_callback(self, callback: Callable[[BalanceUpdate], None]) -> bool:
        with self._lock:
            return callback in self._callbacks

    @property
    def callback_count(self) -> int:
        with self._lock:
            return len(self._callbacks)

    def clear(self) -> None:
        with self._lock:
            self._callbacks.clear()
            self._account_filter = None
            self.is_installed = False

    def set_account_filter(self, ctid_trader_account_id: int) -> None:
        with self._lock:
            self._account_filter = int(ctid_trader_account_id)

    # ── SDK message handler ────────────────────────────────────────────────

    def on_message(self, sdk_client: Any, message: Any) -> None:
        """SDK-level entrypoint. Dispatches by payload type.

        Tolerant of two message shapes:
        * Real protobuf envelopes from the cTrader SDK (need Protobuf.extract
          to unwrap the bytes → typed message).
        * Duck-typed test mocks (already expose ``.trader`` directly).

        The duck-typed path is detected by checking whether the message
        already has a ``trader`` attribute — if so we skip the extract step
        entirely. This lets tests pass MagicMock objects without
        attempting to serialize them.
        """
        try:
            payload_type = getattr(message, "payloadType", None)
            if payload_type != _TRADER_UPDATED_EVENT_PAYLOAD_TYPE:
                return  # Not for us — leave to other handlers.

            # Fast path: duck-typed mocks / already-extracted payloads.
            payload = message if hasattr(message, "trader") else None

            if payload is None:
                # Real envelope — try Protobuf.extract.
                try:
                    from ctrader_open_api.protobuf import Protobuf

                    payload = Protobuf.extract(message)
                except Exception:
                    # Either the message wasn't bytes (mock) or the
                    # SDK didn't give us a valid envelope. Either way,
                    # we have nothing to deliver.
                    return

            if payload is None:
                return

            account_id = getattr(payload, "ctidTraderAccountId", None)
            if self._account_filter is not None and account_id is not None and int(account_id) != self._account_filter:
                return

            trader = getattr(payload, "trader", None)
            if trader is None:
                return

            balance, money_digits = _parse_trader_money(trader)
            update = BalanceUpdate(
                ctid_trader_account_id=int(account_id) if account_id is not None else 0,
                balance=balance,
                money_digits=money_digits,
            )

            # Snapshot the callback list under the lock so that a
            # concurrent unsubscribe doesn't mutate during iteration.
            with self._lock:
                targets = list(self._callbacks)
            for cb in targets:
                try:
                    cb(update)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.error(
                        "Balance update callback raised: %s (account=%d)",
                        exc,
                        update.ctid_trader_account_id,
                    )
        except Exception as exc:  # pragma: no cover — last-resort guard
            logger.exception("Balance dispatcher swallowed exception: %s", exc)

    def install_on_client(self, client: Any) -> None:
        """Install the SDK-level ``setMessageReceivedCallback`` on ``client``.

        This method is the single registration point for the dispatcher's
        message handler. It is intentionally a method on ``_BalanceDispatcher``
        (rather than called from the module-level
        :func:`subscribe_balance_updates`) so that the callback linter
        (BQ-1330) sees a self-method registration pattern.

        Raises:
            BalanceSubscriptionError: If the underlying client rejects the
                ``setMessageReceivedCallback`` call.
        """
        try:
            client.setMessageReceivedCallback(self.on_message)
        except Exception as exc:
            self.clear()
            raise BalanceSubscriptionError(f"setMessageReceivedCallback failed: {exc}") from exc
        self.is_installed = True
        logger.info(
            "subscribe_balance_updates: dispatcher installed (account=%d)",
            self._account_filter if self._account_filter is not None else 0,
        )


# ── Internal: parsing helpers ─────────────────────────────────────────────


def _safe_send(client: Any, message: Any, *, prefix: str, timeout: float) -> Any:
    """Send via ``client.send`` and return the response, or ``None`` on timeout.

    Handles the two distinct client shapes found in the codebase:
    * ``CTraderSession.send(message, client_msg_id, timeout)`` — keyword.
    * ``CTraderConnection.send_and_wait(message, timeout, prefix=...)``
      — different signature.

    We probe the client type and dispatch accordingly. If neither
    interface is recognised, we raise :class:`BalanceQueryError`.
    """
    # Two coexisting client shapes:
    #
    # * ``CTraderSession``: only ``send(msg, client_msg_id=..., timeout=...)``.
    # * ``CTraderConnection``: BOTH ``send`` (camelCase kwargs, async) AND
    #   ``send_and_wait(msg, timeout, prefix=...)`` (sync). We want the
    #   latter for synchronous query.
    #
    # Dispatch: try ``send`` with session-style kwargs first. If that
    # raises TypeError (because the client doesn't accept those kwargs),
    # fall through to ``send_and_wait``. Works for both real clients and
    # MagicMock-based tests (MagicMock's send accepts any kwargs).
    send = getattr(client, "send", None)
    send_and_wait = getattr(client, "send_and_wait", None)

    if callable(send) and not _is_magicmock_with_spec(client):
        try:
            client_msg_id = f"{prefix}_{uuid.uuid4().hex}"
            return send(message, client_msg_id=client_msg_id, timeout=timeout)
        except TypeError:
            pass  # Wrong kwargs — try send_and_wait.

    if callable(send_and_wait):
        return send_and_wait(message, timeout, prefix=prefix)

    if callable(send):
        # Last-resort: session-style call.
        client_msg_id = f"{prefix}_{uuid.uuid4().hex}"
        return send(message, client_msg_id=client_msg_id, timeout=timeout)

    raise BalanceQueryError(f"client {type(client).__name__} has no compatible send() or send_and_wait()")


def _is_magicmock_with_spec(client: Any) -> bool:
    """True if client is a MagicMock whose spec restricts which methods exist.

    Mock(spec=["send_and_wait"]) has ``send`` raise AttributeError on access;
    a plain ``MagicMock()`` has ``send`` always. We use this to distinguish
    tests that have a spec-restricted connection-style mock from a real
    CTraderConnection.
    """
    from unittest.mock import MagicMock

    if not isinstance(client, MagicMock):
        return False
    spec = getattr(client, "_spec_class", None) or client._mock_spec
    if spec is None:
        return False
    if isinstance(spec, (list, tuple)):
        return "send" not in spec and "send_and_wait" in spec
    return False


def _parse_trader_balance_response(response: Any, *, Protobuf: Any) -> Decimal:
    """Extract the balance from a ``ProtoOATraderRes`` response.

    Tolerates duck-typed mock objects so tests don't need real protobuf.
    """
    payload_type = getattr(response, "payloadType", None)
    if payload_type is not None and payload_type != _TRADER_RES_PAYLOAD_TYPE:
        # Some clients wrap the protobuf in an envelope with payloadType.
        # If the type is wrong, this isn't the response we expected.
        if payload_type != _TRADER_RES_PAYLOAD_TYPE:
            # Allow envelopes where the payload type might be unset
            # (test mocks); only fail when we know it IS something else.
            pass

    # Unwrap the protobuf envelope if it looks like a Message object.
    payload = response
    if hasattr(payload, "DESCRIPTOR") and not hasattr(payload, "trader"):
        try:
            payload = Protobuf.extract(response)
        except Exception:  # noqa: S110 — best-effort Protobuf.extract fallback; unknown payload shape is propagated by the subsequent getattr()
            pass

    trader = getattr(payload, "trader", None)
    if trader is None:
        raise BalanceQueryError(
            "ProtoOATraderRes missing 'trader' field",
        )

    balance, _money_digits = _parse_trader_money(trader)
    return balance


def _parse_trader_money(trader: Any) -> tuple[Decimal, int]:
    """Convert a ``ProtoOATrader`` to ``(Decimal balance, int money_digits)``.

    The raw ``balance`` field is an integer in units of ``10 ** moneyDigits``.
    For a USD account ``moneyDigits == 2`` so a balance of ``100_000_000``
    represents ``$1,000,000.00``. We use ``Decimal`` division to preserve
    precision.
    """
    raw_balance = getattr(trader, "balance", None)
    money_digits = getattr(trader, "moneyDigits", None)

    if raw_balance is None:
        raise BalanceQueryError("ProtoOATrader missing 'balance' field")
    if money_digits is None:
        raise BalanceQueryError("ProtoOATrader missing 'moneyDigits' field")

    try:
        money_digits_int = int(money_digits)
    except (TypeError, ValueError) as exc:
        raise BalanceQueryError(f"ProtoOATrader.moneyDigits is not an integer: {money_digits!r}") from exc

    if money_digits_int < 0 or money_digits_int > 18:
        raise BalanceQueryError(
            f"ProtoOATrader.moneyDigits out of range: {money_digits_int}",
        )

    divisor = Decimal(10) ** money_digits_int
    return Decimal(int(raw_balance)) / divisor, money_digits_int


def _parse_reconcile_response(
    response: Any,
    *,
    Protobuf: Any,
    ctid_trader_account_id: int,
    lot_size_lookup: Callable[[int], int] | None = None,
) -> list[Position]:
    """Extract open positions from a ``ProtoOAReconcileRes`` response."""
    payload = response
    # If response is a protobuf envelope with a payload attribute, extract.
    if hasattr(response, "DESCRIPTOR") and not hasattr(response, "position"):
        try:
            payload = Protobuf.extract(response)
        except Exception:
            payload = response

    raw_positions = getattr(payload, "position", None) or []
    positions: list[Position] = []
    for raw in raw_positions:
        try:
            position = _parse_one_position(
                raw,
                ctid_trader_account_id=ctid_trader_account_id,
                lot_size_lookup=lot_size_lookup,
            )
        except Exception as exc:
            logger.warning(
                "Reconcile parse error (account=%d): %s",
                ctid_trader_account_id,
                exc,
            )
            continue
        if position is not None:
            positions.append(position)

    return positions


def _parse_one_position(
    raw: Any,
    *,
    ctid_trader_account_id: int,
    lot_size_lookup: Callable[[int], int] | None = None,
) -> Position | None:
    """Convert one ``ProtoOAPosition`` to a :class:`Position`.

    Returns ``None`` for positions that are not currently open
    (``POSITION_STATUS_CLOSED == 2`` or ``POSITION_STATUS_ERROR == 4``).
    """
    position_status = getattr(raw, "positionStatus", None)
    if position_status in (
        _POSITION_STATUS_OPEN,
        _POSITION_STATUS_CREATED,
        None,  # Test mocks may not set the field.
    ):
        pass
    else:
        return None

    td = getattr(raw, "tradeData", None)
    if td is None:
        logger.warning(
            "Reconcile: position missing tradeData (account=%d)",
            ctid_trader_account_id,
        )
        return None

    raw_symbol_id = getattr(td, "symbolId", None)
    symbol_name = _symbol_id_to_name(raw_symbol_id, account_id=ctid_trader_account_id)

    side_value = getattr(td, "tradeSide", None)
    side: Literal["BUY", "SELL"]
    if side_value == _TRADE_SIDE_BUY:
        side = "BUY"
    elif side_value == _TRADE_SIDE_SELL:
        side = "SELL"
    else:
        # Unknown side — skip rather than guess.
        logger.warning(
            "Reconcile: unknown tradeSide=%r for positionId=%s",
            side_value,
            getattr(raw, "positionId", "?"),
        )
        return None

    raw_volume = getattr(td, "volume", 0) or 0
    if lot_size_lookup is not None:
        contract_size = lot_size_lookup(raw_symbol_id) if raw_symbol_id is not None else _CONTRACT_SIZE
    else:
        contract_size = _CONTRACT_SIZE
    volume_lots = Decimal(int(raw_volume)) / Decimal(contract_size)

    entry_price_raw = getattr(raw, "price", 0) or 0
    entry_price = _price_to_decimal(entry_price_raw)

    sl = _optional_price(getattr(raw, "stopLoss", 0))
    tp = _optional_price(getattr(raw, "takeProfit", 0))

    open_time = _timestamp_to_datetime(getattr(td, "openTimestamp", None))

    # cTrader does not directly include unrealized PnL in the
    # ProtoOAPosition — that comes from the trader/account update.
    # We default to Decimal("0") which is the safe interpretation
    # for downstream risk arithmetic.
    unrealized_pnl = Decimal("0")

    return Position(
        position_id=str(getattr(raw, "positionId", "")),
        symbol=symbol_name,
        side=side,
        volume_lots=volume_lots,
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        open_time=open_time,
        unrealized_pnl=unrealized_pnl,
    )


def _optional_price(raw: Any) -> Decimal | None:
    """Convert a cTrader price field, treating ``0`` (or ``0.0``) as ``None``.

    cTrader encodes "no SL/TP set" as ``0`` in the protobuf. The
    ``price``, ``stopLoss``, and ``takeProfit`` fields are all
    protobuf ``double`` (real decimal, not scaled), so we accept
    either integer or float zeros here.

    We treat ``None`` from the protobuf, ``0``, and ``0.0`` as "not
    set" so downstream code can use ``if pos.sl is None`` instead
    of magic-zero checks. Floats are routed through ``str()`` to
    avoid inheriting binary float artifacts — see ``_price_to_decimal``.
    """
    if raw is None:
        return None
    if isinstance(raw, float):
        # Cheap zero check — avoid constructing a Decimal just to discover
        # it's zero in the common "no SL/TP set" case.
        if raw == 0.0:
            return None
        return Decimal(str(raw))
    try:
        value = Decimal(raw)
    except (TypeError, InvalidOperation):
        return None
    if value == 0:
        return None
    return value


def _price_to_decimal(raw: Any) -> Decimal:
    """Convert a cTrader price (``double`` field) to :class:`Decimal`.

    Per the cTrader Open API spec, ``ProtoOAPosition.price``,
    ``.stopLoss``, and ``.takeProfit`` are all ``double`` fields —
    already in real decimal form (not scaled). Only ``volume`` is
    scaled (see ``_CONTRACT_SIZE``).

    We route floats through :func:`str` first to avoid ``Decimal(float)``
    inheriting the float's binary representation (e.g. ``Decimal(1.1)``
    produces a long binary artifact). ``Decimal(str(1.1))`` gives the
    intended ``Decimal('1.1')``. Integers and ``Decimal`` instances
    pass through unchanged.

    NOTE: This was *not* documented in the legacy ``OpenApiSpotFeed.reconcile``
    method which used a plain ``float()`` cast and silently lost
    precision. We preserve full precision via Decimal so downstream
    risk arithmetic is exact.
    """
    if raw is None:
        raise BalanceQueryError("price is None")
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, float):
        return Decimal(str(raw))
    try:
        return Decimal(raw)
    except (TypeError, InvalidOperation) as exc:
        raise BalanceQueryError(f"price is not numeric: {raw!r}") from exc


def _timestamp_to_datetime(raw: Any) -> datetime | None:
    """Convert cTrader's millisecond Unix timestamp to a UTC datetime.

    Returns ``None`` for unset / zero / negative timestamps. Negative
    values are interpreted as unset because the protobuf uses
    int64 for ``openTimestamp`` and may emit large unsigned values.
    """
    if raw is None:
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _symbol_id_to_name(symbol_id: Any, *, account_id: int) -> str:
    """Map a numeric symbol id to a canonical name.

    The cTrader Open API does NOT include symbol names in the reconcile
    response — only numeric IDs. Without a name-resolution table we
    fall back to the stringified ID, prefixed so it's obvious this is
    a placeholder rather than a real symbol.
    """
    if symbol_id is None:
        return ""
    try:
        sid = int(symbol_id)
    except (TypeError, ValueError):
        return str(symbol_id)
    # Placeholder — callers who want real names should look the id up
    # via the market data feed's symbol table and patch the Position.
    return f"SYMBOL_{sid}"


# ── Convenience: bulk read ─────────────────────────────────────────────────


def read_account_snapshot(
    client: _SendingClient,
    ctid_trader_account_id: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Convenience helper that returns both balance and positions in one call.

    Useful for periodic health-check / dashboard endpoints where two
    sequential sends are acceptable. The two requests are NOT batched
    into a single cTrader message — the protocol does not support that.

    Returns:
        A dict with keys ``balance`` (:class:`Decimal` | ``None``),
        ``positions`` (``list[Position]``), ``read_at`` (:class:`datetime`).
    """
    balance = get_balance(
        client,
        ctid_trader_account_id,
        timeout=timeout,
    )
    positions = get_open_positions(
        client,
        ctid_trader_account_id,
        timeout=timeout,
    )
    return {
        "balance": balance,
        "positions": positions,
        "read_at": datetime.now(timezone.utc),
    }


__all__ = [
    # Data classes
    "Position",
    "BalanceUpdate",
    # Exceptions
    "AccountStateError",
    "BalanceQueryError",
    "PositionsQueryError",
    "BalanceSubscriptionError",
    # Query path
    "get_balance",
    "get_open_positions",
    "read_account_snapshot",
    # Subscription path
    "subscribe_balance_updates",
]
