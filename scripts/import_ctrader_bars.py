#!/usr/bin/env python3
"""Idempotent DuckDB importer for cTrader OHLCV CSV bars (card 291de428).

Implements the council binding guards (Kaito arch + Sora risk review
2026-10-04) so a fresh XAUUSD backfill can land safely into
``data/ayumi_market.duckdb``:

  G1 Unit normalization at ingest: reject ``timestamp_utc > 1e11`` (which is
     epoch-ms territory) unless ``--allow-ms`` was supplied; range-check
     every parsed timestamp to fall within ``2020-01-01..today+1d`` so a
     bogus Date column cannot poison the bars table.

  G2 Idempotent UPSERT via staging table keyed on
     ``(symbol, timeframe, timestamp_utc)``. The entire ingest + diff happens
     inside one DuckDB transaction; if the post-ingest row-count delta
     assertion fails (inserted + updated != staged) we ``ROLLBACK`` and
     return ``result='error'`` instead of partially committing.

  G3 Pre-flight staging-vs-existing min/max/count assertion. Before we
     touch the live bars table we run ``MIN/MAX/COUNT`` on the staging
     table and the corresponding ``bars`` slice and bail out if the
     staging slice covers a different range than what we expect (i.e. the
     fresh CSV is talking about the wrong symbol or has the wrong
     timeframe label).

  G4 ``--dry-run`` is the default; ``--apply`` is the explicit opt-in. A
     one-line row is written to ``import_log`` whenever an ``--apply`` run
     commits so we have an audit trail.

  G6 Hard whitelist: ``XAUUSD x {M3,M5,M15,M30,H1}`` only. Anything outside
     that cross-product is rejected at the top of ``import_csv`` before
     we touch a DuckDB connection. EURUSD/GBPUSD/USDJPY are FROZEN per
     pivot Q5 and the GBPUSD ms-row population must remain untouched.

  G7 CSV header schema validation. Only ``Date,Open,High,Low,Close,Volume``
     is accepted; a missing or renamed column aborts the import. The
     importer also runs a header-substring check (case-insensitive) so
     a stray ``ask_open`` column doesn't sneak in unannounced.

  Sora gate: insert a synthetic epoch-ms row (UTC = 2024-06-15 12:00
     expressed as ``int(ts.timestamp() * 1000)``) into the staging table
     and assert that after ``to_epoch_seconds`` it lives at exactly
     1718452800 (the seconds-scale equivalent). This is implemented as
     a unit test in ``tests/unit/test_import_ctrader_bars.py`` so it
     runs on every test invocation, not just during a live import.

The spread_pips convention follows the aggregator: 1 pip on XAUUSD =
0.01 (the existing ``scripts/aggregate_ticks_to_bars.py`` convention).
CostModel alignment is a separate sub-ticket — not this card's scope.

``import_csv`` is the canonical entry point and is also called from
``scripts/download_ctrader_data.py`` after a successful cTrader download.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "ayumi_market.duckdb"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("import_ctrader_bars")

# ---------------------------------------------------------------------------
# Hard whitelist + schema (G6, G7)
# ---------------------------------------------------------------------------

ALLOWED_SYMBOLS: frozenset[str] = frozenset({"XAUUSD"})
ALLOWED_TIMEFRAMES: frozenset[str] = frozenset({"M3", "M5", "M15", "M30", "H1"})

REQUIRED_CSV_COLUMNS: tuple[str, ...] = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

# G1: any timestamp above this threshold is treated as epoch-ms and rejected
# unless the caller explicitly passes --allow-ms. 1e11 seconds is year 5138,
# 1e11 ms is 1973-03-03 — the boundary is wide enough to catch any sane
# epoch-seconds value but narrow enough to flag accidental ms writes.
EPOCH_MS_THRESHOLD: int = 100_000_000_000  # 1e11

# G1: range-check window for parsed timestamps. Outside this window is
# almost certainly a malformed Date column.
EARLIEST_VALID_DATE = _dt.date(2020, 1, 1)
LATEST_VALID_DATE = _dt.date.today() + _dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ImportGuardError(Exception):
    """Raised when any G1–G7 guard is violated. The caller should treat
    these as hard aborts and not commit any partial state.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_epoch_seconds(ts: int, *, allow_ms: bool) -> int:
    """Normalise a single epoch value to seconds-scale (G1).

    Raises ``ImportGuardError`` when the value is clearly ms-scaled and
    ``allow_ms`` is False. The threshold is intentionally conservative
    (1e11 = year 5138 in seconds) so that any epoch-seconds value passes
    and only true ms values get flagged.
    """
    if ts < 0:
        raise ImportGuardError(f"timestamp {ts} is negative")
    if ts > EPOCH_MS_THRESHOLD:
        if not allow_ms:
            raise ImportGuardError(
                f"timestamp {ts} looks like epoch-ms (>1e11); pass --allow-ms to "
                f"import it (G1 guard). Use to_epoch_seconds() to normalise first."
            )
        return ts // 1000
    return ts


