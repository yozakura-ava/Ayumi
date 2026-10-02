"""Formal ISignalStrategy interface — lifecycle hooks + type safety (BQ-687).

This module defines the canonical strategy interface for the Ayumi trading
system. All signal-generation strategies must implement this ABC.

Lifecycle
---------
    initialize(config)   → Called once at strategy startup
    on_bar(bar)          → Called for each completed bar (primary signal path)
    on_tick(tick)        → Called for each tick (optional, for tick-level strategies)
    generate_signal()    → Called after bar/tick processing to produce a signal
    evaluate(state)      → Convenience: stateful evaluation (legacy compat)
    shutdown()           → Called once at strategy teardown

Design Notes
------------
- ``evaluate(state)`` is retained as the primary entry point for the backtest
  engine (SimpleBacktestEngine calls ``strategy.evaluate(state)`` on each bar).
- ``on_bar()`` and ``generate_signal()`` provide a cleaner separation for
  new strategies that prefer the decomposed lifecycle.
- All lifecycle hooks have default no-op implementations so existing
  strategies that only override ``evaluate()`` continue to work unchanged.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tick type (lightweight, for on_tick hooks)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tick:
    """A single price tick (bid/ask/last).

    Used by ``ISignalStrategy.on_tick()`` for tick-level strategies.
    Strategies that only work on bars can ignore this.
    """

    timestamp: datetime
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0

    @property
    def mid(self) -> float:
        """Mid-market price."""
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        return self.ask - self.bid


# ---------------------------------------------------------------------------
# Strategy config protocol
# ---------------------------------------------------------------------------


class StrategyConfig(Protocol):
    """Protocol for strategy configuration objects.

    Any dataclass or dict-like object with these fields is acceptable.
    Strategies define their own config dataclasses (e.g., ``SRMRPlusConfig``).
    """

    def __getitem__(self, key: str) -> Any: ...
    def get(self, key: str, default: Any = None) -> Any: ...


# ---------------------------------------------------------------------------
# ISignalStrategy — Formal ABC
# ---------------------------------------------------------------------------


class ISignalStrategy(ABC):
    """Canonical signal strategy interface for the Ayumi trading system.

    All strategies must inherit from this class. The primary signal
    generation method is ``evaluate(state)``, which the backtest engine
    calls on each bar. New strategies may optionally use the decomposed
    lifecycle (``on_bar`` → ``generate_signal``) instead.

    Lifecycle Hooks (override as needed)
    ------------------------------------
    - ``initialize(config)``: Set up strategy state, load indicators, etc.
    - ``on_bar(bar)``: Process each completed bar.
    - ``on_tick(tick)``: Process each incoming tick (optional).
    - ``generate_signal()``: Produce a signal from accumulated state.
    - ``shutdown()``: Cleanup resources, flush logs, etc.

    Required
    --------
    - ``name`` property: Human-readable strategy name.
    - ``evaluate(state)``: Primary signal evaluation (called by engine).

    Backward Compatibility
    ----------------------
    Strategies that only implement ``name`` and ``evaluate()`` (the original
    interface) work unchanged because all new hooks have default no-ops.
    """

    def __init__(self) -> None:
        self._initialized: bool = False
        self._bars_processed: int = 0

    # ------------------------------------------------------------------
    # Required: identity + signal generation
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name (e.g., 'MA Crossover')."""
        ...

    @abstractmethod
    def evaluate(self, state: Any) -> Any:
        """Evaluate market state and return a signal (or None).

        This is the primary entry point called by the backtest engine
        on each bar. It must return a ``StrategySignal`` or ``None``.

        Parameters
        ----------
        state : MarketState
            Current market state (bars, session, ATR, etc.).

        Returns
        -------
        StrategySignal | None
        """
        ...

    # ------------------------------------------------------------------
    # Lifecycle hooks (optional — default implementations are no-ops)
    # ------------------------------------------------------------------

    def initialize(self, config: StrategyConfig | dict[str, Any] | None = None) -> None:
        """Initialize the strategy with configuration.

        Called once before the first bar/tick. Override to set up
        indicators, load historical data, or configure parameters.

        Parameters
        ----------
        config : StrategyConfig or dict, optional
            Strategy-specific configuration.
        """
        self._initialized = True
        self._bars_processed = 0
        logger.debug("Strategy %s initialized", self.name)

    def on_bar(self, bar: Any) -> None:
        """Process a completed bar.

        Called by the engine for each new bar before ``evaluate()``.
        Override to accumulate state incrementally.

        Parameters
        ----------
        bar : Bar
            The newly completed bar.
        """
        self._bars_processed += 1

    def on_tick(self, tick: Tick) -> None:  # noqa: B027
        """Process a tick (for tick-level strategies).

        Called by the engine for each incoming tick. Override only
        if your strategy needs tick-level granularity.

        Parameters
        ----------
        tick : Tick
            The incoming tick data.
        """
        pass

    def generate_signal(self) -> Any:
        """Generate a signal from accumulated state.

        Alternative to ``evaluate()`` for strategies that prefer to
        accumulate state via ``on_bar()`` / ``on_tick()`` and then
        produce a signal separately. Default implementation returns
        ``None`` (strategy must override if using this pattern).

        Returns
        -------
        StrategySignal | None
        """
        return None

    def shutdown(self) -> None:
        """Clean up strategy resources.

        Called once after the last bar/tick. Override to flush logs,
        save state, release resources, etc.
        """
        logger.debug(
            "Strategy %s shutdown (processed %d bars)",
            self.name,
            self._bars_processed,
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @property
    def is_initialized(self) -> bool:
        """Whether ``initialize()`` has been called."""
        return self._initialized

    @property
    def bars_processed(self) -> int:
        """Number of bars processed via ``on_bar()``."""
        return self._bars_processed

    def reset(self) -> None:
        """Reset strategy state to initial values.

        Called between backtest runs. Override to reset indicators,
        clear buffers, etc. Default resets the internal counters.
        """
        self._bars_processed = 0
        self._initialized = False

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} initialized={self._initialized}>"
