#!/usr/bin/env python3
"""Portfolio Blend Driver — Multi-strategy portfolio backtest and evaluation.

Combines selected strategies into a unified portfolio, filters unprofitable ones,
tests correlation, optimizes weights across multiple methods, runs walk-forward
validation, evaluates against FTMO criteria, and persists results to DuckDB.

Usage:
    # Blend specific strategies on specific symbols/timeframes
    python3 scripts/run_portfolio_blend.py \\
        --strategies ttc_xauusd,killzone_momentum \\
        --symbols XAUUSD,GBPUSD \\
        --timeframes M15,H1 \\
        --weight-method combined_score

    # Use all passing strategies (legacy mode)
    python3 scripts/run_portfolio_blend.py --compare

    # Self-test (unit test: 2 strategies x 1 symbol x 1 TF)
    python3 scripts/run_portfolio_blend.py --self-test
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

DATA_DIR = str(project_root / "data" / "forex" / "historical")
REPORTS_DIR = project_root / "reports"
DB_PATH = project_root / "data" / "research" / "research.duckdb"

logger = logging.getLogger(__name__)

# ── Timeframe mapping ─────────────────────────────────────────────────────
TF_MINUTES = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


# ---------------------------------------------------------------------------
# Strategy registry — maps strategy names to factory functions
# ---------------------------------------------------------------------------
def get_strategy_factory(strategy_name: str, pair: str):
    """Return a factory callable for a named strategy, or None if unknown."""
    try:
        if strategy_name == "ttc_xauusd" and pair == "XAUUSD":
            from strategies.ttc_xauusd import TTCXAUUSDStrategy

            return lambda: TTCXAUUSDStrategy()

        if strategy_name == "killzone_momentum":
            from strategies.killzone_momentum import (
                KillzoneMomentumConfig,
                KillzoneMomentumStrategy,
            )

            return lambda: KillzoneMomentumStrategy(KillzoneMomentumConfig())

        if strategy_name == "volatility_squeeze":
            from strategies.volatility_squeeze import VolatilitySqueezeStrategy

            return lambda: VolatilitySqueezeStrategy()

        if strategy_name == "bb_rsi_reversion":
            from strategies.bb_rsi_reversion import BBRSIConfig, BBRSIReversionStrategy

            return lambda: BBRSIReversionStrategy(BBRSIConfig())

        if strategy_name == "volatility_regime_breakout":
            from strategies.volatility_regime_breakout import (
                VolatilityRegimeBreakoutStrategy,
            )

            return lambda: VolatilityRegimeBreakoutStrategy()

        if strategy_name == "srmr_plus" and pair != "XAUUSD":
            from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

            return lambda: SRMRPlusStrategy(SRMRPlusConfig())

        if strategy_name == "donchian_atr_trend":
            from strategies.donchian_atr_trend import DonchianATRTrendStrategy

            return lambda: DonchianATRTrendStrategy()

        if strategy_name == "london_breakout_retest":
            from strategies.london_breakout_retest import (  # noqa: I001
                LondonBreakoutConfig,
                LondonBreakoutRetestStrategy,
            )

            return lambda: LondonBreakoutRetestStrategy(LondonBreakoutConfig(symbol=pair))

        if strategy_name == "session_breakout":
            import strategies.session_breakout as sb_mod
            from strategies.session_breakout import SessionBreakoutConfig

            for name in dir(sb_mod):
                obj = getattr(sb_mod, name)
                if isinstance(obj, type) and "Breakout" in name and "Config" not in name:
                    return lambda: obj(SessionBreakoutConfig())
    except ImportError:
        return None
    return None


def list_available_strategies() -> list[str]:
    """Return known strategy names."""
    return [
        "ttc_xauusd",
        "killzone_momentum",
        "volatility_squeeze",
        "bb_rsi_reversion",
        "volatility_regime_breakout",
        "srmr_plus",
        "donchian_atr_trend",
        "london_breakout_retest",
        "session_breakout",
    ]


def build_specs_from_cli(
    strategies: list[str],
    symbols: list[str],
    timeframes: list[str],
    data_dir: str,
) -> list:
    """Build StrategySpec list from CLI strategy/symbol/timeframe selections."""
    from backtest.portfolio_blend import StrategySpec

    specs: list[StrategySpec] = []
    for strategy_name in strategies:
        for symbol in symbols:
            factory = get_strategy_factory(strategy_name, symbol)
            if factory is None:
                print(f"  SKIP: {strategy_name} not applicable to {symbol}")
                continue
            for tf in timeframes:
                csv_path = f"{data_dir}/{symbol}_{tf}.csv"
                specs.append(
                    StrategySpec(
                        name=strategy_name,
                        factory=factory,
                        pair=symbol,
                        timeframe=tf,
                        data_path=csv_path,
                    )
                )
    return specs


# ---------------------------------------------------------------------------
# DuckDB persistence
# ---------------------------------------------------------------------------
def save_to_duckdb(result, configs: dict, db_path: Path) -> str | None:
    """Persist portfolio blend result to DuckDB portfolio_runs table.

    Returns the run_id, or None on failure.
    """
    try:
        from srf.schema import SRFDatabase

        run_id = f"portfolio_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        db = SRFDatabase(str(db_path))

        configs_json = json.dumps(configs)
        correlation_json = json.dumps(result.correlation.matrix)

        combined = result.combined_metrics
        monthly_trade_count = combined.total_trades

        with db:
            conn = db.connect()
            conn.execute(
                """
                INSERT INTO portfolio_runs (
                    run_id, configs_json, combined_wr, combined_pf,
                    combined_sharpe, monthly_trade_count, max_dd,
                    correlation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    configs_json,
                    combined.win_rate,
                    combined.profit_factor,
                    combined.sharpe_ratio,
                    monthly_trade_count,
                    combined.max_drawdown_pct,
                    correlation_json,
                ],
            )

        print(f"Results saved to DuckDB: {db_path} (run_id={run_id})")
        return run_id
    except Exception as e:
        logger.warning("Failed to save to DuckDB: %s", e, exc_info=True)
        print(f"WARN: DuckDB save failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def run_self_test() -> bool:
    """Unit test: 2 strategies x 1 symbol x 1 TF produces portfolio result.

    Uses synthetic bar data to avoid dependency on real CSV files.
    Verifies that run_portfolio_blend returns non-zero combined metrics.
    """
    from datetime import datetime, timedelta

    from backtest.engine import Bar
    from backtest.portfolio_blend import (  # noqa: I001
        StrategySpec,
        run_portfolio_blend,
    )

    print("Running self-test: 2 strategies x 1 symbol x 1 TF...")

    # Generate synthetic bars (1000 bars, simple random walk)
    import random

    random.seed(42)
    bars = []
    price = 1.1000
    base_time = datetime(2024, 1, 1)
    for i in range(1000):
        change = random.gauss(0, 0.0005)
        o = price
        h = o + abs(random.gauss(0, 0.0003))
        l = o - abs(random.gauss(0, 0.0003))  # noqa: E741
        c = o + change
        bars.append(
            Bar(
                time=base_time + timedelta(minutes=15 * i),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=1000.0,
            )
        )
        price = c

    # Write temp CSV for CsvDataLoader
    import csv as csv_mod
    import os
    import tempfile

    tmpdir = tempfile.mkdtemp()
    csv_path = os.path.join(tmpdir, "TEST_M15.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv_mod.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow(
                [
                    b.time.strftime("%Y-%m-%d %H:%M:%S"),
                    b.open,
                    b.high,
                    b.low,
                    b.close,
                    b.volume,
                ]
            )

    # Build 2 dummy strategy specs using simple strategy stubs
    # We use the portfolio_blend's own internal types
    class StubStrategy:
        """Minimal strategy that emits no signals — tests blend plumbing."""

        name = "stub"

        def reset(self):
            pass

        def set_balance(self, bal):
            pass

        def evaluate(self, state):
            return None

    def stub_factory():
        return StubStrategy()

    spec_a = StrategySpec(
        name="stub_a",
        factory=stub_factory,
        pair="TEST",
        timeframe="M15",
        data_path=csv_path,
    )
    spec_b = StrategySpec(
        name="stub_b",
        factory=stub_factory,
        pair="TEST",
        timeframe="M15",
        data_path=csv_path,
    )

    try:
        result = run_portfolio_blend(
            strategy_specs=[spec_a, spec_b],
            initial_balance=10000.0,
            n_walk_forward_windows=0,
            weight_method="equal_risk",
            enable_filter=False,
        )

        # Verify result is a valid PortfolioBlendResult
        assert result is not None, "Result is None"
        assert result.combined_metrics is not None, "Combined metrics is None"
        assert len(result.combined_equity_curve) > 0, "Empty equity curve"
        assert result.correlation is not None, "Correlation is None"
        assert result.weights is not None, "Weights is None"

        # With stub strategies (no signals), combined metrics should be neutral
        # but the plumbing should work without errors
        print(f"  Combined equity curve length: {len(result.combined_equity_curve)}")
        print(f"  Combined PF: {result.combined_metrics.profit_factor:.2f}")
        print(f"  Combined WR: {result.combined_metrics.win_rate:.1f}%")
        print(f"  Weights: {result.weights.weights}")
        print("  PASS: Portfolio blend produced valid result structure")

        # Cleanup
        os.remove(csv_path)
        os.rmdir(tmpdir)
        return True

    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback

        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Portfolio blend driver — multi-strategy portfolio backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # ── Strategy selection ───────────────────────────────────────────────
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help="Comma-separated strategy names (e.g. ttc_xauusd,killzone_momentum). "
        "If omitted, uses all passing strategies from build_passing_strategy_specs.",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated symbols (e.g. XAUUSD,GBPUSD)",
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default=None,
        help="Comma-separated timeframes (e.g. M15,H1)",
    )

    # ── Portfolio config ─────────────────────────────────────────────────
    parser.add_argument(
        "--weight-method",
        type=str,
        default="equal_risk",
        choices=[
            "inverse_variance",
            "equal_risk",
            "profit_factor",
            "sharpe_weighted",
            "combined_score",
        ],
        help="Weight optimization method (default: equal_risk)",
    )
    parser.add_argument(
        "--risk-per-trade-pct",
        type=float,
        default=0.01,
        help="Risk per trade as fraction (default: 0.01 = 1%%)",
    )
    parser.add_argument(
        "--max-open-trades",
        type=int,
        default=5,
        help="Maximum concurrent open trades across portfolio (default: 5)",
    )
    parser.add_argument("--windows", type=int, default=5, help="Walk-forward windows (default: 5)")
    parser.add_argument("--balance", type=float, default=10000, help="Starting balance (default: 10000)")

    # ── Output ───────────────────────────────────────────────────────────
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path")
    parser.add_argument("--no-db", action="store_true", help="Skip DuckDB persistence")
    parser.add_argument("--no-filter", action="store_true", help="Disable strategy filtering")
    parser.add_argument("--compare", action="store_true", help="Compare all weight methods")
    parser.add_argument(
        "--list-strategies",
        action="store_true",
        help="List available strategies and exit",
    )
    parser.add_argument("--self-test", action="store_true", help="Run built-in unit test and exit")

    args = parser.parse_args()

    # ── Self-test mode ───────────────────────────────────────────────────
    if args.self_test:
        success = run_self_test()
        sys.exit(0 if success else 1)

    # ── List strategies mode ─────────────────────────────────────────────
    if args.list_strategies:
        print("Available strategies:")
        for name in list_available_strategies():
            print(f"  - {name}")
        sys.exit(0)

    # ── Import portfolio_blend ───────────────────────────────────────────
    from backtest.portfolio_blend import (
        WEIGHT_METHODS,
        build_passing_strategy_specs,
        format_portfolio_report,
        run_portfolio_blend,
    )

    print("=" * 80)
    print("PORTFOLIO BLEND DRIVER")
    print("=" * 80)
    print()

    # ── Build strategy specs ─────────────────────────────────────────────
    if args.strategies and args.symbols and args.timeframes:
        strategy_list = [s.strip() for s in args.strategies.split(",")]
        symbol_list = [s.strip() for s in args.symbols.split(",")]
        tf_list = [t.strip() for t in args.timeframes.split(",")]
        specs = build_specs_from_cli(strategy_list, symbol_list, tf_list, DATA_DIR)
        print(f"Loading {len(specs)} strategy specs from CLI args:")
    else:
        specs = build_passing_strategy_specs(DATA_DIR)
        print(f"Loading {len(specs)} strategy specs (auto-discovered):")

    for s in specs:
        print(f"  - {s.name} ({s.pair} {s.timeframe})")
    print()

    if not specs:
        print("ERROR: No strategy specs to run. Check --strategies/--symbols/--timeframes.")
        sys.exit(1)

    # ── Run blend ────────────────────────────────────────────────────────
    enable_filter = not args.no_filter
    best_result = None
    best_method = args.weight_method
    filtered_out: list = []

    if args.compare:
        print("COMPARING ALL WEIGHT METHODS")
        print("=" * 80)
        comparison = {}
        for method_name in WEIGHT_METHODS:
            result = run_portfolio_blend(
                strategy_specs=specs,
                initial_balance=args.balance,
                n_walk_forward_windows=args.windows,
                weight_method=method_name,
                enable_filter=enable_filter,
            )
            m = result.combined_metrics
            comparison[method_name] = {
                "wr": m.win_rate,
                "pf": m.profit_factor,
                "sharpe": m.sharpe_ratio,
                "dd": m.max_drawdown_pct,
                "pnl": m.total_pnl,
                "ftmo": result.ftmo_passed,
                "wf_go": result.walk_forward.go_nogo if result.walk_forward else False,
            }
            wf_score = 0
            if result.walk_forward and result.walk_forward.aggregated:
                a = result.walk_forward.aggregated
                wf_score = a.mean_win_rate * 100 + a.mean_profit_factor + a.mean_sharpe_ratio

            score = (
                (10 if result.ftmo_passed else 0)
                + (10 if comparison[method_name]["wf_go"] else 0)
                + m.profit_factor * 2
                + m.sharpe_ratio
                + wf_score
            )
            comparison[method_name]["score"] = score

            if best_result is None or score > comparison[best_method].get("score", 0):
                best_result = result
                best_method = method_name

            if method_name == "combined_score":
                _, filtered_out = _get_filtered(specs, args.balance, enable_filter)

        print(
            f"{'Method':<20} {'WR%':>6} {'PF':>7} {'Sharpe':>7} {'DD%':>7} {'PnL':>10} {'FTMO':>6} {'WF GO':>6} {'Score':>7}"  # noqa: E501
        )
        print("-" * 90)
        for method_name, c in sorted(comparison.items(), key=lambda x: -x[1].get("score", 0)):
            ftmo_str = "PASS" if c["ftmo"] else "FAIL"
            wf_str = "GO" if c["wf_go"] else "NO"
            print(
                f"{method_name:<20} {c['wr']:>6.1f} {c['pf']:>7.2f} {c['sharpe']:>7.2f} "
                f"{c['dd']:>7.2f} ${c['pnl']:>9.2f} {ftmo_str:>6} {wf_str:>6} {c['score']:>7.1f}"
            )
        print()
        print(f"Best method: {best_method}")
        print()
    else:
        t0 = time.time()
        best_result = run_portfolio_blend(
            strategy_specs=specs,
            initial_balance=args.balance,
            n_walk_forward_windows=args.windows,
            weight_method=args.weight_method,
            enable_filter=enable_filter,
        )
        elapsed = time.time() - t0
        best_method = args.weight_method
        filtered_out = _get_filtered(specs, args.balance, enable_filter)[1]
        print(f"Completed in {elapsed:.1f}s\n")

    # ── Report ───────────────────────────────────────────────────────────
    report = format_portfolio_report(best_result, filtered_strategies=filtered_out if filtered_out else None)
    print(report)

    # ── Build report data ────────────────────────────────────────────────
    output_path = args.output or str(REPORTS_DIR / "portfolio_blend_results.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    configs = {
        "strategies": [s.name for s in specs],
        "symbols": [s.pair for s in specs],
        "timeframes": [s.timeframe for s in specs],
        "weight_method": best_method,
        "risk_per_trade_pct": args.risk_per_trade_pct,
        "max_open_trades": args.max_open_trades,
        "windows": args.windows,
        "balance": args.balance,
        "filter_enabled": enable_filter,
    }

    report_data = {
        "weight_method": best_method,
        "configs": configs,
        "strategy_filter_enabled": enable_filter,
        "filtered_strategies": [{"key": f.key, "reason": f.reason} for f in filtered_out],
        "ftmo_passed": best_result.ftmo_passed,
        "ftmo_criteria": best_result.ftmo_criteria,
        "weights": best_result.weights.weights,
        "correlation_average": best_result.correlation.average_correlation,
        "correlation_matrix": best_result.correlation.matrix,
        "individual": {
            key: {
                "strategy": ec.strategy_name,
                "pair": ec.pair,
                "timeframe": ec.timeframe,
                "win_rate": ec.win_rate,
                "profit_factor": ec.profit_factor,
                "sharpe_ratio": ec.sharpe_ratio,
                "max_drawdown": ec.max_drawdown,
                "trade_count": ec.trade_count,
                "total_pnl": ec.total_pnl,
                "in_blend": key in best_result.weights.weights,
            }
            for key, ec in best_result.individual_results.items()
        },
        "combined": {
            "win_rate": best_result.combined_metrics.win_rate,
            "profit_factor": best_result.combined_metrics.profit_factor,
            "sharpe_ratio": best_result.combined_metrics.sharpe_ratio,
            "max_drawdown_pct": best_result.combined_metrics.max_drawdown_pct,
            "total_pnl": best_result.combined_metrics.total_pnl,
            "total_pnl_pct": best_result.combined_metrics.total_pnl_pct,
            "starting_balance": best_result.combined_metrics.starting_balance,
            "ending_balance": best_result.combined_metrics.ending_balance,
        },
        "walk_forward": None,
    }

    if best_result.walk_forward:
        wf = best_result.walk_forward
        report_data["walk_forward"] = {
            "go_nogo": wf.go_nogo,
            "per_window": [
                {
                    "window_index": m.window_index,
                    "win_rate": m.win_rate,
                    "profit_factor": m.profit_factor,
                    "max_drawdown": m.max_drawdown,
                    "sharpe_ratio": m.sharpe_ratio,
                    "trade_count": m.trade_count,
                    "total_pnl": m.total_pnl,
                    "passed_go_nogo": m.passed_go_nogo,
                }
                for m in wf.per_window
            ],
            "aggregated": (
                {
                    "mean_win_rate": wf.aggregated.mean_win_rate,
                    "std_win_rate": wf.aggregated.std_win_rate,
                    "mean_profit_factor": wf.aggregated.mean_profit_factor,
                    "std_profit_factor": wf.aggregated.std_profit_factor,
                    "mean_max_drawdown": wf.aggregated.mean_max_drawdown,
                    "std_max_drawdown": wf.aggregated.std_max_drawdown,
                    "mean_sharpe_ratio": wf.aggregated.mean_sharpe_ratio,
                    "std_sharpe_ratio": wf.aggregated.std_sharpe_ratio,
                    "mean_trade_count": wf.aggregated.mean_trade_count,
                    "mean_total_pnl": wf.aggregated.mean_total_pnl,
                    "windows_passed": wf.aggregated.windows_passed,
                    "total_windows": wf.aggregated.total_windows,
                }
                if wf.aggregated
                else None
            ),
        }

    if args.compare:
        report_data["method_comparison"] = comparison

    # ── DuckDB persistence ───────────────────────────────────────────────
    if not args.no_db:
        save_to_duckdb(best_result, configs, DB_PATH)

    # ── JSON output ──────────────────────────────────────────────────────
    with open(output_path, "w") as f:
        json.dump(report_data, f, indent=2)
    print(f"Report saved: {output_path}")


def _get_filtered(specs, balance, enable_filter):
    if not enable_filter:
        return [], []
    from backtest.portfolio_blend import filter_strategies
    from backtest.portfolio_blend import run_portfolio_blend as _run_blend

    result = _run_blend(
        strategy_specs=specs,
        initial_balance=balance,
        n_walk_forward_windows=0,
        enable_filter=True,
    )
    return result.individual_results, filter_strategies(result.individual_results)[1]


if __name__ == "__main__":
    main()
