"""Unit tests for scripts/import_ctrader_bars.py — card 291de428.

Covers the council binding guards (Kaito arch + Sora risk review 2026-10-04):

  Sora gate: ``to_epoch_seconds`` must convert a synthetic epoch-ms value
  to its seconds-scale equivalent. This is the loudest, most
  regression-prone part of the import path so it's pinned here as a
  permanent unit test.

  G1: timestamps above 1e11 raise ``ImportGuardError`` unless --allow-ms
        is passed. Range-check rejects dates outside 2020-01-01..today+1d.
  G6: hard whitelist (XAUUSD x {M3,M5,M15,M30,H1}) enforced at the top
        of ``import_csv``.
  G7: CSV header schema validation rejects files missing required cols.
  G3: pre-flight check rejects a mislabelled CSV whose earliest timestamp
        is more than one candle-width before the existing slice's earliest.
  G2: the UPSERT is idempotent — running it twice yields the same row count
        and the second run reports ``inserted=0 updated=N``.
  G4: every apply (and dry-run) writes an ``import_log`` row.

These tests are deliberately scoped to the importer module — they do NOT
touch ``data/ayumi_market.duckdb`` (use a tmp_path DuckDB for the end-to-end
check) and they do NOT run the live cTrader API.
"""

from __future__ import annotations

import csv
import datetime as _dt
import sys
from pathlib import Path

import duckdb
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from import_ctrader_bars import (  # noqa: E402
    ALLOWED_SYMBOLS,
    ALLOWED_TIMEFRAMES,
    EARLIEST_VALID_DATE,
    EPOCH_MS_THRESHOLD,
    ImportGuardError,
    ImportResult,
    _candle_seconds_for,
    _migrate_import_log,
    ensure_schema,
    import_csv,
    normalise_csv_timestamp,
    to_epoch_seconds,
    validate_csv_header,
)
import import_ctrader_bars  # noqa: E402  full module reference for migration tests


# ---------------------------------------------------------------------------
# Sora gate — the loudest, most pinned guard
# ---------------------------------------------------------------------------


class TestSoraGateEpochMsNormalization:
    """Pins the unit-normalisation contract (Sora gate).

    A synthetic epoch-ms row (2024-06-15 12:00 UTC) must normalise to
    1718452800 seconds when forced through the importer's normalisation
    path. This is what prevents the mixed-unit bug we already saw in
    production (GBPUSD still has 1.5M ms rows).
    """

    def test_synthetic_ms_value_normalises_to_seconds(self):
        # Pin the canonical "2024-06-15 12:00:00 UTC" date in epoch-seconds,
        # then derive the ms twin. Using datetime arithmetic instead of a
        # hard-coded number so the test stays correct across DST/TZ changes
        # (the canonical date is past the epoch).
        canonical_seconds = int(
            _dt.datetime(2024, 6, 15, 12, tzinfo=_dt.timezone.utc).timestamp()
        )
        ms_value = canonical_seconds * 1000
        # Sanity: ms value is above the guard threshold
        assert ms_value > EPOCH_MS_THRESHOLD
        # Normalise via the importer helper
        normalised = to_epoch_seconds(ms_value, allow_ms=True)
        assert normalised == canonical_seconds

    def test_ms_without_allow_ms_raises(self):
        canonical_seconds = int(
            _dt.datetime(2024, 6, 15, 12, tzinfo=_dt.timezone.utc).timestamp()
        )
        ms_value = canonical_seconds * 1000  # epoch-ms
        with pytest.raises(ImportGuardError, match="epoch-ms"):
            to_epoch_seconds(ms_value, allow_ms=False)

    def test_seconds_value_passes_through(self):
        # Same canonical date expressed in epoch-seconds.
        canonical_seconds = int(
            _dt.datetime(2024, 6, 15, 12, tzinfo=_dt.timezone.utc).timestamp()
        )
        assert to_epoch_seconds(canonical_seconds, allow_ms=False) == canonical_seconds

    def test_seconds_value_below_threshold(self):
        # 1e11 boundary sanity check
        assert to_epoch_seconds(999_999_999, allow_ms=False) == 999_999_999

    def test_negative_raises(self):
        with pytest.raises(ImportGuardError, match="negative"):
            to_epoch_seconds(-1, allow_ms=False)


