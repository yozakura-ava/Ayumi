"""Market data feed — processes spot events into bars and ticks.

Implements MarketFeedProtocol. Does NOT own a TCP connection.
Receives spot events from Session via on_spot_event().

Replaces the market-data portions of open_api_spot_feed.py (BQ-1043
Phase 2b).  Connection lifecycle lives in session.py; this module is
pure data processing.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from .protocols import Bar as _ProtoBar
from .protocols import Tick as _ProtoTick

logger = logging.getLogger("ayumi.market_data")

# ── Defaults ────────────────────────────────────────────────────────────────

_MAX_STORED_BARS = 500


class MarketDataFeed:
    """Processes incoming spot events into bars and ticks.

    Thread-safe. Spot events arrive on the reactor thread.
    Bar/tick listeners are called on the reactor thread.

    Implements ``MarketFeedProtocol`` structurally (duck typing).
    """

    def __init__(
        self,
        symbols: list[str],
        bar_period_seconds: int = 3600,
        symbol_id_map: dict[int, str] | None = None,
    ):
        """Initialise the feed.

        Args:
            symbols: List of symbol names to track (e.g. ["GBPUSD", "USDJPY"]).
            bar_period_seconds: Bar period in seconds (3600 = H1, 900 = M15).
            symbol_id_map: Optional mapping of cTrader numeric symbol IDs
                to symbol names.  If *None*, ``str(symbol_id)`` is used
                (only suitable for tests).
        """
        self._symbols = set(symbols)
        self._bar_period = bar_period_seconds
        self._symbol_id_map: dict[int, str] = symbol_id_map or {}
        self._lock = threading.Lock()

        # Tick tracking
        self._latest_ticks: dict[str, _ProtoTick] = {}
        self._ticks_received = 0

        # Bar tracking
        self._bars: dict[str, list[_ProtoBar]] = defaultdict(list)
        self._forming_bars: dict[str, _ProtoBar | None] = {}
        self._bars_built = 0

        # Listeners
        self._tick_listeners: list[Callable] = []
        self._bar_listeners: list[Callable] = []

    # ── Spot event ingestion ───────────────────────────────────────────────

    def on_spot_event(
        self,
        symbol_id: int,
        bid: float,
        ask: float,
        timestamp: datetime,
    ) -> None:
        """Called by Session when a spot event arrives.

        Updates latest tick, feeds into the bar builder, notifies listeners.
        """
        symbol = self._resolve_symbol(symbol_id)
        if symbol is None:
            return

        # Reject zero / inverted quotes
        if bid <= 0 or ask <= 0 or bid >= ask:
            return

        mid = (bid + ask) / 2
        tick = _ProtoTick(symbol=symbol, bid=bid, ask=ask, timestamp=timestamp)

        closed_bar = None

        with self._lock:
            # Update tick state
            self._latest_ticks[symbol] = tick
            self._ticks_received += 1

            # Update / close bar
            closed_bar = self._update_bar(symbol, mid, bid, ask, timestamp)

        # Fire listeners outside the lock
        for cb in self._tick_listeners:
            try:
                cb(tick)
            except Exception as exc:
                logger.error("Tick listener error: %s", exc)

        if closed_bar is not None:
            for cb in self._bar_listeners:
                try:
                    cb(closed_bar)
                except Exception as exc:
                    logger.error("Bar listener error: %s", exc)

    # ── Listener registration ──────────────────────────────────────────────

    def add_tick_listener(self, callback: Callable) -> None:
        """Register a callback invoked on every incoming tick."""
        with self._lock:
            self._tick_listeners.append(callback)

    def add_bar_listener(self, callback: Callable) -> None:
        """Register a callback invoked when a bar closes."""
        with self._lock:
            self._bar_listeners.append(callback)

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def ticks_received(self) -> int:
        """Total ticks received since creation."""
        with self._lock:
            return self._ticks_received

    @property
    def bars_built(self) -> int:
        """Total bars built (closed) since creation."""
        with self._lock:
            return self._bars_built

    @property
    def is_connected(self) -> bool:
        """Always ``True`` — connection health is Session's responsibility."""
        return True

    # ── Market data access ─────────────────────────────────────────────────

    def get_tick(self, symbol: str) -> _ProtoTick | None:
        """Get the latest tick for *symbol*, or ``None``."""
        with self._lock:
            return self._latest_ticks.get(symbol)

    def get_latest_bar(self, symbol: str, timeframe: str = "H1") -> _ProtoBar | None:
        """Get the most recent closed bar for *symbol*.

        If no bars have closed yet, returns the current forming bar.
        """
        with self._lock:
            bars = self._bars.get(symbol, [])
            if bars:
                return bars[-1]
            return self._forming_bars.get(symbol)

    def get_closed_bars(self, symbol: str, count: int = 100) -> list[_ProtoBar]:
        """Get up to *count* most recent closed bars for *symbol*."""
        with self._lock:
            bars = self._bars.get(symbol, [])
            return list(bars[-count:])

    # ── Internal: bar building ─────────────────────────────────────────────

    def _bar_period_start(self, ts: datetime) -> datetime:
        """Compute the start of the bar period containing *ts*."""
        epoch_seconds = int(ts.timestamp())
        period_start = (epoch_seconds // self._bar_period) * self._bar_period
        return datetime.fromtimestamp(period_start, tz=ts.tzinfo or timezone.utc)

    def _update_bar(
        self,
        symbol: str,
        mid: float,
        bid: float,
        ask: float,
        timestamp: datetime,
    ) -> _ProtoBar | None:
        """Update forming bar for *symbol*. Return closed bar if one closed.

        Must be called under ``self._lock``.
        """
        period_start = self._bar_period_start(timestamp)
        forming = self._forming_bars.get(symbol)

        if forming is not None and forming.timestamp == period_start:
            # Same period — update OHLC
            self._forming_bars[symbol] = _ProtoBar(
                symbol=symbol,
                timeframe=self._timeframe_label(),
                open=forming.open,
                high=max(forming.high, ask),
                low=min(forming.low, bid),
                close=mid,
                volume=forming.volume + 1,
                timestamp=forming.timestamp,
                is_closed=False,
            )
            return None

        # New period — close old bar, start new one
        closed = None
        if forming is not None:
            closed = _ProtoBar(
                symbol=forming.symbol,
                timeframe=forming.timeframe,
                open=forming.open,
                high=forming.high,
                low=forming.low,
                close=forming.close,
                volume=forming.volume,
                timestamp=forming.timestamp,
                is_closed=True,
            )
            self._store_closed_bar(symbol, closed)
            self._bars_built += 1

        # Start new forming bar
        self._forming_bars[symbol] = _ProtoBar(
            symbol=symbol,
            timeframe=self._timeframe_label(),
            open=mid,
            high=ask,
            low=bid,
            close=mid,
            volume=1,
            timestamp=period_start,
            is_closed=False,
        )
        return closed

    def _store_closed_bar(self, symbol: str, bar: _ProtoBar) -> None:
        """Append a closed bar and trim to ``_MAX_STORED_BARS``."""
        self._bars[symbol].append(bar)
        if len(self._bars[symbol]) > _MAX_STORED_BARS:
            self._bars[symbol] = self._bars[symbol][-_MAX_STORED_BARS:]

    def _timeframe_label(self) -> str:
        """Human-readable timeframe label for the current bar period."""
        seconds = self._bar_period
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"H{seconds // 3600}"
        if seconds >= 60 and seconds % 60 == 0:
            return f"M{seconds // 60}"
        return f"S{seconds}"

    # ── Internal: symbol resolution ────────────────────────────────────────

    def _resolve_symbol(self, symbol_id: int) -> str | None:
        """Resolve a cTrader numeric symbol ID to a symbol name."""
        name = self._symbol_id_map.get(symbol_id)
        if name is not None:
            return name
        # If no map was provided, use str(symbol_id) as fallback (test mode)
        if not self._symbol_id_map:
            return str(symbol_id)
        logger.warning("Unknown symbol_id %s — not in symbol_id_map", symbol_id)
        return None


# ── Legacy compatibility types ──────────────────────────────────────────────
# Old infra code (open_api_spot_feed.py, forward_test_engine.py, orchestrator.py)
# imports ``Tick`` and ``SymbolInfo`` from this module.  These legacy types
# remain until the full cTrader rebuild removes those callers.  The new
# ``MarketDataFeed`` above uses the protocol ``Tick`` from ``protocols.py``
# (with ``symbol: str``), which is deliberately different.

from dataclasses import dataclass, field  # noqa: E402


@dataclass
class LegacyTick:
    """Legacy tick type (symbol_id-based). Used by old infra code only."""

    symbol_id: int
    bid: float
    ask: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


# Keep the old name alive for backward-compatible imports.
Tick = LegacyTick  # type: ignore[assignment,misc]


@dataclass
class SymbolInfo:
    """Unified symbol metadata for live trading and paper simulation.

    Expanded (Sprint: Order System Modularization) to carry volume
    fields so that VolumeCalculator and downstream modules can derive
    contract size, validate constraints, and decode prices per-symbol
    instead of hardcoding 100 000.
    """

    symbol_id: int
    name: str
    pip_size: float = 0.0001
    digits: int = 5
    lot_size: int = 100_000  # contract size per lot (100k FX, 100 crypto)
    min_volume: int = 0  # minimum cTrader volume (0 = unspecified)
    max_volume: int = 0  # maximum cTrader volume (0 = unlimited)
    step_volume: int = 1  # volume step increment

    @property
    def contract_size(self) -> float:
        """Alias for lot_size as float (backward compat with models.py consumers)."""
        return float(self.lot_size)

    @property
    def pip_value_per_lot(self) -> float:
        """Approximate pip value per standard lot (USD).

        Standard forex default; override in SYMBOL_METADATA for non-FX.
        """
        return 10.0


DEFAULT_SYMBOLS: dict[int, str] = {
    1: "EUR/USD",
    2: "GBP/USD",
    3: "USD/JPY",
    4: "USD/CHF",
    5: "AUD/USD",
    6: "USD/CAD",
    7: "NZD/USD",
    31: "XAU/USD",
}

FOREX_PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD"]


class LiveMarketDataFeed:
    """DEPRECATED: FIX-mode live feed. Use MarketDataFeed instead."""

    def __init__(self, credentials):
        raise NotImplementedError(
            "LiveMarketDataFeed (FIX mode) is deprecated. Use MarketDataFeed with the new session.py instead."
        )

    def on_tick(self, handler):
        pass

    def start(self, auto_subscribe=None):
        raise NotImplementedError("FIX mode is deprecated")

    def stop(self):
        pass
