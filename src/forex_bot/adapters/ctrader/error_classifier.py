"""Structured cTrader error classification helpers."""

from dataclasses import dataclass
from enum import Enum


class ErrorTier(Enum):
    """Recovery tier for a cTrader error."""

    TIER_1_TRANSIENT = "tier_1_transient"
    TIER_2_BACKOFF = "tier_2_backoff"
    TIER_3A_OPERATION = "tier_3a_operation"
    TIER_3B_SYSTEM = "tier_3b_system"


@dataclass(frozen=True)
class ClassifiedError:
    """Normalized error classification result."""

    raw_code: str
    raw_description: str
    tier: ErrorTier
    action: str
    retry_after_ms: int
    is_recoverable: bool


_TIER_RULES: dict[str, tuple[ErrorTier, str, int, bool]] = {
    "CONNECTION_LOST": (ErrorTier.TIER_1_TRANSIENT, "reconnect", 0, True),
    "HEARTBEAT_TIMEOUT": (ErrorTier.TIER_1_TRANSIENT, "reconnect", 0, True),
    "TCP_RESET": (ErrorTier.TIER_1_TRANSIENT, "reconnect", 0, True),
    "DNS_FAILURE": (ErrorTier.TIER_1_TRANSIENT, "reconnect", 0, True),
    "SERVER_NOT_READY": (ErrorTier.TIER_2_BACKOFF, "retry", 5_000, True),
    "REQUEST_TIMEOUT": (ErrorTier.TIER_2_BACKOFF, "retry", 1_000, True),
    "TOO_MANY_REQUESTS": (ErrorTier.TIER_2_BACKOFF, "retry", 10_000, True),
    "SERVICE_UNAVAILABLE": (ErrorTier.TIER_2_BACKOFF, "retry", 30_000, True),
    "INVALID_VOLUME": (ErrorTier.TIER_3A_OPERATION, "reject_request", 0, False),
    "INVALID_SYMBOL": (ErrorTier.TIER_3A_OPERATION, "reject_request", 0, False),
    "INSUFFICIENT_FUNDS": (ErrorTier.TIER_3A_OPERATION, "reject_request", 0, False),
    "MARKET_CLOSED": (ErrorTier.TIER_3A_OPERATION, "reject_request", 0, False),
    "BAD_REQUEST": (ErrorTier.TIER_3A_OPERATION, "reject_request", 0, False),
    "NOT_LOGGED_IN": (ErrorTier.TIER_3B_SYSTEM, "halt", 0, False),
    "AUTH_EXPIRED": (ErrorTier.TIER_3B_SYSTEM, "halt", 0, False),
    "TOKEN_INVALIDATED": (ErrorTier.TIER_3B_SYSTEM, "halt", 0, False),
    "ACCOUNT_DISABLED": (ErrorTier.TIER_3B_SYSTEM, "halt", 0, False),
    "MAX_RETRIES_EXCEEDED": (ErrorTier.TIER_3B_SYSTEM, "halt", 0, False),
}


def classify_error(error_code: str, description: str = "") -> ClassifiedError:
    """Classify a cTrader error code into a recovery tier."""

    normalized_code = (error_code or "").strip().upper() or "UNKNOWN"
    tier, action, retry_after_ms, is_recoverable = _TIER_RULES.get(
        normalized_code,
        (ErrorTier.TIER_3A_OPERATION, "reject_request", 0, False),
    )
    return ClassifiedError(
        raw_code=normalized_code,
        raw_description=description or "",
        tier=tier,
        action=action,
        retry_after_ms=retry_after_ms,
        is_recoverable=is_recoverable,
    )
