"""Tests for BestDayRuleTracker (FTMO funded-phase Best Day Rule).

Covers:
1. Construction validation (threshold range, account_phase values)
2. Inactive during challenge phase (check_entry always allowed)
3. Active during funded phase
4. record_trade_close accumulation and challenge-phase no-op
5. Daily reset at 00:00 in the configured timezone
6. No reset within same local date
7. check_entry allows / blocks based on today's projected share
8. check_entry behavior for non-positive planned profit
9. check_entry behavior when cumulative P/L is non-positive
10. Threshold boundary precision (just under, exactly at, just over)
11. status() returns correct fields
12. Timezone handling: naive (assumed UTC) vs aware datetimes
13. Multi-day scenarios across resets
14. Edge: today's P/L is negative, entry brings it positive
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Load best_day_rule.py directly to avoid the heavy backtest/__init__.py chain.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FXBOT = (_PACKAGE_ROOT / "src" / "forex_bot").resolve()
sys.path.insert(0, str(_FXBOT))

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("best_day_rule", str(_FXBOT / "backtest" / "best_day_rule.py"))
_mod = _ilu.module_from_spec(_spec)
sys.modules["best_day_rule"] = _mod
_spec.loader.exec_module(_mod)

from best_day_rule import (  # noqa: E402, I001
    DEFAULT_RESET_TZ_OFFSET_HOURS,
    DEFAULT_THRESHOLD,
    VALID_PHASES,
    BestDayCheckResult,
    BestDayRuleTracker,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _utc(year, month, day, hour=12, minute=0):
    """Build a UTC-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ── Construction validation ──────────────────────────────────────────────────


class TestConstruction:
    """AC: tracker rejects invalid threshold and account_phase."""

    def test_default_threshold_is_50_percent(self):
        t = BestDayRuleTracker(account_phase="funded")
        assert t.threshold == DEFAULT_THRESHOLD == 0.50

    def test_default_reset_tz_offset_is_cet(self):
        t = BestDayRuleTracker(account_phase="funded")
        assert t.reset_tz_offset_hours == DEFAULT_RESET_TZ_OFFSET_HOURS == 1

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.01, 2.0, -1.0])
    def test_invalid_threshold_rejected(self, bad):
        with pytest.raises(ValueError, match="threshold"):
            BestDayRuleTracker(account_phase="funded", threshold=bad)

    @pytest.mark.parametrize("good", [0.01, 0.25, 0.5, 0.75, 1.0])
    def test_valid_threshold_accepted(self, good):
        t = BestDayRuleTracker(account_phase="funded", threshold=good)
        assert t.threshold == good

    @pytest.mark.parametrize("bad", ["", "live", "demo", "FUNDED", None, 42])
    def test_invalid_account_phase_rejected(self, bad):
        with pytest.raises(ValueError, match="account_phase"):
            BestDayRuleTracker(account_phase=bad)

    @pytest.mark.parametrize("phase", VALID_PHASES)
    def test_valid_phases_accepted(self, phase):
        t = BestDayRuleTracker(account_phase=phase)
        assert t.account_phase == phase


# ── Phase gating ─────────────────────────────────────────────────────────────


class TestPhaseGating:
    """AC: rule only active in funded phase."""

    def test_inactive_in_challenge_phase(self):
        t = BestDayRuleTracker(account_phase="challenge")
        assert t.is_active is False

    def test_active_in_funded_phase(self):
        t = BestDayRuleTracker(account_phase="funded")
        assert t.is_active is True

    def test_check_entry_always_allowed_in_challenge_phase(self):
        """Even an absurdly large planned profit must not be blocked during challenge."""
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12), 1000.0)
        # Now flip to challenge mid-flight (not normally possible in real life,
        # but tests the gating logic in isolation).
        # Construct a fresh challenge-phase tracker instead — simulating a
        # challenge-to-funded promotion.
        chal = BestDayRuleTracker(account_phase="challenge")
        result = chal.check_entry(planned_profit_dollars=1_000_000.0)
        assert result.allowed is True
        assert "inactive" in result.reason


# ── record_trade_close ───────────────────────────────────────────────────────


