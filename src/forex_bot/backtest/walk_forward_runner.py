from __future__ import annotations

import inspect
import logging
import math
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

from quant.walk_forward import (
    AggregatedMetrics,
    WalkForwardResults,
    WalkForwardValidator,
    WindowMetrics,
    _compute_metrics,
    _mean,
    _std,
    detect_regime_for_window,
)
from signal_engine.risk_sizer import ConfidencePositionSizer

from .engine import BacktestConfig, Bar, get_spread_for_pair
from .multi_strategy_engine import MultiStrategyBacktestEngine
from .strategies import ISignalStrategy

logger = logging.getLogger(__name__)

# --- PF sanitisation & trade-count guardrail (T6) ---

PF_CAP = 99.0
MIN_TRADES_WARNING = 15


def _sanitize_profit_factor(pf: float) -> float:
    """Return a finite profit factor, capping Infinity and replacing NaN.

    When ``gross_loss == 0`` and ``gross_profit > 0`` the raw division yields
    ``Infinity``.  We cap to :data:`PF_CAP` (99.0) so downstream aggregations
    (mean, std) remain meaningful instead of propagating ``inf``.
    """
    if math.isnan(pf):
        return 0.0
    if math.isinf(pf):
        return PF_CAP
    return pf


def _check_trade_count_warning(window_idx: int, trade_count: int) -> bool:
    """``True`` when a walk-forward window has fewer than 15 trades.

    Logs a ``WARNING`` but does **not** fail the window - statistical
    significance is informational only.
    """
    if trade_count < MIN_TRADES_WARNING:
        logger.warning(
            "[WARNING] Window %d: only %d trades (minimum %d recommended for statistical significance)",
            window_idx,
            trade_count,
            MIN_TRADES_WARNING,
        )
        return True
    return False


def _reliability_flag(n: int) -> str:
    """Classify sample reliability for regime aggregation (BQ-508).

    - < 3 samples  -> "exploratory"
    - 3-9 samples   -> "tentative"
    - >= 10 samples  -> "robust"
    """
    if n < 3:
        return "exploratory"
    elif n < 10:
        return "tentative"
    else:
        return "robust"


def aggregate_by_regime(
    per_window: list[WindowMetrics],
) -> dict[str, dict[str, Any]]:
    """Group windows by ``regime_combined`` and compute per-regime stats.

    Returns a dict keyed by regime string, each value containing mean
    win_rate, profit_factor, sharpe_ratio, max_drawdown, sample count,
    and a reliability flag.
    """
    groups: dict[str, list[WindowMetrics]] = defaultdict(list)
    for m in per_window:
        groups[m.regime_combined].append(m)

    result: dict[str, dict[str, Any]] = {}
    for regime, windows in groups.items():
        n = len(windows)
        wr = _mean([w.win_rate for w in windows])
        pf = _mean([w.profit_factor for w in windows])
        sr = _mean([w.sharpe_ratio for w in windows])
        dd = _mean([w.max_drawdown for w in windows])
        result[regime] = {
            "sample_count": n,
            "reliability": _reliability_flag(n),
            "mean_win_rate": round(wr, 6),
            "mean_profit_factor": round(pf, 6),
            "mean_sharpe_ratio": round(sr, 6),
            "mean_max_drawdown": round(dd, 6),
        }
    return result


class SupportsTrain(Protocol):
    def train(self, data: Any) -> None: ...


