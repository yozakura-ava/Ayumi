"""Abstract base class for OHLCV bar data loaders.

Defines the common interface that all data loaders (CsvDataLoader,
DbDataLoader, and future implementations) must satisfy. This enables
pluggable data sources for the walk-forward pipeline, SRMR+ runner,
and other backtest infrastructure.

Usage:
    loader: AbstractDataLoader = CsvDataLoader(csv_dir="data/forex/historical")
    bars = loader.load_bars("XAUUSD", "M15")

    loader = DbDataLoader(db_path="data/ayumi_market.duckdb")
    symbols = loader.get_available_symbols()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .types import Bar


class AbstractDataLoader(ABC):
    """Abstract base class for OHLCV bar data loaders.

    Subclasses must implement:
        - :meth:`load_bars` — load OHLCV bars for a symbol/timeframe
        - :meth:`get_available_symbols` — list available symbols
        - :meth:`get_date_range` — return first/last timestamp

    Subclasses may optionally override:
        - :meth:`load_ticks` — default raises ``NotImplementedError``
    """

    @abstractmethod
    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Load OHLCV bars for ``(symbol, timeframe)``.

        Args:
            symbol: Trading pair symbol (e.g. ``"XAUUSD"``, ``"EURUSD"``).
            timeframe: Timeframe string (e.g. ``"M15"``, ``"H1"``, ``"D1"``).
            start: Optional UTC start datetime. ``None`` = no lower bound.
            end: Optional UTC end datetime. ``None`` = no upper bound.

        Returns:
            List of :class:`Bar` objects sorted by time ascending.
        """

    def load_ticks(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Load tick-level data for ``(symbol, timeframe)``.

        Not all data sources support tick data. The default implementation
        raises :class:`NotImplementedError`. Subclasses that support ticks
        should override this.

        Args:
            symbol: Trading pair symbol.
            timeframe: Timeframe string.
            start: Optional UTC start datetime.
            end: Optional UTC end datetime.

        Raises:
            NotImplementedError: If the data source does not support ticks.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support tick data loading")

    @abstractmethod
    def get_available_symbols(self) -> list[str]:
        """Return a sorted list of symbols available in this data source.

        Returns:
            List of symbol strings (e.g. ``["EURUSD", "XAUUSD"]``).
        """

    @abstractmethod
    def get_date_range(
        self,
        symbol: str,
        timeframe: str,
    ) -> tuple[datetime | None, datetime | None]:
        """Return the ``(first, last)`` timestamp for ``(symbol, timeframe)``.

        Args:
            symbol: Trading pair symbol.
            timeframe: Timeframe string.

        Returns:
            Tuple of ``(first_datetime, last_datetime)`` in UTC, or
            ``(None, None)`` when no data is available.
        """
