"""Position tracker — tracks open positions and reconciles with cTrader.

Updated by ExecutionEventHandler (on fill → open, on close → remove).
Can query cTrader for real position reconciliation.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .protocols import Position, PositionStatus

logger = logging.getLogger(__name__)

# Contract size for forex: 1 lot = 100,000 units
_CONTRACT_SIZE = 100_000


class PositionTracker:
    """Tracks open positions and their P&L.

    Stores positions internally as mutable dicts so that ``update_prices``
    can patch current_price and PnL on every tick.  Public getters return
    immutable :class:`Position` instances from ``protocols.py``.
    """

    def __init__(self, session: Any) -> None:
        """Initialise the tracker.

        Args:
            session: A :class:`cTraderSession` (or any object with a
                ``send(message, client_msg_id, timeout)`` method).  The
                session is only used for ``reconcile_with_ctrader``.
        """
        self._session = session
        self._positions: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ── Event callbacks (called by ExecutionEventHandler) ──────────────────

    def on_position_opened(
        self,
        position_id: int,
        symbol: str,
        direction: str,
        volume: float,
        entry_price: float,
        sl: float | None = None,
        tp: float | None = None,
    ) -> None:
        """Record a newly opened position.

        Called by :class:`ExecutionEventHandler` when an order fill
        creates a position.
        """
        with self._lock:
            self._positions[position_id] = {
                "position_id": str(position_id),
                "symbol": symbol,
                "direction": direction,
                "volume": volume,
                "entry_price": entry_price,
                "current_price": entry_price,
                "stop_loss": sl,
                "take_profit": tp,
                "pnl": 0.0,
                "status": PositionStatus.OPEN,
            }
            logger.info(
                "Position opened: id=%s symbol=%s dir=%s vol=%.2f entry=%.5f",
                position_id,
                symbol,
                direction,
                volume,
                entry_price,
            )

    def on_position_closed(self, position_id: int, pnl: float = 0.0) -> None:
        """Remove a closed position from tracking.

        Called by :class:`ExecutionEventHandler` when a position is
        closed (manually, SL, or TP).
        """
        with self._lock:
            removed = self._positions.pop(position_id, None)
            if removed:
                logger.info(
                    "Position closed: id=%s pnl=%.2f",
                    position_id,
                    pnl,
                )
            else:
                logger.warning(
                    "Close for unknown position id=%s",
                    position_id,
                )

    # ── Read API ───────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[Position]:
        """Return all currently open positions."""
        with self._lock:
            return [self._to_position(v) for v in self._positions.values()]

    def get_position(self, position_id: int) -> Position | None:
        """Return a single position by ID, or ``None``."""
        with self._lock:
            raw = self._positions.get(position_id)
            return self._to_position(raw) if raw else None

    # ── Price updates ──────────────────────────────────────────────────────

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update current_price and recompute PnL for all open positions.

        Called on every tick.

        Args:
            prices: Mapping of symbol → current mid price.
        """
        with self._lock:
            for pos in self._positions.values():
                price = prices.get(pos["symbol"])
                if price is None:
                    continue
                pos["current_price"] = price
                pos["pnl"] = self._compute_pnl(
                    pos["direction"],
                    pos["entry_price"],
                    price,
                    pos["volume"],
                )

    # ── Reconciliation ─────────────────────────────────────────────────────

    def reconcile_with_ctrader(self) -> list[Position]:
        """Fetch real positions from cTrader and return discrepancies.

        Sends ``ProtoOAReconcileReq`` via the session and compares the
        result with locally-tracked positions.

        Returns:
            List of positions present on cTrader but **not** tracked
            locally (i.e. positions we missed).  An empty list means
            we are in sync.
        """
        with self._lock:
            local_ids = {p["position_id"] for p in self._positions.values()}

        # Build the reconcile request.  We import here so the module
        # can be used without the cTrader SDK installed (tests mock
        # the session).
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import (
                ProtoOAReconcileReq,
            )
        except ImportError:
            logger.warning("ctrader_open_api not available — reconcile skipped")
            return []

        req = ProtoOAReconcileReq()
        # The session is expected to set ctidTraderAccountId itself or
        # it was already configured at construction time.
        try:
            response = self._session.send(req, "reconcile", timeout=10.0)
        except Exception as exc:
            logger.error("Reconcile request failed: %s", exc)
            return []

        if response is None:
            logger.warning("Reconcile timed out — no response")
            return []

        remote_positions = self._parse_reconcile_response(response)
        discrepancies = [rp for rp in remote_positions if rp.position_id not in local_ids]

        if discrepancies:
            logger.warning(
                "Reconcile found %d position(s) not tracked locally",
                len(discrepancies),
            )
        else:
            logger.info("Reconcile OK — local state matches cTrader")

        return discrepancies

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _compute_pnl(
        direction: str,
        entry_price: float,
        current_price: float,
        volume: float,
    ) -> float:
        """Compute unrealised PnL for a position.

        Uses the standard forex contract size of 100,000 units per lot.
        """
        size = volume * _CONTRACT_SIZE
        if direction.upper() in ("BUY", "LONG"):
            return round((current_price - entry_price) * size, 2)
        else:
            return round((entry_price - current_price) * size, 2)

    @staticmethod
    def _to_position(raw: dict[str, Any]) -> Position:
        """Convert internal mutable dict to immutable :class:`Position`."""
        return Position(
            position_id=raw["position_id"],
            symbol=raw["symbol"],
            direction=raw["direction"],
            volume=raw["volume"],
            entry_price=raw["entry_price"],
            current_price=raw["current_price"],
            stop_loss=raw.get("stop_loss"),
            take_profit=raw.get("take_profit"),
            pnl=raw.get("pnl", 0.0),
            status=raw.get("status", PositionStatus.OPEN),
        )

    @staticmethod
    def _parse_reconcile_response(response: Any) -> list[Position]:
        """Parse a ``ProtoOAReconcileRes`` into a list of :class:`Position`.

        Tolerates duck-typed mock objects (``MagicMock``) so tests don't
        need the real protobuf classes.
        """
        # Unwrap envelope if the response has a payload attribute.
        payload = getattr(response, "payload", response)

        positions: list[Position] = []
        for raw in getattr(payload, "position", []) or []:
            try:
                td = getattr(raw, "tradeData", None)
                pos = Position(
                    position_id=str(getattr(raw, "positionId", "")),
                    symbol=str(getattr(td, "symbolId", "") if td else ""),
                    direction="BUY",
                    volume=float(getattr(td, "volume", 0)) / _CONTRACT_SIZE,
                    entry_price=float(getattr(raw, "price", 0.0)),
                    current_price=float(getattr(raw, "price", 0.0)),
                    stop_loss=float(getattr(raw, "stopLoss", 0)) or None,
                    take_profit=float(getattr(raw, "takeProfit", 0)) or None,
                    status=PositionStatus.OPEN,
                )
                positions.append(pos)
            except Exception as exc:
                logger.warning("Reconcile parse error: %s", exc)

        return positions
