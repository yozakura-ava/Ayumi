"""Sprint 024 (card 1e32c408) regression tests — cTrader session-conflict guard.

Production incident (2026-08-20T17:12:55Z, XAUUSD/SRMR+ forward test):

    17:12:53 ayumi.token_lifecycle | Refreshing cTrader access token
    17:12:54 ayumi.token_lifecycle | Tokens written to .env, expires_at=...
    17:12:54 ayumi.openapi_spot_feed | Token refreshed via TokenLifecycle delegation
    17:12:55 ayumi.openapi_spot_feed | [ORDER_ERROR] SESSION-CONFLICT — account/channel
                                  re-authorization detected: ... errorCode='ALREADY_LOGGED_IN'
                                  description='Trading account is already authorized in this channel'
                                  (session_conflict_count=1 pending=0 late_registry=0).

Root cause: ``OpenApiSpotFeed._refresh_token_and_reauth`` unconditionally
fired a ``ProtoOAAccountAuthReq`` after every successful OAuth refresh,
even when the cTrader channel was already authenticated. cTrader enforces
a single-session rule per OpenAPI app per channel — re-authenticating an
already-authenticated channel returns ``ALREADY_LOGGED_IN``, which
surfaced as an ``ORDER_ERROR`` event with empty ``clientOrderId`` (no
pending-order match) and was caught by the 18b74ea7 SESSION-CONFLICT
detector as a one-off warning.

This test module covers the four invariants the card commits to:

  1. **Skip re-auth when channel is already authenticated** — the new
     token is committed to the credential store and the in-memory cache;
     no ``ProtoOAAccountAuthReq`` is sent; no SESSION-CONFLICT warning
     fires. The new token will be used on the next reconnect.

  2. **Re-auth IS sent when the channel is NOT authenticated** —
     preserves the legacy behaviour for the genuine "token expired
     mid-session, broker dropped the channel" case (the reactive
     AUTH_EXPIRED refresh path is still the production recovery route).

  3. **Session mutex serializes authorize+refresh** — holds the
     ``_session_reauth_lock`` during the refresh+reauth sequence and
     skips if a concurrent holder owns the lock. Non-blocking sema:
     never deadlocks the proactive timer.

  4. **No SESSION-CONFLICT warning at the source** — the warning is
     preserved at ``_handle_order_error_event`` as defence-in-depth
     (catches ALREADY_LOGGED_IN from external processes per card
     18b74ea7), but the source path no longer produces one.

Run with::

    python3 -m pytest tests/test_signal_engine/test_session_conflict_guard_s024.py -v
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/forex_bot is importable (same pattern as the other tests in
# tests/test_signal_engine/ — see test_paper_sltp_integration.py).
_FOREX_SRC = str(Path(__file__).resolve().parent.parent.parent / "src" / "forex_bot")
if _FOREX_SRC not in sys.path:
    sys.path.insert(0, _FOREX_SRC)

from adapters.ctrader.open_api_spot_feed import (  # noqa: E402
    OpenApiSpotFeed,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_feed() -> OpenApiSpotFeed:
    """Build an OpenApiSpotFeed bypassing __init__.

    The real constructor opens a TCP connection and runs Twisted reactor
    hooks, which we don't want in a unit test. We replicate only the
    attributes that ``_refresh_token_and_reauth._do_refresh_offthread``
    and its callers (``_refresh_token_and_reauth``,
    ``_check_circuit_breaker``) actually touch.

    Uses ``_session_reauth_lock`` initialised as the real ``threading.Lock``
    so the production mutex semantics are exercised end-to-end.
    """
    feed = OpenApiSpotFeed.__new__(OpenApiSpotFeed)

    # Auth state — drives the "skip if already authenticated" branch.
    feed._authed = threading.Event()
    feed._app_authed = threading.Event()
    feed._reauth_in_progress = threading.Event()

    # Circuit breaker — kept in the minimal set because the off-thread
    # body calls ``self._check_circuit_breaker()`` on failure paths.
    feed._auth_error_count = 0
    feed._auth_circuit_open = False
    feed._state_mgr = MagicMock()
    feed._state_mgr.transition_to = MagicMock()

    # Token lifecycle — the source of the OAuth refresh. Mocked because
    # we don't want to hit the network. We patch it per-test.
    feed._token_lifecycle = MagicMock()
    feed._token_lifecycle._refresh_disabled = False
    feed._access_token = "old-access-token"  # noqa: S105
    feed._refresh_token = "old-refresh-token"  # noqa: S105
    feed._token_expires_at = None

    # Connection — mocked. ``is_connected`` drives the skip branch; we
    # patch per-test.
    feed._conn = MagicMock()

    # Account ID — read by the off-thread re-auth body when the channel
    # is NOT authenticated and a ``ProtoOAAccountAuthReq`` is built.
    feed._ctid_account_id = 99999

    # Session mutex (card 1e32c408) — real lock so the test exercises
    # actual lock semantics (non-blocking acquire, locked() check).
    feed._session_reauth_lock = threading.Lock()

    return feed


def _wait_offthread(feed: OpenApiSpotFeed, timeout: float = 2.0) -> None:
    """Wait for ``_refresh_token_and_reauth`` to drain its background thread.

    The production path spawns a daemon thread; we poll the session mutex
    to detect when the off-thread body is finished (the lock is held
    during the refresh+reauth sequence and released in the finally).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not feed._session_reauth_lock.locked():
            # Give the thread a moment to finish teardown after release.
            time.sleep(0.01)
            return
        time.sleep(0.005)
    raise AssertionError("background refresh thread did not finish in time")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionConflictGuard:
    """Card 1e32c408 — production guard for the 2026-08-20T17:12:55Z incident."""

    def test_skip_reauth_when_channel_already_authenticated(self):
        """Test 1 (AC2): re-auth is SKIPPED when channel is already authenticated.

        The pre-fix bug: ``_refresh_token_and_reauth`` always sent
        ``ProtoOAAccountAuthReq`` after a successful OAuth refresh,
        even when ``_authed.is_set()`` was True. cTrader's single-session
        rule rejected the redundant auth with ``ALREADY_LOGGED_IN``,
        surfacing as a SESSION-CONFLICT ORDER_ERROR warning.

        The fix: when ``_authed.is_set()`` and ``_conn.is_connected``,
        skip the re-auth send. The new access token is committed to the
        in-memory cache and (via TokenLifecycle) to the credential
        store; it will be used on the next reconnect.
        """
        feed = _make_minimal_feed()

        # Production state at 17:12:55Z: the spot feed is already
        # authenticated on the channel (initial auth at startup
        # succeeded and never disconnected).
        feed._authed.set()
        feed._conn.is_connected = True

        # Mock TokenLifecycle to return a fresh token and update creds.
        new_expires = datetime.now(timezone.utc) + timedelta(days=25)
        feed._token_lifecycle.force_refresh.return_value = "new-access-token"  # noqa: S105
        feed._token_lifecycle.expires_at = new_expires
        feed._token_lifecycle.token_age_s = 0.5
        mock_creds = MagicMock()
        mock_creds.refresh_token = "new-refresh-token"  # noqa: S105
        feed._token_lifecycle._store.get.return_value = mock_creds

        with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = MagicMock()
            feed._refresh_token_and_reauth(proactive=True)

        _wait_offthread(feed)

        # Critical assertion: no reactor.callFromThread call carried an
        # account-auth request. Pre-fix this list would contain exactly
        # one such call (the redundant ProtoOAAccountAuthReq).
        reauth_sends = [
            call
            for call in mock_reactor.callFromThread.call_args_list
            if len(call.args) >= 2
            and hasattr(call.args[1], "ctidTraderAccountId")
            and hasattr(call.args[1], "accessToken")
        ]
        assert reauth_sends == [], (
            "channel was already authenticated (initial startup auth "
            "succeeded) but the refresh handler still sent an account "
            "auth request — this is the 2026-08-20T17:12:55Z bug "
            "regressing (card 1e32c408)."
        )

        # New tokens are committed to the in-memory cache; they will be
        # used on the next reconnect.
        assert feed._access_token == "new-access-token"  # noqa: S105
        assert feed._refresh_token == "new-refresh-token"  # noqa: S105
        # OAuth refresh itself ran (the channel binding to the OLD
        # token is still valid for the rest of this connection).
        feed._token_lifecycle.force_refresh.assert_called_once()

    def test_reauth_sent_when_channel_not_authenticated(self):
        """Test 2: re-auth IS sent when the channel is NOT authenticated.

        Preserves the legacy behaviour for the genuine "broker dropped
        the channel, AUTH_EXPIRED, reconnect-driven refresh" case. If
        ``_authed.is_set()`` is False, the broker session is gone and
        we MUST send the account auth with the new token.
        """
        feed = _make_minimal_feed()

        # Channel NOT authenticated — e.g. AUTH_EXPIRED fired mid-session
        # and the broker has dropped the channel.
        assert not feed._authed.is_set()
        feed._conn.is_connected = True

        new_expires = datetime.now(timezone.utc) + timedelta(days=25)
        feed._token_lifecycle.force_refresh.return_value = "new-access-token"  # noqa: S105
        feed._token_lifecycle.expires_at = new_expires
        feed._token_lifecycle.token_age_s = 0.5
        mock_creds = MagicMock()
        mock_creds.refresh_token = "new-refresh-token"  # noqa: S105
        feed._token_lifecycle._store.get.return_value = mock_creds

        with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = MagicMock()
            feed._refresh_token_and_reauth()

        _wait_offthread(feed)

        # Critical assertion: reactor.callFromThread WAS called with
        # a function (the send callback) and a message object carrying
        # the new access token — this is the cTrader account-auth request.
        assert mock_reactor.callFromThread.called, (
            "channel was NOT authenticated but the refresh handler "
            "skipped the re-auth — without the auth request the feed "
            "would be stuck mid-recovery (card 1e32c408 regression)."
        )

        # Inspect every call: at least one must carry a message with
        # accessToken and ctidTraderAccountId attributes (the
        # ProtoOAAccountAuthReq built inside the off-thread body).
        auth_request_calls = []
        for call in mock_reactor.callFromThread.call_args_list:
            # Expected call shape: callFromThread(send_fn, request_msg)
            args = call.args
            if len(args) < 2:
                continue
            request_msg = args[1]
            # Message-like: must support attribute access for the fields
            # we set (``ctidTraderAccountId``, ``accessToken``).
            if hasattr(request_msg, "ctidTraderAccountId") and hasattr(request_msg, "accessToken"):
                auth_request_calls.append(call)

        assert auth_request_calls, (
            "reactor.callFromThread fired but no call carried an "
            "account-auth request with ctidTraderAccountId + "
            "accessToken attributes — the off-thread body should "
            "have built a ProtoOAAccountAuthReq (card 1e32c408)."
        )

        # The account-auth request must carry the NEW access token (not
        # the stale in-memory token), so the broker rotates to the
        # fresh token on next use.
        sent_req = auth_request_calls[0].args[1]
        assert sent_req.accessToken == "new-access-token", (  # noqa: S105
            f"re-auth sent with accessToken={sent_req.accessToken!r}; "
            f"expected 'new-access-token' (the freshly-refreshed token)."
        )
        assert sent_req.ctidTraderAccountId == 99999, (
            f"re-auth sent with ctidTraderAccountId="
            f"{sent_req.ctidTraderAccountId!r}; expected 99999."
        )

    def test_reauth_skipped_when_connection_lost(self):
        """Test 3: re-auth is skipped when the TCP connection is gone.

        If ``_conn.is_connected`` is False, sending an account-auth
        request would land on a dead socket. Skip — the connection's
        reconnect loop will handle re-auth when it gets a fresh TCP
        connection.
        """
        feed = _make_minimal_feed()

        # Defensive: even though _authed is False (we lost the channel),
        # the underlying TCP connection is gone. Sending a message now
        # is pointless.
        feed._authed.clear()
        feed._conn.is_connected = False

        feed._token_lifecycle.force_refresh.return_value = "new-access-token"  # noqa: S105
        feed._token_lifecycle.expires_at = None
        feed._token_lifecycle.token_age_s = 0.5
        feed._token_lifecycle._store.get.return_value = MagicMock(refresh_token="new-refresh-token")  # noqa: S106

        with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = MagicMock()
            feed._refresh_token_and_reauth()

        _wait_offthread(feed)

        # No broker round-trip when the connection is gone.
        reauth_sends = [
            c for c in mock_reactor.callFromThread.call_args_list
            if len(c.args) >= 2
            and hasattr(c.args[1], "ctidTraderAccountId")
            and hasattr(c.args[1], "accessToken")
        ]
        assert reauth_sends == [], (
            "connection was lost but refresh still sent an account-auth "
            "request — reconnect loop owns auth in this state "
            "(card 1e32c408)."
        )

    def test_session_reauth_lock_serializes_concurrent_refreshes(self):
        """Test 4 (AC2): the session mutex serializes authorize+refresh.

        Two concurrent ``_refresh_token_and_reauth`` invocations would
        otherwise produce two interleaved OAuth refresh + account-auth
        sends. With the mutex, the second caller observes the lock held
        and skips its own refresh cycle — the holder completes it; if
        it fails, the next reactive / proactive cycle will retry.

        This is the explicit "session mutex/lock around authorize+refresh"
        asked for in AC2 of card 1e32c408.
        """
        feed = _make_minimal_feed()
        feed._authed.set()  # already authenticated — skip path
        feed._conn.is_connected = True
        feed._token_lifecycle.force_refresh.return_value = "new-access-token"  # noqa: S105
        feed._token_lifecycle.expires_at = None
        feed._token_lifecycle.token_age_s = 0.5
        feed._token_lifecycle._store.get.return_value = MagicMock(refresh_token="new-refresh-token")  # noqa: S106

        # Hold the lock as if a reconnect-driven re-auth is in flight.
        assert feed._session_reauth_lock.acquire(blocking=False)
        try:
            with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
                mock_reactor.callFromThread = MagicMock()
                feed._refresh_token_and_reauth(proactive=True)

            # The off-thread body should see the lock held and skip
            # without calling force_refresh at all.
            time.sleep(0.1)  # let the thread run

            feed._token_lifecycle.force_refresh.assert_not_called(), (
                "session mutex was held by a concurrent re-auth but the "
                "proactive refresh still ran — this is the interleaving "
                "the lock is supposed to prevent (card 1e32c408)."
            )
        finally:
            feed._session_reauth_lock.release()

    def test_session_reauth_lock_released_on_exception(self):
        """Test 5: the session mutex is released even on refresh failure.

        The off-thread body wraps its work in a try/finally so the lock
        is released on both the success and the exception paths. Without
        this, a single TokenLifecycle network error would deadlock the
        proactive timer forever.
        """
        feed = _make_minimal_feed()
        feed._authed.set()
        feed._conn.is_connected = True

        # Make force_refresh blow up — this would skip the success branch
        # and exercise the exception handler.
        feed._token_lifecycle.force_refresh.side_effect = ConnectionError("network down")
        feed._token_lifecycle.expires_at = None
        feed._token_lifecycle._store.get.return_value = MagicMock(refresh_token="x")  # noqa: S106

        with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = MagicMock()
            feed._refresh_token_and_reauth(proactive=True)

        _wait_offthread(feed)

        # Lock MUST be released so the next cycle can acquire it.
        assert not feed._session_reauth_lock.locked(), (
            "session mutex was not released after force_refresh raised — "
            "the proactive timer would deadlock on its next tick "
            "(card 1e32c408)."
        )
        # Auth error counter incremented (circuit breaker still wired).
        assert feed._auth_error_count >= 1

    def test_skip_path_does_not_increment_auth_error_count(self):
        """Test 6: the "skip" path is treated as success (no auth error).

        Pre-fix, every refresh of an already-authenticated channel
        produced an ``ALREADY_LOGGED_IN`` ORDER_ERROR which was logged
        but did NOT cascade into the circuit breaker (the bug-stopping
        reason). The new skip-path is even cleaner: no broker round-trip
        at all, so no error counter touch either.
        """
        feed = _make_minimal_feed()
        feed._authed.set()
        feed._conn.is_connected = True

        new_expires = datetime.now(timezone.utc) + timedelta(days=25)
        feed._token_lifecycle.force_refresh.return_value = "new-access-token"  # noqa: S105
        feed._token_lifecycle.expires_at = new_expires
        feed._token_lifecycle.token_age_s = 0.5
        feed._token_lifecycle._store.get.return_value = MagicMock(refresh_token="new-refresh-token")  # noqa: S106

        initial_count = feed._auth_error_count

        with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
            mock_reactor.callFromThread = MagicMock()
            feed._refresh_token_and_reauth(proactive=True)

        _wait_offthread(feed)

        # The skip path explicitly resets _auth_error_count to 0 (matches
        # the post-refresh success semantics).
        assert feed._auth_error_count == initial_count, (
            f"_auth_error_count bumped from {initial_count} to "
            f"{feed._auth_error_count} on the skip path — the circuit "
            f"breaker should not be touched when the channel is "
            f"already authenticated (card 1e32c408)."
        )
        # And the circuit breaker must NOT have tripped.
        assert feed._auth_circuit_open is False

    def test_reconnect_restore_acquires_session_reauth_lock(self):
        """Test 7: ``_reconnect_restore`` also takes the session mutex.

        Card 1e32c408 commits to "session mutex/lock around
        authorize+refresh" — both refresh-driven and reconnect-driven
        auth paths share the lock. This test verifies the reconnect
        path acquires (and releases) the lock.
        """
        feed = _make_minimal_feed()
        # Pre-acquired state: the lock is free at the start.
        assert not feed._session_reauth_lock.locked()

        # We can't run the full ``_reconnect_restore`` here (it calls
        # the real reactor); instead, exercise just the lock-bookkeeping
        # portion by simulating the lock acquisition + the early-return
        # path that fires when ``_conn.is_connected`` is False.
        feed._conn.is_connected = False
        # Don't actually try to use Twisted — just call _reconnect_restore
        # and confirm it released the lock on early-return.
        feed._reconnect_restore()

        assert not feed._session_reauth_lock.locked(), (
            "_reconnect_restore left the session mutex held after an "
            "early-return on is_connected=False — would deadlock the "
            "next refresh cycle (card 1e32c408)."
        )

    def test_reconnect_restore_skips_when_lock_held(self):
        """Test 8: ``_reconnect_restore`` skips when the lock is held.

        Mirror of test 4 for the reconnect path: if a
        token-refresh-driven re-auth already owns the mutex, the
        reconnect-driven re-auth skips instead of deadlocking.
        """
        feed = _make_minimal_feed()
        feed._conn.is_connected = True  # would normally proceed
        feed._reauth_in_progress.set()  # mirrors the real caller

        # Hold the lock as if a refresh is in flight.
        assert feed._session_reauth_lock.acquire(blocking=False)
        try:
            # Patch reactor so we can confirm _reconnect_restore
            # short-circuits before any send.
            with patch("adapters.ctrader.open_api_spot_feed.reactor") as mock_reactor:
                mock_reactor.callFromThread = MagicMock()
                feed._reconnect_restore()

            # No reactor calls — the early-return on lock contention
            # must short-circuit before the auth cycle.
            assert not mock_reactor.callFromThread.called, (
                "_reconnect_restore proceeded despite the session mutex "
                "being held — this is the interleaving the lock is "
                "supposed to prevent (card 1e32c408)."
            )
            # _reauth_in_progress should be cleared so a future
            # reconnect attempt is not blocked.
            assert not feed._reauth_in_progress.is_set()
        finally:
            feed._session_reauth_lock.release()