# ---------------------------------------------------------------------------
# G1 — range-check + ms-guard at parse time
# ---------------------------------------------------------------------------


class TestNormaliseCsvTimestamp:
    def test_iso_with_time(self):
        # Self-compute the expected seconds-scale value so this test doesn't
        # rot when Python's TZDB shifts. The hardcoded form below would
        # silently start failing on non-UTC systems.
        expected = int(
            _dt.datetime(2026, 7, 13, 0, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
        )
        assert normalise_csv_timestamp("2026-07-13 00:00:00", allow_ms=False) == expected

    def test_iso_date_only(self):
        expected = int(
            _dt.datetime(2026, 7, 13, tzinfo=_dt.timezone.utc).timestamp()
        )
        assert normalise_csv_timestamp("2026-07-13", allow_ms=False) == expected

    def test_epoch_seconds_string(self):
        expected = int(
            _dt.datetime(2026, 7, 13, tzinfo=_dt.timezone.utc).timestamp()
        )
        assert normalise_csv_timestamp(str(expected), allow_ms=False) == expected

    def test_epoch_ms_string_rejected_by_default(self):
        canonical = int(
            _dt.datetime(2026, 7, 13, tzinfo=_dt.timezone.utc).timestamp()
        )
        with pytest.raises(ImportGuardError, match="epoch-ms"):
            normalise_csv_timestamp(str(canonical * 1000), allow_ms=False)

    def test_epoch_ms_string_allowed_when_flag(self):
        canonical = int(
            _dt.datetime(2026, 7, 13, tzinfo=_dt.timezone.utc).timestamp()
        )
        assert normalise_csv_timestamp(str(canonical * 1000), allow_ms=True) == canonical

    def test_empty_string_rejected(self):
        with pytest.raises(ImportGuardError, match="empty"):
            normalise_csv_timestamp("", allow_ms=False)

    def test_garbage_rejected(self):
        with pytest.raises(ImportGuardError, match="unparseable"):
            normalise_csv_timestamp("not-a-date", allow_ms=False)


# ---------------------------------------------------------------------------
# G7 — CSV header schema validation
# ---------------------------------------------------------------------------


class TestValidateCsvHeader:
    def _write_csv(self, tmp_path: Path, header: list[str]) -> Path:
        p = tmp_path / "XAUUSD_M15.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerow(["2026-07-13 00:00:00", "1800", "1810", "1795", "1805", "100"])
        return p

    def test_canonical_header_passes(self, tmp_path):
        p = self._write_csv(
            tmp_path, ["Date", "Open", "High", "Low", "Close", "Volume"]
        )
        validate_csv_header(p)  # must not raise

    def test_missing_volume_raises(self, tmp_path):
        p = self._write_csv(
            tmp_path, ["Date", "Open", "High", "Low", "Close"]
        )
        with pytest.raises(ImportGuardError, match="missing required columns"):
            validate_csv_header(p)

    def test_lowercase_header_passes(self, tmp_path):
        # Header is case-insensitive
        p = self._write_csv(
            tmp_path, ["date", "open", "high", "low", "close", "volume"]
        )
        validate_csv_header(p)

    def test_extra_columns_allowed(self, tmp_path):
        # ask_open / ask_high are OK; we don't require them.
        p = self._write_csv(
            tmp_path,
            ["Date", "Open", "High", "Low", "Close", "Volume", "ask_open"],
        )
        validate_csv_header(p)

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("")
        with pytest.raises(ImportGuardError, match="no header row"):
            validate_csv_header(p)


# ---------------------------------------------------------------------------
# G6 — hard whitelist at the import_csv entry point
# ---------------------------------------------------------------------------


class TestWhitelist:
    def test_symbol_whitelist_excludes_others(self):
        assert "EURUSD" not in ALLOWED_SYMBOLS
        assert "GBPUSD" not in ALLOWED_SYMBOLS
        assert "USDJPY" not in ALLOWED_SYMBOLS

    def test_timeframe_whitelist_excludes_htf(self):
        assert "H4" not in ALLOWED_TIMEFRAMES
        assert "D1" not in ALLOWED_TIMEFRAMES


class TestImportCsvRejectsOffWhitelist:
    def _csv(self, tmp_path: Path) -> Path:
        p = tmp_path / "EURUSD_M15.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            w.writerow(["2026-07-13 00:00:00", "1.10", "1.11", "1.09", "1.105", "100"])
        return p

    def test_eurusd_rejected(self, tmp_path):
        result = import_csv(
            csv_path=self._csv(tmp_path),
            db_path=tmp_path / "out.duckdb",
            symbol="EURUSD",
            timeframe="M15",
            dry_run=True,
            allow_ms=False,
        )
        assert result["result"] == "error"
        assert "not in hard whitelist" in result["error"]

    def test_h4_rejected(self, tmp_path):
        result = import_csv(
            csv_path=self._csv(tmp_path),
            db_path=tmp_path / "out.duckdb",
            symbol="XAUUSD",
            timeframe="H4",
            dry_run=True,
            allow_ms=False,
        )
        assert result["result"] == "error"
        assert "not in hard whitelist" in result["error"]


# ---------------------------------------------------------------------------
# G3 — pre-flight rejects mislabelled CSV
# ---------------------------------------------------------------------------


class TestPreflightRejectsMislabelled:
    def test_preflight_rejects_early_backfill(self, tmp_path):
        # Existing data starts at 2026-09-01, but the CSV claims 2026-01-01
        db_path = tmp_path / "ayumi.duckdb"
        con = duckdb.connect(str(db_path))
        con.execute(
            """
            CREATE TABLE bars(
                timestamp_utc BIGINT, symbol TEXT, timeframe TEXT,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                volume BIGINT, spread_pips DOUBLE, is_holdout BOOLEAN
            )
            """
        )
        con.execute(
            "CREATE UNIQUE INDEX idx_bars_unique ON bars(symbol, timeframe, timestamp_utc)"
        )
        # Seed existing data 2026-09-01 onwards (epoch-seconds derived)
        seed_ts = int(
            _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc).timestamp()
        )
        con.execute(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [seed_ts, "XAUUSD", "M15",
             1800.0, 1810.0, 1795.0, 1805.0, 100, 0.0, False],
        )
        con.close()

        # CSV claims to be M15 but contains a 2026-01-01 row (1754006400) which
        # is way before the existing earliest — pre-flight should reject.
        csv_path = tmp_path / "XAUUSD_M15.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            w.writerow(["2026-01-01 00:00:00", "1800", "1810", "1795", "1805", "100"])

        result = import_csv(
            csv_path=csv_path, db_path=db_path,
            symbol="XAUUSD", timeframe="M15",
            dry_run=False, allow_ms=False,
        )
        assert result["result"] == "error"
        assert "G3" in result["error"]


