"""Tests for the calendar-anchored walk-forward runner (card 1312f03a).

Covers: window slicing, regime-label propagation, timeout enforcement,
drawdown kill, pip-value validated sizing, aggregate CI.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.walk_forward import (  # noqa: E402
    WALL_CLOCK_LIMIT_SECONDS,
    DrawdownKill,
    _aggregate_pf_ci,
    _window_metrics,
    register_strategy,
    run_walk_forward,
    slice_windows,
)


def _ts(*months: int) -> list[datetime]:
    """Timestamps one per listed month (day 15)."""
    return [datetime(2022 + (m - 1) // 12, (m - 1) % 12 + 1, 15) for m in months]


# ---------------------------------------------------------------------------
# Window slicing
# ---------------------------------------------------------------------------


def test_slice_windows_count_and_bounds():
    specs = slice_windows(_ts(1, 2, 3, 4, 5, 6, 7, 8), "2022-01", n_windows=4,
                          window_months=2, step_months=2)
    assert len(specs) == 4
    assert specs[0].start == datetime(2022, 1, 1)
    assert specs[0].end == datetime(2022, 3, 1)
    assert specs[1].start == datetime(2022, 3, 1)


def test_slice_windows_half_open():
    # A point exactly at a boundary belongs to the later window.
    pts = [datetime(2022, 1, 15), datetime(2022, 2, 15), datetime(2022, 3, 1)]
    specs = slice_windows(pts, "2022-01", n_windows=2, window_months=2, step_months=2)
    w0 = [t for t in pts if specs[0].start <= t < specs[0].end]
    w1 = [t for t in pts if specs[1].start <= t < specs[1].end]
    assert w0 == [datetime(2022, 1, 15), datetime(2022, 2, 15)]
    assert w1 == [datetime(2022, 3, 1)]


def test_slice_windows_anchor_rolling_step():
    specs = slice_windows([], "2022-01", n_windows=3, window_months=6, step_months=1)
    assert specs[0].start == datetime(2022, 1, 1)
    assert specs[1].start == datetime(2022, 2, 1)
    assert specs[2].start == datetime(2022, 3, 1)
    assert all((s.end - s.start).days > 150 for s in specs)  # 6-month span


def test_slice_windows_validation():
    with pytest.raises(ValueError):
        slice_windows([], "2022-01", n_windows=0)
    with pytest.raises(ValueError):
        slice_windows([], "2022-01", n_windows=1, window_months=0)
    with pytest.raises(ValueError):
        slice_windows([], "Jan-2022", n_windows=1)


# ---------------------------------------------------------------------------
# Regime labels
# ---------------------------------------------------------------------------


def test_regime_label_propagates_to_output():
    dense = [datetime(2022, m, d) for m in range(1, 7) for d in range(1, 28, 2)]
    sparse = [datetime(2022, 1, 1), datetime(2022, 3, 1)]

    report_dense = run_walk_forward(
        "probe", {}, timestamps=dense, strategy_fn=lambda ts, p: [1.0] * len(ts),
    )
    report_sparse = run_walk_forward(
        "probe", {}, timestamps=sparse, strategy_fn=lambda ts, p: [1.0] * max(0, len(ts) - 1),
    )
    labels_dense = {w["regime_label"] for w in report_dense["per_window"]}
    labels_sparse = {w["regime_label"] for w in report_sparse["per_window"]}
    assert labels_dense <= {"quiet", "normal", "active"} and labels_dense
    assert labels_sparse == {"quiet"}


def test_regime_label_empty_window():
    specs = slice_windows([], "2022-01", n_windows=1)
    assert specs[0].regime_label == "quiet"


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------


def test_timeout_kills_run():
    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return calls["n"] * 1000.0  # advances 1000s per call, exceeds 12h eventually

    report = run_walk_forward(
        "probe", {}, timestamps=_ts(1, 2, 3, 4, 5, 6), n_windows=4,
        strategy_fn=lambda ts, p: [1.0, -0.5, 1.0],
        wall_clock_limit=3500.0,  # exceeded after ~4 clock calls
        clock=fake_clock,
    )
    assert report["killed_reason"] is not None
    assert "timeout" in report["killed_reason"]
    assert report["passed"] is False


def test_timeout_default_is_12h():
    assert WALL_CLOCK_LIMIT_SECONDS == 12 * 3600.0


def test_timeout_not_triggered_when_within_budget():
    report = run_walk_forward(
        "probe", {}, timestamps=_ts(1, 2), n_windows=2,
        strategy_fn=lambda ts, p: [1.0, 1.0],
        clock=lambda: 0.0,
    )
    assert report["killed_reason"] is None
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# Drawdown kill
# ---------------------------------------------------------------------------


def test_drawdown_kill_triggers():
    # pip-sized losses compounding past -8% equity drawdown.
    report = run_walk_forward(
        "probe", {}, timestamps=_ts(1, 2), n_windows=2,
        strategy_fn=lambda ts, p: [10.0] + [-10.0] * 10,
        dd_kill_pct=-8.0,
    )
    assert report["killed_reason"] is not None
    assert "drawdown_kill" in report["killed_reason"]
    assert report["n_windows_completed"] == 1
    assert report["passed"] is False


def test_drawdown_within_threshold_passes():
    report = run_walk_forward(
        "probe", {}, timestamps=_ts(1, 2), n_windows=2,
        strategy_fn=lambda ts, p: [10.0, -0.5, 10.0, -0.5],
    )
    assert report["killed_reason"] is None
    assert report["passed"] is True


# ---------------------------------------------------------------------------
# Sizing / report shape
# ---------------------------------------------------------------------------


def test_pip_value_validated_flag_and_sizing():
    # XAUUSD, USD account, 1 lot -> $10/pip. A 2-pip win must report $20.
    report = run_walk_forward(
        "probe", {}, timestamps=_ts(1, 2), n_windows=1,
        strategy_fn=lambda ts, p: [2.0],
        symbol="XAUUSD", account_currency="USD", lot_size=1.0,
    )
    assert report["pip_value_validated"] is True
    assert report["pip_value_per_lot"] == pytest.approx(10.0)
    assert report["per_window"][0]["total_pnl"] == pytest.approx(20.0)


def test_report_contains_required_fields():
    report = run_walk_forward(
        "probe", {"fast": 10}, timestamps=_ts(1, 2, 3, 4), n_windows=4,
        strategy_fn=lambda ts, p: [1.0, -0.4, 0.8, 0.6],
    )
    for key in ("per_window", "aggregate_pf", "killed_reason", "passed"):
        assert key in report
    w = report["per_window"][0]
    for key in ("profit_factor", "sharpe_ratio", "win_rate", "max_drawdown_pct", "regime_label"):
        assert key in w
    for key in ("mean_pf", "ci95_low", "ci95_high"):
        assert key in report["aggregate_pf"]


def test_registry_dispatch():
    register_strategy("unit_probe", lambda ts, p: [1.0] * max(0, len(ts)))
    report = run_walk_forward("unit_probe", {}, timestamps=_ts(1, 2), n_windows=1)
    assert report["strategy_id"] == "unit_probe"
    with pytest.raises(ValueError, match="Unknown strategy"):
        run_walk_forward("nope_missing", {}, timestamps=_ts(1), n_windows=1)


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        run_walk_forward("definitely_not_registered", {}, timestamps=_ts(1))


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def test_window_metrics_known_values():
    m = _window_metrics([10.0, -5.0, 10.0, -5.0])
    assert m["trades"] == 4
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["max_drawdown_pct"] == pytest.approx(-5.0)


def test_window_metrics_empty():
    m = _window_metrics([])
    assert m["trades"] == 0 and m["profit_factor"] == 0.0


def test_aggregate_ci_bounds():
    ci = _aggregate_pf_ci([1.5, 2.5])
    assert ci["ci95_low"] <= ci["mean_pf"] <= ci["ci95_high"]
    single = _aggregate_pf_ci([2.0])
    assert single["ci95_low"] == single["ci95_high"] == 2.0


def test_drawdown_kill_exception_exists_for_callers():
    # DrawdownKill / WalkForwardTimeout are part of the public surface.
    assert issubclass(DrawdownKill, RuntimeError)
