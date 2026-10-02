"""Unit tests for the news calendar blackout filter.

Tests cover:
- Calendar parsing (ForexFactory-style JSON)
- High-impact event classification
- Blackout window detection (before, during, after events)
- Multi-currency symbol filtering
- Boundary conditions (exactly at blackout edge)
- Caching behavior (stale cache, auto-fetch disabled)
- Permissive vs fail-closed mode
- next_blackout_window() ordering
- Edge cases (non-forex symbols, missing time, empty calendar)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure the forex_bot source is importable
FOREX_BOT_SRC = Path(__file__).resolve().parents[3] / "src" / "forex_bot"
if str(FOREX_BOT_SRC) not in sys.path:
    sys.path.insert(0, str(FOREX_BOT_SRC))

from data.news_calendar import (  # noqa: I001
    CalendarEvent,
    NewsCalendarFilter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CALENDAR = [
    {
        "title": "Non-Farm Employment Change",
        "country": "USD",
        "date": "2024-12-06",
        "time": "13:30",
        "impact": "High",
        "forecast": "200K",
        "previous": "12K",
    },
    {
        "title": "ECB Main Refinancing Rate",
        "country": "EUR",
        "date": "2024-12-12",
        "time": "12:45",
        "impact": "High",
        "forecast": "3.15%",
        "previous": "3.40%",
    },
    {
        "title": "BoE Official Bank Rate",
        "country": "GBP",
        "date": "2024-12-19",
        "time": "12:00",
        "impact": "High",
        "forecast": "4.75%",
        "previous": "4.75%",
    },
    {
        "title": "BoJ Policy Rate",
        "country": "JPY",
        "date": "2024-12-19",
        "time": "03:00",
        "impact": "High",
        "forecast": "0.25%",
        "previous": "0.25%",
    },
    {
        "title": "Core CPI m/m",
        "country": "USD",
        "date": "2024-12-11",
        "time": "13:30",
        "impact": "High",
        "forecast": "0.3%",
        "previous": "0.3%",
    },
    {
        "title": "German Flash Manufacturing PMI",
        "country": "EUR",
        "date": "2024-12-16",
        "time": "08:30",
        "impact": "Medium",
        "forecast": "43.5",
        "previous": "43.0",
    },
    {
        "title": "ADP Non-Farm Employment Change",
        "country": "USD",
        "date": "2024-12-04",
        "time": "13:15",
        "impact": "Medium",
        "forecast": "110K",
        "previous": "184K",
    },
    {
        "title": "Bank Holiday",
        "country": "USD",
        "date": "2024-12-25",
        "time": "",
        "impact": "Holiday",
    },
]


@pytest.fixture
def tmp_cal_file(tmp_path: Path) -> Path:
    """Write sample calendar to a temp JSON file."""
    p = tmp_path / "calendar.json"
    p.write_text(json.dumps(SAMPLE_CALENDAR), encoding="utf-8")
    return p


@pytest.fixture
def filter_with_cache(tmp_cal_file: Path) -> NewsCalendarFilter:
    """Filter pointed at the temp calendar cache, auto_fetch disabled."""
    return NewsCalendarFilter(
        cache_path=tmp_cal_file,
        auto_fetch=False,
        blackout_minutes=5,
    )


# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------


class TestCalendarParsing:
    def test_parses_valid_calendar(self, tmp_cal_file: Path):
        nf = NewsCalendarFilter(cache_path=tmp_cal_file, auto_fetch=False)
        nf._try_load_cache()
        assert len(nf._events) == 8  # 8 entries in SAMPLE_CALENDAR

    def test_skips_malformed_entries(self, tmp_path: Path):
        bad_data = [
            {
                "title": "Good",
                "country": "USD",
                "date": "2024-01-01",
                "time": "10:00",
                "impact": "High",
            },
            {"title": "No country", "date": "2024-01-01", "time": "10:00"},
            {"title": "No date", "country": "EUR", "time": "10:00"},
            {
                "title": "Bad date",
                "country": "GBP",
                "date": "not-a-date",
                "time": "10:00",
            },
        ]
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(bad_data))
        nf = NewsCalendarFilter(cache_path=p, auto_fetch=False)
        nf._try_load_cache()
        assert len(nf._events) == 1

    def test_handles_missing_time(self, tmp_path: Path):
        data = [
            {
                "title": "All Day Event",
                "country": "USD",
                "date": "2024-06-15",
                "time": "",
                "impact": "High",
            }
        ]
        p = tmp_path / "notime.json"
        p.write_text(json.dumps(data))
        nf = NewsCalendarFilter(cache_path=p, auto_fetch=False)
        nf._try_load_cache()
        assert len(nf._events) == 1
        assert nf._events[0].timestamp.hour == 0

    def test_events_sorted_chronologically(self, tmp_cal_file: Path):
        nf = NewsCalendarFilter(cache_path=tmp_cal_file, auto_fetch=False)
        nf._try_load_cache()
        timestamps = [e.timestamp for e in nf._events]
        assert timestamps == sorted(timestamps)

    def test_country_field_fallback_to_currency(self, tmp_path: Path):
        data = [
            {
                "title": "Test",
                "currency": "JPY",
                "date": "2024-01-01",
                "time": "10:00",
                "impact": "High",
            }
        ]
        p = tmp_path / "cur.json"
        p.write_text(json.dumps(data))
        nf = NewsCalendarFilter(cache_path=p, auto_fetch=False)
        nf._try_load_cache()
        assert len(nf._events) == 1
        assert nf._events[0].currency == "JPY"


# ---------------------------------------------------------------------------
# High-impact classification
# ---------------------------------------------------------------------------


class TestHighImpactClassification:
    def test_high_impact_field(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="USD",
            title="Some Event",
            impact="high",
        )
        assert NewsCalendarFilter._is_high_impact(event)

    def test_keyword_nfp(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="USD",
            title="Non-Farm Employment Change",
            impact="",
        )
        assert NewsCalendarFilter._is_high_impact(event)

    def test_keyword_fomc(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="USD",
            title="FOMC Statement",
            impact="",
        )
        assert NewsCalendarFilter._is_high_impact(event)

    def test_keyword_cpi(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="USD",
            title="Core CPI m/m",
            impact="",
        )
        assert NewsCalendarFilter._is_high_impact(event)

    def test_central_bank_eur(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="EUR",
            title="Minimum Bid Rate",
            impact="",
        )
        assert NewsCalendarFilter._is_high_impact(event)

    def test_medium_impact_not_blocked(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="EUR",
            title="German Flash Manufacturing PMI",
            impact="medium",
        )
        assert not NewsCalendarFilter._is_high_impact(event)

    def test_low_impact_not_blocked(self):
        event = CalendarEvent(
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            currency="USD",
            title="Unimportant Speech",
            impact="low",
        )
        assert not NewsCalendarFilter._is_high_impact(event)


# ---------------------------------------------------------------------------
# Blackout detection
# ---------------------------------------------------------------------------


class TestBlackoutDetection:
    def test_inside_blackout_window(self, filter_with_cache: NewsCalendarFilter):
        """Exactly at event time → blocked."""
        # NFP at 2024-12-06T13:30 UTC
        now = datetime(2024, 12, 6, 13, 30, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_4min_before_event_blocked(self, filter_with_cache: NewsCalendarFilter):
        """4 minutes before = within 5-min window → blocked."""
        now = datetime(2024, 12, 6, 13, 26, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_6min_before_event_allowed(self, filter_with_cache: NewsCalendarFilter):
        """6 minutes before = outside 5-min window → allowed."""
        now = datetime(2024, 12, 6, 13, 24, tzinfo=timezone.utc)
        assert not filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_4min_after_event_blocked(self, filter_with_cache: NewsCalendarFilter):
        """4 minutes after = within 5-min window → blocked."""
        now = datetime(2024, 12, 6, 13, 34, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_6min_after_event_allowed(self, filter_with_cache: NewsCalendarFilter):
        """6 minutes after = outside 5-min window → allowed."""
        now = datetime(2024, 12, 6, 13, 36, tzinfo=timezone.utc)
        assert not filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_exact_boundary_5min_before(self, filter_with_cache: NewsCalendarFilter):
        """Exactly 5 min before = on the boundary → blocked (inclusive)."""
        now = datetime(2024, 12, 6, 13, 25, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_exact_boundary_5min_after(self, filter_with_cache: NewsCalendarFilter):
        """Exactly 5 min after = on the boundary → blocked (inclusive)."""
        now = datetime(2024, 12, 6, 13, 35, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_far_from_event_allowed(self, filter_with_cache: NewsCalendarFilter):
        """Hours from any event → allowed."""
        now = datetime(2024, 12, 6, 10, 0, tzinfo=timezone.utc)
        assert not filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_jpy_event_blocks_usdjpy(self, filter_with_cache: NewsCalendarFilter):
        """BoJ event should block USDJPY (contains JPY)."""
        # BoJ at 2024-12-19T03:00
        now = datetime(2024, 12, 19, 3, 0, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["USDJPY"], now=now)

    def test_jpy_event_does_not_block_eurusd(self, filter_with_cache: NewsCalendarFilter):
        """BoJ event should NOT block EURUSD (no JPY)."""
        now = datetime(2024, 12, 19, 3, 0, tzinfo=timezone.utc)
        assert not filter_with_cache.is_blackout_now(["EURUSD"], now=now)

    def test_gbp_event_blocks_gbpusd(self, filter_with_cache: NewsCalendarFilter):
        """BoE event should block GBPUSD."""
        # BoE at 2024-12-19T12:00
        now = datetime(2024, 12, 19, 12, 0, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["GBPUSD"], now=now)

    def test_multiple_symbols_any_match(self, filter_with_cache: NewsCalendarFilter):
        """If any symbol's currency has a blackout, entry is blocked."""
        # NFP at 13:30 USD → EURUSD blocked
        now = datetime(2024, 12, 6, 13, 30, tzinfo=timezone.utc)
        assert filter_with_cache.is_blackout_now(["EURUSD", "GBPJPY"], now=now)


