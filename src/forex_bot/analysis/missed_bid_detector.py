"""Missed-bid detector — signal-vs-fill gap analysis.

Compares generated OrchestratorTradeSignals against opened positions to identify
signals that never resulted in a live position, classifying the likely reason.

Phase 1: read-only log analysis. Does NOT modify strategy/orchestrator/order code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orchestrator.signal_orchestrator import OrchestratorTradeSignal


@dataclass
class MissedBid:
    """A signal that did not result in a filled position."""

    signal_id: str
    symbol: str
    entry_price: float
    signal_time: datetime
    direction: str  # "long" | "short" (mirrors OrchestratorTradeSignal.direction)
    bars_to_expiry: int  # bars elapsed before signal invalidated
    reason: str  # "no_fill" | "filtered" | "price_reversed"


@dataclass
class _Bar:
    """Normalised OHLC bar used for fill simulation."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


class MissedBidDetector:
    """Compare signals against positions to find missed bids.

    Parameters
    ----------
    max_bars_to_fill : int
        Maximum number of bars after a signal within which a position
        must have been opened. Signals older than this without a matching
        position are classified as missed.
    """

    def __init__(self, max_bars_to_fill: int = 5) -> None:
        if max_bars_to_fill < 1:
            raise ValueError("max_bars_to_fill must be >= 1")
        self._max_bars_to_fill = max_bars_to_fill

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        signals: list[OrchestratorTradeSignal],
        positions: list[dict[str, Any]],
        bars: list[dict[str, Any]],
    ) -> list[MissedBid]:
        """Compare *signals* against *positions* and return missed bids.

        Parameters
        ----------
        signals : list[OrchestratorTradeSignal]
            Strategy-generated signals.
        positions : list[dict]
            Opened positions.  Each dict should carry at least
            ``symbol``, ``direction`` (``"long"``/``"short"``) and
            ``signal_id`` (the originating signal) or ``open_time``
            (approximate fill time).
        bars : list[dict]
            OHLC bars for the relevant symbol(s).  Each dict should
            carry ``symbol``, ``timestamp`` (datetime or ISO str),
            ``open``, ``high``, ``low``, ``close``.

        Returns
        -------
        list[MissedBid]
            One entry per signal that did NOT result in a fill,
            with the inferred ``reason``.
        """
        matched_ids = self._matched_signal_ids(signals, positions)
        bars_by_symbol = self._index_bars(bars)

        missed: list[MissedBid] = []

        for signal in signals:
            sig_id = self._signal_id(signal)
            if sig_id in matched_ids:
                continue

            symbol_bars = bars_by_symbol.get(signal.symbol, [])
            reason = self._classify_miss(signal, symbol_bars)

            missed.append(
                MissedBid(
                    signal_id=sig_id,
                    symbol=signal.symbol,
                    entry_price=signal.entry_price,
                    signal_time=signal.timestamp,
                    direction=signal.direction,
                    bars_to_expiry=min(self._max_bars_to_fill, len(symbol_bars)),
                    reason=reason,
                )
            )

        return missed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _signal_id(signal: OrchestratorTradeSignal) -> str:
        """Derive a stable id from an OrchestratorTradeSignal."""
        return signal.metadata.get(
            "signal_id",
            f"{signal.strategy_id}:{signal.symbol}:{signal.timestamp.isoformat()}",
        )

    @staticmethod
    def _matched_signal_ids(
        signals: list[OrchestratorTradeSignal],
        positions: list[dict[str, Any]],
    ) -> set[str]:
        """Return the set of signal IDs that have a matching position."""
        matched: set[str] = set()

        # Build a quick lookup of position signal references
        for pos in positions:
            sid = pos.get("signal_id")
            if sid:
                matched.add(sid)
                continue

            # Fallback: match on symbol + direction + rough time proximity
            pos_symbol = pos.get("symbol")
            pos_direction = pos.get("direction", "")
            for sig in signals:
                sig_id = MissedBidDetector._signal_id(sig)
                if sig.symbol == pos_symbol and sig.direction == pos_direction:
                    matched.add(sig_id)

        return matched

    @staticmethod
    def _index_bars(
        bars: list[dict[str, Any]],
    ) -> dict[str, list[_Bar]]:
        """Group bars by symbol, sorted chronologically."""
        by_symbol: dict[str, list[_Bar]] = {}
        for raw in bars:
            symbol = raw.get("symbol", "")
            ts = raw.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            elif not isinstance(ts, datetime):
                continue  # skip malformed bars

            bar = _Bar(
                timestamp=ts,
                open=float(raw["open"]),
                high=float(raw["high"]),
                low=float(raw["low"]),
                close=float(raw["close"]),
            )
            by_symbol.setdefault(symbol, []).append(bar)

        for symbol in by_symbol:
            by_symbol[symbol].sort(key=lambda b: b.timestamp)

        return by_symbol

    def _classify_miss(
        self,
        signal: OrchestratorTradeSignal,
        symbol_bars: list[_Bar],
    ) -> str:
        """Determine why a signal was not filled.

        Decision logic (first match wins):

        1. **filtered** — fewer than ``max_bars_to_fill`` bars after the
           signal AND the entry price was never touched.  The signal was
           likely dropped by a downstream filter before reaching execution.

        2. **no_fill** — the entry price was reached within the window
           (high ≥ entry for longs, low ≤ entry for shorts) but no
           position was opened.  Indicates an execution gap.

        3. **price_reversed** — the price initially approached the entry
           but then moved away without touching it, and never came back
           within the window.
        """
        window = symbol_bars[: self._max_bars_to_fill]

        if not window:
            # No bars at all — treat as filtered (signal generated but
            # no market data to confirm execution opportunity).
            return "filtered"

        entry = signal.entry_price
        is_long = signal.direction == "long"

        # Did price ever reach the entry level within the window?
        entry_touched = False
        approached_then_reversed = False
        min_distance = float("inf")

        for bar in window:
            if is_long:
                touched = bar.low <= entry <= bar.high
            else:
                touched = bar.low <= entry <= bar.high

            if touched:
                entry_touched = True
                break

            # Track how close price got to entry
            if is_long:
                distance = entry - bar.low  # how far above entry the low is
                if distance < 0:  # price is above entry — closer for a short
                    distance = abs(entry - bar.high)
            else:
                distance = bar.high - entry
                if distance < 0:
                    distance = abs(entry - bar.low)

            min_distance = min(min_distance, distance)

        # Check for price_reversed: first bar(s) approach, then move away
        if not entry_touched and len(window) >= 2:
            first_half = window[: len(window) // 2]
            second_half = window[len(window) // 2 :]

            if is_long:
                first_close = min(b.close for b in first_half)
                last_close = min(b.close for b in second_half)
                # Price was below entry early, then dropped further
                if first_close < entry and last_close < first_close:
                    approached_then_reversed = True
            else:
                first_close = max(b.close for b in first_half)
                last_close = max(b.close for b in second_half)
                if first_close > entry and last_close > first_close:
                    approached_then_reversed = True

        if entry_touched:
            return "no_fill"

        if approached_then_reversed:
            return "price_reversed"

        return "filtered"
