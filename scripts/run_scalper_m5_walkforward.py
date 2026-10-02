#!/usr/bin/env python3
"""M5 Scalper Walk-Forward Validation

Runs ScalperStrategy across 5 walk-forward windows on M5 data.
GO/NO-GO criteria: WR > 45%, PF > 1.3, 3/5 windows pass.

Refs AYUAA-768.
"""

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.engine import BacktestConfig, get_spread_for_pair
from backtest.multi_strategy_engine import MultiStrategyBacktestEngine
from backtest.strategies import ScalperStrategy
from common.resource_limits import add_resource_args, run_limited
from quant.go_nogo_criteria import AggregateCriteria, PerWindowCriteria
from quant.walk_forward import WalkForwardValidator
from signal_engine.risk_sizer import ConfidencePositionSizer

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
DATA_DIR = PROJECT_ROOT / "data" / "forex" / "historical"
REPORT_DIR = PROJECT_ROOT / "reports" / "scalper_m5_wf"
N_WINDOWS = 5
TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
INITIAL_BALANCE = 10000.0

SCALPER_PER_WINDOW = PerWindowCriteria(
    min_trades=5,
    win_rate=0.45,
    profit_factor=1.3,
    max_drawdown=0.10,
)
SCALPER_AGGREGATE = AggregateCriteria(
    min_total_trades=50,
    min_windows_passed=3,
    min_total_windows=3,
)


@dataclass
class WindowResult:
    window_index: int
    train_bars: int
    test_bars: int
    trade_count: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    total_pnl: float
    passed: bool


@dataclass
class PairResult:
    pair: str
    windows: list[WindowResult] = field(default_factory=list)
    windows_passed: int = 0
    total_windows: int = 0
    go_nogo: bool = False
    mean_wr: float = 0.0
    mean_pf: float = 0.0
    mean_trades: float = 0.0
    mean_pnl: float = 0.0


