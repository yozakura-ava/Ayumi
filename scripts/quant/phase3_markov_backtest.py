"""AYUAA-401 Phase 3 — Markov sizing backtest gate.

Runs FIXED_FRACTIONAL vs MARKOV_ADAPTIVE sizing on three pairs (GBPUSD,
EURUSD, USDJPY) for two periods:

  * Baseline period: full available data, capped at 2020-2024.
  * Out-of-sample period: 2025-2026.

For each pair/period/sizing-mode it records:
  - Sharpe ratio, max drawdown, win rate, total trades, profit factor
  - Average win / average loss (for risk:reward ratio)
  - Per-bar Markov confidence and regime.py confidence (for Pearson)

The Pearson correlation between Markov confidence and regime.py confidence
is computed over the OOS window only — that is where the filter is
fully "warm" (≥ min_history transitions observed) and the comparison
is meaningful.

Outputs JSON results to docs/research/AYUAA-401-phase3-results.json
and prints a summary table.

Run:
  PYTHONPATH=src/forex_bot .venv/bin/python scripts/quant/phase3_markov_backtest.py
"""

from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Make src/forex_bot importable as top-level packages (backtest, quant, etc.)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "forex_bot"))

from backtest.data_loader import CsvDataLoader  # noqa: E402, I001
from backtest.engine import Bar  # noqa: E402
from backtest.enhanced_engine import EnhancedBacktestEngine  # noqa: E402
from backtest.strategy_legacy import MACrossStrategy  # noqa: E402
from backtest.trade_management import TradeManagementConfig  # noqa: E402
from backtest.types import BacktestConfig  # noqa: E402
from quant.config import (  # noqa: E402
    MarkovConfig,
    PositionSizingConfig,
    QuantConfig,
    RegimeConfig,
    SizingMode,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase3")
logger.setLevel(logging.INFO)

DATA_DIR = ROOT / "data" / "forex" / "historical"
RESULTS_DIR = ROOT / "docs" / "research"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Periods (UTC dates). The 2020-2024 baseline matches the task spec.
# OOS is the train-period-blind 2025-2026 window.
BASELINE_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
BASELINE_END = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
OOS_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
OOS_END = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

PAIRS = ["GBPUSD", "EURUSD", "USDJPY"]

# Markov tuning — exactly the documented Phase 2 contract.
MARKOV_CONFIG = MarkovConfig(
    enabled=True,
    min_history=100,
    min_multiplier=0.5,
    max_multiplier=1.3,
)


@dataclass
class RunResult:
    pair: str
    period: str
    sizing_mode: str
    total_trades: int
    win_rate: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    risk_reward: float
    total_pnl: float
    total_pnl_pct: float
    starting_balance: float
    ending_balance: float
    # Confidence samples — empty for FIXED_FRACTIONAL since the filter is off.
    markov_confidence_samples: list[float]
    regime_confidence_samples: list[float]


@dataclass
class PairResult:
    pair: str
    baseline_ff: RunResult
    baseline_markov: RunResult
    oos_ff: RunResult
    oos_markov: RunResult
    sharpe_improvement_baseline: float
    sharpe_improvement_oos: float
    dd_delta_baseline: float  # markov - ff, in pct points
    dd_delta_oos: float
    pearson_r_markov_vs_regime: float  # OOS only
    pearson_n: int


def _filter_bars(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    return [b for b in bars if start <= b.time <= end]


def _make_quant_config(mode: SizingMode, *, markov_enabled: bool) -> QuantConfig:
    """Build a QuantConfig pinning sizing mode + regime/correlation on.

    Regime/correlation are kept enabled so the Markov filter has the
    regime labels it needs. Markov is on only for MARKOV_ADAPTIVE runs.
    """
    return QuantConfig(
        regime=RegimeConfig(enabled=True),
        correlation=QuantConfig().correlation,  # default enabled
        position_sizing=PositionSizingConfig(
            enabled=True,
            mode=mode,
            risk_pct=1.0,
        ),
        walk_forward=QuantConfig().walk_forward,  # default off — we don't
        # need W-F here, we just want a single-run pipeline
        markov=replace(MARKOV_CONFIG, enabled=markov_enabled),
    )


def _run_backtest(
    bars: list[Bar],
    pair: str,
    quant_config: QuantConfig,
    sample_confidences: bool,
) -> tuple[object, list[float], list[float]]:
    """Run a single backtest and optionally sample per-bar confidences.

    The confidence sampling needs to be done *outside* the engine because
    ``EnhancedBacktestEngine`` does not expose its internal Markov state.
    We replicate the bar-feeding loop here against the pipeline to record
    Markov persistence and regime.py combined confidence per bar. The
    pipeline we build is the *same* one the engine uses; we feed it the
    same ATR/high/low/close sequence, so the recorded values exactly
    mirror what the engine saw when sizing each trade.
    """
    config = BacktestConfig(
        starting_balance=10_000.0,
        pair=pair,
        max_open_trades=1,
        min_bars_before_signal=50,
        # Disable the engine's own DD/daily-loss circuit breakers so we
        # can compare sizing modes apples-to-apples. Markov-vs-FF must
        # NOT be confounded by one of them tripping a daily loss and
        # stopping early. The DD we care about is the realised drawdown
        # in the equity curve, which BacktestMetrics computes from the
        # trade record.
        max_daily_drawdown_pct=1.0,
        max_total_drawdown_pct=1.0,
    )
    strategy = MACrossStrategy(fast_period=5, slow_period=13, atr_multiplier=2.0)
    tm = TradeManagementConfig(pair=pair)
    engine = EnhancedBacktestEngine(config, [strategy], tm_config=tm, quant_config=quant_config)
    metrics = engine.run_all_strategies(bars)  # type: ignore[func-returns-value]
    metrics_obj = metrics["MA Crossover"].metrics  # type: ignore[index]

    markov_samples: list[float] = []
    regime_samples: list[float] = []

    if sample_confidences and engine._quant_pipeline is not None:
        markov_samples, regime_samples = _sample_confidences(
            bars=bars,
            pair=pair,
            quant_config=quant_config,
        )

    return metrics_obj, markov_samples, regime_samples


def _sample_confidences(
    bars: list[Bar],
    pair: str,
    quant_config: QuantConfig,
) -> tuple[list[float], list[float]]:
    """Replay bars through the pipeline and sample Markov/regime confidence.

    This does NOT trade; it only classifies each bar so we can record
    the two confidence values that the sizing layer consumed.
    """
    from quant.pipeline import QuantPipeline  # local import to avoid cycle

    pipeline = QuantPipeline(quant_config)

    # The pipeline keeps bounded deques internally; we just feed it the
    # same high/low/close/ATR sequence the engine would feed it.
    atr_values = _atr_series(bars, period=14)

    markov_samples: list[float] = []
    regime_samples: list[float] = []
    for i, bar in enumerate(bars):
        atr = atr_values[i] if atr_values[i] > 0 else 0.0001
        pipeline.update_bars(
            high=bar.high,
            low=bar.low,
            close=bar.close,
            atr=atr,
        )
        # Markov confidence = diagonal of transition matrix for current state
        if pipeline._markov_filter is not None and pipeline._markov_filter.is_ready():
            current_state = pipeline._get_current_markov_state()
            if current_state is not None:
                markov_samples.append(float(pipeline._markov_filter.confidence(current_state)))
            else:
                markov_samples.append(0.0)
        else:
            markov_samples.append(0.0)

        # Regime.py combined confidence — same call _check_regime makes
        if pipeline._config.regime.enabled:
            regime_confidence = pipeline._check_regime(bar.time)
            regime_samples.append(float(regime_confidence))
        else:
            regime_samples.append(0.0)

    return markov_samples, regime_samples


def _atr_series(bars: list[Bar], period: int = 14) -> list[float]:
    """ATR computed identically to MarketState.atr — last-N-bar mean TR."""
    out = [0.0001] * len(bars)
    if len(bars) < period:
        return out
    for i in range(period - 1, len(bars)):
        tr_sum = 0.0
        for j in range(i - period + 1, i + 1):
            if j > 0:
                tr = max(
                    bars[j].high - bars[j].low,
                    max(
                        abs(bars[j].high - bars[j - 1].close),
                        abs(bars[j].low - bars[j - 1].close),
                    ),
                )
                tr_sum += tr
        out[i] = tr_sum / period
    return out


def _risk_reward(metrics: object) -> float:
    """R:R = avg_win / |avg_loss|. Falls back to 0 if no losses."""
    avg_win = getattr(metrics, "avg_win", 0.0) or 0.0
    avg_loss = getattr(metrics, "avg_loss", 0.0) or 0.0
    if avg_loss == 0.0:
        return 0.0 if avg_win == 0.0 else float("inf")
    return avg_win / abs(avg_loss)


def _to_run_result(
    pair: str,
    period: str,
    mode: str,
    metrics: object,
    markov_samples: list[float],
    regime_samples: list[float],
) -> RunResult:
    return RunResult(
        pair=pair,
        period=period,
        sizing_mode=mode,
        total_trades=int(getattr(metrics, "total_trades", 0)),
        win_rate=float(getattr(metrics, "win_rate", 0.0)),
        sharpe_ratio=float(getattr(metrics, "sharpe_ratio", 0.0)),
        max_drawdown_pct=float(getattr(metrics, "max_drawdown_pct", 0.0)),
        profit_factor=float(getattr(metrics, "profit_factor", 0.0)),
        avg_win=float(getattr(metrics, "avg_win", 0.0) or 0.0),
        avg_loss=float(getattr(metrics, "avg_loss", 0.0) or 0.0),
        risk_reward=_risk_reward(metrics),
        total_pnl=float(getattr(metrics, "total_pnl", 0.0)),
        total_pnl_pct=float(getattr(metrics, "total_pnl_pct", 0.0)),
        starting_balance=float(getattr(metrics, "starting_balance", 0.0)),
        ending_balance=float(getattr(metrics, "ending_balance", 0.0)),
        markov_confidence_samples=markov_samples,
        regime_confidence_samples=regime_samples,
    )


def _pearson(xs: list[float], ys: list[float]) -> tuple[float, int]:
    n = min(len(xs), len(ys))
    if n < 3:
        return float("nan"), n
    a = np.array(xs[:n], dtype=float)
    b = np.array(ys[:n], dtype=float)
    if np.std(a) == 0 or np.std(b) == 0:
        return float("nan"), n
    r = float(np.corrcoef(a, b)[0, 1])
    return r, n


def main() -> int:
    loader = CsvDataLoader(csv_dir=str(DATA_DIR))

    # Load all pair data once
    pair_bars: dict[str, list[Bar]] = {}
    for pair in PAIRS:
        path = DATA_DIR / f"{pair}_H1.csv"
        if not path.exists():
            logger.warning("Missing CSV for %s at %s", pair, path)
            continue
        bars = loader.load(str(path))
        pair_bars[pair] = bars
        logger.info(
            "Loaded %s H1: %d bars, %s → %s",
            pair,
            len(bars),
            bars[0].time.isoformat() if bars else "?",
            bars[-1].time.isoformat() if bars else "?",
        )

    out: dict[str, PairResult] = {}

    for pair in PAIRS:
        if pair not in pair_bars:
            logger.error("No data for %s, skipping", pair)
            continue
        bars = pair_bars[pair]
        # Period slices
        baseline_bars = _filter_bars(bars, BASELINE_START, BASELINE_END)
        oos_bars = _filter_bars(bars, OOS_START, OOS_END)
        logger.info(
            "%s: baseline=%d bars (2020-2024), oos=%d bars (2025-2026)",
            pair,
            len(baseline_bars),
            len(oos_bars),
        )
        if len(baseline_bars) < 200 or len(oos_bars) < 200:
            logger.warning(
                "%s: insufficient bars (baseline=%d, oos=%d) — reporting anyway",
                pair,
                len(baseline_bars),
                len(oos_bars),
            )

        # --- Baseline 2020-2024 ---
        cfg_ff = _make_quant_config(SizingMode.FIXED_FRACTIONAL, markov_enabled=False)
        cfg_mkv = _make_quant_config(SizingMode.MARKOV_ADAPTIVE, markov_enabled=True)

        m_ff, _, _ = _run_backtest(baseline_bars, pair, cfg_ff, sample_confidences=False)
        m_mkv, ms, rs = _run_backtest(baseline_bars, pair, cfg_mkv, sample_confidences=True)

        rr_ff = _to_run_result(pair, "2020-2024", "FIXED_FRACTIONAL", m_ff, [], [])
        rr_mkv = _to_run_result(pair, "2020-2024", "MARKOV_ADAPTIVE", m_mkv, ms, rs)

        # --- OOS 2025-2026 ---
        m_oos_ff, _, _ = _run_backtest(oos_bars, pair, cfg_ff, sample_confidences=False)
        m_oos_mkv, ms_oos, rs_oos = _run_backtest(oos_bars, pair, cfg_mkv, sample_confidences=True)
        rr_oos_ff = _to_run_result(pair, "2025-2026", "FIXED_FRACTIONAL", m_oos_ff, [], [])
        rr_oos_mkv = _to_run_result(pair, "2025-2026", "MARKOV_ADAPTIVE", m_oos_mkv, ms_oos, rs_oos)

        sharpe_imp_baseline = rr_mkv.sharpe_ratio - rr_ff.sharpe_ratio
        sharpe_imp_oos = rr_oos_mkv.sharpe_ratio - rr_oos_ff.sharpe_ratio
        dd_delta_baseline = rr_mkv.max_drawdown_pct - rr_ff.max_drawdown_pct
        dd_delta_oos = rr_oos_mkv.max_drawdown_pct - rr_oos_ff.max_drawdown_pct

        # Pearson: OOS only (filter is warm there)
        pearson_r, n_pearson = _pearson(ms_oos, rs_oos)

        out[pair] = PairResult(
            pair=pair,
            baseline_ff=rr_ff,
            baseline_markov=rr_mkv,
            oos_ff=rr_oos_ff,
            oos_markov=rr_oos_mkv,
            sharpe_improvement_baseline=sharpe_imp_baseline,
            sharpe_improvement_oos=sharpe_imp_oos,
            dd_delta_baseline=dd_delta_baseline,
            dd_delta_oos=dd_delta_oos,
            pearson_r_markov_vs_regime=pearson_r,
            pearson_n=n_pearson,
        )

    # Print summary table
    print()
    print("=" * 100)
    print("AYUAA-401 Phase 3 — Markov sizing backtest gate")
    print("=" * 100)
    for pair, pr in out.items():
        print(f"\n## {pair}")
        print(
            f"{'Period':<11} {'Mode':<18} {'Trades':>7} {'Win%':>7} "
            f"{'Sharpe':>8} {'MaxDD%':>8} {'PF':>7} {'R:R':>7} {'PnL%':>8}"
        )
        for rr in (
            pr.baseline_ff,
            pr.baseline_markov,
            pr.oos_ff,
            pr.oos_markov,
        ):
            rr_str = "inf" if math.isinf(rr.risk_reward) else f"{rr.risk_reward:.2f}"
            print(
                f"{rr.period:<11} {rr.sizing_mode:<18} {rr.total_trades:>7d} "
                f"{rr.win_rate * 100:>6.1f}% {rr.sharpe_ratio:>8.3f} "
                f"{rr.max_drawdown_pct:>7.2f}% {rr.profit_factor:>7.2f} "
                f"{rr_str:>7} {rr.total_pnl_pct:>7.2f}%"
            )
        print(f"  Sharpe Δ (baseline): {pr.sharpe_improvement_baseline:+.3f}  DD Δ: {pr.dd_delta_baseline:+.2f}%")
        print(f"  Sharpe Δ (OOS):      {pr.sharpe_improvement_oos:+.3f}  DD Δ: {pr.dd_delta_oos:+.2f}%")
        print(f"  Pearson r (Markov vs regime confidence, OOS n={pr.pearson_n}): {pr.pearson_r_markov_vs_regime:+.3f}")

    # Save JSON
    serialised = {
        pair: {
            "baseline_ff": asdict(pr.baseline_ff),
            "baseline_markov": asdict(pr.baseline_markov),
            "oos_ff": asdict(pr.oos_ff),
            "oos_markov": asdict(pr.oos_markov),
            "sharpe_improvement_baseline": pr.sharpe_improvement_baseline,
            "sharpe_improvement_oos": pr.sharpe_improvement_oos,
            "dd_delta_baseline": pr.dd_delta_baseline,
            "dd_delta_oos": pr.dd_delta_oos,
            "pearson_r_markov_vs_regime": pr.pearson_r_markov_vs_regime,
            "pearson_n": pr.pearson_n,
        }
        for pair, pr in out.items()
    }
    out_path = RESULTS_DIR / "AYUAA-401-phase3-results.json"
    with open(out_path, "w") as f:
        json.dump(serialised, f, indent=2)
    print(f"\nResults JSON saved → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # noqa: W292
