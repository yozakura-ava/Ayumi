"""SQLite-backed persistent trade store for Ayumi.

Thread-safe, WAL-mode, auto-migrating.  Provides trade lifecycle management,
equity curve tracking, and rolling performance metrics.

Thread safety model:
- Each thread gets its own SQLite connection via ``threading.local``.
- Write operations are serialized through a single write lock.
- Read operations use thread-local connections without requiring the lock,
  which is safe under WAL journal mode (concurrent readers are fine).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .migrations import run_migrations

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Deserialize JSON columns
    for key in ("metadata", "strategies_used"):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ---------------------------------------------------------------------------
# TradeStore
# ---------------------------------------------------------------------------


class TradeStore:
    """Persistent SQLite trade store with equity curve and performance tracking.

    Uses thread-local connections so every thread gets its own SQLite handle.
    Writes are serialized through a single write lock; reads proceed on
    thread-local connections without locking (safe under WAL mode).
    """

    def __init__(self, db_path: str = "data/trading.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()

        # Run migrations on the main-thread connection, then close it.
        # Future connections will be created lazily per-thread.
        conn = self._create_connection()
        try:
            run_migrations(conn)
            conn.commit()
        finally:
            conn.close()

    # -- connection management -----------------------------------------------

    def _create_connection(self) -> sqlite3.Connection:
        """Create a fresh SQLite connection configured for WAL mode."""
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Return the thread-local connection, creating one if needed."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._create_connection()
        return self._local.conn

    def _write(self, fn):
        """Execute *fn(conn)* under the write lock and commit afterwards."""
        with self._write_lock:
            conn = self._get_connection()
            try:
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def _read(self, fn):
        """Execute *fn(conn)* on a thread-local connection (no lock)."""
        conn = self._get_connection()
        return fn(conn)

    @property
    def _conn(self) -> sqlite3.Connection:
        """Backward-compat property — returns the calling thread's connection."""
        return self._get_connection()

    # -- trade lifecycle -----------------------------------------------------

    def record_open(self, trade: dict) -> str:
        """Record a new open trade.  Returns the trade_id (generated if not provided)."""
        trade_id = trade.get("trade_id") or str(uuid.uuid4())
        metadata = json.dumps(trade.get("metadata")) if trade.get("metadata") else None

        def _do(conn):
            conn.execute(
                """
                INSERT INTO trades (
                    trade_id, strategy_name, symbol, direction,
                    entry_price, entry_time, lot_size,
                    stop_loss, take_profit, confidence, source, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    entry_price  = excluded.entry_price,
                    stop_loss    = excluded.stop_loss,
                    take_profit  = excluded.take_profit,
                    confidence   = excluded.confidence
                """,
                (
                    trade_id,
                    trade["strategy_name"],
                    trade["symbol"],
                    trade["direction"],
                    trade["entry_price"],
                    trade.get("entry_time", _utcnow()),
                    trade["lot_size"],
                    trade.get("stop_loss"),
                    trade.get("take_profit"),
                    trade.get("confidence"),
                    trade.get("source"),
                    metadata,
                ),
            )

        self._write(_do)
        return trade_id

    def record_close(self, trade_id: str, exit_price: float, close_reason: str) -> dict:
        """Close a trade, compute P&L, return the closed trade dict."""

        def _do(conn):
            trade = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if trade is None:
                raise ValueError(f"Trade {trade_id} not found")

            trade = _row_to_dict(trade)
            if trade["status"] != "open":
                raise ValueError(f"Trade {trade_id} is already {trade['status']}")

            # P&L calculation
            direction = trade["direction"]
            entry = trade["entry_price"]
            if direction == "BUY":
                pnl = exit_price - entry
                pnl_pips = pnl * 10_000  # standard pip for 4-decimal pairs
            else:
                pnl = entry - exit_price
                pnl_pips = pnl * 10_000

            pnl *= trade["lot_size"] * 100_000  # scale by lot size
            pnl_pips *= trade["lot_size"]

            now = _utcnow()
            conn.execute(
                """
                UPDATE trades SET
                    exit_price = ?, exit_time = ?, pnl = ?, pnl_pips = ?,
                    status = 'closed', close_reason = ?
                WHERE trade_id = ?
                """,
                (exit_price, now, pnl, pnl_pips, close_reason, trade_id),
            )

            trade.update(
                exit_price=exit_price,
                exit_time=now,
                pnl=pnl,
                pnl_pips=pnl_pips,
                status="closed",
                close_reason=close_reason,
            )
            return trade

        return self._write(_do)

    def update_unrealized(self, trade_id: str, current_price: float) -> float:
        """Update unrealized P&L for an open trade. Returns the unrealized P&L."""

        def _read_trade(conn):
            trade = conn.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
            if trade is None:
                raise ValueError(f"Trade {trade_id} not found")
            return _row_to_dict(trade)

        trade = self._read(_read_trade)

        direction = trade["direction"]
        entry = trade["entry_price"]
        if direction == "BUY":
            unrealized = (current_price - entry) * trade["lot_size"] * 100_000
        else:
            unrealized = (entry - current_price) * trade["lot_size"] * 100_000

        return unrealized

    # -- equity curve --------------------------------------------------------

    def record_equity_snapshot(
        self,
        balance: float,
        equity: float,
        unrealized: float,
        open_count: int,
        daily_pnl: Optional[float] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Record a point on the equity curve."""
        meta_json = json.dumps(metadata) if metadata else None

        def _do(conn):
            conn.execute(
                """
                INSERT INTO equity_curve (timestamp, balance, equity, unrealized_pnl,
                                          open_positions, daily_pnl, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utcnow(),
                    balance,
                    equity,
                    unrealized,
                    open_count,
                    daily_pnl,
                    meta_json,
                ),
            )

        self._write(_do)

    # -- queries -------------------------------------------------------------

    def get_open_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Return all open positions, optionally filtered by symbol."""

        def _do(conn):
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE status = 'open' AND symbol = ? ORDER BY entry_time",
                    (symbol,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time").fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def get_trades(
        self,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        date_range: Optional[tuple[str, str]] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query trades with optional filters."""

        clauses: list[str] = []
        params: list[Any] = []

        if strategy:
            clauses.append("strategy_name = ?")
            params.append(strategy)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if date_range:
            clauses.append("entry_time >= ? AND entry_time <= ?")
            params.extend(date_range)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM trades {where} ORDER BY entry_time DESC LIMIT ?"  # noqa: S608
        params.append(limit)

        def _do(conn):
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def get_equity_curve(self, days: int = 30) -> list[dict]:
        """Return equity curve points for the last N days."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def _do(conn):
            rows = conn.execute(
                "SELECT * FROM equity_curve WHERE timestamp >= ? ORDER BY timestamp",
                (cutoff,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

        return self._read(_do)

    def get_daily_summary(self, date: Optional[str] = None) -> Optional[dict]:
        """Return daily summary for a specific date (defaults to today)."""
        date = date or _today()

        def _do(conn):
            row = conn.execute("SELECT * FROM daily_summary WHERE date = ?", (date,)).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    def get_rolling_metrics(self, window_days: int = 30) -> Optional[dict]:
        """Return the most recent rolling metrics for a window size."""

        def _do(conn):
            row = conn.execute(
                "SELECT * FROM rolling_metrics WHERE window_days = ? ORDER BY timestamp DESC LIMIT 1",
                (window_days,),
            ).fetchone()
            return _row_to_dict(row) if row else None

        return self._read(_do)

    # -- performance calculations ---------------------------------------------

    def calculate_daily_summary(self, date: Optional[str] = None) -> dict:
        """Compute and upsert daily performance summary."""
        date = date or _today()

        def _do(conn):
            # Closed trades for the day
            rows = conn.execute(
                """
                SELECT * FROM trades
                WHERE status = 'closed' AND date(exit_time) = ?
                """,
                (date,),
            ).fetchall()
            closed = [_row_to_dict(r) for r in rows]

            # Open trades at start of day
            open_start = conn.execute(
                """
                SELECT * FROM trades
                WHERE status = 'open' AND entry_time < ?
                """,
                (f"{date}T00:00:00",),
            ).fetchall()
            open_at_start = [_row_to_dict(r) for r in open_start]  # noqa: F841 — live storage analytics; dead result preserved per zero-live-path-change policy

            winning = sum(1 for t in closed if t.get("pnl", 0) > 0)
            losing = sum(1 for t in closed if t.get("pnl", 0) < 0)
            total_pnl = sum(t.get("pnl", 0) for t in closed)

            pnl_values = [t["pnl"] for t in closed if t.get("pnl") is not None]
            best = max(pnl_values) if pnl_values else 0.0
            worst = min(pnl_values) if pnl_values else 0.0

            strategies = list({t["strategy_name"] for t in closed})
            strategies_json = json.dumps(strategies)

            # Balance tracking via equity curve
            start_snap = conn.execute(
                "SELECT balance FROM equity_curve WHERE date(timestamp) = ? ORDER BY timestamp ASC LIMIT 1",
                (date,),
            ).fetchone()
            end_snap = conn.execute(
                "SELECT balance FROM equity_curve WHERE date(timestamp) = ? ORDER BY timestamp DESC LIMIT 1",
                (date,),
            ).fetchone()

            starting_balance = start_snap["balance"] if start_snap else 0.0
            ending_balance = end_snap["balance"] if end_snap else starting_balance + total_pnl

            # Max drawdown for the day
            curve = conn.execute(
                "SELECT equity FROM equity_curve WHERE date(timestamp) = ? ORDER BY timestamp",
                (date,),
            ).fetchall()
            if curve:
                equities = [r["equity"] for r in curve]
                peak = max(equities)
                trough = min(equities)
                max_dd = ((peak - trough) / peak * 100) if peak > 0 else 0.0
            else:
                max_dd = 0.0

            # Sharpe estimate (simple daily)
            sharpe = _simple_sharpe(pnl_values) if len(pnl_values) >= 2 else None

            summary = {
                "date": date,
                "starting_balance": starting_balance,
                "ending_balance": ending_balance,
                "total_trades": len(closed),
                "winning_trades": winning,
                "losing_trades": losing,
                "total_pnl": total_pnl,
                "max_drawdown_pct": max_dd,
                "sharpe_estimate": sharpe,
                "best_trade_pnl": best,
                "worst_trade_pnl": worst,
                "strategies_used": strategies_json,
            }

            conn.execute(
                """
                INSERT INTO daily_summary (
                    date, starting_balance, ending_balance, total_trades,
                    winning_trades, losing_trades, total_pnl, max_drawdown_pct,
                    sharpe_estimate, best_trade_pnl, worst_trade_pnl, strategies_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    ending_balance   = excluded.ending_balance,
                    total_trades     = excluded.total_trades,
                    winning_trades   = excluded.winning_trades,
                    losing_trades    = excluded.losing_trades,
                    total_pnl        = excluded.total_pnl,
                    max_drawdown_pct = excluded.max_drawdown_pct,
                    sharpe_estimate  = excluded.sharpe_estimate,
                    best_trade_pnl   = excluded.best_trade_pnl,
                    worst_trade_pnl  = excluded.worst_trade_pnl,
                    strategies_used  = excluded.strategies_used
                """,
                (
                    date,
                    starting_balance,
                    ending_balance,
                    len(closed),
                    winning,
                    losing,
                    total_pnl,
                    max_dd,
                    sharpe,
                    best,
                    worst,
                    strategies_json,
                ),
            )

            return summary

        return self._write(_do)

    def calculate_rolling_metrics(self, window_days: int = 30) -> dict:
        """Compute and store rolling performance metrics."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        def _do(conn):
            rows = conn.execute(
                """
                SELECT * FROM trades
                WHERE status = 'closed' AND exit_time >= ?
                ORDER BY exit_time
                """,
                (cutoff,),
            ).fetchall()
            trades = [_row_to_dict(r) for r in rows]

            if not trades:
                return {
                    "window_days": window_days,
                    "sharpe_ratio": None,
                    "max_drawdown_pct": None,
                    "win_rate": None,
                    "profit_factor": None,
                    "avg_trade_pnl": None,
                    "trade_count": 0,
                }

            pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
            winning = [p for p in pnls if p > 0]
            losing = [p for p in pnls if p < 0]

            win_rate = len(winning) / len(pnls) if pnls else 0.0
            gross_profit = sum(winning) if winning else 0.0
            gross_loss = abs(sum(losing)) if losing else 0.0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
            avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0
            sharpe = _simple_sharpe(pnls)

            # Max drawdown from equity curve
            curve_rows = conn.execute(
                "SELECT equity FROM equity_curve WHERE timestamp >= ? ORDER BY timestamp",
                (cutoff,),
            ).fetchall()
            if curve_rows:
                equities = [r["equity"] for r in curve_rows]
                peak = equities[0]
                max_dd = 0.0
                for eq in equities:
                    if eq > peak:
                        peak = eq
                    dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
                    if dd > max_dd:
                        max_dd = dd
            else:
                max_dd = None

            metrics = {
                "timestamp": _utcnow(),
                "window_days": window_days,
                "sharpe_ratio": sharpe,
                "max_drawdown_pct": max_dd,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "avg_trade_pnl": avg_pnl,
                "trade_count": len(pnls),
            }

            conn.execute(
                """
                INSERT INTO rolling_metrics (timestamp, window_days, sharpe_ratio,
                    max_drawdown_pct, win_rate, profit_factor, avg_trade_pnl, trade_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metrics["timestamp"],
                    window_days,
                    sharpe,
                    max_dd,
                    win_rate,
                    profit_factor,
                    avg_pnl,
                    len(pnls),
                ),
            )

            return metrics

        return self._write(_do)

    # -- utility queries -----------------------------------------------------

    def get_total_trades(self) -> int:

        def _do(conn):
            row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
            return row[0]

        return self._read(_do)

    def get_current_drawdown(self) -> float:
        """Current drawdown as percentage from equity curve peak."""

        def _do(conn):
            row = conn.execute("SELECT equity FROM equity_curve ORDER BY timestamp DESC LIMIT 1").fetchone()
            if row is None:
                return 0.0
            current = row["equity"]
            peak_row = conn.execute("SELECT MAX(equity) as peak FROM equity_curve").fetchone()
            peak = peak_row["peak"] if peak_row["peak"] else current
            return ((peak - current) / peak * 100) if peak > 0 else 0.0

        return self._read(_do)

    def get_win_rate(self, days: int = 30) -> float:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def _do(conn):
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as wr
                FROM trades
                WHERE status = 'closed' AND exit_time >= ?
                """,
                (cutoff,),
            ).fetchone()
            return row["wr"] if row and row["wr"] is not None else 0.0

        return self._read(_do)

    def get_sharpe(self, days: int = 30) -> Optional[float]:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        def _do(conn):
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE status = 'closed' AND exit_time >= ?",
                (cutoff,),
            ).fetchall()
            pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
            return _simple_sharpe(pnls) if len(pnls) >= 2 else None

        return self._read(_do)

    # -- internals -----------------------------------------------------------

    def close(self) -> None:
        """Close the thread-local connection for the calling thread."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# Module-level helper (kept for backward compat)
# ---------------------------------------------------------------------------


def _simple_sharpe(pnls: list[float]) -> Optional[float]:
    """Simple Sharpe ratio (no risk-free rate, assumes daily returns)."""
    if len(pnls) < 2:
        return None
    import statistics

    mean = statistics.mean(pnls)
    stdev = statistics.pstdev(pnls)
    return mean / stdev if stdev > 0 else None
