"""Exponential backoff reconnection strategy with decorrelated jitter.

Implements the AWS-style "decorrelated jitter" backoff algorithm (research
doc section 5):

    sleep = min(cap, random(base, prev * 3))

This avoids synchronized retry storms and provides bounded, predictable
backoff behaviour.

Error tier routing (research doc section 6):
    - TIER_1 (transient)  → RETRY (fast, short sleep)
    - TIER_2 (backoff)    → RETRY (longer sleep)
    - TIER_3A (operation) → NO_RETRY (can't fix by reconnecting)
    - TIER_3B (system)    → HALT (auth/system failure — stop)

Public API::

    strategy = ReconnectStrategy(max_attempts=10)
    decision = strategy.decide(error, attempt=3)
    if decision.action == ReconnectAction.RETRY:
        time.sleep(decision.sleep_seconds)
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import Enum

from .error_classifier import ClassifiedError, ErrorTier, classify_error

logger = logging.getLogger("ayumi.connection.reconnect")

# ── Constants ──────────────────────────────────────────────────────────────

BASE_SLEEP_S = 1.0
CAP_SLEEP_S = 60.0
DEFAULT_MAX_ATTEMPTS = 10


# ── Data types ─────────────────────────────────────────────────────────────


class ReconnectAction(Enum):
    """Decision actions for the reconnect strategy."""

    RETRY = "retry"
    NO_RETRY = "no_retry"
    HALT = "halt"


@dataclass(frozen=True)
class ReconnectDecision:
    """Immutable reconnect decision.

    Attributes:
        action: What to do (RETRY, NO_RETRY, HALT).
        sleep_seconds: How long to sleep before retrying (0 if not retrying).
        reason: Human-readable reason for the decision.
        attempt: The attempt number this decision was made for.
    """

    action: ReconnectAction
    sleep_seconds: float
    reason: str
    attempt: int


# ── Strategy ───────────────────────────────────────────────────────────────


class ReconnectStrategy:
    """Exponential backoff with decorrelated jitter.

    Args:
        base_sleep: Minimum sleep in seconds (default 1).
        cap_sleep: Maximum sleep in seconds (default 60).
        max_attempts: Maximum retry attempts before giving up (default 10).
    """

    def __init__(
        self,
        *,
        base_sleep: float = BASE_SLEEP_S,
        cap_sleep: float = CAP_SLEEP_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        self._base = base_sleep
        self._cap = cap_sleep
        self._max_attempts = max_attempts
        self._prev_sleep = base_sleep  # for decorrelated jitter state

    def reset(self) -> None:
        """Reset the internal jitter state after a successful connection."""
        self._prev_sleep = self._base
        logger.debug("[Reconnect] Reset jitter state to base=%s", self._base)

    def decide(
        self,
        error: str | ClassifiedError,
        attempt: int = 1,
        description: str = "",
    ) -> ReconnectDecision:
        """Evaluate an error and decide what to do.

        Args:
            error: Error code string OR a ClassifiedError instance.
            attempt: The current attempt number (1-based).
            description: Optional error description (used if *error* is a string).

        Returns:
            ReconnectDecision telling the caller whether to retry, skip, or halt.
        """
        if isinstance(error, ClassifiedError):
            classified = error
        else:
            classified = classify_error(error, description)

        # Check max attempts first
        if attempt >= self._max_attempts:
            return ReconnectDecision(
                action=ReconnectAction.NO_RETRY,
                sleep_seconds=0,
                reason=f"max_attempts_reached ({attempt}/{self._max_attempts})",
                attempt=attempt,
            )

        # Route by tier
        if classified.tier == ErrorTier.TIER_1_TRANSIENT:
            sleep = self._compute_sleep()
            logger.info(
                "[Reconnect] TIER_1 RETRY attempt=%d sleep=%.1fs (%s)",
                attempt,
                sleep,
                classified.raw_code,
            )
            return ReconnectDecision(
                action=ReconnectAction.RETRY,
                sleep_seconds=sleep,
                reason=f"tier_1_transient:{classified.raw_code}",
                attempt=attempt,
            )

        if classified.tier == ErrorTier.TIER_2_BACKOFF:
            sleep = self._compute_sleep(multiplier=2.0)
            logger.info(
                "[Reconnect] TIER_2 RETRY attempt=%d sleep=%.1fs (%s, retry_after=%dms)",
                attempt,
                sleep,
                classified.raw_code,
                classified.retry_after_ms,
            )
            return ReconnectDecision(
                action=ReconnectAction.RETRY,
                sleep_seconds=sleep,
                reason=f"tier_2_backoff:{classified.raw_code}",
                attempt=attempt,
            )

        if classified.tier == ErrorTier.TIER_3A_OPERATION:
            logger.warning(
                "[Reconnect] TIER_3A NO_RETRY (%s) — operation error, reconnect won't help",
                classified.raw_code,
            )
            return ReconnectDecision(
                action=ReconnectAction.NO_RETRY,
                sleep_seconds=0,
                reason=f"tier_3a_operation:{classified.raw_code}",
                attempt=attempt,
            )

        # TIER_3B_SYSTEM
        logger.error(
            "[Reconnect] TIER_3B HALT (%s) — system/auth failure, stopping",
            classified.raw_code,
        )
        return ReconnectDecision(
            action=ReconnectAction.HALT,
            sleep_seconds=0,
            reason=f"tier_3b_system:{classified.raw_code}",
            attempt=attempt,
        )

    # ── Jitter algorithm ───────────────────────────────────────────────────

    def _compute_sleep(self, multiplier: float = 1.0) -> float:
        """Compute decorrelated jitter sleep.

        AWS formula: ``sleep = min(cap, random(base, prev * multiplier * 3))``

        The multiplier allows TIER_2 errors to back off more aggressively.
        """
        upper = self._prev_sleep * multiplier * 3
        sleep = min(self._cap, random.uniform(self._base, max(self._base, upper)))  # noqa: S311 — non-cryptographic reconnect sleep jitter
        self._prev_sleep = sleep
        return round(sleep, 2)

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def prev_sleep(self) -> float:
        """Last computed sleep value (for diagnostics)."""
        return self._prev_sleep
