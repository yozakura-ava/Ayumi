"""Portfolio Blend Framework — Combine multiple strategies into a unified portfolio.

AYUAA-487/AYUAA-481: Loads passing strategies, filters unprofitable ones,
computes correlation between equity curves, optimizes weight allocation with
multiple methods, runs walk-forward validation, and evaluates against
FTMO criteria (WR >55%, PF >1.3, Sharpe >0.5).

FTMO Defensive Rule Hooks
-------------------------
Live-trading entry decisions can use the Best Day Rule via
:func:`check_ftmo_best_day_rule` together with :class:`BestDayRuleTracker`.
The tracker is **inactive** during backtests and challenge phases — it only
gates entries when ``account_phase='funded'``. See
``src/forex_bot/backtest/best_day_rule.py`` for the per-day semantics and
``docs/runbooks/backtesting-strategy.md`` (Best Day Rule section) for the
integration pattern.

News Blackout Filter
--------------------
Use :func:`check_news_blackout` to gate entries around high-impact economic
events (NFP, FOMC, ECB, BOJ, BOE rate decisions, CPI).  The filter wraps
:class:`~data.news_calendar.NewsCalendarFilter` and returns a flat
``(allowed, reason)`` tuple.  See
``src/forex_bot/data/news_calendar.py`` for configuration details and
``docs/runbooks/backtesting-strategy.md`` (News Blackout section).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from data.news_calendar import NewsCalendarFilter
from quant.walk_forward import (
    AggregatedMetrics,
    WalkForwardResults,
    WindowMetrics,
    _mean,
    _std,
)

from .best_day_rule import (
    BestDayCheckResult,
    BestDayRuleTracker,
)
from .data_loader import CsvDataLoader
from .engine import (
    BacktestConfig,
    BacktestMetrics,
    Bar,
    MarketState,
    SimulatedTrade,
    TradeDirection,
    determine_session,
    get_spread_for_pair,
)
from .multi_strategy_engine import MultiStrategyBacktestEngine
from .strategies import ISignalStrategy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategySpec:
    name: str
    factory: Any
    pair: str
    timeframe: str
    data_path: str


@dataclass
class StrategyEquityCurve:
    strategy_name: str
    pair: str
    timeframe: str
    equity_curve: list[float]
    returns: list[float]
    total_pnl: float
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    trade_count: int


@dataclass
class CorrelationResult:
    matrix: dict[str, dict[str, float]]
    average_correlation: float


@dataclass
class FilteredStrategy:
    key: str
    reason: str


@dataclass
class WeightAllocation:
    weights: dict[str, float]
    method: str


@dataclass
class PortfolioBlendResult:
    individual_results: dict[str, StrategyEquityCurve]
    correlation: CorrelationResult
    weights: WeightAllocation
    combined_equity_curve: list[float]
    combined_metrics: BacktestMetrics
    walk_forward: WalkForwardResults | None = None
    ftmo_passed: bool = False
    ftmo_criteria: dict[str, bool] = field(default_factory=dict)


FTMO_CRITERIA = {
    "win_rate": 0.55,
    "profit_factor": 1.3,
    "sharpe_ratio": 0.5,
}


def check_ftmo_best_day_rule(
    tracker: BestDayRuleTracker,
    planned_profit_dollars: float,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Gate an entry decision against the FTMO Best Day Rule.

    Thin wrapper around :meth:`BestDayRuleTracker.check_entry` that returns
    a flat ``(allowed, reason)`` tuple — convenient for destructuring in
    trading loops. The reason string is ``""`` when the entry is allowed.

    Args:
        tracker: Configured :class:`BestDayRuleTracker`. When the tracker's
            ``account_phase`` is not ``"funded"``, this is a no-op and
            ``(True, "")`` is returned.
        planned_profit_dollars: Expected profit if the trade works. Must
            be ``>= 0`` for the rule to apply.
        now: Current time. Forwarded to the tracker. Defaults to system
            UTC time inside the tracker.

    Returns:
        ``(allowed, reason)``: ``allowed=True`` when the entry may proceed,
        otherwise ``allowed=False`` with a human-readable reason describing
        the projected share and threshold.

    Example:
        >>> tracker = BestDayRuleTracker(account_phase="funded")
        >>> tracker.record_trade_close(some_trade_time, 400.0)
        >>> allowed, reason = check_ftmo_best_day_rule(
        ...     tracker, planned_profit_dollars=250.0, now=datetime.now(timezone.utc)
        ... )
        >>> if not allowed:
        ...     logger.info("Entry blocked: %s", reason)
    """
    result: BestDayCheckResult = tracker.check_entry(
        planned_profit_dollars=planned_profit_dollars,
        now=now,
    )
    return result.allowed, result.reason