# ---------------------------------------------------------------------------
# G2 — idempotent UPSERT inside atomic txn
# ---------------------------------------------------------------------------


class TestIdempotentUpsert:
    def _good_csv(self, tmp_path: Path) -> Path:
        p = tmp_path / "XAUUSD_M15.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            w.writerow(["2026-09-15 00:00:00", "1800.0", "1810.0", "1795.0", "1805.0", "100"])
            w.writerow(["2026-09-15 00:15:00", "1805.0", "1812.0", "1803.0", "1810.0", "120"])
        return p

    def test_first_apply_inserts(self, tmp_path):
        db_path = tmp_path / "ayumi.duckdb"
        result = import_csv(
            csv_path=self._good_csv(tmp_path), db_path=db_path,
            symbol="XAUUSD", timeframe="M15",
            dry_run=False, allow_ms=False,
        )
        assert result["result"] == "ok"
        assert result["staged_rows"] == 2
        assert result["inserted_rows"] == 2
        assert result["updated_rows"] == 0

    def test_second_apply_is_idempotent(self, tmp_path):
        db_path = tmp_path / "ayumi.duckdb"
        first = import_csv(
            csv_path=self._good_csv(tmp_path), db_path=db_path,
            symbol="XAUUSD", timeframe="M15",
            dry_run=False, allow_ms=False,
        )
        assert first["result"] == "ok"
        assert first["inserted_rows"] == 2

        second = import_csv(
            csv_path=self._good_csv(tmp_path), db_path=db_path,
            symbol="XAUUSD", timeframe="M15",
            dry_run=False, allow_ms=False,
        )
        assert second["result"] == "ok"
        # All 2 are updates on second pass — nothing new inserted.
        assert second["inserted_rows"] == 0
        assert second["updated_rows"] == 2


