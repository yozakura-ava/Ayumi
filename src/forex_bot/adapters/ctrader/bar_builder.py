"""BarBuilder — aggregate ticks into OHLCV bars for multiple timeframes.

Extracts bar-building logic from ForwardTestEngine into a standalone,
testable component. Supports multiple timeframes per symbol, bar
completion callbacks, memory-bounded storage, and historical bar
pre-loading.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("ayumi.bar_builder")

# ── Defaults ───────────────────────────────────────────────────────────────

_DEFAULT_MAX_BARS = 500
_BURST_MULTIPLIER = 1.5


class BarBuilder:
    """Aggregates ticks into OHLCV bars for multiple timeframes.

    Thread-safe: all mutations are guarded by an internal lock.

    Usage::

        builder = BarBuilder()
        builder.add_timeframe("GBPUSD", 15)
        builder.add_timeframe("GBPUSD", 60)

        builder.on_bar_close = lambda key, bar: print(f"{key}: {bar}")
        builder.process_tick(tick, "GBPUSD")

        bars = builder.get_bars("GBPUSD", 60)
    """

    def __init__(self, max_bars: int = _DEFAULT_MAX_BARS):
        self._max_bars = max_bars
        self._lock = threading.Lock()

        # Per-timeframe state: key = "SYMBOL:period_minutes"
        self._bars: dict[str, list] = {}  # finalized bars
        self._current_bar: dict[str, Optional[dict]] = {}  # forming bar
        self._timeframes: set[int] = set()

        # Burst allocation budget during reconnects
        self._burst_mode = False

        # Callbacks
        self.on_bar_close: Optional[Callable[[str, dict], None]] = None

    # ── Configuration ──────────────────────────────────────────────────────

    def add_timeframe(self, symbol: str, period_minutes: int) -> None:
        """Register a timeframe for a symbol."""
        key = self._bar_key(symbol, period_minutes)
        with self._lock:
            self._timeframes.add(period_minutes)
            if key not in self._bars:
                self._bars[key] = []

    @property
    def timeframes(self) -> set[int]:
        return set(self._timeframes)

    def set_burst_mode(self, enabled: bool) -> None:
        """Enable burst allocation during reconnects (max_bars × 1.5)."""
        self._burst_mode = enabled

    # ── Tick processing ────────────────────────────────────────────────────

    def process_tick(self, tick, symbol: str) -> list[str]:
        """Process a tick, updating bars for all registered timeframes.

        Args:
            tick: A Tick-like object with .bid, .ask, .mid, .timestamp.
            symbol: Normalized symbol name (e.g. "GBPUSD").

        Returns:
            List of bar keys that were completed by this tick.
        """
        completed_keys: list[str] = []

        with self._lock:
            for tf in self._timeframes:
                key = self._bar_key(symbol, tf)
                if key not in self._bars:
                    self._bars[key] = []

                bar_time = self._bar_period_start(tick.timestamp, tf)
                if self._update_bar(key, tick, bar_time):
                    completed_keys.append(key)

        # Fire callbacks outside lock
        for key in completed_keys:
            bars = self._bars.get(key, [])
            if bars and self.on_bar_close is not None:
                try:
                    self.on_bar_close(key, bars[-1])
                except Exception as exc:
                    logger.error("Bar close callback error for %s: %s", key, exc)

        return completed_keys

    # ── Bar access ─────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, period_minutes: int) -> list[dict]:
        """Get finalized bars for a symbol+timeframe (excludes forming bar)."""
        key = self._bar_key(symbol, period_minutes)
        with self._lock:
            return list(self._bars.get(key, []))

    def get_bars_including_forming(self, symbol: str, period_minutes: int) -> list[dict]:
        """Get bars including the current forming bar as last element."""
        key = self._bar_key(symbol, period_minutes)
        with self._lock:
            bars = list(self._bars.get(key, []))
            forming = self._current_bar.get(key)
            if forming is not None:
                bars.append(forming)
            return bars

    def bar_count(self, symbol: str, period_minutes: int) -> int:
        """Number of finalized bars for a symbol+timeframe."""
        key = self._bar_key(symbol, period_minutes)
        with self._lock:
            return len(self._bars.get(key, []))

    def forming_bar(self, symbol: str, period_minutes: int) -> Optional[dict]:
        """Get the current forming bar (or None)."""
        key = self._bar_key(symbol, period_minutes)
        with self._lock:
            return self._current_bar.get(key)

    def total_bar_count(self) -> int:
        """Total finalized bars across all keys."""
        with self._lock:
            return sum(len(b) for b in self._bars.values())

    # ── Pre-loading ────────────────────────────────────────────────────────

    def preload_bars(self, symbol: str, period_minutes: int, bars: list) -> None:
        """Pre-load historical bars for a symbol+timeframe.

        Trims to max_bars (or max_bars × 1.5 in burst mode).
        """
        key = self._bar_key(symbol, period_minutes)
        limit = self._effective_max_bars()
        with self._lock:
            self._bars[key] = bars[-limit:]
            self._timeframes.add(period_minutes)
        logger.info("Preloaded %d bars into '%s'", len(self._bars[key]), key)

    # ── Finalization ───────────────────────────────────────────────────────

    def finalize_all(self) -> list[str]:
        """Finalize all forming bars (e.g. on shutdown).

        Returns list of keys that were finalized.
        """
        finalized_keys: list[str] = []
        with self._lock:
            for key in list(self._current_bar.keys()):
                bar = self._current_bar.get(key)
                if bar is not None:
                    self._store_bar(key, bar)
                    self._current_bar[key] = None
                    finalized_keys.append(key)
        return finalized_keys

    # ── Internal ───────────────────────────────────────────────────────────

    @staticmethod
    def _bar_key(symbol: str, period_minutes: int) -> str:
        return f"{symbol}:{period_minutes}"

    @staticmethod
    def _bar_period_start(ts: datetime, period_minutes: int) -> datetime:
        """Compute the start of the bar period containing `ts`."""
        return ts.replace(second=0, microsecond=0) - timedelta(
            minutes=ts.minute % period_minutes,
        )

    def _effective_max_bars(self) -> int:
        """Max bars considering burst mode."""
        if self._burst_mode:
            return int(self._max_bars * _BURST_MULTIPLIER)
        return self._max_bars

    def _update_bar(self, key: str, tick, bar_time: datetime) -> bool:
        """Update or create bar for key. Returns True if a bar was completed."""
        current = self._current_bar.get(key)

        if current is not None and current["time"] == bar_time:
            # Update existing forming bar
            self._current_bar[key] = {
                "time": current["time"],
                "open": current["open"],
                "high": max(current["high"], tick.ask),
                "low": min(current["low"], tick.bid),
                "close": tick.mid,
                "volume": current["volume"] + 1,
            }
            return False

        # New bar period — finalize the old one
        completed = False
        if current is not None:
            self._store_bar(key, current)
            completed = True

        # Start new forming bar
        self._current_bar[key] = {
            "time": bar_time,
            "open": tick.mid,
            "high": tick.ask,
            "low": tick.bid,
            "close": tick.mid,
            "volume": 1,
        }
        return completed

    def _store_bar(self, key: str, bar: dict) -> None:
        """Store a finalized bar, trimming to max_bars."""
        self._assert_bar_integrity(bar)
        self._bars.setdefault(key, []).append(bar)
        limit = self._effective_max_bars()
        if len(self._bars[key]) > limit:
            self._bars[key] = self._bars[key][-limit:]

    @staticmethod
    def _assert_bar_integrity(bar: dict) -> None:
        """Verify OHLC integrity.

        These were previously ``assert`` statements but asserts are stripped
        under ``python -O``. Bar data flows in from the live tick stream, so
        a malformed bar (high < close, low > open) must fail loudly on every
        Python invocation, not just debug runs — converted to explicit raises
        so the live-trading integrity gate survives optimization.
        """
        if not bar["high"] >= max(bar["open"], bar["close"]):
            raise ValueError(
                f"Bar integrity fail: high={bar['high']} < max(open={bar['open']}, close={bar['close']})"
            )
        if not bar["low"] <= min(bar["open"], bar["close"]):
            raise ValueError(
                f"Bar integrity fail: low={bar['low']} > min(open={bar['open']}, close={bar['close']})"
            )
