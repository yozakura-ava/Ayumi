import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Optional

from .models import (
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    TradeDirection,
    get_symbol_info,
)

if TYPE_CHECKING:
    from .api_client import cTraderAPIClient


logger = logging.getLogger(__name__)


@dataclass
class PositionSizeConfig:
    risk_per_trade_pct: float = 0.005
    max_lot_size: float = 1.0
    min_lot_size: float = 0.01
    default_lot_size: float = 0.1


@dataclass
class PendingOrderTimeoutConfig:
    timeout_seconds: float = 60.0
    check_interval_seconds: float = 30.0


@dataclass
class SlippageModel:
    base_pips: float = 0.1
    random_pips: float = 0.2
    pip_value: float = 0.0001

    def apply(self, price: float, direction: TradeDirection) -> float:
        slippage_pips = self.base_pips + random.random() * self.random_pips  # noqa: S311 — non-cryptographic slippage simulation jitter on order fills
        slippage = slippage_pips * self.pip_value
        if direction == TradeDirection.LONG:
            return price + slippage
        return price - slippage

    def apply_with_spread(self, price: float, direction: TradeDirection, spread: float = 0.0) -> float:
        if spread > 0:
            if direction == TradeDirection.LONG:
                price = price + spread / 2
            else:
                price = price - spread / 2
        return self.apply(price, direction)


@dataclass
class OrderExecutionResult:
    success: bool
    order: Order | None = None
    position: Position | None = None
    error_message: str = ""
    rejection_reason: str = ""
    slippage_applied: float = 0.0


