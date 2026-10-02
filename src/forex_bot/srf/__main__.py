"""SRF CLI entry point: python -m srf run/query"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .schema import SRFDatabase


def cmd_run(args: argparse.Namespace) -> int:
    """Run a strategy sweep through the SRF pipeline."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from .runner import StrategyRunner

    # Dynamically import strategy
    parts = args.strategy.rsplit(".", 1)
    if len(parts) == 2:
        # Module.Class format
        import importlib

        mod = importlib.import_module(parts[0])
        strategy_cls = getattr(mod, parts[1])
    else:
        # Registered name — try discovery
        from .registry import discover_strategies, get_strategy

        discover_strategies()
        reg = get_strategy(args.strategy)
        if reg is None:
            print(f"Strategy '{args.strategy}' not found. Use Module.Class or registered name.")
            return 1
        strategy_cls = reg.strategy_class

    # Build factory
    def factory():
        return strategy_cls()

    runner = StrategyRunner(
        db_path=args.db_path,
        repo_path=args.repo,
    )

    params = {}
    if args.params:
        params = json.loads(args.params)

    result = runner.run(
        strategy_name=args.name or args.strategy.split(".")[-1].lower(),
        strategy_factory=factory,
        pair=args.pair,
        timeframe=args.tf,
        data_path=args.data,
        params=params,
        n_windows=args.windows,
        initial_balance=args.balance,
        spread_pips=args.spread,
        min_confidence=args.confidence,
    )

    print(json.dumps(result, indent=2))
    return 0 if result["go_nogo"] else 1


def cmd_query(args: argparse.Namespace) -> int:
    """Query results from the SRF database."""
    db = SRFDatabase(args.db_path)
    with db as conn:
        if args.top:
            rows = conn.execute(
                """SELECT strategy_name, pair, timeframe, dsr,
                          CAST(pf AS DECIMAL(8,2)) as pf,
                          CAST(wr AS DECIMAL(8,2)) as wr,
                          windows_passed, windows_total, go_nogo
                   FROM v_top_strategies
                   ORDER BY COALESCE(dsr, 0) DESC
                   LIMIT ?""",
                [args.top],
            ).fetchall()

            if not rows:
                print("No completed runs found.")
                return 0

            print(f"{'Strategy':<20} {'Pair':<8} {'TF':>4} {'DSR':>6} {'PF':>6} {'WR':>6} {'W/T':>6} {'Go/No':<6}")
            print("-" * 72)
            for r in rows:
                dsr = f"{r[3]:.2f}" if r[3] else "—"
                pf = f"{float(r[4]):.2f}" if r[4] else "—"
                wr = f"{float(r[5]):.1%}" if r[5] else "—"
                wt = f"{r[6]}/{r[7]}"
                print(f"{r[0]:<20} {r[1]:<8} {r[2]:>4}m {dsr:>6} {pf:>6} {wr:>6} {wt:>6} {r[8]:<6}")

        elif args.pair:
            rows = conn.execute(
                "SELECT * FROM v_pair_performance WHERE pair=?",
                [args.pair],
            ).fetchall()
            for r in rows:
                print(r)

        elif args.promotion:
            rows = conn.execute("SELECT * FROM v_promotion_queue").fetchall()
            if not rows:
                print("No strategies in promotion queue.")
            for r in rows:
                print(r)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="srf",
        description="Strategy Research Framework — run sweeps, query results",
    )
    parser.add_argument("--version", action="version", version=f"srf {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── run ──────────────────────────────────────────────────────────────
    p_run = sub.add_parser("run", help="Run a strategy sweep")
    p_run.add_argument("--strategy", required=True, help="Strategy name or Module.Class path")
    p_run.add_argument("--name", help="Override strategy name in DB")
    p_run.add_argument("--pair", required=True, help="Currency pair (e.g. GBPUSD)")
    p_run.add_argument("--tf", type=int, required=True, help="Timeframe in minutes (e.g. 15)")
    p_run.add_argument("--data", required=True, help="Path to bar data CSV")
    p_run.add_argument("--params", help="JSON string of strategy params")
    p_run.add_argument("--windows", type=int, default=5, help="Number of walk-forward windows")
    p_run.add_argument("--balance", type=float, default=10_000, help="Initial balance")
    p_run.add_argument("--spread", type=float, default=None, help="Spread in pips")
    p_run.add_argument("--confidence", type=float, default=0.30, help="Min signal confidence")
    p_run.add_argument("--db-path", default="data/research/research.duckdb")
    p_run.add_argument("--repo", default=".", help="Repo root for git checks")
    p_run.set_defaults(func=cmd_run)

    # ── query ────────────────────────────────────────────────────────────
    p_query = sub.add_parser("query", help="Query results from the database")
    p_query.add_argument("--top", type=int, help="Show top N strategies by DSR")
    p_query.add_argument("--pair", help="Show best strategy for a specific pair")
    p_query.add_argument("--promotion", action="store_true", help="Show promotion queue")
    p_query.add_argument("--db-path", default="data/research/research.duckdb")
    p_query.set_defaults(func=cmd_query)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
