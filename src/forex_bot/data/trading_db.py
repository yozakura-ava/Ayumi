"""Persist closed-trade P&L to the ``trades`` table in ``trading.db``.

This module provides a single entry point :func:`insert_closed_trade` that
the forward-test engine calls whenever a position closes (SL hit, TP hit,
or manual close).  The data lands in the existing ``trades`` SQLite table
alongside the schema created by the initial migration.

Usage (from ``forward_test_engine._on_position_closed``)::

    from data.trading_db import insert_closed_trade
    insert_closed_trade(
        trade_id=position.position_id,
        strategy_name="srmr_plus",
        symbol="GBPUSD",
        direction="BUY",
        entry_price=1.2750,
        exit_price=1.2760,
        entry_time=position.opened_at,
        exit_time=position.closed_at,
        lot_size=0.10,
        pnl=10.0,
        source="paper",
    )

Canonical DB location
---------------------
``<project_root>/data/trading.db``

Override for testing via ``TRADING_DB_PATH`` env var.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ayumi.trading_db")

# Canonical DB location: <project_root>/data/trading.db
# Configurable via TRADING_DB_PATH env var for testing.
# This must match the path used by readers (trade_store.py, audit_bar_close.py).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DB_PATH = Path(os.environ.get("TRADING_DB_PATH", str(_PROJECT_ROOT / "data" / "trading.db")))

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    strategy_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    lot_size REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    confidence REAL,
    source TEXT,
    pnl REAL,
    pnl_pips REAL,
    status TEXT NOT NULL DEFAULT 'open',
    close_reason TEXT,
    metadata TEXT
)
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_name)",
    "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
    "CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)",
]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the trades table and indexes if they don't exist."""
    conn.execute(_CREATE_TABLE_SQL)
    for sql in _CREATE_INDEXES_SQL:
        conn.execute(sql)
    conn.commit()


def _pip_size(symbol: str) -> float:
    """Return pip size for a symbol (heuristic)."""
    sym = symbol.upper()
    if "JPY" in sym:
        return 0.01
    if sym in ("XAUUSD", "GOLD", "XAGUSD", "SILVER"):
        return 0.1
    return 0.0001


def _compute_pips(
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: Optional[float],
) -> Optional[float]:
    """Compute realised pips from entry/exit prices."""
    if exit_price is None or entry_price is None or entry_price == 0:
        return None
    diff = exit_price - entry_price
    if direction.upper() in ("SELL", "SHORT"):
        diff = -diff
    return round(diff / _pip_size(symbol), 1)


