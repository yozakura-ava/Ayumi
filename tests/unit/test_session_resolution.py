"""Tests for MarketState session resolution in runtime code.

Verifies that MarketState constructed via StrategyExecutor.try_evaluate()
gets current_session resolved from determine_session() instead of defaulting
to SessionType.OUTSIDE.
"""

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

# Ensure src is on path
sys.path.insert(0, "src/forex_bot")

from backtest.engine import Bar
from backtest.types import SessionType, determine_session


class TestDetermineSession:
    """Test the determine_session function directly."""

    def test_london_session(self):
        """10:00 UTC falls in London session (08-12 UTC range in backtest types)."""
        dt = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
        result = determine_session(dt)
        assert result == SessionType.LONDON

    def test_ny_session(self):
        """17:00 UTC falls in NY_PM session (16-20 UTC range)."""
        dt = datetime(2026, 7, 2, 17, 0, tzinfo=timezone.utc)
        result = determine_session(dt)
        assert result == SessionType.NY_PM

    def test_ny_am_session(self):
        """13:00 UTC falls in NY_AM session (12-16 UTC range)."""
        dt = datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc)
        result = determine_session(dt)
        assert result == SessionType.NY_AM

    def test_asian_session(self):
        """03:00 UTC falls in Asian session (00-06 UTC range)."""
        dt = datetime(2026, 7, 2, 3, 0, tzinfo=timezone.utc)
        result = determine_session(dt)
        assert result == SessionType.ASIAN

    def test_outside_session(self):
        """06:00 UTC is outside all sessions."""
        dt = datetime(2026, 7, 2, 6, 0, tzinfo=timezone.utc)
        result = determine_session(dt)
        assert result == SessionType.OUTSIDE


class TestStrategyExecutorSessionResolution:
    """Test that StrategyExecutor.try_evaluate() resolves current_session."""

    def _make_bar(self, hour: int, close: float = 1.1000) -> Bar:
        return Bar(
            time=datetime(2026, 7, 2, hour, 0, tzinfo=timezone.utc),
            open=close,
            high=close + 0.0005,
            low=close - 0.0005,
            close=close,
            volume=1000,
        )

    def test_session_resolved_london(self):
        """When the latest bar is in London hours, current_session should be LONDON."""
        from engine.strategy_executor import StrategyExecutor

        # Build a minimal executor with enough bars (latest bar at 11:00 = London)
        bars = [self._make_bar(h) for h in range(7, 12)]

        slot = MagicMock()
        slot.id = "test-london"
        slot.symbol = "GBPUSD"
        slot.timeframe = "M15"

        strategy = MagicMock()
        strategy.evaluate = MagicMock(return_value=None)

        executor = StrategyExecutor(slot=slot, strategy=strategy, min_bars=1)
        executor._bars = bars
        executor._current_bar = None
        executor._bar_closed = True

        # Call try_evaluate; strategy returns None so no signal, but
        # the state should still be constructed with the right session.
        captured_state = []

        def capture(state):
            captured_state.append(state)
            return None

        strategy.evaluate = capture
        executor.try_evaluate()

        assert len(captured_state) == 1
        assert captured_state[0].current_session == SessionType.LONDON

    def test_session_resolved_ny(self):
        """When the latest bar is in NY hours, current_session should be NY."""
        from engine.strategy_executor import StrategyExecutor

        bars = [self._make_bar(h) for h in range(13, 18)]

        slot = MagicMock()
        slot.id = "test-ny"
        slot.symbol = "EURUSD"
        slot.timeframe = "M15"

        strategy = MagicMock()

        executor = StrategyExecutor(slot=slot, strategy=strategy, min_bars=1)
        executor._bars = bars
        executor._current_bar = None
        executor._bar_closed = True

        captured_state = []

        def capture(state):
            captured_state.append(state)
            return None

        strategy.evaluate = capture
        executor.try_evaluate()

        assert len(captured_state) == 1
        assert captured_state[0].current_session == SessionType.NY_PM

    def test_session_not_outside_during_active_hours(self):
        """During active session hours, current_session must not be OUTSIDE."""
        from engine.strategy_executor import StrategyExecutor

        bars = [self._make_bar(h) for h in range(9, 14)]

        slot = MagicMock()
        slot.id = "test-not-outside"
        slot.symbol = "GBPUSD"
        slot.timeframe = "M15"

        strategy = MagicMock()

        executor = StrategyExecutor(slot=slot, strategy=strategy, min_bars=1)
        executor._bars = bars
        executor._current_bar = None
        executor._bar_closed = True

        captured_state = []

        def capture(state):
            captured_state.append(state)
            return None

        strategy.evaluate = capture
        executor.try_evaluate()

        assert len(captured_state) == 1
        assert captured_state[0].current_session != SessionType.OUTSIDE
