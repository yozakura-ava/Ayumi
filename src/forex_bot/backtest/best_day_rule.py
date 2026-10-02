"""Best Day Rule tracker (FTMO funded phase).

Implements FTMO's Best Day Rule for the funded account phase: no single
trading day's contribution to cumulative profits may exceed a configured
threshold (default 50%).

Reference: docs/research/ftmo-risk-and-port-sizing-2026-07.md §A.7
Blend plan: docs/plans/multi-strategy-blend-plan-2026-07.md (Phase A, item 2)

Mechanics:
- Active only during the funded account phase (challenge phase → rule inactive)
- Daily reset at 00:00 CE(S)T (default; configurable per broker)
- Compares TODAY's realized P/L + planned entry profit to the configured
  threshold of CUMULATIVE realized P/L since funding start
- Rule constrains concentration of profits; losing trades are never blocked
  because they reduce today's share, not increase it

Caveat (per research §Caveats):
    The 50% threshold appears in multiple sources but is not clearly
    documented on FTMO's official objectives page. Verify the exact
    threshold in the FTMO client area terms before relying on this rule
    for live trading. The threshold is configurable via constructor arg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# Conservative default threshold per FTMO research §A.7.
DEFAULT_THRESHOLD = 0.50

# Default reset timezone offset (hours from UTC). 1 = CET (winter);
# 2 = CEST (summer). For exact DST handling, pass a zoneinfo-backed
# tz_offset_hours via the constructor — see __init__ docstring.
DEFAULT_RESET_TZ_OFFSET_HOURS = 1

# Valid account phases for this tracker.
VALID_PHASES = ("challenge", "funded")


@dataclass(frozen=True)
class BestDayCheckResult:
    """Outcome of a single check_entry() call."""

    allowed: bool
    reason: str
    today_pnl: float
    cumulative_pnl: float
    projected_today_share: float
    threshold: float


class BestDayRuleTracker:
    """Track daily P/L since funding start and gate entries against the 50% cap.

    The tracker is INACTIVE when account_phase != "funded". When inactive,
    check_entry() always returns True (no gating, no overhead beyond the
    attribute lookup).

    Thread-safety: not thread-safe. Use one tracker per trading thread/loop.
    """

    def __init__(
        self,
        account_phase: str,
        threshold: float = DEFAULT_THRESHOLD,
        reset_tz_offset_hours: int = DEFAULT_RESET_TZ_OFFSET_HOURS,
    ) -> None:
        """Construct a tracker.

        Args:
            account_phase: "challenge" or "funded". Only "funded" activates
                the rule. Other values raise ValueError.
            threshold: Maximum allowed share of today's profit contribution
                to cumulative profits. Must be in (0, 1]. Default 0.50.
            reset_tz_offset_hours: Hours from UTC for the daily reset. CET
                is +1 (winter), CEST is +2 (summer). For DST-correct
                behavior, compute the current offset for "Europe/Berlin"
                and pass it explicitly each morning (out of scope here).

        Raises:
            ValueError: If threshold or account_phase is invalid.
        """
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold!r}")
        if account_phase not in VALID_PHASES:
            raise ValueError(f"account_phase must be one of {VALID_PHASES}, got {account_phase!r}")

        self._account_phase: str = account_phase
        self._threshold: float = threshold
        self._reset_tz_offset: int = reset_tz_offset_hours

        # Cumulative realized P/L since funding start. Can be negative
        # in early funded days; rule only applies when positive.
        self._cumulative_pnl: float = 0.0
        # Today's realized P/L (resets at 00:00 in the configured tz).
        self._today_pnl: float = 0.0
        # ISO date string (in the configured tz) of the last reset. None
        # until the first reset/check happens.
        self._last_reset_date: Optional[str] = None

    # ── Read-only state ────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """True iff the rule is being enforced (funded phase only)."""
        return self._account_phase == "funded"

    @property
    def account_phase(self) -> str:
        return self._account_phase

    @property
    def reset_tz_offset_hours(self) -> int:
        return self._reset_tz_offset

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def cumulative_pnl(self) -> float:
        return self._cumulative_pnl

    @property
    def today_pnl(self) -> float:
        return self._today_pnl

    @property
    def last_reset_date(self) -> Optional[str]:
        return self._last_reset_date

    # ── Internal helpers ───────────────────────────────────────────────────

    def _to_local(self, dt: datetime) -> datetime:
        """Convert a datetime to the reset timezone.

        Naive datetimes are assumed UTC. Aware datetimes are converted to
        the configured offset (default +1 = CET winter).
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=self._reset_tz_offset)))

    def _maybe_reset(self, now: datetime) -> None:
        """Reset today's P/L if the local date has advanced."""
        local = self._to_local(now)
        local_date = local.date().isoformat()
        if self._last_reset_date != local_date:
            self._today_pnl = 0.0
            self._last_reset_date = local_date

    # ── Public API ────────────────────────────────────────────────────────

    def record_trade_close(self, close_time: datetime, pnl_dollars: float) -> None:
        """Record a closed trade's realized P/L.

        Args:
            close_time: When the trade closed. Naive datetimes are
                treated as UTC. The daily-reset logic uses this timestamp.
            pnl_dollars: Realized P/L in account currency (can be negative).
        """
        if not self.is_active:
            return
        self._maybe_reset(close_time)
        self._today_pnl += pnl_dollars
        self._cumulative_pnl += pnl_dollars

    def check_entry(
        self,
        planned_profit_dollars: float,
        now: Optional[datetime] = None,
    ) -> BestDayCheckResult:
        """Decide whether a planned entry is allowed.

        The rule says: today's profit contribution to cumulative profits
        must not exceed the configured threshold. We project what today's
        share WOULD be if the planned entry wins, then compare.

        Notes:
        - Inactive phase → always allowed.
        - Non-positive planned profit → always allowed (rule is about
          profit concentration; losses reduce today's share).
        - Non-positive cumulative P/L → always allowed (no concentration
          of profits exists yet).
        - The check uses "if the trade wins" projection — we don't know
          the outcome in advance, but the rule's intent is to prevent
          over-committing to a winning day, so we gate on the upside.

        Args:
            planned_profit_dollars: Expected profit if the trade works.
                Must be >= 0 for the rule to apply.
            now: Current time. Defaults to system UTC now.

        Returns:
            BestDayCheckResult with allowed/reason/projected numbers.
        """
        if not self.is_active:
            return BestDayCheckResult(
                allowed=True,
                reason="rule inactive (challenge phase)",
                today_pnl=self._today_pnl,
                cumulative_pnl=self._cumulative_pnl,
                projected_today_share=0.0,
                threshold=self._threshold,
            )

        if planned_profit_dollars <= 0.0:
            return BestDayCheckResult(
                allowed=True,
                reason="non-positive planned profit (rule is profit-only)",
                today_pnl=self._today_pnl,
                cumulative_pnl=self._cumulative_pnl,
                projected_today_share=0.0,
                threshold=self._threshold,
            )

        if now is None:
            now = datetime.now(timezone.utc)
        self._maybe_reset(now)

        if self._cumulative_pnl <= 0.0:
            return BestDayCheckResult(
                allowed=True,
                reason="cumulative P/L non-positive (no profit concentration yet)",
                today_pnl=self._today_pnl,
                cumulative_pnl=self._cumulative_pnl,
                projected_today_share=0.0,
                threshold=self._threshold,
            )

        projected_today = self._today_pnl + planned_profit_dollars
        share = projected_today / self._cumulative_pnl

        if share > self._threshold:
            return BestDayCheckResult(
                allowed=False,
                reason=(
                    f"Best Day Rule: projected today share {share:.1%} would exceed threshold {self._threshold:.0%}"
                ),
                today_pnl=self._today_pnl,
                cumulative_pnl=self._cumulative_pnl,
                projected_today_share=share,
                threshold=self._threshold,
            )

        return BestDayCheckResult(
            allowed=True,
            reason="within threshold",
            today_pnl=self._today_pnl,
            cumulative_pnl=self._cumulative_pnl,
            projected_today_share=share,
            threshold=self._threshold,
        )

    def status(self, now: Optional[datetime] = None) -> dict:
        """Return current state for logging / UI.

        If `now` is provided, also trigger a daily reset if the local date
        has advanced since the last reset. If `now` is None, return the
        tracker's state as-is — no reset is performed. This avoids
        retroactively wiping today's P/L when called from a logging path
        where the "now" is irrelevant.

        Keys: is_active, account_phase, threshold, today_pnl,
        cumulative_pnl, today_share, remaining_today_headroom_dollars,
        last_reset_date.
        """
        if now is not None and self.is_active:
            self._maybe_reset(now)

        if self._cumulative_pnl > 0:
            today_share = self._today_pnl / self._cumulative_pnl
            headroom = max(
                0.0,
                self._threshold * self._cumulative_pnl - self._today_pnl,
            )
        else:
            today_share = 0.0
            headroom = float("inf")  # unlimited until first profit

        return {
            "is_active": self.is_active,
            "account_phase": self._account_phase,
            "threshold": self._threshold,
            "today_pnl": self._today_pnl,
            "cumulative_pnl": self._cumulative_pnl,
            "today_share": today_share,
            "remaining_today_headroom_dollars": headroom,
            "last_reset_date": self._last_reset_date,
        }


__all__ = [
    "BestDayRuleTracker",
    "BestDayCheckResult",
    "DEFAULT_THRESHOLD",
    "DEFAULT_RESET_TZ_OFFSET_HOURS",
    "VALID_PHASES",
]
