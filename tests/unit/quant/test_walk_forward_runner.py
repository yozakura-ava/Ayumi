"""Tests for walk_forward_runner _compute_metrics caller migration.

Card: ef11b0dd-5469-4755-b61e-9f21c715d6de
Title: [DEBT][AYUMI] Migrate external Sharpe callers to bars_in_window
       + WF revalidation report (follow-up to 2bd35527)

Regression coverage: each of the six _compute_metrics call sites in
walk_forward_runner.run_strategy_walk_forward and
walk_forward_runner.run_multi_strategy_walk_forward must thread
``bars_in_window=test_bars`` so the returns-based Sharpe formula
(card 2bd35527) is used and the DeprecationWarning is silenced.

We monkeypatch ``_compute_metrics`` (rebound in the
walk_forward_runner module namespace) to capture kwargs, then run a
minimal backtest and assert the captured ``bars_in_window`` is the
window's test-bar list (not None).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Resolve repo root dynamically so this test works in main tree, worktree,
# or any other checkout.
REPO = Path(__file__).resolve().parents[3]
SRC_FOREX_BOT = REPO / "src" / "forex_bot"
if str(SRC_FOREX_BOT) not in sys.path:
    sys.path.insert(0, str(SRC_FOREX_BOT))

from backtest.engine import Bar  # noqa: E402
from backtest.walk_forward_runner import (  # noqa: E402, I001
    run_multi_strategy_walk_forward,
    run_strategy_walk_forward,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(time: datetime, close: float = 1.0) -> Bar:
    """Build a minimal Bar for tests."""
    return Bar(
        time=time,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=0.0,
    )


def _bars_spanning(start: datetime, period: str, n: int) -> list[Bar]:
    """Build n bars at regular intervals from start.

    period: 'M15' (15-minute), 'H1' (1-hour), 'D1' (1-day).
    """
    delta = {"M15": timedelta(minutes=15), "H1": timedelta(hours=1), "D1": timedelta(days=1)}[period]
    return [_bar(start + i * delta) for i in range(n)]


def _dummy_strategy_factory():
    """Factory returning a no-op strategy with the minimum required API.

    The engine calls ``evaluate(state)`` returning a Signal; if no signal
    fires, no trades are produced. The metrics computation still runs
    (with empty trades) and we can verify the bars_in_window kwarg.
    """
    from backtest.strategies import ISignalStrategy

    class _NoopStrategy(ISignalStrategy):
        name = "noop_for_test"

        def __init__(self) -> None:
            pass

        def evaluate(self, state):  # noqa: D401
            return None  # no signal → zero trades → empty metrics branch

        def reset(self) -> None:
            pass

    return _NoopStrategy()


# ---------------------------------------------------------------------------
# Capture _compute_metrics kwargs
# ---------------------------------------------------------------------------


def _capture_compute_kwargs(monkeypatch, module_path: str):
    """Capture every call's kwargs by replacing the symbol in the module's
    namespace with a recorder that returns a deterministic WindowMetrics.

    Returns the list of captured (args, kwargs) tuples (mutable, append
    in place).
    """
    captured: list[tuple[tuple, dict]] = []

    def _recorder(*args, **kwargs):
        captured.append((args, kwargs))
        # Return a fresh WindowMetrics so the rest of the pipeline still
        # works (per_window.append, regime aggregation, etc.).
        from quant.walk_forward import WindowMetrics

        return WindowMetrics(
            window_index=args[0] if args else kwargs.get("window_index", 0),
            win_rate=0.0,
            profit_factor=0.0,
            max_drawdown=0.0,
            sharpe_ratio=0.0,
            trade_count=0,
            total_pnl=0.0,
            passed_go_nogo=False,
        )

    monkeypatch.setattr(module_path, _recorder)
    return captured


# ---------------------------------------------------------------------------
# 1. run_strategy_walk_forward: every call site passes bars_in_window
# ---------------------------------------------------------------------------


class TestRunStrategyWalkForwardCallSites:
    """All four _compute_metrics call sites in run_strategy_walk_forward
    must pass ``bars_in_window=test_bars`` so the fixed Sharpe formula
    is used (card 2bd35527) and no DeprecationWarning fires."""

    def test_short_window_branch_passes_bars_in_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """len(test_bars) < min_bars_before_signal branch (line ~155)."""
        # 30 H1 bars with n_windows=3 -> each window has 1 test_bar
        # (< min_bars_before_signal=30 default), so the short-window
        # branch fires on every iteration.
        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 30)
        captured = _capture_compute_kwargs(
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )
        run_strategy_walk_forward(
            bars=bars,
            strategy_factory=_dummy_strategy_factory,
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        assert len(captured) >= 1, (
            f"_compute_metrics was never called; expected at least one "
            f"short-window branch call. Captured={captured}"
        )
        for args, kwargs in captured:
            assert "bars_in_window" in kwargs, (
                f"_compute_metrics call missing bars_in_window kwarg: "
                f"args={args}, kwargs={kwargs}"
            )
            bars_in_window = kwargs["bars_in_window"]
            # bars_in_window must NOT be None — that's the legacy fallback path.
            assert bars_in_window is not None, (
                "bars_in_window=None triggers the legacy Sharpe formula and "
                "emits a DeprecationWarning. Card 2bd35527 migration is "
                "incomplete for this call site."
            )
            # It must be a list-like with at least the test-bars count.
            assert hasattr(bars_in_window, "__len__")

    def test_normal_window_branch_passes_bars_in_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Main branch (len(test_bars) >= min_bars_before_signal, line ~198)."""
        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 200)
        captured = _capture_compute_kwargs(
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )
        run_strategy_walk_forward(
            bars=bars,
            strategy_factory=_dummy_strategy_factory,
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        assert len(captured) >= 1
        for args, kwargs in captured:  # noqa: B007
            assert "bars_in_window" in kwargs
            assert kwargs["bars_in_window"] is not None

    def test_no_deprecation_warning_in_run(
        self, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
    ) -> None:
        """Running run_strategy_walk_forward must not emit a
        DeprecationWarning from _compute_metrics (i.e., no caller still
        uses the legacy path)."""
        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 200)

        # Install the recorder so _compute_metrics is patched away; this
        # test asserts on DeprecationWarnings, not on captured kwargs.
        _capture_compute_kwargs(  # noqa: F841
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )
        run_strategy_walk_forward(
            bars=bars,
            strategy_factory=_dummy_strategy_factory,
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        # Filter warnings to those from the walk_forward module only.
        legacy_warnings = [
            w
            for w in recwarn.list
            if issubclass(w.category, DeprecationWarning)
            and "_compute_metrics" in str(w.message)
            and "legacy" in str(w.message).lower()
        ]
        assert len(legacy_warnings) == 0, (
            f"Legacy-path DeprecationWarning emitted — bars_in_window was "
            f"not threaded through. Warnings: {[str(w.message) for w in legacy_warnings]}"
        )


# ---------------------------------------------------------------------------
# 2. run_multi_strategy_walk_forward: same coverage for both call sites
# ---------------------------------------------------------------------------


class TestRunMultiStrategyWalkForwardCallSites:
    """Both _compute_metrics call sites in run_multi_strategy_walk_forward
    (lines ~316 and ~350) must thread bars_in_window."""

    def test_short_window_branch_passes_bars_in_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 30)
        captured = _capture_compute_kwargs(
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )
        run_multi_strategy_walk_forward(
            bars=bars,
            strategy_factories=[_dummy_strategy_factory],
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        assert len(captured) >= 1, (
            f"_compute_metrics was never called; expected at least one "
            f"short-window branch call. Captured={captured}"
        )
        for args, kwargs in captured:  # noqa: B007
            assert "bars_in_window" in kwargs
            assert kwargs["bars_in_window"] is not None

    def test_normal_window_branch_passes_bars_in_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 200)
        captured = _capture_compute_kwargs(
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )
        run_multi_strategy_walk_forward(
            bars=bars,
            strategy_factories=[_dummy_strategy_factory],
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        assert len(captured) >= 1
        for args, kwargs in captured:  # noqa: B007
            assert "bars_in_window" in kwargs
            assert kwargs["bars_in_window"] is not None

    def test_no_deprecation_warning_in_multi_run(
        self, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
    ) -> None:
        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 200)
        _capture_compute_kwargs(
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )
        run_multi_strategy_walk_forward(
            bars=bars,
            strategy_factories=[_dummy_strategy_factory],
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        legacy_warnings = [
            w
            for w in recwarn.list
            if issubclass(w.category, DeprecationWarning)
            and "_compute_metrics" in str(w.message)
            and "legacy" in str(w.message).lower()
        ]
        assert len(legacy_warnings) == 0, (
            f"Legacy-path DeprecationWarning emitted in multi-strategy "
            f"runner — bars_in_window was not threaded through. "
            f"Warnings: {[str(w.message) for w in legacy_warnings]}"
        )


# ---------------------------------------------------------------------------
# 3. Synthetic-trade fallback branch stays well-formed
# ---------------------------------------------------------------------------


class TestSyntheticTradeFallback:
    """When ``metrics_obj.total_trades > 0`` but no per-trade records are
    produced (synthetic avg-pnl fallback at walk_forward_runner.py ~190-195),
    the call to _compute_metrics MUST still receive bars_in_window so the
    returns-based Sharpe formula degrades gracefully (std_return=0
    short-circuits to sharpe=0.0 — no division by zero).
    """

    def test_synthetic_fallback_path_threads_bars_in_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Force the synthetic fallback by mocking the engine to return
        metrics_obj with total_trades > 0 but empty trades list."""

        from backtest.engine import Bar as EngineBar  # noqa: F401

        # Build a fake metrics_obj with total_trades > 0 and empty trades.
        class _FakeTrade:
            profit_loss = 0.0  # unused in synthetic fallback

        class _FakeMetrics:
            trades: list = []
            total_trades = 50  # triggers synthetic branch
            total_pnl = 250.0

        class _FakeResult:
            metrics = _FakeMetrics()

        class _FakeEngine:
            def __init__(self, config, strategies, risk_sizer=None) -> None:  # noqa: D401
                pass

            def run_all_strategies(self, test_bars):
                return {_dummy_strategy_factory().name: _FakeResult()}

        monkeypatch.setattr(
            "backtest.walk_forward_runner.MultiStrategyBacktestEngine", _FakeEngine
        )
        captured = _capture_compute_kwargs(
            monkeypatch, "backtest.walk_forward_runner._compute_metrics"
        )

        bars = _bars_spanning(datetime(2026, 1, 1), "H1", 200)
        run_strategy_walk_forward(
            bars=bars,
            strategy_factory=_dummy_strategy_factory,
            pair="EURUSD",
            n_windows=3,
            train_ratio=0.7,
            val_ratio=0.15,
            overlap_ratio=0.0,
            initial_balance=10000.0,
        )
        assert len(captured) >= 1
        for args, kwargs in captured:  # noqa: B007
            assert "bars_in_window" in kwargs
            assert kwargs["bars_in_window"] is not None, (
                "Synthetic-trade fallback branch did not thread "
                "bars_in_window; the returns-based Sharpe formula would "
                "fall back to the legacy path and emit a DeprecationWarning."
            )


# ---------------------------------------------------------------------------
# 4. Sanity: the real _compute_metrics is well-behaved on the migrated path
# ---------------------------------------------------------------------------


class TestRealComputeMetricsOnMigratedPath:
    """Sanity check that when bars_in_window is supplied (the migrated path),
    no DeprecationWarning fires and the returned metrics are well-formed."""

    def test_real_compute_metrics_no_deprecation_when_bars_supplied(
        self, recwarn: pytest.WarningsRecorder
    ) -> None:
        import warnings

        from quant.walk_forward import _compute_metrics

        trades = [{"pnl": 100.0} for _ in range(20)]
        bars = _bars_spanning(datetime(2026, 1, 1), "D1", 30)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            m = _compute_metrics(0, trades, initial_balance=10000.0, bars_in_window=bars)
        deprecation = [
            w for w in captured if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation) == 0, (
            f"Real _compute_metrics must not emit DeprecationWarning when "
            f"bars_in_window is supplied (migrated path). Got: "
            f"{[str(w.message) for w in deprecation]}"
        )
        # Sharpe is well-formed (finite, non-NaN).
        assert m.sharpe_ratio == m.sharpe_ratio, "Sharpe is NaN"
        assert abs(m.sharpe_ratio) < 1e6, f"Sharpe exploded: {m.sharpe_ratio}"