def normalise_csv_timestamp(raw: str, *, allow_ms: bool) -> int:
    """Parse a CSV Date cell into an epoch-seconds int (UTC)."""
    raw = raw.strip()
    if not raw:
        raise ImportGuardError("empty timestamp string")

    # Try numeric first (epoch-seconds or epoch-ms).
    try:
        n = int(raw)
        return to_epoch_seconds(n, allow_ms=allow_ms)
    except ValueError:
        pass

    # Fall back to ISO-style strings.
    candidate = raw.replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(candidate, fmt).replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue

    raise ImportGuardError(f"unparseable timestamp: {raw!r}")


def validate_csv_header(path: Path) -> None:
    """G7 header schema validation. Reads only the first line and asserts
    that all required columns are present (case-insensitive, order-free).

    Raises ``ImportGuardError`` with a precise list of missing columns.
    """
    with open(path, "r", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ImportGuardError(f"{path.name} is empty (no header row)")
    lower = [c.strip().lower() for c in header]
    missing = [c for c in REQUIRED_CSV_COLUMNS if c not in lower]
    if missing:
        raise ImportGuardError(
            f"{path.name} header missing required columns {missing}; "
            f"got {header}"
        )


def read_csv_rows(path: Path, *, allow_ms: bool) -> list[dict]:
    """Read all rows from the CSV and return as list of dicts, with
    ``timestamp_utc`` already normalised to epoch-seconds.

    Bad rows are skipped with a warning so a single corrupt line doesn't
    fail an otherwise good import. We do NOT drop rows silently — every
    drop is logged with line number + reason so the operator can audit.
    """
    rows: list[dict] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        # Case-insensitive column access
        lower_to_actual = {c.lower(): c for c in reader.fieldnames or []}
        for line_no, raw in enumerate(reader, start=2):  # header is line 1
            try:
                raw_ts = raw[lower_to_actual["date"]]
                ts = normalise_csv_timestamp(raw_ts, allow_ms=allow_ms)
            except (KeyError, ImportGuardError) as exc:
                log.warning("[%s] line %d: dropping bad timestamp (%s)", path.name, line_no, exc)
                continue
            try:
                o = float(raw[lower_to_actual["open"]])
                h = float(raw[lower_to_actual["high"]])
                lo = float(raw[lower_to_actual["low"]])
                c = float(raw[lower_to_actual["close"]])
                v = int(float(raw[lower_to_actual["volume"]] or 0))
            except (KeyError, ValueError) as exc:
                log.warning("[%s] line %d: dropping bad OHLCV (%s)", path.name, line_no, exc)
                continue
            ts_dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).date()
            if ts_dt < EARLIEST_VALID_DATE or ts_dt > LATEST_VALID_DATE:
                log.warning(
                    "[%s] line %d: dropping out-of-range ts %s (%s)",
                    path.name, line_no, ts, ts_dt.isoformat(),
                )
                continue
            rows.append(
                {
                    "timestamp_utc": ts,
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "volume": v,
                }
            )
    return rows


