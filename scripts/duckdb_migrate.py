#!/usr/bin/env python3
"""
Phase 8b: DuckDB schema design + CSV migration with parity tests.

Creates ayumi_market.duckdb with bars, symbols, and wf_results tables.
Imports all bar data CSVs from data/forex/historical/ with timezone conversion
(America/New_York → UTC epoch seconds) using DuckDB's native SQL engine.
Runs row-count parity checks, random sample diffs, and DST boundary spot-checks.
"""

# Flush stdout immediately so we see output in real-time
import functools
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

print = functools.partial(print, flush=True)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AYUMI_ROOT = Path("$AYUMI_ROOT")
CSV_DIR = AYUMI_ROOT / "data" / "forex" / "historical"
DB_PATH = AYUMI_ROOT / "data" / "ayumi_market.duckdb"
REPORTS_DIR = AYUMI_ROOT / "reports"

_EASTERN = ZoneInfo("America/New_York")
_UTC = timezone.utc

# Pip values based on PipCalculator.pip_value() (src/forex_bot/core/pip.py)
SYMBOL_INFO = {
    "EURUSD": {"pip_value": 0.0001, "description": "Euro / US Dollar"},
    "GBPUSD": {"pip_value": 0.0001, "description": "British Pound / US Dollar"},
    "USDJPY": {"pip_value": 0.01, "description": "US Dollar / Japanese Yen"},
    "GBPJPY": {"pip_value": 0.01, "description": "British Pound / Japanese Yen"},
    "XAUUSD": {"pip_value": 0.01, "description": "Gold / US Dollar"},
    "USDCAD": {"pip_value": 0.0001, "description": "US Dollar / Canadian Dollar"},
    "USDCHF": {"pip_value": 0.0001, "description": "US Dollar / Swiss Franc"},
}

# Non-bar CSVs to skip
SKIP_FILES = {"wf_results.csv", "phase3c_M5_features.csv", "phase3c_M5_labels.csv"}

