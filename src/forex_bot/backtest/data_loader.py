import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pyarrow.parquet as pq
from core.pip import PipCalculator

from .abstract_data_loader import AbstractDataLoader
from .engine import Bar, BarPeriod

logger = logging.getLogger(__name__)

# Phase 10a: default locations for the DB-first loader / CSV fallback. We do
# NOT import ``backtest.db_data_loader`` at module level to avoid the circular
# import (``db_data_loader`` already imports :class:`CsvDataLoader` from this
# module). The functions below perform a lazy import instead.
DEFAULT_DB_PATH = Path("data/ayumi_market.duckdb")
DEFAULT_CSV_DIR = Path("data/forex/historical")

_EASTERN = ZoneInfo("America/New_York")
_UTC = timezone.utc

_ASK_OPEN = "ask_open"
_ASK_CLOSE = "ask_close"
_BID_OPEN = "Open"
_BID_CLOSE = "Close"

_OHLC_COL_MAP = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}


def _find_column(df: pd.DataFrame, name: str) -> str:
    if name in df.columns:
        return name
    lower_map = {c.lower(): c for c in df.columns}
    if name.lower() in lower_map:
        return lower_map[name.lower()]
    return name


def _parse_csv_timestamp(ts_str: str) -> datetime:
    """Parse a CSV timestamp into a UTC ``datetime``.

    Accepts three families of input (in order):

    1. **ISO 8601** with explicit offset or ``Z`` suffix — e.g.
       ``2025-01-09T15:30:00``, ``2025-01-09T15:30:00Z``,
       ``2025-01-09T15:30:00+00:00``. The supplied offset is preserved; the
       result is converted to UTC.
    2. **ISO 8601 naive** — same as above but with no offset. Stamped as
       ``_EASTERN`` (the historical default) then converted to UTC.
    3. **Legacy space-separated** — ``%Y-%m-%d %H:%M:%S`` or
       ``%Y-%m-%d %H:%M``. Stamped as ``_EASTERN`` then converted to UTC.

    Raises ``ValueError`` if none of the above succeed.
    """
    iso_candidate = ts_str.replace("Z", "+00:00") if ts_str else ts_str
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            # Naive ISO 8601 — stamp as Eastern (matches legacy default).
            dt = dt.replace(tzinfo=_EASTERN)
        return dt.astimezone(_UTC)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            dt = dt.replace(tzinfo=_EASTERN)
            return dt.astimezone(_UTC)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts_str}")


def _compute_spread_pips(bid_price: float, ask_price: float) -> float:
    spread_price = abs(ask_price - bid_price)
    return PipCalculator.price_to_pips(bid_price, spread_price)


def _detect_ask_columns(df: pd.DataFrame) -> bool:
    return _ASK_OPEN in df.columns and _ASK_CLOSE in df.columns