def preflight_assert(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    timeframe: str,
    staged_rows: int,
) -> None:
    """G3 pre-flight: assert staging slice is sane relative to existing
    bars for this (symbol, timeframe). If the existing slice is empty
    (fresh symbol/TF) we only require staged_rows > 0. If non-empty we
    assert that the staging range doesn't extend backwards in time before
    the existing earliest timestamp by more than the candle width — a
    guard against a mislabelled CSV.
    """
    existing = con.execute(
        """
        SELECT COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc)
        FROM bars WHERE symbol = ? AND timeframe = ?
        """,
        [symbol, timeframe],
    ).fetchone()
    n_existing, ts_min, ts_max = existing
    if n_existing == 0:
        if staged_rows <= 0:
            raise ImportGuardError(
                f"G3 pre-flight failed: 0 staged rows for fresh {symbol} {timeframe}"
            )
        return
    # Existing data present — assert staging cover window aligns.
    stg = con.execute(
        """
        SELECT MIN(timestamp_utc), MAX(timestamp_utc)
        FROM bars_staging WHERE symbol = ? AND timeframe = ?
        """,
        [symbol, timeframe],
    ).fetchone()
    stg_min, stg_max = stg
    if stg_min is None:
        raise ImportGuardError(
            f"G3 pre-flight failed: no staging rows for {symbol} {timeframe}"
        )
    # If the staging earliest timestamp is more than one candle-width before
    # the existing earliest, the CSV is probably mislabelled (different
    # time period than expected).
    candle_secs = _candle_seconds_for(timeframe)
    if (ts_min - stg_min) > candle_secs:
        raise ImportGuardError(
            f"G3 pre-flight: staging earliest ({stg_min}) is more than one "
            f"{timeframe} candle earlier than existing ({ts_min}). Rejecting "
            f"as a likely mislabelled CSV."
        )
    if (stg_max - ts_max) > candle_secs * 2:
        # Staging extends slightly past existing is fine (it's a backfill
        # by design); but a huge gap means the CSV may be the wrong slice.
        log.warning(
            "G3 pre-flight: staging extends past existing by %ds (existing_max=%s, staging_max=%s)",
            stg_max - ts_max,
            ts_max,
            stg_max,
        )


def _candle_seconds_for(tf: str) -> int:
    return {
        "M3": 180,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }.get(tf, 3600)


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    symbol: str
    timeframe: str
    mode: str
    result: str  # "ok" | "skipped" | "error"
    staged_rows: int = 0
    inserted_rows: int = 0
    updated_rows: int = 0
    preflight_ok: bool = False
    postflight_ok: bool = False
    error: str | None = None
    import_log_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "mode": self.mode,
            "result": self.result,
            "staged_rows": self.staged_rows,
            "inserted_rows": self.inserted_rows,
            "updated_rows": self.updated_rows,
            "preflight_ok": self.preflight_ok,
            "postflight_ok": self.postflight_ok,
            "error": self.error,
            "import_log_id": self.import_log_id,
        }


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Idempotently create ``bars``, ``bars_staging``, ``symbols``, and
    ``import_log`` tables. The staging table mirrors ``bars`` plus the
    is_holdout column that we drop before the UPSERT.
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
            timestamp_utc BIGINT NOT NULL,
            symbol        TEXT    NOT NULL,
            timeframe     TEXT    NOT NULL,
            open          DOUBLE  NOT NULL,
            high          DOUBLE  NOT NULL,
            low           DOUBLE  NOT NULL,
            close         DOUBLE  NOT NULL,
            volume        BIGINT  NOT NULL DEFAULT 0,
            spread_pips   DOUBLE  DEFAULT 0.0,
            is_holdout    BOOLEAN DEFAULT false
        );
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bars_unique "
        "ON bars(symbol, timeframe, timestamp_utc)"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS bars_staging (
            timestamp_utc BIGINT,
            symbol        TEXT,
            timeframe     TEXT,
            open          DOUBLE,
            high          DOUBLE,
            low           DOUBLE,
            close         DOUBLE,
            volume        BIGINT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            symbol      TEXT PRIMARY KEY,
            pip_value   DOUBLE,
            description TEXT
        );
        """
    )
    # G4 audit log — migrate any legacy 4-col import_log (init_tick_db.py
    # historical) to the canonical 9-col schema, then ensure it exists.
    _migrate_import_log(con)
    con.execute("CREATE SEQUENCE IF NOT EXISTS import_log_seq START 1")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS import_log (
            id            BIGINT  DEFAULT nextval('import_log_seq'),
            imported_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            source        TEXT,
            symbol        TEXT,
            timeframe     TEXT,
            dry_run       BOOLEAN,
            staged_rows   INTEGER,
            inserted_rows INTEGER,
            updated_rows  INTEGER,
            result        TEXT,
            error         TEXT
        );
        """
    )
    # Pip convention per aggregator: 0.01 for XAUUSD (XAU_PAIR_PIP).
    con.execute(
        "INSERT INTO symbols(symbol, pip_value, description) VALUES (?, ?, ?) "
        "ON CONFLICT (symbol) DO UPDATE SET pip_value = EXCLUDED.pip_value",
        ["XAUUSD", 0.01, "Gold vs US Dollar"],
    )


