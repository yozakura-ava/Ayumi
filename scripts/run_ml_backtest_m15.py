"""Full backtest of ML Mean Reversion Signal Filter on EURUSD M15 real data.

Runs walk-forward validation, computes WR/PF/max drawdown/Sharpe/trade count,
and checks FTMO acceptance criteria.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src" / "forex_bot"))
sys.path.insert(0, str(project_root / "src"))

os.environ["FOREX_DB_PATH"] = str(project_root / "data" / "forex" / "forex.db")

from common.resource_limits import add_resource_args, run_limited  # noqa: E402
from ml.features import add_multi_timeframe_features, build_feature_matrix, load_csv  # noqa: E402
from ml.signal_simulator import build_labeled_dataset
from ml.train_model import (  # noqa: E402, I001
    FEATURE_COLUMNS,
    MODEL_TYPE_DEFAULT,
    walk_forward_train,
)
from quant.go_nogo_criteria import AggregateCriteria, PerWindowCriteria  # noqa: E402

SEED = 42
SYMBOL = "EURUSD"
TIMEFRAME = "M15"
DATA_DIR = str(project_root / "data" / "forex")
OUTPUT_DIR = str(project_root / "data" / "forex" / "models")
N_FOLDS = 5
MAX_HOLDING_BARS = 50

ML_PER_WINDOW = PerWindowCriteria(
    min_trades=5,
    win_rate=0.55,
    profit_factor=1.3,
    max_drawdown=0.10,
)
ML_AGGREGATE = AggregateCriteria(
    min_total_trades=50,
    min_windows_passed=3,
    min_total_windows=3,
)


def compute_drawdown(equity_curve: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity_curve)
    drawdown = (peak - equity_curve) / peak
    return float(np.max(drawdown)) * 100


def compute_sharpe(returns: np.ndarray, risk_free_rate: float = 0.0) -> float:
    if len(returns) < 2 or np.std(returns) == 0:
        return 0.0
    excess = returns - risk_free_rate
    return float(np.mean(excess) / np.std(excess) * np.sqrt(252))


def build_equity_curve(trades: pd.DataFrame, starting_balance: float = 10000.0) -> np.ndarray:
    equity = [starting_balance]
    for _, trade in trades.iterrows():
        equity.append(equity[-1] + trade["pnl"])
    return np.array(equity)


def run_full_backtest():
    np.random.seed(SEED)

    print("=" * 70)
    print(f"ML Signal Filter — Full Backtest: {SYMBOL} {TIMEFRAME}")
    print(f"Seed: {SEED} | Folds: {N_FOLDS} | Max holding: {MAX_HOLDING_BARS} bars")
    print(f"Data dir: {DATA_DIR}")
    print("=" * 70)

    csv_path = os.path.join(DATA_DIR, "historical", f"{SYMBOL}_{TIMEFRAME}.csv")
    if not os.path.exists(csv_path):
        print(f"ERROR: Data file not found: {csv_path}")
        sys.exit(1)

    df = load_csv(csv_path)
    print(f"\nLoaded {len(df)} bars from {csv_path}")
    print(f"Date range: {df['date'].iloc[0]} to {df['date'].iloc[-1]}")

    features = build_feature_matrix(df)

    h4_path = os.path.join(DATA_DIR, "historical", f"{SYMBOL}_H4.csv")
    d1_path = os.path.join(DATA_DIR, "historical", f"{SYMBOL}_D1.csv")
    if os.path.exists(h4_path) and os.path.exists(d1_path):
        h4_df = load_csv(h4_path)
        d1_df = load_csv(d1_path)
        features = add_multi_timeframe_features(features, h4_df, d1_df)
        print("Added multi-timeframe features (H4 + D1)")

    dataset = build_labeled_dataset(df, features, MAX_HOLDING_BARS)
    if dataset.empty:
        print("ERROR: No labeled trades generated")
        sys.exit(1)

    print(f"\nLabeled dataset: {len(dataset)} trades")
    print(f"  Overall win rate: {dataset['outcome'].mean() * 100:.1f}%")
    print(f"  Strategies: {dataset['strategy'].value_counts().to_dict()}")

    feature_names = [f for f in FEATURE_COLUMNS if f in dataset.columns]
    print(f"  Features: {len(feature_names)}")

    print("\n--- Walk-Forward Validation ---\n")

    wf_results = walk_forward_train(dataset, n_folds=N_FOLDS, random_state=SEED, model_types=[MODEL_TYPE_DEFAULT])

    folds = wf_results.get("folds", [])
    if not folds:
        print("ERROR: Walk-forward validation produced no folds")
        sys.exit(1)

    print(f"Completed {len(folds)} folds\n")

    window_results = []
    passing_windows = 0

    for fold in folds:
        fold_num = fold["fold"]
        train_size = fold["train_size"]
        test_size = fold["test_size"]

        opt_wr = fold.get("opt_win_rate", 0)
        opt_pf = fold.get("opt_profit_factor", 0)
        opt_pnl = fold.get("opt_total_pnl", 0)
        opt_tc = fold.get("opt_trade_count", 0)
        opt_threshold = fold.get("opt_threshold", 0.5)

        baseline_wr = fold.get("baseline_win_rate", 0)
        baseline_pf = fold.get("baseline_profit_factor", 0)
        filtered_wr = fold.get("filtered_win_rate", 0)
        filtered_pf = fold.get("filtered_profit_factor", 0)

        pw_result = ML_PER_WINDOW.evaluate(
            trade_count=opt_tc,
            win_rate=opt_wr / 100.0,
            profit_factor=opt_pf,
            total_pnl=opt_pnl,
            max_drawdown=0.10,
        )
        passes = pw_result.passed
        if passes:
            passing_windows += 1

        window_results.append(
            {
                "window": fold_num + 1,
                "train_size": train_size,
                "test_size": test_size,
                "baseline_wr": baseline_wr,
                "baseline_pf": baseline_pf,
                "filtered_wr": filtered_wr,
                "filtered_pf": filtered_pf,
                "opt_threshold": opt_threshold,
                "opt_win_rate": opt_wr,
                "opt_profit_factor": opt_pf,
                "opt_total_pnl": opt_pnl,
                "opt_trade_count": opt_tc,
                "passes": passes,
            }
        )

        status = "PASS" if passes else "FAIL"
        print(f"Window {fold_num + 1}: train={train_size}, test={test_size}")
        print(f"  Baseline: WR={baseline_wr:.1f}%, PF={baseline_pf:.2f}")
        print(f"  Filtered: WR={filtered_wr:.1f}%, PF={filtered_pf:.2f}")
        print(
            f"  Optimized: threshold={opt_threshold:.2f}, WR={opt_wr:.1f}%, PF={opt_pf:.2f}, trades={opt_tc}, PnL={opt_pnl:.2f}"  # noqa: E501
        )
        print(f"  [{status}]\n")

    summary = wf_results.get("summary", {})

    avg_wr = summary.get("avg_opt_win_rate", 0)
    avg_pf = summary.get("avg_opt_profit_factor", 0)
    _avg_pnl = summary.get("avg_opt_total_pnl", 0)
    _avg_tc = summary.get("avg_opt_trade_count", 0)
    avg_threshold = summary.get("avg_opt_threshold", 0)
    avg_baseline_wr = summary.get("avg_baseline_win_rate", 0)
    avg_filtered_wr = summary.get("avg_filtered_win_rate", 0)
    avg_baseline_pf = summary.get("avg_baseline_profit_factor", 0)
    avg_filtered_pf = summary.get("avg_filtered_profit_factor", 0)

    all_opt_pnls = [f["opt_total_pnl"] for f in window_results]
    _all_opt_wrs = [f["opt_win_rate"] for f in window_results]
    _all_opt_pfs = [f["opt_profit_factor"] for f in window_results]
    all_opt_tcs = [f["opt_trade_count"] for f in window_results]

    total_trades = sum(all_opt_tcs)
    total_pnl = sum(all_opt_pnls)

    returns = np.array(all_opt_pnls)
    sharpe = compute_sharpe(returns) if len(returns) > 1 else 0.0

    simulated_equity = build_equity_curve(pd.DataFrame({"pnl": all_opt_pnls}), 10000.0)
    max_dd = compute_drawdown(simulated_equity)

    overall_wr = avg_wr
    overall_pf = avg_pf

    meets_wr = overall_wr >= ML_PER_WINDOW.win_rate * 100
    meets_pf = overall_pf >= ML_PER_WINDOW.profit_factor
    agg_result = ML_AGGREGATE.evaluate(
        total_trades=total_trades,
        windows_passed=passing_windows,
        total_windows=len(window_results),
    )
    meets_windows = agg_result.passed
    go_nogo = meets_wr and meets_pf and meets_windows

    print("=" * 70)
    print("AGGREGATE RESULTS")
    print("=" * 70)
    print(f"\nWalk-Forward Windows: {len(window_results)}")
    print(f"Passing Windows:     {passing_windows}/{len(window_results)} (need >= {ML_AGGREGATE.min_windows_passed})")
    print("\n--- Average Optimized Metrics ---")
    print(
        f"  Win Rate:       {avg_wr:.2f}% (target: >= {ML_PER_WINDOW.win_rate * 100}%) [{'PASS' if meets_wr else 'FAIL'}]"  # noqa: E501
    )
    print(
        f"  Profit Factor:  {avg_pf:.2f} (target: >= {ML_PER_WINDOW.profit_factor}) [{'PASS' if meets_pf else 'FAIL'}]"
    )
    print(f"  Sharpe Ratio:   {sharpe:.4f}")
    print(f"  Max Drawdown:   {max_dd:.2f}%")
    print(f"  Total Trades:   {total_trades}")
    print(f"  Total PnL:      {total_pnl:.2f}")
    print(f"  Avg Threshold:  {avg_threshold:.2f}")
    print("\n--- Improvement over Baseline ---")
    print(f"  Win Rate:  {avg_baseline_wr:.1f}% -> {avg_wr:.1f}% (filtered: {avg_filtered_wr:.1f}%)")
    print(f"  PF:        {avg_baseline_pf:.2f} -> {avg_pf:.2f} (filtered: {avg_filtered_pf:.2f})")
    print(f"\n{'=' * 70}")
    print(f"GO/NO-GO: {'GO' if go_nogo else 'NO-GO'}")
    print(f"{'=' * 70}")

    report = {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "seed": SEED,
        "n_folds": N_FOLDS,
        "max_holding_bars": MAX_HOLDING_BARS,
        "data_bars": len(df),
        "date_range": {
            "start": str(df["date"].iloc[0]),
            "end": str(df["date"].iloc[-1]),
        },
        "labeled_trades": len(dataset),
        "strategy_breakdown": dataset["strategy"].value_counts().to_dict(),
        "baseline_win_rate": dataset["outcome"].mean() * 100,
        "acceptance_criteria": {
            "min_win_rate": ML_PER_WINDOW.win_rate * 100,
            "min_profit_factor": ML_PER_WINDOW.profit_factor,
            "min_passing_windows": ML_AGGREGATE.min_windows_passed,
        },
        "window_results": window_results,
        "passing_windows": passing_windows,
        "total_windows": len(window_results),
        "aggregate": {
            "avg_win_rate": avg_wr,
            "avg_profit_factor": avg_pf,
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown_pct": round(max_dd, 2),
            "total_trades": total_trades,
            "total_pnl": round(total_pnl, 4),
            "avg_threshold": avg_threshold,
            "avg_baseline_win_rate": avg_baseline_wr,
            "avg_baseline_profit_factor": avg_baseline_pf,
            "avg_filtered_win_rate": avg_filtered_wr,
            "avg_filtered_profit_factor": avg_filtered_pf,
        },
        "acceptance": {
            "meets_win_rate": meets_wr,
            "meets_profit_factor": meets_pf,
            "meets_passing_windows": meets_windows,
            "go_nogo": go_nogo,
        },
        "model_type": MODEL_TYPE_DEFAULT,
        "model_params": wf_results.get("final_params"),
    }

    if "feature_importance" in folds[-1]:
        report["top_features"] = dict(list(folds[-1]["feature_importance"].items())[:15])

    report_path = os.path.join(OUTPUT_DIR, "m15_backtest_report.json")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nReport saved to: {report_path}")

    return report


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        report = run_full_backtest()
        sys.exit(0 if report["acceptance"]["go_nogo"] else 1)

    _run()