class TestRecordTradeClose:
    """AC: track daily P/L since funding start."""

    def test_single_trade_accumulates(self):
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 10), 100.0)
        assert t.today_pnl == 100.0
        assert t.cumulative_pnl == 100.0

    def test_multiple_trades_same_day_sum(self):
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 10), 50.0)
        t.record_trade_close(_utc(2026, 7, 12, 14), 30.0)
        t.record_trade_close(_utc(2026, 7, 12, 18), -20.0)
        assert t.today_pnl == 60.0
        assert t.cumulative_pnl == 60.0

    def test_losses_decrease_today_and_cumulative(self):
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 10), 100.0)
        t.record_trade_close(_utc(2026, 7, 12, 11), -40.0)
        assert t.today_pnl == 60.0
        assert t.cumulative_pnl == 60.0

    def test_challenge_phase_record_is_noop(self):
        t = BestDayRuleTracker(account_phase="challenge")
        t.record_trade_close(_utc(2026, 7, 12, 10), 100.0)
        assert t.today_pnl == 0.0
        assert t.cumulative_pnl == 0.0


# ── Daily reset ──────────────────────────────────────────────────────────────


class TestDailyReset:
    """AC: daily reset at 00:00 CE(S)T."""

    def test_no_reset_within_same_day(self):
        # With default reset_tz_offset_hours=1 (CET), 22:00 UTC = 23:00 CET (still 12th).
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 1), 100.0)
        t.record_trade_close(_utc(2026, 7, 12, 22), 50.0)
        assert t.today_pnl == 150.0
        assert t.cumulative_pnl == 150.0
        assert t.last_reset_date == "2026-07-12"

    def test_reset_at_midnight_local(self):
        """A trade after 00:00 CET starts a new day for the tracker."""
        t = BestDayRuleTracker(account_phase="funded", reset_tz_offset_hours=1)  # CET
        # 2026-07-12 22:00 UTC = 2026-07-12 23:00 CET (still 12th)
        t.record_trade_close(_utc(2026, 7, 12, 22), 100.0)
        # 2026-07-12 23:30 UTC = 2026-07-13 00:30 CET (now 13th)
        t.record_trade_close(_utc(2026, 7, 12, 23, 30), 50.0)
        assert t.today_pnl == 50.0, "today should reset at CET midnight"
        assert t.cumulative_pnl == 150.0, "cumulative persists across reset"
        assert t.last_reset_date == "2026-07-13"

    def test_reset_at_midnight_summer_time_cest(self):
        """CEST is UTC+2 (summer). At 22:00 UTC = 00:00 next-day CEST."""
        t = BestDayRuleTracker(account_phase="funded", reset_tz_offset_hours=2)  # CEST
        # 2026-07-12 20:00 UTC = 2026-07-12 22:00 CEST (still 12th)
        t.record_trade_close(_utc(2026, 7, 12, 20), 100.0)
        # 2026-07-12 22:00 UTC = 2026-07-13 00:00 CEST → next day
        t.record_trade_close(_utc(2026, 7, 12, 22), 50.0)
        assert t.today_pnl == 50.0
        assert t.cumulative_pnl == 150.0
        assert t.last_reset_date == "2026-07-13"

    def test_check_entry_triggers_reset_on_new_day(self):
        """A check_entry after midnight should see today_pnl reset."""
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 10), 1000.0)
        # check_entry on the next day at the same time — today should be 0
        result = t.check_entry(
            planned_profit_dollars=10.0,
            now=_utc(2026, 7, 13, 10),
        )
        assert result.today_pnl == 0.0, "today_pnl must reset on new day"
        assert result.cumulative_pnl == 1000.0


# ── check_entry gating logic ─────────────────────────────────────────────────


