"""Unit tests for scripts/aggregate_ticks_to_bars.py — card 0721ec62 (5ad1bfc3 follow-up).

Pins the post-migration audit-table redirect for the bar-aggregation path.

  * After 5ad1bfc3, ``import_log`` is the canonical 11-col table owned by
    ``scripts/import_ctrader_bars.py`` and the legacy 4-col shape lives
    in ``import_log_legacy_v4col``.
  * ``aggregate_ticks_to_bars.aggregate_symbol_timeframe()`` must write
    its audit row to ``import_log_legacy_v4col`` (4-col shape, dedup on
    ``filename`` where the row's filename is ``tick_aggregation:<symbol>:<tf>``).
  * Pre-fix, this raised CatalogError because the canonical import_log
    has no ``filename`` column.

These tests construct a fresh DuckDB with the post-5ad-migration
schema, populate a handful of ticks for a single symbol, then exercise
``aggregate_symbol_timeframe`` and assert no CatalogError and the audit
row landed in ``import_log_legacy_v4col``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import aggregate_ticks_to_bars as agg  # noqa: E402
import import_ctrader_bars  # noqa: E402
import init_tick_db  # noqa: E402


def _bootstrap_post_migration_db(db_path: Path) -> None:
    """Mirror the production flow: init_tick_db (legacy 4-col) → ensure_schema
    (migrates to legacy_v4col + canonical 11-col). ensure_schema takes a
    connection, not a path."""
    init_tick_db.init_db(db_path)
    con = duckdb.connect(str(db_path))
    try:
        import_ctrader_bars.ensure_schema(con)
    finally:
        con.close()


def _seed_ticks(db_path: Path, symbol: str = "XAUUSD", n_ticks: int = 60) -> None:
    """Insert a small batch of synthetic ticks spanning 1 minute (1 tick/sec)."""
    con = duckdb.connect(str(db_path))
    try:
        base_ms = 1_700_000_000_000  # 2023-11-14T22:13:20Z
        con.executemany(
            "INSERT INTO ticks (timestamp_ms, symbol, bid, ask, bid_vol, ask_vol) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (base_ms + i * 1_000, symbol, 2000.0 + i * 0.01, 2000.3 + i * 0.01, 1.0, 1.0)
                for i in range(n_ticks)
            ],
        )
    finally:
        con.close()


class TestAggregateTicksToBarsPostMigrationRedirect:
    """Card 0721ec62 acceptance: aggregate_symbol_timeframe routes to
    import_log_legacy_v4col."""

    def test_aggregate_writes_audit_row_to_legacy_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)
        _seed_ticks(db_path, symbol="XAUUSD", n_ticks=60)

        con = duckdb.connect(str(db_path))
        try:
            bars_created = agg.aggregate_symbol_timeframe(
                con, "XAUUSD", "M1", force=True
            )
            assert bars_created >= 1

            # Audit row must land in import_log_legacy_v4col with the
            # synthetic filename "tick_aggregation:XAUUSD:M1".
            rows = con.execute(
                "SELECT filename, symbol, row_count FROM import_log_legacy_v4col "
                "WHERE filename = ?",
                ["tick_aggregation:XAUUSD:M1"],
            ).fetchall()
            assert rows is not None  # narrow duckdb's Optional return for mypy
            assert len(rows) == 1
            row = rows[0]
            assert row is not None  # narrow element type for mypy
            filename, symbol, row_count = row
            assert filename == "tick_aggregation:XAUUSD:M1"
            assert symbol == "XAUUSD"
            assert row_count == bars_created

            # Canonical import_log must NOT carry this row — the bars
            # audit pipeline (import_ctrader_bars) owns that table.
            canon_row = con.execute(
                "SELECT count(*) FROM import_log"
            ).fetchone()
            assert canon_row is not None  # narrow duckdb's Optional return for mypy
            canon_count = canon_row[0]
            assert canon_count == 0
        finally:
            con.close()

    def test_aggregate_does_not_raise_catalog_error(self, tmp_path: Path) -> None:
        """Regression pin: pre-fix, this raised CatalogError because
        import_log had no `filename` column post-5ad-migration."""
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)
        _seed_ticks(db_path, symbol="XAUUSD", n_ticks=10)

        con = duckdb.connect(str(db_path))
        try:
            # Must not raise.
            agg.aggregate_symbol_timeframe(con, "XAUUSD", "M1", force=True)
        finally:
            con.close()


class TestAggregateGetAggregatedHelper:
    """Genuine coverage for aggregate_ticks_to_bars.get_aggregated() —
    also wires a previously-unwired helper that the builder QG flags.
    """

    def test_get_aggregated_empty_when_no_bars(self, tmp_path: Path) -> None:
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)

        con = duckdb.connect(str(db_path))
        try:
            pairs = agg.get_aggregated(con)
            assert pairs == []
        finally:
            con.close()

    def test_get_aggregated_returns_pairs_after_aggregation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "market.duckdb"
        _bootstrap_post_migration_db(db_path)
        _seed_ticks(db_path, symbol="XAUUSD", n_ticks=60)

        con = duckdb.connect(str(db_path))
        try:
            agg.aggregate_symbol_timeframe(con, "XAUUSD", "M1", force=True)
            pairs = agg.get_aggregated(con)
            assert pairs == [("XAUUSD", "M1")]
        finally:
            con.close()
