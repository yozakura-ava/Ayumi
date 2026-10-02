"""Tests for FAILED state recovery and auth failure escalation.

Covers the 2026-07-03 incident where the connection state machine's FAILED
state was too restrictive, trapping the connection in a recovery loop:
  - 70 rejected state transitions from FAILED in one session
  - auth_errors climbed from 5 → 21 without recovery
  - TCP reconnected but state transitions to CONNECTED/RECONNECTING were rejected

Test areas:
  1. FAILED → RECONNECTING transition succeeds (was previously rejected)
  2. FAILED → CONNECTED transition succeeds (was previously rejected)
  3. FAILED → APP_AUTHENTICATING transition succeeds (was previously rejected)
  4. Auth failure burst → verify state machine recovers when auth succeeds
  5. Auth failure escalation threshold triggers full reconnect signal
  6. Auth success resets the failure counter
"""

import sys
import unittest

# Ensure the source path is available
sys.path.insert(0, "src/forex_bot")

from adapters.ctrader.connection_manager import (
    AUTH_FULL_RECONNECT_THRESHOLD,
    ConnectionManager,
)
from adapters.ctrader.connection_state import (  # noqa: I001
    ConnectionState,
    ConnectionStateManager,
    is_valid_transition,
)


class TestFailedStateRecoveryTransitions(unittest.TestCase):
    """Verify that FAILED state allows recovery transitions."""

    def setUp(self):
        self.sm = ConnectionStateManager(name="test")

    def test_failed_to_reconnecting_allowed(self):
        """FAILED → RECONNECTING must be allowed for recovery."""
        self.assertTrue(
            is_valid_transition(ConnectionState.FAILED, ConnectionState.RECONNECTING),
            "FAILED → RECONNECTING should be a valid transition",
        )

    def test_failed_to_connected_allowed(self):
        """FAILED → CONNECTED must be allowed (TCP reconnected from FAILED)."""
        self.assertTrue(
            is_valid_transition(ConnectionState.FAILED, ConnectionState.CONNECTED),
            "FAILED → CONNECTED should be a valid transition",
        )

    def test_failed_to_app_authenticating_allowed(self):
        """FAILED → APP_AUTHENTICATING must be allowed (re-auth from FAILED)."""
        self.assertTrue(
            is_valid_transition(ConnectionState.FAILED, ConnectionState.APP_AUTHENTICATING),
            "FAILED → APP_AUTHENTICATING should be a valid transition",
        )

    def test_failed_to_connecting_still_allowed(self):
        """FAILED → CONNECTING must remain allowed (pre-existing)."""
        self.assertTrue(
            is_valid_transition(ConnectionState.FAILED, ConnectionState.CONNECTING),
        )

    def test_failed_to_disconnected_still_allowed(self):
        """FAILED → DISCONNECTED must remain allowed (pre-existing)."""
        self.assertTrue(
            is_valid_transition(ConnectionState.FAILED, ConnectionState.DISCONNECTED),
        )

    def test_full_recovery_path_from_failed(self):
        """End-to-end: FAILED → RECONNECTING → CONNECTING → CONNECTED → AUTHENTICATED."""
        self.sm._state = ConnectionState.FAILED

        # FAILED → RECONNECTING
        self.assertTrue(self.sm.transition_to(ConnectionState.RECONNECTING, reason="recovery"))
        self.assertEqual(self.sm.state, ConnectionState.RECONNECTING)

        # RECONNECTING → CONNECTING
        self.assertTrue(self.sm.transition_to(ConnectionState.CONNECTING, reason="backoff_done"))
        self.assertEqual(self.sm.state, ConnectionState.CONNECTING)

        # CONNECTING → CONNECTED
        self.assertTrue(self.sm.transition_to(ConnectionState.CONNECTED, reason="tcp_up"))
        self.assertEqual(self.sm.state, ConnectionState.CONNECTED)

        # CONNECTED → APP_AUTHENTICATING
        self.assertTrue(self.sm.transition_to(ConnectionState.APP_AUTHENTICATING, reason="start_auth"))
        self.assertEqual(self.sm.state, ConnectionState.APP_AUTHENTICATING)

        # APP_AUTHENTICATING → ACCT_AUTHENTICATING
        self.assertTrue(self.sm.transition_to(ConnectionState.ACCT_AUTHENTICATING, reason="app_ok"))
        self.assertEqual(self.sm.state, ConnectionState.ACCT_AUTHENTICATING)

        # ACCT_AUTHENTICATING → AUTHENTICATED
        self.assertTrue(self.sm.transition_to(ConnectionState.AUTHENTICATED, reason="acct_ok"))
        self.assertEqual(self.sm.state, ConnectionState.AUTHENTICATED)


