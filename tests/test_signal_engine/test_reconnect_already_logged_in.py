"""Regression tests for ALREADY_LOGGED_IN handling during reconnect (card ecbecd01).

Root cause: cTrader enforces a single-session rule. During a reconnect cycle,
``_on_conn_disconnected`` clears ``_app_authed`` *before* the new auth attempt
fires. When the new ``ProtoOAApplicationAuthReq`` lands while the server still
considers the previous session active, cTrader replies with
``ALREADY_LOGGED_IN`` (payloadType=2142, errorCode="ALREADY_LOGGED_IN").

The previous implementation only treated ``ALREADY_LOGGED_IN`` as expected
when ``self._app_authed.is_set()`` — which is False in the reconnect window.
That made the response look like an auth failure, bumped
``_auth_error_count`` on every retry, and after 5 strikes tripped the
auth circuit breaker → ``ConnectionState.FAILED`` → kill switch →
engine stop → systemd restart loop.

Fix: ``_is_expected_auth_response`` now accepts ``ALREADY_LOGGED_IN`` when
either ``_app_authed`` is set (cold re-auth) or ``_reauth_in_progress`` is
set (reconnect-driven re-auth).

These tests guard against:
- Test 1 — the bug regressing: reconnect-time ``ALREADY_LOGGED_IN`` MUST
  be treated as a successful auth response.
- Test 2 — over-correction: a cold-start ``ALREADY_LOGGED_IN`` (no prior
  auth, no reconnect) MUST still be rejected so we don't silently accept
  misconfigured credentials.
- Test 3 — circuit-breaker cascade: a reconnect that hits
  ``ALREADY_LOGGED_IN`` MUST NOT bump ``_auth_error_count``, which is
  the root mechanism that drove the restart loop in production.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure src/forex_bot is importable (same pattern as the other tests in
# tests/test_signal_engine/ — see test_paper_sltp_integration.py).
_FOREX_SRC = str(Path(__file__).resolve().parent.parent.parent / "src" / "forex_bot")
if _FOREX_SRC not in sys.path:
    sys.path.insert(0, _FOREX_SRC)

from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed  # noqa: E402

# Payload-type constants from open_api_spot_feed.py (re-declared here to
# keep the test self-contained — these are stable wire-protocol values).
_APP_AUTH_RES_PAYLOAD_TYPE = 2101
_ACCT_AUTH_RES_PAYLOAD_TYPE = 2103
_ERROR_RES_PAYLOAD_TYPE = 2142


def _make_minimal_feed() -> OpenApiSpotFeed:
    """Build an OpenApiSpotFeed bypassing __init__.

    The real constructor opens a TCP connection, which we don't want in a
    unit test. We replicate only the attributes that
    ``_is_expected_auth_response`` and ``_handle_auth_failure`` touch.
    """
    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)
    feed._app_authed = threading.Event()
    feed._authed = threading.Event()
    feed._reauth_in_progress = threading.Event()
    feed._auth_error_count = 0
    feed._auth_circuit_open = False
    # State manager stub — _is_expected_auth_response doesn't call into it,
    # but _check_circuit_breaker does, so give it a transition_to no-op.
    feed._state_mgr = MagicMock()
    return feed


def _make_error_response(error_code: str) -> MagicMock:
    """Build a fake cTrader error response carrying ``error_code``."""
    response = MagicMock()
    response.payloadType = _ERROR_RES_PAYLOAD_TYPE
    # Protobuf.extract(response) is called on this object; the real impl
    # walks the protobuf descriptor, but our MagicMock returns a sibling
    # MagicMock from .errorCode by default — so set the attribute the code
    # reads: ``getattr(payload, "errorCode", "UNKNOWN")``.
    extracted = MagicMock()
    extracted.errorCode = error_code
    # Protobuf.extract is imported into open_api_spot_feed's namespace;
    # we patch it at the *use site* so the MagicMock returns our payload.
    response._extracted_payload = extracted
    return response


class TestReconnectAlreadyLoggedIn:
    """Regression tests for the ecbecd01 fix."""

    def test_already_logged_in_accepted_when_reauth_in_progress(self):
        """Test 1: ``ALREADY_LOGGED_IN`` is accepted during reconnect.

        Reconnect path: ``_on_conn_disconnected`` cleared ``_app_authed``,
        but ``_reauth_in_progress`` is set (see ``_on_conn_connected`` →
        ``_reconnect_restore``). The server returns ALREADY_LOGGED_IN
        because the previous session is still considered active. We must
        treat this as expected — the local app-auth handshake succeeded
        from cTrader's perspective; the server is just telling us a
        sibling session still owns the slot.
        """
        feed = _make_minimal_feed()

        # Reconnect-time state: app-auth flag cleared, reauth in flight.
        assert not feed._app_authed.is_set()
        feed._reauth_in_progress.set()

        response = _make_error_response("ALREADY_LOGGED_IN")

        # Patch Protobuf.extract at the module use site so the test
        # returns our fake payload.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                lambda _resp: response._extracted_payload,
            )
            accepted = feed._is_expected_auth_response(
                response,
                _APP_AUTH_RES_PAYLOAD_TYPE,
                "reconnect_app",
            )

        assert accepted is True, (
            "ALREADY_LOGGED_IN must be accepted when _reauth_in_progress "
            "is set — this is the fix that breaks the restart loop "
            "(card ecbecd01)."
        )

    def test_already_logged_in_rejected_when_no_reauth_in_progress(self):
        """Test 2: ``ALREADY_LOGGED_IN`` is rejected on cold start.

        If we never had a prior session (``_app_authed`` unset) and we're
        not in a reconnect (``_reauth_in_progress`` unset), then
        ``ALREADY_LOGGED_IN`` means our credentials are being used by
        another process / app and we genuinely cannot authenticate. This
        is a real failure — accept it would silently mask the problem.
        """
        feed = _make_minimal_feed()

        # Cold-start state: nothing set.
        assert not feed._app_authed.is_set()
        assert not feed._reauth_in_progress.is_set()

        response = _make_error_response("ALREADY_LOGGED_IN")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                lambda _resp: response._extracted_payload,
            )
            accepted = feed._is_expected_auth_response(
                response,
                _APP_AUTH_RES_PAYLOAD_TYPE,
                "app",
            )

        assert accepted is False, (
            "ALREADY_LOGGED_IN on cold start (no _app_authed, no "
            "_reauth_in_progress) must still be rejected — otherwise we "
            "silently accept a session collision as success."
        )

    def test_reconnect_already_logged_in_does_not_increment_auth_error_count(self):
        """Test 3: reconnect-time ``ALREADY_LOGGED_IN`` does NOT cascade.

        End-to-end simulation of the reconnect path: ``_reauth_in_progress``
        is set, the server returns ``ALREADY_LOGGED_IN``, and we verify the
        caller would NOT call ``_handle_auth_failure`` (because
        ``_is_expected_auth_response`` returned True). This is the exact
        cascade that produced the production restart loop: every reconnect
        bumped ``_auth_error_count``, and at 5 the circuit breaker tripped
        → FAILED state → kill switch → systemd restart → repeat.
        """
        feed = _make_minimal_feed()
        feed._reauth_in_progress.set()
        initial_count = feed._auth_error_count

        response = _make_error_response("ALREADY_LOGGED_IN")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                lambda _resp: response._extracted_payload,
            )
            # Simulate what _reconnect_restore does: call
            # _is_expected_auth_response and, on True, mark app authed
            # WITHOUT calling _handle_auth_failure.
            accepted = feed._is_expected_auth_response(
                response,
                _APP_AUTH_RES_PAYLOAD_TYPE,
                "reconnect_app",
            )
            if accepted:
                feed._app_authed.set()
            else:
                # Mirror the real _reconnect_restore failure path.
                feed._handle_auth_failure("reconnect_app")

        # Mirror the reconnect_acct handshake to prove a full reconnect
        # cycle with ALREADY_LOGGED_IN on both app and account stages
        # does not bump the counter.
        acct_response = _make_error_response("ALREADY_LOGGED_IN")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "adapters.ctrader.open_api_spot_feed.Protobuf.extract",
                lambda _resp: acct_response._extracted_payload,
            )
            accepted_acct = feed._is_expected_auth_response(
                acct_response,
                _ACCT_AUTH_RES_PAYLOAD_TYPE,
                "reconnect_acct",
            )
            if accepted_acct:
                feed._authed.set()
            else:
                feed._handle_auth_failure("reconnect_acct")

        assert accepted is True
        assert accepted_acct is True
        assert feed._auth_error_count == initial_count, (
            f"auth_error_count bumped from {initial_count} to "
            f"{feed._auth_error_count} after ALREADY_LOGGED_IN during "
            f"reconnect — this is the cascade that trips the circuit "
            f"breaker at 5 and triggers the systemd restart loop "
            f"(card ecbecd01)."
        )
        # Sanity: circuit breaker must NOT be open.
        assert feed._auth_circuit_open is False
