from __future__ import annotations

from dataclasses import dataclass, field

# Production-grade go/no-go gating threshold.
# Use this when evaluating a strategy for live deployment, not for dev/CI
# walk-forward checks. The default min_windows_passed=3 below is the
# canonical 3-of-5 rule used by research and backtests; production paths
# should override with AggregateCriteria(min_windows_passed=PRODUCTION_MIN_WINDOWS_PASSED).
PRODUCTION_MIN_WINDOWS_PASSED = 5


@dataclass(frozen=True)
class EvaluateResult:
    passed: bool
    details: str = ""


@dataclass(frozen=True)
class PerWindowCheck:
    metric: str
    value: float
    threshold: float
    operator: str
    passed: bool


@dataclass(frozen=True)
class PerWindowCriteria:
    min_trades: int = 5
    win_rate: float = 0.55
    profit_factor: float = 1.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.10

    def evaluate(
        self,
        trade_count: int,
        win_rate: float,
        profit_factor: float,
        total_pnl: float,
        max_drawdown: float,
    ) -> PerWindowEvaluateResult:
        checks: dict[str, PerWindowCheck] = {}

        checks["min_trades"] = PerWindowCheck(
            metric="min_trades",
            value=float(trade_count),
            threshold=float(self.min_trades),
            operator=">=",
            passed=trade_count >= self.min_trades,
        )
        checks["win_rate"] = PerWindowCheck(
            metric="win_rate",
            value=win_rate,
            threshold=self.win_rate,
            operator=">",
            passed=win_rate > self.win_rate,
        )
        checks["profit_factor"] = PerWindowCheck(
            metric="profit_factor",
            value=profit_factor,
            threshold=self.profit_factor,
            operator=">",
            passed=profit_factor > self.profit_factor,
        )
        checks["total_pnl"] = PerWindowCheck(
            metric="total_pnl",
            value=total_pnl,
            threshold=self.total_pnl,
            operator=">",
            passed=total_pnl > self.total_pnl,
        )
        checks["max_drawdown"] = PerWindowCheck(
            metric="max_drawdown",
            value=max_drawdown,
            threshold=self.max_drawdown,
            operator="<",
            passed=max_drawdown < self.max_drawdown,
        )

        passed = all(c.passed for c in checks.values())
        return PerWindowEvaluateResult(passed=passed, checks=checks)


@dataclass(frozen=True)
class PerWindowEvaluateResult:
    passed: bool
    checks: dict[str, PerWindowCheck] = field(default_factory=dict)


@dataclass(frozen=True)
class AggregateCheck:
    metric: str
    value: float
    threshold: float
    operator: str
    passed: bool


@dataclass(frozen=True)
class AggregateCriteria:
    min_total_trades: int = 50
    # Verbal rule: 3-of-5 windows must pass. Code defaulted to 2 historically;
    # corrected 2026-07-08 to match the documented rule. Production-grade
    # gating should use PRODUCTION_MIN_WINDOWS_PASSED (5) instead of the default.
    min_windows_passed: int = 3
    min_total_windows: int = 3
    p_value_threshold: float = 0.10

    def evaluate(
        self,
        total_trades: int,
        windows_passed: int,
        total_windows: int,
        p_value: float | None = None,
    ) -> AggregateEvaluateResult:
        checks: dict[str, AggregateCheck] = {}

        checks["min_total_trades"] = AggregateCheck(
            metric="min_total_trades",
            value=float(total_trades),
            threshold=float(self.min_total_trades),
            operator=">=",
            passed=total_trades >= self.min_total_trades,
        )
        checks["min_windows_passed"] = AggregateCheck(
            metric="min_windows_passed",
            value=float(windows_passed),
            threshold=float(self.min_windows_passed),
            operator=">=",
            passed=windows_passed >= self.min_windows_passed,
        )
        checks["min_total_windows"] = AggregateCheck(
            metric="min_total_windows",
            value=float(total_windows),
            threshold=float(self.min_total_windows),
            operator=">=",
            passed=total_windows >= self.min_total_windows,
        )

        if p_value is not None:
            checks["p_value"] = AggregateCheck(
                metric="p_value",
                value=p_value,
                threshold=self.p_value_threshold,
                operator="<",
                passed=p_value < self.p_value_threshold,
            )

        passed = all(c.passed for c in checks.values())
        return AggregateEvaluateResult(passed=passed, checks=checks)


@dataclass(frozen=True)
class AggregateEvaluateResult:
    passed: bool
    checks: dict[str, AggregateCheck] = field(default_factory=dict)


@dataclass(frozen=True)
class FullGoNoGoResult:
    go: bool
    windows_passed: int
    total_windows: int
    total_trades: int
    per_window: list[PerWindowEvaluateResult] = field(default_factory=list)
    aggregate: AggregateEvaluateResult | None = None


CANONICAL_PER_WINDOW = PerWindowCriteria()
CANONICAL_AGGREGATE = AggregateCriteria()


def evaluate_window(
    trade_count: int,
    win_rate: float,
    profit_factor: float,
    total_pnl: float,
    max_drawdown: float,
    criteria: PerWindowCriteria | None = None,
) -> PerWindowEvaluateResult:
    if criteria is None:
        criteria = CANONICAL_PER_WINDOW
    return criteria.evaluate(trade_count, win_rate, profit_factor, total_pnl, max_drawdown)


def evaluate_aggregate(
    total_trades: int,
    windows_passed: int,
    total_windows: int,
    criteria: AggregateCriteria | None = None,
    p_value: float | None = None,
) -> AggregateEvaluateResult:
    if criteria is None:
        criteria = CANONICAL_AGGREGATE
    return criteria.evaluate(total_trades, windows_passed, total_windows, p_value=p_value)


def evaluate_full(
    windows: list[dict],
    per_window_criteria: PerWindowCriteria | None = None,
    aggregate_criteria: AggregateCriteria | None = None,
) -> FullGoNoGoResult:
    if per_window_criteria is None:
        per_window_criteria = CANONICAL_PER_WINDOW
    if aggregate_criteria is None:
        aggregate_criteria = CANONICAL_AGGREGATE

    per_window_results: list[PerWindowEvaluateResult] = []
    total_trades = 0

    for w in windows:
        r = evaluate_window(
            trade_count=w.get("trade_count", 0),
            win_rate=w.get("win_rate", 0.0),
            profit_factor=w.get("profit_factor", 0.0),
            total_pnl=w.get("total_pnl", 0.0),
            max_drawdown=w.get("max_drawdown", 1.0),
            criteria=per_window_criteria,
        )
        per_window_results.append(r)
        total_trades += w.get("trade_count", 0)

    windows_passed = sum(1 for r in per_window_results if r.passed)
    total_windows = len(windows)

    agg_result = evaluate_aggregate(
        total_trades=total_trades,
        windows_passed=windows_passed,
        total_windows=total_windows,
        criteria=aggregate_criteria,
    )

    return FullGoNoGoResult(
        go=agg_result.passed,
        windows_passed=windows_passed,
        total_windows=total_windows,
        total_trades=total_trades,
        per_window=per_window_results,
        aggregate=agg_result,
    )
