"""DuckDB-backed data loader for OHLCV bar history.

Phase 8c of the Ayumi market-data analytics roadmap. ``DbDataLoader`` exposes
the same ``load(symbol, timeframe, ...) -> list[Bar]`` contract used by the
existing ``CsvDataLoader`` while reading rows from ``data/ayumi_market.duckdb``.

Behaviour notes
---------------
* ``bars`` rows are stored as ``timestamp_utc`` (Unix epoch seconds, UTC),
  so we convert them back to a tz-aware UTC ``datetime`` to match what
  ``CsvDataLoader.load()`` produces (``datetime.astimezone(UTC)``).
* ``volume`` is stored as ``BIGINT`` in DuckDB; we coerce to ``float`` to match
  the ``Bar`` dataclass which declares ``volume: float``.
* ``spread_pips`` defaults to ``0.0`` when the source migration left the
  column NULL (i.e. the original CSV had no ``ask_open`` column).
* When the database file is missing *or* a query returns zero rows we fall
  back to ``CsvDataLoader`` on the conventional
  ``data/forex/historical/{symbol}_{timeframe}.csv`` filename. This keeps
  the drop-in replacement safe during rollout.

Drop-in usage
-------------
    loader = DbDataLoader()
    bars = loader.load_by_filepath("data/forex/historical/XAUUSD_M15.csv")
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
from backtest.abstract_data_loader import AbstractDataLoader
from backtest.data_loader import CsvDataLoader
from backtest.engine import Bar

logger = logging.getLogger(__name__)

_UTC = timezone.utc

# Default locations — overridable per-instance or via env-style kwargs.
DEFAULT_DB_PATH = Path("data/ayumi_market.duckdb")
DEFAULT_CSV_DIR = Path("data/forex/historical")

# Filename pattern used by the historical CSV layout, e.g. ``XAUUSD_M15.csv``
# or ``EURUSD_M15_2026.csv``. We anchor on the symbol and timeframe at the
# start of the stem so suffixes like ``_fresh`` / ``_2026`` are ignored.
_FILENAME_RE = re.compile(r"^(?P<symbol>[A-Z]+)_(?P<timeframe>[A-Z0-9]+)(?:_.+)?$")

# Column ordering used when we synthesize an empty DataFrame on error.
_RESULT_COLUMNS = [
    "timestamp_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "spread_pips",
]


class DbDataLoader(AbstractDataLoader):
    """DuckDB-backed loader with a CSV fallback for forward compatibility."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        csv_dir: str | Path = DEFAULT_CSV_DIR,
        csv_loader: CsvDataLoader | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.csv_dir = Path(csv_dir)
        self._csv_loader = csv_loader or CsvDataLoader(csv_dir=self.csv_dir)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def load(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[Bar]:
        """Return ``Bar`` objects for ``symbol``/``timeframe``.

        Parameters
        ----------
        symbol : str
            e.g. ``"XAUUSD"``
        timeframe : str
            e.g. ``"M15"``, ``"H1"``, ``"D1"``
        start_ts, end_ts : int | None
            Optional Unix-epoch-seconds (UTC) range filter. ``None`` ⇒ no
            bound on that side. Both bounds are inclusive.
        """
        if not self._db_available():
            return self._fallback_to_csv(symbol, timeframe)

        df = self._query_rows(symbol, timeframe, start_ts, end_ts)
        if df.empty:
            return self._fallback_to_csv(symbol, timeframe)

        return self._df_to_bars(df)

    def load_by_filepath(self, filepath: str | Path) -> list[Bar]:
        """Drop-in replacement for ``CsvDataLoader.load(filepath)``.

        Parses ``{symbol}_{timeframe}.csv`` from the filename (suffixes after
        the timeframe are ignored) and delegates to :meth:`load`.
        """
        symbol, timeframe = self._parse_filename(filepath)
        return self.load(symbol, timeframe)

    # ------------------------------------------------------------------ #
    # AbstractDataLoader interface
    # ------------------------------------------------------------------ #
    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Load bars for ``(symbol, timeframe)``.

        Delegates to :meth:`load`, converting ``start``/``end`` datetimes
        to Unix epoch seconds for the DB query.
        """
        start_ts = int(start.timestamp()) if start is not None else None
        end_ts = int(end.timestamp()) if end is not None else None
        return self.load(symbol, timeframe, start_ts=start_ts, end_ts=end_ts)

    def get_available_symbols(self) -> list[str]:
        """Return distinct symbols from the ``bars`` table.

        Falls back to scanning the CSV directory when the DB is unavailable.
        """
        if not self._db_available():
            return self._csv_loader.get_available_symbols()
        try:
            con = duckdb.connect(str(self.db_path), read_only=True)
            try:
                df = con.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol").fetch_df()
            finally:
                con.close()
        except duckdb.Error as exc:
            logger.warning(
                "DbDataLoader.get_available_symbols: DuckDB error (%s); falling back to CSV scanner",
                exc,
            )
            return self._csv_loader.get_available_symbols()
        if df is None or df.empty:
            return []
        return df["symbol"].tolist()

    def get_date_range(
        self,
        symbol: str,
        timeframe: str,
    ) -> tuple[datetime | None, datetime | None]:
        """Return ``(first, last)`` UTC timestamp for ``(symbol, timeframe)``."""
        if not self._db_available():
            return self._csv_loader.get_date_range(symbol, timeframe)
        try:
            con = duckdb.connect(str(self.db_path), read_only=True)
            try:
                df = con.execute(
                    "SELECT MIN(timestamp_utc) AS first_ts, "
                    "MAX(timestamp_utc) AS last_ts "
                    "FROM bars WHERE symbol = ? AND timeframe = ?",
                    [symbol, timeframe],
                ).fetch_df()
            finally:
                con.close()
        except duckdb.Error as exc:
            logger.warning(
                "DbDataLoader.get_date_range: DuckDB error (%s); falling back to CSV",
                exc,
            )
            return self._csv_loader.get_date_range(symbol, timeframe)
        if df is None or df.empty:
            return None, None
        first_ts = df["first_ts"].iloc[0]
        last_ts = df["last_ts"].iloc[0]
        if first_ts is None or last_ts is None:
            return None, None
        return (
            datetime.fromtimestamp(int(first_ts), tz=timezone.utc),
            datetime.fromtimestamp(int(last_ts), tz=timezone.utc),
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _db_available(self) -> bool:
        if not self.db_path.exists():
            logger.debug("DbDataLoader: DB not found at %s", self.db_path)
            return False
        return True

    def _query_rows(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int | None,
        end_ts: int | None,
    ) -> pd.DataFrame:
        """Query DuckDB and return rows as a DataFrame.

        Uses ``fetch_df()`` (Pandas-backed) rather than ``fetchall()`` because
        large windows (e.g. XAUUSD M15 ≈ 86k rows) overflow the stdlib tuple
        allocator in some DuckDB builds. Pandas handles the same result set
        with materially less pressure.
        """
        sql = (
            "SELECT timestamp_utc, open, high, low, close, volume, spread_pips "
            "FROM bars "
            "WHERE symbol = ? AND timeframe = ? "
        )
        params: list = [symbol, timeframe]
        if start_ts is not None:
            sql += "AND timestamp_utc >= ? "
            params.append(int(start_ts))
        if end_ts is not None:
            sql += "AND timestamp_utc <= ? "
            params.append(int(end_ts))
        sql += "ORDER BY timestamp_utc"

        try:
            con = duckdb.connect(str(self.db_path), read_only=True)
            try:
                df = con.execute(sql, params).fetch_df()
            finally:
                con.close()
        except duckdb.Error as exc:
            logger.warning(
                "DbDataLoader: DuckDB error querying %s/%s (%s); falling back to CSV",
                symbol,
                timeframe,
                exc,
            )
            return pd.DataFrame(columns=_RESULT_COLUMNS)

        if df is None:
            return pd.DataFrame(columns=_RESULT_COLUMNS)
        return df

    def _df_to_bars(self, df: pd.DataFrame) -> list[Bar]:
        """Convert a DuckDB result DataFrame into ``list[Bar]``.

        Vectorised where possible: the timestamp conversion is done once via
        ``pd.to_datetime`` with unit ``s`` and ``utc=True`` rather than per-
        row ``datetime.fromtimestamp`` calls.
        """
        if df.empty:
            return []
        timestamps = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
        volumes = df["volume"].astype("float64")
        spread = df["spread_pips"].astype("float64")
        bars: list[Bar] = []
        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        for ts, o, h, lo, c, v, s in zip(  # noqa: B905
            timestamps,
            opens,
            highs,
            lows,
            closes,
            volumes,
            spread,
        ):
            bars.append(
                Bar(
                    time=ts.to_pydatetime(),
                    open=float(o),
                    high=float(h),
                    low=float(lo),
                    close=float(c),
                    volume=float(v) if v == v else 0.0,  # NaN guard
                    spread_pips=float(s) if s == s else 0.0,
                )
            )
        return bars

    @staticmethod
    def _row_to_bar(
        ts_utc: int,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int | float | None,
        spread: float | None,
    ) -> Bar:
        """Build a single ``Bar`` from primitive column values.

        Retained as a public-ish helper for unit tests and for callers that
        don't want to round-trip a DataFrame through ``_df_to_bars``.
        """
        dt = datetime.fromtimestamp(int(ts_utc), tz=_UTC)
        return Bar(
            time=dt,
            open=float(open_),
            high=float(high),
            low=float(low),
            close=float(close),
            volume=float(volume) if volume is not None else 0.0,
            spread_pips=float(spread) if spread is not None else 0.0,
        )

    def _fallback_to_csv(self, symbol: str, timeframe: str) -> list[Bar]:
        csv_path = self.csv_dir / f"{symbol}_{timeframe}.csv"
        if not csv_path.exists():
            logger.warning(
                "DbDataLoader: DB miss and no CSV at %s; returning empty list",
                csv_path,
            )
            return []
        logger.warning(
            "DbDataLoader: DB miss for %s/%s, falling back to CSV (%s)",
            symbol,
            timeframe,
            csv_path,
        )
        return self._csv_loader.load(str(csv_path))

    @staticmethod
    def _parse_filename(filepath: str | Path) -> tuple[str, str]:
        """Extract ``(symbol, timeframe)`` from ``{symbol}_{timeframe}.csv``.

        Raises ``ValueError`` when the filename doesn't match the expected
        pattern so callers can detect bad paths instead of getting a silent
        empty list.
        """
        stem = Path(filepath).stem
        match = _FILENAME_RE.match(stem)
        if not match:
            raise ValueError(f"Cannot parse symbol/timeframe from filename: {filepath!r}")
        return match.group("symbol"), match.group("timeframe")