def _compute_window_metrics(
    window_index: int,
    trades: list,
    initial_balance: float,
) -> WindowResult:
    if not trades:
        return WindowResult(
            window_index=window_index,
            train_bars=0,
            test_bars=0,
            trade_count=0,
            win_rate=0.0,
            profit_factor=0.0,
            max_drawdown=0.0,
            sharpe_ratio=0.0,
            total_pnl=0.0,
            passed=False,
        )

    pnls = [t.profit_loss for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_win = sum(wins)
    total_loss = abs(sum(losses))

    win_rate = len(wins) / len(pnls)
    profit_factor = total_win / total_loss if total_loss > 0 else (10.0 if total_win > 0 else 0.0)
    total_pnl = sum(pnls)
    trade_count = len(pnls)

    balance = initial_balance
    peak_balance = initial_balance
    max_dd = 0.0
    for p in pnls:
        balance = max(0.0, balance + p)
        if balance > peak_balance:
            peak_balance = balance
        if peak_balance > 0:
            dd = (peak_balance - balance) / peak_balance
            if dd > max_dd:
                max_dd = dd

    if trade_count < 2:
        sharpe_ratio = 0.0
    else:
        mean_pnl = total_pnl / trade_count
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (trade_count - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.0
        sharpe_ratio = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0.0

    pw_result = SCALPER_PER_WINDOW.evaluate(
        trade_count=trade_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        max_drawdown=max_dd,
    )
    passed = pw_result.passed

    return WindowResult(
        window_index=window_index,
        train_bars=0,
        test_bars=0,
        trade_count=trade_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe_ratio,
        total_pnl=total_pnl,
        passed=passed,
    )


def run_scalper_walkforward(pair: str) -> PairResult:
    csv_path = DATA_DIR / f"{pair}_M5.csv"
    if not csv_path.exists():
        print(f"  SKIP: {csv_path} not found")
        return PairResult(pair=pair)

    loader = CsvDataLoader()
    bars = loader.load(str(csv_path))
    print(f"  Loaded {len(bars)} M5 bars")

    if len(bars) < 1000:
        print(f"  SKIP: insufficient data ({len(bars)} bars)")
        return PairResult(pair=pair)

    validator = WalkForwardValidator(
        data=bars,
        n_windows=N_WINDOWS,
        train_ratio=TRAIN_RATIO,
        val_ratio=VAL_RATIO,
        overlap_ratio=0.2,
    )

    spread = get_spread_for_pair(pair)
    config = BacktestConfig(
        starting_balance=INITIAL_BALANCE,
        spread_pips=spread,
        commission_per_lot=3.5,
        pair=pair,
        min_confidence=0.25,
        min_bars_before_signal=55,
        max_open_trades=1,
    )

    result = PairResult(pair=pair)
    risk_sizer = ConfidencePositionSizer(account_size=INITIAL_BALANCE)

    for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):
        print(f"  Window {idx}: train={len(train_bars)}, val={len(val_bars)}, test={len(test_bars)}")

        if len(test_bars) < config.min_bars_before_signal:
            print(f"    SKIP: test too short ({len(test_bars)} bars)")
            w = WindowResult(
                window_index=idx,
                train_bars=len(train_bars),
                test_bars=len(test_bars),
                trade_count=0,
                win_rate=0.0,
                profit_factor=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                total_pnl=0.0,
                passed=False,
            )
            result.windows.append(w)
            result.total_windows += 1
            continue

        strategy = ScalperStrategy(symbol=pair, timeframe="M5")
        strategy.reset()

        try:
            engine = MultiStrategyBacktestEngine(config, [strategy], risk_sizer=risk_sizer)
            engine_result = engine.run_all_strategies(test_bars)
            metrics = engine_result[strategy.name].metrics
            trades = metrics.trades
        except Exception as e:
            print(f"    ERROR: {e}")
            trades = []

        w = _compute_window_metrics(idx, trades, INITIAL_BALANCE)
        w.train_bars = len(train_bars)
        w.test_bars = len(test_bars)
        result.windows.append(w)
        result.total_windows += 1

        status = "PASS" if w.passed else "FAIL"
        print(
            f"    {status}: {w.trade_count} trades, "
            f"WR={w.win_rate:.1%}, PF={w.profit_factor:.2f}, "
            f"PnL=${w.total_pnl:.2f}, DD={w.max_drawdown:.1%}"
        )

    result.windows_passed = sum(1 for w in result.windows if w.passed)
    total_trades = sum(w.trade_count for w in result.windows)
    agg_result = SCALPER_AGGREGATE.evaluate(
        total_trades=total_trades,
        windows_passed=result.windows_passed,
        total_windows=result.total_windows,
    )
    result.go_nogo = agg_result.passed

    if result.windows:
        result.mean_wr = sum(w.win_rate for w in result.windows) / len(result.windows)
        result.mean_pf = sum(w.profit_factor for w in result.windows) / len(result.windows)
        result.mean_trades = sum(w.trade_count for w in result.windows) / len(result.windows)
        result.mean_pnl = sum(w.total_pnl for w in result.windows) / len(result.windows)

    return result


def main():
    parser = argparse.ArgumentParser(description="M5 Scalper walk-forward validation")
    add_resource_args(parser)
    _args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    all_results = {}

    for pair in PAIRS:
        print(f"\n{'=' * 70}")
        print(f"  Scalper M5 Walk-Forward: {pair}")
        print(f"{'=' * 70}")

        result = run_scalper_walkforward(pair)
        all_results[pair] = result

        verdict = "GO" if result.go_nogo else "NO-GO"
        print(f"\n  RESULT: {result.windows_passed}/{result.total_windows} windows passed -> {verdict}")
        print(f"  Mean WR={result.mean_wr:.1%}, PF={result.mean_pf:.2f}, Trades={result.mean_trades:.0f}/window")

    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")

    go_pairs = []
    for pair, r in all_results.items():
        verdict = "GO" if r.go_nogo else "NO-GO"
        print(f"  {pair}: {r.windows_passed}/{r.total_windows} windows -> {verdict}")
        if r.go_nogo:
            go_pairs.append(pair)

    print(f"\n  Pairs passing GO/NO-GO: {go_pairs}")
    print(f"  Overall: {'DIVERSIFICATION READY' if len(go_pairs) >= 2 else 'NEEDS IMPROVEMENT'}")

    report_path = REPORT_DIR / f"scalper_m5_wf_{timestamp}.json"
    report = {
        "timestamp": timestamp,
        "criteria": {
            "min_wr": SCALPER_PER_WINDOW.win_rate,
            "min_pf": SCALPER_PER_WINDOW.profit_factor,
            "min_max_drawdown": SCALPER_PER_WINDOW.max_drawdown,
            "min_windows_passed": SCALPER_AGGREGATE.min_windows_passed,
            "total_windows": N_WINDOWS,
        },
        "pairs": {},
    }
    for pair, r in all_results.items():
        report["pairs"][pair] = {
            "go_nogo": r.go_nogo,
            "windows_passed": r.windows_passed,
            "total_windows": r.total_windows,
            "mean_wr": round(r.mean_wr, 4),
            "mean_pf": round(r.mean_pf, 4),
            "mean_trades": round(r.mean_trades, 1),
            "mean_pnl": round(r.mean_pnl, 2),
            "windows": [asdict(w) for w in r.windows],
        }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report: {report_path}")


if __name__ == "__main__":
    _p = argparse.ArgumentParser(add_help=False)
    add_resource_args(_p)
    _known, _unknown = _p.parse_known_args()

    @run_limited(cpu_percent=_known.max_cpu, memory_mb=_known.max_memory_mb)
    def _run():
        main()

    _run()
