#!/usr/bin/env python3
"""ttc_xauusd XAUUSD M15 verification — fresh investigation.

Tests:
  1. Synthetic GBM with XAUUSD M15 realistic vol (sigma derived from log returns)
  2. Trade inspection (window 4 trades from a 5-window walk-forward on real data)
  3. Parameter sensitivity: lookback=5/10, HISTORY_BARS=50/80

Outputs JSON results to /tmp/ttc_xauusd_verification.json and prints summary.

Usage:
    python3 scripts/verify_ttc_xauusd_verification.py
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

import numpy as np

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path("$AYUMI_ROOT")
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))

import backtest.strategies.tts_strategy as tts_mod  # noqa: E402
from backtest.db_data_loader import DbDataLoader  # noqa: E402, I001
from backtest.multi_strategy_engine import MultiStrategyBacktestEngine  # noqa: E402
from backtest.types import BacktestConfig  # noqa: E402
from backtest.walk_forward_runner import (  # noqa: E402
    WalkForwardValidator,
    _compute_metrics,
    _sanitize_profit_factor,
)
from signal_engine.risk_sizer import ConfidencePositionSizer  # noqa: E402
from strategies.ttc_xauusd import TTCXAUUSDStrategy  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────
def summarize_trades(trades) -> dict:
    if not trades:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "confidence_min": None,
            "confidence_max": None,
            "confidence_mean": None,
            "high_conf_count": 0,  # >= 0.95
        }
    pnls = [t.profit_loss for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_win = sum(wins)
    total_loss = abs(sum(losses))
    pf = (total_win / total_loss) if total_loss > 0 else (10.0 if total_win > 0 else 0.0)
    confs = [t.confidence_score for t in trades]
    return {
        "trade_count": len(trades),
        "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
        "profit_factor": pf,
        "total_pnl": sum(pnls),
        "confidence_min": min(confs) if confs else None,
        "confidence_max": max(confs) if confs else None,
        "confidence_mean": sum(confs) / len(confs) if confs else None,
        "high_conf_count": sum(1 for c in confs if c >= 0.95),
    }


def run_engine_on_bars(
    bars: list,
    label: str,
    override_lookback: int | None = None,
    override_history_bars: int | None = None,
) -> dict:
    """Run TTCXAUUSDStrategy on bars using a direct MultiStrategyBacktestEngine.

    Returns a dict with metrics, per_window, and confidence summary.
    """
    from backtest.strategies.tts_strategy import TTSStrategy

    # Optionally override SWING_LOOKBACK / HISTORY_BARS by patching class
    # attributes on TTSStrategy for the duration of this run.
    saved = {}
    if override_lookback is not None:
        saved["SWING_LOOKBACK"] = TTSStrategy.SWING_LOOKBACK
        TTSStrategy.SWING_LOOKBACK = override_lookback
    if override_history_bars is not None:
        saved["HISTORY_BARS"] = TTSStrategy.HISTORY_BARS
        TTSStrategy.HISTORY_BARS = override_history_bars
    try:
        config = BacktestConfig(
            starting_balance=10000,
            spread_pips=3.0,
            commission_per_lot=3.5,
            pair="XAUUSD",
            min_confidence=0.30,
        )
        strategy = TTCXAUUSDStrategy()
        # Force SwingDetector to use overridden lookback (TTC init uses 5 hardcoded)
        if override_lookback is not None:
            strategy._strategy._swing_detector = tts_mod.SwingDetector(lookback=override_lookback)
        risk_sizer = ConfidencePositionSizer(account_size=10000)
        engine = MultiStrategyBacktestEngine(config, [strategy], risk_sizer=risk_sizer)
        t0 = time.time()
        result = engine.run_all_strategies(bars)
        elapsed = time.time() - t0
        sr = result[strategy.name]
        metrics = sr.metrics
        summary = summarize_trades(metrics.trades)
        summary["label"] = label
        summary["elapsed_s"] = round(elapsed, 1)
        summary["n_bars"] = len(bars)
        summary["override_lookback"] = override_lookback
        summary["override_history_bars"] = override_history_bars
        return summary
    finally:
        for k, v in saved.items():
            setattr(TTSStrategy, k, v)


def run_walk_forward(
    bars: list,
    label: str,
    n_windows: int = 5,
    override_lookback: int | None = None,
    override_history_bars: int | None = None,
) -> dict:
    """Walk-forward run with explicit window tracking.

    Returns per-window metrics + aggregated metrics.
    """
    from backtest.strategies.tts_strategy import TTSStrategy

    saved = {}
    if override_lookback is not None:
        saved["SWING_LOOKBACK"] = TTSStrategy.SWING_LOOKBACK
        TTSStrategy.SWING_LOOKBACK = override_lookback
    if override_history_bars is not None:
        saved["HISTORY_BARS"] = TTSStrategy.HISTORY_BARS
        TTSStrategy.HISTORY_BARS = override_history_bars
    try:
        validator = WalkForwardValidator(
            data=bars,
            n_windows=n_windows,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
        )
        per_window = []
        all_trade_records = []
        all_trade_objects = []  # list of (window_idx, SimulatedTrade)
        config = BacktestConfig(
            starting_balance=10000,
            spread_pips=3.0,
            commission_per_lot=3.5,
            pair="XAUUSD",
            min_confidence=0.30,
        )
        risk_sizer = ConfidencePositionSizer(account_size=10000)
        t0 = time.time()
        for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):  # noqa: B007
            strategy = TTCXAUUSDStrategy()
            if override_lookback is not None:
                strategy._strategy._swing_detector = tts_mod.SwingDetector(lookback=override_lookback)
            if hasattr(strategy, "reset") and callable(strategy.reset):
                strategy.reset()
            engine = MultiStrategyBacktestEngine(config, [strategy], risk_sizer=risk_sizer)
            result = engine.run_all_strategies(test_bars)
            metrics_obj = result[strategy.name].metrics
            for t in metrics_obj.trades:
                all_trade_objects.append((idx, t))
            trade_dicts = [
                {
                    "pnl": t.profit_loss,
                    "direction": str(getattr(t, "direction", "")),
                    "window_idx": idx,
                    "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "stop_loss": t.stop_loss,
                    "confidence_score": t.confidence_score,
                    "rationale": t.rationale[:200] if t.rationale else "",
                }
                for t in metrics_obj.trades
            ]
            all_trade_records.extend(trade_dicts)
            window_metrics = _compute_metrics(
                idx, trade_dicts, initial_balance=10000, bars_in_window=test_bars
            )
            window_metrics = window_metrics.__class__(
                window_index=window_metrics.window_index,
                win_rate=window_metrics.win_rate,
                profit_factor=_sanitize_profit_factor(window_metrics.profit_factor),
                max_drawdown=window_metrics.max_drawdown,
                sharpe_ratio=window_metrics.sharpe_ratio,
                trade_count=window_metrics.trade_count,
                total_pnl=window_metrics.total_pnl,
                passed_go_nogo=window_metrics.passed_go_nogo,
            )
            per_window.append(window_metrics)
        elapsed = time.time() - t0

        # Aggregate
        wrs = [m.win_rate for m in per_window]
        pfs = [m.profit_factor for m in per_window]
        trades_per_w = [m.trade_count for m in per_window]
        pnls = [m.total_pnl for m in per_window]
        windows_passed = sum(1 for m in per_window if m.passed_go_nogo)
        summary = {
            "label": label,
            "n_bars": len(bars),
            "n_windows": n_windows,
            "elapsed_s": round(elapsed, 1),
            "per_window": [
                {
                    "window_index": m.window_index,
                    "win_rate": round(m.win_rate, 4),
                    "profit_factor": round(m.profit_factor, 4),
                    "trade_count": m.trade_count,
                    "total_pnl": round(m.total_pnl, 2),
                    "passed_go_nogo": m.passed_go_nogo,
                }
                for m in per_window
            ],
            "aggregated": {
                "mean_win_rate": round(sum(wrs) / len(wrs), 4) if wrs else 0.0,
                "mean_profit_factor": round(sum(pfs) / len(pfs), 4) if pfs else 0.0,
                "mean_trade_count": round(sum(trades_per_w) / len(trades_per_w), 1) if trades_per_w else 0,
                "mean_total_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
                "windows_passed": windows_passed,
                "total_windows": len(per_window),
            },
            "n_trades_total": sum(trades_per_w),
            "override_lookback": override_lookback,
            "override_history_bars": override_history_bars,
        }
        return summary, all_trade_objects
    finally:
        for k, v in saved.items():
            setattr(TTSStrategy, k, v)


# ── Test 1: Synthetic GBM ──────────────────────────────────────────────────
def generate_gbm_bars(
    n_bars: int,
    sigma: float,
    drift: float = 0.0,
    seed: int = 42,
    start_price: float = 2813.0,
) -> list:
    """Generate GBM bars with given per-bar sigma.

    Each bar is a step: close_t = close_{t-1} * exp(N(drift, sigma))
    Open of bar_t = close of bar_{t-1}.
    High/Low are noisy around the close.
    """
    from datetime import datetime, timedelta, timezone

    from core.types import Bar, BarPeriod

    rng = np.random.default_rng(seed)
    returns = rng.normal(drift, sigma, n_bars)
    log_prices = np.cumsum(returns)
    closes = start_price * np.exp(log_prices)

    base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(n_bars):
        c = float(closes[i])
        o = float(closes[i - 1]) if i > 0 else c
        noise = abs(rng.normal(0, sigma * 0.5))
        h = c * (1.0 + noise)
        lo = c * (1.0 - noise)
        # Ensure H >= max(O, C) and L <= min(O, C)
        h = max(h, o, c)
        lo = min(lo, o, c)
        t = base_time + timedelta(minutes=15 * i)
        bars.append(
            Bar(
                time=t,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=1000.0,
                period=BarPeriod(15),
                spread_pips=3.0,
            )
        )
    return bars


def get_xauusd_vol() -> dict:
    """Compute XAUUSD M15 realized vol stats from DuckDB."""
    import duckdb

    con = duckdb.connect(str(ROOT / "data" / "ayumi_market.duckdb"), read_only=True)
    df = con.execute("""
        SELECT timestamp_utc, close, high, low
        FROM bars WHERE symbol='XAUUSD' AND timeframe='M15'
        ORDER BY timestamp_utc
    """).fetchdf()
    con.close()
    df["log_ret"] = np.log(df["close"] / df["close"].shift(1))
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - df["prev_close"]).abs(),
            (df["low"] - df["prev_close"]).abs(),
        ),
    )
    df["atr14"] = df["tr"].rolling(14).mean()
    return {
        "n_bars": int(len(df)),
        "avg_close": float(df["close"].mean()),
        "median_close": float(df["close"].median()),
        "avg_atr14": float(df["atr14"].mean()),
        "median_atr14": float(df["atr14"].median()),
        "log_return_sigma": float(df["log_ret"].std()),
        "log_return_mean": float(df["log_ret"].mean()),
        "atr_pct_of_price": float((df["atr14"].mean() / df["close"].mean()) * 100),
    }


def test_synthetic(n_bars: int = 5000, sigma: float = 0.001381) -> dict:
    """Run the strategy on synthetic GBM with realistic vol."""
    print(f"Generating {n_bars} synthetic GBM bars (sigma={sigma})...")
    bars = generate_gbm_bars(n_bars=n_bars, sigma=sigma, seed=42, start_price=2813.0)
    print("Running strategy on synthetic data...")
    summary = run_engine_on_bars(bars, label=f"synthetic_gbm_sigma{sigma}")
    summary["n_bars"] = n_bars
    summary["sigma"] = sigma
    summary["pass"] = summary["profit_factor"] < 2.0 and summary["win_rate"] < 0.65
    summary["verdict"] = (
        "BUG: PF>=2.0 on noise → strategy detects noise-as-pattern"
        if not summary["pass"]
        else "PASS: PF<2.0 on noise → no spurious edge"
    )
    return summary


# ── Test 2: Trade inspection (window 4 from walk-forward) ──────────────────
def test_inspect_trades(bars: list) -> dict:
    """Run walk-forward (5 windows) on real XAUUSD M15, inspect window 4."""
    print("Running walk-forward (5 windows) on real XAUUSD M15...")
    summary, trade_objects = run_walk_forward(
        bars=bars,
        label="real_xauusd_m15_wf5",
        n_windows=5,
    )
    # Window 4 trades
    w4_trades = [(idx, t) for idx, t in trade_objects if idx == 4]
    sample_size = min(20, len(w4_trades))
    rng = random.Random(7)  # noqa: S311
    if w4_trades:
        sample = rng.sample(w4_trades, sample_size)
    else:
        sample = []

    issues = []
    coherent_count = 0
    for idx, t in sample:  # noqa: B007
        ok = True
        d = str(t.direction)
        if t.entry_price == t.exit_price:
            issues.append(f"{t.entry_time} {d}: entry=exit={t.entry_price:.5f} (zero move; pnl={t.profit_loss:.2f})")
            ok = False
        # Long win: exit > entry; Long loss: exit < entry
        if d == "LONG" and t.profit_loss > 0 and t.exit_price <= t.entry_price:
            issues.append(f"{t.entry_time} LONG win with exit<=entry ({t.entry_price:.5f} → {t.exit_price:.5f})")
            ok = False
        if d == "SHORT" and t.profit_loss > 0 and t.exit_price >= t.entry_price:
            issues.append(f"{t.entry_time} SHORT win with exit>=entry ({t.entry_price:.5f} → {t.exit_price:.5f})")
            ok = False
        # Stop loss should be on opposite side
        if d == "LONG" and t.stop_loss >= t.entry_price:
            issues.append(f"{t.entry_time} LONG with SL>=entry ({t.stop_loss:.5f} >= {t.entry_price:.5f})")
            ok = False
        if d == "SHORT" and t.stop_loss <= t.entry_price and t.stop_loss > 0:
            issues.append(f"{t.entry_time} SHORT with SL<=entry ({t.stop_loss:.5f} <= {t.entry_price:.5f})")
            ok = False
        if ok:
            coherent_count += 1

    # Window-by-window confidence inspection
    per_window_conf = []
    for idx in range(5):
        w_trades = [t for i, t in trade_objects if i == idx]
        if w_trades:
            confs = [t.confidence_score for t in w_trades]
            per_window_conf.append(
                {
                    "window": idx,
                    "n_trades": len(confs),
                    "conf_min": round(min(confs), 4),
                    "conf_max": round(max(confs), 4),
                    "conf_mean": round(sum(confs) / len(confs), 4),
                    "high_conf_count": sum(1 for c in confs if c >= 0.95),
                }
            )

    return {
        "label": "real_xauusd_m15_wf5",
        "summary": summary,
        "window4_trade_count": len(w4_trades),
        "window4_sampled": sample_size,
        "window4_coherent_count": coherent_count,
        "window4_issues": issues,
        "window4_pass": len(issues) == 0,
        "window4_samples": [
            {
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "direction": str(t.direction),
                "entry_price": round(t.entry_price, 5),
                "exit_price": round(t.exit_price, 5),
                "stop_loss": round(t.stop_loss, 5),
                "take_profit_1": round(t.take_profit_1, 5),
                "take_profit_2": round(t.take_profit_2, 5),
                "pnl": round(t.profit_loss, 2),
                "exit_reason": str(t.exit_reason),
                "confidence_score": round(t.confidence_score, 4),
                "rationale": (t.rationale[:150] + "...")
                if t.rationale and len(t.rationale) > 150
                else (t.rationale or ""),
            }
            for _, t in sample
        ],
        "per_window_confidence": per_window_conf,
    }


# ── Test 3: Parameter sensitivity ───────────────────────────────────────────
def test_param_sensitivity(bars: list) -> dict:
    """Run walk-forward with different lookback/HISTORY_BARS settings."""
    results = []
    configs = [
        ("baseline_look5_hist50", 5, 50),
        ("lookback10_hist50", 10, 50),
        ("lookback5_hist80", 5, 80),
        ("lookback10_hist80", 10, 80),
    ]
    for label, lookback, hist_bars in configs:
        print(f"\n--- {label}: lookback={lookback}, HISTORY_BARS={hist_bars} ---")
        try:
            summary, _ = run_walk_forward(
                bars=bars,
                label=label,
                n_windows=5,
                override_lookback=lookback,
                override_history_bars=hist_bars,
            )
            results.append(
                {
                    "config": label,
                    "lookback": lookback,
                    "history_bars": hist_bars,
                    "summary": summary,
                }
            )
        except Exception as e:
            results.append(
                {
                    "config": label,
                    "lookback": lookback,
                    "history_bars": hist_bars,
                    "error": str(e),
                }
            )
    return {"label": "param_sensitivity", "configs": results}


# ── Driver ──────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 80)
    print("TTC XAUUSD XAUUSD M15 VERIFICATION — Fresh Investigation")
    print("=" * 80)
    print()

    # Get XAUUSD vol characteristics
    print("Step 0: Compute XAUUSD M15 vol characteristics...")
    vol = get_xauusd_vol()
    print(json.dumps(vol, indent=2))
    print()

    # Load real bars
    print("Step 0a: Load XAUUSD M15 real bars from DuckDB...")
    loader = DbDataLoader()
    bars = loader.load_bars("XAUUSD", "M15")
    print(f"Loaded {len(bars)} bars: {bars[0].time} → {bars[-1].time}")
    print()

    # Test 1: Synthetic GBM with realistic sigma
    print("=" * 60)
    print("TEST 1: Synthetic GBM (vol = XAUUSD M15 realized)")
    print("=" * 60)
    t1 = test_synthetic(n_bars=5000, sigma=vol["log_return_sigma"])
    print(json.dumps(t1, indent=2, default=str))
    print()

    # Test 2: Trade inspection
    print("=" * 60)
    print("TEST 2: Walk-forward on real XAUUSD M15 + window 4 inspection")
    print("=" * 60)
    t2 = test_inspect_trades(bars)
    print(f"Window 4 trade count: {t2['window4_trade_count']}")
    print(f"Window 4 sampled: {t2['window4_sampled']}")
    print(f"Window 4 coherent: {t2['window4_coherent_count']}")
    print(f"Window 4 issues: {t2['window4_issues']}")
    print(f"Window 4 pass: {t2['window4_pass']}")
    print()
    print("Per-window summary:")
    for w in t2["summary"]["per_window"]:
        print(
            f"  W{w['window_index']}: WR={w['win_rate']:.3f}, PF={w['profit_factor']:.3f}, trades={w['trade_count']}, pnl={w['total_pnl']:.2f}"  # noqa: E501
        )
    print(f"Aggregated: {t2['summary']['aggregated']}")
    print()
    print("Window 4 sample trades:")
    for s in t2["window4_samples"][:5]:
        print(
            f"  {s['entry_time']} {s['direction']} entry={s['entry_price']:.5f} exit={s['exit_price']:.5f} SL={s['stop_loss']:.5f} pnl={s['pnl']:.2f} conf={s['confidence_score']:.3f}"  # noqa: E501
        )
    print()
    print("Per-window confidence distribution:")
    for c in t2["per_window_confidence"]:
        print(
            f"  W{c['window']}: n={c['n_trades']}, conf min/max/mean = {c['conf_min']:.3f}/{c['conf_max']:.3f}/{c['conf_mean']:.3f}, high_conf(>=0.95)={c['high_conf_count']}"  # noqa: E501
        )
    print()

    # Test 3: Parameter sensitivity
    print("=" * 60)
    print("TEST 3: Parameter sensitivity (lookback × HISTORY_BARS)")
    print("=" * 60)
    t3 = test_param_sensitivity(bars)
    print()
    for c in t3["configs"]:
        if "error" in c:
            print(f"  {c['config']}: ERROR - {c['error']}")
        else:
            s = c["summary"]
            agg = s["aggregated"]
            print(f"  {c['config']} (lookback={c['lookback']}, hist={c['history_bars']}):")
            print(
                f"    mean PF={agg['mean_profit_factor']:.3f}, mean WR={agg['mean_win_rate']:.3f}, mean trades/w={agg['mean_trade_count']:.1f}, mean pnl={agg['mean_total_pnl']:.2f}, windows_passed={agg['windows_passed']}/{agg['total_windows']}"  # noqa: E501
            )
            for w in s["per_window"]:
                print(
                    f"      W{w['window_index']}: WR={w['win_rate']:.3f}, PF={w['profit_factor']:.3f}, trades={w['trade_count']}, pnl={w['total_pnl']:.2f}"  # noqa: E501
                )
    print()

    # Save results
    out = {
        "xauusd_vol": vol,
        "test1_synthetic": t1,
        "test2_real_inspection": t2,
        "test3_param_sensitivity": t3,
    }
    out_path = Path("/tmp/ttc_xauusd_verification.json")  # noqa: S108
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nFull results written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
