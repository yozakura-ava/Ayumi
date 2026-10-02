#!/usr/bin/env python3
"""CLI wrapper for the calendar-anchored walk-forward runner (card 1312f03a).

Usage:
    python3 scripts/wf_runner.py --strategy ttc_xauusd --params '{"fast":10}' \
        --anchor 2022-01 --windows 4 --window-months 6 --step-months 1

Emits the validation report as JSON on stdout, including per-window
PF/Sharpe/WR/max-DD, aggregate PF with 95% CI, regime labels per window and
``pip_value_validated: true``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow running both from repo root and from src/forex_bot/.
_HERE = Path(__file__).resolve()
for base in (_HERE.parent.parent / "src" / "forex_bot", _HERE.parent.parent.parent):
    if (base / "backtest" / "walk_forward.py").exists():
        sys.path.insert(0, str(base))
        break

from backtest.walk_forward import (  # noqa: E402
    STRATEGY_REGISTRY,
    run_walk_forward,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="wf_runner",
        description="Walk-forward validation runner (calendar-anchored windows).",
    )
    p.add_argument("--strategy", required=True, help="registered strategy id (e.g. ttc_xauusd)")
    p.add_argument("--params", default="{}", help="candidate params as JSON string")
    p.add_argument("--params-file", default=None, help="read candidate params JSON from file")
    p.add_argument("--anchor", default="2022-01", help="anchor month YYYY-MM (default 2022-01)")
    p.add_argument("--windows", type=int, default=4, help="number of windows M (default 4)")
    p.add_argument("--window-months", type=int, default=6, help="window length in months (default 6)")
    p.add_argument("--step-months", type=int, default=1, help="step between windows in months (default 1)")
    p.add_argument("--symbol", default="XAUUSD", help="trading symbol (default XAUUSD)")
    p.add_argument("--account-currency", default="USD", help="account currency (default USD)")
    p.add_argument("--lot-size", type=float, default=1.0, help="lot size for sizing (default 1.0)")
    p.add_argument("--timestamps-file", default=None, help="newline-separated ISO timestamps to slice windows over")
    p.add_argument("--list-strategies", action="store_true", help="list registered strategy ids and exit")
    return p.parse_args(argv)


def _load_timestamps(path: str) -> list[datetime]:
    out: list[datetime] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(datetime.fromisoformat(line))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.list_strategies:
        print(json.dumps({"strategies": sorted(STRATEGY_REGISTRY)}, indent=2))
        return 0

    if args.params_file:
        params = json.loads(Path(args.params_file).read_text())
    else:
        params = json.loads(args.params)

    timestamps = _load_timestamps(args.timestamps_file) if args.timestamps_file else []

    try:
        report = run_walk_forward(
            strategy_id=args.strategy,
            candidate_params=params,
            anchor_date=args.anchor,
            n_windows=args.windows,
            window_months=args.window_months,
            step_months=args.step_months,
            timestamps=timestamps,
            symbol=args.symbol,
            account_currency=args.account_currency,
            lot_size=args.lot_size,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