# ---------------------------------------------------------------------------
# Currency extraction
# ---------------------------------------------------------------------------


class TestCurrencyExtraction:
    def test_standard_pair(self):
        result = NewsCalendarFilter._extract_currencies(["EURUSD"])
        assert result == {"EUR", "USD"}

    def test_multiple_pairs(self):
        result = NewsCalendarFilter._extract_currencies(["EURUSD", "USDJPY"])
        assert result == {"EUR", "USD", "JPY"}

    def test_with_separator(self):
        result = NewsCalendarFilter._extract_currencies(["EUR/USD", "GBP_JPY"])
        assert result == {"EUR", "USD", "GBP", "JPY"}

    def test_non_forex_ignored(self):
        result = NewsCalendarFilter._extract_currencies(["XAUUSD", "BTCUSDT", "SP500"])
        # XAUUSD is 6 chars alpha → processed, but XAU not in SUPPORTED_CURRENCIES
        # BTCUSDT is 7 chars → ignored
        # SP500 has digits → ignored
        assert "BTC" not in result
        assert "USDT" not in result

    def test_lowercase_normalized(self):
        result = NewsCalendarFilter._extract_currencies(["eurusd"])
        assert result == {"EUR", "USD"}

    def test_empty_list(self):
        assert NewsCalendarFilter._extract_currencies([]) == set()


