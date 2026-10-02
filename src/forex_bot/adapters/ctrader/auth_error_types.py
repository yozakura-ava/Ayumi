"""Centralized cTrader auth/request error classification.

Classifies error codes from cTrader OpenAPI into actionable buckets.
Each classification defines whether refresh, reconnect, order-sending,
and kill/freeze activation are allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AuthFaultType(Enum):
    REFRESHABLE_TOKEN_FAULT = "refreshable_token_fault"  # noqa: S105 — enum identifier string, not a credential; the heuristic matches "_token_" in the name
    ACCOUNT_AUTHORIZATION_FAULT = "account_authorization_fault"
    PERMISSION_OR_ACCESS_DENIED = "permission_or_access_denied"
    MALFORMED_REQUEST = "malformed_request"
    CONFIGURATION_FAULT = "configuration_fault"
    TRANSIENT_CONNECTION_FAULT = "transient_connection_fault"
    UNKNOWN_FATAL_AUTH_FAULT = "unknown_fatal_auth_fault"


@dataclass
class AuthFaultPolicy:
    """Defines what actions are allowed for a given fault type."""

    can_refresh: bool = False
    can_reconnect: bool = False
    can_send_orders: bool = False
    activate_kill_switch: bool = False
    requires_escalation: bool = False
    safe_log_fields: tuple = ("error_code", "description", "fault_type")

    @property
    def should_fail_closed(self) -> bool:
        return not self.can_send_orders


# Classification table
ERROR_CLASSIFICATIONS: dict[str, AuthFaultType] = {
    "CH_OAUTH_TOKEN_EXPIRED": AuthFaultType.REFRESHABLE_TOKEN_FAULT,
    "CH_INVALID_TOKEN": AuthFaultType.REFRESHABLE_TOKEN_FAULT,
    "SESSION_EXPIRED": AuthFaultType.REFRESHABLE_TOKEN_FAULT,
    "CH_ACCESS_TOKEN_INVALID": AuthFaultType.REFRESHABLE_TOKEN_FAULT,
    "ACCESS_DENIED": AuthFaultType.PERMISSION_OR_ACCESS_DENIED,
    "INVALID_REQUEST": AuthFaultType.MALFORMED_REQUEST,
    "CH_ACCOUNT_NOT_AUTHORIZED": AuthFaultType.ACCOUNT_AUTHORIZATION_FAULT,
    "CH_TRADING_ACCOUNT_NOT_AUTHORIZED": AuthFaultType.ACCOUNT_AUTHORIZATION_FAULT,
}

POLICIES: dict[AuthFaultType, AuthFaultPolicy] = {
    AuthFaultType.REFRESHABLE_TOKEN_FAULT: AuthFaultPolicy(can_refresh=True, can_reconnect=True, can_send_orders=False),
    AuthFaultType.ACCOUNT_AUTHORIZATION_FAULT: AuthFaultPolicy(
        can_refresh=False,
        can_reconnect=False,
        can_send_orders=False,
        activate_kill_switch=True,
        requires_escalation=True,
    ),
    AuthFaultType.PERMISSION_OR_ACCESS_DENIED: AuthFaultPolicy(
        can_refresh=False,
        can_reconnect=False,
        can_send_orders=False,
        activate_kill_switch=True,
        requires_escalation=True,
    ),
    AuthFaultType.MALFORMED_REQUEST: AuthFaultPolicy(
        can_refresh=False,
        can_reconnect=False,
        can_send_orders=False,
        requires_escalation=True,
    ),
    AuthFaultType.CONFIGURATION_FAULT: AuthFaultPolicy(
        can_refresh=False,
        can_reconnect=False,
        can_send_orders=False,
        activate_kill_switch=True,
        requires_escalation=True,
    ),
    AuthFaultType.TRANSIENT_CONNECTION_FAULT: AuthFaultPolicy(
        can_refresh=False, can_reconnect=True, can_send_orders=False
    ),
    AuthFaultType.UNKNOWN_FATAL_AUTH_FAULT: AuthFaultPolicy(
        can_refresh=False,
        can_reconnect=False,
        can_send_orders=False,
        activate_kill_switch=True,
        requires_escalation=True,
    ),
}


def classify_error(error_code: str, description: str = "") -> AuthFaultType:
    """Classify an error code into a fault type."""
    code_upper = error_code.upper().strip()
    if code_upper in ERROR_CLASSIFICATIONS:
        return ERROR_CLASSIFICATIONS[code_upper]
    # Heuristic classification for unknown codes
    desc_lower = description.lower()
    if "not authorized" in desc_lower or "not authorised" in desc_lower:
        return AuthFaultType.ACCOUNT_AUTHORIZATION_FAULT
    if "access" in desc_lower and "denied" in desc_lower:
        return AuthFaultType.PERMISSION_OR_ACCESS_DENIED
    if "malformed" in desc_lower or "invalid format" in desc_lower:
        return AuthFaultType.MALFORMED_REQUEST
    if "timeout" in desc_lower or "unreachable" in desc_lower:
        return AuthFaultType.TRANSIENT_CONNECTION_FAULT
    return AuthFaultType.UNKNOWN_FATAL_AUTH_FAULT


def get_policy(error_code: str, description: str = "") -> tuple[AuthFaultType, AuthFaultPolicy]:
    """Classify an error and return both the fault type and its policy."""
    fault_type = classify_error(error_code, description)
    return fault_type, POLICIES[fault_type]
