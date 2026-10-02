#!/usr/bin/env python3
"""
SRMR+ Focused Pipeline — per-pair search spaces + WalkForwardResults dataclass access.

Learned from full pipeline: M5/M15 produced too few trades with default thresholds.
H1 is where the original XAUUSD validation came from. Start there.

Optimizations:
- 30 trials (not 50) — sufficient for TPE sampler
- H1 first, then extend to others
- Lower trade-count threshold (3 per window, not 5)
- Per-pair Optuna search spaces (XAUUSD vs FX pairs)
- WalkForwardResults dataclass access via .aggregated
- Suppress WARNING noise to stderr
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

# Suppress WARNINGs from walk_forward_runner
import logging

logging.getLogger("backtest.walk_forward_runner").setLevel(logging.ERROR)

from backtest import CsvDataLoader
from backtest.engine import get_spread_for_pair
from backtest.walk_forward_runner import run_strategy_walk_forward
from common.resource_limits import cpu_limited, memory_capped
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

try:
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    print("ERROR: optuna not installed")
    sys.exit(1)

DATA_DIR = Path("data/forex/historical")
REPORT_DIR = Path("reports/srmr-plus-pipeline-2026-07-08")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# Per-pair search space configuration.
# XAUUSD validated params are centered around: session_range=200, entry_near=150, hard_cap=300, pip_value=0.01
# FX pairs use smaller pip values; USDJPY also uses 0.01 pip but smaller ranges.
PAIR_SEARCH_SPACES = {
    "XAUUSD": {
        "session_range_min_pips": (50.0, 400.0),
        "entry_near_extreme_pips": (30.0, 300.0),
        "hard_cap_sl_pips": (100.0, 500.0),
        "pip_value": 0.01,
    },
    "USDJPY": {
        # USDJPY has pip_value=0.01 like XAUUSD but smaller price ranges
        "session_range_min_pips": (15.0, 80.0),
        "entry_near_extreme_pips": (8.0, 50.0),
        "hard_cap_sl_pips": (20.0, 100.0),
        "pip_value": 0.01,
    },
    "GBPUSD": {
        "session_range_min_pips": (5.0, 50.0),
        "entry_near_extreme_pips": (3.0, 30.0),
        "hard_cap_sl_pips": (10.0, 50.0),
        "pip_value": None,
    },
    "EURUSD": {
        "session_range_min_pips": (5.0, 50.0),
        "entry_near_extreme_pips": (3.0, 30.0),
        "hard_cap_sl_pips": (10.0, 50.0),
        "pip_value": None,
    },
    "AUDUSD": {
        "session_range_min_pips": (5.0, 50.0),
        "entry_near_extreme_pips": (3.0, 30.0),
        "hard_cap_sl_pips": (10.0, 50.0),
        "pip_value": None,
    },
    "USDCHF": {
        "session_range_min_pips": (5.0, 50.0),
        "entry_near_extreme_pips": (3.0, 30.0),
        "hard_cap_sl_pips": (10.0, 50.0),
        "pip_value": None,
    },
    "USDCAD": {
        "session_range_min_pips": (5.0, 50.0),
        "entry_near_extreme_pips": (3.0, 30.0),
        "hard_cap_sl_pips": (10.0, 50.0),
        "pip_value": None,
    },
}

DEFAULT_SEARCH_SPACE = {
    "session_range_min_pips": (5.0, 50.0),
    "entry_near_extreme_pips": (3.0, 30.0),
    "hard_cap_sl_pips": (10.0, 50.0),
    "pip_value": None,
}


def get_search_space(pair: str) -> dict:
    return PAIR_SEARCH_SPACES.get(pair, DEFAULT_SEARCH_SPACE)


def find_data_file(pair: str, timeframe: str) -> Path | None:
    for suffix in ["", "_fresh", "_2026"]:
        path = DATA_DIR / f"{pair}_{timeframe}{suffix}.csv"
        if path.exists():
            return path
    return None


def suggest_srmr_params(trial, pair: str) -> dict:
    """Per-pair Optuna search space.

    XAUUSD ranges are centered on validated params (200/150/300).
    FX pairs use smaller pip-based ranges. pip_value is FIXED per pair.
    """
    space = get_search_space(pair)
    sr_lo, sr_hi = space["session_range_min_pips"]
    en_lo, en_hi = space["entry_near_extreme_pips"]
    hc_lo, hc_hi = space["hard_cap_sl_pips"]

    params: dict = {
        "atr_period": trial.suggest_int("atr_period", 7, 28),
        "rsi_period": trial.suggest_int("rsi_period", 7, 21),
        "rsi_long_level": trial.suggest_float("rsi_long_level", 20.0, 45.0),
        "rsi_short_level": trial.suggest_float("rsi_short_level", 55.0, 80.0),
        "adx_period": trial.suggest_int("adx_period", 7, 28),
        "adx_max_threshold": trial.suggest_float("adx_max_threshold", 15.0, 40.0),
        "session_range_min_pips": trial.suggest_float("session_range_min_pips", sr_lo, sr_hi),
        "entry_near_extreme_pips": trial.suggest_float("entry_near_extreme_pips", en_lo, en_hi),
        "hard_cap_sl_pips": trial.suggest_float("hard_cap_sl_pips", hc_lo, hc_hi),
        "tp1_rr": trial.suggest_float("tp1_rr", 0.5, 3.0),
        "tp2_rr": trial.suggest_float("tp2_rr", 0.5, 3.0),
        "ema_trend_period": trial.suggest_int("ema_trend_period", 20, 100),
        "use_same_day_range": trial.suggest_categorical("use_same_day_range", [True, False]),
        # pip_value is fixed per pair (validated), not part of search space
        "pip_value": space["pip_value"],
        "dxy_overlay": False,
    }
    return params


def _wf_aggregated_metric(wf, key: str, default: float = 0.0) -> float:
    """Safely extract a metric from a WalkForwardResults dataclass.

    WalkForwardResults.aggregated is the AggregatedMetrics dataclass.
    Fall back to per-window mean if aggregated is missing.
    """
    agg = getattr(wf, "aggregated", None)
    if agg is not None:
        val = getattr(agg, key, None)
        if val is not None:
            return float(val)
    # Per-window fallback: aggregate across per_window if available
    per_window = getattr(wf, "per_window", None)
    if per_window:
        # Mapping from aggregated key to WindowMetrics attr
        mapping = {
            "mean_win_rate": "win_rate",
            "mean_profit_factor": "profit_factor",
            "mean_max_drawdown": "max_drawdown",
            "mean_sharpe_ratio": "sharpe_ratio",
            "mean_trade_count": "trade_count",
            "mean_total_pnl": "total_pnl",
            "windows_passed": None,  # special
            "total_windows": None,  # special
        }
        win_key = mapping.get(key)
        if win_key == "windows_passed" or key == "windows_passed":
            return float(sum(1 for m in per_window if getattr(m, "passed_go_nogo", False)))
        if win_key == "total_windows" or key == "total_windows":
            return float(len(per_window))
        if win_key:
            vals = [getattr(m, win_key, 0) for m in per_window]
            if vals:
                return float(sum(vals) / len(vals))
    return default


def run_optuna_for_timeframe(
    pair: str,
    timeframe: str,
    data_path: Path,
    n_trials: int = 30,
    n_windows: int = 5,
    min_trades_per_window: int = 3,
) -> dict:
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

        spread = get_spread_for_pair(pair)
        space = get_search_space(pair)

        def objective(trial):
            params = suggest_srmr_params(trial, pair=pair)
            try:

                def factory():
                    return SRMRPlusStrategy(config=SRMRPlusConfig(**params))

                wf = run_strategy_walk_forward(
                    bars=bars,
                    strategy_factory=factory,
                    pair=pair,
                    n_windows=n_windows,
                    spread_pips=spread,
                )

                # Extract from WalkForwardResults.aggregated dataclass
                if wf is None or getattr(wf, "aggregated", None) is None:
                    return -1000

                agg = wf.aggregated
                trades = float(getattr(agg, "mean_trade_count", 0.0))
                if trades < min_trades_per_window:
                    return -1000
                pnl = float(getattr(agg, "mean_total_pnl", 0.0))
                return pnl
            except Exception:
                return -1000

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        best_params = suggest_srmr_params(study.best_trial, pair=pair)
        # ensure pip_value persists from space even if Optuna pruned it
        best_params["pip_value"] = space["pip_value"]
        best_params["dxy_overlay"] = False

        result["optuna_best_value"] = study.best_value
        result["optuna_n_trials"] = n_trials
        result["best_params"] = best_params

        # Final WF validation with best params
        def factory():
            return SRMRPlusStrategy(config=SRMRPlusConfig(**best_params))

        wf = run_strategy_walk_forward(
            bars=bars,
            strategy_factory=factory,
            pair=pair,
            n_windows=n_windows,
            spread_pips=spread,
        )

        result["status"] = "complete"
        # Aggregate metrics via dataclass access (wf.aggregated.*)
        result["windows_passed"] = int(_wf_aggregated_metric(wf, "windows_passed", 0))
        result["windows_total"] = int(_wf_aggregated_metric(wf, "total_windows", n_windows))
        result["go_nogo"] = bool(getattr(wf, "go_nogo", False))
        result["mean_profit_factor"] = _wf_aggregated_metric(wf, "mean_profit_factor", 0)
        result["mean_win_rate"] = _wf_aggregated_metric(wf, "mean_win_rate", 0)
        result["mean_sharpe"] = _wf_aggregated_metric(wf, "mean_sharpe_ratio", 0)
        result["mean_max_drawdown"] = _wf_aggregated_metric(wf, "mean_max_drawdown", 0)
        result["mean_trade_count"] = _wf_aggregated_metric(wf, "mean_trade_count", 0)
        result["mean_total_pnl"] = _wf_aggregated_metric(wf, "mean_total_pnl", 0)

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()[-300:]

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=str, default="XAUUSD")
    parser.add_argument("--timeframes", type=str, default="H1,M15,D1,H4")
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--windows", type=int, default=5)
    parser.add_argument("--min-trades", type=int, default=3)
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",")]
    pair = args.pair

    evals = []
    for tf in timeframes:
        path = find_data_file(pair, tf)
        if path:
            evals.append({"timeframe": tf, "data_path": path})

    print(f"SRMR+ Focused — {pair}")
    print(f"  Timeframes: {[e['timeframe'] for e in evals]}")
    print(f"  Trials: {args.trials} per tf, WF windows: {args.windows}")
    print(f"  Min trades/window threshold: {args.min_trades}")
    print("  CPU cap: 20%, Memory cap: 2048MB")
    print()

    all_results = []
    with cpu_limited(percent=20):
        with memory_capped(mb=2048):
            for i, ec in enumerate(evals, 1):
                tf = ec["timeframe"]
                print(f"[{i}/{len(evals)}] {pair} {tf}...", end=" ", flush=True)
                start = time.time()

                result = run_optuna_for_timeframe(
                    pair=pair,
                    timeframe=tf,
                    data_path=ec["data_path"],
                    n_trials=args.trials,
                    n_windows=args.windows,
                    min_trades_per_window=args.min_trades,
                )
                elapsed = time.time() - start

                if result["status"] == "complete":
                    pf = result.get("mean_profit_factor", 0)
                    wr = result.get("mean_win_rate", 0)
                    passed = result.get("windows_passed", 0)
                    total_w = result.get("windows_total", args.windows)
                    pnl = result.get("mean_total_pnl", 0)
                    trades = result.get("mean_trade_count", 0)
                    print(
                        f"DONE ({elapsed:.0f}s) — {passed}/{total_w} PASS | "
                        f"PF={pf:.2f} | WR={wr:.1%} | PnL={pnl:.0f} | trades={trades:.0f}"
                    )
                elif result["status"] == "skipped":
                    print(f"SKIP — {result.get('reason', '')}")
                else:
                    print(f"ERROR — {result.get('error', '')[:60]}")

                all_results.append(result)
                with open(REPORT_DIR / f"{pair}_focused_results.jsonl", "a") as f:
                    f.write(json.dumps(result, default=str) + "\n")

    viable = [r for r in all_results if r.get("mean_profit_factor", 0) > 1.0 and r.get("windows_passed", 0) >= 3]
    print(f"\n{'=' * 60}")
    print(f"Viable: {len(viable)} of {len(all_results)} timeframes")
    for v in viable:
        print(
            f"  ✅ {v['timeframe']} — PF={v['mean_profit_factor']:.2f} "
            f"WR={v['mean_win_rate']:.1%} {v['windows_passed']}/5 PASS "
            f"PnL={v['mean_total_pnl']:.0f}"
        )


if __name__ == "__main__":
    main()
