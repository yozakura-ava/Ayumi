"""Unit tests: Kill switch scope — query errors should not activate kill switch.

Tests AC1, AC2, AC3 from card c9876cce:
- AC1: Kill switch only activates on actual order submission failures,
      not account query calls
- AC2: "Trading account is not authorized" on trader_query_* calls logs
      warning but does NOT trigger kill switch
- AC3: Test added: query error → no kill switch activation;
      order error → kill switch activation
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure src/forex_bot is importable
_SRC = str(Path(__file__).resolve().parents[3] / "src" / "forex_bot")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed


def _make_minimal_feed():
    """Create an OpenApiSpotFeed with mocked internals for error handling tests."""
    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._kill_switch = MagicMock()
    feed._kill_switch.is_active = False
    feed._authed = MagicMock()
    feed._authed.is_set.return_value = True
    feed._auth_circuit_open = False
    feed._refresh_in_progress = False
    feed._auth_error_count = 0
    feed._last_reactive_refresh_time = 0.0
    feed._token_lifecycle = None
    feed._refresh_token_and_reauth = MagicMock()
    feed._activate_kill_switch_freeze = MagicMock()
    return feed


def _make_error_message(error_code, description):
    """Create a mock protobuf error message."""
    msg = MagicMock()
    msg.errorCode = error_code
    msg.description = description
    return msg


def _make_envelope(client_msg_id):
    """Create a mock message envelope with clientMsgId."""
    env = MagicMock()
    env.clientMsgId = client_msg_id
    return env


class TestKillSwitchQueryVsOrder:
    """Verify kill switch scope is narroweed to order operations only."""

    def test_query_error_no_kill_switch(self):
        """AC1/AC2/AC3: trader_query_* error with 'not authorized' → no kill switch."""
        feed = _make_minimal_feed()

        msg = _make_error_message(
            "INVALID_REQUEST",
            "Trading account is not authorized",
        )
        envelope = _make_envelope("trader_query_account_balance_req_12345")

        feed._handle_error(msg, envelope)

        feed._activate_kill_switch_freeze.assert_not_called()

    def test_order_error_activates_kill_switch(self):
        """AC3: Non-query error with 'not authorized' → kill switch activates."""
        feed = _make_minimal_feed()

        msg = _make_error_message(
            "INVALID_REQUEST",
            "Trading account is not authorized",
        )
        # No envelope or non-query clientMsgId
        envelope = _make_envelope("order_abc123")

        feed._handle_error(msg, envelope)

        feed._activate_kill_switch_freeze.assert_called()

    def test_query_error_with_no_envelope_still_cautious(self):
        """When no envelope is available (can't determine call type),
        default to activating kill switch for 'not authorized' errors."""
        feed = _make_minimal_feed()

        msg = _make_error_message(
            "INVALID_REQUEST",
            "Trading account is not authorized",
        )

        feed._handle_error(msg, envelope=None)

        feed._activate_kill_switch_freeze.assert_called()

    def test_query_prefixes_all_excluded(self):
        """All known query prefixes should be excluded from kill switch."""
        feed = _make_minimal_feed()

        query_prefixes = [
            "trader_query_balance_1",
            "protoOaTrades_req_2",
            "protoOaAccount_req_3",
            "protoOaReconcile_req_4",
            "protoOaSymbolBy_req_5",
            "protoOaTrader_req_6",
        ]

        for prefix in query_prefixes:
            feed._activate_kill_switch_freeze.reset_mock()
            msg = _make_error_message("INVALID_REQUEST", "Trading account is not authorized")
            envelope = _make_envelope(prefix)

            feed._handle_error(msg, envelope)

            assert not feed._activate_kill_switch_freeze.called, (
                f"Kill switch should NOT activate for query call with clientMsgId prefix '{prefix}'"
            )

    def test_non_not_authorized_error_not_affected(self):
        """Errors without 'not authorized' should behave as before (no reclassification)."""
        feed = _make_minimal_feed()

        # Use a transient code that normally doesn't trigger kill switch.
        # The description doesn't contain 'not authorized', so the
        # reclassification path should not be reached at all.
        msg = _make_error_message("CH_OAUTH_TOKEN_EXPIRED", "Token has expired")
        envelope = _make_envelope("trader_query_balance_1")

        feed._handle_error(msg, envelope)

        feed._activate_kill_switch_freeze.assert_not_called()
