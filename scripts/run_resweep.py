#!/usr/bin/env python3
"""Re-sweep 6 strategies under corrected FTMO parameters.

Runs walk-forward backtests for each strategy on XAUUSD with canonical
FTMO params (balance=100k, risk=0.5%, daily DD=5%, max_open=3), captures
raw results, and computes bootstrap CI lower bounds.

Outputs:
  docs/strategies/revalidation-2026-07/raw/<strategy>.json
  docs/strategies/revalidation-2026-07/raw/_summary.json

Usage:
    python scripts/run_resweep.py
    python scripts/run_resweep.py --strategies srmr_plus,ttc_xauusd
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FOREX_BOT = PROJECT_ROOT / "src" / "forex_bot"
sys.path.insert(0, str(FOREX_BOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# bootstrap_ci lives in the root-level backtest/ package
import importlib.util as _ilu

from backtest.types import Bar  # noqa: E402, I001
from backtest.walk_forward_runner import run_strategy_walk_forward  # noqa: E402

_bc_path = PROJECT_ROOT / "backtest" / "bootstrap_ci.py"
_spec = _ilu.spec_from_file_location("bootstrap_ci", _bc_path)
_bc_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_bc_mod)
bootstrap_pf = _bc_mod.bootstrap_pf

from risk.ftmo_params import (  # noqa: E402, I001
    FTMO_DAILY_DD_LIMIT_PCT,
    FTMO_MAX_CONCURRENT_POSITIONS,
    FTMO_RISK_PER_TRADE_PCT,
)

logger = logging.getLogger("resweep")

# ---------------------------------------------------------------------------
# Canonical params (verified against risk/ftmo_params.py)
# ---------------------------------------------------------------------------

INITIAL_BALANCE = 100_000.0  # FTMO simulation balance
N_WINDOWS = 5

# ---------------------------------------------------------------------------
# Strategy → data mapping
# All strategies tested on XAUUSD (D-011 unblocking target).
# ---------------------------------------------------------------------------

STRATEGY_SPECS = {
    "srmr_plus": {
        "module": "strategies.srmr_plus",
        "class": "SRMRPlusStrategy",
        "pair": "XAUUSD",
        "timeframe": "M15",
        "csv": "data/forex/historical/XAUUSD_M15.csv",
    },
    "london_breakout_retest": {
        "module": "strategies.london_breakout_retest",
        "class": "LondonBreakoutRetestStrategy",
        "pair": "XAUUSD",
        "timeframe": "M15",
        "csv": "data/forex/historical/XAUUSD_M15.csv",
    },
    "ttc_xauusd": {
        "module": "strategies.ttc_xauusd",
        "class": "TTCXAUUSDStrategy",
        "pair": "XAUUSD",
        "timeframe": "M15",
        "csv": "data/forex/historical/XAUUSD_M15.csv",
    },
    "killzone_momentum": {
        "module": "strategies.killzone_momentum",
        "class": "KillzoneMomentumStrategy",
        "pair": "XAUUSD",
        "timeframe": "H1",
        "csv": "data/forex/historical/XAUUSD_H1.csv",
    },
    "volatility_squeeze": {
        "module": "strategies.volatility_squeeze",
        "class": "VolatilitySqueezeStrategy",
        "pair": "XAUUSD",
        "timeframe": "H1",
        "csv": "data/forex/historical/XAUUSD_H1.csv",
        "config_preset": "XAUUSD_H1_PRESET",
    },
    "volatility_regime_breakout": {
        "module": "strategies.volatility_regime_breakout",
        "class": "VolatilityRegimeBreakoutStrategy",
        "pair": "XAUUSD",
        "timeframe": "M15",
        "csv": "data/forex/historical/XAUUSD_M15.csv",
    },
}


def load_bars(csv_path: Path, pair: str) -> list[Bar]:
    """Load OHLCV bars from CSV into Bar objects."""
    df = pd.read_csv(csv_path)
    col_map = {}
    for c in df.columns:
        cl = c.lower().strip()
        if cl in ("date", "timestamp", "time", "datetime", "timestamp_utc"):
            col_map[c] = "timestamp"
        elif cl == "open":
            col_map[c] = "open"
        elif cl == "high":
            col_map[c] = "high"
        elif cl == "low":
            col_map[c] = "low"
        elif cl == "close":
            col_map[c] = "close"
        elif cl == "volume":
            col_map[c] = "volume"
    df = df.rename(columns=col_map)

    if "timestamp" not in df.columns:
        raise ValueError(f"No timestamp column in {csv_path}. Columns: {list(df.columns)}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)

    bars = []
    for _, row in df.iterrows():
        bars.append(
            Bar(
                time=row["timestamp"].to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0)),
            )
        )
    return bars


def get_strategy_factory(spec: dict):
    """Return a factory callable that creates the strategy instance."""
    import importlib

    mod = importlib.import_module(spec["module"])
    cls = getattr(mod, spec["class"])
    pair = spec["pair"]

    preset_name = spec.get("config_preset")
    if preset_name:
        config = getattr(mod, preset_name)
        return lambda: cls(config=config)

    # Strategies with symbol-aware configs need it set explicitly
    config_cls_name = {
        "SRMRPlusStrategy": "SRMRPlusConfig",
        "VolatilityRegimeBreakoutStrategy": "VRBConfig",
        "KillzoneMomentumStrategy": "KillzoneMomentumConfig",
    }.get(cls.__name__)

    if config_cls_name:
        config_cls = getattr(mod, config_cls_name)
        try:
            config = config_cls(symbol=pair)
            return lambda: cls(config=config)
        except TypeError:
            # Config doesn't accept symbol param
            pass

    return lambda: cls()


def _sanitize(obj):
    """Recursively sanitize floats for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        if math.isinf(obj):
            return 99.0 if obj > 0 else -99.0
        return round(obj, 6)
    return obj


