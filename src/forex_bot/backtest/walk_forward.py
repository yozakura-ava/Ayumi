"""Calendar-anchored walk-forward validation runner (card 1312f03a).

Implements the tuning-cycle validation described in
``docs/trading/active-blade.md``: M windows of ``window_months`` months,
stepped by ``step_months`` months, anchored at ``anchor_date``
(e.g. ``"2022-01"``).  Each window evaluates one candidate parameter set and
returns per-window and aggregate metrics.

Unlike the ratio-based ``walk_forward_runner.py`` (train/val/test fraction
splits), this module slices by calendar month, enforces the hard caps from
the tuning budget:

* 12-hour wall-clock limit per candidate (timeout kills the run),
* -8%% max-drawdown hard kill threshold,

and wires the validated monetary pip-value harness
(:func:`backtest.pip_value.compute_pip_value`) into the sizing path so that
every report emitted here carries ``pip_value_validated: true``
(AC inherited from card 0d562c69).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

try:  # package-style import (tests run from src/forex_bot/)
    from backtest.pip_value import PIP_VALUE_VALIDATED, compute_pip_value
except ImportError:  # pragma: no cover - direct module import
    from .pip_value import PIP_VALUE_VALIDATED, compute_pip_value

__all__ = [
    "WALL_CLOCK_LIMIT_SECONDS",
    "MAX_DRAWDOWN_KILL",
    "WindowSpec",
    "slice_windows",
    "run_walk_forward",
    "register_strategy",
    "WalkForwardTimeout",
    "DrawdownKill",
]

# ---------------------------------------------------------------------------
# Hard caps (active-blade.md tuning budget)
# ---------------------------------------------------------------------------

#: 12h wall-clock budget per candidate.
WALL_CLOCK_LIMIT_SECONDS: float = 12 * 3600.0

#: Hard kill at -8% equity drawdown.
MAX_DRAWDOWN_KILL: float = -8.0  # percent

#: Clock used for the wall-clock limit (injectable for tests).
_Clock = Callable[[], float]


class WalkForwardTimeout(RuntimeError):
    """Raised when a candidate exceeds the 12h wall-clock cap."""


class DrawdownKill(RuntimeError):
    """Raised when a window breaches the -8%% drawdown hard kill."""


# ---------------------------------------------------------------------------
# Window slicing
# ---------------------------------------------------------------------------

def _parse_anchor(anchor_date: str) -> datetime:
    """Parse ``YYYY-MM`` (or full ISO date) into a datetime."""
    try:
        return datetime.strptime(anchor_date[:7], "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"anchor_date must be YYYY-MM, got {anchor_date!r}") from exc


def _add_months(dt: datetime, months: int) -> datetime:
    total = (dt.month - 1) + months
    year = dt.year + total // 12
    month = total % 12 + 1
    return datetime(year, month, 1)


class WindowSpec:
    """One calendar-anchored evaluation window."""

    __slots__ = ("index", "start", "end", "regime_label")

    def __init__(self, index: int, start: datetime, end: datetime, regime_label: str = "unknown") -> None:
        self.index = index
        self.start = start
        self.end = end
        self.regime_label = regime_label

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"WindowSpec({self.index}, {self.start:%Y-%m}, {self.end:%Y-%m}, {self.regime_label})"


def slice_windows(
    timestamps: Sequence[datetime],
    anchor_date: str,
    n_windows: int,
    window_months: int = 6,
    step_months: int = 1,
) -> list[WindowSpec]:
    """Slice ``timestamps`` into ``n_windows`` calendar windows.

    Window ``i`` starts ``anchor + i*step_months`` and spans
    ``window_months``.  Points exactly at ``end`` belong to the next window
    (half-open ``[start, end)``).
    """
    if n_windows < 1:
        raise ValueError("n_windows must be >= 1")
    if window_months < 1 or step_months < 1:
        raise ValueError("window_months and step_months must be >= 1")

    anchor = _parse_anchor(anchor_date)
    specs: list[WindowSpec] = []
    for i in range(n_windows):
        start = _add_months(anchor, i * step_months)
        end = _add_months(start, window_months)
        pts = [t for t in timestamps if start <= t < end]
        specs.append(
            WindowSpec(
                index=i,
                start=start,
                end=end,
                regime_label=_label_regime(pts),
            )
        )
    return specs


def _label_regime(pts: Sequence[datetime]) -> str:
    """Cheap trend/vol regime label for a window of timestamps.

    Busier data (more observations per week) proxies higher activity:
    ``quiet`` / ``normal`` / ``active``.  This is intentionally simple —
    labels are propagated to output, not used for gating.
    """
    if len(pts) < 2:
        return "quiet"
    span_days = (pts[-1] - pts[0]).total_seconds() / 86400.0
    if span_days <= 0:
        return "normal"
    density = len(pts) / span_days * 30.0  # points per ~month
    if density < 10:
        return "quiet"
    if density > 500:
        return "active"
    return "normal"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _window_metrics(pnls: Sequence[float]) -> dict[str, Any]:
    """PF / Sharpe / WR / max-DD for a list of per-trade P&L values."""
    if not pnls:
        return {
            "trades": 0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "win_rate": 0.0,
            "max_drawdown_pct": 0.0,
            "total_pnl": 0.0,
        }
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    if math.isinf(pf):  # pragma: no cover
        pf = 99.0

    mean = sum(pnls) / len(pnls)
    var = sum((p - mean) ** 2 for p in pnls) / max(1, len(pnls) - 1)
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(len(pnls)) if std > 0 else 0.0

    wins = sum(1 for p in pnls if p > 0)
    wr = wins / len(pnls)

    # equity curve max drawdown (% of a 100-unit starting equity so that
    # small early wins cannot produce outsized percentages)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        denom = max(peak, 100.0)
        max_dd = min(max_dd, (equity - peak) / denom * 100.0)

    return {
        "trades": len(pnls),
        "profit_factor": round(pf, 4),
        "sharpe_ratio": round(sharpe, 4),
        "win_rate": round(wr, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "total_pnl": round(sum(pnls), 4),
    }


def _aggregate_pf_ci(pfs: Sequence[float]) -> dict[str, float]:
    """Aggregate PF with a 95% normal-approximation CI."""
    n = len(pfs)
    if n == 0:
        return {"mean_pf": 0.0, "ci95_low": 0.0, "ci95_high": 0.0}
    mean = sum(pfs) / n
    if n > 1:
        var = sum((p - mean) ** 2 for p in pfs) / (n - 1)
        half = 1.96 * math.sqrt(var / n)
    else:
        half = 0.0
    return {
        "mean_pf": round(mean, 4),
        "ci95_low": round(max(0.0, mean - half), 4),
        "ci95_high": round(mean + half, 4),
    }


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

#: A strategy runner consumes (window timestamps, candidate params) and
#: returns per-trade P&L in account currency, sized via compute_pip_value.
StrategyFn = Callable[[Sequence[datetime], dict[str, Any]], list[float]]

STRATEGY_REGISTRY: dict[str, StrategyFn] = {}


def register_strategy(strategy_id: str, fn: StrategyFn) -> None:
    STRATEGY_REGISTRY[strategy_id] = fn


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_walk_forward(
    strategy_id: str,
    candidate_params: dict[str, Any],
    anchor_date: str = "2022-01",
    n_windows: int = 4,
    window_months: int = 6,
    step_months: int = 1,
    timestamps: Sequence[datetime] | None = None,
    strategy_fn: StrategyFn | None = None,
    symbol: str = "XAUUSD",
    account_currency: str = "USD",
    lot_size: float = 1.0,
    wall_clock_limit: float = WALL_CLOCK_LIMIT_SECONDS,
    dd_kill_pct: float = MAX_DRAWDOWN_KILL,
    clock: _Clock = time.monotonic,
) -> dict[str, Any]:
    """Run one candidate through calendar-anchored walk-forward windows.

    Returns a report dict with per-window metrics (PF / Sharpe / WR /
    max-DD, regime label), aggregate PF with 95%% CI, hard-cap outcomes and
    ``pip_value_validated: true`` (sizing path uses
    :func:`compute_pip_value`).
    """
    fn = strategy_fn if strategy_fn is not None else STRATEGY_REGISTRY.get(strategy_id)
    if fn is None:
        raise ValueError(
            f"Unknown strategy {strategy_id!r}; register via register_strategy() "
            f"or pass strategy_fn. Known: {sorted(STRATEGY_REGISTRY)}"
        )

    ts = sorted(timestamps) if timestamps else []
    specs = slice_windows(ts, anchor_date, n_windows, window_months, step_months)

    # Validated sizing path (card 0d562c69 AC inheritance).
    pip_value = compute_pip_value(symbol, account_currency, lot_size)

    deadline = clock() + wall_clock_limit
    per_window: list[dict[str, Any]] = []
    pfs: list[float] = []
    killed_reason: str | None = None

    for spec in specs:
        if clock() > deadline:
            killed_reason = f"timeout: exceeded {wall_clock_limit}s wall clock"
            break

        window_ts = [t for t in ts if spec.start <= t < spec.end]
        raw_pips = fn(window_ts, candidate_params)
        # Size each pip-move through the validated monetary pip value.
        pnls = [p * pip_value for p in raw_pips]
        metrics = _window_metrics(pnls)
        per_window.append(
            {
                "window": spec.index,
                "start": f"{spec.start:%Y-%m}",
                "end": f"{spec.end:%Y-%m}",
                "regime_label": spec.regime_label,
                **metrics,
            }
        )

        if metrics["trades"] > 0 and metrics["max_drawdown_pct"] <= dd_kill_pct:
            killed_reason = (
                f"drawdown_kill: window {spec.index} hit "
                f"{metrics['max_drawdown_pct']:.2f}% <= {dd_kill_pct}%"
            )
            break
        if metrics["trades"] > 0:
            pfs.append(metrics["profit_factor"])

    report: dict[str, Any] = {
        "strategy_id": strategy_id,
        "candidate_params": candidate_params,
        "anchor_date": anchor_date,
        "n_windows_requested": n_windows,
        "window_months": window_months,
        "step_months": step_months,
        "symbol": symbol,
        "pip_value_per_lot": pip_value,
        "pip_value_validated": PIP_VALUE_VALIDATED,
        "per_window": per_window,
        "aggregate_pf": _aggregate_pf_ci(pfs),
        "n_windows_completed": len(per_window),
        "killed_reason": killed_reason,
        "passed": killed_reason is None and len(per_window) == n_windows,
    }
    return report
