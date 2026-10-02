#!/usr/bin/env python3
"""Bulk-load USDJPY M15 CSV into the `bars` table of `ayumi_market.duckdb`.

The DuckDB `bars` table only contained 480 USDJPY M15 bars (a one-week slice
in June 2024), which caused the walk-forward factory to skip every USDJPY
cell because ``len(bars) < 1000``. The historical CSV at
``data/forex/historical/USDJPY_M15.csv`` contains 78,178 rows from
2023-01-01 to 2026-04-10.

This script:

1. Streams ``data/forex/historical/USDJPY_M15.csv`` row-by-row.
2. Converts the ``Date`` column (interpreted as UTC, matching the Dukascopy
   export convention used for the other FX CSVs) to ``epoch SECONDS``.
3. Bulk-inserts in batches of 5,000 rows via ``INSERT OR REPLACE`` so
   re-running the script is idempotent (existing 480 rows are overwritten
   with their CSV equivalents rather than duplicated).
4. Logs progress every 10,000 rows and a final summary.
5. Verifies the post-load row count and asserts ``>= 78,000``.

Schema written into ``bars`` (matches the existing table):

    symbol        VARCHAR
    timeframe     VARCHAR
    timestamp_utc BIGINT  -- Unix epoch seconds, UTC
    open          DOUBLE
    high          DOUBLE
    low           DOUBLE
    close         DOUBLE
    volume        BIGINT
    spread_pips   DOUBLE  -- always 0.0 for backfilled CSV rows

Usage::

    python3 src/forex_bot/scripts/bulk_load_usdjpy_m15.py
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# File layout: src/forex_bot/scripts/bulk_load_usdjpy_m15.py
# parents[0] -> src/forex_bot/scripts/
# parents[1] -> src/forex_bot/
# parents[2] -> src/
# parents[3] -> repo root (where data/ lives)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CSV = PROJECT_ROOT / "data" / "forex" / "historical" / "USDJPY_M15.csv"
DEFAULT_DB = PROJECT_ROOT / "data" / "ayumi_market.duckdb"

SYMBOL = "USDJPY"
TIMEFRAME = "M15"
SPREAD_PIPS = 0.0

# CSV format: "Date,Open,High,Low,Close,Volume"
# Date is naive but exported in UTC by Dukascopy — see module docstring.
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")

BATCH_SIZE = 5_000
PROGRESS_EVERY = 10_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bulk_load_usdjpy_m15")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_date_to_epoch_seconds(date_str: str) -> int:
    """Parse a CSV date string into a UTC epoch-seconds integer.

    The CSV has no timezone suffix. The Dukascopy convention used by the
    other FX CSVs (e.g. ``EURUSD_M15.csv``) is to emit UTC, so we treat
    naive timestamps as UTC. If a future caller needs a different
    interpretation, this is the single place to change.
    """
    s = (date_str or "").strip()
    if not s:
        raise ValueError("empty Date value")
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        # Naive -> stamp as UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return int(dt.timestamp())
    raise ValueError(f"unrecognised Date format: {date_str!r}")


def _ensure_unique_index(con: duckdb.DuckDBPyConnection) -> None:
    """Ensure a UNIQUE index exists on (symbol, timeframe, timestamp_utc).

    DuckDB's ``INSERT OR REPLACE`` semantics need a UNIQUE constraint or
    PRIMARY KEY on the table. The existing ``bars`` table only ships a
    non-unique lookup index, so we create a unique index here if absent.
    Safe to call repeatedly: ``CREATE UNIQUE INDEX IF NOT EXISTS`` and
    the table has no pre-existing duplicates (verified separately).
    """
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bars_usdjpy_backfill_unique
        ON bars(symbol, timeframe, timestamp_utc)
        """
    )


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------
def load_usdjpy_m15(
    csv_path: Path = DEFAULT_CSV,
    db_path: Path = DEFAULT_DB,
) -> int:
    """Load USDJPY M15 CSV into the DuckDB ``bars`` table.

    Returns the number of rows that were *inserted or replaced* (i.e. the
    number of CSV rows processed; the net delta in the table is smaller
    when pre-existing rows overlap).
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    log.info("Source CSV: %s", csv_path)
    log.info("Target DB:  %s", db_path)

    con = duckdb.connect(str(db_path))
    try:
        _ensure_unique_index(con)

        batch: list[tuple] = []
        inserted_total = 0
        line_no = 0
        skipped = 0

        with csv_path.open("r", newline="") as fh:
            reader = csv.DictReader(fh)
            required = {"Date", "Open", "High", "Low", "Close", "Volume"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSV {csv_path.name} missing required columns: {sorted(missing)}")

            for row in reader:
                line_no += 1
                try:
                    ts = _parse_date_to_epoch_seconds(row["Date"])
                    o = float(row["Open"])
                    h = float(row["High"])
                    l = float(row["Low"])  # noqa: E741
                    c = float(row["Close"])
                    v = int(float(row["Volume"]))
                except (KeyError, ValueError) as exc:
                    skipped += 1
                    log.warning("Skipping malformed row at line %d: %s", line_no, exc)
                    continue

                batch.append((SYMBOL, TIMEFRAME, ts, o, h, l, c, v, SPREAD_PIPS))

                if len(batch) >= BATCH_SIZE:
                    inserted_total += _flush_batch(con, batch)
                    batch.clear()
                    if inserted_total % PROGRESS_EVERY < BATCH_SIZE:
                        log.info("Progress: %d rows committed so far", inserted_total)

            if batch:
                inserted_total += _flush_batch(con, batch)
                batch.clear()

        log.info(
            "CSV processing complete: %d rows committed, %d skipped",
            inserted_total,
            skipped,
        )

        final_count = con.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
            [SYMBOL, TIMEFRAME],
        ).fetchone()[0]
        log.info("Final USDJPY M15 bar count in DB: %d", final_count)

        if final_count < 78_000:
            raise AssertionError(
                f"USDJPY M15 row count {final_count} is below expected 78,000 "
                f"— investigate source CSV or schema before trusting downstream "
                f"walk-forward results."
            )

        return final_count
    finally:
        con.close()


def _flush_batch(
    con: duckdb.DuckDBPyConnection,
    batch: list[tuple],
) -> int:
    """Execute one INSERT OR REPLACE batch. Returns the row count attempted."""
    if not batch:
        return 0
    con.executemany(
        """
        INSERT OR REPLACE INTO bars (
            symbol, timeframe, timestamp_utc,
            open, high, low, close, volume, spread_pips
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )
    return len(batch)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """CLI entry point — keeps argument surface minimal on purpose."""
    # Placeholder for future flags (--csv, --db). Today the paths are
    # derived from __file__ so the script is location-independent.
    return 0 if load_usdjpy_m15() >= 78_000 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
