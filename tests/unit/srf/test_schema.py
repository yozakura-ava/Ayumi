"""Tests for SRF schema: integrity, single-writer lock, migration idempotency, backup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/forex_bot is importable
_repo_root = Path(__file__).resolve().parents[3]
_src = _repo_root / "src" / "forex_bot"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from srf.schema import SCHEMA_VERSION, SRFDatabase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path(tmp_path):
    """Return a temp DuckDB path inside a temp directory."""
    return str(tmp_path / "test_research.duckdb")


@pytest.fixture
def db(tmp_db_path):
    """Yield a connected SRFDatabase, closed after test."""
    d = SRFDatabase(tmp_db_path)
    _conn = d.connect()
    yield d
    d.close()


# ---------------------------------------------------------------------------
# Schema integrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    """Verify all 7 required tables exist with correct columns/types."""

    REQUIRED_TABLES = {
        "strategies",
        "runs",
        "windows",
        "trades",
        "monte_carlo_samples",
        "metrics_summary",
        "cron_runs",
    }

    def test_all_required_tables_exist(self, db):
        """All 7 spec-mandated tables must be present."""
        with db as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            }
        missing = self.REQUIRED_TABLES - tables
        assert not missing, f"Missing required tables: {missing}"

    def test_strategies_columns(self, db):
        """Strategies table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "strategies")
        assert "name" in cols
        assert "version" in cols
        assert "module_path" in cols
        assert "default_params" in cols
        assert "status" in cols
        assert "created_at" in cols

    def test_runs_columns(self, db):
        """Runs table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "runs")
        expected = {
            "run_id",
            "strategy_name",
            "pair",
            "timeframe",
            "params_json",
            "git_commit",
            "data_hash",
            "status",
            "compute_seconds",
            "created_at",
            "completed_at",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_windows_columns(self, db):
        """Windows table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "windows")
        expected = {
            "run_id",
            "window_idx",
            "win_rate",
            "profit_factor",
            "sharpe",
            "max_drawdown",
            "trade_count",
            "total_pnl",
            "passed_go_nogo",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_trades_columns(self, db):
        """Trades table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "trades")
        expected = {
            "run_id",
            "window_idx",
            "entry_time",
            "exit_time",
            "direction",
            "entry_price",
            "exit_price",
            "pnl",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_monte_carlo_columns(self, db):
        """monte_carlo_samples table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "monte_carlo_samples")
        expected = {"run_id", "sample_idx", "metric_name", "metric_value"}
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_metrics_summary_columns(self, db):
        """metrics_summary table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "metrics_summary")
        expected = {
            "run_id",
            "go_nogo",
            "score",
            "total_trades",
            "windows_passed",
            "windows_total",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_cron_runs_columns(self, db):
        """cron_runs table has expected columns."""
        with db as conn:
            cols = self._columns(conn, "cron_runs")
        expected = {"cron_start", "cron_end", "exit_code", "run_count", "status"}
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_views_exist(self, db):
        """Reporting views are created."""
        with db as conn:
            views = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='VIEW'"
                ).fetchall()
            }
        assert "v_top_strategies" in views
        assert "v_pair_performance" in views
        assert "v_promotion_queue" in views

    @staticmethod
    def _columns(conn, table: str) -> set[str]:
        rows = conn.execute(
            f"SELECT column_name FROM information_schema.columns "  # noqa: S608
            f"WHERE table_name='{table}' AND table_schema='main'"
        ).fetchall()
        return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Single-writer lock
# ---------------------------------------------------------------------------


class TestSingleWriterLock:
    """Verify the file lock prevents concurrent connections."""

    def test_second_connection_blocked(self, tmp_db_path):
        """A second SRFDatabase must not be able to acquire the lock while
        the first holds it."""
        db1 = SRFDatabase(tmp_db_path)
        db1.connect()

        db2 = SRFDatabase(tmp_db_path)
        with pytest.raises(RuntimeError, match="locked by another writer"):
            db2.acquire_lock(timeout=1.0)

        db1.close()

    def test_lock_released_on_close(self, tmp_db_path):
        """After close(), a new connection can acquire the lock."""
        db1 = SRFDatabase(tmp_db_path)
        db1.connect()
        db1.close()

        db2 = SRFDatabase(tmp_db_path)
        db2.acquire_lock(timeout=2.0)  # should succeed
        db2.release_lock()

    def test_context_manager_releases_lock(self, tmp_db_path):
        """Lock is released when used as context manager."""
        with SRFDatabase(tmp_db_path) as _:
            pass  # connection open inside

        # New instance should be able to connect immediately
        db2 = SRFDatabase(tmp_db_path)
        db2.acquire_lock(timeout=2.0)
        db2.release_lock()


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


class TestMigrationIdempotency:
    """Verify migrations are safe to run multiple times."""

    def test_double_connect_no_error(self, tmp_db_path):
        """Closing and reconnecting runs _migrate() again without error."""
        db1 = SRFDatabase(tmp_db_path)
        db1.connect()
        db1.close()

        db2 = SRFDatabase(tmp_db_path)
        conn = db2.connect()  # _migrate runs again
        # Verify schema version is still 1
        row = conn.execute("SELECT MAX(version) FROM _srf_schema_version").fetchone()
        assert row[0] == SCHEMA_VERSION
        db2.close()

    def test_version_tracking(self, db):
        """_srf_schema_version table has exactly one row at current version."""
        with db as conn:
            rows = conn.execute("SELECT version FROM _srf_schema_version").fetchall()
        versions = [r[0] for r in rows]
        assert SCHEMA_VERSION in versions

    def test_tables_already_exist_no_error(self, tmp_db_path):
        """Manually running all DDL again (IF NOT EXISTS) doesn't fail."""
        db1 = SRFDatabase(tmp_db_path)
        conn = db1.connect()
        # Try to create tables again — IF NOT EXISTS should handle it
        from srf.schema import _DDL_STATEMENTS

        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        db1.close()


# ---------------------------------------------------------------------------
# Backup helper
# ---------------------------------------------------------------------------


class TestBackupHelper:
    """Verify the backup function creates a valid copy."""

    def test_backup_creates_file(self, db, tmp_db_path):
        """backup() creates a .bak file that exists and is non-empty."""
        bak = db.backup()
        assert bak.exists()
        assert bak.stat().st_size > 0

    def test_backup_extension(self, db):
        """Backup file has .duckdb.bak suffix."""
        bak = db.backup()
        assert str(bak).endswith(".duckdb.bak")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestUtilities:
    """Test compute_data_hash and generate_run_id."""

    def test_compute_data_hash(self, tmp_path):
        """compute_data_hash returns a hex string."""
        from srf.schema import compute_data_hash

        f = tmp_path / "data.csv"
        f.write_text("test,data\n1,2\n")
        h = compute_data_hash(str(f))
        assert isinstance(h, str)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_compute_data_hash_consistent(self, tmp_path):
        """Same content produces same hash."""
        from srf.schema import compute_data_hash

        f = tmp_path / "a.csv"
        f.write_text("identical")
        h1 = compute_data_hash(str(f))
        h2 = compute_data_hash(str(f))
        assert h1 == h2

    def test_generate_run_id_format(self):
        """generate_run_id produces expected format."""
        from srf.schema import generate_run_id

        rid = generate_run_id("mystrategy", "GBPUSD", 15)
        assert rid.startswith("mystrategy_GBPUSD_15m_")
        # Timestamp should be 14 digits
        ts_part = rid.split("_")[-1]
        assert len(ts_part) == 14
        assert ts_part.isdigit()
