"""Unit tests for RiskGuard best-day rule enforcement.

Tests that ``RiskGuard._check_best_day_rule`` enforces the FTMO best-day
rule by setting ``_blocked_until`` when the ratio exceeds the configurable
``best_day_enforce_pct`` threshold (default 40%).

Covers:
    1. Enforcement at >40% ratio (default threshold) → _blocked_until set
    2. No enforcement at exactly 40% (strictly > only)
    3. No enforcement below 40%
    4. CRITICAL log at >50% (FTMO hard cap)
    5. Custom enforce_pct configuration
    6. Blocked trading actually prevents signals
    7. Minimum 2 positive days still required
    8. _blocked_until auto-recovers after expiry
    9. Negative days excluded from computation
   10. Best-day rule interacts correctly with can_trade()
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, "src/forex_bot")
sys.path.insert(0, "src/forex_bot/adapters/ctrader")

from adapters.ctrader.risk_guard import (  # noqa: I001
    DailyTradingStats,
    FTMOConfig,
    FTMOProfile,
    RiskGuard,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def guard():
    """Standard RiskGuard with default FTMO config (no state persistence)."""
    with patch.object(RiskGuard, "_restore_state"):
        rg = RiskGuard(
            ftmo_config=FTMOConfig(),
            starting_balance=10000.0,
            state_path="/tmp/test_risk_guard_no_state.json",  # noqa: S108
        )
        return rg


def _add_day(guard: RiskGuard, d: date, pnl: float) -> None:
    """Append a DailyTradingStats entry to _daily_stats."""
    guard._daily_stats.append(DailyTradingStats(date=d, trades_count=3, pnl=pnl))


# ── 1. Enforcement at >40% ratio ───────────────────────────────────────────


class TestEnforcementTriggered:
    """Best-day ratio > 40% (default enforce threshold) → _blocked_until set."""

    def test_ratio_above_enforce_sets_blocked_until(self, guard):
        """60% ratio should trigger enforcement (40% default threshold)."""
        _add_day(guard, date(2026, 7, 8), 150.0)  # best day
        _add_day(guard, date(2026, 7, 9), 100.0)  # today

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is not None
        # Should be in the future
        assert guard._blocked_until > datetime.now(timezone.utc)

    def test_enforcement_blocked_until_is_next_utc_midnight(self, guard):
        """_blocked_until should be set to next UTC midnight."""
        _add_day(guard, date(2026, 7, 8), 200.0)
        _add_day(guard, date(2026, 7, 9), 50.0)  # 200/250 = 80%

        guard._check_best_day_rule(guard._daily_stats[-1])

        now = datetime.now(timezone.utc)
        expected_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        assert guard._blocked_until == expected_midnight

    def test_enforcement_does_not_set_circuit_breaker(self, guard):
        """Best-day enforcement should NOT trigger permanent circuit breaker.

        Unlike total drawdown, this is a daily-level halt that auto-recovers.
        """
        _add_day(guard, date(2026, 7, 8), 200.0)
        _add_day(guard, date(2026, 7, 9), 50.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is not None
        assert guard._circuit_breaker_triggered is False


# ── 2. No enforcement at exactly 40% ───────────────────────────────────────


class TestBoundaryNoEnforcement:
    """Ratio exactly at the enforcement threshold → no enforcement (strictly >)."""

    def test_ratio_exactly_40_pct_no_enforcement(self, guard):
        """best=100, total=250 → 40% exactly → no enforcement."""
        # 3 days: 100, 100, 50 → total=250, best=100 → 40%
        _add_day(guard, date(2026, 7, 7), 100.0)
        _add_day(guard, date(2026, 7, 8), 50.0)
        _add_day(guard, date(2026, 7, 9), 100.0)  # best day

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is None


# ── 3. No enforcement below 40% ────────────────────────────────────────────


class TestBelowEnforcementThreshold:
    """Ratio below 40% → no enforcement."""

    def test_ratio_33_pct_no_enforcement(self, guard):
        """3 equal days → best is 33% → no enforcement."""
        _add_day(guard, date(2026, 7, 7), 100.0)
        _add_day(guard, date(2026, 7, 8), 100.0)
        _add_day(guard, date(2026, 7, 9), 100.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is None

    def test_ratio_25_pct_no_enforcement(self, guard):
        """4 equal days → best is 25% → no enforcement."""
        _add_day(guard, date(2026, 7, 6), 100.0)
        _add_day(guard, date(2026, 7, 7), 100.0)
        _add_day(guard, date(2026, 7, 8), 100.0)
        _add_day(guard, date(2026, 7, 9), 100.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is None


# ── 4. CRITICAL log at >50% (FTMO hard cap) ────────────────────────────────


class TestHardCapLogging:
    """Ratio > 50% → CRITICAL log in addition to enforcement."""

    def test_hard_cap_breach_logs_critical(self, guard, caplog):
        """80% ratio should produce a CRITICAL log for the FTMO hard cap."""
        _add_day(guard, date(2026, 7, 8), 200.0)
        _add_day(guard, date(2026, 7, 9), 50.0)  # 200/250 = 80%

        with caplog.at_level("CRITICAL", logger="adapters.ctrader.risk_guard"):
            guard._check_best_day_rule(guard._daily_stats[-1])

        critical_msgs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_msgs) >= 1
        assert "HARD CAP" in critical_msgs[0].message


# ── 5. Custom enforce_pct configuration ────────────────────────────────────


class TestCustomEnforcePct:
    """Custom ``best_day_enforce_pct`` overrides default 40%."""

    def test_custom_enforce_pct_30(self):
        """enforce_pct=0.30 → 33% ratio triggers enforcement."""
        custom_config = FTMOConfig(best_day_enforce_pct=0.30)
        with patch.object(RiskGuard, "_restore_state"):
            guard = RiskGuard(
                ftmo_config=custom_config,
                starting_balance=10000.0,
                state_path="/tmp/test_risk_guard_no_state.json",  # noqa: S108
            )

        # 3 days: 100, 100, 100 → 33.3% > 30% → enforcement
        _add_day(guard, date(2026, 7, 7), 100.0)
        _add_day(guard, date(2026, 7, 8), 100.0)
        _add_day(guard, date(2026, 7, 9), 100.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is not None

    def test_custom_enforce_pct_60_no_trigger(self):
        """enforce_pct=0.60 → 55% ratio does NOT trigger enforcement."""
        custom_config = FTMOConfig(best_day_enforce_pct=0.60)
        with patch.object(RiskGuard, "_restore_state"):
            guard = RiskGuard(
                ftmo_config=custom_config,
                starting_balance=10000.0,
                state_path="/tmp/test_risk_guard_no_state.json",  # noqa: S108
            )

        # 2 days: 55, 45 → best=55% < 60% → no enforcement
        _add_day(guard, date(2026, 7, 8), 55.0)
        _add_day(guard, date(2026, 7, 9), 45.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is None

    def test_default_enforce_pct_is_40(self):
        """FTMOConfig() should default best_day_enforce_pct to 0.40."""
        config = FTMOConfig()
        assert config.best_day_enforce_pct == 0.40

    def test_profile_default_is_40(self):
        """FTMOProfile() should default best_day_enforce_pct to 0.40."""
        profile = FTMOProfile()
        assert profile.best_day_enforce_pct == 0.40


# ── 6. Blocked trading prevents signals ────────────────────────────────────


class TestBlockedTradingPreventsSignals:
    """When _blocked_until is set by best-day rule, check_signal blocks trades."""

    def test_check_signal_blocked_after_enforcement(self, guard):
        """After best-day enforcement, check_signal should reject."""

        _add_day(guard, date(2026, 7, 8), 200.0)
        _add_day(guard, date(2026, 7, 9), 50.0)  # 80% → enforcement

        guard._check_best_day_rule(guard._daily_stats[-1])
        assert guard._blocked_until is not None

        # Try to trade — should be blocked
        from unittest.mock import MagicMock

        mock_signal = MagicMock()
        result = guard.check_signal(mock_signal)

        assert result.allowed is False
        assert "blocked" in result.message.lower() or "circuit" in result.message.lower()


# ── 7. Minimum 2 positive days still required ──────────────────────────────


class TestMinimumPositiveDays:
    """Best-day rule enforcement requires ≥2 positive days."""

    def test_single_positive_day_no_enforcement(self, guard):
        """One positive day → no enforcement possible."""
        _add_day(guard, date(2026, 7, 8), 500.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is None

    def test_zero_positive_days_no_enforcement(self, guard):
        """No positive days → no enforcement."""
        _add_day(guard, date(2026, 7, 8), -100.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        assert guard._blocked_until is None


# ── 8. Auto-recovery after _blocked_until expiry ───────────────────────────


class TestAutoRecovery:
    """_blocked_until from best-day rule auto-recovers after expiry."""

    def test_blocked_until_in_past_allows_trading(self, guard):
        """If _blocked_until is in the past, trading should be allowed again."""
        # Set _blocked_until to 1 second ago
        guard._blocked_until = datetime.now(timezone.utc) - timedelta(seconds=1)

        # check_signal_internal checks _blocked_until and clears it if expired
        # The guard should allow trading after expiry
        from unittest.mock import MagicMock

        mock_signal = MagicMock()
        # Set up mock to pass other checks
        mock_signal.direction = None
        mock_signal.volume = 0.01
        mock_signal.entry_price = 1.0
        mock_signal.stop_loss = 0.99
        mock_signal.take_profit_1 = 1.02
        mock_signal.symbol = "EURUSD"
        mock_signal.confidence = 1.0
        mock_signal.strategy_id = "test"

        result = guard.check_signal(mock_signal)
        # Should NOT be blocked by _blocked_until (may fail on other checks,
        # but the message should not mention "blocked until")
        assert "blocked until" not in result.message.lower()


# ── 9. Negative days excluded ──────────────────────────────────────────────


class TestNegativeDaysExcluded:
    """Negative-PnL days are excluded from best-day ratio computation."""

    def test_negative_day_does_not_affect_ratio(self, guard):
        """Negative day excluded: 200+100 (positive only) → 66.7% → enforce."""
        _add_day(guard, date(2026, 7, 7), -500.0)  # excluded
        _add_day(guard, date(2026, 7, 8), 200.0)  # best
        _add_day(guard, date(2026, 7, 9), 100.0)

        guard._check_best_day_rule(guard._daily_stats[-1])

        # 200 / (200 + 100) = 66.7% > 40% → enforcement
        assert guard._blocked_until is not None


# ── 10. Integration: can_trade() respects enforcement ──────────────────────


class TestCanTradeIntegration:
    """can_trade / check_signal respects _blocked_until from best-day rule."""

    def test_today_stats_added_then_check_blocks(self, guard):
        """Simulate: stats added → check_best_day → check_signal blocked."""
        _add_day(guard, date(2026, 7, 8), 300.0)
        _add_day(guard, date(2026, 7, 9), 100.0)  # 300/400 = 75% → enforce

        # Before check_best_day_rule: not blocked
        assert guard._blocked_until is None

        # After check_best_day_rule: blocked
        guard._check_best_day_rule(guard._daily_stats[-1])
        assert guard._blocked_until is not None

        # check_signal should reject
        from unittest.mock import MagicMock

        mock_signal = MagicMock()
        result = guard.check_signal(mock_signal)
        assert result.allowed is False