class TestCheckEntry:
    """AC: check_entry blocks when projected share exceeds threshold."""

    def test_allows_when_cumulative_is_zero(self):
        t = BestDayRuleTracker(account_phase="funded")
        result = t.check_entry(planned_profit_dollars=100.0)
        assert result.allowed is True
        assert "non-positive" in result.reason

    def test_allows_when_cumulative_is_negative(self):
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12), -200.0)
        result = t.check_entry(planned_profit_dollars=50.0)
        assert result.allowed is True
        assert "non-positive" in result.reason

    def test_allows_small_planned_entry(self):
        """today=0, cumulative=1000, planned=100 → share=10% → ALLOW."""
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 10), 1000.0)
        result = t.check_entry(planned_profit_dollars=100.0)
        assert result.allowed is True
        assert result.projected_today_share == pytest.approx(0.10)

    def test_blocks_when_projected_share_exceeds_threshold(self):
        """today=400, cumulative=1000, planned=200 → projected=600 → 60% > 50% → BLOCK.

        Build cumulative by recording yesterday FIRST (so today is just the
        later 400.0 trade, not the combined total). Order matters here
        because record_trade_close performs a daily reset before adding.
        """
        t = BestDayRuleTracker(account_phase="funded")
        # Day 1 (yesterday): +600 → cumulative=600
        t.record_trade_close(_utc(2026, 7, 11, 10), 600.0)
        # Day 2 (today, after midnight CET): +400 → today=400, cumulative=1000
        t.record_trade_close(_utc(2026, 7, 12, 10), 400.0)
        # Verify state before the check
        assert t.today_pnl == 400.0
        assert t.cumulative_pnl == 1000.0
        # Check planned=200 entry → projected_today=600, share=60% → BLOCK
        result = t.check_entry(
            planned_profit_dollars=200.0,
            now=_utc(2026, 7, 12, 14),
        )
        assert result.allowed is False
        assert "Best Day Rule" in result.reason
        assert result.projected_today_share == pytest.approx(0.60)

    def test_allows_at_exact_threshold(self):
        """Boundary: projected share == threshold → ALLOW (≤)."""
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 11, 10), 500.0)  # yesterday
        t.record_trade_close(_utc(2026, 7, 12, 10), 500.0)  # today
        # Pass `now` on the same day to avoid system-time reset
        check_now = _utc(2026, 7, 12, 14)
        # today=500, cumulative=1000, planned=0 → share=50% → ALLOW
        result = t.check_entry(planned_profit_dollars=0.0, now=check_now)
        assert result.allowed is True
        # Now bump planned to 1 → projected=501, share=50.1% → BLOCK
        result2 = t.check_entry(planned_profit_dollars=1.0, now=check_now)
        assert result2.allowed is False

    def test_non_positive_planned_profit_always_allowed(self):
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 11, 10), 1000.0)  # cumulative=1000
        # Even when today is already at 100% of cumulative, a losing entry is fine
        result_neg = t.check_entry(planned_profit_dollars=-100.0)
        assert result_neg.allowed is True
        assert "non-positive" in result_neg.reason
        result_zero = t.check_entry(planned_profit_dollars=0.0)
        assert result_zero.allowed is True

    def test_today_loss_with_huge_planned_profit_still_gated(self):
        """today=-200, cumulative=1000, planned=800 → projected=600 → 60% > 50% → BLOCK.

        Sanity check that the rule applies even when today started in the red.
        """
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 11, 10), 1200.0)  # yesterday
        t.record_trade_close(_utc(2026, 7, 12, 9), -200.0)  # today (loss)
        # today=-200, cumulative=1000, planned=800 → projected=600, share=60%
        result = t.check_entry(
            planned_profit_dollars=800.0,
            now=_utc(2026, 7, 12, 14),
        )
        assert result.allowed is False
        assert result.projected_today_share == pytest.approx(0.60)

    def test_custom_threshold(self):
        """With threshold=0.30, smaller entries should be blocked."""
        t = BestDayRuleTracker(account_phase="funded", threshold=0.30)
        t.record_trade_close(_utc(2026, 7, 11, 10), 700.0)  # yesterday
        t.record_trade_close(_utc(2026, 7, 12, 10), 200.0)  # today
        # today=200, cumulative=900, planned=100 → projected=300, share=33.3% > 30% → BLOCK
        result = t.check_entry(planned_profit_dollars=100.0, now=_utc(2026, 7, 12, 14))
        assert result.allowed is False
        assert result.threshold == 0.30

    def test_result_dataclass_fields_populated(self):
        t = BestDayRuleTracker(account_phase="funded")
        # Build cumulative > today so a small planned entry stays under threshold
        t.record_trade_close(_utc(2026, 7, 11, 10), 900.0)  # yesterday
        t.record_trade_close(_utc(2026, 7, 12, 10), 100.0)  # today
        # today=100, cumulative=1000, planned=20 → projected=120, share=12% → ALLOW
        result = t.check_entry(planned_profit_dollars=20.0, now=_utc(2026, 7, 12, 14))
        assert isinstance(result, BestDayCheckResult)
        assert result.allowed is True
        assert result.today_pnl == 100.0
        assert result.cumulative_pnl == 1000.0
        assert result.threshold == 0.50
        assert result.projected_today_share == pytest.approx(0.12)


# ── status() ─────────────────────────────────────────────────────────────────


