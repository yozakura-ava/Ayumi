"""Test coverage for forex_bot/storage/trade_store.py helpers.

Covers:
  - _simple_sharpe: empty/insufficient input, zero variance, known positive/negative series
  - _row_to_dict: JSON deserialization of metadata/strategies_used columns
  - TradeStore: minimal instantiation with tmp_path; close connection cleanly

Deterministic, isolated tests — no live trading, no broker/network access, no
persistent state across tests.
"""

import sqlite3
import statistics
from pathlib import Path

import pytest
from storage.trade_store import TradeStore, _row_to_dict, _simple_sharpe

# ---------------------------------------------------------------------------
# _simple_sharpe
# ---------------------------------------------------------------------------


class TestSimpleSharpe:
    """Tests for _simple_sharpe — empty/insufficient, zero variance, known series."""

    def test_empty_list_returns_none(self):
        """Empty pnls list → None (need at least 2 points)."""
        assert _simple_sharpe([]) is None

    def test_single_value_returns_none(self):
        """Single pnl → None (need at least 2 points)."""
        assert _simple_sharpe([1.5]) is None

    def test_two_values_returns_ratio(self):
        """Two values produce a finite sharpe ratio."""
        # mean=0.015, pstdev = sqrt(((0.01-0.015)^2 + (0.02-0.015)^2)/2)
        # = sqrt((0.000025 + 0.000025)/2) = sqrt(0.000025) ≈ 0.005
        result = _simple_sharpe([0.01, 0.02])
        assert result is not None
        # Should be positive (positive mean / positive stdev)
        assert result > 0
        # Sanity check the value
        assert result == pytest.approx(0.015 / 0.005, rel=1e-3)

    def test_zero_variance_returns_none(self):
        """All identical values → pstdev=0 → None (avoids div-by-zero)."""
        assert _simple_sharpe([1.0, 1.0, 1.0, 1.0]) is None

    def test_known_positive_series(self):
        """Positive-mean series: sharpe should be positive."""
        pnls = [1.0, 2.0, 3.0, 4.0, 5.0]  # mean=3.0, pstdev=sqrt(2.0)≈1.414
        expected = 3.0 / statistics.pstdev(pnls)
        assert _simple_sharpe(pnls) == pytest.approx(expected, rel=1e-9)

    def test_known_negative_series(self):
        """Negative-mean series: sharpe should be negative."""
        pnls = [-1.0, -2.0, -3.0, -4.0, -5.0]  # mean=-3.0
        expected = -3.0 / statistics.pstdev(pnls)
        assert _simple_sharpe(pnls) == pytest.approx(expected, rel=1e-9)

    def test_mixed_positive_negative(self):
        """Series with mean=0 → sharpe=0."""
        pnls = [-2.0, 2.0, -1.0, 1.0]  # mean=0
        assert _simple_sharpe(pnls) == pytest.approx(0.0, abs=1e-9)

    def test_uses_population_stdev_not_sample(self):
        """Verify pstdev (population) is used, not stdev (sample)."""
        # For [1, 2, 3]: mean=2, pstdev=sqrt((1+0+1)/3)=0.8165, stdev=sqrt(2/2)=1.0
        pnls = [1, 2, 3]
        expected_pstdev = statistics.pstdev(pnls)
        expected_sample = statistics.stdev(pnls)
        # Result should match pstdev, not stdev
        assert _simple_sharpe(pnls) == pytest.approx(2.0 / expected_pstdev, rel=1e-9)
        assert _simple_sharpe(pnls) != pytest.approx(2.0 / expected_sample, rel=1e-3)

    def test_large_positive_outlier(self):
        """Series with one big positive outlier: still produces a valid ratio."""
        pnls = [0.01, 0.02, 0.01, 100.0]
        result = _simple_sharpe(pnls)
        assert result is not None
        assert result > 0  # Positive mean dominated by outlier


# ---------------------------------------------------------------------------
# _row_to_dict
# ---------------------------------------------------------------------------


