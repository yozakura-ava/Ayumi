#!/usr/bin/env python3
"""Import tick CSV files from the Dukascopy harvester into DuckDB.

Reads CSVs from a staging directory, bulk-inserts into the ticks table,
then deletes the source CSVs. Also generates M1/M5/M15/M30/H1/H4/D1 bars
from the imported ticks.

Usage:
    python3 scripts/import_ticks.py [--staging tools/dukascopy-harvester/output]
                                    [--db-path data/ayumi_market.duckdb]
                                    [--keep-csvs]
                                    [--generate-bars]
                                    [--timeframes M1,M5,M15,M30,H1,H4,D1]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

DEFAULT_STAGING = Path("tools/dukascopy-harvester/output")
DEFAULT_DB = Path("data/ayumi_market.duckdb")

# Timeframe → seconds (for time bucketing)
TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


def import_csv(con: duckdb.DuckDBPyConnection, csv_path: Path, keep_csv: bool) -> int:
    """Import a single CSV into ticks table. Returns row count inserted."""
    filename = csv_path.name

    # Check if already imported
    # NOTE: 5ad1bfc3 renamed the legacy 4-col `import_log` to
    # `import_log_legacy_v4col` (canonical 11-col is owned by
    # `import_ctrader_bars.py`). Tick imports stay on the legacy shape —
    # dedup key is filename. Redirects here + the matching INSERT below.
    existing = con.execute(
        "SELECT row_count FROM import_log_legacy_v4col WHERE filename = ?",
        [filename],
    ).fetchone()
    if existing:
        print(f"  ⏭️  {filename} already imported ({existing[0]} rows), skipping")
        return 0

    # Determine symbol from filename (e.g. GBPUSD_20240603.csv → GBPUSD)
    symbol = filename.split("_")[0]

    # Skip M1 fallback files (weekend bar data, not tick data)
    if "_M1.csv" in filename or "_M1_" in filename:
        print(f"  ⏭️  {filename}: M1 bar fallback (not tick data), skipping")
        return 0

    # Bulk insert via DuckDB's read_csv_auto — orders of magnitude faster than row-by-row
    # CSV format: timestamp,instrument,bid,ask,bidVol,askVol
    temp_table = f"temp_import_{symbol}_{csv_path.stem}"

    con.execute(f"""
        CREATE TEMPORARY TABLE {temp_table} AS
        SELECT
            CAST(timestamp AS BIGINT) AS timestamp_ms,
            '{symbol}' AS symbol,
            CAST(bid AS DOUBLE) AS bid,
            CAST(ask AS DOUBLE) AS ask,
            COALESCE(CAST(bidVol AS DOUBLE), 0.0) AS bid_vol,
            COALESCE(CAST(askVol AS DOUBLE), 0.0) AS ask_vol
        FROM read_csv_auto('{csv_path}')
    """)  # noqa: S608

    # Insert into ticks, ignoring duplicates
    _result = con.execute(f"""
        INSERT OR IGNORE INTO ticks (timestamp_ms, symbol, bid, ask, bid_vol, ask_vol)
        SELECT timestamp_ms, symbol, bid, ask, bid_vol, ask_vol
        FROM {temp_table}
    """)  # noqa: S608

    # DuckDB doesn't return row count from INSERT directly; count the temp table
    count = con.execute(f"SELECT COUNT(*) FROM {temp_table}").fetchone()[0]  # noqa: S608

    con.execute(f"DROP TABLE {temp_table}")

    # Log the import (4-col legacy shape lives in import_log_legacy_v4col
    # post-5ad1bfc3; canonical 11-col import_log is owned by
    # import_ctrader_bars.py and uses a different shape).
    con.execute(
        "INSERT INTO import_log_legacy_v4col (filename, symbol, row_count, imported_at) VALUES (?, ?, ?, ?)",
        [filename, symbol, count, datetime.now(timezone.utc).isoformat()],
    )

    if not keep_csv:
        csv_path.unlink()
        print(f"  ✅ {filename}: {count:,} ticks imported, CSV deleted")
    else:
        print(f"  ✅ {filename}: {count:,} ticks imported")

    return count


def generate_bars(
    con: duckdb.DuckDBPyConnection,
    symbols: list[str],
    timeframes: list[str],
    replace: bool = True,
) -> None:
    """Generate OHLCV bars from ticks for each symbol/timeframe."""
    for symbol in symbols:
        tick_range = con.execute(
            "SELECT MIN(timestamp_ms), MAX(timestamp_ms) FROM ticks WHERE symbol = ?",
            [symbol],
        ).fetchone()

        if tick_range[0] is None:
            print(f"  ⚠️  No ticks for {symbol}, skipping bar generation")
            continue

        for tf in timeframes:
            secs = TF_SECONDS[tf]

            if replace:
                con.execute(
                    "DELETE FROM bars WHERE symbol = ? AND timeframe = ?",
                    [symbol, tf],
                )

            # Aggregate ticks into OHLCV bars
            # timestamp_ms is in milliseconds; bucket to get bar start time in seconds
            bucket_ms = secs * 1000

            con.execute(f"""
                INSERT INTO bars (symbol, timeframe, timestamp_utc, open, high, low, close, volume, spread_pips)
                SELECT
                    symbol,
                    '{tf}' AS timeframe,
                    CAST(FLOOR(timestamp_ms / {bucket_ms}) AS BIGINT) * {secs} AS timestamp_utc,
                    FIRST(bid ORDER BY timestamp_ms) AS open,
                    MAX(bid) AS high,
                    MIN(bid) AS low,
                    LAST(bid ORDER BY timestamp_ms) AS close,
                    COUNT(*) AS volume,
                    AVG(ask - bid) AS spread_pips
                FROM ticks
                WHERE symbol = '{symbol}'
                GROUP BY symbol, CAST(FLOOR(timestamp_ms / {bucket_ms}) AS BIGINT) * {secs}
                ORDER BY timestamp_utc
            """)  # noqa: S608

            bar_count = con.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
                [symbol, tf],
            ).fetchone()[0]
            print(f"  📊 {symbol} {tf}: {bar_count:,} bars generated")


def main():
    ap = argparse.ArgumentParser(description="Import tick CSVs into DuckDB")
    ap.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    ap.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    ap.add_argument("--keep-csvs", action="store_true", help="Don't delete CSVs after import")
    ap.add_argument("--generate-bars", action="store_true", help="Generate OHLCV bars from ticks")
    ap.add_argument(
        "--timeframes",
        type=str,
        default="M1,M5,M15,M30,H1,H4,D1",
        help="Comma-separated timeframes for bar generation",
    )
    args = ap.parse_args()

    if not args.db_path.exists():
        print(f"❌ Database not found at {args.db_path}. Run init_tick_db.py first.")
        sys.exit(1)

    if not args.staging.exists():
        print(f"❌ Staging directory not found: {args.staging}")
        sys.exit(1)

    csvs = sorted(args.staging.glob("*.csv"))
    if not csvs:
        print(f"❌ No CSV files found in {args.staging}")
        sys.exit(1)

    con = duckdb.connect(str(args.db_path))

    total = 0
    for csv in csvs:
        total += import_csv(con, csv, args.keep_csvs)

    print(f"\n Total: {total:,} ticks imported")

    if args.generate_bars:
        symbols = sorted(set(c.name.split("_")[0] for c in csvs))
        timeframes = args.timeframes.split(",")
        print("\nGenerating bars...")
        generate_bars(con, symbols, timeframes)

    con.close()
    print("\n✅ Done")


if __name__ == "__main__":
    main()
