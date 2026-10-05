#!/usr/bin/env python3
"""SFA-3 end-to-end pilot runner.

Card 96a82c52.  Phase 1 exit gate (spec §7): wire tournament survivors
through the factory validation pipeline end-to-end on the existing
strategy pool, persist verdicts to ``research.duckdb``, and emit a
markdown summary under ``docs/research/``.

Pipeline
--------
1. (Optional) Run the tournament harness over a chosen strategy pool
   when ``--re-run-tournament`` is passed.  By default we reuse the
   canonical scorecard (e.g. ``scorecard_EURUSD_H1_full_all17.json``)
   so the front door can be exercised without re-paying the ~40 min
   tournament cost.
2. Apply the FTMO survivor filter via :func:`select_survivors`.
3. Build :class:`CandidateSpec` rows per survivor.
4. Run :class:`ValidationRunner` end-to-end (WF + DSR + spread-cost gate).
5. Persist verdicts via :class:`FactoryVerdictStore` to
   ``data/research/research.duckdb``.
6. Emit a ranked-table markdown summary.

Hard rules (SFA-3 spec §7 + Liora ground rule)
----------------------------------------------
* OOS Jan–Jul 2026 is **locked** — bars are clipped to the pre-OOS
  window unless ``--unlock-oos`` is passed AND ``--include-oos`` is
  passed (the validation runner's OOS guard refuses bars unless
  ``CandidateSpec.oos_unlocked=True``).
* Spread cost table is sourced from
  :func:`forex_bot.factory.spread_costs.default_spread_costs` — the
  single source of truth for the Liora-ground-rule defaults.  No
  inline magic numbers.

CLI usage
---------
::

    python3 scripts/run_pilot_sfa3.py \\
        --scorecard data/tournament/scorecard_EURUSD_H1_full_all17.json \\
        --pair EURUSD --timeframe H1 \\
        --research-db data/research/research.duckdb \\
        --summary-path docs/research/sfa-3-pilot-2026-10-04.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
# Mirror run_tournament.py: prepend both ``src/`` (so ``tournament``
# resolves) and ``src/forex_bot`` (so strategies.core_types resolves).
for _p in (str(_REPO / "src"), str(_REPO / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tournament.front_door import (  # noqa: E402
    render_pilot_summary,
    run_front_door,
)

logger = logging.getLogger("ayumi.tournament.pilot")


def _strategy_factory() -> Callable[[str], Any]:
    """Return a ``(strategy_id) -> ISignalStrategy-like`` closure.

    Built lazily so the harness-side imports happen only on first call
    (the ``tournament.harness._build_strategy_instance`` helper does the
    constructor-shape introspection card 9e9aaf30 added).
    """
    from tournament.harness import _build_strategy_instance

    def _factory(strategy_id: str) -> Any:
        return _build_strategy_instance(strategy_id)

    return _factory


def _resolve_bar_db(explicit: str | None) -> Path:
    """Resolve the DuckDB bar source (explicit → env → tournament-resolver).

    Mirrors :func:`tournament.harness.resolve_default_duckdb_path` so the
    pilot finds the primary worktree's ``ayumi_market.duckdb`` when run
    from a feature worktree that does not symlink the data directory.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("AYUMI_DUCKDB_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # Import the tournament resolver lazily so the script remains
    # importable without the heavy strategy modules at startup.
    from tournament.harness import resolve_default_duckdb_path

    return resolve_default_duckdb_path()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pilot_sfa3",
        description=(
            "SFA-3 end-to-end pilot — wire tournament survivors through "
            "factory validation (spec §7 Phase 1 exit gate)."
        ),
    )
    parser.add_argument(
        "--scorecard",
        required=True,
        type=Path,
        help="Tournament scorecard JSON (output of scripts/run_tournament.py).",
    )
    parser.add_argument(
        "--pair",
        default="EURUSD",
        help="Default pair when the scorecard row omits it (default: EURUSD).",
    )
    parser.add_argument(
        "--timeframe",
        default="H1",
        help="Default timeframe when the scorecard row omits it (default: H1).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="DuckDB bar source (default: data/ayumi_market.duckdb).",
    )
    parser.add_argument(
        "--research-db",
        type=Path,
        default=_REPO / "data" / "research" / "research.duckdb",
        help="research.duckdb path (default: data/research/research.duckdb).",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=_REPO
        / "docs"
        / "research"
        / f"sfa-3-pilot-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md",
        help="Markdown summary output (default: docs/research/sfa-3-pilot-YYYY-MM-DD.md).",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=20,
        help="Survivor min trade count (default: 20 — Liora ground rule).",
    )
    parser.add_argument(
        "--max-dd",
        type=float,
        default=10.0,
        help="Survivor max drawdown percent (default: 10.0 — Liora ground rule).",
    )
    parser.add_argument(
        "--unlock-oos",
        action="store_true",
        help="Forward OOS unlock to CandidateSpec (validation mode).",
    )
    parser.add_argument(
        "--include-oos",
        action="store_true",
        help="Include OOS-window bars when loading from DuckDB (counselled — breaks spec §4.5 lock).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    level = "DEBUG" if args.verbose else "INFO"
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )

    bar_db = _resolve_bar_db(str(args.db_path) if args.db_path else None)
    if not bar_db.is_file():
        print(f"ERROR: DuckDB bar source not found: {bar_db}", file=sys.stderr)
        return 2

    factory = _strategy_factory()

    from tournament.front_door import SurvivorCriteria

    criteria = SurvivorCriteria(
        max_drawdown_pct=args.max_dd,
        min_trades=args.min_trades,
    )

    result = run_front_door(
        scorecard_path=args.scorecard,
        db_path=bar_db,
        pair=args.pair,
        timeframe=args.timeframe,
        research_db=args.research_db,
        strategy_factory=factory,
        criteria=criteria,
        oos_unlocked=args.unlock_oos,
        include_oos=args.include_oos,
    )

    # Pull harness meta from the scorecard for the summary header.
    scorecard_meta: dict | None = None
    try:
        payload = json.loads(args.scorecard.read_text())
        if isinstance(payload, dict):
            scorecard_meta = payload.get("meta")
    except (json.JSONDecodeError, OSError):
        pass

    title = f"SFA-3 Pilot — {args.scorecard.stem}"
    summary = render_pilot_summary(result, title=title, scorecard_meta=scorecard_meta)

    args.summary_path.parent.mkdir(parents=True, exist_ok=True)
    args.summary_path.write_text(summary)

    print(f"PILOT_OK scorecard={args.scorecard.name} "
          f"survivors={result.survivor_count} candidates={result.candidate_count} "
          f"verdicts={result.verdict_count} written={result.row_counts.get('written', 0)} "
          f"summary={args.summary_path} db={args.research_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
