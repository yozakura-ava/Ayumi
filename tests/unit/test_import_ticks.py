"""Unit tests for scripts/import_ticks.py — card 0721ec62 (5ad1bfc3 follow-up).

Pins the post-migration audit-table redirect:

  * After 5ad1bfc3 merged, ``import_log`` is the canonical 11-col table
    owned by ``scripts/import_ctrader_bars.py``. The legacy 4-col shape
    (filename, symbol, row_count, imported_at) was renamed to
    ``import_log_legacy_v4col``.
  * ``import_ticks.import_csv()`` must read (dedup) and write into the
    legacy 4-col table — NOT the canonical 11-col one — so that:
      - The filename dedup check still works (canonical import_log has
        no ``filename`` column).
      - Historical tick-import audit rows stay in the legacy table they
        were always written to.

These tests construct a fresh DuckDB, run the legacy ``init_tick_db.py``
flow, then run ``ensure_schema()`` from ``import_ctrader_bars`` (which
calls ``_migrate_import_log`` internally, renaming the legacy table to
``import_log_legacy_v4col`` and minting the canonical 11-col
``import_log``). Only then do we exercise ``import_csv`` and assert no
CatalogError and the audit row landed in the right table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import import_ctrader_bars  # noqa: E402
import import_ticks  # noqa: E402
import init_tick_db  # noqa: E402


def _bootstrap_post_migration_db(db_path: Path) -> None:
    """Mirror the production flow: init_tick_db (legacy 4-col) → ensure_schema
    (migrates to legacy_v4col + canonical 11-col). Returns the DB ready for
    tick-script tests.
    """
    # Step 1: legacy init — produces the 4-col import_log init_tick_db.py
    # has always produced. This is what historical tick imports wrote
    # against.
    init_tick_db.init_db(db_path)

    con = duckdb.connect(str(db_path))
    try:
        # Sanity: legacy 4-col exists pre-migration.
        cols = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'import_log' ORDER BY ordinal_position"
        ).fetchall()
        assert [c[0] for c in cols] == [
            "filename",
            "symbol",
            "row_count",
            "imported_at",
        ], f"unexpected pre-migration import_log columns: {cols}"
    finally:
        con.close()

    # Step 2: apply 5ad1bfc3 migration by calling ensure_schema() — this
    # invokes _migrate_import_log (renames legacy → legacy_v4col) and
    # mints the canonical 11-col import_log. ensure_schema takes a
    # connection, not a path.
    con = duckdb.connect(str(db_path))
    try:
        import_ctrader_bars.ensure_schema(con)
    finally:
        con.close()


def _write_synthetic_csv(csv_path: Path, symbol: str = "XAUUSD", n_rows: int = 5) -> None:
    """Write a minimal Dukascopy-format tick CSV (timestamp, instrument,
    bid, ask, bidVol, askVol). Timestamps are in epoch milliseconds.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    base_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z, arbitrary
    lines = ["timestamp,instrument,bid,ask,bidVol,askVol"]
    for i in range(n_rows):
        ts = base_ms + i * 1_000  # 1-second spacing
        bid = 2000.00 + i * 0.10
        ask = bid + 0.30
        lines.append(f"{ts},{symbol},{bid:.2f},{ask:.2f},1.0,1.0")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestImportTicksPostMigrationRedirect:
    """Card 0721ec62 acceptance: import_csv routes to import_log_legacy_v4col."""

    def test_legacy_table_exists_after_migration(self, tmp_path: Path) -> None:
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)

        con = duckdb.connect(str(db_path), read_only=True)
        try:
            tables = sorted(
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'"
                ).fetchall()
            )
            assert "import_log" in tables
            assert "import_log_legacy_v4col" in tables

            # Canonical 11-col has no `filename` column — that's exactly
            # the bug this card is fixing. Pre-fix, import_csv would
            # raise CatalogError trying to SELECT filename from this
            # table.
            canonical_cols = {
                r[0]
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'import_log'"
                ).fetchall()
            }
            assert "filename" not in canonical_cols
        finally:
            con.close()

    def test_import_csv_writes_audit_row_to_legacy_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)
        staging = tmp_path / "staging"
        csv_path = staging / "XAUUSD_20231114.csv"
        _write_synthetic_csv(csv_path, symbol="XAUUSD", n_rows=5)

        # Exercise the import path. keep_csv=True so the synthetic CSV
        # is left in place for the dedup test below.
        con = duckdb.connect(str(db_path))
        try:
            inserted = import_ticks.import_csv(con, csv_path, keep_csv=True)
            assert inserted == 5

            # Audit row must be in import_log_legacy_v4col — the legacy
            # 4-col shape. If the fix is regressed, this query throws
            # CatalogError against the canonical import_log (no
            # `filename` column).
            rows = con.execute(
                "SELECT filename, symbol, row_count FROM import_log_legacy_v4col "
                "WHERE filename = ?",
                [csv_path.name],
            ).fetchall()
            assert rows is not None  # narrow duckdb's Optional return for mypy
            assert len(rows) == 1
            row = rows[0]
            assert row is not None  # narrow element type for mypy
            filename, symbol, row_count = row
            assert filename == "XAUUSD_20231114.csv"
            assert symbol == "XAUUSD"
            assert row_count == 5

            # Canonical import_log must NOT contain this row — keeps the
            # tick-script audit isolated from the bars audit pipeline.
            canon_row = con.execute(
                "SELECT count(*) FROM import_log WHERE source = ? OR symbol = ?",
                ["XAUUSD ticks CSV", "XAUUSD"],
            ).fetchone()
            assert canon_row is not None  # narrow duckdb's Optional return for mypy
            canon_count = canon_row[0]
            assert canon_count == 0

            # And the ticks themselves landed in the canonical ticks
            # table — that part of import_csv was never broken.
            tick_row = con.execute(
                "SELECT count(*) FROM ticks WHERE symbol = ?", ["XAUUSD"]
            ).fetchone()
            assert tick_row is not None  # narrow duckdb's Optional return for mypy
            tick_count = tick_row[0]
            assert tick_count == 5
        finally:
            con.close()

    def test_dedup_via_filename_reads_legacy_table(self, tmp_path: Path) -> None:
        """Regression pin for import_ticks.py L45 dedup SELECT — also
        routes through the legacy table post-migration."""
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)
        staging = tmp_path / "staging"
        csv_path = staging / "XAUUSD_20231114.csv"
        _write_synthetic_csv(csv_path, n_rows=5)

        con = duckdb.connect(str(db_path))
        try:
            # First import — actually inserts.
            first = import_ticks.import_csv(con, csv_path, keep_csv=True)
            assert first == 5

            # Second import — must skip via the legacy dedup SELECT,
            # not raise CatalogError.
            second = import_ticks.import_csv(con, csv_path, keep_csv=True)
            assert second == 0

            # Legacy table has exactly one row for this filename
            # (UNIQUE on filename).
            audit_row = con.execute(
                "SELECT count(*) FROM import_log_legacy_v4col "
                "WHERE filename = ?",
                [csv_path.name],
            ).fetchone()
            assert audit_row is not None  # narrow duckdb's Optional return for mypy
            n_audit = audit_row[0]
            assert n_audit == 1
        finally:
            con.close()
