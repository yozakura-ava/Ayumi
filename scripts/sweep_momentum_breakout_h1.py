#!/usr/bin/env python3
"""
Parameter Sweep + Walk-Forward for Momentum Breakout Strategy (H1)

Runs a parameter sweep on the MomentumBreakoutStrategy across 5 pairs (EURUSD,
GBPUSD, GBPJPY, USDJPY, XAUUSD) on H1 data, identifies top 5 parameter sets
per pair, then runs full 5-window walk-forward validation.

Parameter mapping (parent params -> existing strategy params):
  - fast_period, slow_period: EMA crossover periods
  - adx_period, adx_threshold: ADX trend filter
  - atr_multiplier: ATR-based stop loss
  - rsi_period: optional RSI filter (None = disabled)
  - rsi_overbought, rsi_oversold: RSI filter zones

GO Criteria: 3/5 windows pass (WR>55%, PF>1.2, DD<10%)
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

PAIRS = {
    "EURUSD": "data/forex/historical/EURUSD_H1.csv",
    "GBPUSD": "data/forex/historical/GBPUSD_H1.csv",
    "GBPJPY": "data/forex/historical/GBPJPY_H1.csv",
    "USDJPY": "data/forex/historical/USDJPY_H1.csv",
    "XAUUSD": "data/forex/historical/XAUUSD_H1.csv",
}

SWEEP_BARS = 2000
REPORT_DIR = Path("reports/walk_forward")
SWEEP_REPORT_DIR = Path("reports/parameter_sweep")

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader  # noqa: E402
from backtest.engine import BacktestConfig  # noqa: E402, I001
from backtest.parameter_sweep import ParameterGrid, SweepResult, SweepRunner  # noqa: E402
from backtest.strategies import MomentumBreakoutStrategy  # noqa: E402

PARAM_SPACE_NO_RSI: Dict[str, List[Any]] = {
    "fast_period": [5, 9, 12],
    "slow_period": [21, 26],
    "adx_threshold": [20.0, 25.0, 30.0],
    "atr_multiplier": [1.5, 2.0, 2.5],
}

PARAM_SPACE_WITH_RSI: Dict[str, List[Any]] = {
    "fast_period": [5, 9, 12],
    "slow_period": [21, 26],
    "adx_threshold": [20.0, 25.0],
    "atr_multiplier": [1.5, 2.0],
    "rsi_period": [14],
    "rsi_overbought": [70.0],
    "rsi_oversold": [30.0],
}

BACKTEST_CONFIG = {
    "starting_balance": 10000.0,
    "risk_per_trade_pct": 0.005,
    "max_daily_drawdown_pct": 0.03,
    "max_total_drawdown_pct": 0.05,
    "spread_pips": 0.0,
    "commission_per_lot": 3.5,
    "leverage": 100,
    "min_confidence": 0.5,
    "max_open_trades": 1,
    "min_bars_before_signal": 30,
    "round_trip_spread": True,
    "slippage_pips": 0.2,
    "swap_per_lot_per_day": -2.0,
}


def create_strategy(params: Dict[str, Any]) -> MomentumBreakoutStrategy:
    kwargs = {
        "fast_period": params["fast_period"],
        "slow_period": params["slow_period"],
        "adx_threshold": params["adx_threshold"],
        "atr_multiplier": params["atr_multiplier"],
    }
    if params.get("rsi_period") is not None:
        kwargs["rsi_period"] = params["rsi_period"]
        kwargs["rsi_overbought"] = params["rsi_overbought"]
        kwargs["rsi_oversold"] = params["rsi_oversold"]
    return MomentumBreakoutStrategy(**kwargs)


def _strategy_factory(point):
    return create_strategy(point.params)


def run_sweep(
    bars: list,
    pair: str,
    config: BacktestConfig,
    param_space: Dict[str, List[Any]],
    max_workers: int | None = None,
) -> tuple:
    grid = ParameterGrid(param_space)
    print(f"  Parameter space: {len(grid)} combinations")

    def factory(point):
        return create_strategy(point.params)

    runner = SweepRunner(config=config, bars=bars, strategy_factory=factory, max_workers=max_workers)
    result = runner.run(grid)

    return result, grid


def run_walk_forward_for_params(
    bars: list,
    params: Dict[str, Any],
    pair: str,
    config: BacktestConfig,
    n_windows: int = 5,
    train_ratio: float = 0.60,
    val_ratio: float = 0.15,
) -> Dict[str, Any]:
    from backtest.walk_forward_runner import run_strategy_walk_forward

    def strategy_factory():
        return create_strategy(params)

    wf_results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=strategy_factory,
        pair=pair,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        initial_balance=config.starting_balance,
        spread_pips=config.spread_pips,
        commission_per_lot=config.commission_per_lot,
    )

    per_window = []
    for wm in wf_results.per_window:
        per_window.append(
            {
                "window_id": wm.window_index,
                "win_rate": wm.win_rate,
                "profit_factor": wm.profit_factor,
                "max_drawdown": wm.max_drawdown,
                "sharpe_ratio": wm.sharpe_ratio,
                "trade_count": wm.trade_count,
                "total_pnl": wm.total_pnl,
                "passed_go_nogo": wm.passed_go_nogo,
            }
        )

    windows_passed = sum(1 for r in per_window if r["passed_go_nogo"])
    avg_wr = sum(r["win_rate"] for r in per_window) / len(per_window) if per_window else 0
    avg_pf = sum(r["profit_factor"] for r in per_window) / len(per_window) if per_window else 0
    avg_dd = sum(r["max_drawdown"] for r in per_window) / len(per_window) if per_window else 0

    return {
        "params": {k: v for k, v in params.items()},
        "per_window": per_window,
        "windows_passed": windows_passed,
        "total_windows": len(per_window),
        "avg_wr": avg_wr,
        "avg_pf": avg_pf,
        "avg_dd": avg_dd,
        "go_nogo": wf_results.go_nogo,
    }


def main() -> None:
    SWEEP_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    loader = CsvDataLoader()

    all_sweep_results = {}
    top_params_by_pair = {}

    param_spaces = [
        ("no_rsi", PARAM_SPACE_NO_RSI),
        ("with_rsi", PARAM_SPACE_WITH_RSI),
    ]

    for pair, csv_path in PAIRS.items():
        print(f"\n{'=' * 70}")
        print(f"  PARAMETER SWEEP: {pair}")
        print(f"{'=' * 70}")

        bars = loader.load(csv_path)
        print(f"  Loaded {len(bars)} bars: {bars[0].time} -> {bars[-1].time}")

        config = BacktestConfig(pair=pair, **BACKTEST_CONFIG)
        sweep_bars = bars[-SWEEP_BARS:] if len(bars) > SWEEP_BARS else bars
        print(f"  Using last {len(sweep_bars)} bars for sweep phase")

        merged_rows = []

        for sweep_name, param_space in param_spaces:
            print(f"\n  Running {sweep_name} sweep ({len(ParameterGrid(param_space))} combos)...")
            sweep_result, grid = run_sweep(sweep_bars, pair, config, param_space, max_workers=None)

            print(f"  {sweep_name}: {len(sweep_result)} parameter sets with trades")
            for row in sweep_result:
                merged_rows.append(row)

        combined = SweepResult(rows=merged_rows)
        print(f"\n  Combined sweep results: {len(combined)} parameter sets with trades")

        sweep_data = {
            "pair": pair,
            "param_space_no_rsi": {k: v for k, v in PARAM_SPACE_NO_RSI.items()},
            "param_space_with_rsi": {k: v for k, v in PARAM_SPACE_WITH_RSI.items()},
            "sets_with_trades": len(combined),
            "results": [
                {
                    "params": row.params,
                    "win_rate": row.win_rate,
                    "profit_factor": row.profit_factor,
                    "max_drawdown_pct": row.max_dd,
                    "sharpe_ratio": row.sharpe_ratio,
                    "total_trades": row.trade_count,
                    "total_return": row.total_return,
                }
                for row in combined
            ],
        }

        sweep_path = SWEEP_REPORT_DIR / f"{pair}_momentum_breakout_sweep.json"
        with open(sweep_path, "w") as f:
            json.dump(sweep_data, f, indent=2, default=str)
        print(f"  Sweep report saved: {sweep_path}")

        all_sweep_results[pair] = sweep_data

        profitable = combined.filter(lambda r: r.profit_factor > 1.0 and r.trade_count >= 10)
        print(f"  Profitable sets (PF>1.0, trades>=10): {len(profitable)}")

        viable = combined.filter(lambda r: r.trade_count >= 10)
        top5 = viable.top_n(5, metric="profit_factor", ascending=False)
        top_params_by_pair[pair] = top5

        print("\n  Top 5 parameter sets by profit_factor (min 10 trades):")
        for i, row in enumerate(top5):
            print(
                f"    {i + 1}. PF={row.profit_factor:.2f}, WR={row.win_rate:.1f}%, "
                f"DD={row.max_dd:.2f}%, trades={row.trade_count}"
            )
            print(f"       {row.params}")

    print(f"\n{'=' * 70}")
    print("  WALK-FORWARD VALIDATION ON TOP 5 PARAM SETS")
    print(f"{'=' * 70}")

    final_results = {}

    for pair, top5 in top_params_by_pair.items():
        print(f"\n  {pair}:")
        csv_path = PAIRS[pair]
        bars = loader.load(csv_path)
        config = BacktestConfig(pair=pair, **BACKTEST_CONFIG)

        pair_wf_results = []
        for i, row in enumerate(top5):
            print(f"\n  [{i + 1}/{len(top5)}] Testing params: {row.params}")
            wf_result = run_walk_forward_for_params(bars, row.params, pair, config)

            status = "GO" if wf_result["go_nogo"] else "NO-GO"
            print(f"      Windows passed: {wf_result['windows_passed']}/{wf_result['total_windows']}")
            print(
                f"      Avg WR: {wf_result['avg_wr']:.1f}%, PF: {wf_result['avg_pf']:.2f}, "
                f"DD: {wf_result['avg_dd']:.2f}%"
            )
            print(f"      Status: {status}")

            pair_wf_results.append(wf_result)

        final_results[pair] = pair_wf_results

        wf_report_path = REPORT_DIR / f"MomentumBreakout_{pair}_sweep_walkforward.json"
        with open(wf_report_path, "w") as f:
            json.dump(pair_wf_results, f, indent=2, default=str)
        print(f"\n  Walk-forward report saved: {wf_report_path}")

    print(f"\n{'=' * 70}")
    print("  FINAL SUMMARY")
    print(f"{'=' * 70}")

    any_go = False
    for pair, results in final_results.items():
        print(f"\n  {pair}:")
        for i, r in enumerate(results):
            status = "GO" if r["go_nogo"] else "NO-GO"
            print(f"    Param set {i + 1}: {r['params']}")
            print(f"      -> {status} ({r['windows_passed']}/{r['total_windows']} windows passed)")

        go_sets = [r for r in results if r["go_nogo"]]
        if go_sets:
            any_go = True
            print(f"\n  *** {pair}: {len(go_sets)} parameter set(s) passed 3/5 windows - GO! ***")
        else:
            print(f"\n  {pair}: No parameter sets passed - NO-GO")

    if any_go:
        print(f"\n{'=' * 70}")
        print("  AT LEAST ONE GO RESULT FOUND!")
        print(f"{'=' * 70}")
    else:
        print(f"\n{'=' * 70}")
        print("  ALL PAIRS: NO-GO")
        print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
