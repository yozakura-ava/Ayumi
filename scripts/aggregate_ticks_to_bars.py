#!/usr/bin/env python3
"""Aggregate raw tick data into OHLCV bars and store in ayumi_market.duckdb.

Once aggregated, bars are permanent — subsequent loads read from the bars table
directly with no re-processing of ticks.

Usage:
    # Aggregate all available timeframes for a symbol
    python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD

    # Specific timeframes only
    python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD --timeframes M5,M15,H1

    # Force re-aggregation (delete + rebuild)
    python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD --force

    # List what's already aggregated
    python3 scripts/aggregate_ticks_to_bars.py --list
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "ayumi_market.duckdb"

# Timeframe definitions: (name, milliseconds)
TIMEFRAMES = {
    "M1": 60_000,
    "M3": 180_000,
    "M5": 300_000,
    "M15": 900_000,
    "M30": 1_800_000,
    "H1": 3_600_000,
    "H4": 14_400_000,
    "D1": 86_400_000,
}


# Pip values per symbol family for spread calculation
def _pip_size(symbol: str) -> float:
    """Return pip size for spread_pips calculation."""
    if "XAU" in symbol or "XAG" in symbol:
        return 0.01  # gold/silver: 1 pip = 0.01
    if "JPY" in symbol:
        return 0.01  # JPY pairs: 1 pip = 0.01
    return 0.0001  # standard FX: 1 pip = 0.0001


def get_available_symbols(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Get all symbols that have tick data."""
    return [r[0] for r in con.execute("SELECT DISTINCT symbol FROM ticks ORDER BY symbol").fetchall()]