def check_news_blackout(
    news_filter: NewsCalendarFilter,
    symbols: list[str],
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Gate an entry decision against the FTMO news blackout filter.

    Thin wrapper around :meth:`NewsCalendarFilter.is_blackout_now` that
    returns a flat ``(allowed, reason)`` tuple — convenient for
    destructuring in trading loops.  The reason string is ``""`` when the
    entry is allowed.

    Args:
        news_filter: Configured :class:`NewsCalendarFilter`.
        symbols: Trading symbols being evaluated (e.g.
            ``["EURUSD", "USDJPY"]``).
        now: Current time.  Defaults to system UTC time inside the filter.

    Returns:
        ``(allowed, reason)``: ``allowed=True`` when the entry may proceed,
        otherwise ``allowed=False`` with a human-readable reason describing
        the active blackout event.

    Example:
        >>> nf = NewsCalendarFilter(auto_fetch=False, cache_path="cal.json")
        >>> allowed, reason = check_news_blackout(nf, ["EURUSD"])
        >>> if not allowed:
        ...     logger.info("Entry blocked: %s", reason)
    """
    if news_filter.is_blackout_now(symbols, now=now):
        window = news_filter.next_blackout_window(symbols, now=now)
        if window:
            reason = f"News blackout: {window.title} ({window.currency}) until {window.end.strftime('%H:%M UTC')}"
        else:
            reason = "News blackout active"
        return False, reason
    return True, ""


def _build_strategy_name(strategy_name: str, pair: str, timeframe: str) -> str:
    return f"{strategy_name}|{pair}|{timeframe}"


def _compute_equity_curve_from_trades(trades: list[SimulatedTrade], initial_balance: float) -> list[float]:
    curve = [initial_balance]
    balance = initial_balance
    for t in trades:
        balance = max(0.0, balance + t.profit_loss)
        curve.append(balance)
    return curve


def _compute_returns(equity_curve: list[float]) -> list[float]:
    returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
    return returns


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a_trunc = a[:n]
    b_trunc = b[:n]
    mean_a = sum(a_trunc) / n
    mean_b = sum(b_trunc) / n
    cov = sum((a_trunc[i] - mean_a) * (b_trunc[i] - mean_b) for i in range(n)) / (n - 1)
    var_a = sum((x - mean_a) ** 2 for x in a_trunc) / (n - 1)
    var_b = sum((x - mean_b) ** 2 for x in b_trunc) / (n - 1)
    if var_a == 0 or var_b == 0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def compute_correlation_matrix(
    equity_curves: dict[str, StrategyEquityCurve],
) -> CorrelationResult:
    keys = list(equity_curves.keys())
    matrix: dict[str, dict[str, float]] = {}
    total_corr = 0.0
    count = 0

    for key_a in keys:
        matrix[key_a] = {}
        for key_b in keys:
            if key_a == key_b:
                matrix[key_a][key_b] = 1.0
            elif key_b in matrix and key_a in matrix[key_b]:
                matrix[key_a][key_b] = matrix[key_b][key_a]
            else:
                corr = _correlation(equity_curves[key_a].returns, equity_curves[key_b].returns)
                matrix[key_a][key_b] = corr
                if key_a < key_b:
                    total_corr += abs(corr)
                    count += 1

    avg_corr = total_corr / count if count > 0 else 0.0
    return CorrelationResult(matrix=matrix, average_correlation=avg_corr)


def optimize_weights_inverse_variance(
    equity_curves: dict[str, StrategyEquityCurve],
) -> WeightAllocation:
    variances: dict[str, float] = {}
    for key, ec in equity_curves.items():
        if len(ec.returns) < 2:
            variances[key] = 1.0
            continue
        mean_r = sum(ec.returns) / len(ec.returns)
        var = sum((r - mean_r) ** 2 for r in ec.returns) / (len(ec.returns) - 1)
        variances[key] = var if var > 0 else 0.0001

    inv_var = {k: 1.0 / v for k, v in variances.items()}
    total_inv = sum(inv_var.values())
    weights = {k: v / total_inv for k, v in inv_var.items()}
    return WeightAllocation(weights=weights, method="inverse_variance")


def optimize_weights_equal_risk(
    equity_curves: dict[str, StrategyEquityCurve],
) -> WeightAllocation:
    sharpe_scores: dict[str, float] = {}
    for key, ec in equity_curves.items():
        if ec.max_drawdown > 0:
            sharpe_scores[key] = ec.sharpe_ratio / ec.max_drawdown
        else:
            sharpe_scores[key] = ec.sharpe_ratio

    positive = {k: max(v, 0.01) for k, v in sharpe_scores.items()}
    total = sum(positive.values())
    weights = {k: v / total for k, v in positive.items()}
    return WeightAllocation(weights=weights, method="equal_risk")


def optimize_weights_profit_factor(
    equity_curves: dict[str, StrategyEquityCurve],
) -> WeightAllocation:
    pf_scores: dict[str, float] = {}
    for key, ec in equity_curves.items():
        pf_scores[key] = max(ec.profit_factor, 0.1)

    total = sum(pf_scores.values())
    weights = {k: v / total for k, v in pf_scores.items()}
    return WeightAllocation(weights=weights, method="profit_factor")


def optimize_weights_sharpe(
    equity_curves: dict[str, StrategyEquityCurve],
) -> WeightAllocation:
    sharpe_scores: dict[str, float] = {}
    for key, ec in equity_curves.items():
        sharpe_scores[key] = max(ec.sharpe_ratio, 0.01)

    total = sum(sharpe_scores.values())
    weights = {k: v / total for k, v in sharpe_scores.items()}
    return WeightAllocation(weights=weights, method="sharpe_weighted")


def optimize_weights_combined_score(
    equity_curves: dict[str, StrategyEquityCurve],
) -> WeightAllocation:
    scores: dict[str, float] = {}
    for key, ec in equity_curves.items():
        if len(ec.returns) < 2:
            scores[key] = 0.01
            continue
        mean_r = sum(ec.returns) / len(ec.returns)
        var = sum((r - mean_r) ** 2 for r in ec.returns) / (len(ec.returns) - 1)
        std = math.sqrt(var) if var > 0 else 0.0001

        sharpe_component = ec.sharpe_ratio if ec.sharpe_ratio > 0 else 0.0
        pf_component = ec.profit_factor if ec.profit_factor > 1.0 else 0.5
        consistency = 1.0 / (1.0 + std * 100) if std > 0 else 1.0
        dd_penalty = 1.0 / (1.0 + ec.max_drawdown * 5)

        scores[key] = sharpe_component * 0.3 + pf_component * 0.3 + consistency * 0.2 + dd_penalty * 0.2
        scores[key] = max(scores[key], 0.01)

    total = sum(scores.values())
    weights = {k: v / total for k, v in scores.items()}
    return WeightAllocation(weights=weights, method="combined_score")


MIN_PROFIT_FACTOR = 1.0
MIN_WIN_RATE = 0.45


def filter_strategies(
    equity_curves: dict[str, StrategyEquityCurve],
) -> tuple[dict[str, StrategyEquityCurve], list[FilteredStrategy]]:
    filtered: dict[str, StrategyEquityCurve] = {}
    removed: list[FilteredStrategy] = []

    for key, ec in equity_curves.items():
        reasons: list[str] = []
        if ec.profit_factor < MIN_PROFIT_FACTOR:
            reasons.append(f"PF {ec.profit_factor:.2f} < {MIN_PROFIT_FACTOR}")
        if ec.win_rate < MIN_WIN_RATE:
            reasons.append(f"WR {ec.win_rate:.1%} < {MIN_WIN_RATE:.1%}")

        if reasons:
            removed.append(FilteredStrategy(key=key, reason="; ".join(reasons)))
        else:
            filtered[key] = ec

    return filtered, removed


WEIGHT_METHODS = {
    "inverse_variance": optimize_weights_inverse_variance,
    "equal_risk": optimize_weights_equal_risk,
    "profit_factor": optimize_weights_profit_factor,
    "sharpe_weighted": optimize_weights_sharpe,
    "combined_score": optimize_weights_combined_score,
}


def run_single_strategy_backtest(
    strategy: ISignalStrategy,
    bars: list[Bar],
    pair: str,
    initial_balance: float = 10000.0,
) -> BacktestMetrics:
    # FTMO-aligned config — matches SRF-validated parameters.
    # SRF validates at spread=0.3 for XAUUSD; the PAIR_SPREAD_PIPS table
    # defaults to 2.5 which is far too conservative for gold and kills the
    # strategy before it can trade. Use tighter spread that reflects actual
    # demo account conditions.
    from backtest.types import get_spread_for_pair as _gsp

    pair_spread = _gsp(pair)
    # XAUUSD default in the table is 2.5 pips but actual demo spread is ~0.3-0.5
    if pair == "XAUUSD":
        pair_spread = 0.5

    config = BacktestConfig(
        starting_balance=initial_balance,
        spread_pips=pair_spread,
        commission_per_lot=3.5,
        pair=pair,
        max_open_trades=1,
        risk_per_trade_pct=0.005,  # 0.5% per trade (FTMO-safe)
        max_daily_drawdown_pct=0.05,  # 5% daily DD (FTMO 1-Step)
        max_total_drawdown_pct=0.10,  # 10% max DD (FTMO 1-Step)
        min_confidence=0.30,  # Match SRF-validated threshold
        slippage_pips=0.1,  # Realistic for demo
        swap_per_lot_per_day=-1.0,  # Reduced from -2.0
    )
    engine = MultiStrategyBacktestEngine(config, [strategy])
    results = engine.run_all_strategies(bars)
    return results[strategy.name].metrics


def run_portfolio_blend(
    strategy_specs: list[StrategySpec],
    initial_balance: float = 10000.0,
    n_walk_forward_windows: int = 5,
    weight_method: str = "combined_score",
    enable_filter: bool = True,
) -> PortfolioBlendResult:
    loader = CsvDataLoader()
    individual_results: dict[str, StrategyEquityCurve] = {}
    individual_metrics: dict[str, BacktestMetrics] = {}

    for spec in strategy_specs:
        try:
            bars = loader.load(spec.data_path)
        except (FileNotFoundError, OSError):
            continue
        if len(bars) < 100:
            continue

        strategy = spec.factory()
        if hasattr(strategy, "set_balance"):
            strategy.set_balance(initial_balance)

        metrics = run_single_strategy_backtest(strategy, bars, spec.pair, initial_balance)

        equity = _compute_equity_curve_from_trades(metrics.trades, initial_balance)
        returns = _compute_returns(equity)

        max_dd = 0.0
        peak = initial_balance
        for val in equity:
            if val > peak:
                peak = val
            if peak > 0:
                dd = (peak - val) / peak
                if dd > max_dd:
                    max_dd = dd

        key = _build_strategy_name(spec.name, spec.pair, spec.timeframe)
        individual_results[key] = StrategyEquityCurve(
            strategy_name=spec.name,
            pair=spec.pair,
            timeframe=spec.timeframe,
            equity_curve=equity,
            returns=returns,
            total_pnl=metrics.total_pnl,
            win_rate=metrics.win_rate,
            profit_factor=metrics.profit_factor,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown=max_dd,
            trade_count=metrics.total_trades,
        )
        individual_metrics[key] = metrics

    active_curves = individual_results
    filtered_out: list[FilteredStrategy] = []
    if enable_filter and len(individual_results) > 1:
        active_curves, filtered_out = filter_strategies(individual_results)

    filtered_specs = [s for s in strategy_specs if _build_strategy_name(s.name, s.pair, s.timeframe) in active_curves]

    correlation = compute_correlation_matrix(active_curves)

    optimize_fn = WEIGHT_METHODS.get(weight_method, optimize_weights_combined_score)
    weights = optimize_fn(active_curves)

    weighted_equity = _build_weighted_equity(active_curves, weights, initial_balance)
    combined_metrics = _compute_combined_metrics(weighted_equity, initial_balance)

    wf_result = None
    if n_walk_forward_windows > 0 and active_curves:
        wf_result = _run_portfolio_walk_forward(filtered_specs, weights, initial_balance, n_walk_forward_windows)

    ftmo_check = {
        "win_rate": combined_metrics.win_rate >= FTMO_CRITERIA["win_rate"],
        "profit_factor": combined_metrics.profit_factor >= FTMO_CRITERIA["profit_factor"],
        "sharpe_ratio": combined_metrics.sharpe_ratio >= FTMO_CRITERIA["sharpe_ratio"],
    }

    if wf_result and wf_result.aggregated:
        ftmo_check["wf_mean_win_rate"] = wf_result.aggregated.mean_win_rate >= FTMO_CRITERIA["win_rate"]
        ftmo_check["wf_mean_profit_factor"] = wf_result.aggregated.mean_profit_factor >= FTMO_CRITERIA["profit_factor"]
        ftmo_check["wf_mean_sharpe"] = wf_result.aggregated.mean_sharpe_ratio >= FTMO_CRITERIA["sharpe_ratio"]
        ftmo_check["wf_go_nogo"] = wf_result.go_nogo

    ftmo_passed = all(v for k, v in ftmo_check.items() if not k.startswith("wf_"))

    return PortfolioBlendResult(
        individual_results=individual_results,
        correlation=correlation,
        weights=weights,
        combined_equity_curve=weighted_equity,
        combined_metrics=combined_metrics,
        walk_forward=wf_result,
        ftmo_passed=ftmo_passed,
        ftmo_criteria=ftmo_check,
    )


def _build_weighted_equity(
    equity_curves: dict[str, StrategyEquityCurve],
    weights: WeightAllocation,
    initial_balance: float,
) -> list[float]:
    if not equity_curves:
        return [initial_balance]

    max_len = max(len(ec.equity_curve) for ec in equity_curves.values())
    portfolio_curve: list[float] = [initial_balance]

    for i in range(1, max_len):
        weighted_return = 0.0
        total_weight = 0.0
        for key, ec in equity_curves.items():
            w = weights.weights.get(key, 0.0)
            if w <= 0:
                continue
            if i < len(ec.equity_curve) and i - 1 < len(ec.equity_curve):
                prev_val = ec.equity_curve[i - 1]
                curr_val = ec.equity_curve[i]
                if prev_val > 0:
                    ret = (curr_val - prev_val) / prev_val
                    weighted_return += w * ret
                    total_weight += w

        if total_weight > 0:
            portfolio_return = weighted_return / total_weight
        else:
            portfolio_return = 0.0

        new_balance = portfolio_curve[-1] * (1.0 + portfolio_return)
        portfolio_curve.append(max(0.0, new_balance))

    return portfolio_curve


def _compute_combined_metrics(equity_curve: list[float], initial_balance: float) -> BacktestMetrics:
    ending = equity_curve[-1] if equity_curve else initial_balance
    total_pnl = ending - initial_balance
    total_pnl_pct = total_pnl / initial_balance if initial_balance > 0 else 0.0

    returns = _compute_returns(equity_curve)
    max_dd = 0.0
    peak = initial_balance
    for val in equity_curve:
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd

    sharpe = 0.0
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
        if std_r > 0:
            sharpe = (mean_r / std_r) * math.sqrt(252)

    positive_returns = [r for r in returns if r > 0]
    negative_returns = [r for r in returns if r < 0]
    win_rate = (len(positive_returns) / len(returns)) if returns else 0.0

    total_wins = sum(positive_returns) * initial_balance
    total_losses = abs(sum(negative_returns) * initial_balance)
    profit_factor = total_wins / total_losses if total_losses > 0 else 0.0

    return BacktestMetrics(
        starting_balance=initial_balance,
        ending_balance=ending,
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        win_rate=win_rate,
        total_trades=len(returns),
        winning_trades=len(positive_returns),
        losing_trades=len(negative_returns),
        breakeven_trades=0,
        avg_win=0.0,
        avg_loss=0.0,
        largest_win=0.0,
        largest_loss=0.0,
        profit_factor=profit_factor,
        max_drawdown_pct=max_dd * 100,
        max_drawdown_dollar=max_dd * peak,
        max_daily_loss_dollar=0.0,
        sharpe_ratio=sharpe,
        avg_risk_reward=0.0,
        expectancy=0.0,
        avg_holding_bars=0.0,
        equity_curve=equity_curve,
        trades=[],
        total_spread_cost=0.0,
        total_commission_cost=0.0,
        rejected_signals=0,
    )


def _run_portfolio_walk_forward(
    strategy_specs: list[StrategySpec],
    weights: WeightAllocation,
    initial_balance: float,
    n_windows: int,
) -> WalkForwardResults:
    loader = CsvDataLoader()
    data_map: dict[str, list[Bar]] = {}

    for spec in strategy_specs:
        bars = loader.load(spec.data_path)
        if len(bars) >= 100:
            key = _build_strategy_name(spec.name, spec.pair, spec.timeframe)
            data_map[key] = bars

    if not data_map:
        return WalkForwardResults(per_window=[], go_nogo=False)

    first_data = next(iter(data_map.values()))
    n = len(first_data)
    full_window_size = n // n_windows
    if full_window_size == 0:
        return WalkForwardResults(per_window=[], go_nogo=False)

    train_ratio = 0.7
    val_ratio = 0.15
    overlap_ratio = 0.2
    test_ratio = 1.0 - train_ratio - val_ratio

    train_size = int(full_window_size * train_ratio)
    val_size = int(full_window_size * val_ratio)
    window_size = train_size + val_size + int(full_window_size * test_ratio)
    overlap_size = int(window_size * overlap_ratio)
    step = max(window_size - overlap_size, 1)

    per_window: list[WindowMetrics] = []

    for win_idx in range(n_windows):
        start = win_idx * step
        end = min(start + window_size, n)
        if end - start < window_size:
            end = min(start + window_size, n)
            start = end - window_size
            if start < 0:
                start = 0

        test_start = start + train_size + val_size
        test_end = end
        if test_end <= test_start:
            per_window.append(
                WindowMetrics(
                    window_index=win_idx,
                    win_rate=0.0,
                    profit_factor=0.0,
                    max_drawdown=0.0,
                    sharpe_ratio=0.0,
                    trade_count=0,
                    total_pnl=0.0,
                    passed_go_nogo=False,
                )
            )
            continue

        window_returns: list[float] = []
        for spec in strategy_specs:
            key = _build_strategy_name(spec.name, spec.pair, spec.timeframe)
            if key not in data_map:
                continue
            all_bars = data_map[key]
            test_bars = all_bars[test_start:test_end]
            if len(test_bars) < 30:
                continue

            w = weights.weights.get(key, 0.0)
            if w <= 0:
                continue

            strategy = spec.factory()
            if hasattr(strategy, "set_balance"):
                strategy.set_balance(initial_balance)

            metrics = run_single_strategy_backtest(strategy, test_bars, spec.pair, initial_balance)

            equity = _compute_equity_curve_from_trades(metrics.trades, initial_balance)
            strat_returns = _compute_returns(equity)
            for r in strat_returns:
                window_returns.append(w * r)

        if not window_returns:
            per_window.append(
                WindowMetrics(
                    window_index=win_idx,
                    win_rate=0.0,
                    profit_factor=0.0,
                    max_drawdown=0.0,
                    sharpe_ratio=0.0,
                    trade_count=0,
                    total_pnl=0.0,
                    passed_go_nogo=False,
                )
            )
            continue

        total_weight = sum(
            weights.weights.get(_build_strategy_name(s.name, s.pair, s.timeframe), 0.0)
            for s in strategy_specs
            if _build_strategy_name(s.name, s.pair, s.timeframe) in data_map
        )
        if total_weight > 0:
            portfolio_returns = [r / total_weight for r in window_returns]
        else:
            portfolio_returns = window_returns

        portfolio_pnl = sum(portfolio_returns) * initial_balance
        positive = [r for r in portfolio_returns if r > 0]
        negative = [r for r in portfolio_returns if r < 0]
        n_returns = len(portfolio_returns)
        win_rate = len(positive) / n_returns if n_returns > 0 else 0.0
        total_wins = sum(positive) * initial_balance
        total_losses = abs(sum(negative)) * initial_balance
        pf = total_wins / total_losses if total_losses > 0 else 0.0

        balance = initial_balance
        peak = initial_balance
        max_dd = 0.0
        for r in portfolio_returns:
            balance = max(0.0, balance + r * initial_balance)
            if balance > peak:
                peak = balance
            if peak > 0:
                dd = (peak - balance) / peak
                if dd > max_dd:
                    max_dd = dd

        sharpe = 0.0
        if n_returns >= 2:
            mean_r = sum(portfolio_returns) / n_returns
            std_r = math.sqrt(sum((r - mean_r) ** 2 for r in portfolio_returns) / n_returns)
            if std_r > 0:
                sharpe = (mean_r / std_r) * math.sqrt(252)

        passed = win_rate > 0.55 and pf > 1.0 and portfolio_pnl > 0 and max_dd < 0.10

        per_window.append(
            WindowMetrics(
                window_index=win_idx,
                win_rate=win_rate,
                profit_factor=pf,
                max_drawdown=max_dd,
                sharpe_ratio=sharpe,
                trade_count=n_returns,
                total_pnl=portfolio_pnl,
                passed_go_nogo=passed,
            )
        )

    if per_window:
        wr_vals = [m.win_rate for m in per_window]
        pf_vals = [m.profit_factor for m in per_window]
        dd_vals = [m.max_drawdown for m in per_window]
        sr_vals = [m.sharpe_ratio for m in per_window]
        tc_vals = [float(m.trade_count) for m in per_window]
        pnl_vals = [m.total_pnl for m in per_window]

        aggregated = AggregatedMetrics(
            mean_win_rate=_mean(wr_vals),
            std_win_rate=_std(wr_vals, _mean(wr_vals)),
            mean_profit_factor=_mean(pf_vals),
            std_profit_factor=_std(pf_vals, _mean(pf_vals)),
            mean_max_drawdown=_mean(dd_vals),
            std_max_drawdown=_std(dd_vals, _mean(dd_vals)),
            mean_sharpe_ratio=_mean(sr_vals),
            std_sharpe_ratio=_std(sr_vals, _mean(sr_vals)),
            mean_trade_count=_mean(tc_vals),
            std_trade_count=_std(tc_vals, _mean(tc_vals)),
            mean_total_pnl=_mean(pnl_vals),
            std_total_pnl=_std(pnl_vals, _mean(pnl_vals)),
            windows_passed=sum(1 for m in per_window if m.passed_go_nogo),
            total_windows=len(per_window),
        )
    else:
        aggregated = None

    windows_passed = sum(1 for m in per_window if m.passed_go_nogo)
    total = len(per_window)
    go_nogo = total >= 3 and windows_passed >= 3

    return WalkForwardResults(
        per_window=per_window,
        aggregated=aggregated,
        go_nogo=go_nogo,
    )


def format_portfolio_report(
    result: PortfolioBlendResult,
    filtered_strategies: list[FilteredStrategy] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("PORTFOLIO BLEND TEST REPORT")
    lines.append("=" * 80)
    lines.append("")

    lines.append("INDIVIDUAL STRATEGY RESULTS")
    lines.append("-" * 80)
    lines.append(f"{'Strategy':<45} {'WR%':>6} {'PF':>7} {'Sharpe':>7} {'DD%':>7} {'Trades':>7} {'PnL':>10}")
    for key, ec in result.individual_results.items():
        label = f"{ec.strategy_name} ({ec.pair} {ec.timeframe})"
        is_active = key in result.weights.weights
        marker = "" if is_active else " [FILTERED]"
        lines.append(
            f"{label:<45} {ec.win_rate:>6.1%} {ec.profit_factor:>7.2f} "
            f"{ec.sharpe_ratio:>7.2f} {ec.max_drawdown * 100:>7.2f} {ec.trade_count:>7} "
            f"${ec.total_pnl:>9.2f}{marker}"
        )
    lines.append("")

    if filtered_strategies:
        lines.append("FILTERED STRATEGIES (excluded from blend)")
        lines.append("-" * 80)
        for fs in filtered_strategies:
            lines.append(f"  {fs.key:<50} {fs.reason}")
        lines.append("")

    lines.append("CORRELATION MATRIX")
    lines.append("-" * 80)
    keys = list(result.correlation.matrix.keys())
    header = f"{'':<45}" + "".join(f"{k[:12]:>12}" for k in keys)
    lines.append(header)
    for key_a in keys:
        row = f"{key_a[:45]:<45}"
        for key_b in keys:
            val = result.correlation.matrix[key_a][key_b]
            row += f"{val:>12.3f}"
        lines.append(row)
    lines.append(f"\nAverage |correlation|: {result.correlation.average_correlation:.3f}")
    lines.append("")

    lines.append(f"WEIGHT ALLOCATION ({result.weights.method})")
    lines.append("-" * 80)
    for key, w in result.weights.weights.items():
        ec = result.individual_results.get(key)
        if ec:
            label = f"{ec.strategy_name} ({ec.pair} {ec.timeframe})"
        else:
            label = key
        lines.append(f"  {label:<45} {w:>6.1%}")
    lines.append("")

    lines.append("COMBINED PORTFOLIO METRICS")
    lines.append("-" * 80)
    m = result.combined_metrics
    lines.append(f"  Starting Balance:  ${m.starting_balance:>10.2f}")
    lines.append(f"  Ending Balance:    ${m.ending_balance:>10.2f}")
    lines.append(f"  Total P&L:         ${m.total_pnl:>10.2f} ({m.total_pnl_pct:>7.2f}%)")
    lines.append(f"  Win Rate:          {m.win_rate:>10.1%}")
    lines.append(f"  Profit Factor:     {m.profit_factor:>10.2f}")
    lines.append(f"  Sharpe Ratio:      {m.sharpe_ratio:>10.2f}")
    lines.append(f"  Max Drawdown:      {m.max_drawdown_pct:>10.2f}%")
    lines.append("")

    if result.walk_forward:
        wf = result.walk_forward
        lines.append("WALK-FORWARD VALIDATION")
        lines.append("-" * 80)
        lines.append(f"  GO/NO-GO: {'GO' if wf.go_nogo else 'NO-GO'}")
        if wf.aggregated:
            a = wf.aggregated
            lines.append(f"  Mean Win Rate:      {a.mean_win_rate:>10.2%}")
            lines.append(f"  Mean Profit Factor: {a.mean_profit_factor:>10.2f}")
            lines.append(f"  Mean Sharpe:        {a.mean_sharpe_ratio:>10.2f}")
            lines.append(f"  Mean Max DD:        {a.mean_max_drawdown:>10.2%}")
            lines.append(f"  Windows Passed:     {a.windows_passed}/{a.total_windows}")

        lines.append("")
        lines.append(f"  {'Win':<8} {'PF':>8} {'MaxDD':>8} {'Sharpe':>8} {'Trades':>8} {'PnL':>12} {'GO?':>6}")
        for wm in wf.per_window:
            lines.append(
                f"  {wm.win_rate:<8.2%} {wm.profit_factor:>8.2f} {wm.max_drawdown:>8.2%} "
                f"{wm.sharpe_ratio:>8.2f} {wm.trade_count:>8} "
                f"${wm.total_pnl:>10.2f} {'YES' if wm.passed_go_nogo else 'NO':>6}"
            )
        lines.append("")

    lines.append("FTMO CRITERIA CHECK")
    lines.append("-" * 80)
    all_pass = True
    for criterion, passed in result.ftmo_criteria.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        lines.append(f"  {criterion:<30} {status}")
    lines.append(f"\n  OVERALL: {'FTMO PASS' if all_pass else 'FTMO FAIL'}")
    lines.append("")
    lines.append("=" * 80)

    return "\n".join(lines)


def build_passing_strategy_specs(data_dir: str) -> list[StrategySpec]:
    try:
        from strategies.grid import GridConfig, GridStrategyAdapter
    except ImportError:
        GridConfig = None  # type: ignore
        GridStrategyAdapter = None  # type: ignore
    from strategies.session_range_mean_reversion import (
        SessionRangeMeanReversionStrategy,
    )

    from .stat_arb import StatArbStrategy
    from .strategies import CommodityMeanReversionStrategy

    loader = CsvDataLoader()
    pair_b_bars = loader.load(f"{data_dir}/GBPUSD_M15.csv")

    specs: list[StrategySpec] = []

    specs.append(
        StrategySpec(
            name="Statistical Arbitrage",
            factory=lambda: StatArbStrategy(pair_b_bars=pair_b_bars),
            pair="EURUSD",
            timeframe="M15",
            data_path=f"{data_dir}/EURUSD_M15.csv",
        )
    )

    specs.append(
        StrategySpec(
            name="Commodity XAUUSD",
            factory=CommodityMeanReversionStrategy,
            pair="XAUUSD",
            timeframe="M15",
            data_path=f"{data_dir}/XAUUSD_M15.csv",
        )
    )

    if GridConfig is not None and GridStrategyAdapter is not None:
        specs.append(
            StrategySpec(
                name="Grid Trading",
                factory=lambda: GridStrategyAdapter(GridConfig.ftmo("EURUSD")),
                pair="EURUSD",
                timeframe="M15",
                data_path=f"{data_dir}/EURUSD_M15.csv",
            )
        )

    specs.append(
        StrategySpec(
            name="Session-Range Mean Reversion",
            factory=SessionRangeMeanReversionStrategy,
            pair="EURUSD",
            timeframe="H1",
            data_path=f"{data_dir}/EURUSD_H1.csv",
        )
    )

    specs.append(
        StrategySpec(
            name="Session-Range Mean Reversion",
            factory=SessionRangeMeanReversionStrategy,
            pair="GBPUSD",
            timeframe="H1",
            data_path=f"{data_dir}/GBPUSD_H1.csv",
        )
    )

    return specs


@dataclass
class SignalRecord:
    bar_index: int
    direction: int
    confidence: float


@dataclass
class StrategyInventoryResult:
    strategy_name: str
    metrics: BacktestMetrics
    signals: list[SignalRecord]


def inventory_strategies_on_data(
    strategy_factories: dict[str, Any],
    bars: list[Bar],
    pair: str,
    initial_balance: float = 10000.0,
    ftmo_guard: Any | None = None,
) -> dict[str, StrategyInventoryResult]:
    config = BacktestConfig(
        starting_balance=initial_balance,
        spread_pips=get_spread_for_pair(pair) if pair != "XAUUSD" else 0.5,
        commission_per_lot=3.5,
        pair=pair,
        max_open_trades=1,
        risk_per_trade_pct=0.005,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.10,
        min_confidence=0.30,
        slippage_pips=0.1,
        swap_per_lot_per_day=-1.0,
    )

    results: dict[str, StrategyInventoryResult] = {}

    for name, factory in strategy_factories.items():
        try:
            strategy = factory()
            if hasattr(strategy, "set_balance"):
                strategy.set_balance(initial_balance)
        except Exception:
            logger.warning("Strategy %s initialization failed, skipping", name, exc_info=True)
            continue

        signals: list[SignalRecord] = []
        engine = MultiStrategyBacktestEngine(config, [strategy])

        if len(bars) < config.min_bars_before_signal:
            continue

        engine._reset()
        trades: list[SimulatedTrade] = []
        equity_curve = [engine.balance]
        open_trades: list[SimulatedTrade] = []

        for i in range(len(bars)):
            bar = bars[i]
            engine._update_daily_tracking(bar.time)

            if engine.balance <= 0:
                break
            if engine._is_max_drawdown_breached():
                break
            if engine._is_max_daily_loss_breached():
                continue

            engine._check_open_trades(open_trades, bar, i, trades, equity_curve)

            if len(open_trades) < config.max_open_trades and i >= config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bars[i].time),
                )
                signal = strategy.evaluate(state)
                if signal is not None and signal.confidence >= config.min_confidence:
                    # FTMO guard: reject entry if it would breach daily or total loss limits
                    if ftmo_guard is not None:
                        planned_risk = config.risk_per_trade_pct * engine.balance
                        if not ftmo_guard.check_entry(planned_risk, balance=engine.balance):
                            continue
                    direction_val = (
                        1
                        if signal.direction == TradeDirection.LONG
                        else -1
                        if signal.direction == TradeDirection.SHORT
                        else 0
                    )
                    signals.append(
                        SignalRecord(
                            bar_index=i,
                            direction=direction_val,
                            confidence=signal.confidence,
                        )
                    )
                    trade = engine._open_trade(signal, bar, i)
                    if trade is not None:
                        open_trades.append(trade)

            equity_curve.append(engine.balance)

        trades.extend(engine._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close))
        metrics = engine._calculate_metrics(trades, equity_curve, 0)

        results[name] = StrategyInventoryResult(
            strategy_name=name,
            metrics=metrics,
            signals=signals,
        )

    return results


def compute_signal_correlation(
    inventory_results: dict[str, StrategyInventoryResult],
    total_bars: int,
) -> CorrelationResult:
    keys = list(inventory_results.keys())
    signal_vectors: dict[str, list[float]] = {}

    for key in keys:
        vec = [0.0] * total_bars
        for sig in inventory_results[key].signals:
            if 0 <= sig.bar_index < total_bars:
                vec[sig.bar_index] = float(sig.direction)
        signal_vectors[key] = vec

    return compute_correlation_matrix(
        {
            key: StrategyEquityCurve(
                strategy_name=key,
                pair="",
                timeframe="",
                equity_curve=[],
                returns=signal_vectors[key],
                total_pnl=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                trade_count=0,
            )
            for key in keys
        }
    )


@dataclass
class SelectionResult:
    selected: list[str]
    weights: dict[str, float]
    correlation_matrix: dict[str, dict[str, float]]
    skipped: list[tuple[str, str]]


def select_least_correlated(
    inventory_results: dict[str, StrategyInventoryResult],
    correlation: CorrelationResult,
    max_strategies: int = 4,
    min_pf: float = 1.0,
    min_wr: float = 0.45,
    max_pairwise_corr: float = 0.5,
) -> SelectionResult:
    candidates: list[str] = []
    for key, inv in inventory_results.items():
        if inv.metrics.profit_factor >= min_pf and inv.metrics.win_rate >= min_wr:
            candidates.append(key)

    candidates.sort(key=lambda k: inventory_results[k].metrics.sharpe_ratio, reverse=True)

    selected: list[str] = []
    skipped: list[tuple[str, str]] = []

    for candidate in candidates:
        if len(selected) >= max_strategies:
            break

        can_add = True
        for existing in selected:
            corr = abs(correlation.matrix.get(candidate, {}).get(existing, 0.0))
            if corr > max_pairwise_corr:
                skipped.append((candidate, existing))
                can_add = False
                break

        if can_add:
            selected.append(candidate)

    weights: dict[str, float] = {}
    if selected:
        variances: dict[str, float] = {}
        for key in selected:
            ec_returns = [1.0 if s.direction != 0 else 0.0 for s in inventory_results[key].signals]
            if len(ec_returns) < 2:
                variances[key] = 1.0
                continue
            mean_r = sum(ec_returns) / len(ec_returns)
            var = sum((r - mean_r) ** 2 for r in ec_returns) / (len(ec_returns) - 1)
            variances[key] = var if var > 0 else 0.0001

        inv_var = {k: 1.0 / v for k, v in variances.items()}
        total_inv = sum(inv_var.values())
        weights = {k: v / total_inv for k, v in inv_var.items()}

    sub_matrix: dict[str, dict[str, float]] = {}
    for key_a in selected:
        sub_matrix[key_a] = {}
        for key_b in selected:
            sub_matrix[key_a][key_b] = correlation.matrix.get(key_a, {}).get(key_b, 0.0)

    return SelectionResult(
        selected=selected,
        weights=weights,
        correlation_matrix=sub_matrix,
        skipped=skipped,
    )
