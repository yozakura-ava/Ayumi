"""FTMO Execution Guardrail Engine — pre-trade risk checks.

This module implements :class:`GuardrailEngine`, a synchronous pre-trade
risk gate that enforces the **FTMO 1-Step Standard** rule set before any
new order is submitted to the broker.

LOCKED FTMO 1-STEP STANDARD PARAMETERS
--------------------------------------
The following limits are HARD-CODED — they cannot be overridden via the
``config`` argument of :class:`GuardrailEngine` (raising ``ValueError`` if
attempted).  See Craig's FTMO directive 2026-07-13.

* **Account size:** $10,000 starting balance
* **Daily DD limit:** 3% of starting balance ($300)
* **Total DD limit:** 10% of starting balance ($1,000)

ADJUSTABLE LIMITS (configurable per ``GuardrailEngine(config=...)``)
--------------------------------------------------------------------
* ``max_concurrent_positions`` (default 3)
* ``per_trade_risk_pct``       (default 0.005 = 0.5%)
* ``slippage_threshold_pips``  (default 3.0)
* ``daily_dd_scale_threshold`` (default 0.015 = 1.5% → halve size)
* ``daily_dd_stop_threshold``  (default 0.025 = 2.5% → block all new entries)
* ``total_dd_stop_threshold``  (default 0.08  = 8%   → block + CRITICAL alert)
* ``blackout_window_minutes``  (default ±30 minutes around news events)

KILL SWITCH
-----------
``GuardrailEngine.kill_switch()`` is a **separate** mechanism from the
DD-limit rules.  When activated it blocks ALL new entries immediately.
Only an explicit ``reset_kill_switch()`` clears it — there is no automatic
reset from drawdown recovery or daily reset.

The engine is deliberately broker-agnostic (no cTrader dependency) so it
can be exercised in unit tests with no I/O.  The caller is responsible
for translating ``OrderRequest`` into a broker-specific message.

Usage
-----

    engine = GuardrailEngine(
        starting_balance=10_000.0,
        config=GuardrailConfig(),
        blackout_windows=news_blackouts,
    )

    allowed, reason, adjusted_size = engine.check_order(order)

    if allowed:
        broker.send(order, adjusted_size or order.size)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

logger = logging.getLogger("ayumi.risk.engine")


# ─────────────────────────────────────────────────────────────────────────────
# LOCKED FTMO 1-STEP STANDARD LIMITS — imported from canonical source
# ─────────────────────────────────────────────────────────────────────────────
# All FTMO parameter constants live in :mod:`risk.ftmo_params` (single source
# of truth).  Re-exported here for backward compatibility with existing imports.
# Any attempt to override these via GuardrailConfig raises ValueError at
# construction time.  See :meth:`GuardrailEngine.__post_init__`.
# ─────────────────────────────────────────────────────────────────────────────

from risk.ftmo_params import (
    FTMO_DAILY_DD_LIMIT_PCT,
    FTMO_REFERENCE_ACCOUNT_SIZE,
    FTMO_TOTAL_DD_LIMIT_PCT,
)


# ─────────────────────────────────────────────────────────────────────────────
# Rejection reason codes
# ─────────────────────────────────────────────────────────────────────────────
class RejectReason(str, Enum):
    """Reason an order was rejected by the guardrail engine."""

    KILL_SWITCH_ACTIVE = "kill_switch_active"
    BLACKOUT_WINDOW = "blackout_window"
    TOTAL_DD_BREACH = "total_dd_breach"
    DAILY_DD_STOP = "daily_dd_stop"
    MAX_CONCURRENT = "max_concurrent_positions"
    PER_TRADE_RISK = "per_trade_risk_exceeded"
    SLIPPAGE = "slippage_threshold_exceeded"
    INVALID_ORDER = "invalid_order"


# ─────────────────────────────────────────────────────────────────────────────
# Configurable parameters (everything except the two LOCKED FTMO constants)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GuardrailConfig:
    """Adjustable guardrail parameters.

    The two FTMO hard-coded limits (3% daily DD, 10% total DD) are
    NOT exposed here — they are module-level constants.  Construction of
    :class:`GuardrailEngine` with any attempt to override them raises
    ``ValueError``.
    """

    max_concurrent_positions: int = 3
    per_trade_risk_pct: float = 0.005  # 0.5% of balance
    slippage_threshold_pips: float = 3.0
    daily_dd_scale_threshold: float = 0.015  # 1.5% → halve size
    daily_dd_stop_threshold: float = 0.025  # 2.5% → block entries
    total_dd_stop_threshold: float = 0.08  # 8%   → block + CRITICAL alert

    def __post_init__(self) -> None:
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be >= 1")
        if not (0 < self.per_trade_risk_pct <= 1.0):
            raise ValueError("per_trade_risk_pct must be in (0, 1.0]")
        if self.slippage_threshold_pips < 0:
            raise ValueError("slippage_threshold_pips must be >= 0")
        if not (0 < self.daily_dd_scale_threshold < self.daily_dd_stop_threshold):
            raise ValueError("daily_dd_scale_threshold (1.5%) must be < daily_dd_stop_threshold (2.5%)")
        if not (0 < self.daily_dd_stop_threshold <= FTMO_DAILY_DD_LIMIT_PCT):
            raise ValueError(f"daily_dd_stop_threshold must be in (0, {FTMO_DAILY_DD_LIMIT_PCT}]")
        if not (0 < self.total_dd_stop_threshold <= FTMO_TOTAL_DD_LIMIT_PCT):
            raise ValueError(f"total_dd_stop_threshold must be in (0, {FTMO_TOTAL_DD_LIMIT_PCT}]")


# ─────────────────────────────────────────────────────────────────────────────
# Blackout window for session filter (NFP / FOMC / ECB windows)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BlackoutWindow:
    """A news event blackout window — block entries inside this range."""

    event_name: str
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        if self.start_utc.tzinfo is None or self.end_utc.tzinfo is None:
            raise ValueError("BlackoutWindow datetimes must be timezone-aware (UTC)")
        if self.start_utc >= self.end_utc:
            raise ValueError(f"BlackoutWindow start ({self.start_utc}) must be < end ({self.end_utc})")

    def contains(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return self.start_utc <= ts <= self.end_utc


# ─────────────────────────────────────────────────────────────────────────────
# OrderRequest — what the strategy wants to send
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrderRequest:
    """A pre-trade order request submitted to the guardrail engine.

    ``risk_amount_usd`` is the dollar amount that would be lost if the
    stop-loss is hit.  The strategy layer is responsible for computing
    this from its position-sizing logic.  The engine does NOT compute
    risk from price/SL because lot math varies by broker/symbol and
    would couple the engine to a pricing model.
    """

    symbol: str
    side: str  # "buy" or "sell"
    size: float  # in lots (or whatever unit the broker uses)
    entry_price: float
    stop_loss_price: float
    risk_amount_usd: float
    estimated_slippage_pips: float = 0.0
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.size <= 0:
            raise ValueError(f"size must be > 0, got {self.size}")
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be > 0, got {self.entry_price}")
        if self.stop_loss_price <= 0:
            raise ValueError(f"stop_loss_price must be > 0, got {self.stop_loss_price}")
        if self.risk_amount_usd < 0:
            raise ValueError(f"risk_amount_usd must be >= 0, got {self.risk_amount_usd}")
        if self.estimated_slippage_pips < 0:
            raise ValueError("estimated_slippage_pips must be >= 0")


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CheckResult:
    """Outcome of :meth:`GuardrailEngine.check_order`."""

    allowed: bool
    reason: str
    adjusted_size: float | None

    def __iter__(self):  # tuple unpacking compatibility
        yield self.allowed
        yield self.reason
        yield self.adjusted_size


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────
class GuardrailEngine:
    """Pre-trade risk gate enforcing FTMO 1-Step Standard rules.

    The engine tracks account state (daily P&L, total P&L, open positions)
    and exposes :meth:`check_order` which returns a 3-tuple of
    ``(allowed: bool, reason: str, adjusted_size: float | None)``.
    ``adjusted_size`` is the size the broker should use — ``None`` means
    use the requested size verbatim; a number means the engine scaled it
    down (e.g. drawdown halving).

    Thread safety: simple ``RLock`` around state mutations.  The engine
    is expected to be called from a single execution thread, but the lock
    makes it safe against accidental concurrent access.
    """

    def __init__(
        self,
        starting_balance: float,
        config: GuardrailConfig | None = None,
        blackout_windows: Iterable[BlackoutWindow] | None = None,
        clock: "callable[[], datetime] | None" = None,
    ) -> None:
        import threading

        self._lock = threading.RLock()

        if starting_balance <= 0:
            raise ValueError(f"starting_balance must be > 0, got {starting_balance}")

        # ── LOCKED FTMO constants (intentionally no override path) ─────
        # These are NOT exposed via GuardrailConfig.  We re-declare them
        # as instance attributes for convenient access in check_order,
        # but they cannot be changed after construction.
        self._daily_dd_limit_pct: float = FTMO_DAILY_DD_LIMIT_PCT
        self._total_dd_limit_pct: float = FTMO_TOTAL_DD_LIMIT_PCT

        self._config = config or GuardrailConfig()
        self._starting_balance: float = starting_balance
        self._current_balance: float = starting_balance
        self._peak_balance: float = starting_balance

        self._daily_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._open_positions: int = 0
        self._daily_trade_count: int = 0

        self._kill_switch_active: bool = False
        self._kill_switch_reason: str | None = None

        # News blackouts (NFP/FOMC/ECB windows) — caller provides.
        self._blackout_windows: list[BlackoutWindow] = list(blackout_windows or [])

        # Injectable clock for deterministic tests.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        # Initialize _current_date from the clock so that the first
        # _maybe_reset_daily call doesn't wipe a non-zero starting _daily_pnl
        # (which can occur in tests that seed state directly).
        self._current_date: str = self._clock().strftime("%Y-%m-%d")

    # ── Public properties ────────────────────────────────────────────────
    @property
    def config(self) -> GuardrailConfig:
        return self._config

    @property
    def starting_balance(self) -> float:
        return self._starting_balance

    @property
    def current_balance(self) -> float:
        return self._current_balance

    @property
    def peak_balance(self) -> float:
        return self._peak_balance

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def total_pnl(self) -> float:
        return self._total_pnl

    @property
    def open_positions(self) -> int:
        return self._open_positions

    @property
    def daily_trade_count(self) -> int:
        return self._daily_trade_count

    @property
    def is_killed(self) -> bool:
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> str | None:
        return self._kill_switch_reason

    @property
    def daily_dd_pct(self) -> float:
        """Current daily DD as a fraction of starting balance."""
        if self._starting_balance <= 0:
            return 0.0
        return max(0.0, -self._daily_pnl) / self._starting_balance

    @property
    def total_dd_pct(self) -> float:
        """Current total DD as a fraction of starting balance (peak → now)."""
        if self._peak_balance <= 0:
            return 0.0
        drawdown = self._peak_balance - self._current_balance
        return max(0.0, drawdown) / self._peak_balance

    # ── Override-protection (Hard Rule defense) ──────────────────────────
    @property
    def daily_dd_limit_pct(self) -> float:
        """LOCKED FTMO 1-Step Standard daily DD limit (read-only)."""
        return self._daily_dd_limit_pct

    @property
    def total_dd_limit_pct(self) -> float:
        """LOCKED FTMO 1-Step Standard total DD limit (read-only)."""
        return self._total_dd_limit_pct

    # ── State update ─────────────────────────────────────────────────────
    def update_state(
        self,
        position_closed: bool,
        pnl: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Update engine state after a position closes.

        Parameters
        ----------
        position_closed
            If ``True``, decrement ``open_positions``.
        pnl
            Realized P&L for the trade (positive = win, negative = loss).
        timestamp
            Time of the close; defaults to the engine's clock.  Used to
            drive the daily reset boundary.
        """
        with self._lock:
            ts = timestamp or self._clock()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            self._maybe_reset_daily(ts)

            self._daily_pnl += pnl
            self._total_pnl += pnl
            self._current_balance += pnl

            if position_closed and self._open_positions > 0:
                self._open_positions -= 1

            if self._current_balance > self._peak_balance:
                self._peak_balance = self._current_balance

            logger.info(
                "guardrail.update_state pnl=%.2f daily_pnl=%.2f total_pnl=%.2f open_positions=%d",
                pnl,
                self._daily_pnl,
                self._total_pnl,
                self._open_positions,
            )

    def register_open_position(self) -> None:
        """Register that a new position was opened (called by execution layer)."""
        with self._lock:
            self._open_positions += 1
            self._daily_trade_count += 1
            logger.info(
                "guardrail.position_opened open_positions=%d daily_trades=%d",
                self._open_positions,
                self._daily_trade_count,
            )

    def reset_daily(self, timestamp: datetime | None = None) -> None:
        """Zero the daily P&L counter and trade count.

        Idempotent.  Called automatically by :meth:`check_order` /
        :meth:`update_state` on UTC date rollover.
        """
        with self._lock:
            ts = timestamp or self._clock()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            self._daily_pnl = 0.0
            self._daily_trade_count = 0
            self._current_date = ts.strftime("%Y-%m-%d")
            logger.info(
                "guardrail.daily_reset date=%s starting_balance=%.2f",
                self._current_date,
                self._starting_balance,
            )

    def _seed_daily_pnl(self, pnl: float) -> None:
        """Seed daily P&L directly (used by tests and recovery flows).

        Sets ``_daily_pnl`` to ``pnl`` and adjusts ``_current_balance`` /
        ``_total_pnl`` / ``_peak_balance`` to keep the public-facing
        computed properties consistent.  No logging — internal helper.
        """
        with self._lock:
            delta = pnl - self._daily_pnl
            self._daily_pnl = pnl
            self._total_pnl += delta
            self._current_balance += delta
            if self._current_balance > self._peak_balance:
                self._peak_balance = self._current_balance

    # ── Kill switch ──────────────────────────────────────────────────────
    def kill_switch(self, reason: str = "manual") -> None:
        """Activate the kill switch — blocks ALL new entries.

        The kill switch is a separate mechanism from the DD-limit rules.
        Only :meth:`reset_kill_switch` can clear it.
        """
        with self._lock:
            self._kill_switch_active = True
            self._kill_switch_reason = reason
            logger.critical(
                "guardrail.KILL_SWITCH_ACTIVATED reason=%s — all new entries blocked",
                reason,
            )

    def reset_kill_switch(self, reason: str = "manual_reset") -> None:
        """Clear the kill switch.  Must be called explicitly."""
        with self._lock:
            if not self._kill_switch_active:
                return
            self._kill_switch_active = False
            self._kill_switch_reason = None
            logger.warning(
                "guardrail.KILL_SWITCH_RESET reason=%s — trading resumed",
                reason,
            )

    # ── Blackout windows (session filter) ────────────────────────────────
    def add_blackout_window(self, window: BlackoutWindow) -> None:
        """Add a news blackout window (e.g. NFP/FOMC/ECB ±30 min)."""
        with self._lock:
            self._blackout_windows.append(window)
            logger.info(
                "guardrail.blackout_added event=%s window=%s..%s",
                window.event_name,
                window.start_utc.isoformat(),
                window.end_utc.isoformat(),
            )

    def clear_blackout_windows(self) -> None:
        """Remove all blackout windows."""
        with self._lock:
            self._blackout_windows.clear()

    def is_in_blackout(self, timestamp: datetime | None = None) -> bool:
        """Return True if ``timestamp`` (default: now) falls inside any blackout."""
        ts = timestamp or self._clock()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return any(w.contains(ts) for w in self._blackout_windows)

    # ── Core pre-trade check ─────────────────────────────────────────────
    def check_order(
        self,
        order: OrderRequest,
        timestamp: datetime | None = None,
    ) -> CheckResult:
        """Pre-trade risk check.

        Returns ``(allowed, reason, adjusted_size)``.  If ``allowed`` is
        ``True``, the order is permitted (possibly with a downsized
        ``adjusted_size``).  If ``allowed`` is ``False``, ``reason``
        carries the :class:`RejectReason` code and ``adjusted_size`` is
        ``None``.
        """
        ts = timestamp or order.timestamp or self._clock()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        with self._lock:
            self._maybe_reset_daily(ts)

            # 1) Kill switch — fastest, most critical path.
            if self._kill_switch_active:
                msg = f"kill_switch active (reason={self._kill_switch_reason!r}); rejecting all new entries"
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.KILL_SWITCH_ACTIVE.value,
                    adjusted_size=None,
                )

            # 2) Session filter — block inside NFP/FOMC/ECB windows.
            if self._is_in_blackout_at(ts):
                window = self._blackout_at(ts)
                name = window.event_name if window else "unknown"
                msg = f"inside news blackout window ({name}) at {ts.isoformat()}"
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.BLACKOUT_WINDOW.value,
                    adjusted_size=None,
                )

            # 3) Total DD hard limit (10%) — absolute account-level breach.
            total_dd = self.total_dd_pct
            if total_dd > self._total_dd_limit_pct:
                msg = f"total DD {total_dd:.2%} exceeds FTMO limit {self._total_dd_limit_pct:.2%}"
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.TOTAL_DD_BREACH.value,
                    adjusted_size=None,
                )

            # 4) Total DD stop threshold (8%) — block + CRITICAL alert.
            if total_dd > self._config.total_dd_stop_threshold:
                msg = (
                    f"total DD {total_dd:.2%} exceeds stop threshold "
                    f"{self._config.total_dd_stop_threshold:.2%} — "
                    "BLOCKING + CRITICAL ALERT"
                )
                logger.critical("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.TOTAL_DD_BREACH.value,
                    adjusted_size=None,
                )

            # 5) Daily DD stop threshold (2.5%) — no new entries for the day.
            daily_dd = self.daily_dd_pct
            if daily_dd > self._config.daily_dd_stop_threshold:
                msg = (
                    f"daily DD {daily_dd:.2%} exceeds stop threshold "
                    f"{self._config.daily_dd_stop_threshold:.2%} — "
                    "no new entries today"
                )
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.DAILY_DD_STOP.value,
                    adjusted_size=None,
                )

            # 6) Max concurrent positions.
            if self._open_positions >= self._config.max_concurrent_positions:
                msg = (
                    f"max concurrent positions reached ({self._open_positions}/{self._config.max_concurrent_positions})"
                )
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.MAX_CONCURRENT.value,
                    adjusted_size=None,
                )

            # 7) Slippage threshold.
            if order.estimated_slippage_pips > self._config.slippage_threshold_pips:
                msg = (
                    f"estimated slippage {order.estimated_slippage_pips:.2f} pips "
                    f"exceeds threshold {self._config.slippage_threshold_pips:.2f}"
                )
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.SLIPPAGE.value,
                    adjusted_size=None,
                )

            # 8) Per-trade risk — check absolute amount vs balance.
            per_trade_max_usd = self._current_balance * self._config.per_trade_risk_pct
            if order.risk_amount_usd > per_trade_max_usd:
                msg = (
                    f"trade risk ${order.risk_amount_usd:.2f} exceeds per-trade cap "
                    f"${per_trade_max_usd:.2f} "
                    f"({self._config.per_trade_risk_pct:.2%} of balance)"
                )
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.PER_TRADE_RISK.value,
                    adjusted_size=None,
                )

            # 9) Projected daily DD: current daily loss + trade risk vs 3% limit.
            projected_daily_dd_pct = self.daily_dd_pct + (order.risk_amount_usd / self._starting_balance)
            # Use a small epsilon to avoid floating-point boundary false rejects.
            # At exactly 3.0% projected DD, we are AT the limit, not over it.
            _EPS = 1e-9
            if projected_daily_dd_pct > self._daily_dd_limit_pct + _EPS:
                msg = (
                    f"projected daily DD {projected_daily_dd_pct:.2%} would exceed "
                    f"FTMO limit {self._daily_dd_limit_pct:.2%} "
                    f"(current {self.daily_dd_pct:.2%} + risk "
                    f"{order.risk_amount_usd / self._starting_balance:.2%})"
                )
                logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                return CheckResult(
                    allowed=False,
                    reason=RejectReason.DAILY_DD_STOP.value,
                    adjusted_size=None,
                )

            # 10) Drawdown scaling — at >1.5% daily DD, halve position size.
            adjusted_size: float | None = None
            if daily_dd > self._config.daily_dd_scale_threshold:
                scaled_risk = order.risk_amount_usd / 2.0
                # Scale size proportionally to half the risk.
                scaled_size = order.size * 0.5
                # Make sure the scaled size still respects per-trade cap.
                per_trade_max_usd_scaled = self._current_balance * self._config.per_trade_risk_pct
                if scaled_risk > per_trade_max_usd_scaled:
                    msg = (
                        f"DD scaling (current {daily_dd:.2%} > "
                        f"{self._config.daily_dd_scale_threshold:.2%}) still leaves "
                        f"halved risk ${scaled_risk:.2f} above per-trade cap "
                        f"${per_trade_max_usd_scaled:.2f} — REJECT"
                    )
                    logger.warning("guardrail.REJECT %s reason=%s", order.symbol, msg)
                    return CheckResult(
                        allowed=False,
                        reason=RejectReason.PER_TRADE_RISK.value,
                        adjusted_size=None,
                    )
                adjusted_size = scaled_size
                msg = (
                    f"DD scaling applied: daily DD {daily_dd:.2%} > "
                    f"{self._config.daily_dd_scale_threshold:.2%}; "
                    f"halving size {order.size:.4f} -> {adjusted_size:.4f}"
                )
                logger.info("guardrail.SCALE %s %s", order.symbol, msg)

            # ── All checks passed ──
            logger.info(
                "guardrail.ALLOW %s side=%s size=%.4f risk=$%.2f daily_dd=%.2f%% total_dd=%.2f%% adjusted_size=%s",
                order.symbol,
                order.side,
                order.size,
                order.risk_amount_usd,
                self.daily_dd_pct * 100,
                self.total_dd_pct * 100,
                f"{adjusted_size:.4f}" if adjusted_size is not None else "none",
            )
            return CheckResult(allowed=True, reason="ok", adjusted_size=adjusted_size)

    # ── Internal helpers ─────────────────────────────────────────────────
    def _maybe_reset_daily(self, ts: datetime) -> None:
        today = ts.strftime("%Y-%m-%d")
        if self._current_date != today:
            self._current_date = today
            self._daily_pnl = 0.0
            self._daily_trade_count = 0
            logger.info(
                "guardrail.auto_daily_reset date=%s starting_balance=%.2f",
                today,
                self._starting_balance,
            )

    def _is_in_blackout_at(self, ts: datetime) -> bool:
        return any(w.contains(ts) for w in self._blackout_windows)

    def _blackout_at(self, ts: datetime) -> BlackoutWindow | None:
        for w in self._blackout_windows:
            if w.contains(ts):
                return w
        return None

    # ── Diagnostics ──────────────────────────────────────────────────────
    def get_status(self) -> dict:
        """Return a status snapshot for dashboards / logging."""
        return {
            "starting_balance": self._starting_balance,
            "current_balance": self._current_balance,
            "peak_balance": self._peak_balance,
            "daily_pnl": self._daily_pnl,
            "total_pnl": self._total_pnl,
            "daily_dd_pct": self.daily_dd_pct,
            "total_dd_pct": self.total_dd_pct,
            "open_positions": self._open_positions,
            "daily_trade_count": self._daily_trade_count,
            "kill_switch_active": self._kill_switch_active,
            "kill_switch_reason": self._kill_switch_reason,
            "ftmo_daily_dd_limit_pct": self._daily_dd_limit_pct,
            "ftmo_total_dd_limit_pct": self._total_dd_limit_pct,
            "config": {
                "max_concurrent_positions": self._config.max_concurrent_positions,
                "per_trade_risk_pct": self._config.per_trade_risk_pct,
                "slippage_threshold_pips": self._config.slippage_threshold_pips,
                "daily_dd_scale_threshold": self._config.daily_dd_scale_threshold,
                "daily_dd_stop_threshold": self._config.daily_dd_stop_threshold,
                "total_dd_stop_threshold": self._config.total_dd_stop_threshold,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience constructor
# ─────────────────────────────────────────────────────────────────────────────
def build_ftmo1step_guardrail(
    starting_balance: float = FTMO_REFERENCE_ACCOUNT_SIZE,
    blackout_windows: Iterable[BlackoutWindow] | None = None,
    clock: "callable[[], datetime] | None" = None,
) -> GuardrailEngine:
    """Build a :class:`GuardrailEngine` pre-configured for FTMO 1-Step Standard.

    Uses the locked FTMO 1-Step Standard rules + the configurable
    defaults from :class:`GuardrailConfig` (3 concurrent positions,
    0.5% per-trade risk, 3-pip slippage cap, 1.5%/2.5%/8% scaling).
    """
    return GuardrailEngine(
        starting_balance=starting_balance,
        config=GuardrailConfig(),
        blackout_windows=blackout_windows,
        clock=clock,
    )


__all__ = [
    "FTMO_DAILY_DD_LIMIT_PCT",
    "FTMO_TOTAL_DD_LIMIT_PCT",
    "FTMO_REFERENCE_ACCOUNT_SIZE",
    "RejectReason",
    "GuardrailConfig",
    "BlackoutWindow",
    "OrderRequest",
    "CheckResult",
    "GuardrailEngine",
    "build_ftmo1step_guardrail",
]