# ---------------------------------------------------------------------------
# G4 — every run writes an import_log row
# ---------------------------------------------------------------------------


class TestImportLogRow:
    def test_dry_run_writes_log(self, tmp_path):
        db_path = tmp_path / "ayumi.duckdb"
        # Build a minimal CSV
        csv_path = tmp_path / "XAUUSD_M15.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            w.writerow(["2026-09-15 00:00:00", "1800", "1810", "1795", "1805", "100"])
        result = import_csv(
            csv_path=csv_path, db_path=db_path,
            symbol="XAUUSD", timeframe="M15",
            dry_run=True, allow_ms=False,
        )
        assert result["result"] == "ok"
        assert result["import_log_id"] is not None
        con = duckdb.connect(str(db_path), read_only=True)
        n = con.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]
        assert n == 1
        con.close()

    def test_apply_writes_log(self, tmp_path):
        db_path = tmp_path / "ayumi.duckdb"
        csv_path = tmp_path / "XAUUSD_M15.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            w.writerow(["2026-09-15 00:00:00", "1800", "1810", "1795", "1805", "100"])
        result = import_csv(
            csv_path=csv_path, db_path=db_path,
            symbol="XAUUSD", timeframe="M15",
            dry_run=False, allow_ms=False,
        )
        assert result["result"] == "ok"
        con = duckdb.connect(str(db_path), read_only=True)
        n = con.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]
        assert n == 1
        con.close()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_candle_seconds_for_known_timeframes():
    assert _candle_seconds_for("M3") == 180
    assert _candle_seconds_for("M5") == 300
    assert _candle_seconds_for("M15") == 900
    assert _candle_seconds_for("M30") == 1800
    assert _candle_seconds_for("H1") == 3600


def test_importresult_dataclass():
    r = ImportResult(symbol="XAUUSD", timeframe="M15", mode="apply", result="ok")
    d = r.to_dict() if hasattr(r, "to_dict") else r.as_dict()
    assert d["symbol"] == "XAUUSD"
    assert d["result"] == "ok"
    # Inserted/updated default to 0
    assert d["inserted_rows"] == 0


# ---------------------------------------------------------------------------
# G4 follow-up — _migrate_import_log (card 5ad1bfc3)
# ---------------------------------------------------------------------------