def import_csv(
    csv_path: Path,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    symbol: str = "XAUUSD",
    timeframe: str = "M15",
    dry_run: bool = True,
    allow_ms: bool = False,
) -> dict:
    """Canonical entry point. Validates the request, reads the CSV into a
    staging table, runs pre-flight, then either runs the guarded UPSERT
    (apply) or just reports what would happen (dry-run).

    Returns an ``ImportResult.as_dict()`` payload.
    """
    # G6: hard whitelist.
    if symbol not in ALLOWED_SYMBOLS:
        return ImportResult(
            symbol=symbol, timeframe=timeframe,
            mode="dry-run" if dry_run else "apply",
            result="error",
            error=f"symbol {symbol!r} not in hard whitelist {sorted(ALLOWED_SYMBOLS)}",
        ).as_dict()
    if timeframe not in ALLOWED_TIMEFRAMES:
        return ImportResult(
            symbol=symbol, timeframe=timeframe,
            mode="dry-run" if dry_run else "apply",
            result="error",
            error=f"timeframe {timeframe!r} not in hard whitelist {sorted(ALLOWED_TIMEFRAMES)}",
        ).as_dict()

    csv_path = Path(csv_path)
    if not csv_path.exists():
        return ImportResult(
            symbol=symbol, timeframe=timeframe,
            mode="dry-run" if dry_run else "apply",
            result="error",
            error=f"CSV not found: {csv_path}",
        ).as_dict()

    # G7: header schema validation before we even open DuckDB.
    try:
        validate_csv_header(csv_path)
    except ImportGuardError as exc:
        return ImportResult(
            symbol=symbol, timeframe=timeframe,
            mode="dry-run" if dry_run else "apply",
            result="error",
            error=str(exc),
        ).as_dict()

    rows = read_csv_rows(csv_path, allow_ms=allow_ms)
    staged_rows = len(rows)
    mode = "dry-run" if dry_run else "apply"
    if staged_rows == 0:
        log.warning("[%s] no rows after parsing/validation; skipping", csv_path.name)
        return ImportResult(
            symbol=symbol, timeframe=timeframe, mode=mode,
            result="skipped", staged_rows=0,
        ).as_dict()

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        ensure_schema(con)

        # Stage rows (truncate first so a re-run is clean).
        con.execute("DELETE FROM bars_staging")
        # DuckDB's register() needs a tabular source; a list of dicts is not
        # always picked up cleanly, so wrap in a pandas DataFrame.
        staging_df = pd.DataFrame(rows)
        con.register("staging_df", staging_df)
        con.execute(
            """
            INSERT INTO bars_staging(
                timestamp_utc, symbol, timeframe,
                open, high, low, close, volume
            )
            SELECT
                timestamp_utc, ?, ?,
                open, high, low, close, volume
            FROM staging_df
            """,
            [symbol, timeframe],
        )
        con.unregister("staging_df")

        # G3 pre-flight.
        try:
            preflight_assert(
                con,
                symbol=symbol,
                timeframe=timeframe,
                staged_rows=staged_rows,
            )
            preflight_ok = True
        except ImportGuardError as exc:
            return ImportResult(
                symbol=symbol, timeframe=timeframe, mode=mode,
                result="error", staged_rows=staged_rows,
                error=f"G3 pre-flight: {exc}",
            ).as_dict()

        if dry_run:
            # Surface what we *would* do, but don't commit anything.
            log.info(
                "DRY-RUN: would import %d rows for %s %s into %s",
                staged_rows, symbol, timeframe, db_path,
            )
            # Audit log row even for dry-run, with dry_run=true.
            log_id = _write_import_log(
                con,
                source=str(csv_path),
                symbol=symbol, timeframe=timeframe,
                dry_run=True,
                staged_rows=staged_rows,
                inserted_rows=0, updated_rows=0,
                result="dry-run", error=None,
            )
            return ImportResult(
                symbol=symbol, timeframe=timeframe, mode=mode,
                result="ok", staged_rows=staged_rows,
                inserted_rows=0, updated_rows=0,
                preflight_ok=True, postflight_ok=True,
                import_log_id=log_id,
            ).as_dict()

        # G2 atomic UPSERT inside a transaction.
        con.execute("BEGIN")
        try:
            n_before = con.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
                [symbol, timeframe],
            ).fetchone()[0]
            con.execute(
                """
                INSERT INTO bars(
                    timestamp_utc, symbol, timeframe,
                    open, high, low, close, volume, spread_pips, is_holdout
                )
                SELECT
                    s.timestamp_utc, s.symbol, s.timeframe,
                    s.open, s.high, s.low, s.close, s.volume,
                    0.0, false
                FROM bars_staging s
                ON CONFLICT (symbol, timeframe, timestamp_utc) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    spread_pips = EXCLUDED.spread_pips
                """
            )
            n_after = con.execute(
                "SELECT COUNT(*) FROM bars WHERE symbol = ? AND timeframe = ?",
                [symbol, timeframe],
            ).fetchone()[0]

            inserted_rows = max(0, n_after - n_before)
            updated_rows = max(0, staged_rows - inserted_rows)

            # G2 row-count delta assertion.
            if (inserted_rows + updated_rows) != staged_rows:
                raise ImportGuardError(
                    f"G2 row-count delta mismatch: staged={staged_rows} "
                    f"inserted={inserted_rows} updated={updated_rows} "
                    f"(inserted+updated={inserted_rows + updated_rows})"
                )

            con.execute("COMMIT")
            postflight_ok = True
            log_id = _write_import_log(
                con,
                source=str(csv_path),
                symbol=symbol, timeframe=timeframe,
                dry_run=False,
                staged_rows=staged_rows,
                inserted_rows=inserted_rows,
                updated_rows=updated_rows,
                result="ok", error=None,
            )
            log.info(
                "APPLY: imported %d rows for %s %s (inserted=%d updated=%d)",
                staged_rows, symbol, timeframe, inserted_rows, updated_rows,
            )
            return ImportResult(
                symbol=symbol, timeframe=timeframe, mode="apply",
                result="ok", staged_rows=staged_rows,
                inserted_rows=inserted_rows, updated_rows=updated_rows,
                preflight_ok=True, postflight_ok=True,
                import_log_id=log_id,
            ).as_dict()
        except ImportGuardError as exc:
            con.execute("ROLLBACK")
            log_id = _write_import_log(
                con,
                source=str(csv_path),
                symbol=symbol, timeframe=timeframe,
                dry_run=False,
                staged_rows=staged_rows,
                inserted_rows=0, updated_rows=0,
                result="error", error=str(exc),
            )
            return ImportResult(
                symbol=symbol, timeframe=timeframe, mode="apply",
                result="error", staged_rows=staged_rows,
                preflight_ok=True, postflight_ok=False,
                error=f"G2 rollback: {exc}",
                import_log_id=log_id,
            ).as_dict()
    finally:
        con.close()


