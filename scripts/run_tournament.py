#!/usr/bin/env python3
"""Tournament CLI — run strategies on a historical window, emit scorecard.

Card db04d5b5 (walking skeleton).  This is the only entry point for the
tournament harness.  The ``--smoke`` flag wires sane defaults so a fresh
clone with the in-worktree parquet / main-tree DuckDB produces a
deterministic scorecard on the first invocation.

Usage::

    python3 scripts/run_tournament.py --smoke
    python3 scripts/run_tournament.py --strategies srmr_plus bb_rsi_reversion --window 2024-06-03:2024-06-09
    python3 scripts/run_tournament.py --help
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Make the tournament module importable.  When invoked as a script from
# any CWD, prepend both ``src/`` (so ``tournament`` resolves) and
# ``src/forex_bot`` (so strategies.core_types resolves).  Mirrors the
# pythonpath declared in pytest.ini so ``--smoke`` produces the same
# import context as ``pytest``.
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
for _p in (str(_REPO / "src"), str(_REPO / "src" / "forex_bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tournament import (  # noqa: E402
    STRATEGY_CLASS_MAP,
    TournamentEmptyWindow,
    TournamentHarness,
    render_console_table,
    render_scorecard_json,
)

logger = logging.getLogger("ayumi.tournament.cli")


def _default_smoke_window() -> tuple[str, str]:
    """Resolve a deterministic 5-day smoke window anchored to dataset coverage.

    Picks a hardcoded 2024-06-03 → 2024-06-09 range that matches the
    DuckDB smoke slice (USDJPY H1, ~120 bars).  Hardcoded rather than
    computed so re-runs are byte-identical.
    """
    return "2024-06-03", "2024-06-09"


def _parse_window(arg: str) -> tuple[str, str]:
    """Parse ``YYYY-MM-DD:YYYY-MM-DD`` (colon-separated)."""
    if ":" not in arg:
        raise argparse.ArgumentTypeError(
            f"--window expects 'YYYY-MM-DD:YYYY-MM-DD', got {arg!r}"
        )
    a, b = arg.split(":", 1)
    for part in (a, b):
        try:
            datetime.strptime(part, "%Y-%m-%d")
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"--window dates must be YYYY-MM-DD; got {part!r}: {exc}"
            ) from exc
    return a, b


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_tournament",
        description=(
            "Tournament harness — register ≥1 strategy, run on a historical window, "
            "emit a ranked scorecard (JSON + console) with FTMO-constraint columns."
        ),
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=list(STRATEGY_CLASS_MAP.keys()),
        metavar="STRATEGY_ID",
        help=(
            f"Strategy ids to run (default from STRATEGY_CLASS_MAP): "
            f"{', '.join(STRATEGY_CLASS_MAP)}.  Strategies run UNMODIFIED."
        ),
    )
    parser.add_argument(
        "--symbol",
        default="USDJPY",
        help="Bar symbol (default: USDJPY — the smoke slice is H1-only on USDJPY).",
    )
    parser.add_argument(
        "--timeframe",
        default="H1",
        choices=["M3", "M5", "M15", "M30", "H1"],
        help=(
            "Bar timeframe (default H1).  Supported choices: M3, M5, M15, M30, H1.  "
            "Sub-hour choices (M3, M5) and M30 are wired for strategies that "
            "register at those timeframes; clean main currently ships H1 + M15 "
            "data and raises TournamentEmptyWindow if the DuckDB bars table "
            "lacks the requested timeframe."
        ),
    )
    parser.add_argument(
        "--window",
        type=_parse_window,
        default=None,
        help="Historical window 'YYYY-MM-DD:YYYY-MM-DD' (inclusive).  Default: smoke window.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help=(
            "DuckDB bar source.  Default: auto-resolved via git worktree list "
            "(primary/main tree's data/ayumi_market.duckdb).  Override with $AYUMI_DUCKDB_PATH "
            "or this flag."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default: data/tournament/scorecard.json; --smoke overrides to scorecard_smoke.json).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Smoke mode — wires defaults: 2 default strategies, USDJPY H1, hardcoded 2024-06-03..09 "
            "window (or any window that fits DuckDB coverage), output to data/tournament/scorecard_smoke.json. "
            "Intended for CI + first-run sanity checks."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def _resolve_output_path(args: argparse.Namespace) -> Path:
    """Resolve the JSON output path with --smoke default."""
    if args.output is not None:
        return Path(args.output)
    name = "scorecard_smoke.json" if args.smoke else "scorecard.json"
    return _REPO / "data" / "tournament" / name


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    level = "DEBUG" if args.verbose else "INFO"
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )

    # Apply --smoke default overrides
    window = args.window
    output_path = _resolve_output_path(args)
    if args.smoke:
        if window is None:
            window = _default_smoke_window()
        # Constrain to two strategies even if STRATEGY_CLASS_MAP grows.
        smoke_ids = [sid for sid in ("srmr_plus", "bb_rsi_reversion") if sid in STRATEGY_CLASS_MAP]
        if not smoke_ids:
            smoke_ids = list(STRATEGY_CLASS_MAP.keys())[:2]
        args.strategies = smoke_ids

    if window is None:
        window = _default_smoke_window()
    start_date, end_date = window

    # Build the harness
    db_path: str | Path | None = args.db_path
    if db_path is None:
        # Pass through env override so resolve_default_duckdb_path honors it
        # (Harness picks up AYUMI_DUCKDB_PATH in its own resolver).
        pass

    harness = TournamentHarness(
        strategy_ids=list(args.strategies),
        db_path=db_path,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_date=start_date,
        end_date=end_date,
    )

    try:
        scorecard = harness.run()
    except TournamentEmptyWindow as exc:
        logger.error("TournamentEmptyWindow: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        logger.error("File not found: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Console output
    print("=" * 72)
    print("  TOURNAMENT SCORECARD")
    print("=" * 72)
    meta = scorecard.meta
    harness_meta = meta.get("harness_meta", {})
    bars_loaded = harness_meta.get("bars_loaded", "?")
    print(f"  Window:      {start_date} → {end_date}  ({bars_loaded} bars)")
    print(f"  Symbol/TF:   {args.symbol}/{args.timeframe}")
    print(f"  Source:      {harness.source}")
    if meta.get("run_meta"):
        for sid, rm in sorted(meta["run_meta"].items()):
            if rm.get("skipped"):
                print(f"  {sid:<28} SKIPPED  ({rm.get('error', 'unknown')})")
            else:
                print(f"  {sid:<28} signals={rm.get('signals', 0):>4}   trades={rm.get('trades', 0):>4}")
    print()
    print(render_console_table(scorecard))
    print()

    # JSON output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_scorecard_json(scorecard)
    output_path.write_text(payload)
    print(f"Scorecard JSON written to {output_path}")

    # Summary line for log scrapers
    print(
        f"TOURNAMENT_OK rows={len(scorecard.rows)} "
        f"top={scorecard.rows[0].strategy_id if scorecard.rows else 'NONE'} "
        f"return_pct={scorecard.rows[0].return_pct if scorecard.rows else 0.0:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