def run_strategy_walk_forward(
    bars: list[Bar],
    strategy_factory: Callable[[], ISignalStrategy] | Callable[[list[Bar]], ISignalStrategy],
    pair: str,
    n_windows: int = 5,
    train_ratio: float = 0.7,
    val_ratio: float = 0.15,
    overlap_ratio: float = 0.2,
    initial_balance: float = 10000,
    spread_pips: float | None = None,
    commission_per_lot: float | None = None,
    min_confidence: float = 0.30,
    risk_sizer: ConfidencePositionSizer | None = None,
    embargo_bars: int = 0,
) -> WalkForwardResults:
    factory_params = len(inspect.signature(strategy_factory).parameters)

    effective_spread = spread_pips if spread_pips is not None else get_spread_for_pair(pair)

    config = BacktestConfig(
        starting_balance=initial_balance,
        spread_pips=effective_spread,
        commission_per_lot=commission_per_lot if commission_per_lot is not None else 3.5,
        pair=pair,
        min_confidence=min_confidence,
    )

    validator = WalkForwardValidator(
        data=bars,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        overlap_ratio=overlap_ratio,
        embargo_bars=embargo_bars,
    )

    per_window = []
    all_trade_records: list[dict] = []
    for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):  # noqa: B007
        if len(test_bars) < config.min_bars_before_signal:
            window_metrics = _compute_metrics(
                idx, [], initial_balance=initial_balance, bars_in_window=test_bars
            )
            per_window.append(window_metrics)
            continue

        if factory_params == 0:
            strategy = strategy_factory()
        else:
            strategy = strategy_factory(train_bars)

        if hasattr(strategy, "reset") and callable(strategy.reset):
            strategy.reset()

        if hasattr(strategy, "train") and callable(strategy.train) and factory_params > 0:
            strategy.train(train_bars)

        try:
            if risk_sizer is None:
                risk_sizer = ConfidencePositionSizer(account_size=initial_balance)
            engine = MultiStrategyBacktestEngine(config, [strategy], risk_sizer=risk_sizer)
            result = engine.run_all_strategies(test_bars)
            metrics_obj = result[strategy.name].metrics

            trades = []
            for t in metrics_obj.trades:
                if hasattr(t, "profit_loss"):
                    rec = {
                        "pnl": t.profit_loss,
                        "confidence_score": getattr(t, "confidence_score", 0.5),
                        "direction": getattr(t, "direction", None),
                        "window_id": idx,
                        "rationale": getattr(t, "rationale", ""),
                    }
                    trades.append(rec)
                    all_trade_records.append(rec)

            if not trades and metrics_obj.total_trades > 0:
                # Synthetic avg-pnl fallback (card ef11b0dd). Trades here
                # have no per-trade timing; pass bars_in_window anyway so
                # the returns-based Sharpe formula degrades gracefully
                # (std_return=0 short-circuits to sharpe=0.0 rather than
                # dividing by zero).
                trades = [
                    {"pnl": metrics_obj.total_pnl / metrics_obj.total_trades} for _ in range(metrics_obj.total_trades)
                ]
        except ValueError:
            logger.warning("Trade extraction failed for window %d, skipping", idx)
            raise

        window_metrics = _compute_metrics(
            idx, trades, initial_balance=initial_balance, bars_in_window=test_bars
        )

        # T6: Sanitise PF (no Infinity) and warn on low trade count
        _check_trade_count_warning(idx, window_metrics.trade_count)
        sanitized_pf = _sanitize_profit_factor(window_metrics.profit_factor)

        # Regime detection on training window (BQ-508)
        regime = detect_regime_for_window(train_bars)
        window_metrics = WindowMetrics(
            window_index=window_metrics.window_index,
            win_rate=window_metrics.win_rate,
            profit_factor=sanitized_pf,
            max_drawdown=window_metrics.max_drawdown,
            sharpe_ratio=window_metrics.sharpe_ratio,
            trade_count=window_metrics.trade_count,
            total_pnl=window_metrics.total_pnl,
            passed_go_nogo=window_metrics.passed_go_nogo,
            regime_volatility=regime["regime_volatility"],
            regime_trend=regime["regime_trend"],
            regime_session=regime["regime_session"],
            regime_combined=regime["regime_combined"],
            regime_quality=regime["regime_quality"],
            btc_regime=regime.get("btc_regime", "unknown"),
        )
        per_window.append(window_metrics)

    aggregated = None
    if per_window:
        wr_values = [m.win_rate for m in per_window]
        pf_values = [m.profit_factor for m in per_window]
        dd_values = [m.max_drawdown for m in per_window]
        sr_values = [m.sharpe_ratio for m in per_window]
        tc_values = [float(m.trade_count) for m in per_window]
        pnl_values = [m.total_pnl for m in per_window]

        mean_wr = _mean(wr_values)
        mean_pf = _mean(pf_values)
        mean_dd = _mean(dd_values)
        mean_sr = _mean(sr_values)
        mean_tc = _mean(tc_values)
        mean_pnl = _mean(pnl_values)

        aggregated = AggregatedMetrics(
            mean_win_rate=mean_wr,
            std_win_rate=_std(wr_values, mean_wr),
            mean_profit_factor=mean_pf,
            std_profit_factor=_std(pf_values, mean_pf),
            mean_max_drawdown=mean_dd,
            std_max_drawdown=_std(dd_values, mean_dd),
            mean_sharpe_ratio=mean_sr,
            std_sharpe_ratio=_std(sr_values, mean_sr),
            mean_trade_count=mean_tc,
            std_trade_count=_std(tc_values, mean_tc),
            mean_total_pnl=mean_pnl,
            std_total_pnl=_std(pnl_values, mean_pnl),
            windows_passed=sum(1 for m in per_window if m.passed_go_nogo),
            total_windows=len(per_window),
        )

    windows_passed = sum(1 for m in per_window if m.passed_go_nogo)
    total = len(per_window)
    go_nogo = total >= 3 and windows_passed >= 3

    # Per-regime aggregation (BQ-508)
    regime_breakdown = aggregate_by_regime(per_window)

    result = WalkForwardResults(
        per_window=per_window,
        aggregated=aggregated,
        go_nogo=go_nogo,
    )
    result._trade_records = all_trade_records  # type: ignore[attr-defined]
    result._regime_breakdown = regime_breakdown  # type: ignore[attr-defined]
    return result


