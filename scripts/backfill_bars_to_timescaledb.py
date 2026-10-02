#!/usr/bin/env python3
"""Backfill historical bars from cTrader Open API into TimescaleDB.

Usage:
    source .venv/bin/activate
    python scripts/backfill_bars_to_timescaledb.py [--symbols GBPUSD,USDJPY] [--periods M1,M5,...] [--lookback-days 365]

Iterates symbol × period combinations, fetches bars in chunks (max 5000 per request),
and upserts into market_bars hypertable. Skips already-fetched ranges using the
latest bar timestamp per (symbol, timeframe, broker) in the DB.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from adapters.ctrader.open_api_client import CTraderOpenApiClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill")

# ── Configuration ──────────────────────────────────────────────────────────────

SYMBOL_MAP = {
    "GBPUSD": 2,
    "USDJPY": 4,
    "EURUSD": 1,
}

PERIOD_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

MAX_BARS_PER_REQUEST = 5001  # conservative; API max is 5760
REQUEST_DELAY_SECONDS = 2.0  # rate limit buffer between API calls (2s to avoid 429s)
BROKER = "ctrader"
DEFAULT_LOOKBACK_DAYS = 365


def get_db_conn() -> psycopg2.extensions.connection:
    """Connect to TimescaleDB."""
    dsn = os.getenv(
        "TIMESCALEDB_DSN",
        "postgresql://ayumi_writer:AyWr1t_2026x!Secure@localhost:5432/ayumi_market",
    )
    return psycopg2.connect(dsn)


def get_latest_bar_ts(conn, symbol: str, timeframe: str) -> datetime | None:
    """Return the latest open_time for a given symbol/timeframe/broker."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(open_time) FROM market_bars
            WHERE symbol = %s AND timeframe = %s AND broker = %s
            """,
            (symbol, timeframe, BROKER),
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else None


def upsert_bars(conn, symbol: str, timeframe: str, bars: list[dict]) -> int:
    """Upsert bars into market_bars. Returns count of rows attempted."""
    if not bars:
        return 0

    values = []
    for b in bars:
        values.append(
            (
                symbol,
                timeframe,
                BROKER,
                b["timestamp"],  # datetime
                b["close_time"],  # datetime
                b["open"],
                b["high"],
                b["low"],
                b["close"],
                b.get("volume", 0),
            )
        )

    sql = """
        INSERT INTO market_bars
            (symbol, timeframe, broker, open_time, close_time,
             open_price, high_price, low_price, close_price, volume)
        VALUES %s
        ON CONFLICT (symbol, timeframe, broker, open_time) DO NOTHING
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, values, fetch=False)
    conn.commit()
    return len(values)


def backfill_symbol_period(
    client: CTraderOpenApiClient,
    conn,
    symbol: str,
    symbol_id: int,
    period: str,
    lookback_days: int,
) -> int:
    """Backfill one symbol × period combination. Returns total bars inserted.

    Pagination strategy: BACKWARD from present to lookback_start.
    The cTrader API returns the most recent N bars in a [from_ts, to_ts] window.
    So we start at the present and work backwards by moving to_ts to just before
    the earliest bar in each chunk.
    """
    period_secs = PERIOD_SECONDS[period]
    period_ms = period_secs * 1000
    total_inserted = 0
    total_fetched = 0

    # Determine the lookback start (earliest data we want)
    latest = get_latest_bar_ts(conn, symbol, period)
    if latest:
        # Resume: only fetch from the latest bar we already have onward
        from_dt = latest
        from_ts = int(latest.timestamp() * 1000)
        logger.info(f"  Resuming from {latest.isoformat()}")
    else:
        from_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        from_ts = int(from_dt.timestamp() * 1000)
        logger.info(f"  Starting from {from_dt.isoformat()} ({lookback_days}d lookback)")

    cursor_end = int(datetime.now(timezone.utc).timestamp() * 1000)
    iteration = 0
    MAX_RETRIES = 5
    retry_delays = [10, 20, 40, 80, 160]  # exponential backoff in seconds

    # Fetch in chunks, paginating BACKWARDS
    while cursor_end > from_ts:
        iteration += 1
        raw_bars = None
        for retry in range(MAX_RETRIES):
            try:
                raw_bars = client.get_trendbars(
                    symbol_id=symbol_id,
                    period=period,
                    from_ts=from_ts,
                    to_ts=cursor_end,
                    max_bars=MAX_BARS_PER_REQUEST,
                )
                break  # success
            except Exception as e:
                err_str = str(e).lower()
                if "rate" in err_str or "429" in err_str or "limit" in err_str or "blocked" in err_str:
                    wait = retry_delays[retry] if retry < len(retry_delays) else 160
                    logger.warning(f"  Rate limited, retry {retry + 1}/{MAX_RETRIES}, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"  API error: {e}")
                break
        else:
            logger.error("  Max retries exceeded, skipping this chunk")
            break

        if raw_bars is None:
            break

        if not raw_bars:
            logger.info("  No bars returned — gap or end of data")
            break

        # Convert timestamps from ms (int) to datetime objects
        bars = []
        for b in raw_bars:
            ts_ms = b["timestamp"]
            if isinstance(ts_ms, (int, float)):
                ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            else:
                ts_dt = ts_ms
            bars.append(
                {
                    "timestamp": ts_dt,
                    "close_time": ts_dt + timedelta(seconds=period_secs),
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b.get("volume", 0),
                }
            )

        # Sort bars ascending by timestamp (earliest first)
        bars.sort(key=lambda b: b["timestamp"])

        count = upsert_bars(conn, symbol, period, bars)
        total_inserted += count
        total_fetched += len(bars)

        # Log actual insert count
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM market_bars WHERE symbol = %s AND timeframe = %s AND broker = %s AND open_time >= %s",  # noqa: E501
                (symbol, period, BROKER, bars[0]["timestamp"]),
            )
            actual_count = cur.fetchone()[0]

        # Move cursor backwards: set cursor_end to just before the earliest bar
        earliest_bar_ts = bars[0]["timestamp"]
        new_cursor_end = int(earliest_bar_ts.timestamp() * 1000) - period_ms

        # No hasMore field on TrendbarsRes — use cursor movement as pagination signal
        # The API consistently returns (count - 1) bars, so len-based threshold is unreliable.
        # Rely on: (1) empty response, (2) cursor not moving backwards, or (3) past lookback start.
        if new_cursor_end <= from_ts:
            logger.info("  Reached lookback start — done")
            break
        if new_cursor_end >= cursor_end:
            logger.info(f"  Cursor didn't move backwards (got {len(bars)} bars) — all data fetched")
            break
        cursor_end = new_cursor_end

        logger.info(
            f"  Chunk {iteration}: {len(bars)} bars (actual in DB: {actual_count}), "
            f"earliest={earliest_bar_ts.strftime('%Y-%m-%d %H:%M')}, "
            f"total: {total_fetched}"
        )

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(f"  ✅ {symbol}/{period}: {total_fetched} fetched, ~{total_inserted} inserted")
    return total_inserted


