#!/usr/bin/env python3
"""SRF Full Sweep — runs walk-forward + DSR gate + go/no-go for a given pair.

Runs all applicable strategies × timeframes through the SRF pipeline,
records results to DuckDB, and prints a summary.

Usage:
    python3 scripts/run_srf_sweep.py --pair XAUUSD
    python3 scripts/run_srf_sweep.py --pair XAUUSD --timeframes M15,H1
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backtest.types import Bar
from backtest.walk_forward_runner import run_strategy_walk_forward

logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data" / "forex" / "historical"
DB_PATH = PROJECT_ROOT / "data" / "research" / "research.duckdb"

# ── Timeframe mapping ─────────────────────────────────────────────────────
TF_MINUTES = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def load_bars_from_csv(csv_path: Path, pair: str, tf_minutes: int) -> list[Bar]:
    """Load bars from CSV into Bar objects."""
    df = pd.read_csv(csv_path)
    # Normalize column names
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
        raise ValueError(f"No timestamp column found in {csv_path}. Columns: {list(df.columns)}")

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


def get_strategies_for_pair(pair: str) -> dict[str, callable]:
    """Return {strategy_name: factory_fn} for all strategies applicable to pair."""
    factories = {}

    # TTC XAUUSD — only for XAUUSD
    if pair == "XAUUSD":
        from strategies.ttc_xauusd import TTCXAUUSDStrategy

        factories["ttc_xauusd"] = lambda: TTCXAUUSDStrategy()

    # Killzone Momentum — works on XAUUSD, forex
    from strategies.killzone_momentum import KillzoneMomentumConfig

    try:
        from strategies.killzone_momentum import KillzoneMomentumStrategy

        factories["killzone_momentum"] = lambda: KillzoneMomentumStrategy(KillzoneMomentumConfig())
    except ImportError:
        # Try alternate class name
        import strategies.killzone_momentum as kz_mod

        for name in dir(kz_mod):
            obj = getattr(kz_mod, name)
            if isinstance(obj, type) and "Killzone" in name and "Config" not in name:
                factories["killzone_momentum"] = lambda: obj(KillzoneMomentumConfig())  # noqa: B023
                break

    # Volatility Squeeze — works on XAUUSD, forex
    from strategies.volatility_squeeze import VolatilitySqueezeStrategy

    factories["volatility_squeeze"] = lambda: VolatilitySqueezeStrategy()

    # BB RSI Reversion
    from strategies.bb_rsi_reversion import BBRSIConfig

    try:
        from strategies.bb_rsi_reversion import BBRSIReversionStrategy

        factories["bb_rsi_reversion"] = lambda: BBRSIReversionStrategy(BBRSIConfig())
    except ImportError:
        import strategies.bb_rsi_reversion as bb_mod

        for name in dir(bb_mod):
            obj = getattr(bb_mod, name)
            if isinstance(obj, type) and "BB" in name and "Config" not in name:
                factories["bb_rsi_reversion"] = lambda: obj(BBRSIConfig())  # noqa: B023
                break

    # Volatility Regime Breakout
    from strategies.volatility_regime_breakout import VolatilityRegimeBreakoutStrategy

    factories["volatility_regime_breakout"] = lambda: VolatilityRegimeBreakoutStrategy()

    # SRMR+ — forex only (session range MR)
    if pair != "XAUUSD":
        from strategies.srmr_plus import SRMRPlusConfig

        try:
            from strategies.srmr_plus import SRMRPlusStrategy

            factories["srmr_plus"] = lambda: SRMRPlusStrategy(SRMRPlusConfig())
        except ImportError:
            import strategies.srmr_plus as srmr_mod

            for name in dir(srmr_mod):
                obj = getattr(srmr_mod, name)
                if isinstance(obj, type) and "SRMR" in name and "Config" not in name:
                    factories["srmr_plus"] = lambda: obj(SRMRPlusConfig())  # noqa: B023
                    break

    # Session Breakout
    try:
        import strategies.session_breakout as sb_mod
        from strategies.session_breakout import SessionBreakoutConfig

        for name in dir(sb_mod):
            obj = getattr(sb_mod, name)
            if isinstance(obj, type) and "Breakout" in name and "Config" not in name:
                factories["session_breakout"] = lambda: obj(SessionBreakoutConfig())  # noqa: B023
                break
    except ImportError:
        pass

    # Donchian + ATR Trailing Trend
    from strategies.donchian_atr_trend import DonchianATRTrendStrategy

    factories["donchian_atr_trend"] = lambda: DonchianATRTrendStrategy()

    # London Breakout + Retest
    from strategies.london_breakout_retest import (  # noqa: I001
        LondonBreakoutConfig,
        LondonBreakoutRetestStrategy,
    )

    lb_cfg = LondonBreakoutConfig(symbol=pair)
    factories["london_breakout_retest"] = lambda: LondonBreakoutRetestStrategy(lb_cfg)

    return factories


def run_sweep(pair: str, timeframes: list[str], n_windows: int = 5) -> list[dict]:
    """Run full sweep for a pair across strategies and timeframes."""
    from srf.gonogo import evaluate_go_nogo
    from srf.schema import SRFDatabase  # noqa: I001

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = SRFDatabase(str(DB_PATH))

    strategies = get_strategies_for_pair(pair)
    logger.info("Strategies for %s: %s", pair, list(strategies.keys()))

    results = []

    for tf in timeframes:
        tf_minutes = TF_MINUTES.get(tf, 15)

        # Use tick loader (real bid/ask data from DuckDB, CSV fallback)
        from backtest.tick_loader import load_bars as load_bars_smart

        logger.info("Loading %s %s bars (tick data preferred)...", pair, tf)
        try:
            bars = load_bars_smart(pair, tf)
        except Exception as e:
            logger.error("Failed to load bars: %s", e)
            continue
        logger.info("  %d bars loaded (%s to %s)", len(bars), bars[0].time, bars[-1].time)

        if len(bars) < 1000:
            logger.warning("Only %d bars — need ≥1000 for meaningful walk-forward", len(bars))
            continue

        for strat_name, factory in strategies.items():
            logger.info(
                "Running %s on %s %s (%d-window walk-forward)...",
                strat_name,
                pair,
                tf,
                n_windows,
            )
            start = time.monotonic()

            try:
                wf = run_strategy_walk_forward(
                    bars=bars,
                    strategy_factory=factory,
                    pair=pair,
                    n_windows=n_windows,
                    train_ratio=0.7,
                    initial_balance=10_000,
                    min_confidence=0.30,
                )

                elapsed = time.monotonic() - start
                agg = wf.aggregated

                if agg is None:
                    logger.warning("  %s %s %s: no aggregated metrics", strat_name, pair, tf)
                    results.append(
                        {
                            "strategy": strat_name,
                            "pair": pair,
                            "tf": tf,
                            "status": "no_metrics",
                            "elapsed_s": round(elapsed, 1),
                        }
                    )
                    continue

                # Run go/no-go evaluation using per_window (WalkForwardResults.per_window)
                window_dicts = []
                for w in wf.per_window if hasattr(wf, "per_window") else []:
                    window_dicts.append(
                        {
                            "win_rate": getattr(w, "win_rate", 0),
                            "profit_factor": getattr(w, "profit_factor", 0),
                            "max_drawdown": getattr(w, "max_drawdown", 0),
                            "trade_count": getattr(w, "trade_count", 0),
                            "sharpe": getattr(w, "sharpe_ratio", 0),
                            "total_pnl": getattr(w, "total_pnl", 0),
                        }
                    )

                go_nogo = evaluate_go_nogo(window_dicts) if window_dicts else None

                result = {
                    "strategy": strat_name,
                    "pair": pair,
                    "tf": tf,
                    "status": "completed",
                    "n_bars": len(bars),
                    "n_windows": agg.total_windows,
                    "windows_passed": agg.windows_passed,
                    "win_rate": round(agg.mean_win_rate, 4),
                    "profit_factor": round(agg.mean_profit_factor, 2),
                    "max_drawdown": round(agg.mean_max_drawdown, 4),
                    "total_trades": round(agg.mean_trade_count * agg.total_windows),
                    "total_pnl": round(agg.mean_total_pnl, 2),
                    "go_nogo": go_nogo.decision if go_nogo else "unknown",
                    "go_nogo_detail": go_nogo.detail if go_nogo else "",
                    "elapsed_s": round(elapsed, 1),
                }

                # Store in DuckDB
                run_id = f"{strat_name}_{pair}_{tf}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                git_commit = "sweep"  # simplified for sweep runs

                with db as conn:
                    # Ensure strategy registered
                    conn.execute(
                        "INSERT INTO strategies (name, version, module_path, status) "
                        "VALUES (?, '1.0', ?, 'production') ON CONFLICT (name) DO NOTHING",
                        [strat_name, f"strategies.{strat_name}"],
                    )
                    # Insert run
                    conn.execute(
                        "INSERT INTO runs (run_id, strategy_name, pair, timeframe, "
                        "params_json, git_commit, data_hash, status, created_at, completed_at, compute_seconds) "
                        "VALUES (?, ?, ?, ?, '{}', ?, ?, 'completed', now(), now(), ?)",
                        [
                            run_id,
                            strat_name,
                            pair,
                            tf_minutes,
                            git_commit,
                            f"tick_db:{pair}:{tf}",
                            elapsed,
                        ],
                    )
                    # Insert metrics summary
                    conn.execute(
                        "INSERT INTO metrics_summary "
                        "(run_id, mean_win_rate, std_win_rate, mean_profit_factor, "
                        " mean_max_drawdown, mean_sharpe, total_trades, windows_passed, "
                        " windows_total, go_nogo) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            run_id,
                            agg.mean_win_rate,
                            getattr(agg, "std_win_rate", 0),
                            agg.mean_profit_factor,
                            agg.mean_max_drawdown,
                            getattr(agg, "mean_sharpe_ratio", 0),
                            int(agg.mean_trade_count * agg.total_windows),
                            agg.windows_passed,
                            agg.total_windows,
                            result["go_nogo"],
                        ],
                    )

                results.append(result)
                logger.info(
                    "  ✓ %s %s %s: WR=%.1f%% PF=%.2f DD=%.1f%% trades=%.0f go=%s (%.1fs)",
                    strat_name,
                    pair,
                    tf,
                    agg.mean_win_rate * 100,
                    agg.mean_profit_factor,
                    agg.mean_max_drawdown * 100,
                    agg.mean_trade_count * agg.total_windows,
                    result["go_nogo"],
                    elapsed,
                )

            except Exception as e:
                elapsed = time.monotonic() - start
                logger.error("  ✗ %s %s %s FAILED: %s (%.1fs)", strat_name, pair, tf, e, elapsed)
                results.append(
                    {
                        "strategy": strat_name,
                        "pair": pair,
                        "tf": tf,
                        "status": "failed",
                        "error": str(e),
                        "elapsed_s": round(elapsed, 1),
                    }
                )

    return results


def main():
    parser = argparse.ArgumentParser(description="SRF Full Sweep")
    parser.add_argument("--pair", required=True, help="Trading pair (e.g., XAUUSD)")
    parser.add_argument("--timeframes", default="M15,H1,M5", help="Comma-separated timeframes")
    parser.add_argument("--windows", type=int, default=5, help="Walk-forward windows")
    parser.add_argument("--output", default=None, help="Output JSON file")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    tfs = args.timeframes.split(",")
    logger.info("=" * 60)
    logger.info("  SRF Full Sweep: %s × %s", args.pair, tfs)
    logger.info("=" * 60)

    results = run_sweep(args.pair, tfs, n_windows=args.windows)

    # Summary
    print(f"\n{'=' * 80}")
    print(f"  SRF Sweep Results: {args.pair}")
    print(f"{'=' * 80}")
    print(f"  {'Strategy':<25} {'TF':<5} {'WR':>6} {'PF':>6} {'MaxDD':>7} {'Trades':>7} {'Go/NoGo':<8}")
    print(f"  {'-' * 25} {'-' * 5} {'-' * 6} {'-' * 6} {'-' * 7} {'-' * 7} {'-' * 8}")

    for r in results:
        if r["status"] == "completed":
            print(
                f"  {r['strategy']:<25} {r['tf']:<5} {r['win_rate'] * 100:>5.1f}% "
                f"{r['profit_factor']:>6.2f} {r['max_drawdown'] * 100:>6.1f}% "
                f"{r['total_trades']:>7.0f} {r['go_nogo']:<8}"
            )
        elif r["status"] == "failed":
            print(f"  {r['strategy']:<25} {r['tf']:<5} FAILED: {r.get('error', '')[:50]}")
        else:
            print(f"  {r['strategy']:<25} {r['tf']:<5} {r['status']}")

    # Save JSON
    output_path = (
        args.output or f"/tmp/srf_sweep_{args.pair}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"  # noqa: S108
    )
    Path(output_path).write_text(json.dumps(results, indent=2, default=str))
    print(f"\n  Results saved: {output_path}")
    print(f"  DuckDB: {DB_PATH}")


if __name__ == "__main__":
    main()
