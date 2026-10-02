"""Bar loader — reads from ayumi_market.duckdb bars table, falls back to CSV.

Architecture:
    1. Check `bars` table in DuckDB (pre-aggregated, permanent)
    2. If not found, check CSV files (legacy)
    3. If neither, return empty

To populate the bars table from tick data:
    python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from core.types import Bar

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TICK_DB = _PROJECT_ROOT / "data" / "ayumi_market.duckdb"
_CSV_DIR = _PROJECT_ROOT / "data" / "forex" / "historical"

# Timeframe to minutes
TF_MINUTES = {
    "M1": 1,
    "M3": 3,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def load_bars_from_db(
    symbol: str,
    timeframe: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[Bar]:
    """Load pre-aggregated bars from the DuckDB bars table.

    This is the primary path — fast, permanent, includes real spread data.
    """
    import duckdb

    tf_upper = timeframe.upper()

    where_clauses = ["symbol = ?", "timeframe = ?"]
    params: list = [symbol, tf_upper]

    if start_ms is not None:
        where_clauses.append("timestamp_utc >= ?")
        params.append(start_ms)
    if end_ms is not None:
        where_clauses.append("timestamp_utc <= ?")
        params.append(end_ms)

    where_sql = " AND ".join(where_clauses)

    query = f"""
        SELECT timestamp_utc, open, high, low, close, volume, spread_pips
        FROM bars
        WHERE {where_sql}
        ORDER BY timestamp_utc
    """  # noqa: S608

    con = duckdb.connect(str(_TICK_DB), read_only=True)
    df = con.execute(query, params).fetchdf()
    con.close()

    if df.empty:
        return []

    bars = []
    for _, row in df.iterrows():
        # DuckDB timestamp_utc is stored in epoch seconds (not milliseconds)
        ts = datetime.fromtimestamp(row["timestamp_utc"], tz=timezone.utc)
        spread = row.get("spread_pips", 0)
        spread = spread if pd.notna(spread) else 0.0

        bar = Bar(
            time=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            spread_pips=float(spread),
        )
        bars.append(bar)

    logger.info(
        "Loaded %d %s bars for %s from DuckDB (avg spread: %.2f pips)",
        len(bars),
        timeframe,
        symbol,
        df["spread_pips"].mean() if "spread_pips" in df.columns else 0,
    )
    return bars


def load_bars_from_csv(symbol: str, timeframe: str) -> list[Bar]:
    """Fallback: load from CSV files (legacy, no spread/volume data)."""
    tf_upper = timeframe.upper()
    csv_path = _CSV_DIR / f"{symbol}_{tf_upper}.csv"

    if not csv_path.exists():
        logger.error("No CSV file for %s %s at %s", symbol, timeframe, csv_path)
        return []

    df = pd.read_csv(csv_path)

    # Normalize column names
    col_map = {}
    for c in df.columns:
        cl = c.lower().strip()
        if cl in ("date", "timestamp", "time", "datetime"):
            col_map[c] = "timestamp"
        elif cl == "open":
            col_map[c] = "open"
        elif cl == "high":
            col_map[c] = "high"
        elif cl == "low":
            col_map[c] = "low"
        elif cl == "close":
            col_map[c] = "close"
        elif cl == "volume":
            col_map[c] = "volume"
    df = df.rename(columns=col_map)

    if "volume" not in df.columns:
        df["volume"] = 0.0

    bars = []
    for _, row in df.iterrows():
        ts = pd.to_datetime(row["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize(timezone.utc)
        else:
            ts = ts.tz_convert(timezone.utc)

        bar = Bar(
            time=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        bars.append(bar)

    logger.warning(
        "Loaded %d bars from CSV for %s %s (no spread/volume data)",
        len(bars),
        symbol,
        timeframe,
    )
    return bars


def load_bars(symbol: str, timeframe: str) -> list[Bar]:
    """Smart loader: DuckDB bars table first, CSV fallback.

    This is the main entry point for all backtest/sweep scripts.

    To add tick data for a symbol:
        python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD
    """
    if _TICK_DB.exists():
        try:
            bars = load_bars_from_db(symbol, timeframe)
            if bars:
                return bars
            logger.info(
                "No %s %s bars in DuckDB, trying CSV",
                symbol,
                timeframe,
            )
        except Exception as e:
            logger.warning("DuckDB load failed (%s), trying CSV", e)

    return load_bars_from_csv(symbol, timeframe)