def main():
    parser = argparse.ArgumentParser(description="Backfill historical bars into TimescaleDB")
    parser.add_argument(
        "--symbols",
        default="GBPUSD,USDJPY",
        help="Comma-separated symbols (default: GBPUSD,USDJPY)",
    )
    parser.add_argument(
        "--periods",
        default="M1,M5,M15,M30,H1,H4,D1,W1",
        help="Comma-separated periods (default: M1,M5,M15,M30,H1,H4,D1,W1)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Days of history to fetch (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect to API but don't write to DB",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    periods = [p.strip().upper() for p in args.periods.split(",")]

    # Validate
    for sym in symbols:
        if sym not in SYMBOL_MAP:
            logger.error(f"Unknown symbol: {sym}. Known: {list(SYMBOL_MAP.keys())}")
            sys.exit(1)
    for p in periods:
        if p not in PERIOD_SECONDS:
            logger.error(f"Unknown period: {p}. Known: {list(PERIOD_SECONDS.keys())}")
            sys.exit(1)

    logger.info(f"Backfill: {len(symbols)} symbols × {len(periods)} periods = {len(symbols) * len(periods)} combos")
    logger.info(f"Symbols: {symbols}")
    logger.info(f"Periods: {periods}")
    logger.info(f"Lookback: {args.lookback_days} days")
    logger.info(f"{'DRY RUN — no writes' if args.dry_run else 'LIVE — writing to TimescaleDB'}")

    # Load .env from project root
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")

    # Connect to cTrader
    client = CTraderOpenApiClient(
        client_id=os.getenv("CTRADER_OPENAPI_CLIENT_ID"),
        client_secret=os.getenv("CTRADER_OPENAPI_CLIENT_SECRET"),
        access_token=os.getenv("CTRADER_OPENAPI_ACCESS_TOKEN"),
        account_id=int(os.getenv("CTRADER_OPENAPI_ACCOUNT_ID", "5795523")),
    )
    logger.info("Connecting to cTrader Open API...")
    client.connect()
    logger.info("Connected to cTrader ✅")

    # Connect to TimescaleDB
    conn = get_db_conn()
    logger.info("Connected to TimescaleDB ✅")

    grand_total = 0
    combos = len(symbols) * len(periods)
    completed = 0

    for symbol in symbols:
        symbol_id = SYMBOL_MAP[symbol]
        for period in periods:
            completed += 1
            logger.info(f"\n[{completed}/{combos}] {symbol}/{period}")
            try:
                if not args.dry_run:
                    count = backfill_symbol_period(
                        client,
                        conn,
                        symbol,
                        symbol_id,
                        period,
                        args.lookback_days,
                    )
                    grand_total += count
                else:
                    logger.info("  (dry run — skipping)")
            except Exception as e:
                logger.error(f"  ❌ Failed: {e}")
                conn.rollback()
                continue

    conn.close()
    client.disconnect()
    logger.info(f"\n🏁 Done. Total bars inserted: {grand_total}")


if __name__ == "__main__":
    main()
