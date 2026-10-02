#!/usr/bin/env python3
"""Parameter tuning cycle orchestrator — XAUUSD regime-gated active blade.

Automates the 24-candidate × 4-window tuning cycle bounded by
``docs/trading/active-blade.md`` § Tuning Budget (branch
``autodev/one-blade-focus``):

    24 candidates = 3 regimes × 2 sizing profiles × 4 signal-weight ratios
    4 walk-forward windows per candidate, hard -8% max-DD kill, ≤12h wall
    per candidate, ≤4 cycles/year, 30-trade live minimum before any metric
    is quoted.

Grid decomposition lives in ``backtest.candidate_generator`` and is frozen —
this orchestrator only executes it.

Output layout under ``data/trading/tuning_runs/<cycle_id>/``:

    candidates/<candidate_id>.json   per-candidate walk-forward record
    manifest.json                    which candidate hit each exit criterion
    summary.json                     cycle-level rollup + gates

pip_value discipline (inherited AC, card 0d562c69 verdict): every report
emits ``pip_value_validated: true`` computed via
``backtest.pip_value.compute_pip_value`` — never the legacy
``core.pip.PipCalculator`` (XAUUSD price-heuristic mis-classification).

Usage:
    python3 scripts/tuning_cycle.py --help
    python3 scripts/tuning_cycle.py --dry-run
    python3 scripts/tuning_cycle.py --cycle-id 2026Q4-a --regime trending
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from backtest.candidate_generator import (  # noqa: E402
    MAX_DD_HARD_KILL,
    N_WINDOWS,
    REGIMES,
    Candidate,
    candidates_for_regime,
    candidates_manifest,
    generate_candidates,
    validate_cycle_id,
)
from backtest.engine import Bar  # noqa: E402
from backtest.pip_value import compute_pip_value  # noqa: E402
from backtest.strategies import TTSStrategy  # noqa: E402
from backtest.walk_forward_runner import run_strategy_walk_forward  # noqa: E402
from signal_engine.risk_sizer import ConfidencePositionSizer, parse_tiers  # noqa: E402

DEFAULT_PAIR = "XAUUSD"
DEFAULT_TIMEFRAME = "M15"
DEFAULT_CSV = "data/forex/historical/XAUUSD_{tf}.csv"
DEFAULT_SPREAD = 3.0
DEFAULT_COMMISSION = 3.5
DEFAULT_MIN_CONFIDENCE = 0.55
DEFAULT_MIN_QUALITY = 0.60
DEFAULT_INITIAL_BALANCE = 10000.0
DEFAULT_MAX_RUNTIME_HOURS = 12.0
MIN_BARS = 500


def load_bars(csv_path: Path, tf: str) -> list[Bar]:
    """Load OHLCV bars from the historical CSV (same format as run_tts_walkforward)."""
    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["Date"])
    df = df.sort_values("time").reset_index(drop=True)
    return [
        Bar(
            time=row["time"].to_pydatetime(),
            open=row["Open"],
            high=row["High"],
            low=row["Low"],
            close=row["Close"],
            volume=row.get("Volume", 0),
        )
        for _, row in df.iterrows()
    ]


def _sizer_for(candidate: Candidate, account_size: float) -> ConfidencePositionSizer:
    from backtest.candidate_generator import SIZING_PROFILES

    tiers_json = json.dumps(SIZING_PROFILES[candidate.sizing_profile]["tiers"])
    tiers = parse_tiers(tiers_json)
    return ConfidencePositionSizer(account_size=account_size, tiers=tiers)


def _window_records(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "window_index": m.window_index,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "max_drawdown": m.max_drawdown,
            "sharpe_ratio": m.sharpe_ratio,
            "trade_count": m.trade_count,
            "total_pnl": m.total_pnl,
            "passed_go_nogo": m.passed_go_nogo,
            "regime_combined": m.regime_combined,
        }
        for m in result.per_window
    ]


def _dd_breach(windows: list[dict[str, Any]]) -> bool:
    """Hard kill: any window breaching -8% max DD (drawdown is a gate, not a metric)."""
    return any(w["max_drawdown"] > MAX_DD_HARD_KILL for w in windows)


def run_candidate(
    candidate: Candidate,
    bars: list[Bar],
    pair: str,
    initial_balance: float,
) -> dict[str, Any]:
    """Run one candidate through walk-forward and classify its exit criterion."""
    started = time.monotonic()
    risk_sizer = _sizer_for(candidate, initial_balance)

    def factory() -> TTSStrategy:
        return TTSStrategy(
            symbol=pair,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
            min_quality_score=DEFAULT_MIN_QUALITY,
        )

    result = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        n_windows=N_WINDOWS,
        train_ratio=0.65,
        val_ratio=0.15,
        spread_pips=DEFAULT_SPREAD,
        commission_per_lot=DEFAULT_COMMISSION,
        min_confidence=DEFAULT_MIN_CONFIDENCE,
        risk_sizer=risk_sizer,
    )

    windows = _window_records(result)
    windows_passed = sum(1 for w in windows if w["passed_go_nogo"])
    elapsed_hours = (time.monotonic() - started) / 3600.0

    agg = result.aggregated
    pip_value = compute_pip_value(pair, "USD", 1.0)

    if _dd_breach(windows):
        exit_criterion = "dd_hard_kill"
    elif elapsed_hours > DEFAULT_MAX_RUNTIME_HOURS:
        exit_criterion = "runtime_timeout"
    elif windows_passed == N_WINDOWS:
        exit_criterion = "all_windows_passed"
    else:
        exit_criterion = "partial_windows_passed"

    return {
        **candidate.to_dict(),
        "windows": windows,
        "windows_passed": windows_passed,
        "total_windows": len(windows),
        "aggregated": (
            {
                "mean_win_rate": agg.mean_win_rate,
                "mean_profit_factor": agg.mean_profit_factor,
                "mean_max_drawdown": agg.mean_max_drawdown,
                "mean_sharpe_ratio": agg.mean_sharpe_ratio,
                "mean_trade_count": agg.mean_trade_count,
                "mean_total_pnl": agg.mean_total_pnl,
            }
            if agg is not None
            else None
        ),
        "elapsed_hours": round(elapsed_hours, 4),
        "exit_criterion": exit_criterion,
        "pip_value": pip_value,
        "pip_value_validated": bool(
            math.isfinite(pip_value) and pip_value > 0.0
        ),
        "noise_guard": {
            "thirty_trade_live_minimum": (
                "no metric may be quoted until 30 live/paper trades close under this candidate"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parameter tuning cycle orchestrator — 24 candidates "
            "(3 regimes × 2 sizing × 4 signal-weight ratios) × 4 walk-forward windows"
        )
    )
    parser.add_argument("--cycle-id", type=str, default=None, help="Cycle identifier (safe dir name)")
    parser.add_argument("--regime", choices=list(REGIMES), default=None, help="Focus regime lane")
    parser.add_argument("--dry-run", action="store_true", help="Print the 24-candidate manifest structure and exit")
    parser.add_argument("--pair", default=DEFAULT_PAIR)
    parser.add_argument("--timeframe", "--tf", default=DEFAULT_TIMEFRAME)
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Historical CSV path template ({tf} substituted)")
    parser.add_argument("--initial-balance", type=float, default=DEFAULT_INITIAL_BALANCE)
    parser.add_argument(
        "--output-root", default="data/trading/tuning_runs", help="Base dir for cycle outputs"
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        print(json.dumps({"candidate_count": 24, "candidates": candidates_manifest(args.regime)}, indent=2))
        return 0

    if not args.cycle_id:
        parser.error("--cycle-id is required unless --dry-run")
    validate_cycle_id(args.cycle_id)

    out_dir = PROJECT_ROOT / args.output_root / args.cycle_id
    if out_dir.exists():
        print(f"ERROR: cycle output dir already exists: {out_dir} (refusing to overwrite)", file=sys.stderr)
        return 2
    (out_dir / "candidates").mkdir(parents=True, exist_ok=True)

    csv_path = PROJECT_ROOT / args.csv.format(tf=args.timeframe)
    if not csv_path.exists():
        print(f"ERROR: historical CSV not found: {csv_path}", file=sys.stderr)
        return 2
    bars = load_bars(csv_path, args.timeframe)
    if len(bars) < MIN_BARS:
        print(f"ERROR: only {len(bars)} bars (<{MIN_BARS})", file=sys.stderr)
        return 2

    candidates = candidates_for_regime(args.regime) if args.regime is not None else generate_candidates()

    started_iso = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for cand in candidates:
        print(f"[{args.cycle_id}] running {cand.candidate_id} ...", flush=True)
        try:
            record = run_candidate(cand, bars, args.pair, args.initial_balance)
        except Exception as exc:  # noqa: BLE001 — record and continue the cycle
            record = {**cand.to_dict(), "error": f"{type(exc).__name__}: {exc}", "exit_criterion": "error"}
        records.append(record)
        with open(out_dir / "candidates" / f"{cand.candidate_id}.json", "w") as f:
            json.dump(record, f, indent=2, default=str)

    completed = [r for r in records if "error" not in r]
    passing = [r for r in completed if r["exit_criterion"] == "all_windows_passed"]
    no_passing_candidate = not passing

    summary = {
        "cycle_id": args.cycle_id,
        "regime_focus": args.regime,
        "pair": args.pair,
        "timeframe": args.timeframe,
        "started_at": started_iso,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "candidates_total": len(records),
        "candidates_completed": len(completed),
        "candidates_passing": len(passing),
        "no_passing_candidate": no_passing_candidate,
        "pip_value_validated": all(r.get("pip_value_validated") for r in completed) if completed else False,
        "gates": {
            "max_dd_hard_kill": MAX_DD_HARD_KILL,
            "n_windows": N_WINDOWS,
            "max_runtime_hours_per_candidate": DEFAULT_MAX_RUNTIME_HOURS,
        },
    }
    manifest = {
        "cycle_id": args.cycle_id,
        "exit_criteria": {r["candidate_id"]: r.get("exit_criterion", "error") for r in records},
        "windows_passed": {r["candidate_id"]: r.get("windows_passed", 0) for r in records},
    }

    with open(out_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    with open(out_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(json.dumps(summary, indent=2))
    if no_passing_candidate:
        print("CYCLE RESULT: no passing candidate", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