def run_multi_strategy_walk_forward(
    bars: list[Bar],
    strategy_factories: list[Callable[[], ISignalStrategy]],
    pair: str,
    n_windows: int = 5,
    train_ratio: float = 0.7,
    val_ratio: float = 0.15,
    overlap_ratio: float = 0.2,
    initial_balance: float = 10000,
    spread_pips: float | None = None,
    commission_per_lot: float | None = None,
    min_confidence: float = 0.30,
    embargo_bars: int = 0,
) -> WalkForwardResults:
    """Walk-forward with multiple strategies run independently, trades merged.

    Runs each strategy independently via run_all_strategies, merges all trades,
    then aggregates combined metrics across windows.
    """
    effective_spread = spread_pips if spread_pips is not None else get_spread_for_pair(pair)

    config = BacktestConfig(
        starting_balance=initial_balance,
        spread_pips=effective_spread,
        commission_per_lot=commission_per_lot if commission_per_lot is not None else 3.5,
        pair=pair,
        min_confidence=min_confidence,
    )

    validator = WalkForwardValidator(
        data=bars,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        overlap_ratio=overlap_ratio,
        embargo_bars=embargo_bars,
    )

    per_window = []
    all_trade_records: list[dict] = []
    for idx, (train_bars, val_bars, test_bars) in enumerate(validator.split(bars)):  # noqa: B007
        if len(test_bars) < config.min_bars_before_signal:
            window_metrics = _compute_metrics(
                idx, [], initial_balance=initial_balance, bars_in_window=test_bars
            )
            per_window.append(window_metrics)
            continue

        strategies = [factory() for factory in strategy_factories]
        for s in strategies:
            if hasattr(s, "reset") and callable(s.reset):
                s.reset()

        try:
            engine = MultiStrategyBacktestEngine(config, strategies)
            results = engine.run_all_strategies(test_bars)

            # Merge trades from all strategies
            all_trades = []
            for name, res in results.items():  # noqa: B007
                all_trades.extend(res.metrics.trades)

            trades = []
            for t in all_trades:
                if hasattr(t, "profit_loss"):
                    rec = {
                        "pnl": t.profit_loss,
                        "confidence_score": getattr(t, "confidence_score", 0.5),
                        "direction": getattr(t, "direction", None),
                        "window_id": idx,
                        "rationale": getattr(t, "rationale", ""),
                    }
                    trades.append(rec)
                    all_trade_records.append(rec)
        except Exception:
            logger.warning("Trade extraction failed for window %d, skipping", idx)
            raise

        window_metrics = _compute_metrics(
            idx, trades, initial_balance=initial_balance, bars_in_window=test_bars
        )

        # T6: Sanitise PF (no Infinity) and warn on low trade count
        _check_trade_count_warning(idx, window_metrics.trade_count)
        sanitized_pf = _sanitize_profit_factor(window_metrics.profit_factor)

        # Regime detection on training window (BQ-508)
        regime = detect_regime_for_window(train_bars)
        window_metrics = WindowMetrics(
            window_index=window_metrics.window_index,
            win_rate=window_metrics.win_rate,
            profit_factor=sanitized_pf,
            max_drawdown=window_metrics.max_drawdown,
            sharpe_ratio=window_metrics.sharpe_ratio,
            trade_count=window_metrics.trade_count,
            total_pnl=window_metrics.total_pnl,
            passed_go_nogo=window_metrics.passed_go_nogo,
            regime_volatility=regime["regime_volatility"],
            regime_trend=regime["regime_trend"],
            regime_session=regime["regime_session"],
            regime_combined=regime["regime_combined"],
            regime_quality=regime["regime_quality"],
            btc_regime=regime.get("btc_regime", "unknown"),
        )
        per_window.append(window_metrics)

    aggregated = None
    if per_window:
        wr_values = [m.win_rate for m in per_window]
        pf_values = [m.profit_factor for m in per_window]
        dd_values = [m.max_drawdown for m in per_window]
        sr_values = [m.sharpe_ratio for m in per_window]
        tc_values = [float(m.trade_count) for m in per_window]
        pnl_values = [m.total_pnl for m in per_window]

        mean_wr = _mean(wr_values)
        mean_pf = _mean(pf_values)
        mean_dd = _mean(dd_values)
        mean_sr = _mean(sr_values)
        mean_tc = _mean(tc_values)
        mean_pnl = _mean(pnl_values)

        aggregated = AggregatedMetrics(
            mean_win_rate=mean_wr,
            std_win_rate=_std(wr_values, mean_wr),
            mean_profit_factor=mean_pf,
            std_profit_factor=_std(pf_values, mean_pf),
            mean_max_drawdown=mean_dd,
            std_max_drawdown=_std(dd_values, mean_dd),
            mean_sharpe_ratio=mean_sr,
            std_sharpe_ratio=_std(sr_values, mean_sr),
            mean_trade_count=mean_tc,
            std_trade_count=_std(tc_values, mean_tc),
            mean_total_pnl=mean_pnl,
            std_total_pnl=_std(pnl_values, mean_pnl),
            windows_passed=sum(1 for m in per_window if m.passed_go_nogo),
            total_windows=len(per_window),
        )

    windows_passed = sum(1 for m in per_window if m.passed_go_nogo)
    total = len(per_window)
    go_nogo = total >= 3 and windows_passed >= 3

    # Per-regime aggregation (BQ-508)
    regime_breakdown = aggregate_by_regime(per_window)

    result = WalkForwardResults(
        per_window=per_window,
        aggregated=aggregated,
        go_nogo=go_nogo,
    )
    result._trade_records = all_trade_records  # type: ignore[attr-defined]
    result._regime_breakdown = regime_breakdown  # type: ignore[attr-defined]
    return result


STRATEGY_REGISTRY: dict[str, Callable[..., ISignalStrategy]] = {}


def register_strategy(name: str, factory: Callable[..., ISignalStrategy]) -> None:
    STRATEGY_REGISTRY[name] = factory


def get_registered_strategies() -> list[str]:
    return sorted(STRATEGY_REGISTRY.keys())


def run_named_strategy_walk_forward(
    strategy_name: str,
    bars: list[Bar],
    pair: str,
    n_windows: int = 5,
    train_ratio: float = 0.7,
    initial_balance: float = 10000,
    spread_pips: float | None = None,
    commission_per_lot: float | None = None,
    embargo_bars: int = 0,
) -> WalkForwardResults:
    if strategy_name not in STRATEGY_REGISTRY:
        available = ", ".join(get_registered_strategies())
        raise ValueError(f"Unknown strategy '{strategy_name}'. Available: {available}")
    return run_strategy_walk_forward(
        bars=bars,
        strategy_factory=STRATEGY_REGISTRY[strategy_name],
        pair=pair,
        n_windows=n_windows,
        train_ratio=train_ratio,
        initial_balance=initial_balance,
        spread_pips=spread_pips,
        commission_per_lot=commission_per_lot,
        embargo_bars=embargo_bars,
    )
