"""Phase 2D: Full-Dataset Walk-Forward + Signal Quality Gate.

AYUAA-211 — Third iteration of ICT/SMC strategy evaluation.

Fixes from Phase 2C NO-GO:
  1. Full dataset (17,877 H1 bars EURUSD, all GBPUSD)
  2. Signal Quality Gate (min 3 ICT/SMC components: OB + FVG + MSS)
  3. Confidence threshold lowered to 0.40
  4. 5 walk-forward windows with 60/15/15/10 anchored splits

Performance: Signals are computed once over the full dataset, then
split into walk-forward windows. This avoids redundant O(n^2) recomputation.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .data_loader import CsvDataLoader
from .engine import Bar, determine_session
from .ict_smc.confluence_engine import SignalConfluenceEngine
from .ict_smc.models import ConfluenceSignal, ICTMarketState
from .selective_pairing import PairingConfig, SelectivePairingHarness
from .trade_management.session_filter import SessionFilter

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "forex" / "historical"

ACCEPTANCE_CRITERIA = {
    "win_rate": 0.55,
    "profit_factor": 1.5,
    "max_drawdown": 0.05,
    "sharpe_ratio": 0.5,
    "min_oos_trades": 100,
    "min_profitable_windows": 3,
}


@dataclass
class WindowResult:
    window_id: int
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    train_bars: int = 0
    test_bars: int = 0
    test_trades: int = 0
    test_win_rate: float = 0.0
    test_profit_factor: float = 0.0
    test_max_drawdown: float = 0.0
    test_sharpe_ratio: float = 0.0
    test_total_pnl: float = 0.0
    test_expectancy: float = 0.0
    passed: bool = False


@dataclass
class EvalResult:
    pair_name: str
    config: dict
    windows: list[WindowResult] = field(default_factory=list)
    total_oos_trades: int = 0
    mean_win_rate: float = 0.0
    mean_profit_factor: float = 0.0
    mean_max_drawdown: float = 0.0
    mean_sharpe_ratio: float = 0.0
    profitable_windows: int = 0
    go_nogo: bool = False
    passed_criteria: dict[str, bool] = field(default_factory=dict)


def compute_all_signals(
    bars: list[Bar],
    config: PairingConfig,
    h4_bars: list[Bar] | None = None,
) -> list[tuple[int, ConfluenceSignal]]:
    """Compute signals for all bars, returning (bar_index, signal) pairs.

    Processes bars sequentially from the start so each bar has full historical
    context. This is the only correct way to compute ICT/SMC signals since
    every detector depends on all prior bars.
    """
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

    session_filter = SessionFilter(
        enabled=True,
        allow_entry_sessions=config.allow_entry_sessions,
    )

    results: list[tuple[int, ConfluenceSignal]] = []
    min_bars = config.min_bars_before_signal

    for i in range(min_bars, len(bars)):
        bar = bars[i]

        entry_check = session_filter.check_entry(bar)
        if not entry_check.allow_entry:
            continue

        state = ICTMarketState(bars=bars[: i + 1])
        state.current_session = determine_session(bar.time)

        h4_slice = None
        if h4_bars is not None:
            h4_slice = [b for b in h4_bars if b.time <= bar.time]
            if not h4_slice:
                h4_slice = None

        signal = engine.evaluate(state, h4_slice)
        if signal is None:
            continue

        if config.min_confluence > 0 and signal.confluence_count < config.min_confluence:
            continue

        results.append((i, signal))

    return results


def compute_window_signals(
    full_bars: list[Bar],
    window_train_end: int,
    window_test_end: int,
    config: PairingConfig,
    h4_bars: list[Bar] | None = None,
) -> list[tuple[int, ConfluenceSignal]]:
    """Compute signals for a walk-forward window.

    Processes all bars from 0 to window_test_end so detectors have full
    historical context. Returns only signals in the test portion
    (after the gap between train_end and test_start).
    """
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

    session_filter = SessionFilter(
        enabled=True,
        allow_entry_sessions=config.allow_entry_sessions,
    )

    results: list[tuple[int, ConfluenceSignal]] = []
    min_bars = config.min_bars_before_signal

    gap_start = window_train_end

    for i in range(min_bars, window_test_end):
        bar = full_bars[i]

        entry_check = session_filter.check_entry(bar)
        if not entry_check.allow_entry:
            continue

        if i < gap_start:
            continue

        state = ICTMarketState(bars=full_bars[: i + 1])
        state.current_session = determine_session(bar.time)

        h4_slice = None
        if h4_bars is not None:
            h4_slice = [b for b in h4_bars if b.time <= bar.time]
            if not h4_slice:
                h4_slice = None

        signal = engine.evaluate(state, h4_slice)
        if signal is None:
            continue

        if config.min_confluence > 0 and signal.confluence_count < config.min_confluence:
            continue

        results.append((i, signal))

    return results


def anchored_walk_forward_indices(
    total_bars: int,
    n_windows: int = 5,
    train_pct: float = 0.60,
    gap_pct: float = 0.15,
    test_pct: float = 0.15,
    step_pct: float = 0.10,
) -> list[tuple[int, int, int, int]]:
    """Return (train_start, train_end, test_start, test_end) index tuples.

    Rolling walk-forward: 5 non-overlapping windows across the full dataset.
    Each window has its own train/gap/test segment computed from the data
    available up to that window's end point.
    """
    step = total_bars // (n_windows + 1)
    if step < 200:
        step = 200

    results = []

    for w in range(n_windows):
        window_end = min(step * (w + 2), total_bars)
        window_start = step * w
        window_size = window_end - window_start

        train_end_idx = window_start + int(window_size * train_pct)
        gap_end_idx = train_end_idx + int(window_size * gap_pct)
        test_start_idx = gap_end_idx
        test_end_idx = window_end

        if test_end_idx > total_bars:
            test_end_idx = total_bars

        train_size = train_end_idx - window_start
        test_size = test_end_idx - test_start_idx

        if train_size < 100 or test_size < 50:
            if window_end >= total_bars:
                break
            continue

        results.append((window_start, train_end_idx, test_start_idx, test_end_idx))

    return results


def run_phase2d_eval(
    pair_name: str,
    bars: list[Bar],
    config: PairingConfig,
    h4_bars: list[Bar] | None = None,
) -> EvalResult:
    """Run Phase 2D walk-forward evaluation."""
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
        },
    )

    for w_idx, (train_start, train_end, test_start, test_end) in enumerate(splits):
        print(
            f"  Window {w_idx}: computing signals for bars 0..{test_end}...",
            end=" ",
            flush=True,
        )
        t0 = time.time()
        test_signals = compute_window_signals(bars, train_end, test_end, config, h4_bars)
        t1 = time.time()
        print(f"{len(test_signals)} test signals in {t1 - t0:.1f}s")

        test_bars = bars[test_start:test_end]

        if not test_bars:
            continue

        metrics = harness._run_backtest(test_bars, [sig for _, sig in test_signals])

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

    ac = ACCEPTANCE_CRITERIA
    result.passed_criteria = {
        "win_rate": result.mean_win_rate >= ac["win_rate"],
        "profit_factor": result.mean_profit_factor >= ac["profit_factor"],
        "max_drawdown": result.mean_max_drawdown <= ac["max_drawdown"],
        "sharpe_ratio": result.mean_sharpe_ratio >= ac["sharpe_ratio"],
        "min_oos_trades": total_trades >= ac["min_oos_trades"],
        "min_profitable_windows": result.profitable_windows >= ac["min_profitable_windows"],
    }

    result.go_nogo = all(result.passed_criteria.values())

    return result


def format_result(result: EvalResult) -> str:
    lines = [
        "=" * 80,
        f"Phase 2D Evaluation: {result.pair_name}",
        "=" * 80,
        "",
        f"Config: min_conf={result.config.get('min_confidence')}, "
        f"min_confl={result.config.get('min_confluence')}, "
        f"SL={result.config.get('sl_atr_multiplier')}x ATR, "
        f"TP={result.config.get('tp1_rr')}/{result.config.get('tp2_rr')}/{result.config.get('tp3_rr')} RR",
        "",
        f"{'Window':>6} {'Train':>14} {'Test':>14} {'Trades':>7} {'WR':>7} {'PF':>7} {'DD':>7} {'Sharpe':>7} {'PnL':>10} {'GO?':>5}",  # noqa: E501
        "-" * 100,
    ]

    for w in result.windows:
        lines.append(
            f"{w.window_id:>6} {w.train_start:>14} {w.test_start:>14} "
            f"{w.test_trades:>7} {w.test_win_rate:>6.1f}% {w.test_profit_factor:>7.2f} "
            f"{w.test_max_drawdown:>6.2f}% {w.test_sharpe_ratio:>7.2f} "
            f"{w.test_total_pnl:>9.2f} {'YES' if w.passed else 'NO':>5}"
        )

    lines.extend(
        [
            "",
            f"Total OOS Trades: {result.total_oos_trades}",
            f"Mean Win Rate: {result.mean_win_rate:.1f}%",
            f"Mean Profit Factor: {result.mean_profit_factor:.2f}",
            f"Mean Max Drawdown: {result.mean_max_drawdown:.2f}%",
            f"Mean Sharpe Ratio: {result.mean_sharpe_ratio:.2f}",
            f"Profitable Windows: {result.profitable_windows}/{len(result.windows)}",
            "",
            "Acceptance Criteria:",
        ]
    )

    for criterion, passed in result.passed_criteria.items():
        status = "PASS" if passed else "FAIL"
        lines.append(f"  {criterion}: {status}")

    verdict = "GO" if result.go_nogo else "NO-GO"
    lines.extend(["", f"VERDICT: {verdict}", "=" * 80])
    return "\n".join(lines)


def main():
    loader = CsvDataLoader()

    print("Loading data...")
    eurusd_h1 = loader.load(str(DATA_DIR / "EURUSD_H1.csv"))
    eurusd_h4 = loader.load(str(DATA_DIR / "EURUSD_H4.csv"))
    gbpusd_h1 = loader.load(str(DATA_DIR / "GBPUSD_H1.csv"))
    gbpusd_h4 = loader.load(str(DATA_DIR / "GBPUSD_H4.csv"))

    print(f"EURUSD H1: {len(eurusd_h1)} bars ({eurusd_h1[0].time} to {eurusd_h1[-1].time})")
    print(f"GBPUSD H1: {len(gbpusd_h1)} bars ({gbpusd_h1[0].time} to {gbpusd_h1[-1].time})")

    config = PairingConfig(
        min_confidence=0.40,
        min_confluence=3,
        sl_atr_multiplier=1.5,
        tp1_rr=1.0,
        tp2_rr=2.0,
        tp3_rr=3.0,
        starting_balance=10000.0,
        risk_per_trade_pct=0.005,
        max_open_trades=3,
        max_total_drawdown_pct=0.05,
        max_daily_drawdown_pct=0.02,
    )

    combinations = [
        ("all_ict_smc (EURUSD)", eurusd_h1, eurusd_h4),
        ("all_ict_smc (GBPUSD)", gbpusd_h1, gbpusd_h4),
    ]

    results: list[EvalResult] = []

    for name, h1_bars, h4_bars in combinations:
        print(f"\n{'=' * 60}")
        print(f"Evaluating: {name}")
        print(f"{'=' * 60}")

        result = run_phase2d_eval(name, h1_bars, config, h4_bars)
        print(format_result(result))
        results.append(result)

    summary_lines = [
        "",
        "=" * 100,
        "PHASE 2D SUMMARY",
        "=" * 100,
        f"{'Combination':<30} {'OOS Trades':>10} {'WR':>7} {'PF':>7} {'DD':>7} {'Sharpe':>7} {'Win':>6} {'GO?':>5}",
        "-" * 100,
    ]

    go_combos = []
    for r in results:
        wr_str = f"{r.mean_win_rate:.1f}%"
        dd_str = f"{r.mean_max_drawdown:.1f}%"
        verdict = "GO" if r.go_nogo else "NO"
        summary_lines.append(
            f"{r.pair_name:<30} {r.total_oos_trades:>10} {wr_str:>7} "
            f"{r.mean_profit_factor:>7.2f} {dd_str:>7} "
            f"{r.mean_sharpe_ratio:>7.2f} "
            f"{r.profitable_windows}/{len(r.windows):>5} {verdict:>5}"
        )
        if r.go_nogo:
            go_combos.append(r)

    summary_lines.append("-" * 100)

    if go_combos:
        summary_lines.append(f"\n{len(go_combos)} combination(s) PASS all acceptance criteria!")
        for gc in go_combos:
            summary_lines.append(f"  - {gc.pair_name}")
    else:
        summary_lines.append("\nNO combinations pass all acceptance criteria.")

    summary_text = "\n".join(summary_lines)
    print(summary_text)

    output_path = Path(__file__).resolve().parents[3] / "data" / "forex" / "phase2d_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_data = {
        "config": {
            "min_confidence": 0.40,
            "min_confluence": 3,
            "n_windows": 5,
            "acceptance_criteria": ACCEPTANCE_CRITERIA,
        },
        "results": [
            {
                "pair_name": r.pair_name,
                "total_oos_trades": r.total_oos_trades,
                "mean_win_rate": r.mean_win_rate,
                "mean_profit_factor": r.mean_profit_factor,
                "mean_max_drawdown": r.mean_max_drawdown,
                "mean_sharpe_ratio": r.mean_sharpe_ratio,
                "profitable_windows": r.profitable_windows,
                "go_nogo": r.go_nogo,
                "passed_criteria": r.passed_criteria,
                "windows": [
                    {
                        "window_id": w.window_id,
                        "train_start": w.train_start,
                        "train_end": w.train_end,
                        "test_start": w.test_start,
                        "test_end": w.test_end,
                        "train_bars": w.train_bars,
                        "test_bars": w.test_bars,
                        "test_trades": w.test_trades,
                        "test_win_rate": w.test_win_rate,
                        "test_profit_factor": w.test_profit_factor,
                        "test_max_drawdown": w.test_max_drawdown,
                        "test_sharpe_ratio": w.test_sharpe_ratio,
                        "test_total_pnl": w.test_total_pnl,
                        "passed": w.passed,
                    }
                    for w in r.windows
                ],
            }
            for r in results
        ],
    }
    output_path.write_text(json.dumps(output_data, indent=2, default=str))
    print(f"\nResults saved to {output_path}")

    return 0 if go_combos else 1


if __name__ == "__main__":
    sys.exit(main())
