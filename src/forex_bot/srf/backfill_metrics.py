"""Backfill missing risk metrics for existing SRF runs.

Computes DSR, Sortino, Calmar, ICIR, score, and oos_sharpe_decay
from per-window data stored in the windows table.

Usage:
    python -m srf.backfill_metrics [--dry-run]
"""

from __future__ import annotations

import logging
import math
import statistics
import sys

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


def _deflated_sharpe(sr_annual: float, n: int, skew: float, kurt_excess: float) -> float:
    """Compute the Deflated Sharpe Ratio (Bailey & López de Prado 2014)."""
    if n < 3 or sr_annual == 0:
        return 0.0
    sr_var = (1 - skew * sr_annual * math.sqrt(1 / 252) + ((kurt_excess) / 4) * (sr_annual**2) / 252) / (n - 1)
    if sr_var <= 0:
        return 0.0
    z = sr_annual * math.sqrt(n) / math.sqrt(252)
    return round(float(stats.norm.cdf(z)), 6)


def _composite_score(sharpe: float, sortino: float, calmar: float, win_rate: float, go_rate: float) -> float:
    s_sharpe = min(max(sharpe / 3.0, 0), 1)
    s_sortino = min(max(sortino / 4.0, 0), 1)
    s_calmar = min(max(calmar / 5.0, 0), 1)
    s_wr = min(max((win_rate - 40) / 40, 0), 1)
    s_go = go_rate
    weights = {"sharpe": 0.25, "sortino": 0.25, "calmar": 0.15, "wr": 0.15, "go": 0.20}
    return round(
        float(
            weights["sharpe"] * s_sharpe
            + weights["sortino"] * s_sortino
            + weights["calmar"] * s_calmar
            + weights["wr"] * s_wr
            + weights["go"] * s_go
        ),
        6,
    )


def backfill(db_path: str = "data/research/research.duckdb", dry_run: bool = False) -> dict:
    """Backfill missing risk metrics for all completed runs."""
    import duckdb

    conn = duckdb.connect(db_path, read_only=False)

    # Find runs with NULL extended metrics
    rows = conn.execute("""SELECT run_id FROM metrics_summary WHERE dsr IS NULL""").fetchall()

    total = len(rows)
    updated = 0
    skipped = 0
    errors = 0

    logger.info("Found %d runs needing backfill", total)

    for (run_id,) in rows:
        # Fetch per-window data for this run
        win_rows = conn.execute(
            """SELECT win_rate, profit_factor, sharpe, max_drawdown, trade_count, total_pnl, passed_go_nogo
               FROM windows WHERE run_id = ? ORDER BY window_idx""",
            [run_id],
        ).fetchall()

        if not win_rows or len(win_rows) < 2:
            skipped += 1
            continue

        wrs = [r[0] for r in win_rows if r[5] is not None and r[5] != 0]  # total_pnl > 0
        pnls = [r[5] or 0.0 for r in win_rows]
        dds = [r[3] or 0.0 for r in win_rows]
        total_windows = len(win_rows)
        windows_passed = sum(1 for r in win_rows if r[6])

        pnl_arr = np.array(pnls, dtype=np.float64)
        n = len(pnl_arr)

        if n < 3 or np.std(pnl_arr, ddof=1) == 0:
            skipped += 1
            continue

        mean_pnl = float(np.mean(pnl_arr))
        std_pnl = float(np.std(pnl_arr, ddof=1))
        skew_val = float(stats.skew(pnl_arr)) if n >= 3 else 0.0
        kurt_val = float(stats.kurtosis(pnl_arr, fisher=True)) if n >= 4 else 0.0

        observed_sharpe = (mean_pnl / std_pnl * math.sqrt(n)) if std_pnl > 0 else 0.0
        sr_annual = observed_sharpe * math.sqrt(252)
        dsr = _deflated_sharpe(sr_annual, n, skew_val, kurt_val)

        downside = pnl_arr[pnl_arr < 0]
        downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else std_pnl
        sortino = (mean_pnl / downside_std * math.sqrt(n)) if downside_std > 0 else 0.0

        cumulative_pnl = float(np.sum(pnl_arr))
        max_dd = max(dds) if dds else 0.0
        calmar = (cumulative_pnl / max_dd) if max_dd > 0 else None

        ic_proxy = [(r[0] - 50.0) / 50.0 for r in win_rows if r[5] and r[5] != 0]
        icir = 0.0
        if len(ic_proxy) > 1 and statistics.stdev(ic_proxy) > 0:
            icir = statistics.mean(ic_proxy) / statistics.stdev(ic_proxy)

        score = _composite_score(
            observed_sharpe,
            sortino,
            calmar or 0.0,
            statistics.mean(wrs) if wrs else 0.0,
            windows_passed / total_windows if total_windows > 0 else 0.0,
        )

        # OOS Sharpe decay
        if n >= 4:
            mid = n // 2
            fh, sh_ = pnl_arr[:mid], pnl_arr[mid:]
            s1 = float(np.mean(fh) / np.std(fh, ddof=1)) if np.std(fh, ddof=1) > 0 and len(fh) > 1 else 0.0
            s2 = float(np.mean(sh_) / np.std(sh_, ddof=1)) if np.std(sh_, ddof=1) > 0 and len(sh_) > 1 else 0.0
            oos_decay = s1 - s2 if s1 > 0 else 0.0
        else:
            oos_decay = None

        if dry_run:
            logger.info(
                "[DRY] %s: dsr=%.4f sortino=%.4f calmar=%s icir=%.4f score=%.4f",
                run_id,
                dsr,
                sortino,
                f"{calmar:.4f}" if calmar else "None",
                icir,
                score,
            )
            updated += 1
            continue

        try:
            conn.execute(
                """UPDATE metrics_summary SET
                   dsr = ?, sortino = ?, calmar = ?, icir = ?, score = ?,
                   oos_sharpe_decay = ?
                   WHERE run_id = ?""",
                [dsr, sortino, calmar, icir, score, oos_decay, run_id],
            )
            updated += 1
        except Exception as exc:
            logger.error("Failed to backfill %s: %s", run_id, exc)
            errors += 1

    conn.close()

    result = {"total": total, "updated": updated, "skipped": skipped, "errors": errors}
    logger.info("Backfill complete: %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry = "--dry-run" in sys.argv
    db = "data/research/research.duckdb"
    for i, arg in enumerate(sys.argv):
        if arg == "--db" and i + 1 < len(sys.argv):
            db = sys.argv[i + 1]
    r = backfill(db, dry_run=dry)
    print(f"\nBackfill result: {r}")
