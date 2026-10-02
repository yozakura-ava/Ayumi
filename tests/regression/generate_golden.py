"""Generate golden reference metrics using the v2 backtest engine.

Run once to produce pickle files consumed by the regression test suite.
Usage: PYTHONPATH=src python tests/regression/generate_golden.py
"""

from __future__ import annotations

import pickle

import numpy as np
import pandas as pd
from _project_root import PROJECT_ROOT
from forex_trading.services.backtest.engine_v2 import (
    BacktestEngine,
    WalkForwardConfig,
)
from forex_trading.services.backtest.prop_firm_rules import PropFirmConfig
from forex_trading.strategies.breakout import BreakoutStrategy
from forex_trading.strategies.carry import CarryTradeStrategy
from forex_trading.strategies.mean_reversion import MeanReversionStrategy
from forex_trading.strategies.momentum import MomentumCrossoverStrategy
from forex_trading.strategies.regime_aware import (
    RegimeAwareStrategy,
    RegimeClassifier,
)
from forex_trading.strategies.regime_switching_momentum import (
    RegimeSwitchingMomentumStrategy,
)

PAIRS = ["EURUSD", "GBPUSD"]
OUTPUT_DIR = PROJECT_ROOT / "tests" / "regression"

PROP_FIRM_CONFIG = PropFirmConfig(
    max_daily_drawdown_pct=0.05,
    max_total_drawdown_pct=0.10,
    profit_target_pct=0.10,
    max_concurrent_positions=2,
    max_daily_trades=10,
)

WF_CONFIG = WalkForwardConfig(
    train_bars=378,
    test_bars=756,
    step_bars=126,
)


def get_strategies(data: pd.DataFrame) -> list:
    regime = RegimeClassifier.classify_regime(data)
    return [
        MomentumCrossoverStrategy(),
        MeanReversionStrategy(),
        BreakoutStrategy(),
        RegimeAwareStrategy(regime=regime),
        CarryTradeStrategy(),
        RegimeSwitchingMomentumStrategy(),
    ]


def extract_metrics(metrics) -> dict:
    return {
        "total_return": metrics.total_return,
        "sharpe_ratio": metrics.sharpe_ratio,
        "max_drawdown": metrics.max_drawdown,
        "max_drawdown_duration": metrics.max_drawdown_duration,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "total_trades": metrics.total_trades,
        "avg_trade_duration": metrics.avg_trade_duration,
    }


def generate_for_pair(pair: str) -> dict:
    data_path = PROJECT_ROOT / "data" / f"{pair}_1h.parquet"
    print(f"Loading {pair} H1 data from {data_path}...")
    df = pd.read_parquet(data_path)
    print(f"  {len(df)} bars loaded")

    engine = BacktestEngine(
        starting_balance=10_000.0,
        prop_firm_config=PROP_FIRM_CONFIG,
    )

    strategies = get_strategies(df)
    golden: dict[str, dict] = {}

    for strategy in strategies:
        name = strategy.name
        print(f"  Strategy: {name}")

        try:
            single_result = engine.run_single(strategy, df, pair)
            single_metrics = extract_metrics(single_result)
        except Exception as e:
            print(f"    Single-pass FAILED: {e}")
            single_metrics = {"error": str(e)}

        try:
            wf_results = engine.run_walk_forward(
                strategy=strategy,
                bars=df,
                pair=pair,
                wf_config=WF_CONFIG,
            )

            oos_list = [m for m in wf_results.oos_results if m is not None]
            window_metrics = [extract_metrics(m) for m in oos_list]

            avg_oos = {}
            if oos_list:
                for key in [
                    "total_return",
                    "sharpe_ratio",
                    "max_drawdown",
                    "max_drawdown_duration",
                    "win_rate",
                    "profit_factor",
                    "total_trades",
                    "avg_trade_duration",
                ]:
                    vals = [w[key] for w in window_metrics]
                    avg_oos[key] = float(np.mean(vals))

            wf_data = {
                "avg_oos": avg_oos,
                "windows": window_metrics,
                "n_windows": len(oos_list),
            }
        except Exception as e:
            print(f"    Walk-forward FAILED: {e}")
            wf_data = {"error": str(e), "n_windows": 0}

        golden[name] = {
            "single": single_metrics,
            "walk_forward": wf_data,
        }

    return golden


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for pair in PAIRS:
        print(f"\n{'=' * 60}")
        print(f"  Generating golden dataset: {pair} H1")
        print(f"{'=' * 60}")

        golden = generate_for_pair(pair)

        out_path = OUTPUT_DIR / f"golden_{pair.lower()}_h1.pkl"
        with open(out_path, "wb") as f:
            pickle.dump(golden, f)

        print(f"  Saved to {out_path}")
        n_strategies = len(golden)
        n_errors = sum(
            1 for v in golden.values() if "error" in v.get("single", {}) or "error" in v.get("walk_forward", {})
        )
        print(f"  {n_strategies} strategies, {n_errors} errors")


if __name__ == "__main__":
    main()
