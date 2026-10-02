#!/usr/bin/env python3
"""Blend-wide walk-forward + JSONL producer for downstream DSR annotation.

Usage:
    python3 scripts/run_blend_walkforward_for_dsr.py --pairs XAUUSD
    python3 scripts/run_blend_walkforward_for_dsr.py --pairs XAUUSD,EURUSD,GBPUSD
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from backtest.tick_loader import load_bars
from backtest.walk_forward_runner import run_strategy_walk_forward
from strategies.donchian_atr_trend_v2 import (
    DonchianATRConfig,
    DonchianATRTrendV2Strategy,
)
from strategies.dual_tf_squeeze_pro import (
    DualTFSqueezeProConfig,
    DualTFSqueezeProStrategy,
)
from strategies.killzone_momentum import (
    KillzoneMomentumConfig,
    KillzoneMomentumStrategy,
)
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy
from strategies.ttc_xauusd import TTCXAUUSDStrategy

# Mirror launch_blend_forward_test STRATEGY_TIMEFRAMES
STRATEGY_FACTORIES = {
    "killzone_momentum": (
        "M15",
        lambda pair: lambda: KillzoneMomentumStrategy(KillzoneMomentumConfig(symbol=pair)),
    ),
    "dual_tf_squeeze_pro": (
        "M15",
        lambda pair: lambda: DualTFSqueezeProStrategy(DualTFSqueezeProConfig(symbol=pair)),
    ),
    "donchian_atr_trend_v2": (
        "H1",
        lambda pair: lambda: DonchianATRTrendV2Strategy(DonchianATRConfig(symbol=pair)),
    ),
    "srmr_plus": (
        "M15",
        lambda pair: lambda: SRMRPlusStrategy(SRMRPlusConfig(symbol=pair)),
    ),
    "london_breakout_retest": (
        "M15",
        lambda pair: lambda: LondonBreakoutRetestStrategy(LondonBreakoutConfig(symbol=pair)),
    ),
    "ttc_xauusd": (
        "M15",
        lambda pair: lambda: TTCXAUUSDStrategy(),
    ),
}

# TTC is XAUUSD-only
STRATEGY_PAIR_RESTRICTIONS = {
    "ttc_xauusd": {"XAUUSD"},
}

# Per-strategy embargo bars (0 default; TTC requires 96 per its docstring)
EMBARGO_BARS = {
    "ttc_xauusd": getattr(TTCXAUUSDStrategy, "EMBARGO_BARS_M15", 96),
}

# Spread values matching launch_blend_forward_test.py blend config
# (conservative — tighter than types.py canonical defaults)
SPREAD_PIPS = {"XAUUSD": 0.3, "EURUSD": 0.8, "GBPUSD": 2.0, "USDJPY": 0.8}


def run_one(strategy_name: str, pair: str) -> dict | None:
    """Run walk-forward for one strategy × pair and return JSONL-eligible dict."""
    tf, factory_template = STRATEGY_FACTORIES[strategy_name]

    # Check pair restrictions
    allowed_pairs = STRATEGY_PAIR_RESTRICTIONS.get(strategy_name)
    if allowed_pairs and pair not in allowed_pairs:
        return None

    # Load bars
    bars = load_bars(pair, tf)
    if len(bars) < 1000:
        return {
            "strategy": strategy_name,
            "pair": pair,
            "timeframe": tf,
            "status": "skipped",
            "reason": f"insufficient bars: {len(bars)}",
        }

    # Build strategy factory
    factory = factory_template(pair)

    t0 = time.monotonic()
    wf = run_strategy_walk_forward(
        bars=bars,
        strategy_factory=factory,
        pair=pair,
        n_windows=5,
        train_ratio=0.7,
        val_ratio=0.15,
        overlap_ratio=0.2,
        initial_balance=10000,
        spread_pips=SPREAD_PIPS.get(pair, 1.5),
        commission_per_lot=3.5,
        min_confidence=0.30,
        embargo_bars=EMBARGO_BARS.get(strategy_name, 0),
    )
    elapsed = time.monotonic() - t0

    if wf.aggregated is None:
        return {
            "strategy": strategy_name,
            "pair": pair,
            "timeframe": tf,
            "status": "no_metrics",
            "elapsed_s": round(elapsed, 1),
        }

    a = wf.aggregated
    return {
        "name": f"{strategy_name}/{pair}/{tf}",
        "strategy": strategy_name,
        "pair": pair,
        "timeframe": tf,
        "data_path": f"tick_db:{pair}:{tf}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "complete",
        "windows_passed": int(a.windows_passed),
        "windows_total": int(a.total_windows),
        "go_nogo": bool(wf.go_nogo),
        "mean_profit_factor": float(a.mean_profit_factor),
        "mean_win_rate": float(a.mean_win_rate),
        "mean_sharpe": float(a.mean_sharpe_ratio),
        "mean_max_drawdown": float(a.mean_max_drawdown),
        "mean_trade_count": float(a.mean_trade_count),
        "mean_total_pnl": float(a.mean_total_pnl),
        "elapsed_s": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(description="Blend walk-forward runner with JSONL output for DSR annotation.")
    parser.add_argument(
        "--pairs",
        default="XAUUSD",
        help="Comma-separated pairs (default: XAUUSD)",
    )
    parser.add_argument(
        "--strategies",
        default=",".join(STRATEGY_FACTORIES.keys()),
        help="Comma-separated strategy names (default: all 6)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: reports/blend-walkforward-YYYY-MM-DD/)",
    )
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",")]
    strategies = [s.strip() for s in args.strategies.split(",")]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_dir = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "reports" / f"blend-walkforward-{today}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[blend-wf] Output dir: {output_dir}")
    print(f"[blend-wf] Pairs: {pairs}")
    print(f"[blend-wf] Strategies: {strategies}")
    print()

    total_results = []
    for strat in strategies:
        jsonl_path = output_dir / f"{strat}_focused_results.jsonl"
        lines_written = 0
        with open(jsonl_path, "w") as f:  # truncates: same-day rerun overwrites
            for pair in pairs:
                print(f"  [{strat} / {pair}] running walk-forward...", end=" ", flush=True)
                try:
                    result = run_one(strat, pair)
                except Exception as e:
                    result = {
                        "strategy": strat,
                        "pair": pair,
                        "status": "error",
                        "error": str(e)[:200],
                    }
                    print(f"ERROR ({e})", end=" ")
                if result is None:
                    print("skipped (pair restriction)")
                    continue
                f.write(json.dumps(result) + "\n")
                f.flush()
                lines_written += 1
                total_results.append(result)
                status = result.get("status", "unknown")
                sharpe = result.get("mean_sharpe", "N/A")
                elapsed = result.get("elapsed_s", "?")
                print(f"{status} (sharpe={sharpe}, {elapsed}s)")
        print(f"  → {jsonl_path} ({lines_written} entries)")
        print()

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in total_results:
        if r.get("status") == "complete":
            tier_marker = "✅" if r.get("go_nogo") else "❌"
            print(
                f"  {tier_marker} {r['strategy']:30s} {r['pair']:8s} "
                f"sharpe={r['mean_sharpe']:7.2f}  "
                f"PF={r['mean_profit_factor']:6.2f}  "
                f"WR={r['mean_win_rate']:.1%}  "
                f"DD={r['mean_max_drawdown']:.2%}  "
                f"trades={r['mean_trade_count']:.0f}  "
                f"windows={r['windows_passed']}/{r['windows_total']}"
            )
        elif r.get("status") == "skipped":
            print(f"  ⏭️  {r['strategy']:30s} {r['pair']:8s} skipped ({r.get('reason', 'unknown')})")
        else:
            print(f"  ❓ {r.get('strategy', '?'):30s} {r.get('pair', '?'):8s} {r.get('status', '?')}")

    # DSR command hint
    # Count actual candidates produced (valid results only, not skipped)
    valid_candidates = sum(1 for r in total_results if r.get("status") == "complete")
    jsonl_flags = " ".join(f"--jsonl {output_dir / f'{s}_focused_results.jsonl'}" for s in strategies)
    # n_trials should reflect actual independent candidates tested
    n_trials = max(valid_candidates, 6)
    print()
    print("DSR command:")
    print(
        f"  python3 scripts/quant/deflated_sharpe.py \\\n"
        f"    {jsonl_flags} \\\n"
        f"    --n-trials {max(n_trials, 48)} \\\n"
        f"    --report {output_dir}/dsr_report.md \\\n"
        f"    --json-out {output_dir}/dsr_summary.json"
    )


if __name__ == "__main__":
    main()
