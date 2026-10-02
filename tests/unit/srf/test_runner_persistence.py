"""Tests for SRF runner persistence: windows, trades, metrics_summary writes.

Regression target: d5e8ca8d — ``SRF runner: persist per-window + per-trade data``

Before the fix the runner wrote ``INSERT INTO windows`` only, and ``INSERT INTO
trades`` was missing entirely. Production research DB (as of 2026-07-13) has
``windows=0, trades=0`` while ``runs=63, metrics_summary=63`` — the metrics
summary path commits but the per-window and per-trade inserts silently fail
when the process gets terminated before DuckDB's autocommit flushes.

These tests exercise ``StrategyRunner._write_results`` directly against a
temp DB so we can assert exact row counts on every code path:
- happy path: windows + trades + summary all written in one transaction;
- empty trade records: trades table empty, no error;
- invalid trade records: skipped, valid rows preserved;
- failure during writes: transaction rolled back, no partial state.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure src/forex_bot is importable
_repo_root = Path(__file__).resolve().parents[3]
_src = _repo_root / "src" / "forex_bot"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from quant.walk_forward import (  # noqa: I001
    AggregatedMetrics,
    WalkForwardResults,
    WindowMetrics,
)
from srf.runner import StrategyRunner
from srf.schema import SRFDatabase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path(tmp_path):
    """Per-test isolated DuckDB inside pytest's tmp_path."""
    return str(tmp_path / "runner_persist.duckdb")


@pytest.fixture
def db(tmp_db_path):
    """Open + yield a connected SRFDatabase, always close at teardown."""
    d = SRFDatabase(tmp_db_path)
    d.connect()
    try:
        yield d
    finally:
        d.close()


@pytest.fixture
def seeded_db(db):
    """DB with one strategy and one 'running' run pre-inserted for FK targets."""
    with db as conn:
        conn.execute(
            "INSERT INTO strategies (name, version, module_path, status) "
            "VALUES ('mock_strat', '1.0', 'mock.module.MockStrategy', 'production')"
        )
        conn.execute(
            """INSERT INTO runs
               (run_id, strategy_name, pair, timeframe, params_json,
                git_commit, data_hash, status, created_at)
               VALUES ('mock_run_001', 'mock_strat', 'GBPUSD', 15,
                       '{}', 'deadbeef', 'abc123', 'running', now())"""
        )
        conn.execute(
            """INSERT INTO runs
               (run_id, strategy_name, pair, timeframe, params_json,
                git_commit, data_hash, status, created_at)
               VALUES ('mock_run_002', 'mock_strat', 'EURUSD', 15,
                       '{}', 'deadbeef', 'abc123', 'running', now())"""
        )
    return db


def make_window(idx: int, *, go: bool = True) -> WindowMetrics:
    """Build a synthetic WindowMetrics for tests."""
    return WindowMetrics(
        window_index=idx,
        win_rate=0.55 + idx * 0.01,
        profit_factor=1.30 + idx * 0.05,
        max_drawdown=0.04 + idx * 0.005,
        sharpe_ratio=1.20 + idx * 0.10,
        trade_count=20 + idx,
        total_pnl=150.0 * (idx + 1),
        passed_go_nogo=go,
        regime_volatility="normal",
        regime_trend="up",
        regime_session="london",
        regime_combined="trending",
        regime_quality=0.7,
        btc_regime="neutral",
    )


