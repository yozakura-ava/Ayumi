"""AYUAA-253 — ICT/SMC Last-Chance Test on EURUSD M15.

FINAL ICT/SMC iteration. If NO-GO, ICT is permanently shelved.

Optimized: computes all signals in a single pass over the full dataset,
then splits by walk-forward window test ranges. This avoids the O(n^2)
per-window recomputation that makes M15 infeasible.

M15 tuning rationale (vs H1 defaults):
  swing_lookback:       10  (was 5)   — wider to avoid M15 noise
  bos_threshold:        0.3 (was 0.5) — catch more structure breaks
  freshness_window:     12  (was 5)   — OBs persist longer (5 bars = 75 min)
  lookback (OB):        150 (was 50)  — 50 M15 bars = 12.5h; need 2-3 days
  max_age (FVG):        50  (was 20)  — 20 M15 bars = 5h; FVGs fill slower
  mini_threshold (FVG): 0.0001 (was 0.0003) — smaller M15 candles
  pool_lookback:        200 (was 50)  — liquidity pools need more history
  sweep_validity_bars:  10  (was 5)   — sweeps valid longer on M15
  sweep_wick_ratio:     0.5 (was 0.6) — more lenient
  pd lookback:          96  (was 20)  — 20 bars = 5h; PD needs daily context
  equilibrium_buffer:   0.0001 (was 0.0002) — tighter for M15 ranges
  sl_atr_multiplier:    2.0 (was 1.5) — wider stops for M15 noise
  min_bars_before:      100 (was 30)  — sufficient warm-up

Run:
  cd $AYUMI_ROOT/worktrees/kai/src/forex_bot
  python ../../scripts/run_m15_ict_last_chance.py
"""

import bisect
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader
from backtest.engine import Bar, determine_session
from backtest.ict_smc.confluence_engine import SignalConfluenceEngine
from backtest.ict_smc.fvg import FVGDetector
from backtest.ict_smc.h4_context import H4ContextModule
from backtest.ict_smc.liquidity_sweep import LiquiditySweepDetector
from backtest.ict_smc.market_structure import MarketStructureAnalyzer
from backtest.ict_smc.models import ConfluenceSignal, ICTMarketState
from backtest.ict_smc.order_block import OrderBlockDetector
from backtest.ict_smc.premium_discount import PremiumDiscountClassifier
from backtest.phase2d_eval import (
    ACCEPTANCE_CRITERIA,
    EvalResult,
    WindowResult,
    anchored_walk_forward_indices,
    format_result,
)
from backtest.selective_pairing import PairingConfig, SelectivePairingHarness
from backtest.trade_management.session_filter import SessionFilter

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "forex" / "historical"
REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "m15_ict_last_chance.json"

M15_TUNING = {
    "swing_lookback": 10,
    "bos_threshold": 0.3,
    "freshness_window": 12,
    "ob_lookback": 150,
    "fvg_max_age": 50,
    "fvg_mini_threshold": 0.0001,
    "pool_lookback": 200,
    "sweep_validity_bars": 10,
    "sweep_wick_ratio": 0.5,
    "pd_lookback": 96,
    "pd_equilibrium_buffer": 0.0001,
}


def create_m15_engine(config: PairingConfig) -> SignalConfluenceEngine:
    engine = SignalConfluenceEngine(
        min_confidence=config.min_confidence,
        structure_weight=0.30,
        ob_weight=0.25,
        fvg_weight=0.15,
        sweep_weight=0.15,
        pd_weight=0.10,
        session_weight=0.05,
        h4_weight=0.15,
        default_sl_multiplier=config.sl_atr_multiplier,
        tp1_rr=config.tp1_rr,
        tp2_rr=config.tp2_rr,
        tp3_rr=config.tp3_rr,
    )
    engine._structure_analyzer = MarketStructureAnalyzer(
        swing_lookback=M15_TUNING["swing_lookback"],
        bos_threshold=M15_TUNING["bos_threshold"],
    )
    engine._ob_detector = OrderBlockDetector(
        freshness_window=M15_TUNING["freshness_window"],
        min_body_ratio=0.5,
        overlap_threshold=0.7,
        lookback=M15_TUNING["ob_lookback"],
    )
    engine._fvg_detector = FVGDetector(
        max_age=M15_TUNING["fvg_max_age"],
        mini_threshold=M15_TUNING["fvg_mini_threshold"],
    )
    engine._sweep_detector = LiquiditySweepDetector(
        sweep_wick_ratio=M15_TUNING["sweep_wick_ratio"],
        pool_lookback=M15_TUNING["pool_lookback"],
        sweep_atr_multiplier=1.5,
        sweep_validity_bars=M15_TUNING["sweep_validity_bars"],
    )
    engine._pd_classifier = PremiumDiscountClassifier(
        lookback_period=M15_TUNING["pd_lookback"],
        equilibrium_buffer=M15_TUNING["pd_equilibrium_buffer"],
    )
    engine._h4_module = H4ContextModule()
    return engine


