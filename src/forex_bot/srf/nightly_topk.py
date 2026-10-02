"""SRF Phase 3 — Nightly top-K re-evaluation cron.

Re-runs the top-K candidates from the research DB with fresh data.
Writes results back to research.duckdb.

Usage:
    python -m srf.nightly_topk --top-k 10
    python -m srf.nightly_topk --top-k 10 --pairs GBPUSD,EURUSD

Intended to run as a nightly cron job.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def get_top_candidates(conn, top_k: int = 10, pairs: list[str] | None = None) -> list[dict]:
    """Fetch top-K candidates by DSR from research.duckdb."""
    query = """
        SELECT r.run_id, r.strategy_name, r.pair, r.timeframe,
               r.params_json, m.dsr, m.mean_profit_factor as pf
        FROM runs r
        JOIN metrics_summary m ON r.run_id = m.run_id
        WHERE m.dsr IS NOT NULL AND r.status = 'completed'
    """
    if pairs:
        placeholders = ",".join("?" for _ in pairs)
        query += f" AND r.pair IN ({placeholders})"
        params = pairs
    else:
        params = []

    query += " ORDER BY m.dsr DESC LIMIT ?"
    params.append(top_k)

    rows = conn.execute(query, params).fetchall()
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r)) for r in rows]  # noqa: B905


def re_evaluate_candidate(conn, candidate: dict) -> dict | None:
    """Re-run a single candidate with fresh data.

    This is a placeholder — the actual re-evaluation requires loading
    fresh bars and re-running the walk-forward engine. In production,
    this would call StrategyRunner.run() with the candidate's params.
    """
    try:
        # TODO: wire to actual strategy re-run
        # For now, log that we'd re-evaluate
        logger.info(
            "Re-evaluating %s %s %dm (DSR was %.3f)",
            candidate["strategy_name"],
            candidate["pair"],
            candidate["timeframe"],
            candidate.get("dsr", 0),
        )
        return None
    except Exception as e:
        logger.error("Re-evaluation failed for %s: %s", candidate["run_id"], e)
        return None


def nightly_topk(top_k: int = 10, pairs: list[str] | None = None) -> dict:
    """Run the nightly top-K re-evaluation pipeline.

    Returns summary dict with candidates checked, re-evaluated, promoted.
    """
    from srf.schema import SRFDatabase

    db_path = PROJECT_ROOT / "data" / "research" / "research.duckdb"

    if not db_path.exists():
        logger.warning("research.duckdb not found at %s — nothing to re-evaluate", db_path)
        return {"status": "no_db", "candidates": 0}

    started_at = datetime.now(timezone.utc)
    logger.info("Nightly top-K started: top_%d, pairs=%s", top_k, pairs or "all")

    with SRFDatabase(str(db_path)) as conn:
        candidates = get_top_candidates(conn, top_k, pairs)
        logger.info("Found %d candidates to re-evaluate", len(candidates))

        re_evaluated = 0
        promoted = 0

        for c in candidates:
            result = re_evaluate_candidate(conn, c)
            if result:
                re_evaluated += 1
                if result.get("go_nogo"):
                    promoted += 1

        # Log to cron_runs table
        conn.execute(
            "INSERT INTO cron_runs (cron_start, cron_end, exit_code, run_count, status) VALUES (?, ?, ?, ?, ?)",
            [started_at, datetime.now(timezone.utc), 0, len(candidates), "ok"],
        )

    summary = {
        "status": "ok",
        "candidates": len(candidates),
        "re_evaluated": re_evaluated,
        "promoted": promoted,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "Nightly top-K done: %d checked, %d re-evaluated, %d promoted",
        len(candidates),
        re_evaluated,
        promoted,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="SRF Nightly Top-K Re-evaluation")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--pairs", type=str, default=None, help="Comma-separated pairs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    pairs = args.pairs.split(",") if args.pairs else None

    if args.dry_run:
        from srf.schema import SRFDatabase

        db_path = PROJECT_ROOT / "data" / "research" / "research.duckdb"
        if db_path.exists():
            with SRFDatabase(str(db_path)) as conn:
                candidates = get_top_candidates(conn, args.top_k, pairs)
                print(f"Dry run: {len(candidates)} candidates would be re-evaluated")
                for c in candidates:
                    print(f"  {c['strategy_name']} {c['pair']} {c['timeframe']}m DSR={c.get('dsr', 'N/A')}")
        else:
            print("No research.duckdb found")
        return

    result = nightly_topk(args.top_k, pairs)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