class OrderManager:
    def __init__(
        self,
        position_config: PositionSizeConfig | None = None,
        api_client: Optional["cTraderAPIClient"] = None,
        pending_timeout_config: PendingOrderTimeoutConfig | None = None,
    ):
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._position_config = position_config or PositionSizeConfig()
        self._api_client = api_client
        self._slippage_model = SlippageModel()
        self._lock = Lock()
        self._locally_filled_order_ids: set = set()
        self._pending_timeout_config = pending_timeout_config or PendingOrderTimeoutConfig()
        self._pending_order_timestamps: dict[str, datetime] = {}
        self._callbacks: dict[str, list[Callable]] = {
            "on_order_placed": [],
            "on_order_filled": [],
            "on_order_cancelled": [],
            "on_order_rejected": [],
            "on_position_opened": [],
            "on_position_closed": [],
            "on_order_new": [],
            "on_order_partial_fill": [],
            "on_order_timeout": [],
        }
        if self._api_client and not self._api_client.is_paper_mode:
            self._wire_live_callbacks()

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss: float,
        symbol: str = "EURUSD",
    ) -> float:
        from .models import get_symbol_info

        risk_amount = account_balance * self._position_config.risk_per_trade_pct
        sl_distance = abs(entry_price - stop_loss)

        if sl_distance == 0:
            logger.warning("Stop loss distance is zero, using default lot size")
            return self._position_config.default_lot_size

        sym_info = get_symbol_info(symbol)
        sl_pips = sl_distance / sym_info.pip_size

        if sl_pips == 0:
            return self._position_config.default_lot_size

        lot_size = risk_amount / (sl_pips * sym_info.pip_value_per_lot)

        lot_size = max(
            self._position_config.min_lot_size,
            min(lot_size, self._position_config.max_lot_size),
        )

        return round(lot_size, 2)

    def execute_market_order(
        self,
        symbol: str,
        direction: TradeDirection,
        volume: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        comment: str = "",
    ) -> OrderExecutionResult:
        order = Order(
            order_id=f"ORD_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            symbol=symbol,
            direction=direction,
            order_type=OrderType.MARKET,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status=OrderStatus.PENDING,
            comment=comment,
        )

        with self._lock:
            self._orders[order.order_id] = order

        self._trigger_callback("on_order_placed", order)

        order.status = OrderStatus.FILLED
        order.filled_at = datetime.utcnow()
        order.filled_price = 0

        position = self._create_position_from_order(order)
        if position:
            with self._lock:
                self._positions[position.position_id] = position
            self._trigger_callback("on_position_opened", position)

        self._trigger_callback("on_order_filled", order)

        return OrderExecutionResult(
            success=True,
            order=order,
            position=position,
        )

    def execute_paper_order(
        self,
        symbol: str,
        direction: TradeDirection,
        volume: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        take_profit_2: float | None = None,
        take_profit_3: float | None = None,
        comment: str = "",
        spread: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> OrderExecutionResult:
        slippage_model = self._slippage_model

        if bid > 0 and ask > 0:
            if direction == TradeDirection.LONG:
                fill_price = slippage_model.apply(ask, direction)
            else:
                fill_price = slippage_model.apply(bid, direction)
        else:
            fill_price = slippage_model.apply_with_spread(entry_price, direction, spread)

        slippage_amount = abs(fill_price - entry_price)

        logger.info(
            f"[PAPER] Executing order: {direction.value} {volume} {symbol} "
            f"@ signal={entry_price:.5f} fill={fill_price:.5f} "
            f"(spread={spread:.5f} slippage={slippage_amount:.5f}), "
            f"SL: {stop_loss}, TP: {take_profit}"
        )

        order = Order(
            order_id=f"PAPER_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}",
            symbol=symbol,
            direction=direction,
            order_type=OrderType.MARKET,
            volume=volume,
            price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status=OrderStatus.FILLED,
            filled_at=datetime.utcnow(),
            filled_price=fill_price,
            comment=f"[PAPER MODE] {comment}",
        )
        # Stash TP2/TP3 on the Order for _create_position_from_order to pick up.
        # cTrader's wire protocol only accepts a single TP per position, so these
        # are client-side tracking fields for the multi-TP monitoring flow.
        order.take_profit_2 = take_profit_2
        order.take_profit_3 = take_profit_3

        with self._lock:
            self._orders[order.order_id] = order

        position = self._create_position_from_order(order)
        if position:
            with self._lock:
                self._positions[position.position_id] = position

        self._trigger_callback("on_order_filled", order)
        if position:
            self._trigger_callback("on_position_opened", position)

        return OrderExecutionResult(
            success=True,
            order=order,
            position=position,
            slippage_applied=slippage_amount,
        )

    def execute_live_order(
        self,
        symbol: str,
        direction: TradeDirection,
        volume: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        take_profit_2: float | None = None,
        take_profit_3: float | None = None,
        comment: str = "",
    ) -> OrderExecutionResult:
        if not symbol or not symbol.strip():
            return OrderExecutionResult(
                success=False,
                error_message="Symbol is required",
                rejection_reason="validation_error",
            )

        if not volume or volume <= 0:
            return OrderExecutionResult(
                success=False,
                error_message=f"Volume must be positive, got {volume}",
                rejection_reason="validation_error",
            )

        if order_type in (OrderType.LIMIT, OrderType.STOP) and not price:
            return OrderExecutionResult(
                success=False,
                error_message=f"Price is required for {order_type.value} orders",
                rejection_reason="validation_error",
            )

        if not self._api_client or getattr(self._api_client, "is_paper_mode", False):
            return OrderExecutionResult(
                success=False,
                error_message="No live API client connected or paper mode is active",
                rejection_reason="no_live_client",
            )

        if not getattr(self._api_client, "is_connected", False):
            return OrderExecutionResult(
                success=False,
                error_message="cTrader connection not established",
                rejection_reason="not_connected",
            )

        order = self._api_client.send_order(
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            volume=volume,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            comment=comment,
        )

        if order is None:
            return OrderExecutionResult(
                success=False,
                error_message="Failed to send order via cTrader connection",
                rejection_reason="send_failed",
            )

        with self._lock:
            self._orders[order.order_id] = order

        # Stash TP2/TP3 on the Order for the async on_filled callback to pick up
        # when the broker confirms the fill. cTrader's wire protocol only accepts
        # one TP per position; these are client-side tracking fields for the
        # multi-TP monitoring flow (Sprint Task 1.1/1.2, card a7b8e896).
        order.take_profit_2 = take_profit_2
        order.take_profit_3 = take_profit_3

        self._trigger_callback("on_order_placed", order)

        if order.status == OrderStatus.FILLED:
            position = self._create_position_from_order(order)
            if position:
                with self._lock:
                    self._positions[position.position_id] = position
                self._trigger_callback("on_position_opened", position)
            self._trigger_callback("on_order_filled", order)
            with self._lock:
                self._locally_filled_order_ids.add(order.order_id)
            return OrderExecutionResult(
                success=True,
                order=order,
                position=position,
            )

        if order.status == OrderStatus.REJECTED:
            self._trigger_callback("on_order_rejected", order)
            return OrderExecutionResult(
                success=False,
                order=order,
                error_message=order.comment or "Order rejected by broker",
                rejection_reason="broker_rejected",
            )

        # Check for timeout — the spot feed sets reason="timeout_awaiting_event"
        # when the deferred event never fires within the timeout window.
        if order.status == OrderStatus.PENDING and getattr(order, "reason", "") == "timeout_awaiting_event":
            logger.warning("[ORDER_MGR] Live order timed out — not counting as success")
            return OrderExecutionResult(
                success=False,
                order=order,
                error_message="Order timed out awaiting cTrader execution event",
                rejection_reason="timeout",
            )

        with self._lock:
            self._pending_order_timestamps[order.order_id] = datetime.utcnow()

        return OrderExecutionResult(
            success=True,
            order=order,
            error_message="Order sent, awaiting execution report",
        )

    def set_api_client(self, api_client: Optional["cTraderAPIClient"]):
        self._api_client = api_client
        if self._api_client and not getattr(self._api_client, "is_paper_mode", False):
            if not getattr(self._api_client, "is_connected", False):
                logger.warning(
                    "cTraderAPIClient not connected — live callbacks will be "
                    "wired on connect. Call connect() before trading."
                )
            self._wire_live_callbacks()

    def _wire_live_callbacks(self):
        if not self._api_client:
            return

        api = self._api_client

        def on_filled(order, msg, *args):
            if not order:
                return
            with self._lock:
                if order.order_id in self._locally_filled_order_ids:
                    self._locally_filled_order_ids.discard(order.order_id)
                    return
            if order.order_id in self._orders:
                with self._lock:
                    self._orders[order.order_id] = order
                self._pending_order_timestamps.pop(order.order_id, None)
                position = self._create_position_from_order(order)
                if position:
                    with self._lock:
                        self._positions[position.position_id] = position
                    self._trigger_callback("on_position_opened", position)
                self._trigger_callback("on_order_filled", order)

        def on_rejected(order, msg, reject_msg, *args):
            if order and order.order_id in self._orders:
                with self._lock:
                    self._orders[order.order_id] = order
                self._pending_order_timestamps.pop(order.order_id, None)
                self._trigger_callback("on_order_rejected", order)

        def on_cancelled(order, msg, *args):
            if order and order.order_id in self._orders:
                with self._lock:
                    self._orders[order.order_id] = order
                self._pending_order_timestamps.pop(order.order_id, None)
                self._trigger_callback("on_order_cancelled", order)

        api.register_callback("on_order_filled", on_filled)
        api.register_callback("on_order_rejected", on_rejected)
        api.register_callback("on_order_cancelled", on_cancelled)

    def _create_position_from_order(self, order: Order) -> Position | None:
        if order.status != OrderStatus.FILLED:
            return None

        position_id = f"POS_{order.order_id}"
        # TP2/TP3 are stashed on the Order at submission time (see
        # execute_paper_order / execute_live_order). Defaults to None for
        # callers that don't pass them — fully backward compatible.
        tp2 = getattr(order, "take_profit_2", None)
        tp3 = getattr(order, "take_profit_3", None)
        position = Position(
            position_id=position_id,
            symbol=order.symbol,
            direction=order.direction,
            volume=order.volume,
            entry_price=order.filled_price or order.price or 0,
            current_price=order.filled_price or order.price or 0,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            take_profit_2=tp2,
            take_profit_3=tp3,
            opened_at=order.filled_at or datetime.utcnow(),
            comment=order.comment,
        )
        return position

    def update_position_id(self, old_id: str, new_id: str):
        """Update a position's ID (e.g., to set broker position_id after fill)."""
        with self._lock:
            if old_id in self._positions:
                pos = self._positions.pop(old_id)
                pos.position_id = new_id
                self._positions[new_id] = pos

    def update_position_tp_levels(
        self,
        position_id: "int | str",
        tp2: float | None = None,
        tp3: float | None = None,
    ) -> bool:
        """Store TP2/TP3 on a Position after a successful amend_sl_tp.

        Sprint Task 1.3 (card a7b8e896): the cTrader Open API proto
        ``ProtoOAAmendPositionSLTPReq`` only accepts a single take-profit
        per position, so TP2/TP3 cannot be sent to the broker.  Instead we
        stash them on the ``Position`` object here so that
        :class:`PositionMonitor` (Task 1.5) can ratchet the broker TP as
        price crosses each level.

        ``position_id`` is fuzzy-matched against the known position keys
        because callers come from different code paths with different
        identifier conventions:

        * **Internal Position id** — ``"POS_{order.order_id}"`` (the key
          OrderManager itself uses when storing positions).
        * **cTrader broker position id** — an integer or numeric string
          (the value ForwardTestEngine passes to ``amend_sl_tp``).  We
          match this against the ``.position_id`` attribute that the
          ``on_filled`` callback stashes on the ``Order`` object.
        * **order_id** — a non-numeric string.  The Position would be
          stored under ``POS_{order_id}``.

        Returns ``True`` if the position was located and updated,
        ``False`` otherwise (with a warning logged).  Callers should treat
        a ``False`` result as non-fatal: the broker TP1 still protects
        the position; ratcheting just won't activate for this trade.
        """
        with self._lock:
            # Try 1: direct hit on the position key.
            pos = self._positions.get(position_id)
            if pos is not None:
                pos.take_profit_2 = tp2
                pos.take_profit_3 = tp3
                return True

            # Try 2: numeric id → resolve via _orders[].position_id
            # (set via setattr from the cTrader execution event payload).
            target_pid: int | None = None
            try:
                if isinstance(position_id, int) and not isinstance(position_id, bool):
                    target_pid = position_id
                elif isinstance(position_id, str) and position_id.isdigit():
                    target_pid = int(position_id)
            except (ValueError, TypeError):
                target_pid = None

            if target_pid is not None:
                for order_id, order in list(self._orders.items()):
                    broker_pid = getattr(order, "position_id", None)
                    if broker_pid is None:
                        continue
                    try:
                        if int(broker_pid) == target_pid:
                            expected_key = f"POS_{order_id}"
                            pos = self._positions.get(expected_key)
                            if pos is not None:
                                pos.take_profit_2 = tp2
                                pos.take_profit_3 = tp3
                                return True
                    except (ValueError, TypeError):
                        continue

            # Try 3: non-numeric string → assume it's an order_id and
            # look up POS_{order_id}.
            if isinstance(position_id, str) and not position_id.isdigit():
                expected_key = f"POS_{position_id}"
                pos = self._positions.get(expected_key)
                if pos is not None:
                    pos.take_profit_2 = tp2
                    pos.take_profit_3 = tp3
                    return True

            logger.warning(
                "update_position_tp_levels: no Position found for lookup=%r "
                "(positions=%d, orders=%d) — TP ratcheting will not activate "
                "for this trade (broker TP1 still protects the position)",
                position_id,
                len(self._positions),
                len(self._orders),
            )
            return False

    def update_position(
        self,
        position_id: str,
        current_price: float,
        bid: float = 0,
        ask: float = 0,
        contract_size: float | None = None,
    ) -> Position | None:
        with self._lock:
            if position_id not in self._positions:
                return None

            position = self._positions[position_id]
            position.current_price = current_price
            effective_contract_size = self._resolve_contract_size(
                position.symbol, contract_size
            )

            if position.direction == TradeDirection.LONG:
                exit_price = bid if bid > 0 else current_price
                position.unrealized_pnl = (
                    (exit_price - position.entry_price)
                    * position.volume
                    * effective_contract_size
                )
            else:
                exit_price = ask if ask > 0 else current_price
                position.unrealized_pnl = (
                    (position.entry_price - exit_price)
                    * position.volume
                    * effective_contract_size
                )

            if self._check_stop_loss_hit(position, current_price, bid, ask):
                # Fill the SL close at the side that actually triggered the
                # check (bid for LONG, ask for SHORT) — this mirrors the
                # take-profit path below and matches what a real broker
                # would fill at: you close a LONG by selling at the bid and
                # a SHORT by buying at the ask. The previous card 75b24f98
                # filled at the configured stop_loss level, which produced
                # closed_price=stop_loss instead of the triggering tick and
                # failed tests expecting bid/ask-aligned fills
                # (card 9f051898 — bid/ask side-selection fix). Conservative
                # pips-per-side slippage remains a SEPARATE concern
                # (card 46b631ab).
                sl_fill = bid if position.direction == TradeDirection.LONG else ask
                self._close_position(
                    position,
                    sl_fill,
                    reason="sl_hit",
                    contract_size=effective_contract_size,
                )
            elif self._check_take_profit_hit(position, current_price, bid, ask):
                tp_fill = ask if position.direction == TradeDirection.LONG else bid
                self._close_position(
                    position,
                    tp_fill if tp_fill > 0 else position.take_profit,
                    reason="tp_hit",
                    contract_size=effective_contract_size,
                )

            return position

    def _check_stop_loss_hit(self, position: Position, current_price: float, bid: float, ask: float) -> bool:
        if position.stop_loss is None:
            return False

        # Require a real quote on the relevant side before evaluating SL.
        # A zero/missing bid (LONG) or ask (SHORT) is a "no quote" sentinel
        # — we cannot conclude the SL level was actually crossed without
        # the side that would have triggered the exit. Falling back to
        # current_price caused false triggers when the bar mid dipped past
        # the SL but the actual exit-side quote was unavailable
        # (card 9f051898 — bid/ask side-selection fix). The unrealized_pnl
        # computation in update_position() still falls back to current_price
        # for P&L display, which is the correct behaviour for a missing
        # quote during a tick.
        if position.direction == TradeDirection.LONG:
            # For long: SL triggers when bid falls to/below SL level.
            if bid <= 0:
                return False
            return bid <= position.stop_loss
        # For short: SL triggers when ask rises to/above SL level.
        if ask <= 0:
            return False
        return ask >= position.stop_loss

    def _check_take_profit_hit(self, position: Position, current_price: float, bid: float, ask: float) -> bool:
        if position.take_profit is None:
            return False

        # Fall back to current_price when bid/ask unavailable (same fix
        # as _check_stop_loss_hit — see card 9310bdd0).
        if position.direction == TradeDirection.LONG:
            # For long: TP triggers when price rises to TP level
            fill_price = ask if ask > 0 else current_price
            return fill_price >= position.take_profit
        else:
            # For short: TP triggers when price falls to TP level
            fill_price = bid if bid > 0 else current_price
            return fill_price <= position.take_profit

    def close_position(
        self,
        position_id: str,
        exit_price: float | None = None,
        reason: str = "manual",
        contract_size: float | None = None,
    ) -> Position | None:
        with self._lock:
            if position_id not in self._positions:
                return None

            position = self._positions[position_id]
            effective_contract_size = self._resolve_contract_size(
                position.symbol, contract_size
            )
            return self._close_position(
                position,
                exit_price,
                reason,
                contract_size=effective_contract_size,
            )

    def _resolve_contract_size(
        self, symbol: str, contract_size: float | None
    ) -> float:
        """Resolve the contract size to use for P&L math.

        Falls back to the symbol-specific canonical contract_size
        (e.g. 100 oz/lot for XAUUSD via
        ``models.SYMBOL_METADATA``/``get_symbol_info``) when the caller
        does not pass an explicit ``contract_size``. This prevents the
        ~1000× P&L distortion on non-FX symbols (card
        ``047cd91d-51b1-46f1-b8a9-dd1add80338a``) where the previous
        default of ``100_000.0`` (FX standard lot units) silently
        mis-scaled P&L by 1000× for XAUUSD's 100-oz contract.

        Args:
            symbol: Trading symbol the position is held on.
            contract_size: Caller-provided value, or ``None`` for
                symbol-canonical lookup.

        Returns:
            The contract size to use (caller value wins when provided).
        """
        if contract_size is not None:
            return contract_size
        try:
            return float(get_symbol_info(symbol).contract_size)
        except Exception:  # noqa: BLE001 — defensive fallback for unknown symbols
            return 100_000.0

    def _close_position(
        self,
        position: Position,
        exit_price: float | None = None,
        reason: str = "unknown",
        *,
        contract_size: float | None = None,
    ) -> Position:
        exit_price = exit_price or position.current_price
        effective_contract_size = self._resolve_contract_size(
            position.symbol, contract_size
        )

        if position.direction == TradeDirection.LONG:
            pnl = (exit_price - position.entry_price) * position.volume * effective_contract_size
        else:
            pnl = (position.entry_price - exit_price) * position.volume * effective_contract_size

        position.status = PositionStatus.CLOSED
        position.closed_at = datetime.utcnow()
        position.closed_price = exit_price
        position.closed_pnl = pnl

        # Store close reason on the position for downstream consumers
        # (PaperTrader stats mapping, signal-stats recorder).  Not a
        # dataclass field — set dynamically to avoid touching models.py.
        position.close_reason = reason

        logger.info(f"Position {position.position_id} closed: {reason} @ {exit_price}, PnL: {pnl:.2f}")

        self._trigger_callback("on_position_closed", position)
        return position

    def get_open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self._positions.values() if p.status == PositionStatus.OPEN]

    def get_position(self, position_id: str) -> Position | None:
        with self._lock:
            return self._positions.get(position_id)

    def get_total_unrealized_pnl(self) -> float:
        with self._lock:
            return sum(p.unrealized_pnl for p in self._positions.values() if p.status == PositionStatus.OPEN)

    def get_total_realized_pnl(self) -> float:
        with self._lock:
            return sum(p.closed_pnl for p in self._positions.values() if p.status.is_closed)

    def get_pending_orders(self) -> list[Order]:
        with self._lock:
            return [o for o in self._orders.values() if o.status == OrderStatus.PENDING]

    def check_pending_orders_timeout(self) -> list[Order]:
        expired_orders = []
        now = datetime.now(timezone.utc)
        timeout = self._pending_timeout_config.timeout_seconds

        with self._lock:
            for order_id, placed_at in list(self._pending_order_timestamps.items()):
                if (now - placed_at).total_seconds() > timeout:
                    if order_id in self._orders:
                        order = self._orders[order_id]
                        if order.status == OrderStatus.PENDING:
                            order.status = OrderStatus.CANCELLED
                            order.comment = f"Timeout: order pending > {timeout}s"
                            expired_orders.append(order)
                            logger.warning(f"Order {order_id} timed out after {timeout}s in PENDING state")

            for order in expired_orders:
                self._pending_order_timestamps.pop(order.order_id, None)

        for order in expired_orders:
            self._trigger_callback("on_order_timeout", order)

        return expired_orders

    def register_callback(self, event: str, callback: Callable):
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def _trigger_callback(self, event: str, *args, **kwargs):
        if event in self._callbacks:
            for callback in self._callbacks[event]:
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Callback error for {event}: {e}")

    @property
    def position_count(self) -> int:
        return len(self.get_open_positions())