def compute_all_signals_single_pass(
    bars: List[Bar],
    config: PairingConfig,
    h4_bars: Optional[List[Bar]] = None,
) -> List[Tuple[int, ConfluenceSignal]]:
    engine = create_m15_engine(config)
    session_filter = SessionFilter(
        enabled=True,
        allow_entry_sessions=config.allow_entry_sessions,
    )

    h4_times: Optional[List] = None
    if h4_bars is not None:
        h4_times = [b.time for b in h4_bars]

    results: List[Tuple[int, ConfluenceSignal]] = []
    min_bars = config.min_bars_before_signal
    total = len(bars)

    running_bars = list(bars[:min_bars])
    for i in range(min_bars, total):
        if i % 5000 == 0:
            print(
                f"\r    Signal computation: {i}/{total} ({100 * i / total:.0f}%)",
                end="",
                flush=True,
            )

        bar = bars[i]
        running_bars.append(bar)

        entry_check = session_filter.check_entry(bar)
        if not entry_check.allow_entry:
            continue

        state = ICTMarketState(bars=running_bars)
        state.current_session = determine_session(bar.time)

        h4_slice = None
        if h4_times is not None and h4_bars is not None:
            pos = bisect.bisect_right(h4_times, bar.time)
            if pos > 0:
                h4_slice = h4_bars[:pos]

        signal = engine.evaluate(state, h4_slice)
        if signal is None:
            continue

        if config.min_confluence > 0 and signal.confluence_count < config.min_confluence:
            continue

        results.append((i, signal))

    print(f"\r    Signal computation: {total}/{total} (100%)")
    return results


def run_m15_eval(
    pair_name: str,
    bars: List[Bar],
    config: PairingConfig,
    h4_bars: Optional[List[Bar]] = None,
) -> EvalResult:
    harness = SelectivePairingHarness(config)
    splits = anchored_walk_forward_indices(len(bars))

    result = EvalResult(
        pair_name=pair_name,
        config={
            "min_confidence": config.min_confidence,
            "min_confluence": config.min_confluence,
            "n_windows": len(splits),
            "sl_atr_multiplier": config.sl_atr_multiplier,
            "tp1_rr": config.tp1_rr,
            "tp2_rr": config.tp2_rr,
            "tp3_rr": config.tp3_rr,
            "m15_tuning": M15_TUNING,
        },
    )

    print(f"  Computing signals (single pass over {len(bars)} bars)...")
    t0 = time.time()
    all_signals = compute_all_signals_single_pass(bars, config, h4_bars)
    t1 = time.time()
    print(f"    Total signals: {len(all_signals)} in {t1 - t0:.1f}s")
    print()

    for w_idx, (train_start, train_end, test_start, test_end) in enumerate(splits):
        window_sigs = [sig for idx, sig in all_signals if test_start <= idx < test_end]

        test_bars = bars[test_start:test_end]
        if not test_bars:
            continue

        print(
            f"  Window {w_idx}: {len(window_sigs)} signals in test range "
            f"[{test_start}:{test_end}] ({len(test_bars)} bars)",
            flush=True,
        )

        metrics = harness._run_backtest(test_bars, window_sigs)

        wr = metrics.win_rate / 100.0 if metrics.total_trades > 0 else 0.0
        dd = metrics.max_drawdown_pct / 100.0 if metrics.max_drawdown_pct > 0 else 0.0

        window_passed = (
            wr >= ACCEPTANCE_CRITERIA["win_rate"]
            and metrics.profit_factor >= ACCEPTANCE_CRITERIA["profit_factor"]
            and dd <= ACCEPTANCE_CRITERIA["max_drawdown"]
            and metrics.sharpe_ratio >= ACCEPTANCE_CRITERIA["sharpe_ratio"]
            and metrics.total_pnl > 0
        )

        result.windows.append(
            WindowResult(
                window_id=w_idx,
                train_start=str(bars[train_start].time),
                train_end=str(bars[train_end - 1].time),
                test_start=str(test_bars[0].time),
                test_end=str(test_bars[-1].time),
                train_bars=train_end - train_start,
                test_bars=len(test_bars),
                test_trades=metrics.total_trades,
                test_win_rate=metrics.win_rate,
                test_profit_factor=metrics.profit_factor,
                test_max_drawdown=metrics.max_drawdown_pct,
                test_sharpe_ratio=metrics.sharpe_ratio,
                test_total_pnl=metrics.total_pnl,
                test_expectancy=metrics.expectancy,
                passed=window_passed,
            )
        )

    if not result.windows:
        return result

    total_trades = sum(w.test_trades for w in result.windows)
    win_rates = [w.test_win_rate for w in result.windows if w.test_trades > 0]
    pfs = [w.test_profit_factor for w in result.windows if w.test_trades > 0]
    dds = [w.test_max_drawdown for w in result.windows if w.test_trades > 0]
    sharpes = [w.test_sharpe_ratio for w in result.windows if w.test_trades > 0]

    result.total_oos_trades = total_trades
    result.mean_win_rate = sum(win_rates) / len(win_rates) if win_rates else 0.0
    result.mean_profit_factor = sum(pfs) / len(pfs) if pfs else 0.0
    result.mean_max_drawdown = sum(dds) / len(dds) if dds else 0.0
    result.mean_sharpe_ratio = sum(sharpes) / len(sharpes) if sharpes else 0.0
    result.profitable_windows = sum(1 for w in result.windows if w.test_total_pnl > 0)

    passing_wr = sum(1 for w in win_rates if w >= ACCEPTANCE_CRITERIA["win_rate"])
    passing_pf = sum(1 for p in pfs if p >= ACCEPTANCE_CRITERIA["profit_factor"])
    passing_dd = sum(1 for d in dds if d <= ACCEPTANCE_CRITERIA["max_drawdown"])
    passing_sh = sum(1 for s in sharpes if s >= ACCEPTANCE_CRITERIA["sharpe_ratio"])

    result.passed_criteria = {
        "win_rate": passing_wr >= ACCEPTANCE_CRITERIA["min_profitable_windows"],
        "profit_factor": passing_pf >= ACCEPTANCE_CRITERIA["min_profitable_windows"],
        "max_drawdown": passing_dd >= ACCEPTANCE_CRITERIA["min_profitable_windows"],
        "sharpe_ratio": passing_sh >= ACCEPTANCE_CRITERIA["min_profitable_windows"],
        "min_profitable_windows": (result.profitable_windows >= ACCEPTANCE_CRITERIA["min_profitable_windows"]),
    }

    result.go_nogo = all(result.passed_criteria.values())

    return result