class TestMigrateImportLog:
    """Pins the schema-migration contract for the legacy 4-col ``import_log``
    table produced by ``scripts/init_tick_db.py``. Without this migration,
    ``_write_import_log`` raises ``BinderException`` because the canonical
    11-col INSERT references columns that the legacy table does not have.

    Migration policy (chosen option (a) variant, card notes):
        - Detect the legacy schema by the presence of the ``filename``
          column (which is NOT NULL PRIMARY KEY in the legacy schema).
        - Rename the legacy table to ``import_log_legacy_v4col`` to
          preserve historical tick-aggregation audit rows (the tick
          module currently queries by ``filename``).
        - Let ``CREATE TABLE IF NOT EXISTS import_log`` in
          ``ensure_schema`` mint the canonical 11-col table.

    Trade-off: scripts/import_ticks.py and scripts/aggregate_ticks_to_bars.py
    (NOT in this card's scope) still INSERT into ``import_log`` expecting
    4-col. They will need a separate follow-up to switch to the canonical
    schema (or to import_log_legacy_v4col). Documented in card notes.
    """

    def test_fresh_db_no_legacy_table(self):
        # _migrate_import_log on a DB with no import_log is a no-op
        con = duckdb.connect(":memory:")
        _migrate_import_log(con)
        # No tables at all yet (canonical CREATE happens in ensure_schema)
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        assert "import_log" not in tables
        assert "import_log_legacy_v4col" not in tables

    def test_legacy_4col_renamed_to_legacy_v4col(self):
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE import_log (
                filename VARCHAR NOT NULL PRIMARY KEY,
                symbol   VARCHAR,
                row_count BIGINT,
                imported_at VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO import_log VALUES "
            "('legacy1.csv', 'XAUUSD', 100, '2026-09-01T00:00:00Z'),"
            "('legacy2.csv', 'USDJPY', 200, '2026-09-02T00:00:00Z')"
        )
        # Run the full pipeline (ensure_schema --import +
        # _migrate_import_log + CREATE canonical)
        ensure_schema(con)
        cols = {
            r[0]
            for r in con.execute("DESCRIBE import_log").fetchall()
        }
        # Canonical 11-col schema
        assert "source" in cols
        assert "timeframe" in cols
        assert "staged_rows" in cols
        assert "result" in cols
        assert "filename" not in cols
        # Legacy preserved
        legacy_rows = con.execute(
            "SELECT COUNT(*) FROM import_log_legacy_v4col"
        ).fetchone()[0]
        assert legacy_rows == 2

    def test_canonical_schema_write_succeeds(self):
        # After migration, _write_import_log must succeed against the
        # canonical 11-col table (no BinderException).
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE import_log (
                filename VARCHAR NOT NULL PRIMARY KEY,
                symbol   VARCHAR,
                row_count BIGINT,
                imported_at VARCHAR
            )
            """
        )
        ensure_schema(con)
        log_id = import_ctrader_bars._write_import_log(
            con,
            source="ctrader_csv",
            symbol="XAUUSD",
            timeframe="M15",
            dry_run=False,
            staged_rows=10,
            inserted_rows=10,
            updated_rows=0,
            result="ok",
            error=None,
        )
        assert isinstance(log_id, int) and log_id > 0
        n = con.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]
        assert n == 1
        row = con.execute(
            "SELECT source, symbol, timeframe, dry_run, "
            "staged_rows, inserted_rows, result "
            "FROM import_log"
        ).fetchone()
        assert row[0] == "ctrader_csv"
        assert row[1] == "XAUUSD"
        assert row[2] == "M15"
        assert row[3] is False
        assert row[4] == 10
        assert row[5] == 10
        assert row[6] == "ok"

    def test_idempotent_double_migration(self):
        # Running ensure_schema twice must not corrupt legacy preservation.
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE import_log (
                filename VARCHAR NOT NULL PRIMARY KEY,
                symbol   VARCHAR,
                row_count BIGINT,
                imported_at VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO import_log VALUES "
            "('legacy1.csv', 'XAUUSD', 100, '2026-09-01T00:00:00Z')"
        )
        ensure_schema(con)
        ensure_schema(con)
        # Still preserved (only one legacy table, no churn)
        legacy_count = con.execute(
            "SELECT COUNT(*) FROM import_log_legacy_v4col"
        ).fetchone()[0]
        assert legacy_count == 1
        canonical_count = con.execute(
            "SELECT COUNT(*) FROM import_log"
        ).fetchone()[0]
        assert canonical_count == 0