# ---------------------------------------------------------------------------
# next_blackout_window
# ---------------------------------------------------------------------------


class TestNextBlackoutWindow:
    def test_returns_next_upcoming(self, filter_with_cache: NewsCalendarFilter):
        """Before NFP → next window is NFP."""
        now = datetime(2024, 12, 6, 12, 0, tzinfo=timezone.utc)
        window = filter_with_cache.next_blackout_window(["EURUSD"], now=now)
        assert window is not None
        assert window.currency == "USD"
        assert "Non-Farm" in window.title or "CPI" in window.title

    def test_returns_none_when_no_upcoming(self, filter_with_cache: NewsCalendarFilter):
        """After all events → None."""
        now = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
        window = filter_with_cache.next_blackout_window(["EURUSD"], now=now)
        assert window is None

    def test_returns_none_for_unrelated_currency(self, filter_with_cache: NewsCalendarFilter):
        """If no tracked currency matches, returns None."""
        now = datetime(2024, 12, 6, 12, 0, tzinfo=timezone.utc)
        window = filter_with_cache.next_blackout_window(["AUDNZD"], now=now)
        # AUD and NZD not in SUPPORTED_CURRENCIES
        assert window is None

    def test_window_times_correct(self, filter_with_cache: NewsCalendarFilter):
        """Window start/end should be event ± blackout_minutes."""
        now = datetime(2024, 12, 6, 12, 0, tzinfo=timezone.utc)
        window = filter_with_cache.next_blackout_window(["EURUSD"], now=now)
        assert window is not None
        # NFP at 13:30, blackout ±5 min
        expected_start = datetime(2024, 12, 6, 13, 25, tzinfo=timezone.utc)
        expected_end = datetime(2024, 12, 6, 13, 35, tzinfo=timezone.utc)
        if "Non-Farm" in window.title:
            assert window.start == expected_start
            assert window.end == expected_end


# ---------------------------------------------------------------------------
# Caching behavior
# ---------------------------------------------------------------------------