class TestStatus:
    """AC: status() returns current state for logging / UI."""

    def test_status_in_challenge_phase(self):
        t = BestDayRuleTracker(account_phase="challenge")
        s = t.status()
        assert s["is_active"] is False
        assert s["account_phase"] == "challenge"
        assert s["threshold"] == 0.50
        assert s["today_pnl"] == 0.0
        assert s["cumulative_pnl"] == 0.0
        assert s["today_share"] == 0.0
        assert s["remaining_today_headroom_dollars"] == float("inf")

    def test_status_in_funded_with_profit(self):
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 10), 400.0)
        s = t.status(now=_utc(2026, 7, 12, 11))
        assert s["is_active"] is True
        assert s["today_pnl"] == 400.0
        assert s["cumulative_pnl"] == 400.0
        assert s["today_share"] == 1.0  # all profit today
        # headroom = 0.50 * 400 - 400 = -200 → clamped to 0
        assert s["remaining_today_headroom_dollars"] == 0.0

    def test_status_headroom_with_yesterday_profit(self):
        """today=100, cumulative=1000 → headroom = 0.50*1000 - 100 = 400."""
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 11, 10), 900.0)
        t.record_trade_close(_utc(2026, 7, 12, 10), 100.0)
        s = t.status(now=_utc(2026, 7, 12, 11))
        assert s["today_pnl"] == 100.0
        assert s["cumulative_pnl"] == 1000.0
        assert s["today_share"] == pytest.approx(0.10)
        assert s["remaining_today_headroom_dollars"] == pytest.approx(400.0)


# ── Multi-day scenarios ──────────────────────────────────────────────────────


class TestMultiDayScenarios:
    """AC: rule tracks cumulative since funding start across many days."""

    def test_cumulative_persists_across_resets(self):
        t = BestDayRuleTracker(account_phase="funded")
        for day, pnl in [(10, 100), (11, 200), (12, -50), (13, 300), (14, 50)]:
            t.record_trade_close(_utc(2026, 7, day, 10), pnl)
        # today (7/14) = 50, cumulative = 100+200-50+300+50 = 600
        assert t.today_pnl == 50.0
        assert t.cumulative_pnl == 600.0

    def test_check_entry_after_many_days(self):
        t = BestDayRuleTracker(account_phase="funded")
        # Day 1: +$400, Day 2: +$600, total cumulative = $1000
        t.record_trade_close(_utc(2026, 7, 10, 10), 400.0)
        t.record_trade_close(_utc(2026, 7, 11, 10), 600.0)
        # Day 3, 9:00 UTC (still 7/11 in CET, so today resets to 600)
        # Actually 2026-07-11 09:00 UTC = 10:00 CET = still 11th → today = 600
        # Let's use noon to be safe.
        # today=600, cumulative=1000, planned=10 → projected=610, share=61% > 50% → BLOCK
        result = t.check_entry(
            planned_profit_dollars=10.0,
            now=_utc(2026, 7, 11, 14),
        )
        assert result.allowed is False
        assert result.today_pnl == 600.0
        assert result.cumulative_pnl == 1000.0


# ── Timezone edge cases ──────────────────────────────────────────────────────


class TestTimezoneHandling:
    """AC: tracker handles naive and aware datetimes consistently."""

    def test_naive_datetime_assumed_utc(self):
        """A naive datetime should be treated as UTC."""
        t = BestDayRuleTracker(account_phase="funded")
        # 2026-07-12 22:00 UTC = 23:00 CET (same day)
        naive = datetime(2026, 7, 12, 22, 0)
        t.record_trade_close(naive, 100.0)
        # No reset expected; we record again at 23:30 UTC = 00:30 CET 13th
        naive2 = datetime(2026, 7, 12, 23, 30)
        t.record_trade_close(naive2, 50.0)
        assert t.today_pnl == 50.0
        assert t.cumulative_pnl == 150.0

    def test_aware_datetime_respected(self):
        """An aware UTC datetime is converted to local before resetting."""
        t = BestDayRuleTracker(account_phase="funded")
        t.record_trade_close(_utc(2026, 7, 12, 22), 100.0)
        t.record_trade_close(_utc(2026, 7, 12, 23, 30), 50.0)
        assert t.today_pnl == 50.0
        assert t.last_reset_date == "2026-07-13"

    def test_aware_non_utc_datetime_converted(self):
        """A datetime in a non-UTC zone is converted properly."""
        t = BestDayRuleTracker(account_phase="funded")
        # 2026-07-12 23:30 UTC+2 = 2026-07-12 21:30 UTC
        cet_plus_2 = timezone(timedelta(hours=2))
        aware_cet = datetime(2026, 7, 12, 23, 30, tzinfo=cet_plus_2)
        t.record_trade_close(aware_cet, 50.0)
        # tracker tz is +1 by default, so this is 2026-07-12 22:30 CET (still 12th)
        assert t.today_pnl == 50.0
        assert t.last_reset_date == "2026-07-12"