def make_results(
    n_windows: int = 5,
    *,
    n_trades: int = 8,
    go: bool = True,
    with_dates: bool = True,
) -> WalkForwardResults:
    """Build a synthetic WalkForwardResults with trade records attached.

    When ``with_dates=True`` (default), attaches ``_window_dates`` sidecar
    with synthetic datetime boundaries so the date-column INSERT path is
    exercised.
    """
    from datetime import datetime, timezone
    from datetime import timedelta as td

    per_window = [make_window(i, go=(i % 2 == 0)) for i in range(n_windows)]
    agg = AggregatedMetrics(
        mean_win_rate=0.56,
        std_win_rate=0.05,
        mean_profit_factor=1.4,
        std_profit_factor=0.1,
        mean_max_drawdown=0.05,
        std_max_drawdown=0.01,
        mean_sharpe_ratio=1.4,
        std_sharpe_ratio=0.2,
        mean_trade_count=22.0,
        std_trade_count=2.0,
        mean_total_pnl=400.0,
        std_total_pnl=200.0,
        windows_passed=sum(1 for w in per_window if w.passed_go_nogo),
        total_windows=n_windows,
    )
    results = WalkForwardResults(
        per_window=per_window,
        aggregated=agg,
        go_nogo=go,
    )
    # Round-robin window assignment to exercise multi-window trade persistence.
    directions = ["long", "short", "long", "short"]
    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    results._trade_records = [
        {
            "pnl": 5.0 + idx,
            "direction": directions[idx % len(directions)],
            "window_id": idx % n_windows,
            "rationale": f"trade_{idx}",
            "confidence_score": 0.6 + (idx % 5) * 0.05,
            "entry_time": base_time + td(hours=idx),
            "exit_time": base_time + td(hours=idx + 1),
            "entry_price": 1.0850 + idx * 0.001,
            "exit_price": 1.0860 + idx * 0.001,
        }
        for idx in range(n_trades)
    ]
    # Window dates sidecar — matches walk-forward split boundaries
    if with_dates:
        results._window_dates = [
            {
                "window_index": i,
                "train_start": base_time + td(days=i * 30),
                "train_end": base_time + td(days=i * 30 + 21),
                "test_start": base_time + td(days=i * 30 + 25),
                "test_end": base_time + td(days=i * 30 + 30),
            }
            for i in range(n_windows)
        ]
    return results


# ---------------------------------------------------------------------------
# Happy path — windows + trades + metrics_summary in a single transaction
# ---------------------------------------------------------------------------


