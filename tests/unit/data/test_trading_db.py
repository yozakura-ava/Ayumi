"""Tests for trading_db.py — verifies path normalization and write/read parity.

The core bug (DEBT card a868179a): _DB_PATH resolved to
``src/forex_bot/data/trading.db`` instead of project-root ``data/trading.db``,
causing writers and readers to hit different files.

These tests verify:
1. _DB_PATH points to the project-root data directory by default
2. insert_closed_trade writes to the same DB that readers query
3. The TRADING_DB_PATH env var override works for test isolation
"""

import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock


def _import_trading_db():
    """Import trading_db fresh to pick up env var changes."""
    import importlib

    import data.trading_db as mod

    importlib.reload(mod)
    return mod


def test_db_path_resolves_to_project_root():
    """_DB_PATH should resolve to <project_root>/data/trading.db, not src/forex_bot/data/."""
    # Clear any override so we test the default
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TRADING_DB_PATH", None)
        mod = _import_trading_db()
        db_path = mod._DB_PATH
        # Must end with data/trading.db at the project root
        assert db_path.name == "trading.db"
        assert db_path.parent.name == "data"
        # Must NOT be inside src/forex_bot/data/
        assert "src" not in str(db_path), f"_DB_PATH still points into src: {db_path}"
        assert "forex_bot" not in str(db_path), f"_DB_PATH still points into forex_bot: {db_path}"


def test_env_var_override():
    """TRADING_DB_PATH env var should override the default path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom = Path(tmpdir) / "custom_test.db"
        with mock.patch.dict(os.environ, {"TRADING_DB_PATH": str(custom)}):
            mod = _import_trading_db()
            assert mod._DB_PATH == custom, f"Env var override failed: {mod._DB_PATH} != {custom}"


def test_write_and_read_same_db():
    """insert_closed_trade and a direct query must hit the same file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_db = Path(tmpdir) / "trading.db"
        with mock.patch.dict(os.environ, {"TRADING_DB_PATH": str(custom_db)}):
            mod = _import_trading_db()

            ok = mod.insert_closed_trade(
                trade_id="TEST_PATH_001",
                strategy_name="test",
                symbol="EURUSD",
                direction="BUY",
                entry_price=1.1000,
                exit_price=1.1050,
                entry_time=datetime(2026, 7, 15, 10, 0, 0),
                exit_time=datetime(2026, 7, 15, 11, 0, 0),
                lot_size=0.10,
                pnl=5.0,
                close_reason="tp_hit",
            )
            assert ok, "insert_closed_trade returned False"

            # Read directly from the same path
            assert custom_db.exists(), f"DB file not created at {custom_db}"
            conn = sqlite3.connect(str(custom_db))
            try:
                row = conn.execute(
                    "SELECT trade_id, symbol, pnl, status FROM trades WHERE trade_id = ?",
                    ("TEST_PATH_001",),
                ).fetchone()
            finally:
                conn.close()

            assert row is not None, "Row not found in DB — write/read path mismatch"
            assert row[0] == "TEST_PATH_001"
            assert row[1] == "EURUSD"
            assert row[2] == 5.0
            assert row[3] == "closed"


def test_db_path_not_in_src_directory():
    """Regression guard: _DB_PATH must never resolve inside src/."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TRADING_DB_PATH", None)
        mod = _import_trading_db()
        path_str = str(mod._DB_PATH)
        # The path should not contain src/forex_bot/data
        assert "src/forex_bot/data" not in path_str, (
            f"Regression: _DB_PATH resolves into src/forex_bot/data/: {path_str}"
        )
