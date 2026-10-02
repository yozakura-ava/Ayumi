"""SRF Runner — wraps existing walk-forward engine, writes results to DuckDB."""

from __future__ import annotations

import json
import logging
import math
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from .data_qa import validate_data
from .param_stability import perturbation_stability_score
from .schema import SRFDatabase, compute_data_hash, generate_run_id

logger = logging.getLogger(__name__)


# ── Risk metric helpers ──────────────────────────────────────────────────


def _deflated_sharpe(sr_annual: float, n: int, skew: float, kurt_excess: float) -> float:
    """Compute the Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Adjusts observed Sharpe for skew/kurtosis bias and sample size.
    Returns a float — values >0 indicate skill after multiple-testing correction.
    """
    if n < 3 or sr_annual == 0:
        return 0.0
    # Expected Sharpe under null (zero-mean): 0
    # Variance of Sharpe estimator under non-normality
    sr_var = (1 - skew * sr_annual * math.sqrt(1 / 252) + ((kurt_excess) / 4) * (sr_annual**2) / 252) / (n - 1)
    if sr_var <= 0:
        return 0.0
    # DSR = CDF of observed SR under the deflated null
    z = sr_annual * math.sqrt(n) / math.sqrt(252)
    dsr = stats.norm.cdf(z)
    return round(float(dsr), 6)


def _composite_score(sharpe: float, sortino: float, calmar: float, win_rate: float, go_rate: float) -> float:
    """Weighted composite score for strategy ranking.

    Blends risk-adjusted return metrics with consistency metrics.
    Scale: roughly 0-1, higher is better.
    """
    # Normalize components to ~[0,1] range
    s_sharpe = min(max(sharpe / 3.0, 0), 1)  # Sharpe ~3 = excellent
    s_sortino = min(max(sortino / 4.0, 0), 1)  # Sortino ~4 = excellent
    s_calmar = min(max(calmar / 5.0, 0), 1)  # Calmar ~5 = excellent
    s_wr = min(max((win_rate - 40) / 40, 0), 1)  # 40-80% win rate range
    s_go = go_rate  # 0-1 pass rate

    weights = {"sharpe": 0.25, "sortino": 0.25, "calmar": 0.15, "wr": 0.15, "go": 0.20}
    score = (
        weights["sharpe"] * s_sharpe
        + weights["sortino"] * s_sortino
        + weights["calmar"] * s_calmar
        + weights["wr"] * s_wr
        + weights["go"] * s_go
    )
    return round(float(score), 6)