class CsvDataLoader(AbstractDataLoader):
    """CSV file-based data loader.

    Implements the :class:`AbstractDataLoader` interface while retaining
    its original file-path-based methods (``load``, ``load_from_string``,
    ``load_parquet``) for backward compatibility.

    The ABC methods (``load_bars``, ``get_available_symbols``,
    ``get_date_range``) operate against a configurable ``csv_dir``
    using the ``{symbol}_{timeframe}.csv`` naming convention.
    """

    # Filename pattern: {SYMBOL}_{TIMEFRAME}[_suffix?].csv
    _FILENAME_RE = re.compile(r"^(?P<symbol>[A-Z]+)_(?P<timeframe>[A-Z0-9]+)(?:_.+)?$")

    def __init__(self, csv_dir: str | Path = DEFAULT_CSV_DIR) -> None:
        self.csv_dir = Path(csv_dir)

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
        """Load bars for ``(symbol, timeframe)`` from ``csv_dir``.

        Uses the ``{symbol}_{timeframe}.csv`` naming convention.
        Optionally filters by ``start`` / ``end`` (UTC datetimes).
        """
        csv_path = self.csv_dir / f"{symbol}_{timeframe}.csv"
        if not csv_path.exists():
            logger.warning(
                "CsvDataLoader.load_bars: no CSV at %s; returning empty list",
                csv_path,
            )
            return []
        bars = self.load(str(csv_path))
        if start is not None:
            bars = [b for b in bars if b.time >= start]
        if end is not None:
            bars = [b for b in bars if b.time <= end]
        return bars

    def get_available_symbols(self) -> list[str]:
        """Scan ``csv_dir`` for ``*.csv`` files and extract unique symbols."""
        symbols: set[str] = set()
        if not self.csv_dir.is_dir():
            return []
        for path in self.csv_dir.glob("*.csv"):
            match = self._FILENAME_RE.match(path.stem)
            if match:
                symbols.add(match.group("symbol"))
        return sorted(symbols)

    def get_date_range(
        self,
        symbol: str,
        timeframe: str,
    ) -> tuple[datetime | None, datetime | None]:
        """Return ``(first, last)`` bar timestamp for ``(symbol, timeframe)``."""
        bars = self.load_bars(symbol, timeframe)
        if not bars:
            return None, None
        return bars[0].time, bars[-1].time

    # ------------------------------------------------------------------ #
    # Original file-based API (backward compatible)
    # ------------------------------------------------------------------ #
    def load(self, filepath: str) -> list[Bar]:
        bars = []
        dropped = 0
        with open(filepath) as f:
            lines = f.readlines()

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 5:
                continue

            try:
                dt = _parse_csv_timestamp(parts[0])
                open_price = float(parts[1])
                high = float(parts[2])
                low = float(parts[3])
                close = float(parts[4])
                volume = float(parts[5]) if len(parts) > 5 else 0.0

                bar = Bar(
                    time=dt,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
                bars.append(bar)
            except (ValueError, IndexError):
                dropped += 1
                continue

        if dropped:
            logger.warning("Dropped %d malformed rows from %s", dropped, filepath)
        return bars

    def load_from_string(self, csv_content: str) -> list[Bar]:
        bars = []
        dropped = 0
        lines = csv_content.strip().split("\n")

        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 5:
                continue

            try:
                dt = _parse_csv_timestamp(parts[0])
                open_price = float(parts[1])
                high = float(parts[2])
                low = float(parts[3])
                close = float(parts[4])
                volume = float(parts[5]) if len(parts) > 5 else 0.0

                bar = Bar(
                    time=dt,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
                bars.append(bar)
            except (ValueError, IndexError):
                dropped += 1
                continue

        if dropped:
            logger.warning("Dropped %d malformed rows from CSV string input", dropped)
        return bars

    @staticmethod
    def _parse_datetime(s: str) -> datetime:
        """Parse a datetime string.

        Accepts ISO 8601 variants (with or without offset) and the legacy
        space-separated formats. Delegates to :func:`_parse_csv_timestamp`
        so both parser copies stay in lock-step.
        """
        return _parse_csv_timestamp(s)

    def infer_timeframe(self, bars: list[Bar]) -> BarPeriod:
        if len(bars) < 2:
            return BarPeriod(60)

        time_diffs = []
        for i in range(1, min(len(bars), 10)):
            diff = (bars[i].time - bars[i - 1].time).total_seconds() / 60
            time_diffs.append(diff)

        avg_diff = sum(time_diffs) / len(time_diffs) if time_diffs else 60

        if avg_diff <= 20:
            return BarPeriod(15)
        elif avg_diff <= 60:
            return BarPeriod(60)
        elif avg_diff <= 300:
            return BarPeriod(240)
        else:
            return BarPeriod(1440)

    def load_parquet(self, filepath: str | Path) -> list[Bar]:
        """Load OHLC(V) bars from a parquet file.

        Supports two parquet formats:
          1. Bid-only: timestamp, Open, High, Low, Close, Volume
          2. Bid+Ask:  timestamp, Open, High, Low, Close, Volume,
                      ask_open, ask_high, ask_low, ask_close

        When ask columns are present, per-bar spread_pips is computed
        from (ask_open - bid_open) using PipCalculator.

        Column names are matched case-insensitively.
        """
        table = pq.read_table(str(filepath))
        df = table.to_pandas(timestamp_as_object=True)
        df = df.reset_index(drop=True)  # Phase 0: fix KeyError 'timestamp' when parquet index is unnamed
        ts_col = _find_column(df, "timestamp")
        timestamps = pd.to_datetime(df[ts_col], utc=True).dt.tz_convert(_UTC)
        has_ask = _detect_ask_columns(df)
        col_open = _find_column(df, "open")
        col_high = _find_column(df, "high")
        col_low = _find_column(df, "low")
        col_close = _find_column(df, "close")
        col_volume = _find_column(df, "volume")
        bars = []
        for i, row in df.iterrows():
            spread = 0.0
            if has_ask:
                bid_open = float(row[col_open])
                ask_open_val = float(row[_ASK_OPEN])
                spread = _compute_spread_pips(bid_open, ask_open_val)
            bar = Bar(
                time=timestamps.iloc[i].to_pydatetime(),
                open=float(row[col_open]),
                high=float(row[col_high]),
                low=float(row[col_low]),
                close=float(row[col_close]),
                volume=float(row.get(col_volume, 0.0)),
                spread_pips=spread,
            )
            bars.append(bar)
        return bars


# ──────────────────────────────────────────────────────────────────────
# Phase 10a — DB-first loaders with CSV fallback
#
# These functions expose the same ``list[Bar]`` contract as
# ``CsvDataLoader.load`` but prefer DuckDB (when present and populated) for
# the source of truth. They preserve the historical ``_2026.csv`` OOS holdout
# convention via the ``is_holdout`` boolean column populated by the Phase 8b
# CSV migration (``is_holdout = true`` for the 2023+ bars that used to live in
# ``{symbol}_{timeframe}_2026.csv`` files).
#
# Design notes:
#   * We deliberately avoid a top-level ``from backtest.db_data_loader``
#     import — ``db_data_loader`` already imports ``CsvDataLoader`` from this
#     module, so a module-level import would create a cycle. ``DbDataLoader``
#     is resolved lazily inside each function instead.
#   * ``load_holdout`` / ``load_training`` are the DB-backed replacements for
#     the implicit ``{symbol}_{timeframe}_2026.csv`` / ``{symbol}_{timeframe}.csv``
#     convention that the ML scripts use today. ML scripts can adopt them
#     without touching the ``CsvDataLoader`` call sites.
# ──────────────────────────────────────────────────────────────────────


def _resolve_paths(
    db_path: str | Path | None,
    csv_dir: str | Path | None,
) -> tuple[Path, Path]:
    """Return ``(db_path, csv_dir)`` with defaults applied."""
    db_p = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    csv_d = Path(csv_dir) if csv_dir is not None else DEFAULT_CSV_DIR
    return db_p, csv_d


def _query_bars_db(
    symbol: str,
    timeframe: str,
    is_holdout: bool | None,
    db_path: Path,
) -> pd.DataFrame:
    """Query the bars table for ``(symbol, timeframe)`` with an optional
    ``is_holdout`` filter.

    Returns an empty DataFrame when the DB file is missing, the query raises,
    or the result set is empty. We intentionally swallow DuckDB errors here —
    callers fall back to CSV on empty result, which keeps the DB optional.
    """
    if not db_path.exists():
        logger.debug("_query_bars_db: DB not found at %s", db_path)
        return pd.DataFrame()

    sql = (
        "SELECT timestamp_utc, open, high, low, close, volume, spread_pips FROM bars WHERE symbol = ? AND timeframe = ?"
    )
    params: list = [symbol, timeframe]
    if is_holdout is not None:
        sql += " AND is_holdout = ?"
        params.append(bool(is_holdout))
    sql += " ORDER BY timestamp_utc"

    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            df = con.execute(sql, params).fetch_df()
        finally:
            con.close()
    except duckdb.Error as exc:
        logger.warning(
            "_query_bars_db: DuckDB error for %s/%s (%s); falling back to CSV",
            symbol,
            timeframe,
            exc,
        )
        return pd.DataFrame()

    if df is None:
        return pd.DataFrame()
    return df


def _bars_from_dataframe(df: pd.DataFrame) -> list[Bar]:
    """Convert a bars-table DataFrame into ``list[Bar]`` with UTC tz-aware
    timestamps. Mirrors the conversion in ``db_data_loader._df_to_bars`` but
    is duplicated here to keep the new functions self-contained without
    crossing the ``data_loader`` / ``db_data_loader`` import boundary.
    """
    if df.empty:
        return []
    timestamps = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    volumes = df["volume"].astype("float64")
    spreads = df["spread_pips"].astype("float64")
    bars: list[Bar] = []
    for ts, o, h, lo, c, v, s in zip(  # noqa: B905
        timestamps, opens, highs, lows, closes, volumes, spreads
    ):
        bars.append(
            Bar(
                time=ts.to_pydatetime(),
                open=float(o),
                high=float(h),
                low=float(lo),
                close=float(c),
                # NaN guard — DuckDB can return NaN for missing spread/volume.
                volume=float(v) if v == v else 0.0,
                spread_pips=float(s) if s == s else 0.0,
            )
        )
    return bars


def _csv_fallback(
    symbol: str,
    timeframe: str,
    csv_path: Path,
    *,
    context: str,
) -> list[Bar]:
    """Load ``csv_path`` via :class:`CsvDataLoader` if present.

    ``context`` is a short tag used in the log line (``"load_data"``,
    ``"load_holdout"``, ``"load_training"``) so operators can see which
    high-level API requested the fallback.
    """
    if not csv_path.exists():
        logger.warning(
            "%s: DB miss and no CSV at %s; returning empty list",
            context,
            csv_path,
        )
        return []
    logger.info(
        "%s: CsvDataLoader fallback for %s/%s, file=%s",
        context,
        symbol,
        timeframe,
        csv_path,
    )
    return CsvDataLoader().load(str(csv_path))


def _log_db_hit(
    fn_name: str,
    symbol: str,
    timeframe: str,
    db_path: Path,
    n_rows: int,
    *,
    is_holdout: bool | None = None,
) -> None:
    """Emit the standard ``DbDataLoader used`` log line."""
    if is_holdout is None:
        filter_label = "all"
    else:
        filter_label = "holdout" if is_holdout else "training"
    logger.info(
        "%s: DuckDB hit (%s) for %s/%s, rows=%d, is_holdout=%s",
        fn_name,
        db_path,
        symbol,
        timeframe,
        n_rows,
        filter_label,
    )


def load_data(
    symbol: str,
    timeframe: str,
    *,
    db_path: str | Path | None = None,
    csv_dir: str | Path | None = None,
) -> list[Bar]:
    """Load all bars for ``(symbol, timeframe)`` with DB-first, CSV fallback.

    Returns the full bars table rows (both training and holdout) — i.e. no
    ``is_holdout`` filter. When ``data/ayumi_market.duckdb`` exists and
    contains rows for the requested pair/timeframe, the DB result is
    returned. Otherwise this falls back to
    ``data/forex/historical/{symbol}_{timeframe}.csv`` via
    :class:`CsvDataLoader`. Logs which loader was used.
    """
    db_p, csv_d = _resolve_paths(db_path, csv_dir)
    df = _query_bars_db(symbol, timeframe, None, db_p)
    if not df.empty:
        _log_db_hit("load_data", symbol, timeframe, db_p, len(df))
        return _bars_from_dataframe(df)
    return _csv_fallback(
        symbol,
        timeframe,
        csv_d / f"{symbol}_{timeframe}.csv",
        context="load_data",
    )


def load_holdout(
    symbol: str,
    timeframe: str,
    *,
    db_path: str | Path | None = None,
    csv_dir: str | Path | None = None,
) -> list[Bar]:
    """Load only the OOS holdout bars (``is_holdout = true``).

    This is the DB-backed equivalent of the historical
    ``{symbol}_{timeframe}_2026.csv`` convention that the ML scripts use
    to identify out-of-sample data. The DB stores ``is_holdout = true`` for
    every bar that used to live in a ``_2026.csv`` file (the Phase 8b CSV
    migration populated this column at import time).

    CSV fallback order:
      1. ``{symbol}_{timeframe}_2026.csv`` (canonical holdout file)
      2. ``{symbol}_{timeframe}.csv`` (no holdout suffix — only used when
         the DB is empty *and* the ``_2026.csv`` variant is also missing;
         the caller almost certainly wants the DB or the suffixed file).

    Logs which loader was used.
    """
    db_p, csv_d = _resolve_paths(db_path, csv_dir)
    df = _query_bars_db(symbol, timeframe, True, db_p)
    if not df.empty:
        _log_db_hit("load_holdout", symbol, timeframe, db_p, len(df), is_holdout=True)
        return _bars_from_dataframe(df)
    holdout_csv = csv_d / f"{symbol}_{timeframe}_2026.csv"
    if holdout_csv.exists():
        return _csv_fallback(symbol, timeframe, holdout_csv, context="load_holdout")
    # Last resort: fall back to the unsuffixed CSV. We still log a warning so
    # the operator can tell the difference between a real _2026.csv fallback
    # and a "we grabbed whatever we had" fallback.
    fallback_csv = csv_d / f"{symbol}_{timeframe}.csv"
    if fallback_csv.exists():
        logger.warning(
            "load_holdout: no _2026.csv for %s/%s; using unsuffixed %s "
            "(caller almost certainly wanted the holdout slice — verify data)",
            symbol,
            timeframe,
            fallback_csv,
        )
        return CsvDataLoader().load(str(fallback_csv))
    logger.warning(
        "load_holdout: DB miss and no CSV (held or unsuffixed) for %s/%s; returning empty list",
        symbol,
        timeframe,
    )
    return []


def load_training(
    symbol: str,
    timeframe: str,
    *,
    db_path: str | Path | None = None,
    csv_dir: str | Path | None = None,
) -> list[Bar]:
    """Load only the training bars (``is_holdout = false``).

    This is the DB-backed equivalent of the historical
    ``{symbol}_{timeframe}.csv`` convention (the unsuffixed filename the ML
    scripts use for in-sample training data).

    CSV fallback: ``{symbol}_{timeframe}.csv`` via :class:`CsvDataLoader`.
    Logs which loader was used.
    """
    db_p, csv_d = _resolve_paths(db_path, csv_dir)
    df = _query_bars_db(symbol, timeframe, False, db_p)
    if not df.empty:
        _log_db_hit("load_training", symbol, timeframe, db_p, len(df), is_holdout=False)
        return _bars_from_dataframe(df)
    return _csv_fallback(
        symbol,
        timeframe,
        csv_d / f"{symbol}_{timeframe}.csv",
        context="load_training",
    )
