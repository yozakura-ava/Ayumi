"""Tests for USDJPY trendbar price scaling fix (Phase 4).

Verifies that:
- USDJPY trendbar OHLC values are correctly scaled (~162.33, not ~16233)
- EURUSD/GBPUSD trendbars remain correctly scaled
- Magnitude sanity guard logs WARNING when JPY pair high > 1000
- Non-JPY pairs are unaffected by the divisor change
"""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure src/forex_bot is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))


@pytest.fixture(autouse=True)
def _clean_backtest_stub():
    """Remove _tsukasa_stub pollution from sys.modules before each test.

    test_forward_test_flag_persistence.py installs fake backtest.* modules
    at import (collection) time without cleanup. The polluter sets
    ``_tsukasa_stub = True`` on the ``backtest`` package but NOT on
    sub-modules created via ``_make()`` (``backtest.engine``,
    ``backtest.types``, etc.), so checking the marker alone misses them.

    When the parent ``backtest`` package is a stub we remove **all**
    ``backtest.*`` entries so Python re-imports the real modules during
    our tests.  Stubs are restored afterward so the polluter's tests
    still work.
    """
    saved = {}
    bt_pkg = sys.modules.get("backtest")
    pkg_is_stub = bt_pkg is not None and getattr(bt_pkg, "_tsukasa_stub", False)

    for key in list(sys.modules.keys()):
        if key.startswith("backtest"):
            mod = sys.modules.get(key)
            if mod is None:
                continue
            if getattr(mod, "_tsukasa_stub", False) or pkg_is_stub:
                saved[key] = sys.modules.pop(key)
    yield
    # Restore stub modules so polluter tests still work
    for key, mod in saved.items():
        if key not in sys.modules:
            sys.modules[key] = mod


@pytest.fixture
def feed_mock():
    """Create an OpenApiSpotFeed with a mocked connection, bypassing __init__."""
    from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed

    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._conn = MagicMock()
    feed._ctid_account_id = 12345
    feed._symbol_digits = {}
    feed._symbols = {}
    feed._name_to_id = {}
    feed._id_to_name = {}
    return feed


def _make_trendbar_response(trendbars):
    """Build a mock ProtoOAGetTrendbarsRes payload.

    Each trendbar is a dict with: low (int), deltaOpen, deltaHigh, deltaClose,
    volume, utcTimestampInMinutes.
    """
    mock_tbs = []
    for tb_data in trendbars:
        mock_tb = MagicMock()
        mock_tb.low = tb_data["low"]
        mock_tb.deltaOpen = tb_data.get("deltaOpen", 0)
        mock_tb.deltaHigh = tb_data.get("deltaHigh", 0)
        mock_tb.deltaClose = tb_data.get("deltaClose", 0)
        mock_tb.volume = tb_data.get("volume", 0)
        mock_tb.utcTimestampInMinutes = tb_data.get("utcTimestampInMinutes", 0)
        mock_tbs.append(mock_tb)

    response = MagicMock()
    payload = MagicMock()
    payload.trendbar = mock_tbs
    response.payloadType = 2135  # trendbar response type
    return response, payload


# ── USDJPY trendbar scaling ───────────────────────────────────────────


class TestUSDJPYTrendbarScaling:
    """Verify USDJPY trendbar OHLC values are decoded at the correct scale."""

    def test_usdjpy_trendbar_decodes_to_correct_scale(self, feed_mock):
        """USDJPY raw trendbar low=16,233,000 (5-digit encoded) should
        decode to ~162.33, not ~16233.00."""
        feed_mock._symbol_digits[4] = 3  # USDJPY digits=3
        feed_mock._id_to_name[4] = "USDJPY"
        feed_mock._name_to_id["USDJPY"] = 4

        # Raw 5-digit-encoded trendbar: low=16,233,000 → 162.33
        # deltaHigh=500 → high = (16233000+500)/100000 = 162.335
        raw_low = 16_233_000
        trendbars = [
            {
                "low": raw_low,
                "deltaHigh": 500,
                "deltaOpen": 200,
                "deltaClose": 300,
                "volume": 1000,
            }
        ]
        response, payload = _make_trendbar_response(trendbars)
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with patch("adapters.ctrader.open_api_spot_feed.Protobuf.extract", return_value=payload):
            bars = feed_mock.fetch_trendbars("USDJPY", period_minutes=60, count=1)

        assert len(bars) == 1
        bar = bars[0]
        # OHLC should be in the ~162 range, not ~16233
        assert bar.low == pytest.approx(162.33, abs=0.01), f"Expected low ~162.33, got {bar.low}"
        assert bar.high == pytest.approx(162.335, abs=0.01), f"Expected high ~162.335, got {bar.high}"
        assert bar.open == pytest.approx(162.332, abs=0.01), f"Expected open ~162.332, got {bar.open}"
        assert bar.close == pytest.approx(162.333, abs=0.01), f"Expected close ~162.333, got {bar.close}"

    def test_usdjpy_trendbar_not_100x_inflated(self, feed_mock):
        """Explicitly verify decoded USDJPY price is below 1000 (plausible
        nominal range for JPY pairs)."""
        feed_mock._symbol_digits[4] = 3
        feed_mock._id_to_name[4] = "USDJPY"
        feed_mock._name_to_id["USDJPY"] = 4

        # A typical USDJPY bar with raw values at 5-digit encoding
        trendbars = [
            {
                "low": 16_200_000,
                "deltaHigh": 500_000,
                "deltaOpen": 100_000,
                "deltaClose": 200_000,
            }
        ]
        response, payload = _make_trendbar_response(trendbars)
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with patch("adapters.ctrader.open_api_spot_feed.Protobuf.extract", return_value=payload):
            bars = feed_mock.fetch_trendbars("USDJPY", period_minutes=60, count=1)

        assert len(bars) == 1
        # All OHLC values must be < 1000 (sane JPY range)
        assert bars[0].high < 1000, f"USDJPY high={bars[0].high} exceeds 1000 — divisor bug still present"
        assert bars[0].low < 1000, f"USDJPY low={bars[0].low} exceeds 1000 — divisor bug still present"