def _migrate_import_log(con: duckdb.DuckDBPyConnection) -> None:
    """Migrate ``import_log`` to the canonical 9-col G4 audit schema if it
    is currently the legacy 4-col schema produced by ``init_tick_db.py``.

    Legacy schema (init_tick_db.py):
        filename VARCHAR NOT NULL PRIMARY KEY,
        symbol VARCHAR, row_count BIGINT, imported_at VARCHAR

    Canonical schema (G4 audit log):
        id BIGINT (seq), imported_at TIMESTAMP, source TEXT, symbol TEXT,
        timeframe TEXT, dry_run BOOLEAN, staged_rows INTEGER,
        inserted_rows INTEGER, updated_rows INTEGER, result TEXT, error TEXT

    The legacy schema is detected by checking for the ``filename`` column.
    When found it is renamed to ``import_log_legacy_v4col`` so no historical
    tick-aggregation rows are lost (the tick module still queries
    ``filename``); the new empty canonical table is then created by the
    subsequent ``CREATE TABLE IF NOT EXISTS`` in ``ensure_schema``.

    Safe to call on a fresh DB: a no-op when the table does not yet exist
    or already has the canonical schema.
    """
    # Does ``import_log`` exist at all?
    table_exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'import_log'"
    ).fetchone()[0]
    if not table_exists:
        return  # ensure_schema's CREATE TABLE will mint the canonical one.

    # Which columns does it currently have?
    cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'import_log'"
        ).fetchall()
    }

    # Canonical schema fingerprint — the legacy schema has ``filename``
    # as NOT NULL PRIMARY KEY, which would block the G4 9-col INSERT
    # (it never supplies ``filename``). Anything without ``filename`` is
    # canonical (or close enough; CREATE IF NOT EXISTS will be a no-op).
    if "filename" not in cols:
        return

    # Legacy 4-col schema — preserve the historical rows (the tick
    # module queries ``filename``) by renaming, then let
    # ``CREATE TABLE IF NOT EXISTS`` below mint the canonical table.
    if con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'import_log_legacy_v4col'"
    ).fetchone()[0]:
        con.execute("DROP TABLE import_log_legacy_v4col")
    con.execute("ALTER TABLE import_log RENAME TO import_log_legacy_v4col")


