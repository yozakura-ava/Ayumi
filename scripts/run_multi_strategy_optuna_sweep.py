#!/usr/bin/env python3
"""
Multi-Strategy Optuna Sweep — AYUAA-793

Runs Optuna optimization on all validated strategies across pairs and timeframes.
Produces ranked list of signal components ready for forward testing.

Usage:
    python scripts/run_multi_strategy_optuna_sweep.py --strategy SESSION_RANGE_MR --pair GBPUSD --timeframe H1 --trials 100
"""  # noqa: E501

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.resource_limits import add_resource_args

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from backtest import CsvDataLoader
from backtest.engine import Bar
from backtest.parameter_sweep.optuna_optimizer import (  # noqa: I001
    OptimizationResult,
    OptunaOptimizer,
    SearchSpace,
    categorical,
    float_range,
    int_range,
)
from backtest.walk_forward_runner import run_strategy_walk_forward

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path("data/forex/historical")
REPORT_DIR = Path("reports/optuna")

DEFAULT_WF_CONFIG = {
    "n_windows": 5,
    "train_ratio": 0.70,
    "val_ratio": 0.15,
    "overlap_ratio": 0.20,
    "initial_balance": 10000.0,
    "spread_pips": 0.0,
    "commission_per_lot": 3.5,
}

SEARCH_SPACES = {}


def session_range_mr_search_space() -> SearchSpace:
    return SearchSpace(
        atr_period=int_range("atr_period", 8, 30),
        atr_sl_multiplier=float_range("atr_sl_multiplier", 0.5, 3.0, step=0.1),
        atr_tp_multiplier=float_range("atr_tp_multiplier", 1.0, 4.0, step=0.1),
        rsi_period=int_range("rsi_period", 8, 28),
        rsi_long_level=float_range("rsi_long_level", 20.0, 40.0, step=1.0),
        rsi_short_level=float_range("rsi_short_level", 60.0, 80.0, step=1.0),
        session_range_min_pips=float_range("session_range_min_pips", 10.0, 50.0, step=5.0),
        entry_near_extreme_pips=float_range("entry_near_extreme_pips", 5.0, 30.0, step=1.0),
        hard_cap_sl_pips=float_range("hard_cap_sl_pips", 15.0, 50.0, step=5.0),
        tp1_rr=float_range("tp1_rr", 0.5, 2.0, step=0.1),
        tp2_rr=float_range("tp2_rr", 1.0, 3.0, step=0.1),
        ema_trend_period=int_range("ema_trend_period", 20, 100),
        session_range_sl_fraction=float_range("session_range_sl_fraction", 0.3, 0.9, step=0.05),
    )


def volatility_squeeze_search_space() -> SearchSpace:
    return SearchSpace(
        bb_period=int_range("bb_period", 14, 30),
        bb_std_dev=float_range("bb_std_dev", 1.5, 2.5, step=0.1),
        kc_period=int_range("kc_period", 14, 30),
        kc_atr_multiplier=float_range("kc_atr_multiplier", 1.0, 2.5, step=0.1),
        min_squeeze_bars=int_range("min_squeeze_bars", 2, 6),
        ema_period=int_range("ema_period", 20, 80),
        adx_period=int_range("adx_period", 14, 25),
        adx_min=float_range("adx_min", 15.0, 30.0, step=1.0),
        atr_period=int_range("atr_period", 10, 25),
        atr_sl_multiplier=float_range("atr_sl_multiplier", 1.0, 2.5, step=0.1),
        tp1_rr=float_range("tp1_rr", 1.0, 2.5, step=0.1),
        tp2_rr=float_range("tp2_rr", 1.5, 3.5, step=0.1),
        tp3_rr=float_range("tp3_rr", 2.0, 4.0, step=0.2),
        session_filter=categorical("session_filter", [True, False]),
        min_confidence=float_range("min_confidence", 0.40, 0.70, step=0.05),
        squeeze_release_mode=categorical("squeeze_release_mode", ["strict", "moderate", "loose"]),
    )


