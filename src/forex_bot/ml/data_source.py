"""Data source loaders for the ML feature pipeline.

Provides CSV and SQLite (forex.db) candle loaders that return identical
pandas DataFrames so the downstream feature pipeline is source-agnostic.
"""

import os
import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB_PATH = Path("data/forex/forex.db")
DB_PATH_ENV_VAR = "FOREX_DB_PATH"

CANDLE_COLUMNS = [
    "symbol",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
]

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _resolve_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env_override = os.environ.get(DB_PATH_ENV_VAR)
    if env_override:
        return Path(env_override)
    return DEFAULT_DB_PATH


class SQLiteCandleLoader:
    """Load candle data from the forex.db SQLite database."""

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = _resolve_db_path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def load_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> pd.DataFrame:
        """Return candles for *symbol* / *timeframe* as a DataFrame.

        The returned DataFrame always contains all ``CANDLE_COLUMNS`` sorted
        by ``timestamp`` ascending.  *start_ts* and *end_ts* are inclusive
        Unix-epoch-second filters.
        """
        query = (
            "SELECT symbol, timeframe, timestamp, open, high, low, close, volume "
            "FROM candles WHERE symbol = ? AND timeframe = ?"
        )
        params: list = [symbol, timeframe]

        if start_ts is not None:
            query += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            query += " AND timestamp <= ?"
            params.append(end_ts)

        query += " ORDER BY timestamp ASC"

        conn = sqlite3.connect(str(self._db_path))
        try:
            df = pd.read_sql_query(query, conn, params=params)
        finally:
            conn.close()

        if df.empty:
            df = pd.DataFrame(columns=CANDLE_COLUMNS)

        for col in CANDLE_COLUMNS:
            if col not in df.columns:
                df[col] = 0.0 if col in OHLCV_COLUMNS + ["timestamp", "volume"] else ""

        df["timestamp"] = df["timestamp"].astype("int64")
        return df

    def list_symbols(self) -> list[str]:
        conn = sqlite3.connect(str(self._db_path))
        try:
            rows = conn.execute("SELECT DISTINCT symbol FROM candles ORDER BY symbol").fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def list_timeframes(self, symbol: str | None = None) -> list[str]:
        query = "SELECT DISTINCT timeframe FROM candles"
        params: list = []
        if symbol is not None:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY timeframe"

        conn = sqlite3.connect(str(self._db_path))
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def to_ohlcv(self, df: pd.DataFrame) -> pd.DataFrame:
        """Convert a candles DataFrame to the OHLCV format expected by the
        feature pipeline (capitalised column names, ``datetime`` index).

        The output columns are: ``Open``, ``High``, ``Low``, ``Close``,
        ``Volume`` with a UTC ``DatetimeIndex`` named ``timestamp``.
        """
        out = pd.DataFrame()
        out["Open"] = df["open"].astype(float)
        out["High"] = df["high"].astype(float)
        out["Low"] = df["low"].astype(float)
        out["Close"] = df["close"].astype(float)
        out["Volume"] = df["volume"].astype(float)
        out.index = pd.to_datetime(df["timestamp"].astype(int), utc=True, unit="s")
        out.index.name = "timestamp"
        return out


class CSVCandleLoader:
    """Load candle data from historical CSV files.

    Matches the format produced by ``data/forex/historical/`` — files named
    ``{SYMBOL}_{TIMEFRAME}.csv`` with columns ``Date``, ``Open``, ``High``,
    ``Low``, ``Close``, ``Volume``.
    """

    def __init__(self, data_dir: str | Path | None = None):
        self._data_dir = Path(data_dir) if data_dir else Path("data/forex/historical")

    def load_candles(
        self,
        symbol: str,
        timeframe: str,
    ) -> pd.DataFrame:
        """Return candles from the matching CSV file as a DataFrame."""
        pattern = f"{symbol.upper()}_{timeframe.upper()}.csv"
        csv_path = self._data_dir / pattern

        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")

        df = pd.read_csv(
            csv_path,
            parse_dates=["Date"] if "Date" in pd.read_csv(csv_path, nrows=0).columns else False,
        )

        if "Date" in df.columns:
            df.rename(columns={"Date": "timestamp"}, inplace=True)
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        for col in OHLCV_COLUMNS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df