class TestRunnerPersistenceHappyPath:
    """The default happy-path: every table gets rows and they match the input."""

    def test_windows_table_populated(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """``_write_results`` must insert one row per WindowMetrics."""
        results = make_results(n_windows=5, n_trades=0)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            count = conn.execute("SELECT COUNT(*) FROM windows WHERE run_id='mock_run_001'").fetchone()[0]
        assert count == 5, f"Expected 5 window rows, got {count}"

    def test_trades_table_populated(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """``_write_results`` must insert one row per ``_trade_records`` entry."""
        results = make_results(n_windows=5, n_trades=8)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            count = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id='mock_run_001'").fetchone()[0]
        assert count == 8, f"Expected 8 trade rows, got {count}"

    def test_metrics_summary_still_written(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """metrics_summary insert is preserved (no regression on existing path)."""
        results = make_results(n_windows=5, n_trades=0, go=True)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            row = conn.execute(
                """SELECT run_id, total_trades, windows_passed,
                          windows_total, go_nogo
                   FROM metrics_summary WHERE run_id='mock_run_001'"""
            ).fetchone()
        assert row is not None, "metrics_summary row was not inserted"
        run_id, total_trades, windows_passed, windows_total, go_nogo = row
        assert run_id == "mock_run_001"
        # 3 of 5 windows have passed_go_nogo=True (even indexes)
        assert windows_passed == 3
        assert windows_total == 5
        assert go_nogo == "go"
        # trade_count from per_window = 20+0+1+2+3+4 = 110
        assert total_trades == 110

    def test_full_persistence_atomic(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """All three tables populated from a single successful call."""
        results = make_results(n_windows=5, n_trades=12)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            counts = {
                t: conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE run_id='mock_run_001'"  # noqa: S608
                ).fetchone()[0]
                for t in ("windows", "trades", "metrics_summary")
            }
        assert counts == {
            "windows": 5,
            "trades": 12,
            "metrics_summary": 1,
        }


# ---------------------------------------------------------------------------
# Multi-run isolation
# ---------------------------------------------------------------------------


class TestMultiRunIsolation:
    """Two different runs must not bleed rows into each other."""

    def test_two_runs_independent(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """Run 001 and 002 each get their own windows/trades, no cross-pollination."""
        runner = StrategyRunner(db_path=tmp_db_path)

        results_001 = make_results(n_windows=5, n_trades=8)
        results_002 = make_results(n_windows=3, n_trades=4, go=False)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results_001)
            runner._write_results(conn, "mock_run_002", results_002)

        with seeded_db as conn:
            rows_001 = conn.execute("SELECT COUNT(*) FROM windows WHERE run_id='mock_run_001'").fetchone()[0]
            rows_002 = conn.execute("SELECT COUNT(*) FROM windows WHERE run_id='mock_run_002'").fetchone()[0]
            trades_001 = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id='mock_run_001'").fetchone()[0]
            trades_002 = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id='mock_run_002'").fetchone()[0]
        assert rows_001 == 5
        assert rows_002 == 3
        assert trades_001 == 8
        assert trades_002 == 4

    def test_metrics_summary_per_run(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """Each run gets exactly one metrics_summary row."""
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", make_results(n_windows=5))
            runner._write_results(conn, "mock_run_002", make_results(n_windows=3))

        with seeded_db as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM metrics_summary WHERE run_id IN ('mock_run_001', 'mock_run_002')"
            ).fetchone()[0]
        assert n == 2


# ---------------------------------------------------------------------------
# Defensive handling of bad / missing trade records
# ---------------------------------------------------------------------------


class TestTradeRecordDefensive:
    """Trade-record defects must not block the windows/summary write."""

    def test_missing_trade_records_attribute(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """A bare WalkForwardResults (no ``_trade_records``) writes 0 trades."""
        # Plain WalkForwardResults - no _trade_records attached.
        results = WalkForwardResults(
            per_window=[make_window(i) for i in range(3)],
            aggregated=AggregatedMetrics(
                mean_win_rate=0.5,
                std_win_rate=0.0,
                mean_profit_factor=1.0,
                std_profit_factor=0.0,
                mean_max_drawdown=0.0,
                std_max_drawdown=0.0,
                mean_sharpe_ratio=1.0,
                std_sharpe_ratio=0.0,
                mean_trade_count=20.0,
                std_trade_count=0.0,
                mean_total_pnl=0.0,
                std_total_pnl=0.0,
                windows_passed=2,
                total_windows=3,
            ),
            go_nogo=False,
        )
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            trades = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id='mock_run_001'").fetchone()[0]
            windows = conn.execute("SELECT COUNT(*) FROM windows WHERE run_id='mock_run_001'").fetchone()[0]
            summary = conn.execute("SELECT COUNT(*) FROM metrics_summary WHERE run_id='mock_run_001'").fetchone()[0]
        assert trades == 0
        assert windows == 3
        assert summary == 1

    def test_trade_records_with_null_pnl_skipped(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """Trades with ``pnl=None`` are skipped; valid ones still persist."""
        results = WalkForwardResults(
            per_window=[make_window(i) for i in range(3)],
            aggregated=AggregatedMetrics(
                mean_win_rate=0.5,
                std_win_rate=0.0,
                mean_profit_factor=1.0,
                std_profit_factor=0.0,
                mean_max_drawdown=0.0,
                std_max_drawdown=0.0,
                mean_sharpe_ratio=1.0,
                std_sharpe_ratio=0.0,
                mean_trade_count=20.0,
                std_trade_count=0.0,
                mean_total_pnl=0.0,
                std_total_pnl=0.0,
                windows_passed=2,
                total_windows=3,
            ),
            go_nogo=False,
        )
        results._trade_records = [
            {"pnl": 10.0, "direction": "long", "window_id": 0},
            {"pnl": None, "direction": "long", "window_id": 1},  # skip
            {"pnl": 5.0, "direction": "short", "window_id": 1},
            "not-a-dict",  # skip
            {"pnl": -3.5, "direction": "long", "window_id": 2, "exit_reason": "stop"},
        ]
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            rows = conn.execute(
                "SELECT window_idx, direction, pnl, exit_reason FROM trades WHERE run_id='mock_run_001' ORDER BY pnl"
            ).fetchall()
        assert len(rows) == 3
        # All three valid entries should be present.
        pnls = [r[2] for r in rows]
        assert sorted(pnls) == [-3.5, 5.0, 10.0]

    def test_trade_columns_are_correct(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """Stored trades have run_id, window_idx, direction, pnl, exit_reason.

        With the date-fix, ``entry_time``, ``exit_time``, ``entry_price``,
        and ``exit_price`` are now populated when the trade record dict
        provides them.
        """
        results = make_results(n_windows=3, n_trades=0)
        results._trade_records = [
            {
                "pnl": 9.99,
                "direction": "long",
                "window_id": 1,
                "exit_reason": "take_profit",
                "rationale": "ranking-engine breakout",
                "entry_time": datetime(2025, 3, 15, 8, 0, tzinfo=timezone.utc),
                "exit_time": datetime(2025, 3, 15, 12, 0, tzinfo=timezone.utc),
                "entry_price": 1.0850,
                "exit_price": 1.0920,
            },
        ]
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            row = conn.execute(
                """SELECT run_id, window_idx, direction, pnl, exit_reason,
                          entry_time, exit_time, entry_price, exit_price
                   FROM trades WHERE run_id='mock_run_001'"""
            ).fetchone()

        run_id, window_idx, direction, pnl, exit_reason, et, xt, ep, xp = row
        assert run_id == "mock_run_001"
        assert window_idx == 1
        assert direction == "long"
        assert pnl == pytest.approx(9.99)
        assert exit_reason == "take_profit"
        assert et == datetime(2025, 3, 15, 8, 0, tzinfo=timezone.utc)
        assert xt == datetime(2025, 3, 15, 12, 0, tzinfo=timezone.utc)
        assert ep == pytest.approx(1.0850)
        assert xp == pytest.approx(1.0920)


# ---------------------------------------------------------------------------
# Per-window column integrity
# ---------------------------------------------------------------------------


class TestWindowColumnIntegrity:
    """window_idx uniqueness + column-mapping sanity check."""

    def test_window_rows_cover_all_indexes(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """All 5 WindowMetrics window_indexes appear in the persisted rows."""
        results = make_results(n_windows=5, n_trades=0)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            indexes = sorted(
                r[0] for r in conn.execute("SELECT window_idx FROM windows WHERE run_id='mock_run_001'").fetchall()
            )
        assert indexes == [0, 1, 2, 3, 4]

    def test_window_metrics_round_trip(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """A window's persisted row mirrors the input WindowMetrics fields."""
        results = WalkForwardResults(
            per_window=[
                WindowMetrics(
                    window_index=7,
                    win_rate=0.618,
                    profit_factor=1.95,
                    max_drawdown=0.08,
                    sharpe_ratio=1.42,
                    trade_count=42,
                    total_pnl=987.0,
                    passed_go_nogo=False,
                ),
            ],
            aggregated=AggregatedMetrics(
                mean_win_rate=0.618,
                std_win_rate=0.0,
                mean_profit_factor=1.95,
                std_profit_factor=0.0,
                mean_max_drawdown=0.08,
                std_max_drawdown=0.0,
                mean_sharpe_ratio=1.42,
                std_sharpe_ratio=0.0,
                mean_trade_count=42.0,
                std_trade_count=0.0,
                mean_total_pnl=987.0,
                std_total_pnl=0.0,
                windows_passed=0,
                total_windows=1,
            ),
            go_nogo=False,
        )
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            row = conn.execute(
                """SELECT window_idx, win_rate, profit_factor, sharpe,
                          max_drawdown, trade_count, total_pnl, passed_go_nogo
                   FROM windows WHERE run_id='mock_run_001' AND window_idx=7"""
            ).fetchone()
        widx, wr, pf, sh, dd, tc, pnl, gng = row
        assert widx == 7
        assert wr == pytest.approx(0.618)
        assert pf == pytest.approx(1.95)
        assert sh == pytest.approx(1.42)
        assert dd == pytest.approx(0.08)
        assert tc == 42
        assert pnl == pytest.approx(987.0)
        assert gng is False


# ---------------------------------------------------------------------------
# Transactional rollback — partial failures are atomic
# ---------------------------------------------------------------------------


class TestTransactionalRollback:
    """If any single insert raises, the prior writes must roll back."""

    def test_failure_in_trades_rolls_back(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """A failure during the trades insert must roll back windows + summary."""
        runner = StrategyRunner(db_path=tmp_db_path)
        results = make_results(n_windows=5, n_trades=3)

        # Inject a failure by patching the static helper. This avoids patching
        # DuckDB's read-only ``Connection.execute`` attribute.
        def boom(conn, rows):
            raise RuntimeError("simulated trades-insert failure")

        runner._insert_trades = boom  # type: ignore[assignment]

        with seeded_db as conn:
            with pytest.raises(RuntimeError, match="simulated trades-insert"):
                runner._write_results(conn, "mock_run_001", results)

        # Confirm rollback: nothing leaked into any of the three tables.
        with seeded_db as conn:
            windows = conn.execute("SELECT COUNT(*) FROM windows WHERE run_id='mock_run_001'").fetchone()[0]
            trades = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id='mock_run_001'").fetchone()[0]
            summary = conn.execute("SELECT COUNT(*) FROM metrics_summary WHERE run_id='mock_run_001'").fetchone()[0]
        assert windows == 0, "Windows rows leaked despite transaction failure"
        assert trades == 0, "Trades rows leaked despite transaction failure"
        assert summary == 0, "metrics_summary row leaked despite transaction failure"

    def test_failure_in_summary_rolls_back(
        self,
        seeded_db,
        tmp_db_path,
    ):
        """A failure during the metrics_summary insert also rolls back."""
        runner = StrategyRunner(db_path=tmp_db_path)
        results = make_results(n_windows=5, n_trades=3)

        def boom(conn, row):
            raise RuntimeError("simulated summary-insert failure")

        runner._insert_metrics_summary = boom  # type: ignore[assignment]

        with seeded_db as conn:
            with pytest.raises(RuntimeError, match="simulated summary-insert"):
                runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            windows = conn.execute("SELECT COUNT(*) FROM windows WHERE run_id='mock_run_001'").fetchone()[0]
            trades = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id='mock_run_001'").fetchone()[0]
            summary = conn.execute("SELECT COUNT(*) FROM metrics_summary WHERE run_id='mock_run_001'").fetchone()[0]
        assert windows == 0
        assert trades == 0
        assert summary == 0

    def test_failure_is_logged(
        self,
        seeded_db,
        tmp_db_path,
        caplog,
    ):
        """A rollback path must emit an error log so silent failures are gone."""
        runner = StrategyRunner(db_path=tmp_db_path)
        results = make_results(n_windows=5, n_trades=3)

        def boom(conn, rows):
            raise RuntimeError("simulated trades-insert failure")

        runner._insert_trades = boom  # type: ignore[assignment]

        with seeded_db as conn:
            with caplog.at_level(logging.ERROR, logger="srf.runner"):
                with pytest.raises(RuntimeError):
                    runner._write_results(conn, "mock_run_001", results)

        assert any("SRF persistence failed" in record.message for record in caplog.records), (
            f"Expected 'SRF persistence failed' in logs, got {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Helper-method smoke tests — exercise each insert in isolation
# ---------------------------------------------------------------------------


class TestInsertHelpers:
    """Per-table helpers are exposed as static methods for clarity + testing."""

    def test_insert_windows_noop_when_empty(self, tmp_db_path):
        """_insert_windows with empty list must NOT touch the table."""
        db = SRFDatabase(tmp_db_path)
        try:
            with db as conn:
                StrategyRunner._insert_windows(conn, [])
                count = conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
            assert count == 0
        finally:
            db.close()

    def test_insert_trades_noop_when_empty(self, tmp_db_path):
        """_insert_trades with empty list must NOT touch the table."""
        db = SRFDatabase(tmp_db_path)
        try:
            with db as conn:
                StrategyRunner._insert_trades(conn, [])
                count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            assert count == 0
        finally:
            db.close()

    def test_insert_metrics_summary_rejects_wrong_arity(self, tmp_db_path):
        """_insert_metrics_summary must reject rows of the wrong length."""
        db = SRFDatabase(tmp_db_path)
        try:
            with db as conn:
                with pytest.raises(ValueError, match="20 elements"):
                    StrategyRunner._insert_metrics_summary(
                        conn,
                        ["too", "short"],
                    )
                with pytest.raises(ValueError, match="20 elements"):
                    StrategyRunner._insert_metrics_summary(conn, None)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Window date columns (train_start/end, test_start/end)
# ---------------------------------------------------------------------------


class TestWindowDateColumns:
    """Verify that the 4 date columns are populated when _window_dates sidecar
    is present, and remain NULL when absent."""

    def test_dates_populated_from_sidecar(self, seeded_db, tmp_db_path):
        """When results._window_dates is provided, all 4 date columns are set."""
        results = make_results(n_windows=3, n_trades=0, with_dates=True)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            rows = conn.execute(
                """SELECT window_idx, train_start, train_end,
                          test_start, test_end
                   FROM windows WHERE run_id='mock_run_001'
                   ORDER BY window_idx"""
            ).fetchall()

        assert len(rows) == 3
        for row in rows:
            widx, ts, te, xs, xe = row
            assert ts is not None, f"train_start NULL for window {widx}"
            assert te is not None, f"train_end NULL for window {widx}"
            assert xs is not None, f"test_start NULL for window {widx}"
            assert xe is not None, f"test_end NULL for window {widx}"

    def test_dates_null_when_no_sidecar(self, seeded_db, tmp_db_path):
        """When no _window_dates sidecar, date columns remain NULL."""
        results = make_results(n_windows=3, n_trades=0, with_dates=False)
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            null_count = conn.execute(
                """SELECT COUNT(*) FROM windows
                   WHERE run_id='mock_run_001' AND train_start IS NULL"""
            ).fetchone()[0]
        assert null_count == 3

    def test_window_dates_round_trip(self, seeded_db, tmp_db_path):
        """Specific date values survive the round-trip through the DB."""
        results = make_results(n_windows=1, n_trades=0, with_dates=True)
        # Override with known values
        results._window_dates = [
            {
                "window_index": 0,
                "train_start": datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc),
                "train_end": datetime(2024, 9, 30, 0, 0, tzinfo=timezone.utc),
                "test_start": datetime(2024, 10, 1, 0, 0, tzinfo=timezone.utc),
                "test_end": datetime(2024, 12, 31, 0, 0, tzinfo=timezone.utc),
            }
        ]
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            row = conn.execute(
                """SELECT train_start, train_end, test_start, test_end
                   FROM windows WHERE run_id='mock_run_001' AND window_idx=0"""
            ).fetchone()

        ts, te, xs, xe = row
        assert ts == datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc)
        assert te == datetime(2024, 9, 30, 0, 0, tzinfo=timezone.utc)
        assert xs == datetime(2024, 10, 1, 0, 0, tzinfo=timezone.utc)
        assert xe == datetime(2024, 12, 31, 0, 0, tzinfo=timezone.utc)


class TestTradeTimestampColumns:
    """Verify that entry_time, exit_time, entry_price, exit_price are populated."""

    def test_trade_timestamps_populated(self, seeded_db, tmp_db_path):
        """Trade records with entry/exit data get full column population."""
        results = make_results(n_windows=2, n_trades=0)
        results._trade_records = [
            {
                "pnl": 42.0,
                "direction": "long",
                "window_id": 0,
                "entry_time": datetime(2025, 1, 15, 8, 0, tzinfo=timezone.utc),
                "exit_time": datetime(2025, 1, 15, 14, 30, tzinfo=timezone.utc),
                "entry_price": 1.0850,
                "exit_price": 1.0920,
                "exit_reason": "take_profit",
            },
        ]
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            row = conn.execute(
                """SELECT entry_time, exit_time, entry_price, exit_price
                   FROM trades WHERE run_id='mock_run_001'"""
            ).fetchone()

        et, xt, ep, xp = row
        assert et == datetime(2025, 1, 15, 8, 0, tzinfo=timezone.utc)
        assert xt == datetime(2025, 1, 15, 14, 30, tzinfo=timezone.utc)
        assert ep == pytest.approx(1.0850)
        assert xp == pytest.approx(1.0920)

    def test_trade_timestamps_null_when_absent(self, seeded_db, tmp_db_path):
        """Trade records without timestamp fields store NULL for those columns."""
        results = make_results(n_windows=2, n_trades=0)
        results._trade_records = [
            {"pnl": 10.0, "direction": "short", "window_id": 1},
        ]
        runner = StrategyRunner(db_path=tmp_db_path)

        with seeded_db as conn:
            runner._write_results(conn, "mock_run_001", results)

        with seeded_db as conn:
            row = conn.execute(
                """SELECT entry_time, exit_time, entry_price, exit_price
                   FROM trades WHERE run_id='mock_run_001'"""
            ).fetchone()

        et, xt, ep, xp = row
        assert et is None
        assert xt is None
        assert ep is None
        assert xp is None


class TestNamingNormalization:
    """Strategy name normalization for duplicate variants."""

    def test_normalize_finds_known_variants(self, tmp_db_path):
        """normalize_strategy_names detects and reports underscore variants."""
        db = SRFDatabase(tmp_db_path)
        try:
            with db as conn:
                conn.execute(
                    "INSERT INTO strategies (name, version, module_path, status) "
                    "VALUES ('killzone_momentum', '1.0', 'm.MockStrategy', 'production')"
                )
                conn.execute(
                    "INSERT INTO strategies (name, version, module_path, status) "
                    "VALUES ('killzonemomentum', '1.0', 'm.MockStrategy', 'production')"
                )
                conn.execute(
                    """INSERT INTO runs
                       (run_id, strategy_name, pair, timeframe, params_json,
                        git_commit, data_hash, status, created_at)
                       VALUES ('killzone_momentum_GBPUSD_M15_001', 'killzone_momentum',
                               'GBPUSD', 15, '{}', 'abc', 'h1', 'completed', now())"""
                )
                conn.execute(
                    """INSERT INTO runs
                       (run_id, strategy_name, pair, timeframe, params_json,
                        git_commit, data_hash, status, created_at)
                       VALUES ('killzonemomentum_GBPUSD_M15_002', 'killzonemomentum',
                               'GBPUSD', 15, '{}', 'abc', 'h1', 'completed', now())"""
                )

            result = StrategyRunner.normalize_strategy_names(tmp_db_path, dry_run=True)

            assert len(result["variants_found"]) == 1
            assert result["variants_found"][0]["from"] == "killzonemomentum"
            assert result["variants_found"][0]["to"] == "killzone_momentum"
        finally:
            db.close()

    def test_normalize_dry_run_does_not_modify(self, tmp_db_path):
        """dry_run=True must not change any data."""
        db = SRFDatabase(tmp_db_path)
        try:
            with db as conn:
                conn.execute(
                    "INSERT INTO strategies (name, version, module_path, status) "
                    "VALUES ('srmr_plus', '1.0', 'm.S', 'production')"
                )
                conn.execute(
                    "INSERT INTO strategies (name, version, module_path, status) "
                    "VALUES ('srmrplus', '1.0', 'm.S', 'production')"
                )
                conn.execute(
                    """INSERT INTO runs
                       (run_id, strategy_name, pair, timeframe, params_json,
                        git_commit, data_hash, status, created_at)
                       VALUES ('srmrplus_XAUUSD_M15_001', 'srmrplus',
                               'XAUUSD', 15, '{}', 'abc', 'h1', 'completed', now())"""
                )

            StrategyRunner.normalize_strategy_names(tmp_db_path, dry_run=True)

            with db as conn:
                name = conn.execute("SELECT strategy_name FROM runs WHERE run_id='srmrplus_XAUUSD_M15_001'").fetchone()[
                    0
                ]
            assert name == "srmrplus"
        finally:
            db.close()

    def test_normalize_applies_changes(self, tmp_db_path):
        """dry_run=False updates strategy_name in the runs table."""
        db = SRFDatabase(tmp_db_path)
        try:
            with db as conn:
                conn.execute(
                    "INSERT INTO strategies (name, version, module_path, status) "
                    "VALUES ('volatility_squeeze', '1.0', 'm.S', 'production')"
                )
                conn.execute(
                    "INSERT INTO strategies (name, version, module_path, status) "
                    "VALUES ('volatilitysqueeze', '1.0', 'm.S', 'production')"
                )
                conn.execute(
                    """INSERT INTO runs
                       (run_id, strategy_name, pair, timeframe, params_json,
                        git_commit, data_hash, status, created_at)
                       VALUES ('vsqueeze_001', 'volatilitysqueeze',
                               'EURUSD', 5, '{}', 'abc', 'h1', 'completed', now())"""
                )
                conn.execute(
                    """INSERT INTO runs
                       (run_id, strategy_name, pair, timeframe, params_json,
                        git_commit, data_hash, status, created_at)
                       VALUES ('vsqueeze_002', 'volatilitysqueeze',
                               'GBPUSD', 60, '{}', 'abc', 'h1', 'completed', now())"""
                )

            result = StrategyRunner.normalize_strategy_names(tmp_db_path)

            assert result["rows_updated"].get("volatilitysqueeze") == 2

            with db as conn:
                names = conn.execute(
                    "SELECT DISTINCT strategy_name FROM runs WHERE run_id IN ('vsqueeze_001', 'vsqueeze_002')"
                ).fetchall()
            assert len(names) == 1
            assert names[0][0] == "volatility_squeeze"
        finally:
            db.close()