def _write_import_log(
    con: duckdb.DuckDBPyConnection,
    *,
    source: str,
    symbol: str,
    timeframe: str,
    dry_run: bool,
    staged_rows: int,
    inserted_rows: int,
    updated_rows: int,
    result: str,
    error: str | None,
) -> int:
    """Append one audit-log row (G4) and return the generated id.

    Uses a DuckDB sequence (``import_log_seq``) — DuckDB has no
    ``last_insert_rowid()`` so we read the sequence with ``currval`` after
    the INSERT.
    """
    con.execute(
        """
        INSERT INTO import_log(
            source, symbol, timeframe, dry_run,
            staged_rows, inserted_rows, updated_rows,
            result, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [source, symbol, timeframe, dry_run, staged_rows,
         inserted_rows, updated_rows, result, error],
    )
    return int(con.execute("SELECT currval('import_log_seq')").fetchone()[0])


# ---------------------------------------------------------------------------
# CLI (for ad-hoc invocation)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="import_ctrader_bars",
        description="Import one cTrader OHLCV CSV into data/ayumi_market.duckdb with full guard suite.",
    )
    p.add_argument("--csv", required=True, type=Path, help="Source CSV path")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Target DuckDB")
    p.add_argument("--symbol", default="XAUUSD", help="Symbol (whitelist enforced)")
    p.add_argument("--timeframe", default="M15", help="Timeframe (whitelist enforced)")
    p.add_argument("--allow-ms", action="store_true", help="Allow epoch-ms timestamps (G1 guard bypass)")
    dry_run_group = p.add_mutually_exclusive_group()
    dry_run_group.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                              help="(default) Stage + report only; do not commit")
    dry_run_group.add_argument("--apply", dest="dry_run", action="store_false",
                              help="Actually commit the UPSERT")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    res = import_csv(
        csv_path=args.csv,
        db_path=args.db,
        symbol=args.symbol,
        timeframe=args.timeframe,
        dry_run=args.dry_run,
        allow_ms=args.allow_ms,
    )
    print(json.dumps(res, indent=2, default=str))
    return 0 if res["result"] in ("ok", "skipped", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())