class TestCachingBehavior:
    def test_no_cache_permissive(self):
        """No cache file + permissive → allows all."""
        nf = NewsCalendarFilter(
            cache_path=None,
            auto_fetch=False,
            permissive_on_failure=True,
        )
        assert not nf.is_blackout_now(["EURUSD"])

    def test_no_cache_fail_closed(self):
        """No cache file + fail-closed → blocks all."""
        nf = NewsCalendarFilter(
            cache_path=None,
            auto_fetch=False,
            permissive_on_failure=False,
        )
        assert nf.is_blackout_now(["EURUSD"])

    def test_auto_fetch_disabled_uses_cache(self, tmp_cal_file: Path):
        """With auto_fetch=False, stale cache is still used."""
        nf = NewsCalendarFilter(
            cache_path=tmp_cal_file,
            auto_fetch=False,
            cache_ttl_hours=0,  # Force stale
        )
        now = datetime(2024, 12, 6, 13, 30, tzinfo=timezone.utc)
        # Should still detect blackout from stale cache
        assert nf.is_blackout_now(["EURUSD"], now=now)

    def test_custom_blackout_minutes(self, tmp_path: Path):
        """10-minute window should be wider than default 5."""
        p = tmp_path / "cal.json"
        p.write_text(json.dumps(SAMPLE_CALENDAR))
        nf = NewsCalendarFilter(
            cache_path=p,
            auto_fetch=False,
            blackout_minutes=10,
        )
        # 8 min before event (13:22) — blocked with 10-min window
        now = datetime(2024, 12, 6, 13, 22, tzinfo=timezone.utc)
        assert nf.is_blackout_now(["EURUSD"], now=now)

        # 6 min before event (13:24) — not blocked with 5-min default
        nf5 = NewsCalendarFilter(cache_path=p, auto_fetch=False, blackout_minutes=5)
        assert not nf5.is_blackout_now(["EURUSD"], now=datetime(2024, 12, 6, 13, 24, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# get_active_blackouts
# ---------------------------------------------------------------------------


class TestGetActiveBlackouts:
    def test_returns_all_active(self, filter_with_cache: NewsCalendarFilter):
        """When multiple events overlap, all active windows returned."""
        # BoE and BoJ both on 2024-12-19, but different times
        # BoJ at 03:00, BoE at 12:00 — not overlapping
        now = datetime(2024, 12, 19, 12, 0, tzinfo=timezone.utc)
        active = filter_with_cache.get_active_blackouts(["GBPUSD"], now=now)
        assert len(active) == 1
        assert active[0].currency == "GBP"

    def test_empty_when_none_active(self, filter_with_cache: NewsCalendarFilter):
        now = datetime(2024, 12, 15, 0, 0, tzinfo=timezone.utc)
        active = filter_with_cache.get_active_blackouts(["EURUSD"], now=now)
        assert len(active) == 0


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_realistic_usage_pattern(self, tmp_cal_file: Path):
        """Simulate a trading loop checking blackout before each entry."""
        nf = NewsCalendarFilter(
            cache_path=tmp_cal_file,
            auto_fetch=False,
            blackout_minutes=5,
        )

        # Mid-day, no events nearby
        now = datetime(2024, 12, 9, 10, 0, tzinfo=timezone.utc)
        assert not nf.is_blackout_now(["EURUSD", "USDJPY"], now=now)

        # CPI on Dec 11 at 13:30 — 2 min before = blocked
        before_cpi = datetime(2024, 12, 11, 13, 28, tzinfo=timezone.utc)
        assert nf.is_blackout_now(["EURUSD"], now=before_cpi)

        # After CPI window
        after_cpi = datetime(2024, 12, 11, 13, 40, tzinfo=timezone.utc)
        assert not nf.is_blackout_now(["EURUSD"], now=after_cpi)

    def test_custom_blackout_minutes_wider_window(self, tmp_path: Path):
        """Verify that wider blackout catches more events."""
        p = tmp_path / "cal.json"
        p.write_text(json.dumps(SAMPLE_CALENDAR))
        nf = NewsCalendarFilter(
            cache_path=p,
            auto_fetch=False,
            blackout_minutes=30,
        )
        # 25 min before NFP → blocked with 30-min window
        now = datetime(2024, 12, 6, 13, 5, tzinfo=timezone.utc)
        assert nf.is_blackout_now(["EURUSD"], now=now)

    def test_empty_calendar_list(self, tmp_path: Path):
        """Empty calendar → permissive mode."""
        p = tmp_path / "empty.json"
        p.write_text("[]")
        nf = NewsCalendarFilter(cache_path=p, auto_fetch=False)
        assert not nf.is_blackout_now(["EURUSD"])

    def test_invalid_json(self, tmp_path: Path):
        """Invalid JSON → cache load fails, permissive mode."""
        p = tmp_path / "bad.json"
        p.write_text("not json at all")
        nf = NewsCalendarFilter(
            cache_path=p,
            auto_fetch=False,
            permissive_on_failure=True,
        )
        assert not nf.is_blackout_now(["EURUSD"])

    def test_non_dict_list_calendar(self, tmp_path: Path):
        """Calendar that's a dict instead of list → empty events."""
        p = tmp_path / "dict.json"
        p.write_text('{"key": "value"}')
        nf = NewsCalendarFilter(cache_path=p, auto_fetch=False)
        nf._try_load_cache()
        assert len(nf._events) == 0
