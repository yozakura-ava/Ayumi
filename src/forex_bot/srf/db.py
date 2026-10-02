"""SRF database access helpers — high-level query/insert on top of SRFDatabase.

This module provides reusable functions for common DB operations so that
callers don't need to hand-write SQL.  For raw access, use SRFDatabase directly
as a context manager.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------


def list_strategies(
    conn,
    status: str | None = None,
) -> list[dict]:
    """Return registered strategies, optionally filtered by status.

    Parameters
    ----------
    conn : duckdb connection (from ``with SRFDatabase() as conn``)
    status : optional filter — 'draft', 'production', or 'retired'
    """
    if status:
        rows = conn.execute(
            "SELECT name, version, module_path, status, created_at FROM strategies WHERE status=? ORDER BY created_at",
            [status],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name, version, module_path, status, created_at FROM strategies ORDER BY created_at"
        ).fetchall()
    return [
        {
            "name": r[0],
            "version": r[1],
            "module_path": r[2],
            "status": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------


def get_run(conn, run_id: str) -> dict | None:
    """Fetch a single run with its metrics summary. Returns None if not found."""
    row = conn.execute(
        """SELECT r.run_id, r.strategy_name, r.pair, r.timeframe,
                  r.params_json, r.git_commit, r.data_hash,
                  r.status, r.compute_seconds, r.created_at, r.completed_at,
                  m.go_nogo, m.score, m.total_trades,
                  m.windows_passed, m.windows_total
           FROM runs r
           LEFT JOIN metrics_summary m USING (run_id)
           WHERE r.run_id = ?""",
        [run_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row[0],
        "strategy_name": row[1],
        "pair": row[2],
        "timeframe": row[3],
        "params": json.loads(row[4]) if row[4] else {},
        "git_commit": row[5],
        "data_hash": row[6],
        "status": row[7],
        "compute_seconds": row[8],
        "created_at": row[9],
        "completed_at": row[10],
        "go_nogo": row[11],
        "score": row[12],
        "total_trades": row[13],
        "windows_passed": row[14],
        "windows_total": row[15],
    }


def list_recent_runs(conn, limit: int = 10) -> list[dict]:
    """Return the N most recent runs (any status)."""
    rows = conn.execute(
        """SELECT run_id, strategy_name, pair, timeframe, status, created_at
           FROM runs ORDER BY created_at DESC LIMIT ?""",
        [limit],
    ).fetchall()
    return [
        {
            "run_id": r[0],
            "strategy_name": r[1],
            "pair": r[2],
            "timeframe": r[3],
            "status": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Window / metrics helpers
# ---------------------------------------------------------------------------


def get_window_results(conn, run_id: str) -> list[dict]:
    """Return per-window results for a run."""
    rows = conn.execute(
        """SELECT window_idx, win_rate, profit_factor, sharpe,
                  max_drawdown, trade_count, total_pnl, passed_go_nogo
           FROM windows WHERE run_id=? ORDER BY window_idx""",
        [run_id],
    ).fetchall()
    return [
        {
            "window_idx": r[0],
            "win_rate": r[1],
            "profit_factor": r[2],
            "sharpe": r[3],
            "max_drawdown": r[4],
            "trade_count": r[5],
            "total_pnl": r[6],
            "passed_go_nogo": r[7],
        }
        for r in rows
    ]


def get_metrics(conn, run_id: str) -> dict | None:
    """Return the metrics_summary row for a run, or None."""
    row = conn.execute(
        """SELECT icir, dsr, calmar, sortino, mean_win_rate, std_win_rate,
                  mean_profit_factor, std_profit_factor, mean_sharpe, std_sharpe,
                  mean_max_drawdown, std_max_drawdown, total_trades,
                  windows_passed, windows_total, go_nogo, score,
                  param_stability_cv, oos_sharpe_decay
           FROM metrics_summary WHERE run_id=?""",
        [run_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "icir": row[0],
        "dsr": row[1],
        "calmar": row[2],
        "sortino": row[3],
        "mean_win_rate": row[4],
        "std_win_rate": row[5],
        "mean_profit_factor": row[6],
        "std_profit_factor": row[7],
        "mean_sharpe": row[8],
        "std_sharpe": row[9],
        "mean_max_drawdown": row[10],
        "std_max_drawdown": row[11],
        "total_trades": row[12],
        "windows_passed": row[13],
        "windows_total": row[14],
        "go_nogo": row[15],
        "score": row[16],
        "param_stability_cv": row[17],
        "oos_sharpe_decay": row[18],
    }


# ---------------------------------------------------------------------------
# Monte Carlo helpers
# ---------------------------------------------------------------------------


def insert_monte_carlo_samples(
    conn,
    run_id: str,
    samples: list[dict],
) -> int:
    """Bulk-insert Monte Carlo sample rows.

    Each dict in *samples* must have: sample_idx, metric_name, metric_value,
    and optionally p5, p95.

    Returns the number of rows inserted.
    """
    rows_inserted = 0
    for s in samples:
        conn.execute(
            """INSERT INTO monte_carlo_samples
               (run_id, sample_idx, metric_name, metric_value, p5, p95)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                run_id,
                s["sample_idx"],
                s["metric_name"],
                s["metric_value"],
                s.get("p5"),
                s.get("p95"),
            ],
        )
        rows_inserted += 1
    return rows_inserted


# ---------------------------------------------------------------------------
# Cron sentinel helpers
# ---------------------------------------------------------------------------


def record_cron_run(
    conn,
    cron_start: datetime,
    cron_end: datetime,
    exit_code: int,
    run_count: int,
    status: str,
) -> None:
    """Insert a cron heartbeat / sentinel row."""
    conn.execute(
        """INSERT INTO cron_runs
           (cron_start, cron_end, exit_code, run_count, status)
           VALUES (?, ?, ?, ?, ?)""",
        [cron_start, cron_end, exit_code, run_count, status],
    )


def last_cron_run(conn) -> dict | None:
    """Return the most recent cron_runs entry, or None."""
    row = conn.execute(
        """SELECT cron_start, cron_end, exit_code, run_count, status
           FROM cron_runs ORDER BY cron_start DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    return {
        "cron_start": row[0],
        "cron_end": row[1],
        "exit_code": row[2],
        "run_count": row[3],
        "status": row[4],
    }


# ---------------------------------------------------------------------------
# Promotion helpers
# ---------------------------------------------------------------------------


def promotion_queue(conn, min_score: float | None = None) -> list[dict]:
    """Return strategies in the promotion queue (go_nogo='go')."""
    if min_score is not None:
        rows = conn.execute(
            """SELECT strategy_name, pair, timeframe, dsr, pf,
                      windows_passed, windows_total, score
               FROM v_promotion_queue WHERE score >= ?
               ORDER BY score DESC NULLS LAST""",
            [min_score],
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT strategy_name, pair, timeframe, dsr, pf,
                      windows_passed, windows_total, score
               FROM v_promotion_queue
               ORDER BY score DESC NULLS LAST"""
        ).fetchall()
    return [
        {
            "strategy_name": r[0],
            "pair": r[1],
            "timeframe": r[2],
            "dsr": r[3],
            "pf": float(r[4]) if r[4] else None,
            "windows_passed": r[5],
            "windows_total": r[6],
            "score": r[7],
        }
        for r in rows
    ]
