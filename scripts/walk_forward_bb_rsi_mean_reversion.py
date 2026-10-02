"""Walk-forward test for BB+RSI Mean Reversion strategy on GBPUSD H1, EURUSD H1, XAUUSD H1."""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.walk_forward_runner import run_strategy_walk_forward
from strategies.bb_rsi_reversion import BBRSIConfig, BBRSIMeanReversion

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "forex", "historical")

GBPUSD_H1_PRESET = BBRSIConfig(
    bb_period=17,
    bb_std_dev=2.4,
    rsi_period=13,
    rsi_long_level=24.0,
    rsi_short_level=69.0,
    atr_period=24,
    atr_sl_multiplier=2.3,
    tp1_rr=1.6,
    tp2_rr=1.4,
    ema_trend_period=42,
    adx_period=17,
    adx_max_threshold=32.0,
    require_low_volatility=False,
)

EURUSD_H1_PRESET = BBRSIConfig(
    bb_period=16,
    bb_std_dev=1.6,
    rsi_period=14,
    rsi_long_level=39.0,
    rsi_short_level=64.0,
    atr_period=21,
    atr_sl_multiplier=2.6,
    tp1_rr=1.2,
    tp2_rr=1.0,
    ema_trend_period=40,
    adx_period=14,
    adx_max_threshold=27.0,
    require_low_volatility=False,
)

XAUUSD_H1_PRESET = BBRSIConfig(
    bb_period=18,
    bb_std_dev=1.9,
    rsi_period=13,
    rsi_long_level=32.0,
    rsi_short_level=75.0,
    atr_period=24,
    atr_sl_multiplier=2.0,
    tp1_rr=1.8,
    tp2_rr=2.1,
    ema_trend_period=29,
    adx_period=20,
    adx_max_threshold=32.0,
    require_low_volatility=False,
    pip_value=0.01,
)

PAIRS = [
    ("GBPUSD", "H1", GBPUSD_H1_PRESET),
    ("EURUSD", "H1", EURUSD_H1_PRESET),
    ("XAUUSD", "H1", XAUUSD_H1_PRESET),
]


def run_walk_forward(pair: str, tf: str, preset: BBRSIConfig) -> dict:
    data_path = os.path.join(DATA_DIR, f"{pair}_{tf}.csv")
    if not os.path.exists(data_path):
        print(f"  SKIP: {data_path} not found")
        return {"pair": pair, "timeframe": tf, "skipped": True, "reason": "no data"}

    loader = CsvDataLoader()
    bars = loader.load(data_path)
    print(f"  Loaded {len(bars)} bars | {bars[0].time} to {bars[-1].time}")

    print(
        f"  Config: BB({preset.bb_period},{preset.bb_std_dev}) "
        f"RSI({preset.rsi_period},{preset.rsi_long_level}/{preset.rsi_short_level}) "
        f"ATR_SL={preset.atr_sl_multiplier} TP1={preset.tp1_rr} TP2={preset.tp2_rr} "
        f"ADX<{preset.adx_max_threshold} LowVol={preset.require_low_volatility}"
    )

    results = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=lambda: BBRSIMeanReversion(preset),
        pair=pair,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        overlap_ratio=0.2,
        initial_balance=10000,
        spread_pips=1.5,
        commission_per_lot=3.5,
        min_confidence=0.30,
    )

    print("\n  Window Results:")
    for wm in results.per_window:
        status = "PASS" if wm.passed_go_nogo else "FAIL"
        print(
            f"    W{wm.window_index}: WR={wm.win_rate:.1%}  "
            f"PF={wm.profit_factor:.2f}  DD={wm.max_drawdown:.1%}  "
            f"Sharpe={wm.sharpe_ratio:.2f}  Trades={wm.trade_count}  "
            f"PnL=${wm.total_pnl:.2f}  [{status}]"
        )

    report = {"pair": pair, "timeframe": tf, "skipped": False}
    if results.aggregated:
        a = results.aggregated
        print("\n  Aggregated:")
        print(f"    Mean WR:       {a.mean_win_rate:.1%} (+/-{a.std_win_rate:.1%})")
        print(f"    Mean PF:       {a.mean_profit_factor:.2f} (+/-{a.std_profit_factor:.2f})")
        print(f"    Mean DD:       {a.mean_max_drawdown:.1%} (+/-{a.std_max_drawdown:.1%})")
        print(f"    Mean Sharpe:   {a.mean_sharpe_ratio:.2f} (+/-{a.std_sharpe_ratio:.2f})")
        print(f"    Mean Trades:   {a.mean_trade_count:.0f} (+/-{a.std_trade_count:.0f})")
        print(f"    Mean PnL:      ${a.mean_total_pnl:.2f}")
        print(f"    Windows Pass:  {a.windows_passed}/{a.total_windows}")

        report["aggregated"] = {
            "mean_win_rate": a.mean_win_rate,
            "std_win_rate": a.std_win_rate,
            "mean_profit_factor": a.mean_profit_factor,
            "std_profit_factor": a.std_profit_factor,
            "mean_max_drawdown": a.mean_max_drawdown,
            "mean_sharpe_ratio": a.mean_sharpe_ratio,
            "mean_trade_count": a.mean_trade_count,
            "mean_total_pnl": a.mean_total_pnl,
            "windows_passed": a.windows_passed,
            "total_windows": a.total_windows,
        }

    print(f"\n  GO/NO-GO: {'GO' if results.go_nogo else 'NO-GO'}")

    report["go_nogo"] = results.go_nogo
    report["windows"] = [
        {
            "index": wm.window_index,
            "win_rate": wm.win_rate,
            "profit_factor": wm.profit_factor,
            "max_drawdown": wm.max_drawdown,
            "sharpe_ratio": wm.sharpe_ratio,
            "trade_count": wm.trade_count,
            "total_pnl": wm.total_pnl,
            "passed": wm.passed_go_nogo,
        }
        for wm in results.per_window
    ]

    return report


def main():
    print("=" * 60)
    print("BB+RSI Mean Reversion Walk-Forward Test")
    print("Refs AYUAA-799")
    print("=" * 60)

    all_reports = []
    for pair, tf, preset in PAIRS:
        print(f"\n{'=' * 60}")
        print(f"{pair} {tf}")
        print(f"{'=' * 60}")
        report = run_walk_forward(pair, tf, preset)
        all_reports.append(report)

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")

    for r in all_reports:
        if r.get("skipped"):
            print(f"  {r['pair']} {r['timeframe']}: SKIPPED ({r['reason']})")
        else:
            go = r["go_nogo"]
            agg = r.get("aggregated", {})
            wr = agg.get("mean_win_rate", 0)
            pf = agg.get("mean_profit_factor", 0)
            wp = agg.get("windows_passed", 0)
            wt = agg.get("total_windows", 0)
            print(
                f"  {r['pair']} {r['timeframe']}: WR={wr:.1%} PF={pf:.2f} Windows={wp}/{wt} {'GO' if go else 'NO-GO'}"
            )

    reports_dir = os.path.join(os.path.dirname(__file__), "..", "reports", "bb_rsi_mean_reversion")
    os.makedirs(reports_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = os.path.join(reports_dir, f"bb_rsi_mr_{ts}.json")
    with open(report_path, "w") as f:
        json.dump(
            {"strategy": "BB+RSI Mean Reversion", "results": all_reports},
            f,
            indent=2,
        )
    print(f"\nReport saved: {report_path}")


if __name__ == "__main__":
    main()