def get_aggregated(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """Get (symbol, timeframe) pairs already in bars table."""
    return [
        (r[0], r[1])
        for r in con.execute("SELECT DISTINCT symbol, timeframe FROM bars ORDER BY symbol, timeframe").fetchall()
    ]


def check_tick_coverage(con: duckdb.DuckDBPyConnection, symbol: str) -> dict:
    """Get tick data coverage for a symbol."""
    row = con.execute(
        """
        SELECT count(*), min(timestamp_ms), max(timestamp_ms)
        FROM ticks WHERE symbol = ?
    """,
        [symbol],
    ).fetchone()
    if not row or row[0] == 0:
        return {"count": 0, "earliest": None, "latest": None}
    return {"count": row[0], "earliest": row[1], "latest": row[2]}


def aggregate_symbol_timeframe(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    force: bool = False,
) -> int:
    """Aggregate ticks into bars for one symbol/timeframe pair.

    Returns number of bars created.
    """
    tf_ms = TIMEFRAMES[timeframe]
    pip = _pip_size(symbol)

    # Check if already done
    existing = con.execute(
        "SELECT count(*) FROM bars WHERE symbol = ? AND timeframe = ?",
        [symbol, timeframe],
    ).fetchone()[0]

    if existing > 0 and not force:
        logger.info(
            "  %s %s: already aggregated (%d bars), skipping",
            symbol,
            timeframe,
            existing,
        )
        return existing

    if existing > 0 and force:
        logger.info(
            "  %s %s: force=True, deleting %d existing bars",
            symbol,
            timeframe,
            existing,
        )
        con.execute(
            "DELETE FROM bars WHERE symbol = ? AND timeframe = ?",
            [symbol, timeframe],
        )

    # Check tick coverage
    coverage = check_tick_coverage(con, symbol)
    if coverage["count"] == 0:
        logger.warning("  %s %s: no tick data available, skipping", symbol, timeframe)
        return 0

    logger.info("  %s %s: aggregating from %d ticks...", symbol, timeframe, coverage["count"])

    start = time.monotonic()

    # Single SQL query: aggregate ticks into OHLCV bars
    # Uses midprice = (bid + ask) / 2 for OHLC
    # spread = avg(ask - bid) in pips
    query = f"""
        WITH ranked AS (
            SELECT
                bid, ask, timestamp_ms,
                floor(timestamp_ms / {tf_ms}) * {tf_ms} AS bar_ts,
                row_number() OVER (
                    PARTITION BY floor(timestamp_ms / {tf_ms}) * {tf_ms}
                    ORDER BY timestamp_ms
                ) AS rn_first,
                row_number() OVER (
                    PARTITION BY floor(timestamp_ms / {tf_ms}) * {tf_ms}
                    ORDER BY timestamp_ms DESC
                ) AS rn_last
            FROM ticks
            WHERE symbol = ?
        )
        INSERT INTO bars (symbol, timeframe, timestamp_utc, open, high, low, close, volume, spread_pips)
        SELECT
            ? AS symbol,
            ? AS timeframe,
            (bar_ts / 1000) AS timestamp_utc,
            MAX(CASE WHEN rn_first = 1 THEN (bid + ask) / 2.0 END) AS open,
            MAX(ask) AS high,
            MIN(bid) AS low,
            MAX(CASE WHEN rn_last = 1 THEN (bid + ask) / 2.0 END) AS close,
            COUNT(*) AS volume,
            AVG((ask - bid) / {pip}) AS spread_pips
        FROM ranked
        GROUP BY bar_ts
        ORDER BY bar_ts
    """  # noqa: S608

    con.execute(query, [symbol, symbol, timeframe])

    elapsed = time.monotonic() - start

    # Count what we created
    created = con.execute(
        "SELECT count(*) FROM bars WHERE symbol = ? AND timeframe = ?",
        [symbol, timeframe],
    ).fetchone()[0]

    # Log to import_log_legacy_v4col (4-col legacy shape preserved by
    # 5ad1bfc3; canonical 11-col import_log is owned by
    # import_ctrader_bars.py and uses a different shape — see card
    # 0721ec62 rationale in workboard notes).
    con.execute(
        "INSERT INTO import_log_legacy_v4col (filename, symbol, row_count, imported_at) VALUES (?, ?, ?, ?)",
        [
            f"tick_aggregation:{symbol}:{timeframe}",
            symbol,
            created,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ],
    )

    logger.info(
        "  %s %s: created %d bars in %.1fs (%.0f ticks/bar avg)",
        symbol,
        timeframe,
        created,
        elapsed,
        coverage["count"] / max(created, 1),
    )

    return created


def aggregate_symbol(
    symbol: str,
    timeframes: list[str] | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Aggregate all timeframes for a symbol.

    Returns dict of {timeframe: bar_count}.
    """
    con = duckdb.connect(str(DB_PATH))

    try:
        # Check symbol exists in ticks
        coverage = check_tick_coverage(con, symbol)
        if coverage["count"] == 0:
            logger.error("No tick data for %s in %s", symbol, DB_PATH)
            return {}

        logger.info(
            "Aggregating %s: %d ticks (%s to %s)",
            symbol,
            coverage["count"],
            time.strftime("%Y-%m-%d", time.gmtime(coverage["earliest"] / 1000)),
            time.strftime("%Y-%m-%d", time.gmtime(coverage["latest"] / 1000)),
        )

        tfs = timeframes or list(TIMEFRAMES.keys())
        results = {}

        for tf in tfs:
            if tf not in TIMEFRAMES:
                logger.warning("Unknown timeframe %s, skipping", tf)
                continue
            count = aggregate_symbol_timeframe(con, symbol, tf, force=force)
            results[tf] = count

        # Create index for fast lookups (if not exists)
        con.execute("CREATE INDEX IF NOT EXISTS idx_bars_symbol_tf ON bars (symbol, timeframe, timestamp_utc)")

        return results

    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description="Aggregate ticks to bars in DuckDB")
    parser.add_argument("--symbol", help="Symbol to aggregate (e.g., GBPUSD)")
    parser.add_argument(
        "--timeframes",
        default="M1,M5,M15,M30,H1,H4,D1",
        help="Comma-separated timeframes to aggregate",
    )
    parser.add_argument("--force", action="store_true", help="Re-aggregate even if bars exist")
    parser.add_argument("--list", action="store_true", help="List aggregated bars and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list:
        con = duckdb.connect(str(DB_PATH), read_only=True)
        print("\n=== Aggregated Bars ===")
        df = con.execute("""
            SELECT symbol, timeframe, count(*) as bars,
                   min(timestamp_utc) as earliest_s,
                   max(timestamp_utc) as latest_s
            FROM bars
            GROUP BY symbol, timeframe
            ORDER BY symbol, timeframe
        """).fetchdf()

        if df.empty:
            print("  (no bars aggregated yet)")
        else:
            import datetime

            for _, row in df.iterrows():
                e = datetime.datetime.fromtimestamp(row["earliest_s"], tz=datetime.timezone.utc)
                l = datetime.datetime.fromtimestamp(  # noqa: E741
                    row["latest_s"], tz=datetime.timezone.utc
                )
                print(f"  {row['symbol']:<8} {row['timeframe']:<4} {row['bars']:>8} bars  {e.date()} → {l.date()}")

        print("\n=== Available Tick Data ===")
        symbols = get_available_symbols(con)
        for sym in symbols:
            cov = check_tick_coverage(con, sym)
            e = datetime.datetime.fromtimestamp(cov["earliest"] / 1000, tz=datetime.timezone.utc)
            l = datetime.datetime.fromtimestamp(  # noqa: E741
                cov["latest"] / 1000, tz=datetime.timezone.utc
            )
            print(f"  {sym:<8} {cov['count']:>12,} ticks  {e.date()} → {l.date()}")

        con.close()
        return

    if not args.symbol:
        # Aggregate all available symbols
        con = duckdb.connect(str(DB_PATH), read_only=True)
        symbols = get_available_symbols(con)
        con.close()
        if not symbols:
            logger.error("No tick data found in %s", DB_PATH)
            sys.exit(1)
        logger.info("Aggregating all symbols: %s", symbols)
        for sym in symbols:
            tfs = args.timeframes.split(",") if args.timeframes else None
            aggregate_symbol(sym, tfs, force=args.force)
    else:
        tfs = args.timeframes.split(",") if args.timeframes else None
        results = aggregate_symbol(args.symbol.upper(), tfs, force=args.force)
        if results:
            total = sum(results.values())
            print(f"\nDone: {total} total bars across {len(results)} timeframes")
            for tf, cnt in sorted(results.items()):
                print(f"  {tf}: {cnt:>8} bars")


if __name__ == "__main__":
    main()
