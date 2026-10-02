"""FTMO Guard — enforces FTMO challenge rules for funded account trading.

Implements three core FTMO protections:

1. **Daily loss limit** — freeze trading when daily loss reaches 4% of
   starting balance.  Resets at 00:00 America/Toronto.
2. **Max concurrent positions** — reject new positions beyond 3.
3. **Drawdown breaker** — reduce new position size by 50% at 8% drawdown,
   freeze all trading at 9% drawdown.

When a threshold is breached, :class:`FTMOGuard` calls the existing
:class:`~adapters.ctrader.kill_switch.KillSwitchManager` to activate
a global freeze or kill.

All methods are synchronous and thread-safe via an internal ``RLock``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Optional, Protocol
from zoneinfo import ZoneInfo

from risk.ftmo_params import (
    FTMO_BEST_DAY_CAP_PCT,
    FTMO_DAILY_DD_LIMIT_PCT,
    FTMO_MAX_CONCURRENT_POSITIONS,
    FTMO_TOTAL_DD_LIMIT_PCT,
)

logger = logging.getLogger("ayumi.risk.ftmo_guard")

# ── Challenge type ───────────────────────────────────────────────────────────

ChallengeType = Literal["1-step", "2-step"]

#: Per-challenge daily loss percentages (fraction of starting balance).
#: 1-Step: 3% (FTMO spec, matches :data:`FTMO_DAILY_DD_LIMIT_PCT`).
#: 2-Step: 5% (FTMO spec).
#: TODO: centralize 2-step value in ``risk.ftmo_params`` (out of scope for this card —
#: ftmo_params.py not in allowed_files).
_CHALLENGE_DAILY_LOSS_PCT: dict[ChallengeType, float] = {
    "1-step": FTMO_DAILY_DD_LIMIT_PCT * 100,  # 3.0%
    "2-step": 5.0,
}

# ── Trading-day timezone helpers ─────────────────────────────────────────────

# Per Craig decision (Jul 17, 2026): both engine and risk guard reset at
# 00:00 America/Toronto (midnight Eastern).  DST is handled automatically
# by ZoneInfo.
_TRADING_TZ = ZoneInfo("America/Toronto")


def _trading_date(now: Optional[datetime] = None) -> str:
    """Return the current America/Toronto date as ``YYYY-MM-DD`` string."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now.astimezone(_TRADING_TZ).strftime("%Y-%m-%d")


