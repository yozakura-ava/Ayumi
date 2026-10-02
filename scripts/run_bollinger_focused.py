#!/usr/bin/env python3
"""
Bollinger + RSI Mean Reversion Focused Pipeline.

Adapted from run_srmr_plus_focused.py after the WalkForwardResults dataclass fix:
- Per-pair Optuna search spaces (XAUUSD uses pip_value=0.01)
- Sequential timeframe execution under CPU/memory caps
- Metrics read from WalkForwardResults.aggregated.* with guarded fallback
- Results appended to reports/bollinger-pipeline-2026-07-08/{pair}_focused_results.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

# Suppress WARNINGs from walk_forward_runner.
logging.getLogger("backtest.walk_forward_runner").setLevel(logging.ERROR)

from backtest import CsvDataLoader
from backtest.engine import get_spread_for_pair
from backtest.walk_forward_runner import run_strategy_walk_forward
from common.resource_limits import cpu_limited, memory_capped
from strategies.bb_rsi_reversion import BBRSIConfig, BBRSIMeanReversion

try:
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError:
    print("ERROR: optuna not installed")
    sys.exit(1)

DATA_DIR = Path("data/forex/historical")
REPORT_DIR = Path("reports/bollinger-pipeline-2026-07-08")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# Per-pair search space configuration.
# XAUUSD uses pip_value=0.01 so pip-derived diagnostics match metal pricing.
# FX majors leave pip_value=None so the strategy's price-based default is used.
PAIR_SEARCH_SPACES: dict[str, dict[str, Any]] = {
    "XAUUSD": {
        "bb_period": (12, 50),
        "bb_std_dev": (1.4, 3.4),
        "rsi_period": (7, 28),
        "rsi_long_level": (20.0, 45.0),
        "rsi_short_level": (55.0, 80.0),
        "atr_period": (7, 28),
        "atr_sl_multiplier": (0.6, 3.5),
        "tp1_rr": (0.5, 3.0),
        "tp2_rr": (0.75, 4.0),
        "ema_trend_period": (20, 150),
        "adx_period": (7, 28),
        "adx_max_threshold": (15.0, 45.0),
        "atr_sma_period": (10, 50),
        "require_low_volatility": [True, False],
        "pip_value": 0.01,
    },
    "USDJPY": {
        "bb_period": (10, 40),
        "bb_std_dev": (1.3, 3.0),
        "rsi_period": (7, 24),
        "rsi_long_level": (20.0, 45.0),
        "rsi_short_level": (55.0, 80.0),
        "atr_period": (7, 24),
        "atr_sl_multiplier": (0.5, 3.0),
        "tp1_rr": (0.5, 2.5),
        "tp2_rr": (0.75, 3.5),
        "ema_trend_period": (20, 120),
        "adx_period": (7, 24),
        "adx_max_threshold": (15.0, 40.0),
        "atr_sma_period": (10, 40),
        "require_low_volatility": [True, False],
        "pip_value": 0.01,
    },
    "GBPUSD": {
        "bb_period": (10, 40),
        "bb_std_dev": (1.3, 3.0),
        "rsi_period": (7, 24),
        "rsi_long_level": (20.0, 45.0),
        "rsi_short_level": (55.0, 80.0),
        "atr_period": (7, 24),
        "atr_sl_multiplier": (0.5, 3.0),
        "tp1_rr": (0.5, 2.5),
        "tp2_rr": (0.75, 3.5),
        "ema_trend_period": (20, 120),
        "adx_period": (7, 24),
        "adx_max_threshold": (15.0, 40.0),
        "atr_sma_period": (10, 40),
        "require_low_volatility": [True, False],
        "pip_value": None,
    },
    "EURUSD": {
        "bb_period": (10, 40),
        "bb_std_dev": (1.3, 3.0),
        "rsi_period": (7, 24),
        "rsi_long_level": (20.0, 45.0),
        "rsi_short_level": (55.0, 80.0),
        "atr_period": (7, 24),
        "atr_sl_multiplier": (0.5, 3.0),
        "tp1_rr": (0.5, 2.5),
        "tp2_rr": (0.75, 3.5),
        "ema_trend_period": (20, 120),
        "adx_period": (7, 24),
        "adx_max_threshold": (15.0, 40.0),
        "atr_sma_period": (10, 40),
        "require_low_volatility": [True, False],
        "pip_value": None,
    },
}

DEFAULT_SEARCH_SPACE = PAIR_SEARCH_SPACES["EURUSD"]


def get_search_space(pair: str) -> dict[str, Any]:
    return PAIR_SEARCH_SPACES.get(pair.upper(), DEFAULT_SEARCH_SPACE)


def find_data_file(pair: str, timeframe: str) -> Path | None:
    for suffix in ["", "_fresh", "_2026"]:
        path = DATA_DIR / f"{pair}_{timeframe}{suffix}.csv"
        if path.exists():
            return path
    return None


def suggest_bollinger_params(trial: Any, pair: str) -> dict[str, Any]:
    """Per-pair Optuna search space for BBRSIConfig."""
    space = get_search_space(pair)
    bb_period_lo, bb_period_hi = space["bb_period"]
    bb_std_lo, bb_std_hi = space["bb_std_dev"]
    rsi_period_lo, rsi_period_hi = space["rsi_period"]
    rsi_long_lo, rsi_long_hi = space["rsi_long_level"]
    rsi_short_lo, rsi_short_hi = space["rsi_short_level"]
    atr_period_lo, atr_period_hi = space["atr_period"]
    atr_sl_lo, atr_sl_hi = space["atr_sl_multiplier"]
    tp1_lo, tp1_hi = space["tp1_rr"]
    tp2_lo, tp2_hi = space["tp2_rr"]
    ema_lo, ema_hi = space["ema_trend_period"]
    adx_period_lo, adx_period_hi = space["adx_period"]
    adx_lo, adx_hi = space["adx_max_threshold"]
    atr_sma_lo, atr_sma_hi = space["atr_sma_period"]

    tp1_rr = trial.suggest_float("tp1_rr", tp1_lo, tp1_hi)
    tp2_rr = max(trial.suggest_float("tp2_rr", tp2_lo, tp2_hi), tp1_rr)

    return {
        "bb_period": trial.suggest_int("bb_period", bb_period_lo, bb_period_hi),
        "bb_std_dev": trial.suggest_float("bb_std_dev", bb_std_lo, bb_std_hi),
        "rsi_period": trial.suggest_int("rsi_period", rsi_period_lo, rsi_period_hi),
        "rsi_long_level": trial.suggest_float("rsi_long_level", rsi_long_lo, rsi_long_hi),
        "rsi_short_level": trial.suggest_float("rsi_short_level", rsi_short_lo, rsi_short_hi),
        "atr_period": trial.suggest_int("atr_period", atr_period_lo, atr_period_hi),
        "atr_sl_multiplier": trial.suggest_float("atr_sl_multiplier", atr_sl_lo, atr_sl_hi),
        "tp1_rr": tp1_rr,
        "tp2_rr": tp2_rr,
        "ema_trend_period": trial.suggest_int("ema_trend_period", ema_lo, ema_hi),
        "adx_period": trial.suggest_int("adx_period", adx_period_lo, adx_period_hi),
        "adx_max_threshold": trial.suggest_float("adx_max_threshold", adx_lo, adx_hi),
        "pip_value": space["pip_value"],
        "require_low_volatility": trial.suggest_categorical("require_low_volatility", space["require_low_volatility"]),
        "atr_sma_period": trial.suggest_int("atr_sma_period", atr_sma_lo, atr_sma_hi),
    }


def _wf_aggregated_metric(wf: Any, key: str, default: float = 0.0) -> float:
    """Safely extract a metric from a WalkForwardResults dataclass.

    WalkForwardResults.aggregated is the AggregatedMetrics dataclass.
    Fall back to per-window mean if aggregated is missing.
    """
    agg = getattr(wf, "aggregated", None)
    if agg is not None:
        val = getattr(agg, key, None)
        if val is not None:
            return float(val)

    per_window = getattr(wf, "per_window", None)
    if per_window:
        mapping = {
            "mean_win_rate": "win_rate",
            "mean_profit_factor": "profit_factor",
            "mean_max_drawdown": "max_drawdown",
            "mean_sharpe_ratio": "sharpe_ratio",
            "mean_trade_count": "trade_count",
            "mean_total_pnl": "total_pnl",
            "windows_passed": None,
            "total_windows": None,
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
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "strategy": "bb_rsi_reversion",
        "strategy_class": "BBRSIMeanReversion",
        "config_class": "BBRSIConfig",
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

        def objective(trial: Any) -> float:
            params = suggest_bollinger_params(trial, pair=pair)
            try:

                def factory() -> BBRSIMeanReversion:
                    return BBRSIMeanReversion(config=BBRSIConfig(**params))

                wf = run_strategy_walk_forward(
                    bars=bars,
                    strategy_factory=factory,
                    pair=pair,
                    n_windows=n_windows,
                    spread_pips=spread,
                )

                if wf is None or getattr(wf, "aggregated", None) is None:
                    return -1000.0

                agg = wf.aggregated
                trades = float(getattr(agg, "mean_trade_count", 0.0))
                if trades < min_trades_per_window:
                    return -1000.0

                # Favor profitable, smooth, trade-producing candidates while still retaining weak final results.
                pnl = float(getattr(agg, "mean_total_pnl", 0.0))
                pf = float(getattr(agg, "mean_profit_factor", 0.0))
                sharpe = float(getattr(agg, "mean_sharpe_ratio", 0.0))
                drawdown = float(getattr(agg, "mean_max_drawdown", 0.0))
                return pnl + (pf * 50.0) + (sharpe * 25.0) - (abs(drawdown) * 0.1)
            except Exception:
                return -1000.0

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        best_params = suggest_bollinger_params(study.best_trial, pair=pair)
        best_params["pip_value"] = space["pip_value"]

        result["optuna_best_value"] = study.best_value
        result["optuna_n_trials"] = n_trials
        result["best_params"] = best_params

        def factory() -> BBRSIMeanReversion:
            return BBRSIMeanReversion(config=BBRSIConfig(**best_params))

        wf = run_strategy_walk_forward(
            bars=bars,
            strategy_factory=factory,
            pair=pair,
            n_windows=n_windows,
            spread_pips=spread,
        )

        result["status"] = "complete"
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=str, default="XAUUSD")
    parser.add_argument("--timeframes", type=str, default="H1,M15,D1,H4")
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--windows", type=int, default=5)
    parser.add_argument("--min-trades", type=int, default=3)
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    pair = args.pair.upper()

    evals = []
    for tf in timeframes:
        path = find_data_file(pair, tf)
        if path:
            evals.append({"timeframe": tf, "data_path": path})

    print(f"Bollinger BB+RSI Focused — {pair}")
    print(f"  Timeframes: {[e['timeframe'] for e in evals]}")
    print(f"  Trials: {args.trials} per tf, WF windows: {args.windows}")
    print(f"  Min trades/window threshold: {args.min_trades}")
    print("  CPU cap: 20%, Memory cap: 2048MB")
    print(f"  Search-space pip_value: {get_search_space(pair)['pip_value']}")
    print()

    all_results: list[dict[str, Any]] = []
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

    reportable = [r for r in all_results if r.get("mean_trade_count", 0) > 0]
    viable = [
        r
        for r in all_results
        if r.get("mean_trade_count", 0) > 0 and r.get("mean_profit_factor", 0) > 1.0 and r.get("windows_passed", 0) >= 3
    ]
    print(f"\n{'=' * 60}")
    print(f"Reportable (trades > 0): {len(reportable)} of {len(all_results)} timeframes")
    print(f"Viable (trades > 0, PF > 1.0, >=3 WF windows passed): {len(viable)} of {len(all_results)} timeframes")
    for v in viable:
        print(
            f"  ✅ {v['timeframe']} — PF={v['mean_profit_factor']:.2f} "
            f"WR={v['mean_win_rate']:.1%} {v['windows_passed']}/{v['windows_total']} PASS "
            f"PnL={v['mean_total_pnl']:.0f} trades={v['mean_trade_count']:.0f}"
        )
    if reportable and not viable:
        print("  Weak but trade-producing results:")
        for r in reportable:
            print(
                f"  • {r['timeframe']} — PF={r['mean_profit_factor']:.2f} "
                f"WR={r['mean_win_rate']:.1%} {r['windows_passed']}/{r['windows_total']} PASS "
                f"PnL={r['mean_total_pnl']:.0f} trades={r['mean_trade_count']:.0f}"
            )


if __name__ == "__main__":
    main()