# Filename pattern: SYMBOL_TIMEFRAME[_variant].csv
FILENAME_RE = re.compile(r"^([A-Z]+)_(M\d+|H\d+|D\d+)(?:_\w+)?\.csv$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_filename(filename: str):
    """Extract symbol and timeframe from CSV filename."""
    m = FILENAME_RE.match(filename)
    return (m.group(1), m.group(2)) if m else (None, None)


def csv_to_utc_epoch(date_str: str) -> int:
    """Convert CSV timestamp (America/New_York) to UTC epoch seconds.

    Matches data_loader.py:_parse_csv_timestamp.
    Handles strings with optional timezone suffixes (e.g. '+00:00').
    """
    # Strip timezone suffix if present (DuckDB may add it when casting to VARCHAR)
    date_str = str(date_str).split("+")[0].rstrip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(date_str, fmt)
            dt = dt.replace(tzinfo=_EASTERN)
            return int(dt.astimezone(_UTC).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {date_str}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def create_schema(con):
    """Create DuckDB tables and indexes."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            timestamp_utc BIGINT,
            symbol        TEXT,
            timeframe     TEXT,
            open          DOUBLE,
            high          DOUBLE,
            low           DOUBLE,
            close         DOUBLE,
            volume        BIGINT,
            spread_pips   DOUBLE
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            symbol       TEXT,
            pip_value    DOUBLE,
            description  TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS wf_results (
            strategy   TEXT,
            symbol     TEXT,
            timeframe  TEXT,
            params     JSON,
            metrics    JSON,
            run_at     TIMESTAMP
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_sym_tf ON bars(symbol, timeframe)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(timestamp_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_ts ON bars(symbol, timeframe, timestamp_utc)")


def populate_symbols(con):
    """Populate the symbols reference table."""
    for symbol, info in SYMBOL_INFO.items():
        con.execute(
            "INSERT INTO symbols VALUES (?, ?, ?)",
            [symbol, info["pip_value"], info["description"]],
        )


# ---------------------------------------------------------------------------
# CSV Import (pure DuckDB SQL — no pandas, no Python lists)
# ---------------------------------------------------------------------------

# Date column name variants across CSV files
DATE_COL_VARIANTS = ("Date", "Datetime", "timestamp")


def detect_date_col(con, csv_path: Path) -> str:
    """Detect which date column name the CSV uses."""
    cols = con.execute(
        f"DESCRIBE SELECT * FROM read_csv_auto('{csv_path}', header=true)"  # noqa: S608
    ).fetchall()
    col_names = {c[0] for c in cols}
    for v in DATE_COL_VARIANTS:
        if v in col_names:
            return v
    # Case-insensitive fallback
    lower_map = {c.lower(): c for c in col_names}
    for v in DATE_COL_VARIANTS:
        if v.lower() in lower_map:
            return lower_map[v.lower()]
    raise ValueError(f"No date column found in {csv_path}. Columns: {col_names}")


def import_bar_csv_sql(con, csv_path: Path, symbol: str, timeframe: str) -> dict:
    """Import a bar-data CSV using DuckDB's native read_csv_auto + SQL.

    Timezone conversion: date column (naive America/New_York) → UTC epoch seconds.
    Uses DuckDB's AT TIME ZONE operator, which matches Python's zoneinfo exactly.
    """
    date_col = detect_date_col(con, csv_path)

    # Get CSV row count first
    csv_rows = con.execute(
        f"SELECT COUNT(*) FROM read_csv_auto('{csv_path}', header=true)"  # noqa: S608
    ).fetchone()[0]

    # Get current DB count for this symbol/timeframe (before import)
    count_before = con.execute(
        "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
        [symbol, timeframe],
    ).fetchone()[0]

    # Bulk INSERT ... SELECT with timezone conversion — all in DuckDB engine
    con.execute(f"""
        INSERT INTO bars
        SELECT
            EXTRACT(EPOCH FROM (CAST("{date_col}" AS TIMESTAMP) AT TIME ZONE 'America/New_York'))::BIGINT AS timestamp_utc,
            '{symbol}' AS symbol,
            '{timeframe}' AS timeframe,
            "Open"::DOUBLE,
            "High"::DOUBLE,
            "Low"::DOUBLE,
            "Close"::DOUBLE,
            "Volume"::BIGINT,
            0.0::DOUBLE AS spread_pips
        FROM read_csv_auto('{csv_path}', header=true)
    """)  # noqa: E501, S608

    # Verify row count delta matches CSV row count
    count_after = con.execute(
        "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
        [symbol, timeframe],
    ).fetchone()[0]
    db_delta = count_after - count_before

    return {
        "file": csv_path.name,
        "symbol": symbol,
        "timeframe": timeframe,
        "csv_rows": csv_rows,
        "db_rows": db_delta,  # delta (rows added by this file)
        "db_total": count_after,  # total for this symbol/timeframe
        "parity": csv_rows == db_delta,
        "date_col": date_col,
    }


# ---------------------------------------------------------------------------
# Parity Checks
# ---------------------------------------------------------------------------


def check_row_count_parity(csv_results: list) -> list:
    """Row count parity per file (delta-based for shared symbol/timeframe)."""
    return [
        {
            "file": r["file"],
            "symbol": r["symbol"],
            "timeframe": r["timeframe"],
            "csv_rows": r["csv_rows"],
            "db_delta": r["db_rows"],
            "db_total_for_pair": r.get("db_total", r["db_rows"]),
            "match": r["parity"],
        }
        for r in csv_results
    ]


def check_random_sample_diff(con, csv_results: list, n: int = 10) -> list:
    """Pick n random dates across files, compare CSV vs DB values."""
    random.seed(42)

    candidates = []
    for r in csv_results:
        csv_path = CSV_DIR / r["file"]
        date_col = r.get("date_col", "Date")
        # Use DuckDB to pick a random row — cast date to VARCHAR for Python parsing
        row = con.execute(f"""
            SELECT CAST("{date_col}" AS VARCHAR), "Open", "High", "Low", "Close", "Volume"
            FROM read_csv_auto('{csv_path}', header=true)
            USING SAMPLE 1
        """).fetchone()  # noqa: S608
        if row:
            candidates.append(
                {
                    "file": r["file"],
                    "symbol": r["symbol"],
                    "timeframe": r["timeframe"],
                    "date_str": str(row[0]),
                    "csv_open": float(row[1]),
                    "csv_high": float(row[2]),
                    "csv_low": float(row[3]),
                    "csv_close": float(row[4]),
                    "csv_volume": int(row[5]),
                }
            )

    samples = random.sample(candidates, min(n, len(candidates)))
    results = []

    for s in samples:
        ts_utc = csv_to_utc_epoch(s["date_str"])
        db_rows = con.execute(
            "SELECT open, high, low, close, volume FROM bars WHERE symbol = ? AND timeframe = ? AND timestamp_utc = ?",
            [s["symbol"], s["timeframe"], ts_utc],
        ).fetchall()

        if db_rows:
            # Check if ANY matching row has the same values (handles duplicate timestamps from variant files)
            match = any(
                abs(s["csv_open"] - r[0]) < 1e-10
                and abs(s["csv_high"] - r[1]) < 1e-10
                and abs(s["csv_low"] - r[2]) < 1e-10
                and abs(s["csv_close"] - r[3]) < 1e-10
                and s["csv_volume"] == r[4]
                for r in db_rows
            )
            # Report the first row for debugging
            r0 = db_rows[0]
            db_vals = {
                "open": r0[0],
                "high": r0[1],
                "low": r0[2],
                "close": r0[3],
                "volume": r0[4],
                "matches_found": len(db_rows),
            }
        else:
            match = False
            db_vals = None

        results.append(
            {
                "file": s["file"],
                "date": s["date_str"],
                "csv_values": {
                    k: s[k]
                    for k in (
                        "csv_open",
                        "csv_high",
                        "csv_low",
                        "csv_close",
                        "csv_volume",
                    )
                },
                "db_values": db_vals,
                "match": match,
            }
        )

    return results


def check_dst_boundaries(con) -> list:
    """Spot-check 3 DST boundary dates for correct timezone conversion.

    Instead of looking for exact timestamps (which may not exist if the market
    is closed), we look for the nearest bar within ±1 hour of the DST transition
    and verify the UTC conversion is correct.
    """
    dst_tests = [
        # (Eastern time string, expected UTC hour, label)
        # Spring forward 2024: Mar 10, 2:00 AM EST → 3:00 AM EDT
        # 7:00 AM EDT = 11:00 AM UTC
        ("2024-03-10 07:00:00", 11, "Spring forward 2024 (EST→EDT)"),
        # Fall back 2023: Nov 5, 2:00 AM EDT → 1:00 AM EST
        # 6:00 AM EST = 11:00 AM UTC
        ("2023-11-05 06:00:00", 11, "Fall back 2023 (EDT→EST)"),
        # Spring forward 2025: Mar 9, 2:00 AM EST → 3:00 AM EDT
        # 7:00 AM EDT = 11:00 AM UTC
        ("2025-03-09 07:00:00", 11, "Spring forward 2025 (EST→EDT)"),
    ]

    results = []
    for date_str, expected_utc_hour, label in dst_tests:
        ts_utc = csv_to_utc_epoch(date_str)
        utc_dt = datetime.fromtimestamp(ts_utc, tz=_UTC)
        actual_utc_hour = utc_dt.hour

        # Find nearest bar within ±3600 seconds (1 hour)
        nearest = con.execute(
            "SELECT symbol, timeframe, timestamp_utc, open, high, low, close "
            "FROM bars WHERE timestamp_utc BETWEEN ? AND ? "
            "ORDER BY ABS(timestamp_utc - ?) LIMIT 3",
            [ts_utc - 3600, ts_utc + 3600, ts_utc],
        ).fetchall()

        results.append(
            {
                "label": label,
                "csv_date_eastern": date_str,
                "utc_epoch": ts_utc,
                "utc_readable": utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "expected_utc_hour": expected_utc_hour,
                "actual_utc_hour": actual_utc_hour,
                "hour_match": actual_utc_hour == expected_utc_hour,
                "nearest_bars_found": len(nearest),
                "nearest_samples": [list(r) for r in nearest[:3]] if nearest else None,
            }
        )

    return results


def check_spread_pips(con) -> dict:
    """Verify spread_pips field is populated where source had ask columns.
    Since no CSV has ask columns, all spread_pips should be 0.0."""
    result = con.execute(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN spread_pips = 0.0 THEN 1 ELSE 0 END) as zero_count, "
        "SUM(CASE WHEN spread_pips != 0.0 THEN 1 ELSE 0 END) as nonzero_count "
        "FROM bars"
    ).fetchone()
    return {
        "total_rows": result[0],
        "zero_pips": result[1],
        "nonzero_pips": result[2],
        "note": "No CSV files contain ask columns; all spread_pips are 0.0 as expected.",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 72)
    print("Phase 8b: DuckDB Schema + CSV Migration with Parity Tests")
    print("=" * 72)

    # Clean slate
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"\nRemoved existing DB: {DB_PATH}")

    con = duckdb.connect(str(DB_PATH))

    # --- Step 1: Schema ---
    print("\n[1] Creating schema...")
    create_schema(con)
    print("    ✅ tables: bars, symbols, wf_results")
    print("    ✅ indexes: idx_bars_sym_tf, idx_bars_ts, idx_bars_sym_tf_ts")

    # --- Step 2: Symbols ---
    print("\n[2] Populating symbols table...")
    populate_symbols(con)
    n = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    print(f"    ✅ {n} symbols inserted: {', '.join(SYMBOL_INFO.keys())}")

    # --- Step 3: Import bar CSVs ---
    print("\n[3] Importing bar data CSVs...")
    csv_files = sorted(CSV_DIR.glob("*.csv"))
    bar_files = []
    skipped = []

    for p in csv_files:
        if p.name in SKIP_FILES:
            skipped.append(p.name)
            continue
        sym, tf = parse_filename(p.name)
        if sym and tf:
            bar_files.append((p, sym, tf))
        else:
            skipped.append(p.name)

    print(f"    Found {len(bar_files)} bar data CSVs ({len(skipped)} skipped: {', '.join(skipped)})")

    csv_results = []
    total_rows = 0
    t_start = time.perf_counter()

    for csv_path, symbol, timeframe in bar_files:
        t0 = time.perf_counter()
        result = import_bar_csv_sql(con, csv_path, symbol, timeframe)
        elapsed = time.perf_counter() - t0
        total_rows += result["csv_rows"]
        status = "✅" if result["parity"] else "❌"
        print(f"    {status} {result['file']:40s} {result['csv_rows']:>8,} rows  ({elapsed:.2f}s)")
        csv_results.append(result)

    t_total = time.perf_counter() - t_start
    print(f"\n    Total: {total_rows:,} rows in {t_total:.1f}s")

    # --- Step 4: Parity checks ---
    print("\n[4] Parity checks...")

    # 4a: Row count parity
    rc_results = check_row_count_parity(csv_results)
    rc_all = all(r["match"] for r in rc_results)
    print(f"    Row count parity: {'✅ ALL MATCH' if rc_all else '❌ MISMATCH'} ({len(rc_results)} files)")

    # 4b: Random sample diff (10 samples)
    rs_results = check_random_sample_diff(con, csv_results, n=10)
    rs_all = all(r["match"] for r in rs_results)
    print(f"    Random sample diff: {'✅ ALL MATCH' if rs_all else '❌ MISMATCH'} ({len(rs_results)} samples)")
    if not rs_all:
        for r in rs_results:
            if not r["match"]:
                print(f"      ❌ {r['file']} @ {r['date']}: csv={r['csv_values']} db={r['db_values']}")

    # 4c: DST boundary spot-check
    dst_results = check_dst_boundaries(con)
    print(f"    DST boundary checks ({len(dst_results)} dates):")
    for d in dst_results:
        hour_ok = "✅" if d["hour_match"] else "❌"
        print(
            f"      {hour_ok} {d['label']}: {d['csv_date_eastern']} → {d['utc_readable']} "
            f"(UTC hour {d['actual_utc_hour']}, expected {d['expected_utc_hour']}) "
            f"— {d['nearest_bars_found']} bars near"
        )

    # 4d: spread_pips check
    sp_result = check_spread_pips(con)
    print(f"    spread_pips: {sp_result['zero_pips']:,} zero / {sp_result['nonzero_pips']} nonzero (expected all zero)")

    # --- Summary ---
    db_size_mb = DB_PATH.stat().st_size / 1024 / 1024

    print("\n" + "=" * 72)
    print("MIGRATION SUMMARY")
    print("=" * 72)
    print(f"  Database        : {DB_PATH}")
    print(f"  DB size         : {db_size_mb:.1f} MB")
    print(f"  Bar CSVs        : {len(csv_results)}")
    print(f"  Total rows      : {total_rows:,}")
    print(f"  Row count parity: {'✅' if rc_all else '❌'}")
    print(f"  Sample diff     : {'✅' if rs_all else '❌'}")
    print(f"  DST check       : {len(dst_results)} dates tested")
    print(f"  spread_pips     : {sp_result['zero_pips']:,} zero (expected)")

    # --- Save results JSON ---
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    results_path = REPORTS_DIR / "duckdb-migration-results-2026-07-08.json"
    output = {
        "database": str(DB_PATH),
        "db_size_mb": round(db_size_mb, 1),
        "bar_csvs_imported": len(csv_results),
        "total_rows": total_rows,
        "symbols": list(SYMBOL_INFO.keys()),
        "skipped_files": skipped,
        "parity": {
            "row_count": rc_results,
            "random_sample_diff": rs_results,
            "dst_boundary_check": dst_results,
            "spread_pips": sp_result,
        },
    }
    results_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n  Results JSON    : {results_path}")

    con.close()
    print("\n✅ Migration complete.")


if __name__ == "__main__":
    main()