def _toronto_midnight_utc(now: Optional[datetime] = None) -> datetime:
    """Return the next America/Toronto midnight as a UTC datetime."""
    if now is None:
        now = datetime.now(timezone.utc)
    now_tz = now.astimezone(_TRADING_TZ)
    next_midnight = (now_tz + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_midnight.astimezone(timezone.utc)


# ── Enums ────────────────────────────────────────────────────────────────────


class FTMOBreachType(str, Enum):
    """Type of FTMO rule breach."""

    DAILY_LOSS = "daily_loss"
    POSITION_LIMIT = "position_limit"
    DD_REDUCE = "dd_reduce"
    DD_FREEZE = "dd_freeze"
    BEST_DAY_RULE = "best_day_rule"  # Phase 0: FTMO 1-Step best-day rule (50% cap)
    TRAILING_DD_FLOOR = "trailing_dd_floor"  # Max-loss floor breach (trailing or static)


class FTMOAction(str, Enum):
    """Action level dictated by FTMO guard."""

    ALLOW = "allow"  # Normal trading
    REDUCE_50 = "reduce_50"  # Reduce new position size by 50%
    FREEZE = "freeze"  # No new positions, hold existing
    KILL = "kill"  # Close all positions


# ── Protocol for kill_switch integration ─────────────────────────────────────


class KillSwitchLike(Protocol):
    """Protocol for kill switch objects (duck-typed)."""

    def activate_global_freeze(self, reason: str, triggered_by: str) -> None: ...
    def activate_global_kill(self, reason: str, triggered_by: str, close_positions: bool = True) -> None: ...
    def is_active(self) -> bool: ...


# ── State ────────────────────────────────────────────────────────────────────


@dataclass
class FTMOState:
    """Serializable FTMO guard state."""

    starting_balance: float = 0.0
    peak_balance: float = 0.0
    current_balance: float = 0.0
    daily_loss_pct: float = 0.0
    daily_loss_date: Optional[str] = None  # Trading date for the current daily loss tracking
    open_position_count: int = 0
    current_dd_pct: float = 0.0
    action_level: str = FTMOAction.ALLOW.value
    breach_history: list = field(default_factory=list)
    # Phase 0: Best-day rule tracking (FTMO 1-Step: best day ≤ 50% of total positive-days profit)
    daily_pnl: float = 0.0  # P&L for current trading day
    daily_pnl_date: Optional[str] = None  # Trading date for daily_pnl tracking
    daily_pnl_history: list = field(default_factory=list)  # [{date, pnl}] for completed days
    # Trailing-DD floor tracking (enabled via FTMOGuard(trailing_dd=True))
    challenge_type: str = "1-step"  # FTMO challenge type
    highest_midnight_balance: float = 0.0  # Peak midnight balance for trailing floor

    def to_dict(self) -> dict:
        return {
            "starting_balance": self.starting_balance,
            "peak_balance": self.peak_balance,
            "current_balance": self.current_balance,
            "daily_loss_pct": self.daily_loss_pct,
            "daily_loss_date": self.daily_loss_date,
            "open_position_count": self.open_position_count,
            "current_dd_pct": self.current_dd_pct,
            "action_level": self.action_level,
            "breach_history": list(self.breach_history),
            "daily_pnl": self.daily_pnl,
            "daily_pnl_date": self.daily_pnl_date,
            "daily_pnl_history": list(self.daily_pnl_history),
            "challenge_type": self.challenge_type,
            "highest_midnight_balance": self.highest_midnight_balance,
        }


# ── FTMOGuard ────────────────────────────────────────────────────────────────


class FTMOGuard:
    """FTMO rule enforcement guard.

    Monitors account metrics and enforces FTMO challenge rules:
    daily loss limit, concurrent position cap, drawdown breaker,
    best-day rule, and optional trailing-DD max-loss floor.

    Call :meth:`update` after every fill or periodic check to keep
    state current.  Call :meth:`should_allow_new_position` before
    opening any new position.

    Args:
        kill_switch: KillSwitchManager (or compatible) for global activation.
        starting_balance: Account starting balance (for daily loss % calc).
        max_daily_loss_pct: Daily loss threshold. If ``None``, derived from
            ``challenge_type`` (3% for 1-step, 5% for 2-step).
        max_concurrent_positions: Max simultaneous positions (default 3).
        dd_reduce_pct: Drawdown % that triggers size reduction (default 8.0).
        dd_freeze_pct: Drawdown % that triggers freeze (default 9.0).
        best_day_cap_pct: Best-day rule cap (default 0.50 = 50%).
        challenge_type: FTMO challenge type (``'1-step'`` or ``'2-step'``).
        trailing_dd: When ``True``, enforce the FTMO max-loss floor (10%).
            For 1-step, the floor trails the highest midnight balance.
            For 2-step, the floor is static from the initial balance.
    """

    # Default FTMO parameters — imported from canonical source (risk.ftmo_params)
    # ftmo_guard uses percentage points (0-100 scale) for daily_loss_pct,
    # so we convert the fraction (0.03) to percentage points (3.0).
    DEFAULT_MAX_DAILY_LOSS_PCT = FTMO_DAILY_DD_LIMIT_PCT * 100  # 3.0%
    DEFAULT_MAX_POSITIONS = FTMO_MAX_CONCURRENT_POSITIONS  # 3
    DEFAULT_DD_REDUCE_PCT = 8.0
    DEFAULT_DD_FREEZE_PCT = 9.0
    # Phase 0: Best-day rule (FTMO 1-Step: best day's profit ≤ 50% of total positive-days profit)
    DEFAULT_BEST_DAY_CAP_PCT = FTMO_BEST_DAY_CAP_PCT  # 0.50
    # Max daily P&L history to retain (days)
    MAX_PNL_HISTORY = 60

    def __init__(
        self,
        kill_switch: Optional[KillSwitchLike] = None,
        starting_balance: float = 10000.0,
        max_daily_loss_pct: Optional[float] = None,
        max_concurrent_positions: int = DEFAULT_MAX_POSITIONS,
        dd_reduce_pct: float = DEFAULT_DD_REDUCE_PCT,
        dd_freeze_pct: float = DEFAULT_DD_FREEZE_PCT,
        best_day_cap_pct: float = DEFAULT_BEST_DAY_CAP_PCT,
        challenge_type: ChallengeType = "1-step",
        trailing_dd: bool = False,
    ):
        if dd_reduce_pct >= dd_freeze_pct:
            raise ValueError(f"dd_reduce_pct ({dd_reduce_pct}) must be < dd_freeze_pct ({dd_freeze_pct})")

        # Derive daily loss pct from challenge type if not explicitly provided
        if max_daily_loss_pct is not None:
            if max_daily_loss_pct <= 0:
                raise ValueError("max_daily_loss_pct must be positive")
            resolved_daily_loss_pct = max_daily_loss_pct
        else:
            resolved_daily_loss_pct = _CHALLENGE_DAILY_LOSS_PCT[challenge_type]

        self._lock = threading.RLock()
        self._kill_switch = kill_switch
        self._max_daily_loss_pct = resolved_daily_loss_pct
        self._max_positions = max_concurrent_positions
        self._dd_reduce_pct = dd_reduce_pct
        self._dd_freeze_pct = dd_freeze_pct
        self._best_day_cap_pct = best_day_cap_pct
        self._challenge_type: ChallengeType = challenge_type
        self._trailing_dd_enabled = trailing_dd

        self._state = FTMOState(
            starting_balance=starting_balance,
            peak_balance=starting_balance,
            current_balance=starting_balance,
            daily_loss_date=_trading_date(),
            daily_pnl_date=_trading_date(),
            challenge_type=challenge_type,
            highest_midnight_balance=starting_balance,
        )

    # ── Public API: State queries ──────────────────────────────────────────

    @property
    def state(self) -> FTMOState:
        """Current FTMO state (thread-safe copy)."""
        with self._lock:
            return FTMOState(**self._state.to_dict())

    @property
    def action_level(self) -> FTMOAction:
        """Current action level (ALLOW / REDUCE_50 / FREEZE / KILL)."""
        with self._lock:
            return FTMOAction(self._state.action_level)

    @property
    def daily_loss_pct(self) -> float:
        """Current daily loss as percentage of starting balance."""
        with self._lock:
            return self._state.daily_loss_pct

    @property
    def current_dd_pct(self) -> float:
        """Current drawdown percentage from peak."""
        with self._lock:
            return self._state.current_dd_pct

    @property
    def open_position_count(self) -> int:
        """Number of currently open positions."""
        with self._lock:
            return self._state.open_position_count

    def get_status(self) -> dict:
        """Full status dict for external consumption / logging."""
        with self._lock:
            return self._state.to_dict()

    # ── Public API: Updates ────────────────────────────────────────────────

    def update(
        self,
        current_balance: float,
        open_positions: int,
        now: Optional[datetime] = None,
    ) -> FTMOAction:
        """Update account metrics and check all FTMO rules.

        This is the primary entry point — call after every fill or
        during periodic risk checks.

        Args:
            current_balance: Current account equity/balance.
            open_positions: Current number of open positions.
            now: Override for current time (testing). Defaults to UTC now.

        Returns:
            Current :class:`FTMOAction` level after all checks.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        with self._lock:
            # ── America/Toronto midnight daily reset ──────────────────
            today_str = _trading_date(now)
            if self._state.daily_loss_date != today_str:
                logger.info(
                    "FTMO daily reset: %s → %s",
                    self._state.daily_loss_date,
                    today_str,
                )
                # Phase 0: Roll over daily P&L before resetting
                if self._state.daily_pnl_date is not None and self._state.daily_pnl_date != today_str:
                    self._state.daily_pnl_history.append(
                        {
                            "date": self._state.daily_pnl_date,
                            "pnl": self._state.daily_pnl,
                        }
                    )
                    if len(self._state.daily_pnl_history) > self.MAX_PNL_HISTORY:
                        self._state.daily_pnl_history = self._state.daily_pnl_history[-self.MAX_PNL_HISTORY :]
                    logger.info(
                        "FTMO daily P&L rollover: %s P&L=%.2f",
                        self._state.daily_pnl_date,
                        self._state.daily_pnl,
                    )
                self._state.daily_loss_pct = 0.0
                self._state.daily_loss_date = today_str
                self._state.daily_pnl = 0.0
                self._state.daily_pnl_date = today_str
                # If we were frozen due to daily loss, allow trading again
                if self._state.action_level == FTMOAction.FREEZE.value:
                    self._set_action(FTMOAction.ALLOW, "Daily reset at America/Toronto midnight")

            # ── Update balance metrics ─────────────────────────────────────
            self._state.current_balance = current_balance
            self._state.open_position_count = open_positions

            if current_balance > self._state.peak_balance:
                self._state.peak_balance = current_balance

            # ── Calculate daily loss ───────────────────────────────────────
            daily_loss = self._state.starting_balance - current_balance
            if self._state.starting_balance > 0:
                self._state.daily_loss_pct = max(0.0, (daily_loss / self._state.starting_balance) * 100.0)

            # ── Calculate drawdown from peak ───────────────────────────────
            if self._state.peak_balance > 0:
                dd = (self._state.peak_balance - current_balance) / self._state.peak_balance * 100.0
                self._state.current_dd_pct = max(0.0, dd)
            else:
                self._state.current_dd_pct = 0.0

            # ── Phase 0: Track daily P&L ────────────────────────────────────
            # Daily P&L = change in balance since start of trading day
            # We approximate start-of-day balance as starting_balance - cumulative_daily_pnl
            # For accuracy, the forward test engine should set daily_start_balance explicitly
            if self._state.daily_pnl_date == today_str:
                # Track balance delta within the day
                # On first update of the day, daily_pnl is 0 (reset at rollover)
                # We use current_balance - starting_balance + cumulative losses as daily P&L
                # Simplest: daily_pnl = current_balance - balance_at_start_of_day
                # Since we don't track balance_at_start_of_day separately,
                # we use the balance delta from the first update each day
                pass  # daily_pnl is updated via record_daily_pnl() for closed trades

            # ── Phase 0: Best-day rule check ────────────────────────────────
            # FTMO 1-Step: best day's profit ≤ 50% of total positive-days profit
            best_day_violation = self._check_best_day_rule()
            if best_day_violation:
                self._breach(
                    FTMOBreachType.BEST_DAY_RULE,
                    best_day_violation,
                    FTMOAction.FREEZE,
                )
                return self.action_level

            # ── Check rules (order: most severe first) ─────────────────────

            # Daily loss check
            if self._state.daily_loss_pct >= self._max_daily_loss_pct:
                self._breach(
                    FTMOBreachType.DAILY_LOSS,
                    f"Daily loss {self._state.daily_loss_pct:.2f}% ≥ limit {self._max_daily_loss_pct}%",
                    FTMOAction.FREEZE,
                )
                return self.action_level

            # Trailing-DD floor check (max-loss rule)
            if self._trailing_dd_enabled and self._is_trailing_floor_breached():
                floor = self.compute_floor()
                self._breach(
                    FTMOBreachType.TRAILING_DD_FLOOR,
                    f"Balance {current_balance:.2f} < trailing floor {floor:.2f}",
                    FTMOAction.FREEZE,
                )
                return self.action_level

            # Drawdown freeze check
            if self._state.current_dd_pct >= self._dd_freeze_pct:
                self._breach(
                    FTMOBreachType.DD_FREEZE,
                    f"Drawdown {self._state.current_dd_pct:.2f}% ≥ freeze threshold {self._dd_freeze_pct}%",
                    FTMOAction.FREEZE,
                )
                return self.action_level

            # Drawdown reduce check
            if self._state.current_dd_pct >= self._dd_reduce_pct:
                self._breach(
                    FTMOBreachType.DD_REDUCE,
                    f"Drawdown {self._state.current_dd_pct:.2f}% ≥ reduce threshold {self._dd_reduce_pct}%",
                    FTMOAction.REDUCE_50,
                )
                return self.action_level

            # Position limit check
            if open_positions > self._max_positions:
                self._breach(
                    FTMOBreachType.POSITION_LIMIT,
                    f"Positions {open_positions} > max {self._max_positions}",
                    FTMOAction.FREEZE,
                )
                return self.action_level

            # All clear — recover from reduce/freeze if rules no longer breached
            if self._state.action_level != FTMOAction.ALLOW.value:
                # Daily loss and DD can recover without daily reset
                self._set_action(FTMOAction.ALLOW, "Metrics within FTMO limits")
            return self.action_level

    # ── Public API: Position gating ───────────────────────────────────────

    def should_allow_new_position(self) -> tuple[bool, str]:
        """Gate check for opening a new position.

        Returns:
            Tuple of (allowed: bool, reason: str).
            If allowed with REDUCE_50, caller should halve position size.
        """
        with self._lock:
            level = FTMOAction(self._state.action_level)

            if level == FTMOAction.FREEZE:
                return (
                    False,
                    f"FTMO freeze active: daily_loss={self._state.daily_loss_pct:.2f}%, dd={self._state.current_dd_pct:.2f}%",  # noqa: E501
                )

            if level == FTMOAction.KILL:
                return False, "FTMO kill active — all positions should be closed"

            if self._state.open_position_count >= self._max_positions:
                return (
                    False,
                    f"Position limit reached: {self._state.open_position_count}/{self._max_positions}",
                )

            if level == FTMOAction.REDUCE_50:
                return True, "FTMO reduce mode — halve position size"

            return True, "OK"

    def get_size_multiplier(self) -> float:
        """Return the position size multiplier based on current FTMO state.

        Returns:
            1.0 (normal), 0.5 (reduce mode), or 0.0 (frozen).
        """
        with self._lock:
            level = FTMOAction(self._state.action_level)
            if level in (FTMOAction.FREEZE, FTMOAction.KILL):
                return 0.0
            if level == FTMOAction.REDUCE_50:
                return 0.5
            return 1.0

    # ── Phase 0: Best-day rule API ─────────────────────────────────────

    def record_daily_pnl(self, pnl: float, date: Optional[str] = None) -> None:
        """Record closed-trade P&L for the current (or specified) trading day.

        Called by the forward test engine when a trade closes.
        Accumulates into ``daily_pnl`` for the current day.

        Args:
            pnl: Realized P&L for the closed trade (positive = profit).
            date: Trading date string (YYYY-MM-DD). Defaults to today.
        """
        if date is None:
            date = _trading_date()
        with self._lock:
            if self._state.daily_pnl_date != date:
                # Rollover if date changed without an update() call
                if self._state.daily_pnl_date is not None:
                    self._state.daily_pnl_history.append(
                        {
                            "date": self._state.daily_pnl_date,
                            "pnl": self._state.daily_pnl,
                        }
                    )
                    if len(self._state.daily_pnl_history) > self.MAX_PNL_HISTORY:
                        self._state.daily_pnl_history = self._state.daily_pnl_history[-self.MAX_PNL_HISTORY :]
                self._state.daily_pnl = 0.0
                self._state.daily_pnl_date = date
            self._state.daily_pnl += pnl
            logger.debug(
                "FTMO daily P&L update: %s += %.2f → total %.2f",
                date,
                pnl,
                self._state.daily_pnl,
            )

    def check_best_day_rule(self) -> Optional[str]:
        """Check FTMO 1-Step best-day rule.

        Rule: best single day's profit must not exceed 50% of
        total positive-days profit.

        Returns:
            None if rule is not violated, or a detail string if violated.
        """
        with self._lock:
            return self._check_best_day_rule()

    def _check_best_day_rule(self) -> Optional[str]:
        """Internal best-day rule check (caller holds lock)."""
        # Need at least 2 positive days to evaluate
        positive_days = [d for d in self._state.daily_pnl_history if d.get("pnl", 0) > 0]
        # Include today if positive
        if self._state.daily_pnl > 0:
            positive_days.append(
                {
                    "date": self._state.daily_pnl_date or _trading_date(),
                    "pnl": self._state.daily_pnl,
                }
            )

        if len(positive_days) < 2:
            return None  # Can't violate with <2 positive days

        total_positive = sum(d["pnl"] for d in positive_days)
        best_day = max(d["pnl"] for d in positive_days)
        best_day_ratio = best_day / total_positive if total_positive > 0 else 0.0

        if best_day_ratio > self._best_day_cap_pct:
            return (
                f"Best day profit {best_day:.2f} is {best_day_ratio:.1%} of "
                f"total positive-days profit {total_positive:.2f} "
                f"(cap: {self._best_day_cap_pct:.0%})"
            )
        return None

    # ── Trailing-DD floor API ──────────────────────────────────────────

    @property
    def challenge_type(self) -> ChallengeType:
        """FTMO challenge type ('1-step' or '2-step')."""
        return self._challenge_type

    @property
    def highest_midnight_balance(self) -> float:
        """Highest midnight balance recorded (for trailing floor)."""
        with self._lock:
            return self._state.highest_midnight_balance

    def compute_floor(self) -> float:
        """Absolute balance floor below which the FTMO challenge is failed.

        - **1-step:** ``highest_midnight_balance × (1 - max_loss_pct)`` (trailing).
        - **2-step:** ``starting_balance × (1 - max_loss_pct)`` (static).

        Uses :data:`FTMO_TOTAL_DD_LIMIT_PCT` (10%) as the max-loss fraction.
        """
        with self._lock:
            if self._challenge_type == "1-step":
                base = self._state.highest_midnight_balance
            else:
                base = self._state.starting_balance
            return base * (1.0 - FTMO_TOTAL_DD_LIMIT_PCT)

    def record_midnight_balance(self, balance: float) -> None:
        """Record the account balance at America/Toronto midnight.

        For 1-step challenge, updates the trailing highest when balance
        exceeds the previous peak.  This should be called once per trading
        day at midnight to checkpoint the floor.

        Args:
            balance: Account balance at midnight.
        """
        with self._lock:
            if balance > self._state.highest_midnight_balance:
                old_floor = self.compute_floor()
                self._state.highest_midnight_balance = balance
                new_floor = self.compute_floor()
                if new_floor > old_floor:
                    logger.info(
                        "FTMO trailing floor raised: %.2f → %.2f (balance %.2f)",
                        old_floor,
                        new_floor,
                        balance,
                    )

    def _is_trailing_floor_breached(self) -> bool:
        """Check if current balance is below the trailing/static floor."""
        with self._lock:
            return self._state.current_balance < self.compute_floor()

    # ── Internal ───────────────────────────────────────────────────────────

    def _breach(
        self,
        breach_type: FTMOBreachType,
        detail: str,
        action: FTMOAction,
    ) -> None:
        """Record a breach and activate kill switch if needed."""
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": breach_type.value,
            "detail": detail,
            "action": action.value,
            "daily_loss_pct": self._state.daily_loss_pct,
            "dd_pct": self._state.current_dd_pct,
            "open_positions": self._state.open_position_count,
        }
        self._state.breach_history.append(event)
        self._set_action(action, detail)

        logger.warning("FTMO BREACH [%s]: %s → action=%s", breach_type.value, detail, action.value)

        # Activate kill switch for freeze/kill actions
        if self._kill_switch is not None:
            if action == FTMOAction.FREEZE:
                self._kill_switch.activate_global_freeze(
                    reason=f"FTMO: {detail}",
                    triggered_by="ftmo_guard",
                )
            elif action == FTMOAction.KILL:
                self._kill_switch.activate_global_kill(
                    reason=f"FTMO: {detail}",
                    triggered_by="ftmo_guard",
                    close_positions=True,
                )

    def _set_action(self, action: FTMOAction, reason: str) -> None:
        """Set the current action level (does not escalate downward)."""
        old = FTMOAction(self._state.action_level)
        # Only escalate or recover, never silently downgrade freeze→reduce
        if action == FTMOAction.ALLOW:
            # Recovery — allowed from reduce/freeze if metrics improved
            logger.info("FTMO action recovered: %s → ALLOW (%s)", old.value, reason)
        elif old == FTMOAction.FREEZE and action == FTMOAction.REDUCE_50:
            # Don't downgrade freeze to reduce — freeze is more severe
            return
        elif old == FTMOAction.KILL and action != FTMOAction.KILL:
            # Don't downgrade kill to anything
            return

        self._state.action_level = action.value
