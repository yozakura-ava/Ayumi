#!/usr/bin/env python3
"""
Parameter Sweep for HighConvictionStrategy — Phased approach

Phase 1: Coarse sweep on trade-frequency-critical params (min_confluences,
          atr_percentile_threshold, max_trades_per_week) to find combos
          that generate >= 20 trades.
Phase 2: Fine sweep on remaining params for promising coarse combos.
Phase 3: Walk-forward validation of top 5 FTMO-passing combos.

Usage:
    python scripts/run_high_conviction_sweep.py
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader  # noqa: E402
from backtest.engine import BacktestConfig, get_spread_for_pair  # noqa: E402, I001
from backtest.enhanced_engine import EnhancedBacktestEngine  # noqa: E402
from backtest.parameter_sweep import (  # noqa: E402
    ParameterGrid,
    SweepResult,
    SweepRow,
    to_csv,
    to_json,
)
from backtest.strategies import HighConvictionStrategy  # noqa: E402
from backtest.walk_forward_runner import run_strategy_walk_forward  # noqa: E402
from common.resource_limits import add_resource_args, run_limited  # noqa: E402

DATA_PATHS = {
    "EURUSD": "data/forex/historical/EURUSD_H1.csv",
    "GBPUSD": "data/forex/historical/GBPUSD_H1.csv",
    "GBPJPY": "data/forex/historical/GBPJPY_H1.csv",
}
REPORT_DIR = Path("reports/parameter_sweeps")

COARSE_PARAMS = {
    "min_confluences": [3, 4, 5],
    "atr_percentile_threshold": [0.30, 0.40, 0.50, 0.60],
    "max_trades_per_week": [1, 2, 3],
}

FINE_PARAMS = {
    "trend_lookback": [10, 15, 20, 25, 30],
    "swing_lookback": [30, 40, 50, 60, 80],
    "rsi_period": [10, 14, 20],
    "sl_atr_mult": [2.0, 2.5, 3.0, 4.0],
    "tp_atr_mult": [4.0, 5.0, 6.0, 8.0],
}

FIXED_PARAMS = {
    "atr_period": 14,
    "atr_percentile_lookback": 100,
}

FTMO_WR = 55.0
FTMO_PF = 1.3
FTMO_SHARPE = 0.5
MIN_TRADES = 20

WF_MIN_TRADES_PER_WINDOW = 5


def _sanitize_float(value):
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return None
    return value


def _sanitize_for_json(obj):
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return _sanitize_float(obj)


def run_single_backtest(bars, config, strategy):
    engine = EnhancedBacktestEngine(config=config, strategies=[strategy])
    try:
        metrics = engine.run_strategy(strategy, bars)
        return {
            "win_rate": metrics.win_rate,
            "max_dd": metrics.max_drawdown_pct,
            "total_return": metrics.total_pnl_pct,
            "sharpe_ratio": metrics.sharpe_ratio,
            "trade_count": metrics.total_trades,
            "profit_factor": metrics.profit_factor,
        }
    except Exception:
        return None


def phase1_coarse_sweep(bars, config):
    grid = ParameterGrid(COARSE_PARAMS)
    print(f"  Phase 1: {grid.size} coarse combinations")

    default_fine = {
        "trend_lookback": 20,
        "swing_lookback": 50,
        "rsi_period": 14,
        "sl_atr_mult": 3.0,
        "tp_atr_mult": 6.0,
    }

    results = []
    for point in grid:
        params = {**FIXED_PARAMS, **default_fine, **point.params}
        strategy = HighConvictionStrategy(**params)
        metrics = run_single_backtest(bars, config, strategy)

        if metrics and metrics["trade_count"] >= MIN_TRADES:
            row = SweepRow(params=point.params, **metrics)
            results.append(row)
            print(
                f"    PASS: mc={point.params['min_confluences']} "
                f"atr_pct={point.params['atr_percentile_threshold']} "
                f"tpw={point.params['max_trades_per_week']} -> "
                f"Trades={metrics['trade_count']} WR={metrics['win_rate']:.2f} "
                f"PF={metrics['profit_factor']:.2f} Sharpe={metrics['sharpe_ratio']:.2f}"
            )

    return results


def phase2_fine_sweep(bars, config, coarse_combos):
    fine_grid = ParameterGrid(FINE_PARAMS)
    print(f"  Phase 2: {len(coarse_combos)} promising combos x {fine_grid.size} fine params")

    all_results = []
    for coarse_row in coarse_combos:
        coarse_params = coarse_row.params
        for fine_point in fine_grid:
            params = {**FIXED_PARAMS, **coarse_params, **fine_point.params}
            strategy = HighConvictionStrategy(**params)
            metrics = run_single_backtest(bars, config, strategy)

            if metrics:
                combined_params = {**coarse_params, **fine_point.params}
                row = SweepRow(params=combined_params, **metrics)
                all_results.append(row)

    return SweepResult(rows=all_results)


def run_walk_forward_for_combo(combo_params, bars, pair, n_windows=5):
    """Walk-forward validation using MultiStrategyBacktestEngine.

    Note: Walk-forward uses MultiStrategyBacktestEngine internally (via
    run_strategy_walk_forward), while Phase 1/2 backtests use
    EnhancedBacktestEngine. The engines produce different trade execution
    semantics (spread/slippage, order management). Walk-forward results
    are therefore not directly comparable to Phase 1/2 sweep metrics on
    a 1:1 basis, but both are valid within their respective contexts.
    """

    def factory():
        return HighConvictionStrategy(**combo_params)

    wf_result = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        n_windows=n_windows,
        train_ratio=0.6,
        val_ratio=0.15,
    )

    per_window = []
    for w in wf_result.per_window:
        per_window.append(
            {
                "window": w.window_index,
                "win_rate": w.win_rate,
                "profit_factor": w.profit_factor,
                "sharpe_ratio": w.sharpe_ratio,
                "max_drawdown": w.max_drawdown,
                "trade_count": w.trade_count,
                "total_pnl": w.total_pnl,
                "passed": w.passed_go_nogo,
            }
        )

    agg = wf_result.aggregated
    return {
        "params": combo_params,
        "pair": pair,
        "go_nogo": wf_result.go_nogo,
        "windows_passed": sum(1 for w in wf_result.per_window if w.passed_go_nogo) if wf_result.per_window else 0,
        "total_windows": len(wf_result.per_window),
        "aggregated": {
            "mean_win_rate": agg.mean_win_rate if agg else 0,
            "mean_profit_factor": agg.mean_profit_factor if agg else 0,
            "mean_sharpe_ratio": agg.mean_sharpe_ratio if agg else 0,
            "mean_trade_count": agg.mean_trade_count if agg else 0,
            "mean_max_drawdown": agg.mean_max_drawdown if agg else 0,
        },
        "per_window": per_window,
    }


def main():
    parser = argparse.ArgumentParser(description="High Conviction Strategy parameter sweep")
    add_resource_args(parser)
    _args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    loader = CsvDataLoader()

    pair_summaries = {}
    sweep_scope = {}
    any_go_nogo_global = False

    for pair, csv_path in DATA_PATHS.items():
        print(f"\n{'=' * 70}")
        print(f"  {pair} — HighConvictionStrategy Parameter Sweep")
        print(f"{'=' * 70}")

        bars = loader.load(csv_path)
        print(f"  Loaded {len(bars)} bars: {bars[0].time} -> {bars[-1].time}")

        config = BacktestConfig(
            pair=pair,
            spread_pips=get_spread_for_pair(pair),
            min_bars_before_signal=30,
        )

        t0 = time.time()

        coarse_results = phase1_coarse_sweep(bars, config)
        print(
            f"  Phase 1 done: {len(coarse_results)}/{ParameterGrid(COARSE_PARAMS).size} combos with >= {MIN_TRADES} trades"  # noqa: E501
        )

        if not coarse_results:
            print("  No coarse combos produce enough trades. Checking all coarse results...")
            grid = ParameterGrid(COARSE_PARAMS)
            default_fine = {
                "trend_lookback": 20,
                "swing_lookback": 50,
                "rsi_period": 14,
                "sl_atr_mult": 3.0,
                "tp_atr_mult": 6.0,
            }
            all_coarse = []
            for point in grid:
                params = {**FIXED_PARAMS, **default_fine, **point.params}
                strategy = HighConvictionStrategy(**params)
                metrics = run_single_backtest(bars, config, strategy)
                if metrics:
                    all_coarse.append((point.params, metrics))
                    print(
                        f"    mc={point.params['min_confluences']} "
                        f"atr_pct={point.params['atr_percentile_threshold']} "
                        f"tpw={point.params['max_trades_per_week']} -> "
                        f"Trades={metrics['trade_count']} WR={metrics['win_rate']:.2f}"
                    )

            top_by_trades = sorted(all_coarse, key=lambda x: x[1]["trade_count"], reverse=True)[:5]
            print("\n  Top 5 by trade count (lowered bar):")
            for params, m in top_by_trades:
                print(f"    {params} -> Trades={m['trade_count']} WR={m['win_rate']:.2f} PF={m['profit_factor']:.2f}")

            wf_results = []
            print("\n  Walk-forward on top 5 by trade count...")
            for params, m in top_by_trades:  # noqa: B007
                wf = run_walk_forward_for_combo(params, bars, pair)
                wf_results.append(wf)
                status = "GO" if wf["go_nogo"] else "NO-GO"
                print(f"    {params} -> {status} ({wf['windows_passed']}/{wf['total_windows']} windows)")

            wf_path = str(REPORT_DIR / f"{pair}_high_conviction_wf_validation.json")
            with open(wf_path, "w") as f:
                json.dump(_sanitize_for_json(wf_results), f, indent=2)

            best = max(all_coarse, key=lambda x: x[1]["trade_count"]) if all_coarse else None
            any_go = any(w["go_nogo"] for w in wf_results) if wf_results else False
            if any_go:
                any_go_nogo_global = True

            pair_summaries[pair] = {
                "combos_tested": len(all_coarse),
                "best_trade_count": best[1]["trade_count"] if best else 0,
                "best_win_rate": round(best[1]["win_rate"] / 100, 2) if best else 0,
                "best_profit_factor": round(best[1]["profit_factor"], 2) if best else 0,
                "best_sharpe": round(best[1]["sharpe_ratio"], 2) if best else 0,
                "ftmo_passing_combos": 0,
                "walk_forward_go_nogo": any_go,
                "verdict": "NO-GO: No parameter combination passes FTMO criteria",
            }
            sweep_scope[f"{pair.lower()}_combos_tested"] = len(all_coarse)
            continue

        sweep_result = phase2_fine_sweep(bars, config, coarse_results)
        elapsed = time.time() - t0
        print(f"  Phase 2 done in {elapsed:.1f}s — {len(sweep_result)} total results")

        csv_out = str(REPORT_DIR / f"{pair}_high_conviction_sweep.csv")
        json_out = str(REPORT_DIR / f"{pair}_high_conviction_sweep.json")
        to_csv(sweep_result, csv_out)
        to_json(sweep_result, json_out)
        print(f"  Results saved to {csv_out}")

        ftmo_passing = sweep_result.filter(
            lambda r: (
                r.trade_count >= MIN_TRADES
                and r.win_rate >= FTMO_WR
                and r.profit_factor >= FTMO_PF
                and r.sharpe_ratio >= FTMO_SHARPE
            )
        )
        print(f"  FTMO-passing combos: {len(ftmo_passing)}")

        wf_results = []
        if ftmo_passing.rows:
            top5 = ftmo_passing.top_n(5, metric="sharpe_ratio")
            print("\n  Top 5 FTMO-passing combos (by Sharpe):")
            for i, row in enumerate(top5, 1):
                print(
                    f"    #{i}: WR={row.win_rate:.2f} PF={row.profit_factor:.2f} "
                    f"Sharpe={row.sharpe_ratio:.2f} Trades={row.trade_count} "
                    f"Return={row.total_return:.2%} DD={row.max_dd:.2%}"
                )
                print(f"         Params: {row.params}")

            print("\n  Running walk-forward validation on top 5...")
            for row in top5:
                wf = run_walk_forward_for_combo(row.params, bars, pair)
                wf_results.append(wf)
                status = "GO" if wf["go_nogo"] else "NO-GO"
                print(
                    f"    -> {status} "
                    f"({wf['windows_passed']}/{wf['total_windows']} windows) "
                    f"agg_WR={wf['aggregated']['mean_win_rate']:.2f} "
                    f"agg_PF={wf['aggregated']['mean_profit_factor']:.2f}"
                )
        else:
            top10 = sweep_result.sort_by("trade_count")[:10]
            print("\n  Top 10 by trade count (no FTMO pass):")
            for i, row in enumerate(top10, 1):
                print(
                    f"    #{i}: Trades={row.trade_count} WR={row.win_rate:.2f} "
                    f"PF={row.profit_factor:.2f} Sharpe={row.sharpe_ratio:.2f} "
                    f"Return={row.total_return:.2%}"
                )

            print("\n  Walk-forward on top 3 by trade count...")
            for row in top10[:3]:
                if row.trade_count < 5:
                    continue
                wf = run_walk_forward_for_combo(row.params, bars, pair)
                wf_results.append(wf)
                status = "GO" if wf["go_nogo"] else "NO-GO"
                print(f"    -> {status} ({wf['windows_passed']}/{wf['total_windows']} windows)")

        wf_path = str(REPORT_DIR / f"{pair}_high_conviction_wf_validation.json")
        with open(wf_path, "w") as f:
            json.dump(_sanitize_for_json(wf_results), f, indent=2)
        print(f"  Walk-forward results saved to {wf_path}")

        best_row = max(sweep_result.rows, key=lambda r: r.trade_count) if sweep_result.rows else None
        any_go = any(w["go_nogo"] for w in wf_results) if wf_results else False
        if any_go:
            any_go_nogo_global = True

        pair_summaries[pair] = {
            "combos_tested": len(sweep_result),
            "best_trade_count": best_row.trade_count if best_row else 0,
            "best_win_rate": round(best_row.win_rate / 100, 2) if best_row else 0,
            "best_profit_factor": round(best_row.profit_factor, 2) if best_row else 0,
            "best_sharpe": round(best_row.sharpe_ratio, 2) if best_row else 0,
            "ftmo_passing_combos": len(ftmo_passing.rows),
            "walk_forward_go_nogo": any_go,
            "verdict": "GO" if any_go else "NO-GO: No parameter combination passes FTMO criteria",
        }
        sweep_scope[f"{pair.lower()}_combos_tested"] = len(sweep_result)

    overall_verdict = (
        "GO: At least one pair passes walk-forward validation"
        if any_go_nogo_global
        else "NO-GO: No pair passes FTMO criteria at any parameter combination"
    )

    summary = {
        "sweep_date": time.strftime("%Y-%m-%d"),
        "strategy": "HighConvictionStrategy",
        "ftmo_criteria": {
            "min_win_rate": FTMO_WR / 100,
            "min_profit_factor": FTMO_PF,
            "min_sharpe": FTMO_SHARPE,
            "min_trades": MIN_TRADES,
        },
        "sweep_scope": sweep_scope,
        "results": pair_summaries,
        "overall_verdict": overall_verdict,
    }

    summary_path = str(REPORT_DIR / "sweep_summary.json")
    with open(summary_path, "w") as f:
        json.dump(_sanitize_for_json(summary), f, indent=2)
    print(f"\n  Summary saved to {summary_path}")
    print("  Done.")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
