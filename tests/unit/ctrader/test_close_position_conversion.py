"""Unit tests for OpenApiSpotFeed.close_position() volume conversion.

Covers the [DEBT] card 02cd1df0 fix:
  - float lots → int raw volume (lots × lot_size) via VolumeCalculator
  - int raw volume → passes through unchanged (backward compat)
  - float without symbol_id → ValueError
  - bool (subclass of int) is treated as int, not float

The mocked ``send_and_wait`` captures the outgoing ``ProtoOAClosePositionReq``
so tests can assert the ``volume`` field after conversion.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

# Ensure src/forex_bot is importable (same pattern as test_amend_sl_tp.py)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))


@pytest.fixture
def feed_mock():
    """Create an OpenApiSpotFeed with a mocked connection, bypassing __init__."""
    from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed

    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._ctid_account_id = 12345
    feed._conn = MagicMock()
    feed._volume_calc = MagicMock()
    return feed


# ── Conversion: float lots → int raw volume ───────────────────────────


class TestClosePositionFloatConversion:
    """float volume (lots) is converted to int raw volume via the volume calculator."""

    def test_close_position_float_0_32_lots_sends_32_000_000_for_forex(self, feed_mock, monkeypatch):
        """0.32 lots on a forex symbol (lot_size=100_000) → volume=32_000_000."""
        # Arrange: lot_size=100_000 (forex convention)
        feed_mock._volume_calc.lots_to_volume = MagicMock(return_value=32_000_000)

        captured: list = []

        def fake_send_and_wait(req, timeout=None, prefix=None):
            captured.append(req)
            return object()  # Non-None → close_position returns True

        feed_mock._conn.send_and_wait = fake_send_and_wait

        # Patch the protobuf class so we get a MagicMock req, not a real proto.
        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        # Act
        result = feed_mock.close_position(
            position_id="pos-1",
            volume=0.32,
            symbol_id=1,  # EURUSD-like
        )

        # Assert
        assert result is True
        feed_mock._volume_calc.lots_to_volume.assert_called_once_with(1, 0.32)
        assert captured[0].volume == 32_000_000
        assert captured[0].ctidTraderAccountId == 12345
        assert captured[0].positionId == "pos-1"

    def test_close_position_float_crypto_uses_smaller_lot_size(self, feed_mock, monkeypatch):
        """0.5 lots on a crypto symbol (lot_size=100) → volume=50."""
        # Crypto lot_size=100: 0.5 * 100 = 50
        feed_mock._volume_calc.lots_to_volume = MagicMock(return_value=50)

        captured: list = []

        def fake_send_and_wait(req, timeout=None, prefix=None):
            captured.append(req)
            return object()

        feed_mock._conn.send_and_wait = fake_send_and_wait
        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        result = feed_mock.close_position("pos-2", 0.5, symbol_id=42)

        assert result is True
        feed_mock._volume_calc.lots_to_volume.assert_called_once_with(42, 0.5)
        assert captured[0].volume == 50

    def test_close_position_float_full_lot_forex(self, feed_mock, monkeypatch):
        """1.0 lot on forex → volume=100_000."""
        feed_mock._volume_calc.lots_to_volume = MagicMock(return_value=100_000)

        captured: list = []
        feed_mock._conn.send_and_wait = lambda req, timeout=None, prefix=None: captured.append(req) or object()

        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        feed_mock.close_position("pos-3", 1.0, symbol_id=1)

        assert captured[0].volume == 100_000
        feed_mock._volume_calc.lots_to_volume.assert_called_once_with(1, 1.0)


# ── Backward compat: int raw volume passes through ────────────────────


class TestClosePositionIntPassthrough:
    """int volume (raw) is passed through unchanged — no conversion call."""

    def test_close_position_int_volume_passes_through_unchanged(self, feed_mock, monkeypatch):
        """32_000_000 (int) → req.volume = 32_000_000, lots_to_volume NOT called."""
        captured: list = []
        feed_mock._conn.send_and_wait = lambda req, timeout=None, prefix=None: captured.append(req) or object()

        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        # No symbol_id provided — int path must not require it.
        result = feed_mock.close_position("pos-1", 32_000_000)

        assert result is True
        assert captured[0].volume == 32_000_000
        # Critical: lots_to_volume must NOT be called on the int path.
        feed_mock._volume_calc.lots_to_volume.assert_not_called()

    def test_close_position_int_works_without_symbol_id(self, feed_mock, monkeypatch):
        """Backward compat: int callers do not need to pass symbol_id."""
        captured: list = []
        feed_mock._conn.send_and_wait = lambda req, timeout=None, prefix=None: captured.append(req) or object()

        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        # No symbol_id — should still work for int
        result = feed_mock.close_position("pos-x", 1_000_000)

        assert result is True
        assert captured[0].volume == 1_000_000

    def test_close_position_int_full_lot(self, feed_mock, monkeypatch):
        """100_000 (int, 1 standard forex lot) → req.volume = 100_000."""
        captured: list = []
        feed_mock._conn.send_and_wait = lambda req, timeout=None, prefix=None: captured.append(req) or object()

        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        feed_mock.close_position("pos-full", 100_000)

        assert captured[0].volume == 100_000
        feed_mock._volume_calc.lots_to_volume.assert_not_called()


# ── Error handling ────────────────────────────────────────────────────


class TestClosePositionErrors:
    """Error paths: missing symbol_id, broker rejection."""

    def test_close_position_float_without_symbol_id_raises(self, feed_mock):
        """float volume without symbol_id → ValueError."""
        with pytest.raises(ValueError, match="symbol_id is required"):
            feed_mock.close_position("pos-1", 0.32)

        # No network call should have been made
        feed_mock._conn.send_and_wait.assert_not_called()

    def test_close_position_returns_false_on_broker_timeout(self, feed_mock, monkeypatch):
        """When send_and_wait returns None (timeout), close_position returns False."""
        feed_mock._conn.send_and_wait = MagicMock(return_value=None)
        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        result = feed_mock.close_position("pos-1", 32_000_000)

        assert result is False


# ── bool is a subclass of int: ensure it is treated as int, not float ──


class TestClosePositionBoolSubclass:
    """Python quirk: bool is a subclass of int, so isinstance(True, int) is True.

    The implementation checks ``isinstance(volume, float)`` BEFORE checking
    int, so bools (which are also ints) take the int passthrough path.
    This is correct — callers should not be passing bools as volumes,
    and the int path is the safe one.
    """

    def test_close_position_bool_takes_int_passthrough_path(self, feed_mock, monkeypatch):
        """True/False (bool, subclass of int) should NOT trigger lots_to_volume."""
        captured: list = []
        feed_mock._conn.send_and_wait = lambda req, timeout=None, prefix=None: captured.append(req) or object()
        from adapters.ctrader import open_api_spot_feed as spot_feed_mod

        monkeypatch.setattr(spot_feed_mod, "ProtoOAClosePositionReq", MagicMock)

        feed_mock.close_position("pos-bool", True)

        # True coerces to int 1 — passes through
        assert captured[0].volume == 1
        feed_mock._volume_calc.lots_to_volume.assert_not_called()
