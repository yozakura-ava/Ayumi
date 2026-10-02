"""Walk-forward validation: Baseline vs MTF-Filtered momentum strategies."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.walk_forward_runner import run_strategy_walk_forward
from quant.mtf_regime import MTFRegimeConfig
from quant.walk_forward import comparison_report
from strategies.momentum import (
    ATRVolatilityBreakoutStrategy,
    DonchianBreakoutStrategy,
    MATrendFollowingStrategy,
)
from strategies.mtf_filtered_momentum import MTFFilteredMomentumStrategy

loader = CsvDataLoader()
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "forex", "historical")

PAIR = "EURUSD"
M15_FILE = os.path.join(DATA_DIR, f"{PAIR}_M15.csv")

print(f"Loading {M15_FILE}...")
bars = loader.load(M15_FILE)
print(f"Loaded {len(bars)} M15 bars")

REGIME_CONFIG = MTFRegimeConfig(
    min_confluence=0.5,
    require_h4_alignment=True,
)

strategies_to_test = [
    ("Donchian", lambda: DonchianBreakoutStrategy(channel_period=20)),
    (
        "ATR Breakout",
        lambda: ATRVolatilityBreakoutStrategy(atr_period=14, breakout_multiplier=1.5),
    ),
    ("MA Trend", lambda: MATrendFollowingStrategy(fast_period=8, slow_period=21)),
]


def make_mtf_factory(baseline_factory):
    def factory():
        return MTFFilteredMomentumStrategy(
            baseline_factory(),
            regime_config=REGIME_CONFIG,
            confidence_boost=0.05,
            direction_filter=True,
            mtf_bars_source_minutes=15,
        )

    return factory


for name, baseline_factory in strategies_to_test:
    print(f"\n{'=' * 60}")
    print(f"Strategy: {name}")
    print(f"{'=' * 60}")

    mtf_factory = make_mtf_factory(baseline_factory)

    print("Running baseline walk-forward...")
    baseline_results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=baseline_factory,
        pair=PAIR,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        overlap_ratio=0.2,
    )

    print("Running MTF-filtered walk-forward...")
    mtf_results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=mtf_factory,
        pair=PAIR,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        overlap_ratio=0.2,
    )

    report = comparison_report(baseline_results, mtf_results)
    print(report)

    agg_b = baseline_results.aggregated
    agg_m = mtf_results.aggregated

    if agg_b and agg_m:
        print(f"Baseline  GO/NO-GO: {'GO' if baseline_results.go_nogo else 'NO-GO'}")
        print(f"MTF-Filter GO/NO-GO: {'GO' if mtf_results.go_nogo else 'NO-GO'}")

        wr_delta = (agg_m.mean_win_rate - agg_b.mean_win_rate) * 100
        pf_delta = agg_m.mean_profit_factor - agg_b.mean_profit_factor
        sr_delta = agg_m.mean_sharpe_ratio - agg_b.mean_sharpe_ratio
        dd_delta = (agg_m.mean_max_drawdown - agg_b.mean_max_drawdown) * 100

        print("\nDelta Summary:")
        print(f"  Win Rate:    {wr_delta:+.2f} pp")
        print(f"  Profit Factor: {pf_delta:+.4f}")
        print(f"  Sharpe:      {sr_delta:+.4f}")
        print(f"  Max DD:      {dd_delta:+.2f} pp")

print("\nDone.")
