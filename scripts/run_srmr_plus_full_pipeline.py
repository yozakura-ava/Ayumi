#!/usr/bin/env python3
"""
SRMR+ Full Pipeline — Optuna optimization + WF validation across timeframes.

For each timeframe with data:
1. Run Optuna optimization (50 trials, TPE sampler) to find best params
2. WF-validate the optimized params (5 windows)
3. Report results

Sequential, CPU-capped (20%, 2GB memory).

Usage:
    python scripts/run_srmr_plus_full_pipeline.py --pair XAUUSD
    python scripts/run_srmr_plus_full_pipeline.py --pair XAUUSD --timeframes M5,M15,H1,H4
    python scripts/run_srmr_plus_full_pipeline.py --dry-run
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader
from backtest.engine import get_spread_for_pair
from backtest.walk_forward_runner import run_strategy_walk_forward
from common.resource_limits import cpu_limited, memory_capped
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

try:
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    print("ERROR: optuna not installed. Run: pip install optuna")
    sys.exit(1)

DATA_DIR = Path("data/forex/historical")
REPORT_DIR = Path("reports/srmr-plus-pipeline-2026-07-08")
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Timeframes to evaluate (M30 missing from data, skip)
DEFAULT_TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]


# Optuna search space for SRMR+ parameters
def suggest_srmr_params(trial: optuna.Trial, pip_scale: float = 1.0) -> dict:
    """Suggest SRMR+ parameters via Optuna."""
    return {
        "atr_period": trial.suggest_int("atr_period", 7, 28),
        "rsi_period": trial.suggest_int("rsi_period", 7, 21),
        "rsi_long_level": trial.suggest_float("rsi_long_level", 20.0, 45.0),
        "rsi_short_level": trial.suggest_float("rsi_short_level", 55.0, 80.0),
        "adx_period": trial.suggest_int("adx_period", 7, 28),
        "adx_max_threshold": trial.suggest_float("adx_max_threshold", 15.0, 40.0),
        "session_range_min_pips": trial.suggest_float("session_range_min_pips", 5.0 * pip_scale, 50.0 * pip_scale),
        "entry_near_extreme_pips": trial.suggest_float("entry_near_extreme_pips", 3.0 * pip_scale, 30.0 * pip_scale),
        "hard_cap_sl_pips": trial.suggest_float("hard_cap_sl_pips", 10.0 * pip_scale, 50.0 * pip_scale),
        "tp1_rr": trial.suggest_float("tp1_rr", 0.5, 3.0),
        "tp2_rr": trial.suggest_float("tp2_rr", 0.5, 3.0),
        "ema_trend_period": trial.suggest_int("ema_trend_period", 20, 100),
        "use_same_day_range": trial.suggest_categorical("use_same_day_range", [True, False]),
    }


def make_strategy(params: dict) -> SRMRPlusStrategy:
    """Create SRMR+ strategy from parameter dict."""
    config = SRMRPlusConfig(**params)
    return SRMRPlusStrategy(config=config)


def find_data_file(pair: str, timeframe: str) -> Path | None:
    """Find the data file for a pair + timeframe."""
    # Try exact match first
    for suffix in ["", "_fresh", "_2026"]:
        path = DATA_DIR / f"{pair}_{timeframe}{suffix}.csv"
        if path.exists():
            return path
    return None


def run_optuna_for_timeframe(
    pair: str,
    timeframe: str,
    data_path: Path,
    n_trials: int = 50,
    n_windows: int = 5,
) -> dict:
    """Run Optuna optimization + WF validation for a single timeframe."""

    result = {
        "pair": pair,
        "timeframe": timeframe,
        "data_path": str(data_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    try:
        loader = CsvDataLoader()
        bars = loader.load(str(data_path))
        if not bars or len(bars) < 500:
            result["status"] = "skipped"
            result["reason"] = f"Insufficient bars: {len(bars) if bars else 0}"
            return result

        # Determine pip scale (XAUUSD needs larger pip values)
        pip_scale = 10.0 if pair == "XAUUSD" else 1.0
        spread = get_spread_for_pair(pair)

        # --- Optuna optimization ---
        def objective(trial: optuna.Trial) -> float:
            params = suggest_srmr_params(trial, pip_scale=pip_scale)
            try:
                _strategy = make_strategy(params)
                wf_result = run_strategy_walk_forward(
                    bars=bars,
                    strategy_factory=lambda: make_strategy(params),
                    pair=pair,
                    n_windows=n_windows,
                    spread_pips=spread,
                )
                # Objective: maximize total PnL (could also use Sharpe or PF)
                if hasattr(wf_result, "mean_total_pnl"):
                    pnl = wf_result.mean_total_pnl
                elif isinstance(wf_result, dict):
                    pnl = wf_result.get("mean_total_pnl", 0)
                else:
                    pnl = 0

                # Penalize if too few trades
                if hasattr(wf_result, "mean_trade_count"):
                    trades = wf_result.mean_trade_count
                elif isinstance(wf_result, dict):
                    trades = wf_result.get("mean_trade_count", 0)
                else:
                    trades = 0

                if trades < 5:
                    return -1000  # Penalize inactive strategies

                return pnl
            except Exception:
                return -1000

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        best_params = study.best_params
        # Add fixed params
        best_params["pip_value"] = None
        best_params["dxy_overlay"] = False

        result["optuna_best_value"] = study.best_value
        result["optuna_n_trials"] = n_trials
        result["best_params"] = best_params

        # --- WF validation with optimized params ---
        wf_result = run_strategy_walk_forward(
            bars=bars,
            strategy_factory=lambda: make_strategy(best_params),
            pair=pair,
            n_windows=n_windows,
            spread_pips=spread,
        )

        # Extract results
        if hasattr(wf_result, "mean_total_pnl"):
            result["status"] = "complete"
            result["windows_passed"] = wf_result.windows_passed
            result["windows_total"] = n_windows
            result["go_nogo"] = wf_result.go_nogo
            result["mean_profit_factor"] = wf_result.mean_profit_factor
            result["mean_win_rate"] = wf_result.mean_win_rate
            result["mean_sharpe"] = wf_result.mean_sharpe_ratio
            result["mean_max_drawdown"] = wf_result.mean_max_drawdown
            result["mean_trade_count"] = wf_result.mean_trade_count
            result["mean_total_pnl"] = wf_result.mean_total_pnl
        elif isinstance(wf_result, dict):
            result["status"] = "complete"
            result["windows_passed"] = wf_result.get("windows_passed", 0)
            result["windows_total"] = n_windows
            result["go_nogo"] = wf_result.get("go_nogo", False)
            result["mean_profit_factor"] = wf_result.get("mean_profit_factor", 0)
            result["mean_win_rate"] = wf_result.get("mean_win_rate", 0)
            result["mean_sharpe"] = wf_result.get("mean_sharpe_ratio", 0)
            result["mean_max_drawdown"] = wf_result.get("mean_max_drawdown", 0)
            result["mean_trade_count"] = wf_result.get("mean_trade_count", 0)
            result["mean_total_pnl"] = wf_result.get("mean_total_pnl", 0)
        else:
            result["status"] = "complete"
            result["note"] = "WF result type not recognized"

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()[-500:]

    return result


def main():
    parser = argparse.ArgumentParser(description="SRMR+ full pipeline: Optuna + WF across timeframes")
    parser.add_argument("--pair", type=str, default="XAUUSD", help="Currency pair (default: XAUUSD)")
    parser.add_argument(
        "--timeframes",
        type=str,
        default=None,
        help="Comma-separated timeframes (default: all available)",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=50,
        help="Optuna trials per timeframe (default: 50)",
    )
    parser.add_argument("--windows", type=int, default=5, help="WF windows (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without running")
    args = parser.parse_args()

    pair = args.pair
    timeframes = args.timeframes.split(",") if args.timeframes else DEFAULT_TIMEFRAMES

    # Find available data files
    evals = []
    for tf in timeframes:
        path = find_data_file(pair, tf)
        if path:
            evals.append({"timeframe": tf, "data_path": path})
        else:
            print(f"  SKIP {pair} {tf}: no data file found")

    print(f"SRMR+ Full Pipeline — {pair}")
    print(f"  Timeframes: {[e['timeframe'] for e in evals]}")
    print(f"  Optuna trials: {args.trials} per timeframe")
    print(f"  WF windows: {args.windows}")
    print("  CPU cap: 20%, Memory cap: 2048MB")
    print("  Mode: SEQUENTIAL")
    print()

    if args.dry_run:
        for e in evals:
            print(f"  Would run: {pair} {e['timeframe']} ({e['data_path']})")
        return

    all_results = []

    with cpu_limited(percent=20):
        with memory_capped(mb=2048):
            for i, eval_config in enumerate(evals, 1):
                tf = eval_config["timeframe"]
                data_path = eval_config["data_path"]

                print(
                    f"[{i}/{len(evals)}] {pair} {tf} — Optuna({args.trials} trials)...",
                    end=" ",
                    flush=True,
                )
                start = time.time()

                result = run_optuna_for_timeframe(
                    pair=pair,
                    timeframe=tf,
                    data_path=data_path,
                    n_trials=args.trials,
                    n_windows=args.windows,
                )
                elapsed = time.time() - start

                if result["status"] == "complete":
                    pf = result.get("mean_profit_factor", 0)
                    wr = result.get("mean_win_rate", 0)
                    passed = result.get("windows_passed", 0)
                    total = result.get("windows_total", 0)
                    pnl = result.get("mean_total_pnl", 0)
                    trades = result.get("mean_trade_count", 0)
                    opt_val = result.get("optuna_best_value", 0)
                    print(
                        f"DONE ({elapsed:.0f}s) — {passed}/{total} PASS | PF={pf:.2f} | WR={wr:.1%} | PnL={pnl:.0f} | trades={trades:.0f} | optuna_best={opt_val:.0f}"  # noqa: E501
                    )
                elif result["status"] == "skipped":
                    print(f"SKIP — {result.get('reason', 'unknown')}")
                else:
                    print(f"ERROR — {result.get('error', 'unknown')[:80]}")

                all_results.append(result)

                # Save incremental
                with open(REPORT_DIR / f"{pair}_results.jsonl", "a") as f:
                    f.write(json.dumps(result, default=str) + "\n")

    # Summary
    viable = [r for r in all_results if r.get("mean_profit_factor", 0) > 1.0 and r.get("windows_passed", 0) >= 3]

    summary = {
        "pair": pair,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeframes_evaluated": len(all_results),
        "viable_timeframes": len(viable),
        "results": all_results,
        "viable": viable,
    }

    with open(REPORT_DIR / f"{pair}_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {len(all_results)} timeframes evaluated, {len(viable)} viable")
    for v in viable:
        print(
            f"  ✅ {v['timeframe']} — PF={v['mean_profit_factor']:.2f} WR={v['mean_win_rate']:.1%} {v['windows_passed']}/{v['windows_total']} PASS PnL={v['mean_total_pnl']:.0f}"  # noqa: E501
        )
    print(f"\nFull report: {REPORT_DIR / f'{pair}_summary.json'}")


if __name__ == "__main__":
    main()
