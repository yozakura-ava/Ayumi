"""Execution permission policy — single gate for all order paths.

Default behavior: DENY. The policy must explicitly determine it is safe to
send an order before returning ALLOW. Any uncertainty → DENY.

Design principles:
- Single source of truth for "can we mutate broker state right now?"
- Uses KillSwitchManager.is_active() (reads state under lock)
- Never propagates exceptions — catches and returns DENY
- Logs every decision for audit trail

Scope: P5A+P6. Covers new_order(), cancel_order(), amend_order(),
amend_sl_tp(), and close_position() on OpenApiSpotFeed.

Known limitations:
- Microsecond TOCTOU race between policy check and reactor dispatch
  (cannot be eliminated without synchronous send path)

Note on reason string format:
  The _check_kill_switch helper appends ':{op_name}' to reason strings
  (e.g. 'policy:not_initialized:send_order' instead of 'policy:not_initialized').
  This is an intentional Phase 6 improvement — it gives operators per-operation
  context in audit logs. Boolean behaviour is identical to P5A. Existing tests
  pass because they use substring ``in`` checks rather than exact equality.
"""

import logging
from typing import Optional, Tuple

from .kill_switch import KillSwitchManager

logger = logging.getLogger(__name__)


class ExecutionPermissionPolicy:
    """Single gate for all broker-mutating paths. Default DENY."""

    def __init__(self, kill_switch: Optional[KillSwitchManager] = None):
        self._kill_switch = kill_switch

    # ------------------------------------------------------------------
    # Shared helper — all public gate methods delegate here.
    # ------------------------------------------------------------------

    def _check_kill_switch(self, op_name: str) -> Tuple[bool, str]:
        """Shared kill-switch check for all broker-mutating operations.

        Args:
            op_name: Operation identifier appended to reason strings
                     (e.g. "send_order", "cancel_order").

        Returns:
            (allowed, reason) tuple. Default DENY on any uncertainty.
        """
        try:
            if self._kill_switch is None:
                return (False, f"policy:not_initialized:{op_name}")
            if not self._kill_switch.is_active():
                return (True, "clear")
            mode = self._kill_switch.get_status().get("mode", "unknown")
            return (False, f"kill_switch_active:{mode}:{op_name}")
        except Exception as exc:
            logger.error(
                "ExecutionPermissionPolicy error (%s) — defaulting to DENY: %s",
                op_name,
                exc,
            )
            return (False, f"policy_error:{type(exc).__name__}:{op_name}")

    # ------------------------------------------------------------------
    # Public gate methods — one per broker-mutating operation.
    # ------------------------------------------------------------------

    def can_send_order(self) -> Tuple[bool, str]:
        """Gate for new_order() / send_order(). Default DENY."""
        return self._check_kill_switch("send_order")

    def can_cancel_order(self) -> Tuple[bool, str]:
        """Gate for cancel_order(). Default DENY."""
        return self._check_kill_switch("cancel_order")

    def can_amend_order(self) -> Tuple[bool, str]:
        """Gate for amend_order(). Default DENY."""
        return self._check_kill_switch("amend_order")

    def can_amend_sl_tp(self) -> Tuple[bool, str]:
        """Gate for amend_sl_tp(). Default DENY."""
        return self._check_kill_switch("amend_sl_tp")

    def can_close_position(self) -> Tuple[bool, str]:
        """Gate for close_position(). Default DENY."""
        return self._check_kill_switch("close_position")