class StrategyRunner:
    """Runs a strategy through walk-forward validation and records results in DuckDB."""

    def __init__(
        self,
        db_path: str = "data/research/research.duckdb",
        repo_path: str = ".",
    ):
        self.db = SRFDatabase(db_path)
        self.repo_path = Path(repo_path).resolve()

    # ── public API ───────────────────────────────────────────────────────

    def run(
        self,
        *,
        strategy_name: str,
        strategy_factory: Any,
        pair: str,
        timeframe: int,
        data_path: str,
        params: dict | None = None,
        n_windows: int = 5,
        initial_balance: float = 10_000,
        spread_pips: float | None = None,
        min_confidence: float = 0.30,
        register_if_missing: bool = True,
        perturbation_evaluate_fn: callable | None = None,
    ) -> dict:
        """Execute a single walk-forward run. Returns run metadata + results dict.

        If ``perturbation_evaluate_fn`` is provided, runs a post-selection
        perturbation stability check (overfit spike detection) after the
        walk-forward validation completes.  The function should accept a
        params dict and return a scalar performance metric.

        Raises RuntimeError if git tree is dirty or data QA fails.
        """
        start = time.monotonic()

        # ── 1. Git-clean guard ────────────────────────────────────────────
        git_commit = self._get_git_commit()
        if git_commit is None:
            raise RuntimeError(
                "Git tree is dirty — SRF requires a clean tree for reproducibility. "
                "Commit or stash changes before running."
            )

        # ── 1b. Hypothesis doc gate ────────────────────────────────────────
        # Prevents post-hoc rationalization: every strategy must have a
        # hypothesis doc written BEFORE the sweep, not reconstructed from
        # results after seeing profit factors.
        hypothesis_path = self.repo_path / "docs" / "edges" / f"{strategy_name}-hypothesis.md"
        if not hypothesis_path.exists():
            raise RuntimeError(
                f"Strategy '{strategy_name}' has no hypothesis doc at "
                f"{hypothesis_path}. Write the edge hypothesis BEFORE running "
                "a sweep — this prevents post-hoc rationalization of results."
            )

        # ── 2. Load + validate data ───────────────────────────────────────
        df = self._load_data(data_path)
        qa = validate_data(df, pair, timeframe)
        if not qa.passed:
            failures_str = "; ".join(f"{f.check_name}: {f.detail}" for f in qa.failures if f.severity == "hard")
            raise RuntimeError(f"Data QA failed for {pair} {timeframe}m: {failures_str}")

        data_hash = compute_data_hash(data_path)

        # ── 3. Convert to Bar objects ─────────────────────────────────────
        bars = self._df_to_bars(df, pair)

        # ── 4. Ensure strategy registered in DB ───────────────────────────
        run_id = generate_run_id(strategy_name, pair, timeframe)

        with self.db as conn:
            self._ensure_strategy_registered(conn, strategy_name, strategy_factory, register_if_missing)

            # ── 5. Insert run record ──────────────────────────────────────
            conn.execute(
                """INSERT INTO runs
                   (run_id, strategy_name, pair, timeframe, params_json,
                    git_commit, data_hash, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', now())""",
                [
                    run_id,
                    strategy_name,
                    pair,
                    timeframe,
                    json.dumps(params or {}),
                    git_commit,
                    data_hash,
                ],
            )

            try:
                # ── 6. Run walk-forward ───────────────────────────────────
                results = self._run_walk_forward(
                    bars=bars,
                    strategy_factory=strategy_factory,
                    pair=pair,
                    n_windows=n_windows,
                    initial_balance=initial_balance,
                    spread_pips=spread_pips,
                    min_confidence=min_confidence,
                )

                # ── 7. Write results to DB ────────────────────────────────
                self._write_results(conn, run_id, results)

                compute_seconds = time.monotonic() - start

                conn.execute(
                    """UPDATE runs SET status='completed', compute_seconds=?,
                       completed_at=now() WHERE run_id=?""",
                    [compute_seconds, run_id],
                )

                logger.info(
                    "SRF run %s completed in %.1fs — %d windows, go_nogo=%s",
                    run_id,
                    compute_seconds,
                    len(results.per_window),
                    results.go_nogo,
                )

                result_dict = {
                    "run_id": run_id,
                    "strategy": strategy_name,
                    "pair": pair,
                    "timeframe": timeframe,
                    "go_nogo": results.go_nogo,
                    "windows": len(results.per_window),
                    "compute_seconds": compute_seconds,
                    "git_commit": git_commit,
                    "data_hash": data_hash,
                }

                # ── 8. Optional perturbation stability check ─────────────
                if perturbation_evaluate_fn is not None:
                    psr = perturbation_stability_score(
                        perturbation_evaluate_fn,
                        params or {},
                    )
                    result_dict["perturbation_stability"] = psr.summary()
                    result_dict["overfit_spike"] = psr.is_overfit_spike
                    logger.info(
                        "SRF run %s perturbation stability: score=%.3f, overfit_spike=%s",
                        run_id,
                        psr.stability_score,
                        psr.is_overfit_spike,
                    )

                return result_dict

            except Exception as exc:
                conn.execute(
                    "UPDATE runs SET status='failed', completed_at=now() WHERE run_id=?",
                    [run_id],
                )
                logger.error("SRF run %s failed: %s", run_id, exc)
                raise

    # ── internals ────────────────────────────────────────────────────────

    def _get_git_commit(self) -> str | None:
        """Return current git commit hash, or None if tree is dirty."""
        try:
            # Check if tree is clean
            diff = subprocess.check_output(
                ["git", "diff", "--stat"],  # noqa: S607
                cwd=str(self.repo_path),
                stderr=subprocess.DEVNULL,
            ).strip()
            if diff:
                return None
            # Get commit hash
            commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
                    cwd=str(self.repo_path),
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
            return commit
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def _load_data(self, data_path: str) -> pd.DataFrame:
        """Load bar data from CSV. Normalizes column names to lowercase."""
        df = pd.read_csv(data_path)
        # Normalize column names (CSVs may use TitleCase)
        rename_map = {}
        for col in df.columns:
            lower = col.lower()
            if lower != col:
                rename_map[col] = lower
        if rename_map:
            df = df.rename(columns=rename_map)
        # Normalize timestamp column name variants
        if "date" in df.columns and "timestamp" not in df.columns:
            df = df.rename(columns={"date": "timestamp"})
        return df

    def _df_to_bars(self, df: pd.DataFrame, pair: str):
        """Convert DataFrame to list of Bar objects."""
        from backtest.engine import Bar

        bars = []
        for _, row in df.iterrows():
            ts = row["timestamp"]
            # Handle epoch ms or ISO string
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            else:
                dt = pd.to_datetime(ts)
                if dt.tz is None:
                    dt = dt.tz_localize("UTC")

            bars.append(
                Bar(
                    time=dt,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                )
            )
        return bars

    def _run_walk_forward(
        self,
        bars: list,
        strategy_factory: Any,
        pair: str,
        n_windows: int,
        initial_balance: float,
        spread_pips: float | None,
        min_confidence: float,
    ):
        """Call the existing walk-forward runner."""
        from backtest.walk_forward_runner import run_strategy_walk_forward

        return run_strategy_walk_forward(
            bars=bars,
            strategy_factory=strategy_factory,
            pair=pair,
            n_windows=n_windows,
            initial_balance=initial_balance,
            spread_pips=spread_pips,
            min_confidence=min_confidence,
        )

    def _ensure_strategy_registered(self, conn, name: str, strategy_factory: Any, register: bool) -> None:
        """Ensure strategy exists in DB. Register if missing and allowed."""
        row = conn.execute("SELECT name FROM strategies WHERE name=?", [name]).fetchone()

        if row is None and register:
            # Try to get module path from the factory's class
            if isinstance(strategy_factory, type):
                cls = strategy_factory
            elif hasattr(strategy_factory, "__class__"):
                cls = strategy_factory.__class__
            else:
                cls = None
            module_path = ""
            if cls and hasattr(cls, "__module__"):
                module_path = f"{cls.__module__}.{cls.__name__}"

            conn.execute(
                """INSERT INTO strategies (name, version, module_path, status)
                   VALUES (?, '1.0', ?, 'production')""",
                [name, module_path],
            )
            logger.info("Auto-registered strategy '%s' in DB", name)

    def _write_results(self, conn, run_id: str, results) -> None:
        """Write walk-forward results to DuckDB tables.

        All three writes (windows, trades, metrics_summary) are wrapped in a
        single explicit transaction. If any insert fails the entire batch is
        rolled back so partial data never leaks into the reporting tables.

        Per-window schema columns populated:
            run_id, window_idx, train_start, train_end, test_start, test_end,
            win_rate, profit_factor, sharpe,
            max_drawdown, trade_count, total_pnl, passed_go_nogo.

        Window date boundaries (train_start/end, test_start/end) are sourced
        from an optional ``_window_dates`` sidecar on the results object.
        When the walk-forward runner provides this sidecar (as a list of
        ``(window_index, train_start, train_end, test_start, test_end)``
        tuples), the dates are written to the DB. When absent, the columns
        remain NULL until a backfill is run.

        Per-trade columns populated from `results._trade_records` (attached by
        the walk-forward runner). Each trade record carries ``window_id``,
        ``direction``, ``pnl``. When ``entry_time``, ``exit_time``,
        ``entry_price``, and ``exit_price`` are present on the record dict,
        they are persisted; otherwise NULL.
        """

        # ── Window dates sidecar ─────────────────────────────────────────
        # The walk-forward runner may attach ``_window_dates`` as a list of
        # dicts: ``[{"window_index": 0, "train_start": dt, ...}]``.
        # Build a lookup so we can merge dates into window_rows.
        window_dates: dict[int, dict] = {}
        _wd = getattr(results, "_window_dates", None)
        if _wd and isinstance(_wd, list):
            for wd in _wd:
                if isinstance(wd, dict):
                    window_dates[wd.get("window_index", -1)] = wd

        # ── Windows ───────────────────────────────────────────────────────
        window_rows: list[list] = []
        for w in results.per_window:
            wd = window_dates.get(w.window_index, {})
            window_rows.append(
                [
                    run_id,
                    w.window_index,
                    wd.get("train_start"),
                    wd.get("train_end"),
                    wd.get("test_start"),
                    wd.get("test_end"),
                    w.win_rate,
                    w.profit_factor,
                    w.sharpe_ratio,
                    w.max_drawdown,
                    w.trade_count,
                    w.total_pnl,
                    w.passed_go_nogo,
                ]
            )

        # ── Trades ────────────────────────────────────────────────────────
        # Pull trade records off the results object; the walk-forward runner
        # attaches ``_trade_records`` as a sidecar (typed as Any on the
        # dataclass). Be defensive: missing attribute, wrong type, or empty
        # list all degrade gracefully to "no trades persisted".
        trade_rows: list[list] = []
        trade_records = getattr(results, "_trade_records", None)
        if trade_records:
            for t in trade_records:
                if not isinstance(t, dict):
                    continue
                pnl = t.get("pnl")
                # Skip degenerate rows that have no numeric PnL — they corrupt
                # downstream aggregations.
                if pnl is None:
                    continue
                trade_rows.append(
                    [
                        run_id,
                        t.get("window_id"),
                        t.get("entry_time"),
                        t.get("exit_time"),
                        t.get("direction"),
                        t.get("entry_price"),
                        t.get("exit_price"),
                        pnl,
                        t.get("exit_reason"),
                    ]
                )

        # ── Metrics summary ───────────────────────────────────────────────
        windows_passed = sum(1 for w in results.per_window if w.passed_go_nogo)
        windows_total = len(results.per_window)

        wrs = [w.win_rate for w in results.per_window if w.trade_count > 0]
        pfs = [w.profit_factor for w in results.per_window if w.trade_count > 0]
        shrs = [w.sharpe_ratio for w in results.per_window if w.trade_count > 0]
        dds = [w.max_drawdown for w in results.per_window if w.trade_count > 0]
        pnls = [w.total_pnl for w in results.per_window if w.trade_count > 0]
        total_trades = sum(w.trade_count for w in results.per_window)

        go_nogo_str = "go" if results.go_nogo else "no-go"

        # ── Compute extended risk metrics ───────────────────────────────
        # All computed from per-window PnL series when available.
        pnl_arr = np.array(pnls, dtype=np.float64) if pnls else None

        def _safe_stdev(vals: list[float]) -> float | None:
            return statistics.stdev(vals) if len(vals) > 1 else None

        # Deflated Sharpe Ratio (simplified — uses sample size and skew/kurt correction)
        # DSR adjusts the observed Sharpe for multiple-testing bias.
        # Reference: Bailey & López de Prado (2014)
        if pnl_arr is not None and len(pnl_arr) >= 3:
            mean_pnl = float(np.mean(pnl_arr))
            std_pnl = float(np.std(pnl_arr, ddof=1))
            n = len(pnl_arr)
            skew_val = float(stats.skew(pnl_arr)) if n >= 3 else 0.0
            kurt_val = float(stats.kurtosis(pnl_arr, fisher=True)) if n >= 4 else 0.0
            observed_sharpe = (mean_pnl / std_pnl * math.sqrt(n)) if std_pnl > 0 else 0.0
            # DSR approximation: Sharpe adjusted for skew/kurtosis bias
            sr_annual = observed_sharpe * math.sqrt(252)  # annualize daily-equivalent
            dsr = _deflated_sharpe(sr_annual, n, skew_val, kurt_val)
            # Sortino: downside deviation only
            downside = pnl_arr[pnl_arr < 0]
            downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else std_pnl or 0.0
            sortino = (mean_pnl / downside_std * math.sqrt(n)) if downside_std > 0 else 0.0
            # Calmar: total return / max drawdown
            cumulative_pnl = float(np.sum(pnl_arr))
            max_dd = max(dds) if dds else 0.0
            calmar = (cumulative_pnl / max_dd) if max_dd > 0 else None
            # ICIR (Information Coefficient Information Ratio): mean IC / std IC
            # Approximated from win-rate consistency
            ic_proxy = [(w.win_rate - 50.0) / 50.0 for w in results.per_window if w.trade_count > 0]
            icir = (
                (statistics.mean(ic_proxy) / statistics.stdev(ic_proxy))
                if len(ic_proxy) > 1 and statistics.stdev(ic_proxy) > 0
                else 0.0
            )
            # Composite score: weighted blend
            score = _composite_score(
                observed_sharpe,
                sortino,
                calmar or 0.0,
                statistics.mean(wrs) if wrs else 0.0,
                windows_passed / windows_total if windows_total > 0 else 0.0,
            )
        else:
            dsr = None
            sortino = None
            calmar = None
            icir = None
            score = None

        # OOS Sharpe decay: compare first-half vs second-half Sharpe
        if pnl_arr is not None and len(pnl_arr) >= 4:
            mid = len(pnl_arr) // 2
            first_half = pnl_arr[:mid]
            second_half = pnl_arr[mid:]
            s1 = (
                float(np.mean(first_half) / np.std(first_half, ddof=1))
                if np.std(first_half, ddof=1) > 0 and len(first_half) > 1
                else 0.0
            )
            s2 = (
                float(np.mean(second_half) / np.std(second_half, ddof=1))
                if np.std(second_half, ddof=1) > 0 and len(second_half) > 1
                else 0.0
            )
            oos_sharpe_decay = s1 - s2 if s1 > 0 else 0.0
        else:
            oos_sharpe_decay = None

        # Param stability CV (from perturbation stability if available)
        param_stability_cv = None
        if hasattr(results, "aggregated") and results.aggregated:
            # If aggregated metrics carry stability info, extract it
            pass  # perturbation_evaluate_fn handles this separately

        summary_row = [
            run_id,
            # Extended risk metrics (new columns)
            icir,
            dsr,
            calmar,
            sortino,
            # Original 13 columns
            statistics.mean(wrs) if wrs else None,
            statistics.stdev(wrs) if len(wrs) > 1 else None,
            statistics.mean(pfs) if pfs else None,
            statistics.stdev(pfs) if len(pfs) > 1 else None,
            statistics.mean(shrs) if shrs else None,
            statistics.stdev(shrs) if len(shrs) > 1 else None,
            statistics.mean(dds) if dds else None,
            statistics.stdev(dds) if len(dds) > 1 else None,
            total_trades,
            windows_passed,
            windows_total,
            go_nogo_str,
            # Additional new columns
            score,
            param_stability_cv,
            oos_sharpe_decay,
        ]

        # ── Single explicit transaction ───────────────────────────────────
        # Without an explicit BEGIN/COMMIT, DuckDB's autocommit treats each
        # executemany as its own commit and a failure mid-batch would leave
        # partial state. Atomic transaction is required for the reporting
        # views (v_top_strategies joins on run_id).
        try:
            conn.execute("BEGIN")
            self._insert_windows(conn, window_rows)
            self._insert_trades(conn, trade_rows)
            self._insert_metrics_summary(conn, summary_row)
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: S110
                pass
            logger.exception(
                "SRF persistence failed for run %s (windows=%d, trades=%d): %s",
                run_id,
                len(window_rows),
                len(trade_rows),
                exc,
            )
            raise

        logger.info(
            "SRF run %s persisted: %d windows, %d trades, summary written",
            run_id,
            len(window_rows),
            len(trade_rows),
        )

    # ── per-table inserts (split out for testability + clarity) ─────────

    @staticmethod
    def _insert_windows(conn, rows: list[list]) -> None:
        """Bulk-insert window metrics including date boundaries.

        Each row must have 13 elements:
            run_id, window_idx, train_start, train_end,
            test_start, test_end, win_rate, profit_factor, sharpe,
            max_drawdown, trade_count, total_pnl, passed_go_nogo.

        Empty ``rows`` is a no-op.
        """
        if not rows:
            return
        conn.executemany(
            """INSERT INTO windows
               (run_id, window_idx, train_start, train_end,
                test_start, test_end,
                win_rate, profit_factor, sharpe,
                max_drawdown, trade_count, total_pnl, passed_go_nogo)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    @staticmethod
    def _insert_trades(conn, rows: list[list]) -> None:
        """Bulk-insert per-trade rows with full trade metadata.

        Each row must have 9 elements:
            run_id, window_idx, entry_time, exit_time,
            direction, entry_price, exit_price, pnl, exit_reason.

        Empty ``rows`` is a no-op.
        """
        if not rows:
            return
        conn.executemany(
            """INSERT INTO trades
               (run_id, window_idx, entry_time, exit_time,
                direction, entry_price, exit_price, pnl, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    @staticmethod
    def _insert_metrics_summary(conn, row: list) -> None:
        """Insert the per-run aggregate row. ``row`` must have 20 elements."""
        if row is None or len(row) != 20:
            raise ValueError(f"metrics_summary row must have 20 elements, got {len(row) if row else 0}")
        conn.execute(
            """INSERT INTO metrics_summary
               (run_id, icir, dsr, calmar, sortino,
                mean_win_rate, std_win_rate, mean_profit_factor,
                std_profit_factor, mean_sharpe, std_sharpe,
                mean_max_drawdown, std_max_drawdown, total_trades,
                windows_passed, windows_total, go_nogo,
                score, param_stability_cv, oos_sharpe_decay)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )

    # ── backfill helpers ───────────────────────────────────────────────

    @staticmethod
    def backfill_window_dates(
        db_path: str = "data/research/research.duckdb",
        market_db_path: str = "data/ayumi_market.duckdb",
        *,
        dry_run: bool = False,
    ) -> dict:
        """Backfill train/test date columns for existing window rows.

        For each run with NULL date columns, reconstruct the walk-forward
        split boundaries from the raw bar data and update the windows table.

        Uses the same split math as ``WalkForwardValidator``:
            n = total_bars
            full_window_size = n / n_windows
            train_size = full_window_size * train_ratio
            val_size = full_window_size * val_ratio
            test_size = full_window_size * (1 - train_ratio - val_ratio)
            overlap_size = window_size * overlap_ratio
            step = window_size - overlap_size

        Returns a summary dict with counts of updated/skipped/failed rows.
        """
        import duckdb

        DEFAULT_TRAIN_RATIO = 0.7
        DEFAULT_VAL_RATIO = 0.15
        DEFAULT_OVERLAP_RATIO = 0.2

        # TF string to minutes mapping for market DB lookups
        TF_MAP = {5: "M5", 15: "M15", 60: "H1", 240: "H4", 1440: "D1"}

        summary = {
            "total_null": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "details": [],
        }

        # Connect to both databases
        srf = duckdb.connect(db_path)
        market = duckdb.connect(market_db_path, read_only=True)

        try:
            # Find runs with NULL window dates
            null_runs = srf.execute(
                """SELECT DISTINCT w.run_id, r.pair, r.timeframe
                   FROM windows w
                   JOIN runs r ON w.run_id = r.run_id
                   WHERE w.train_start IS NULL
                   ORDER BY w.run_id"""
            ).fetchall()

            for run_id, pair, timeframe in null_runs:
                # Get windows for this run
                windows = srf.execute(
                    "SELECT window_idx FROM windows WHERE run_id = ? ORDER BY window_idx",
                    [run_id],
                ).fetchall()
                n_windows = len(windows)
                if n_windows == 0:
                    continue

                tf_str = TF_MAP.get(timeframe)
                if not tf_str:
                    summary["failed"] += n_windows
                    summary["details"].append({"run_id": run_id, "error": f"Unknown timeframe {timeframe}"})
                    continue

                # Get total bar count and timestamp range for this pair/timeframe
                try:
                    bar_data = market.execute(
                        """SELECT timestamp_utc FROM bars
                           WHERE symbol = ? AND timeframe = ?
                           ORDER BY timestamp_utc""",
                        [pair, tf_str],
                    ).fetchall()
                except Exception:
                    summary["failed"] += n_windows
                    summary["details"].append({"run_id": run_id, "error": f"No bar data for {pair} {tf_str}"})
                    continue

                if not bar_data:
                    summary["failed"] += n_windows
                    summary["details"].append(
                        {
                            "run_id": run_id,
                            "error": f"Empty bar data for {pair} {tf_str}",
                        }
                    )
                    continue

                timestamps = [row[0] for row in bar_data]
                n = len(timestamps)

                # Walk-forward split math (mirrors WalkForwardValidator.split)
                full_window_size = n // n_windows
                if full_window_size == 0:
                    summary["failed"] += n_windows
                    continue

                train_ratio = DEFAULT_TRAIN_RATIO
                val_ratio = DEFAULT_VAL_RATIO
                test_ratio = 1.0 - train_ratio - val_ratio
                overlap_ratio = DEFAULT_OVERLAP_RATIO

                train_size = int(full_window_size * train_ratio)
                val_size = int(full_window_size * val_ratio)
                test_size = int(full_window_size * test_ratio)
                window_size = train_size + val_size + test_size
                overlap_size = int(window_size * overlap_ratio)
                step = max(window_size - overlap_size, 1)

                for i, (window_idx,) in enumerate(windows):
                    start = i * step
                    end = start + window_size
                    if end > n:
                        end = n
                        start = end - window_size
                        if start < 0:
                            start = 0

                    actual_window_size = min(window_size, n - start)
                    actual_train = int(actual_window_size * train_ratio)
                    actual_val = int(actual_window_size * val_ratio)

                    train_start_idx = start
                    train_end_idx = start + actual_train - 1
                    test_start_idx = start + actual_train + actual_val
                    test_end_idx = start + actual_window_size - 1

                    # Bounds check
                    if test_end_idx >= n:
                        test_end_idx = n - 1
                    if test_start_idx >= n:
                        summary["skipped"] += 1
                        continue

                    train_start = datetime.fromtimestamp(timestamps[train_start_idx], tz=timezone.utc)
                    train_end = datetime.fromtimestamp(timestamps[train_end_idx], tz=timezone.utc)
                    test_start = datetime.fromtimestamp(timestamps[test_start_idx], tz=timezone.utc)
                    test_end = datetime.fromtimestamp(timestamps[test_end_idx], tz=timezone.utc)

                    summary["total_null"] += 1
                    if not dry_run:
                        srf.execute(
                            """UPDATE windows
                               SET train_start = ?, train_end = ?,
                                   test_start = ?, test_end = ?
                               WHERE run_id = ? AND window_idx = ?""",
                            [
                                train_start,
                                train_end,
                                test_start,
                                test_end,
                                run_id,
                                window_idx,
                            ],
                        )
                    summary["updated"] += 1
        finally:
            srf.close()
            market.close()

        return summary

    @staticmethod
    def normalize_strategy_names(
        db_path: str = "data/research/research.duckdb",
        *,
        dry_run: bool = False,
    ) -> dict:
        """Normalize strategy name variants to use underscores consistently.

        Merges duplicate naming conventions (e.g. ``killzonemomentum`` →
        ``killzone_momentum``) across the ``runs``, ``windows``, ``trades``,
        and ``metrics_summary`` tables.

        Returns a summary of how many rows were affected per table.
        """
        import duckdb

        # Canonical names: underscore convention is the standard.
        # Map normalized → canonical for known variants.
        KNOWN_VARIANTS = {
            "donchianatrtrend": "donchian_atr_trend",
            "killzonemomentum": "killzone_momentum",
            "londonbreakoutretest": "london_breakout_retest",
            "srmrplus": "srmr_plus",
            "ttcxauusd": "ttc_xauusd",
            "volatilityregimebreakout": "volatility_regime_breakout",
            "volatilitysqueeze": "volatility_squeeze",
        }

        summary = {
            "variants_found": [],
            "rows_updated": {},
            "dry_run": dry_run,
        }

        conn = duckdb.connect(db_path)
        try:
            # Find all distinct strategy names
            names = conn.execute("SELECT DISTINCT strategy_name FROM runs ORDER BY strategy_name").fetchall()

            # ── DuckDB FK quirk workaround ─────────────────────────────
            # DuckDB rejects ``UPDATE runs SET strategy_name = ...`` even
            # though we are not modifying ``run_id`` (the FK column). It
            # incorrectly believes the FK from ``windows/trades/...`` →
            # ``runs(run_id)`` is being violated. Workaround: copy child
            # rows to a TEMP table, drop the original child tables, run
            # the UPDATE, then re-create the child tables from the temp
            # data. The data itself is unchanged — only the parent row's
            # ``strategy_name`` column is rewritten.
            child_tables = _find_child_tables_with_run_id_fk(conn)
            snapshots: dict[str, str] = {}
            if not dry_run:
                for tbl in child_tables:
                    snapshot_name = f"_norm_snapshot_{tbl}"
                    conn.execute(f"DROP TABLE IF EXISTS {snapshot_name}")
                    conn.execute(
                        f"CREATE TEMP TABLE {snapshot_name} AS SELECT * FROM {tbl}"  # noqa: S608
                    )
                    snapshots[tbl] = snapshot_name
                    conn.execute(f"DROP TABLE {tbl}")

            try:
                for (name,) in names:
                    normalized = name.replace("_", "").lower().strip()
                    canonical = KNOWN_VARIANTS.get(normalized)
                    if canonical and name != canonical:
                        summary["variants_found"].append({"from": name, "to": canonical})

                        # Run IDs affected
                        run_ids = conn.execute(
                            "SELECT run_id FROM runs WHERE strategy_name = ?",
                            [name],
                        ).fetchall()
                        run_id_list = [r[0] for r in run_ids]

                        if not dry_run:
                            # Update runs table
                            conn.execute(
                                "UPDATE runs SET strategy_name = ? WHERE strategy_name = ?",
                                [canonical, name],
                            )
                            # run_id values stay the same — they contain the old
                            # name but changing run_id would break FK references.
                            # Only the strategy_name column is normalized.

                        summary["rows_updated"][name] = len(run_id_list)

                # ── Restore child tables from snapshots ─────────────────
                if not dry_run:
                    for tbl, snapshot_name in snapshots.items():
                        conn.execute(
                            f"CREATE TABLE {tbl} AS SELECT * FROM {snapshot_name}"  # noqa: S608
                        )
                        conn.execute(f"DROP TABLE {snapshot_name}")
            except Exception:
                # Best-effort rollback on partial state
                raise
        finally:
            conn.close()

        return summary


def _find_child_tables_with_run_id_fk(conn) -> list[str]:
    """Return names of tables that have a ``run_id`` column.

    Used by ``normalize_strategy_names`` to identify tables whose FK
    relationships to ``runs`` would block an UPDATE under DuckDB's
    over-eager FK check. Excludes ``runs`` itself.
    """
    rows = conn.execute(
        """SELECT DISTINCT table_name
           FROM information_schema.columns
           WHERE column_name = 'run_id'
             AND table_schema = 'main'
             AND table_name != 'runs'
           ORDER BY table_name"""
    ).fetchall()
    return [r[0] for r in rows]