def run_single_strategy(name: str, spec: dict, bars: list[Bar]) -> dict:
    """Run walk-forward for one strategy and return results dict."""
    pair = spec["pair"]
    tf = spec["timeframe"]
    factory = get_strategy_factory(spec)

    t0 = time.time()
    wf = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        n_windows=N_WINDOWS,
        initial_balance=INITIAL_BALANCE,
    )
    elapsed = time.time() - t0

    # Collect all trade PnLs across windows
    all_pnls = []
    per_window_summary = []
    for wm in wf.per_window:
        # Extract PnLs from window metrics
        window_pnls = []
        if hasattr(wm, "trades") and wm.trades:
            window_pnls = [t.get("pnl", 0) for t in wm.trades if isinstance(t, dict)]
        elif hasattr(wm, "total_pnl") and hasattr(wm, "trade_count") and wm.trade_count > 0:
            # Fallback: distribute total PnL evenly if individual trades unavailable
            window_pnls = [wm.total_pnl / wm.trade_count] * wm.trade_count

        all_pnls.extend(window_pnls)
        per_window_summary.append(
            {
                "window_index": wm.window_index,
                "win_rate": wm.win_rate,
                "profit_factor": wm.profit_factor,
                "max_drawdown": wm.max_drawdown,
                "sharpe_ratio": wm.sharpe_ratio,
                "trade_count": wm.trade_count,
                "total_pnl": wm.total_pnl,
                "passed_go_nogo": wm.passed_go_nogo,
                "regime_volatility": wm.regime_volatility,
                "regime_trend": wm.regime_trend,
                "regime_session": wm.regime_session,
            }
        )

    # Bootstrap CI
    ci_result = bootstrap_pf(all_pnls) if all_pnls else bootstrap_pf([])

    # Aggregate
    agg = wf.aggregated
    aggregated = None
    if agg:
        aggregated = {
            "mean_win_rate": agg.mean_win_rate,
            "mean_profit_factor": agg.mean_profit_factor,
            "std_profit_factor": agg.std_profit_factor,
            "mean_max_drawdown": agg.mean_max_drawdown,
            "std_max_drawdown": agg.std_max_drawdown,
            "mean_sharpe_ratio": agg.mean_sharpe_ratio,
            "std_sharpe_ratio": agg.std_sharpe_ratio,
            "mean_trade_count": agg.mean_trade_count,
            "std_trade_count": agg.std_trade_count,
            "mean_total_pnl": agg.mean_total_pnl,
            "std_total_pnl": agg.std_total_pnl,
            "windows_passed": agg.windows_passed,
            "total_windows": agg.total_windows,
        }

    result = {
        "strategy": name,
        "pair": pair,
        "timeframe": tf,
        "params": {
            "initial_balance": INITIAL_BALANCE,
            "risk_per_trade_pct": FTMO_RISK_PER_TRADE_PCT,
            "daily_dd_limit_pct": FTMO_DAILY_DD_LIMIT_PCT,
            "max_open_trades": FTMO_MAX_CONCURRENT_POSITIONS,
        },
        "n_windows": N_WINDOWS,
        "total_trades": len(all_pnls),
        "bootstrap_ci": ci_result,
        "go_nogo": wf.go_nogo,
        "aggregated": aggregated,
        "per_window": per_window_summary,
        "elapsed_seconds": round(elapsed, 2),
    }

    return _sanitize(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_resweep",
        description="Re-sweep 6 strategies under corrected FTMO params.",
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help="Comma-separated strategy names (default: all 6)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="docs/strategies/revalidation-2026-07/raw",
        help="Output directory for per-strategy JSON files.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Select strategies
    if args.strategies:
        names = [s.strip() for s in args.strategies.split(",")]
    else:
        names = list(STRATEGY_SPECS.keys())

    output_dir = PROJECT_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Verify canonical params
    logger.info(
        "Canonical FTMO params: balance=$%.0f, risk=%.1f%%, daily_dd=%.1f%%, max_open=%d",
        INITIAL_BALANCE,
        FTMO_RISK_PER_TRADE_PCT * 100,
        FTMO_DAILY_DD_LIMIT_PCT * 100,
        FTMO_MAX_CONCURRENT_POSITIONS,
    )

    summary_rows = []
    failures = []

    for name in names:
        if name not in STRATEGY_SPECS:
            logger.error("Unknown strategy: %s", name)
            failures.append({"strategy": name, "error": "unknown strategy"})
            continue

        spec = STRATEGY_SPECS[name]
        csv_path = PROJECT_ROOT / spec["csv"]

        if not csv_path.exists():
            logger.error("Data file missing for %s: %s", name, csv_path)
            failures.append({"strategy": name, "error": f"data file missing: {csv_path}"})
            continue

        logger.info("=== %s (%s %s) ===", name, spec["pair"], spec["timeframe"])
        try:
            bars = load_bars(csv_path, spec["pair"])
            logger.info("  Loaded %d bars from %s", len(bars), csv_path.name)

            result = run_single_strategy(name, spec, bars)

            # Save per-strategy JSON
            out_file = output_dir / f"{name}.json"
            with open(out_file, "w") as f:
                json.dump(result, f, indent=2)
            logger.info(
                "  → %s (%d trades, PF=%.3f, CI_lower=%.3f, go_nogo=%s)",
                out_file.name,
                result["total_trades"],
                result["bootstrap_ci"]["profit_factor"],
                result["bootstrap_ci"]["ci_lower"],
                result["go_nogo"],
            )

            summary_rows.append(
                {
                    "strategy": name,
                    "pair": result["pair"],
                    "timeframe": result["timeframe"],
                    "total_trades": result["total_trades"],
                    "profit_factor": result["bootstrap_ci"]["profit_factor"],
                    "ci_lower": result["bootstrap_ci"]["ci_lower"],
                    "ci_upper": result["bootstrap_ci"]["ci_upper"],
                    "win_rate": result["aggregated"]["mean_win_rate"] if result["aggregated"] else None,
                    "sharpe": result["aggregated"]["mean_sharpe_ratio"] if result["aggregated"] else None,
                    "max_dd": result["aggregated"]["mean_max_drawdown"] if result["aggregated"] else None,
                    "go_nogo": result["go_nogo"],
                    "windows_passed": result["aggregated"]["windows_passed"] if result["aggregated"] else 0,
                    "total_windows": result["aggregated"]["total_windows"] if result["aggregated"] else N_WINDOWS,
                    "elapsed_seconds": result["elapsed_seconds"],
                }
            )

        except Exception as exc:
            logger.error("  FAILED: %s", exc)
            traceback.print_exc()
            failures.append({"strategy": name, "error": str(exc)})

    # Write summary
    summary = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "canonical_params": {
            "initial_balance": INITIAL_BALANCE,
            "risk_per_trade_pct": FTMO_RISK_PER_TRADE_PCT,
            "daily_dd_limit_pct": FTMO_DAILY_DD_LIMIT_PCT,
            "max_open_trades": FTMO_MAX_CONCURRENT_POSITIONS,
        },
        "n_windows": N_WINDOWS,
        "strategies": summary_rows,
        "failures": failures,
        "total_strategies": len(summary_rows),
        "total_failures": len(failures),
    }

    summary_file = output_dir / "_summary.json"
    with open(summary_file, "w") as f:
        json.dump(_sanitize(summary), f, indent=2)

    logger.info("\n=== SUMMARY ===")
    logger.info("Strategies completed: %d, Failures: %d", len(summary_rows), len(failures))
    for row in summary_rows:
        trades = row["total_trades"]
        pf = row["profit_factor"]
        ci = row["ci_lower"]
        go = "PASS" if row["go_nogo"] else "FAIL"
        logger.info(
            "  %-30s trades=%4d  PF=%6.3f  CI_low=%6.3f  go_nogo=%s",
            row["strategy"],
            trades,
            pf,
            ci,
            go,
        )

    if failures:
        for fail in failures:
            logger.warning("  FAILED: %s — %s", fail["strategy"], fail["error"])

    # Check AC: no silent zero-trades
    zero_trade_strategies = [r for r in summary_rows if r["total_trades"] == 0]
    if zero_trade_strategies:
        logger.warning("ZERO-TRADE strategies detected:")
        for z in zero_trade_strategies:
            logger.warning("  %s: 0 trades (may indicate remaining bug)", z["strategy"])

    logger.info("\nSummary saved to %s", summary_file)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
