"""Versioned schema migrations for the trade store database."""

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATIONS = [
    {
        "version": 1,
        "description": "Initial schema: trades, equity_curve, daily_summary, rolling_metrics",
        "up": """
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
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_name);
            CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
            CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);

            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                equity REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                open_positions INTEGER NOT NULL,
                daily_pnl REAL,
                metadata TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_equity_timestamp ON equity_curve(timestamp);

            CREATE TABLE IF NOT EXISTS daily_summary (
                date TEXT PRIMARY KEY,
                starting_balance REAL NOT NULL,
                ending_balance REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                winning_trades INTEGER NOT NULL,
                losing_trades INTEGER NOT NULL,
                total_pnl REAL NOT NULL,
                max_drawdown_pct REAL NOT NULL,
                sharpe_estimate REAL,
                best_trade_pnl REAL,
                worst_trade_pnl REAL,
                strategies_used TEXT
            );

            CREATE TABLE IF NOT EXISTS rolling_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                sharpe_ratio REAL,
                max_drawdown_pct REAL,
                win_rate REAL,
                profit_factor REAL,
                avg_trade_pnl REAL,
                trade_count INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_rolling_window ON rolling_metrics(window_days, timestamp);

            CREATE TABLE IF NOT EXISTS _schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """,
    },
]


def get_current_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version (0 if no version table exists)."""
    try:
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations. Returns the new version number."""
    current = get_current_version(conn)
    for migration in MIGRATIONS:
        if migration["version"] > current:
            logger.info(
                "Applying migration v%d: %s",
                migration["version"],
                migration["description"],
            )
            conn.executescript(migration["up"])
            conn.execute(
                "INSERT INTO _schema_version (version, description) VALUES (?, ?)",
                (migration["version"], migration["description"]),
            )
            current = migration["version"]
    return current