def main():
    t0 = time.time()

    print("=" * 70)
    print("  AYUAA-253: ICT/SMC LAST-CHANCE TEST — EURUSD M15")
    print("=" * 70)

    loader = CsvDataLoader()
    m15_bars = loader.load(str(DATA_DIR / "EURUSD_M15.csv"))
    h4_bars = loader.load(str(DATA_DIR / "EURUSD_H4.csv"))

    print(f"  M15 bars: {len(m15_bars)}")
    print(f"  H4 bars:  {len(h4_bars)}")
    print()

    config = PairingConfig(
        min_confidence=0.40,
        min_confluence=3,
        sl_atr_multiplier=2.0,
        tp1_rr=1.0,
        tp2_rr=2.0,
        tp3_rr=3.0,
        starting_balance=10000.0,
        risk_per_trade_pct=0.005,
        max_open_trades=3,
        spread_pips=0.5,
        commission_per_lot=3.50,
        leverage=100,
        max_total_drawdown_pct=0.05,
        max_daily_drawdown_pct=0.02,
        min_bars_before_signal=100,
        allow_entry_sessions=["london", "ny_am", "ny_pm"],
    )

    print("  PairingConfig:")
    print(f"    min_confidence:     {config.min_confidence}")
    print(f"    min_confluence:     {config.min_confluence}")
    print(f"    sl_atr_multiplier:  {config.sl_atr_multiplier}")
    print(f"    min_bars_before:    {config.min_bars_before_signal}")
    print(f"    sessions:           {config.allow_entry_sessions}")
    print(f"    risk_per_trade:     {config.risk_per_trade_pct:.1%}")
    print(f"    max_daily_dd:       {config.max_daily_drawdown_pct:.1%}")
    print(f"    max_total_dd:       {config.max_total_drawdown_pct:.1%}")
    print()

    print("  M15 Component Tuning:")
    print(
        f"    MarketStructure:  swing_lookback={M15_TUNING['swing_lookback']}, bos_threshold={M15_TUNING['bos_threshold']}"  # noqa: E501
    )
    print(f"    OrderBlock:       freshness={M15_TUNING['freshness_window']}, lookback={M15_TUNING['ob_lookback']}")
    print(
        f"    FVG:              max_age={M15_TUNING['fvg_max_age']}, mini_threshold={M15_TUNING['fvg_mini_threshold']}"
    )
    print(
        f"    LiquiditySweep:   pool_lookback={M15_TUNING['pool_lookback']}, validity={M15_TUNING['sweep_validity_bars']}, wick={M15_TUNING['sweep_wick_ratio']}"  # noqa: E501
    )
    print(f"    PremiumDiscount:  lookback={M15_TUNING['pd_lookback']}, buffer={M15_TUNING['pd_equilibrium_buffer']}")
    print()

    print("  Acceptance criteria (per window):")
    for k, v in ACCEPTANCE_CRITERIA.items():
        print(f"    {k}: {v}")
    print()

    result = run_m15_eval("EURUSD_M15", m15_bars, config, h4_bars)

    elapsed = time.time() - t0
    print()
    print(f"  Elapsed: {elapsed:.0f}s ({elapsed / 60:.1f}m)")
    print()
    print(format_result(result))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(asdict(result), indent=2, default=str))
    print(f"\n  Report saved: {REPORT_PATH}")

    print()
    print("=" * 70)
    if result.go_nogo:
        print("  RESULT: GO — ICT/SMC passes walk-forward on EURUSD M15")
    else:
        print("  RESULT: NO-GO — ICT/SMC is permanently shelved")
    print("=" * 70)

    return 0 if result.go_nogo else 1


if __name__ == "__main__":
    sys.exit(main())