def supertrend_rsi_search_space() -> SearchSpace:
    return SearchSpace(
        supertrend_period=int_range("supertrend_period", 10, 25),
        supertrend_multiplier=float_range("supertrend_multiplier", 1.5, 3.5, step=0.1),
        rsi_period=int_range("rsi_period", 8, 25),
        rsi_threshold=float_range("rsi_threshold", 45.0, 55.0, step=1.0),
        atr_min_pips=float_range("atr_min_pips", 2.0, 8.0, step=0.5),
        atr_period=int_range("atr_period", 10, 25),
        adx_period=int_range("adx_period", 14, 25),
        adx_min=float_range("adx_min", 15.0, 25.0, step=1.0),
        sl_atr_multiplier=float_range("sl_atr_multiplier", 1.0, 2.5, step=0.1),
        hard_cap_pips=float_range("hard_cap_pips", 30.0, 60.0, step=5.0),
        tp1_atr=float_range("tp1_atr", 1.0, 2.5, step=0.1),
        tp2_atr=float_range("tp2_atr", 2.0, 4.0, step=0.1),
    )


def keltner_search_space() -> SearchSpace:
    return SearchSpace(
        ema_period=int_range("ema_period", 15, 40),
        atr_period=int_range("atr_period", 10, 25),
        atr_multiplier=float_range("atr_multiplier", 1.0, 2.5, step=0.1),
        atr_min_pips=float_range("atr_min_pips", 1.0, 10.0, step=0.5),
        adx_period=int_range("adx_period", 14, 25),
        adx_threshold=float_range("adx_threshold", 10.0, 25.0, step=1.0),
        sl_atr_multiplier=float_range("sl_atr_multiplier", 1.0, 2.5, step=0.1),
        sl_max_pips=float_range("sl_max_pips", 25.0, 60.0, step=5.0),
        tp1_atr_multiplier=float_range("tp1_atr_multiplier", 1.5, 3.5, step=0.1),
        tp2_atr_multiplier=float_range("tp2_atr_multiplier", 2.5, 4.5, step=0.1),
    )


def bb_rsi_search_space() -> SearchSpace:
    return SearchSpace(
        bb_period=int_range("bb_period", 14, 30),
        bb_std_dev=float_range("bb_std_dev", 1.5, 2.5, step=0.1),
        rsi_period=int_range("rsi_period", 8, 25),
        rsi_long_level=float_range("rsi_long_level", 20.0, 40.0, step=1.0),
        rsi_short_level=float_range("rsi_short_level", 60.0, 80.0, step=1.0),
        atr_period=int_range("atr_period", 10, 25),
        atr_sl_multiplier=float_range("atr_sl_multiplier", 0.8, 3.0, step=0.1),
        tp1_rr=float_range("tp1_rr", 0.5, 2.0, step=0.1),
        tp2_rr=float_range("tp2_rr", 1.0, 3.0, step=0.1),
        ema_trend_period=int_range("ema_trend_period", 20, 80),
        adx_period=int_range("adx_period", 10, 25),
        adx_max_threshold=float_range("adx_max_threshold", 15.0, 35.0, step=1.0),
        require_low_volatility=categorical("require_low_volatility", [True, False]),
    )


SEARCH_SPACES = {
    "SESSION_RANGE_MR": session_range_mr_search_space,
    "VOLATILITY_SQUEEZE": volatility_squeeze_search_space,
    "SUPERTREND_RSI": supertrend_rsi_search_space,
    "KELTNER": keltner_search_space,
    "BB_RSI": bb_rsi_search_space,
}


STRATEGY_FACTORIES = {}


def _make_srm_factory():
    from strategies.session_range_mean_reversion import (
        SessionRangeMeanReversionStrategy,
        SessionRangeMRConfig,
    )

    def factory(params: dict[str, Any]):
        config = SessionRangeMRConfig(**params)
        return SessionRangeMeanReversionStrategy(config=config)

    return factory


