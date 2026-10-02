"""SRF schema — DuckDB table definitions, single-writer lock, migrations."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — bump when tables change. Migrations are additive only.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# DDL — all tables created in a single transaction on first connect.
# ---------------------------------------------------------------------------

_DDL_STATEMENTS = [
    # ── strategies registry ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS strategies (
        name        VARCHAR PRIMARY KEY,
        version     VARCHAR NOT NULL DEFAULT '1.0',
        module_path VARCHAR NOT NULL,
        default_params JSON,
        status      VARCHAR NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'production', 'retired')),
        created_at  TIMESTAMPTZ DEFAULT now()
    )
    """,
    # ── runs (one per parameter set evaluation) ──────────────────────────
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id          VARCHAR PRIMARY KEY,
        strategy_name   VARCHAR NOT NULL REFERENCES strategies(name),
        pair            VARCHAR NOT NULL,
        timeframe       INTEGER NOT NULL,
        params_json     JSON,
        git_commit      VARCHAR,
        data_hash       VARCHAR,
        status          VARCHAR NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'failed')),
        compute_seconds DOUBLE,
        created_at      TIMESTAMPTZ DEFAULT now(),
        completed_at    TIMESTAMPTZ
    )
    """,
    # ── walk-forward windows ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS windows (
        run_id          VARCHAR NOT NULL REFERENCES runs(run_id),
        window_idx      INTEGER NOT NULL,
        train_start     TIMESTAMPTZ,
        train_end       TIMESTAMPTZ,
        test_start      TIMESTAMPTZ,
        test_end        TIMESTAMPTZ,
        win_rate        DOUBLE,
        profit_factor   DOUBLE,
        sharpe          DOUBLE,
        max_drawdown    DOUBLE,
        trade_count     INTEGER,
        total_pnl       DOUBLE,
        passed_go_nogo  BOOLEAN,
        PRIMARY KEY (run_id, window_idx)
    )
    """,
    # ── individual trades ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS trades (
        run_id      VARCHAR NOT NULL REFERENCES runs(run_id),
        window_idx  INTEGER NOT NULL,
        entry_time  TIMESTAMPTZ,
        exit_time   TIMESTAMPTZ,
        direction   VARCHAR,
        entry_price DOUBLE,
        exit_price  DOUBLE,
        pnl         DOUBLE,
        exit_reason VARCHAR
    )
    """,
    # ── Monte Carlo samples ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS monte_carlo_samples (
        run_id      VARCHAR NOT NULL REFERENCES runs(run_id),
        sample_idx  INTEGER NOT NULL,
        metric_name VARCHAR NOT NULL,
        metric_value DOUBLE,
        p5          DOUBLE,
        p95         DOUBLE,
        PRIMARY KEY (run_id, sample_idx, metric_name)
    )
    """,
    # ── metrics summary (one row per run) ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS metrics_summary (
        run_id              VARCHAR PRIMARY KEY REFERENCES runs(run_id),
        icir                DOUBLE,
        dsr                 DOUBLE,
        calmar              DOUBLE,
        sortino             DOUBLE,
        mean_win_rate       DOUBLE,
        std_win_rate        DOUBLE,
        mean_profit_factor  DOUBLE,
        std_profit_factor   DOUBLE,
        mean_sharpe         DOUBLE,
        std_sharpe          DOUBLE,
        mean_max_drawdown   DOUBLE,
        std_max_drawdown    DOUBLE,
        total_trades        INTEGER,
        windows_passed      INTEGER,
        windows_total       INTEGER,
        go_nogo             VARCHAR CHECK (go_nogo IN ('go', 'watch', 'no-go')),
        score               DOUBLE,
        param_stability_cv  DOUBLE,
        oos_sharpe_decay    DOUBLE
    )
    """,
    # ── cron sentinel (heartbeat monitoring) ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS cron_runs (
        cron_start  TIMESTAMPTZ,
        cron_end    TIMESTAMPTZ,
        exit_code   INTEGER,
        run_count   INTEGER,
        status      VARCHAR
    )
    """,
    # ── schema version tracking ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS _srf_schema_version (
        version     INTEGER PRIMARY KEY,
        applied_at  TIMESTAMPTZ DEFAULT now()
    )
    """,
    # ── study ledger (Optuna study metadata) ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS study_ledger (
        study_id    VARCHAR PRIMARY KEY,
        strategy    VARCHAR NOT NULL,
        pair        VARCHAR NOT NULL,
        timeframe   INTEGER NOT NULL,
        n_trials    INTEGER,
        started_at  TIMESTAMPTZ DEFAULT now(),
        completed_at TIMESTAMPTZ
    )
    """,
    # ── portfolio blend runs ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS portfolio_runs (
        run_id              VARCHAR PRIMARY KEY,
        configs_json        JSON,
        combined_wr         DOUBLE,
        combined_pf         DOUBLE,
        combined_sharpe     DOUBLE,
        monthly_trade_count  INTEGER,
        max_dd              DOUBLE,
        correlation_json    JSON,
        created_at          TIMESTAMPTZ DEFAULT now()
    )
    """,
]

# ---------------------------------------------------------------------------
# SQL views for reporting
# ---------------------------------------------------------------------------

_VIEW_STATEMENTS = [
    """
    CREATE OR REPLACE VIEW v_top_strategies AS
    SELECT
        r.strategy_name,
        r.pair,
        r.timeframe,
        m.dsr,
        m.mean_profit_factor AS pf,
        m.mean_win_rate AS wr,
        m.mean_max_drawdown AS max_dd,
        m.windows_passed,
        m.windows_total,
        m.go_nogo,
        m.score,
        r.created_at
    FROM runs r
    JOIN metrics_summary m USING (run_id)
    WHERE r.status = 'completed'
      AND m.total_trades >= 15
    ORDER BY COALESCE(m.dsr, 0) DESC
    """,
    """
    CREATE OR REPLACE VIEW v_pair_performance AS
    SELECT DISTINCT ON (pair)
        pair,
        strategy_name,
        timeframe,
        dsr,
        pf,
        go_nogo
    FROM v_top_strategies
    ORDER BY pair, COALESCE(dsr, 0) DESC
    """,
    """
    CREATE OR REPLACE VIEW v_promotion_queue AS
    SELECT
        strategy_name,
        pair,
        timeframe,
        dsr,
        pf,
        windows_passed,
        windows_total,
        score
    FROM v_top_strategies
    WHERE go_nogo = 'go'
    ORDER BY score DESC NULLS LAST
    """,
]


class SRFDatabase:
    """DuckDB connection wrapper with single-writer file lock and migrations."""

    def __init__(self, db_path: str = "data/research/research.duckdb"):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.db_path.with_suffix(".lock")
        self._lock_fd: int | None = None
        self._conn: duckdb.DuckDBPyConnection | None = None

    # ── lock management ──────────────────────────────────────────────────

    def acquire_lock(self, timeout: float = 5.0) -> None:
        """Acquire exclusive file lock. Raises RuntimeError if locked."""
        deadline = time.monotonic() + timeout
        self._lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() > deadline:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                    raise RuntimeError(  # noqa: B904
                        f"SRF database is locked by another writer: {self._lock_path}"
                    )
                time.sleep(0.1)

    def release_lock(self) -> None:
        """Release the file lock."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._lock_fd)
            self._lock_fd = None

    # ── connection ───────────────────────────────────────────────────────

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Acquire lock and open DuckDB connection. Runs migrations if needed."""
        if self._conn is not None:
            return self._conn
        self.acquire_lock()
        self._conn = duckdb.connect(str(self.db_path))
        self._migrate()
        return self._conn

    def close(self) -> None:
        """Close connection and release lock."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self.release_lock()

    # ── context manager ──────────────────────────────────────────────────

    def __enter__(self) -> duckdb.DuckDBPyConnection:
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── migrations ───────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Run additive migrations. No destructive changes."""
        assert self._conn is not None  # noqa: S101 — invariant; `_migrate()` is only called from the context-manager `connect()` path which guarantees `_conn` is set, intentionally silenced under `python -O`
        cur = self._conn.cursor()

        # Check current version
        try:
            cur.execute("SELECT MAX(version) FROM _srf_schema_version")
            row = cur.fetchone()
            current = row[0] if row and row[0] is not None else 0
        except duckdb.Error:
            # _srf_schema_version doesn't exist yet — fresh DB
            current = 0

        if current >= SCHEMA_VERSION:
            logger.debug("SRF schema at version %d, no migration needed", current)
            return

        # Run all DDL (IF NOT EXISTS makes this idempotent)
        for stmt in _DDL_STATEMENTS:
            cur.execute(stmt)

        # Create views
        for stmt in _VIEW_STATEMENTS:
            cur.execute(stmt)

        # Record version
        cur.execute(
            "INSERT INTO _srf_schema_version (version) VALUES (?)",
            [SCHEMA_VERSION],
        )
        self._conn.commit()
        logger.info("SRF schema migrated to version %d", SCHEMA_VERSION)

    # ── backup ───────────────────────────────────────────────────────────

    def backup(self) -> Path:
        """Copy DB to .bak. Must be called while connected."""
        import shutil

        bak = self.db_path.with_suffix(".duckdb.bak")
        # DuckDB CHECKPOINT ensures all data is flushed to disk
        if self._conn:
            self._conn.execute("CHECKPOINT")
        shutil.copy2(self.db_path, bak)
        logger.info("SRF DB backed up to %s", bak)
        return bak


def compute_data_hash(file_path: str | Path) -> str:
    """SHA256 of input data file for provenance tracking."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]  # 16 chars is plenty for dedup


def generate_run_id(strategy: str, pair: str, timeframe: int) -> str:
    """Deterministic-ish run ID: strategy_pair_tf_timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{strategy}_{pair}_{timeframe}m_{ts}"