def insert_closed_trade(
    *,
    trade_id: str,
    strategy_name: str = "",
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: Optional[float] = None,
    entry_time: Optional[datetime] = None,
    exit_time: Optional[datetime] = None,
    lot_size: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    confidence: Optional[float] = None,
    source: str = "forward_test",
    pnl: float = 0.0,
    pnl_pips: Optional[float] = None,
    close_reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Insert (or replace) a closed-trade row into the ``trades`` table.

    Returns ``True`` on success, ``False`` on failure.
    Failures are logged as warnings and are non-fatal — the trade itself
    is already closed by the time this function is called.
    """
    if entry_time is None:
        entry_time = datetime.utcnow()

    # Compute pips if not explicitly provided
    if pnl_pips is None and exit_price is not None:
        pnl_pips = _compute_pips(symbol, direction, entry_price, exit_price)

    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5.0)
        try:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO trades (
                    trade_id, strategy_name, symbol, direction,
                    entry_price, exit_price, entry_time, exit_time,
                    lot_size, stop_loss, take_profit, confidence,
                    source, pnl, pnl_pips, status, close_reason, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    strategy_name,
                    symbol,
                    direction,
                    entry_price,
                    exit_price,
                    entry_time.isoformat(),
                    exit_time.isoformat() if exit_time else None,
                    lot_size,
                    stop_loss,
                    take_profit,
                    confidence,
                    source,
                    pnl,
                    pnl_pips,
                    "closed",
                    close_reason,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            conn.commit()
            logger.info(
                "trading.db: inserted close trade_id=%s symbol=%s pnl=%.2f pips=%s",
                trade_id,
                symbol,
                pnl,
                pnl_pips,
            )
            return True
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("trading.db insert failed (non-fatal): %s", exc)
        return False


def _self_test() -> None:
    """Self-test: insert a mock close event and verify the row appears.

    Run with::
        cd src/forex_bot && python -m data.trading_db
    """
    import tempfile

    global _DB_PATH
    orig_path = _DB_PATH
    tmpdb = Path(tempfile.mktemp(suffix=".db"))  # noqa: S306
    _DB_PATH = tmpdb

    try:
        test_trade_id = "TEST_CLOSE_001"
        ok = insert_closed_trade(
            trade_id=test_trade_id,
            strategy_name="test_strategy",
            symbol="GBPUSD",
            direction="BUY",
            entry_price=1.2750,
            exit_price=1.2760,
            entry_time=datetime(2026, 7, 10, 10, 0, 0),
            exit_time=datetime(2026, 7, 10, 11, 30, 0),
            lot_size=0.10,
            stop_loss=1.2700,
            take_profit=1.2800,
            confidence=0.72,
            source="self_test",
            pnl=10.0,
            close_reason="tp_hit",
            metadata={"signal_id": "test_1234"},
        )
        assert ok, "insert_closed_trade returned False"  # noqa: S101 — self-test invariant in `_self_test()`; runs only via `__main__` guard, intentionally silenced under `python -O`

        conn = sqlite3.connect(str(tmpdb))
        try:
            row = conn.execute(
                "SELECT trade_id, symbol, direction, entry_price, exit_price, "
                "pnl, pnl_pips, status, close_reason, strategy_name "
                "FROM trades WHERE trade_id = ?",
                (test_trade_id,),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "No row found in trades table"  # noqa: S101 — self-test invariant; debug-only row-presence check, intentionally silenced under `python -O`
        assert row[0] == test_trade_id, f"trade_id mismatch: {row[0]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[1] == "GBPUSD", f"symbol mismatch: {row[1]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[2] == "BUY", f"direction mismatch: {row[2]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[3] == 1.2750, f"entry_price mismatch: {row[3]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[4] == 1.2760, f"exit_price mismatch: {row[4]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[5] == 10.0, f"pnl mismatch: {row[5]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[6] == 10.0, (  # noqa: S101 — self-test invariant; debug-only pips check, intentionally silenced under `python -O`
            f"pnl_pips mismatch: {row[6]}"
        )  # (1.2760-1.2750)/0.0001 = 10.0
        assert row[7] == "closed", f"status mismatch: {row[7]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[8] == "tp_hit", f"close_reason mismatch: {row[8]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`
        assert row[9] == "test_strategy", f"strategy_name mismatch: {row[9]}"  # noqa: S101 — self-test invariant; debug-only column check, intentionally silenced under `python -O`

        # Test SELL direction pips
        ok2 = insert_closed_trade(
            trade_id="TEST_CLOSE_002",
            strategy_name="test_strategy",
            symbol="USDJPY",
            direction="SELL",
            entry_price=157.50,
            exit_price=157.20,
            entry_time=datetime(2026, 7, 10, 10, 0, 0),
            exit_time=datetime(2026, 7, 10, 12, 0, 0),
            lot_size=0.05,
            pnl=15.0,
            source="self_test",
        )
        assert ok2, "insert for SELL trade returned False"  # noqa: S101 — self-test invariant in `_self_test()`; runs only via `__main__` guard, intentionally silenced under `python -O`

        conn = sqlite3.connect(str(tmpdb))
        try:
            row2 = conn.execute("SELECT pnl_pips FROM trades WHERE trade_id = 'TEST_CLOSE_002'").fetchone()
        finally:
            conn.close()

        # SELL: diff = entry - exit = 157.50 - 157.20 = 0.30; pip_size JPY = 0.01
        # pips = 0.30 / 0.01 = 30.0
        assert row2[0] == 30.0, f"JPY SELL pnl_pips mismatch: {row2[0]} (expected 30.0)"  # noqa: S101 — self-test invariant; debug-only JPY pips check, intentionally silenced under `python -O`

        print("SELF-TEST PASSED: 2 trades inserted and verified")
        print("  Trade 1: GBPUSD BUY, pnl=$10.00, pips=10.0")
        print("  Trade 2: USDJPY SELL, pnl=$15.00, pips=30.0")

    finally:
        _DB_PATH = orig_path
        try:
            tmpdb.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    _self_test()