class TestAuthFailureBurstRecovery(unittest.TestCase):
    """Simulate the 2026-07-03 incident: auth burst then recovery."""

    def setUp(self):
        self.sm = ConnectionStateManager(name="test")
        # Simulate a connection that was AUTHENTICATED then hit auth failures
        self.sm._state = ConnectionState.AUTHENTICATED

    def test_auth_burst_to_failed_then_recovers(self):
        """Simulate: AUTHENTICATED → DEGRADED → FAILED → RECONNECTING → recovery."""
        # Auth errors escalate: 3 → DEGRADED, 5 → FAILED
        self.assertTrue(self.sm.transition_to(ConnectionState.DEGRADED, reason="auth_degraded:3"))
        self.assertTrue(self.sm.transition_to(ConnectionState.FAILED, reason="auth_errors:5"))

        # Before the fix, FAILED → RECONNECTING would be rejected
        # Now it should succeed:
        self.assertTrue(
            self.sm.transition_to(ConnectionState.RECONNECTING, reason="stuck_state_recovery"),
            "FAILED → RECONNECTING must succeed for recovery",
        )

        # Complete the recovery path
        self.assertTrue(self.sm.transition_to(ConnectionState.CONNECTING, reason="backoff"))
        self.assertTrue(self.sm.transition_to(ConnectionState.CONNECTED, reason="tcp_up"))
        self.assertTrue(self.sm.transition_to(ConnectionState.APP_AUTHENTICATING, reason="start_auth"))
        self.assertTrue(self.sm.transition_to(ConnectionState.ACCT_AUTHENTICATING, reason="app_ok"))
        self.assertTrue(self.sm.transition_to(ConnectionState.AUTHENTICATED, reason="auth_success"))
        self.assertEqual(self.sm.state, ConnectionState.AUTHENTICATED)

    def test_failed_direct_to_connected(self):
        """FAILED → CONNECTED: TCP reconnected while state was FAILED."""
        self.sm._state = ConnectionState.FAILED
        self.assertTrue(
            self.sm.transition_to(ConnectionState.CONNECTED, reason="tcp_reconnected"),
            "FAILED → CONNECTED should be allowed when TCP reconnects",
        )


class TestAuthFailureEscalation(unittest.TestCase):
    """Test ConnectionManager auth failure tracking and full reconnect signal."""

    def setUp(self):
        self.mgr = ConnectionManager()

    def test_record_auth_failure_increments(self):
        """Each auth failure increments the counter."""
        self.assertEqual(self.mgr._consecutive_auth_failures, 0)
        result = self.mgr.record_auth_failure()
        self.assertFalse(result)
        self.assertEqual(self.mgr._consecutive_auth_failures, 1)

    def test_threshold_triggers_full_reconnect(self):
        """After AUTH_FULL_RECONNECT_THRESHOLD failures, full reconnect is signalled."""
        for i in range(AUTH_FULL_RECONNECT_THRESHOLD - 1):
            should_reconnect = self.mgr.record_auth_failure()
            self.assertFalse(should_reconnect, f"Should not trigger on failure {i + 1}")

        # The Nth failure triggers the signal
        should_reconnect = self.mgr.record_auth_failure()
        self.assertTrue(should_reconnect, "Should trigger full reconnect at threshold")

    def test_threshold_resets_after_trigger(self):
        """Counter resets to 0 after threshold is hit."""
        for _ in range(AUTH_FULL_RECONNECT_THRESHOLD):
            self.mgr.record_auth_failure()

        self.assertEqual(self.mgr._consecutive_auth_failures, 0)

    def test_auth_success_resets_counter(self):
        """record_auth_success resets the failure counter."""
        for _ in range(3):
            self.mgr.record_auth_failure()
        self.assertEqual(self.mgr._consecutive_auth_failures, 3)

        self.mgr.record_auth_success()
        self.assertEqual(self.mgr._consecutive_auth_failures, 0)

    def test_reset_reconnect_state_also_resets_auth_failures(self):
        """reset_reconnect_state() resets auth failures too."""
        for _ in range(4):
            self.mgr.record_auth_failure()
        self.assertEqual(self.mgr._consecutive_auth_failures, 4)

        self.mgr.reset_reconnect_state()
        self.assertEqual(self.mgr._consecutive_auth_failures, 0)

    def test_authenticate_with_retry_resets_on_success(self):
        """authenticate_with_retry calls record_auth_success on success."""
        call_count = [0]

        def auth_fn():
            call_count[0] += 1
            return call_count[0] >= 2  # succeed on second attempt

        # Pre-poison the counter
        for _ in range(5):
            self.mgr.record_auth_failure()
        self.assertEqual(self.mgr._consecutive_auth_failures, 5)

        result = self.mgr.authenticate_with_retry(auth_fn, max_attempts=3, backoff_seconds=(0.01, 0.01, 0.01))
        self.assertTrue(result)
        self.assertEqual(self.mgr._consecutive_auth_failures, 0)

    def test_simulated_incident_scenario(self):
        """Full incident simulation: auth burst → FAILED → recovery → auth success.

        Mirrors the 2026-07-03 incident timeline:
        - Auth errors climb from 5 to 21
        - State stuck in FAILED
        - Eventually breaks free and recovers
        """
        sm = ConnectionStateManager(name="incident_sim")

        # Start healthy
        sm._state = ConnectionState.AUTHENTICATED

        # Auth failure burst (simulating the 2026-07-03 incident)
        sm.transition_to(ConnectionState.DEGRADED, reason="auth_degraded:3")
        sm.transition_to(ConnectionState.FAILED, reason="auth_errors:5")

        # At this point, before the fix, the system would be stuck.
        # 15 reconnection attempts, all rejected because FAILED→RECONNECTING invalid.

        # With the fix: FAILED → RECONNECTING succeeds
        recovered = sm.transition_to(ConnectionState.RECONNECTING, reason="stuck_state_breakout")
        self.assertTrue(recovered, "State machine must break out of FAILED")

        # Recovery path
        sm.transition_to(ConnectionState.CONNECTING, reason="reconnect_backoff")
        sm.transition_to(ConnectionState.CONNECTED, reason="tcp_established")
        sm.transition_to(ConnectionState.APP_AUTHENTICATING, reason="begin_auth")
        sm.transition_to(ConnectionState.ACCT_AUTHENTICATING, reason="app_auth_ok")
        sm.transition_to(ConnectionState.AUTHENTICATED, reason="account_auth_ok")

        # Verify fully recovered
        self.assertTrue(sm.is_authenticated)
        self.assertFalse(sm.is_failed)


if __name__ == "__main__":
    unittest.main()