def _make_volatility_squeeze_factory():
    from strategies.volatility_squeeze import (  # noqa: I001
        VolatilitySqueezeConfig,
        VolatilitySqueezeStrategy,
    )

    def factory(params: dict[str, Any]):
        config = VolatilitySqueezeConfig(**params)
        return VolatilitySqueezeStrategy(config=config)

    return factory


def _make_supertrend_rsi_factory():
    from backtest.strategies import SupertrendRSIBlendStrategy

    def factory(params: dict[str, Any]):
        return SupertrendRSIBlendStrategy(**params)

    return factory


def _make_keltner_factory():
    from backtest.strategies import KeltnerChannelBreakoutStrategy

    def factory(params: dict[str, Any]):
        return KeltnerChannelBreakoutStrategy(**params)

    return factory


def _make_bb_rsi_factory():
    from strategies.bb_rsi_reversion import BBRSIConfig, BBRSIMeanReversion

    def factory(params: dict[str, Any]):
        config = BBRSIConfig(**params)
        return BBRSIMeanReversion(config=config)

    return factory


STRATEGY_FACTORIES = {
    "SESSION_RANGE_MR": _make_srm_factory(),
    "VOLATILITY_SQUEEZE": _make_volatility_squeeze_factory(),
    "SUPERTREND_RSI": _make_supertrend_rsi_factory(),
    "KELTNER": _make_keltner_factory(),
    "BB_RSI": _make_bb_rsi_factory(),
}


def load_bars(pair: str, timeframe: str) -> list[Bar]:
    csv_path = DATA_DIR / f"{pair}_{timeframe}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Data not found: {csv_path}")
    loader = CsvDataLoader()
    bars = loader.load(str(csv_path))
    if not bars:
        raise ValueError(f"No bars loaded from {csv_path}")
    logger.info(f"Loaded {len(bars)} bars for {pair} {timeframe}: {bars[0].time} -> {bars[-1].time}")
    return bars


def run_baseline(bars: list[Bar], strategy_name: str, pair: str) -> Any:

    factory = STRATEGY_FACTORIES[strategy_name]
    strategy = factory({})

    result = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=lambda: strategy,
        pair=pair,
        **DEFAULT_WF_CONFIG,
    )
    return result


def run_optuna(
    bars: list[Bar],
    strategy_name: str,
    pair: str,
    n_trials: int = 100,
    seed: int = 42,
) -> OptimizationResult:
    search_space = SEARCH_SPACES[strategy_name]()
    factory = STRATEGY_FACTORIES[strategy_name]

    optimizer = OptunaOptimizer(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        search_space=search_space,
        n_trials=n_trials,
        seed=seed,
        **DEFAULT_WF_CONFIG,
    )
    return optimizer.optimize()


def save_report(
    strategy: str,
    pair: str,
    timeframe: str,
    baseline: Any,
    optuna_result: OptimizationResult,
    n_trials: int,
    seed: int,
):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{pair}_{timeframe}_{strategy.lower()}_optuna.json"

    report = {
        "strategy": strategy,
        "pair": pair,
        "timeframe": timeframe,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_trials": n_trials,
        "seed": seed,
        "baseline": {
            "go_nogo": baseline.go_nogo,
            "windows_passed": sum(1 for m in baseline.per_window if m.passed_go_nogo),
            "total_windows": len(baseline.per_window),
        },
        "optuna": {
            "best_params": optuna_result.best_params,
            "best_score": optuna_result.best_value,
            "go_nogo": optuna_result.go_nogo,
            "n_trials": optuna_result.n_trials,
            "study_summary": optuna_result.study_summary,
        },
    }

    if baseline.aggregated:
        agg = baseline.aggregated
        report["baseline"]["metrics"] = {
            "win_rate": agg.mean_win_rate,
            "profit_factor": agg.mean_profit_factor,
            "max_drawdown": agg.mean_max_drawdown,
            "sharpe_ratio": agg.mean_sharpe_ratio,
            "trade_count": agg.mean_trade_count,
            "total_pnl": agg.mean_total_pnl,
        }

    if optuna_result.best_walk_forward and optuna_result.best_walk_forward.aggregated:
        agg = optuna_result.best_walk_forward.aggregated
        report["optuna"]["metrics"] = {
            "win_rate": agg.mean_win_rate,
            "profit_factor": agg.mean_profit_factor,
            "max_drawdown": agg.mean_max_drawdown,
            "sharpe_ratio": agg.mean_sharpe_ratio,
            "trade_count": agg.mean_trade_count,
            "total_pnl": agg.mean_total_pnl,
            "windows_passed": agg.windows_passed,
            "total_windows": agg.total_windows,
        }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info(f"Report saved: {report_path}")
    return report


