"""SRF Phase 3 — Weekly deep sweep (cron entry point).

Runs every strategy × pair × timeframe combo through ``StrategyRunner.run()``
and logs a summary row to ``cron_runs``.

Cron: ``b5a3e5fb-9763-4a1f-99ac-d01cce71ac94``, schedule ``0 4 * * 6``
America/Toronto (Saturdays 4am EDT).

Usage::

    python -m srf.weekly_sweep
    python -m srf.weekly_sweep --strategies srmr_plus,killzone_momentum --pairs GBPUSD,EURUSD
    python -m srf.weekly_sweep --trials 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve project root (this file lives at src/forex_bot/srf/weekly_sweep.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Default sweep grid (preserved from previous implementation)
DEFAULT_PAIRS = ["GBPUSD", "EURUSD", "USDJPY", "XAUUSD"]
DEFAULT_TIMEFRAMES = ["M15", "H1"]
DEFAULT_TRIALS = 50
DEFAULT_WINDOWS = 5

# Timeframe string → minutes
_TF_MINUTES: dict[str, int] = {
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def _resolve_data_path(pair: str, tf_str: str) -> Path:
    """Resolve the CSV data path for a pair/timeframe combo."""
    return PROJECT_ROOT / "data" / "forex" / "historical" / f"{pair}_{tf_str}.csv"


def weekly_sweep(
    strategies: list[str] | None = None,
    pairs: list[str] | None = None,
    timeframes: list[str] | None = None,
    trials_per_combo: int = DEFAULT_TRIALS,
) -> dict:
    """Run the full SRF weekly sweep across all strategy × pair × timeframe combos.

    Each combo is passed to ``StrategyRunner.run()`` which writes results
    to the ``runs`` and ``metrics_summary`` tables in ``research.duckdb``.
    A single ``cron_runs`` row summarises the sweep.
    """
    from .registry import get_strategy_factories
    from .runner import StrategyRunner

    pairs = pairs or DEFAULT_PAIRS
    timeframes = timeframes or DEFAULT_TIMEFRAMES
    db_path = PROJECT_ROOT / "data" / "research" / "research.duckdb"

    # ── Resolve strategies ────────────────────────────────────────────
    all_factories = get_strategy_factories()

    if strategies:
        factories = {n: f for n, f in all_factories.items() if n in strategies}
        missing = set(strategies) - set(all_factories.keys())
        if missing:
            logger.warning("Unknown strategies ignored: %s", ", ".join(sorted(missing)))
    else:
        factories = all_factories

    started_at = datetime.now(timezone.utc)
    total_combos = len(factories) * len(pairs) * len(timeframes)

    logger.info(
        "Weekly sweep started: %d strategies × %d pairs × %d timeframes = %d combos",
        len(factories),
        len(pairs),
        len(timeframes),
        total_combos,
    )

    # ── Run sweep ─────────────────────────────────────────────────────
    runner = StrategyRunner(db_path=str(db_path), repo_path=str(PROJECT_ROOT))

    successes = 0
    failures = 0
    failure_details: list[str] = []

    for strat_name, strat_factory in sorted(factories.items()):
        for pair in pairs:
            for tf_str in timeframes:
                tf_minutes = _TF_MINUTES.get(tf_str)
                if tf_minutes is None:
                    failures += 1
                    failure_details.append(f"{strat_name}/{pair}/{tf_str}: unknown timeframe")
                    continue

                data_path = _resolve_data_path(pair, tf_str)
                if not data_path.exists():
                    failures += 1
                    failure_details.append(f"{strat_name}/{pair}/{tf_str}: no data file")
                    logger.debug(
                        "Skip %s/%s/%s — %s not found",
                        strat_name,
                        pair,
                        tf_str,
                        data_path.name,
                    )
                    continue

                try:
                    result = runner.run(
                        strategy_name=strat_name,
                        strategy_factory=strat_factory,
                        pair=pair,
                        timeframe=tf_minutes,
                        data_path=str(data_path),
                        n_windows=DEFAULT_WINDOWS,
                    )
                    successes += 1
                    logger.info(
                        "OK %s/%s/%s — go_nogo=%s",
                        strat_name,
                        pair,
                        tf_str,
                        result.get("go_nogo"),
                    )
                except Exception as exc:
                    failures += 1
                    failure_details.append(f"{strat_name}/{pair}/{tf_str}: {type(exc).__name__}: {exc}")
                    logger.warning("FAIL %s/%s/%s — %s", strat_name, pair, tf_str, exc)

    # ── Log cron_runs row ─────────────────────────────────────────────
    completed_at = datetime.now(timezone.utc)
    exit_code = 0 if successes > 0 else 1
    status = "weekly_sweep:ok" if exit_code == 0 else "weekly_sweep:failed"

    if db_path.exists():
        try:
            from .schema import SRFDatabase

            with SRFDatabase(str(db_path)) as conn:
                conn.execute(
                    "INSERT INTO cron_runs (cron_start, cron_end, exit_code, run_count, status) VALUES (?, ?, ?, ?, ?)",
                    [started_at, completed_at, exit_code, successes, status],
                )
            logger.info(
                "Logged cron_runs row: exit_code=%d run_count=%d status=%s",
                exit_code,
                successes,
                status,
            )
        except Exception as exc:
            # DB write failure is logged but does NOT fail the cron.
            logger.error("Failed to log cron_runs row: %s", exc)
    else:
        logger.warning("research.duckdb not found at %s — cron_runs row not written", db_path)

    # ── Summary ───────────────────────────────────────────────────────
    summary = {
        "status": "ok" if exit_code == 0 else "failed",
        "total_combos": total_combos,
        "successes": successes,
        "failures": failures,
        "strategies": sorted(factories.keys()),
        "pairs": pairs,
        "timeframes": timeframes,
        "trials_per_combo": trials_per_combo,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
    }
    if failure_details:
        summary["failure_sample"] = failure_details[:10]

    logger.info(
        "Weekly sweep done: %d/%d succeeded, exit_code=%d",
        successes,
        total_combos,
        exit_code,
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="SRF Weekly Deep Sweep")
    parser.add_argument("--strategies", type=str, default=None, help="Comma-separated strategy names")
    parser.add_argument("--pairs", type=str, default=None, help="Comma-separated pairs")
    parser.add_argument("--timeframes", type=str, default=None, help="Comma-separated timeframes")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help="Trials per combo (metadata)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = weekly_sweep(
        strategies=args.strategies.split(",") if args.strategies else None,
        pairs=args.pairs.split(",") if args.pairs else None,
        timeframes=args.timeframes.split(",") if args.timeframes else None,
        trials_per_combo=args.trials,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
