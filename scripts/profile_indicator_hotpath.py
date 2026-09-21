#!/usr/bin/env python3
"""Profile the tournament harness on a short EURUSD H1 window.

Card b1bb93e8 — confirm the O(N²) hot path before optimizing.
Outputs a cProfile dump; not part of the test suite; kept as a builder
helper under scripts/.

Usage:
    .venv/bin/python scripts/profile_indicator_hotpath.py \
        --window 2024-01-01:2024-03-01 --strategies srmr_plus bb_rsi_reversion \
        --out data/diagnostics/profile_before.json
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import logging
import pstats
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
for _p in (str(_REPO / "src"), str(_REPO / "src" / "forex-bot")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from tournament import STRATEGY_CLASS_MAP, TournamentHarness  # noqa: E402


def _parse_window(arg: str) -> tuple[str, str]:
    a, b = arg.split(":", 1)
    return a, b


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", required=True, type=_parse_window)
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument(
        "--strategies",
        nargs="+",
        default=["srmr_plus", "bb_rsi_reversion"],
        choices=sorted(STRATEGY_CLASS_MAP.keys()),
    )
    parser.add_argument("--out", default="data/diagnostics/profile.json")
    parser.add_argument("--top-n", type=int, default=25)
    args = parser.parse_args()

    start, end = args.window
    harness = TournamentHarness(
        strategy_ids=args.strategies,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_date=start,
        end_date=end,
    )

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    try:
        scorecard = harness.run()
    finally:
        pr.disable()
    wall = time.perf_counter() - t0

    s = io.StringIO()
    pstats.Stats(pr, stream=s).strip_dirs().sort_stats("cumulative").print_stats(args.top_n)
    pstats_text = s.getvalue()
    print(pstats_text)

    # Also by total time
    s2 = io.StringIO()
    pstats.Stats(pr, stream=s2).strip_dirs().sort_stats("tottime").print_stats(args.top_n)
    by_tottime = s2.getvalue()
    print("--- by tottime ---")
    print(by_tottime)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "wall_seconds": wall,
                "strategies": args.strategies,
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "window": [start, end],
                "by_cumulative": pstats_text,
                "by_tottime": by_tottime,
            },
            indent=2,
        )
    )
    print(f"\nWall time: {wall:.2f}s")
    print(f"Strategy rows: {len(scorecard.rows)}")
    print(f"Profile written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