def print_results(
    strategy: str,
    pair: str,
    timeframe: str,
    baseline: Any,
    optuna_result: OptimizationResult,
):
    print(f"\n{'=' * 70}")
    print(f"  {strategy} — {pair} {timeframe}")
    print(f"{'=' * 70}")

    print("\n  BASELINE:")
    if baseline.aggregated:
        agg = baseline.aggregated
        print(f"    Win Rate:     {agg.mean_win_rate:.1%}")
        print(f"    Profit Factor: {agg.mean_profit_factor:.2f}")
        print(f"    Max Drawdown:  {agg.mean_max_drawdown:.1%}")
        print(f"    Sharpe Ratio:  {agg.mean_sharpe_ratio:.2f}")
        print(f"    Trade Count:   {agg.mean_trade_count:.1f}")
        print(
            f"    Windows Pass:  {sum(1 for m in baseline.per_window if m.passed_go_nogo)}/{len(baseline.per_window)}"
        )
        print(f"    GO/NO-GO:      {'GO' if baseline.go_nogo else 'NO-GO'}")

    print(f"\n  OPTUNA ({optuna_result.n_trials} trials):")
    print(f"    Best score:    {optuna_result.best_value:.4f}")
    print(f"    GO/NO-GO:      {'GO' if optuna_result.go_nogo else 'NO-GO'}")
    if optuna_result.best_walk_forward and optuna_result.best_walk_forward.aggregated:
        agg = optuna_result.best_walk_forward.aggregated
        print(f"    Win Rate:      {agg.mean_win_rate:.1%}")
        print(f"    Profit Factor: {agg.mean_profit_factor:.2f}")
        print(f"    Max Drawdown:  {agg.mean_max_drawdown:.1%}")
        print(f"    Sharpe Ratio:  {agg.mean_sharpe_ratio:.2f}")
        print(f"    Trade Count:   {agg.mean_trade_count:.1f}")
        print(f"    Windows Pass:  {agg.windows_passed}/{agg.total_windows}")

    print("\n  Best parameters:")
    for key, value in sorted(optuna_result.best_params.items()):
        print(f"    {key}: {value}")


def main():
    parser = argparse.ArgumentParser(description="Multi-strategy Optuna sweep")
    add_resource_args(parser)
    parser.add_argument("--strategy", required=True, choices=list(SEARCH_SPACES.keys()))
    parser.add_argument("--pair", required=True, help="Currency pair (e.g. GBPUSD)")
    parser.add_argument("--timeframe", required=True, help="Timeframe (e.g. H1, M15)")
    parser.add_argument("--trials", type=int, default=100, help="Number of Optuna trials")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    logger.info(f"Starting Optuna sweep: {args.strategy} {args.pair} {args.timeframe} ({args.trials} trials)")

    bars = load_bars(args.pair, args.timeframe)

    logger.info("Running baseline...")
    baseline = run_baseline(bars, args.strategy, args.pair)

    logger.info("Running Optuna optimization...")
    optuna_result = run_optuna(bars, args.strategy, args.pair, n_trials=args.trials, seed=args.seed)

    report = save_report(
        args.strategy,
        args.pair,
        args.timeframe,
        baseline,
        optuna_result,
        args.trials,
        args.seed,
    )
    print_results(args.strategy, args.pair, args.timeframe, baseline, optuna_result)

    return report


if __name__ == "__main__":
    main()