# ── EURUSD trendbar scaling (regression) ──────────────────────────────


class TestEURUSDTrendbarScaling:
    """Verify EURUSD trendbars remain correctly scaled after the fix."""

    def test_eurusd_trendbar_unchanged(self, feed_mock):
        """EURUSD raw trendbar low=108,578 (5-digit encoded, digits=5)
        should still decode to ~1.08578 — unchanged by the fix."""
        feed_mock._symbol_digits[1] = 5  # EURUSD digits=5
        feed_mock._id_to_name[1] = "EURUSD"
        feed_mock._name_to_id["EURUSD"] = 1

        # Raw 5-digit-encoded: low=108,578 → 1.08578
        trendbars = [{"low": 108_578, "deltaHigh": 5, "deltaOpen": 2, "deltaClose": 3}]
        response, payload = _make_trendbar_response(trendbars)
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with patch("adapters.ctrader.open_api_spot_feed.Protobuf.extract", return_value=payload):
            bars = feed_mock.fetch_trendbars("EURUSD", period_minutes=60, count=1)

        assert len(bars) == 1
        bar = bars[0]
        assert bar.low == pytest.approx(1.08578, abs=0.00001), f"Expected low ~1.08578, got {bar.low}"
        assert bar.high == pytest.approx(1.08583, abs=0.00001), f"Expected high ~1.08583, got {bar.high}"
        assert bar.open == pytest.approx(1.08580, abs=0.00001), f"Expected open ~1.08580, got {bar.open}"
        assert bar.close == pytest.approx(1.08581, abs=0.00001), f"Expected close ~1.08581, got {bar.close}"

    def test_gbpusd_trendbar_unchanged(self, feed_mock):
        """GBPUSD raw trendbar low=127,345 (5-digit, digits=5) → 1.27345."""
        feed_mock._symbol_digits[2] = 5  # GBPUSD digits=5
        feed_mock._id_to_name[2] = "GBPUSD"
        feed_mock._name_to_id["GBPUSD"] = 2

        trendbars = [{"low": 127_345, "deltaHigh": 10, "deltaOpen": 5, "deltaClose": 7}]
        response, payload = _make_trendbar_response(trendbars)
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with patch("adapters.ctrader.open_api_spot_feed.Protobuf.extract", return_value=payload):
            bars = feed_mock.fetch_trendbars("GBPUSD", period_minutes=60, count=1)

        assert len(bars) == 1
        bar = bars[0]
        assert bar.low == pytest.approx(1.27345, abs=0.00001), f"Expected low ~1.27345, got {bar.low}"


# ── Magnitude sanity guard ────────────────────────────────────────────


class TestMagnitudeSanityGuard:
    """Verify WARNING is logged when JPY pair decoded high > 1000."""

    def test_jpy_pair_high_above_1000_logs_warning(self, feed_mock, caplog):
        """If a JPY pair trendbar decodes to high > 1000, a WARNING must
        be logged (indicating the divisor fix may not be working or a
        new precision mismatch has emerged)."""
        feed_mock._symbol_digits[4] = 3
        feed_mock._id_to_name[4] = "USDJPY"
        feed_mock._name_to_id["USDJPY"] = 4

        # Simulate a grossly malformed raw value that would produce high > 1000
        # even after the fix: low=200,000,000 → /10^5 = 2000.0
        trendbars = [
            {
                "low": 200_000_000,
                "deltaHigh": 50_000_000,
                "deltaOpen": 0,
                "deltaClose": 0,
            }
        ]
        response, payload = _make_trendbar_response(trendbars)
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with patch("adapters.ctrader.open_api_spot_feed.Protobuf.extract", return_value=payload):
            with caplog.at_level(logging.WARNING, logger="ayumi.openapi_spot_feed"):
                _bars = feed_mock.fetch_trendbars("USDJPY", period_minutes=60, count=1)

        # Verify warning was logged
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING and ("JPY" in r.message or "1000" in r.message)
        ]
        assert len(warning_records) >= 1, (
            f"Expected WARNING log for JPY pair high>1000, got: {[r.message for r in caplog.records]}"
        )

    def test_non_jpy_pair_no_warning(self, feed_mock, caplog):
        """Non-JPY pairs with high > 1000 (e.g. XAUUSD) should NOT trigger
        the JPY sanity guard."""
        feed_mock._symbol_digits[42] = 2  # XAUUSD digits=2
        feed_mock._id_to_name[42] = "XAUUSD"
        feed_mock._name_to_id["XAUUSD"] = 42

        # XAUUSD at 2000.00 = raw 200,000 (digits=2, divisor=100)
        trendbars = [{"low": 200_000, "deltaHigh": 5_000, "deltaOpen": 0, "deltaClose": 0}]
        response, payload = _make_trendbar_response(trendbars)
        feed_mock._conn.send_and_wait = MagicMock(return_value=response)

        with patch("adapters.ctrader.open_api_spot_feed.Protobuf.extract", return_value=payload):
            with caplog.at_level(logging.WARNING, logger="ayumi.openapi_spot_feed"):
                _bars = feed_mock.fetch_trendbars("XAUUSD", period_minutes=60, count=1)

        # No JPY-related warning should be logged
        jpy_warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "JPY" in r.message]
        assert len(jpy_warnings) == 0, (
            f"Non-JPY pair should not trigger JPY warning: {[r.message for r in jpy_warnings]}"
        )