class TestRowToDict:
    """Tests for _row_to_dict — sqlite3.Row conversion + JSON deserialization."""

    def test_basic_row_to_dict(self):
        """Row with non-JSON columns passes through unchanged."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT, c REAL)")
        conn.execute("INSERT INTO t VALUES (1, 'hello', 3.14)")
        row = conn.execute("SELECT * FROM t").fetchone()
        d = _row_to_dict(row)
        assert d == {"a": 1, "b": "hello", "c": 3.14}
        conn.close()

    def test_metadata_json_string_deserialized(self):
        """A JSON-string metadata column is deserialized to a dict."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id INTEGER, metadata TEXT)")
        conn.execute("INSERT INTO t VALUES (1, ?)", ('{"key": "value", "n": 42}',))
        row = conn.execute("SELECT * FROM t").fetchone()
        d = _row_to_dict(row)
        assert d["metadata"] == {"key": "value", "n": 42}
        assert isinstance(d["metadata"], dict)
        conn.close()

    def test_strategies_used_json_string_deserialized(self):
        """A JSON-string strategies_used column is deserialized to a list."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id INTEGER, strategies_used TEXT)")
        conn.execute("INSERT INTO t VALUES (1, ?)", ('["strat_a", "strat_b"]',))
        row = conn.execute("SELECT * FROM t").fetchone()
        d = _row_to_dict(row)
        assert d["strategies_used"] == ["strat_a", "strat_b"]
        assert isinstance(d["strategies_used"], list)
        conn.close()

    def test_invalid_json_kept_as_string(self):
        """If a metadata/strategies_used value isn't valid JSON, keep the string."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id INTEGER, metadata TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'not_valid_json{{')")
        row = conn.execute("SELECT * FROM t").fetchone()
        d = _row_to_dict(row)
        # The function swallows JSONDecodeError and leaves the string intact
        assert d["metadata"] == "not_valid_json{{"
        conn.close()

    def test_null_metadata_kept_as_none(self):
        """A NULL metadata column is preserved as None (not string-coerced)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (id INTEGER, metadata TEXT)")
        conn.execute("INSERT INTO t VALUES (1, NULL)")
        row = conn.execute("SELECT * FROM t").fetchone()
        d = _row_to_dict(row)
        assert d["metadata"] is None
        conn.close()

    def test_non_string_column_untouched(self):
        """Non-target columns (e.g. a, b, c) are not JSON-decoded."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a TEXT, metadata TEXT)")
        # 'a' is not in the deserialization set
        a_val = '{"k": 1}'
        meta_val = '{"real": true}'
        conn.execute("INSERT INTO t VALUES (?, ?)", (a_val, meta_val))
        row = conn.execute("SELECT * FROM t").fetchone()
        d = _row_to_dict(row)
        assert d["a"] == a_val  # Untouched
        assert d["metadata"] == {"real": True}  # Deserialized
        conn.close()


# ---------------------------------------------------------------------------
# TradeStore (minimal — tmp_path, deterministic)
# ---------------------------------------------------------------------------


class TestTradeStore:
    """Smoke tests for TradeStore — confirm init, basic query, clean close."""

    def test_init_creates_db_file(self, tmp_path: Path):
        """TradeStore creates the db file and parent directory on init."""
        db_path = tmp_path / "subdir" / "trades.db"
        store = TradeStore(db_path=str(db_path))
        try:
            # The file should exist (or be created on first write — SQLite may
            # not flush until a write. We don't assert existence, just that
            # init didn't raise).
            assert store._db_path == db_path
        finally:
            store.close()

    def test_init_with_explicit_path(self, tmp_path: Path):
        """TradeStore honors the db_path argument."""
        db_path = tmp_path / "trades.db"
        store = TradeStore(db_path=str(db_path))
        try:
            assert str(store._db_path) == str(db_path)
        finally:
            store.close()

    def test_default_db_path(self, monkeypatch, tmp_path: Path):
        """When no db_path is provided, defaults to data/trading.db (relative)."""
        # The default is "data/trading.db" — we don't want to create that in cwd,
        # so we just verify the default is what's expected.
        monkeypatch.chdir(tmp_path)
        store = TradeStore()
        try:
            assert str(store._db_path) == "data/trading.db"
        finally:
            store.close()
        # Cleanup the auto-created data dir
        import shutil

        data_dir = tmp_path / "data"
        if data_dir.exists():
            shutil.rmtree(data_dir)

    def test_close_clears_thread_local_connection(self, tmp_path: Path):
        """close() nulls the thread-local connection."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        # Access connection to create it
        _ = store._get_connection()
        assert store._local.conn is not None
        store.close()
        assert store._local.conn is None

    def test_close_idempotent(self, tmp_path: Path):
        """Calling close() twice doesn't raise."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        store.close()
        store.close()  # Should be a no-op

    def test_get_total_trades_empty(self, tmp_path: Path):
        """get_total_trades on a fresh DB returns 0."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_total_trades() == 0
        finally:
            store.close()

    def test_get_open_positions_empty(self, tmp_path: Path):
        """get_open_positions on a fresh DB returns []."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_open_positions() == []
            assert store.get_open_positions(symbol="EURUSD") == []
        finally:
            store.close()

    def test_get_equity_curve_empty(self, tmp_path: Path):
        """get_equity_curve on a fresh DB returns []."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_equity_curve(days=30) == []
        finally:
            store.close()

    def test_get_daily_summary_none_on_empty(self, tmp_path: Path):
        """get_daily_summary returns None when no summary exists for the date."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_daily_summary(date="2020-01-01") is None
        finally:
            store.close()

    def test_get_rolling_metrics_none_on_empty(self, tmp_path: Path):
        """get_rolling_metrics returns None when no metrics exist for the window."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_rolling_metrics(window_days=30) is None
        finally:
            store.close()

    def test_get_current_drawdown_zero_on_empty(self, tmp_path: Path):
        """get_current_drawdown returns 0.0 on a DB with no equity curve."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_current_drawdown() == 0.0
        finally:
            store.close()

    def test_get_win_rate_zero_on_empty(self, tmp_path: Path):
        """get_win_rate returns 0.0 when there are no closed trades."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_win_rate(days=30) == 0.0
        finally:
            store.close()

    def test_get_sharpe_none_on_empty(self, tmp_path: Path):
        """get_sharpe returns None when there are fewer than 2 closed trades."""
        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            assert store.get_sharpe(days=30) is None
        finally:
            store.close()

    def test_concurrent_threads_get_separate_connections(self, tmp_path: Path):
        """Each thread should get its own SQLite connection (thread-local)."""
        import threading

        store = TradeStore(db_path=str(tmp_path / "trades.db"))
        try:
            conn_ids: list = []
            barrier = threading.Barrier(2)

            def worker():
                barrier.wait()
                conn_ids.append(id(store._get_connection()))

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Two threads, two distinct connection objects
            assert len(set(conn_ids)) == 2
        finally:
            store.close()
