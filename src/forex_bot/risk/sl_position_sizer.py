"""SL-Derived Position Sizer for Ayumi.

Core principle: Strategy decides WHERE the SL goes (wick, EMA, support).
Position sizer decides HOW MUCH to trade so that SL hit = intended risk.

Formula:
    position_size_lots = (account_risk_amount) / (sl_distance_price * pip_value_per_lot)

Guards:
    - Max position size: 1.0 lot
    - Min SL distance: 5 pips
    - Daily risk cap: 3% (recycling — recovers as positions close)
    - Circuit breaker: halts trading for 24h on trigger
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Union

logger = logging.getLogger(__name__)


# Instrument specifications
@dataclass(frozen=True)
class InstrumentSpec:
    """Specification for a trading instrument."""

    symbol: str
    pip_size: float  # Price change per pip (e.g., 0.0001 for EURUSD, 0.1 for XAUUSD)
    lot_size: int  # Units per lot (e.g., 100000 for forex, 100 for XAUUSD)
    pip_value_per_lot: float  # USD value of 1 pip movement per 1 lot

    @property
    def min_sl_pips(self) -> float:
        return 5.0  # Minimum SL distance in pips


# Common instruments
INSTRUMENTS = {
    "EURUSD": InstrumentSpec("EURUSD", pip_size=0.0001, lot_size=100000, pip_value_per_lot=10.0),
    "GBPUSD": InstrumentSpec("GBPUSD", pip_size=0.0001, lot_size=100000, pip_value_per_lot=10.0),
    # NOTE: USDJPY pip value varies with USD/JPY rate (~6.5 at 154.00).
    #       Update at runtime from broker feed for production accuracy.
    "USDJPY": InstrumentSpec("USDJPY", pip_size=0.01, lot_size=100000, pip_value_per_lot=6.5),
    "XAUUSD": InstrumentSpec("XAUUSD", pip_size=0.1, lot_size=100, pip_value_per_lot=10.0),
    "AUDUSD": InstrumentSpec("AUDUSD", pip_size=0.0001, lot_size=100000, pip_value_per_lot=10.0),
    "USDCHF": InstrumentSpec("USDCHF", pip_size=0.0001, lot_size=100000, pip_value_per_lot=10.0),
    "USDCAD": InstrumentSpec("USDCAD", pip_size=0.0001, lot_size=100000, pip_value_per_lot=10.0),
}


@dataclass
class PositionSizeResult:
    """Result of position sizing calculation."""

    lots: float
    risk_amount: float  # USD risk if SL hit
    sl_distance_pips: float
    sl_distance_price: float  # Absolute price distance
    pip_value: float
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""


@dataclass
class CircuitBreakerState:
    """Tracks circuit breaker conditions."""

    # Win rate tracking
    recent_trades: list[bool] = field(default_factory=list)  # True=win, False=loss
    max_trades_tracked: int = 20
    min_win_rate: float = 0.25

    # Drawdown tracking
    daily_dd_pct: float = 0.0
    daily_dd_limit: float = 0.03  # 3%
    account_dd_pct: float = 0.0
    account_dd_limit: float = 0.07  # 7%

    # Halt state
    halted: bool = False
    halted_until: Optional[datetime] = None
    halt_reason: str = ""

    def record_trade(self, win: bool):
        """Record a trade result."""
        self.recent_trades.append(win)
        if len(self.recent_trades) > self.max_trades_tracked:
            self.recent_trades = self.recent_trades[-self.max_trades_tracked :]

    @property
    def win_rate(self) -> float:
        if len(self.recent_trades) == 0:
            return 1.0  # No data = no breaker trigger
        return sum(self.recent_trades) / len(self.recent_trades)

    def check_breakers(self) -> Optional[str]:
        """Check if any circuit breaker should trigger. Returns reason or None."""
        if self.halted:
            if datetime.now(timezone.utc) >= self.halted_until:
                self.halted = False
                self.halted_until = None
                self.halt_reason = ""
                self.daily_dd_pct = 0.0  # Reset daily DD on un-halt
                logger.info("Circuit breaker lifted: %s", self.halt_reason)
            else:
                return f"Trading halted until {self.halted_until.isoformat()}: {self.halt_reason}"

        # Check win rate
        if len(self.recent_trades) >= 10:  # Need minimum sample
            if self.win_rate < self.min_win_rate:
                return (
                    f"Win rate {self.win_rate:.0%} < {self.min_win_rate:.0%} over last {len(self.recent_trades)} trades"  # noqa: E501
                )

        # Check daily drawdown
        if self.daily_dd_pct >= self.daily_dd_limit:
            return f"Daily drawdown {self.daily_dd_pct:.1%} >= {self.daily_dd_limit:.1%}"

        # Check account drawdown
        if self.account_dd_pct >= self.account_dd_limit:
            return f"Account drawdown {self.account_dd_pct:.1%} >= {self.account_dd_limit:.1%}"

        return None

    def halt(self, reason: str, duration_hours: float = 24.0):
        """Trigger circuit breaker."""
        self.halted = True
        self.halted_until = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        self.halt_reason = reason
        logger.warning("CIRCUIT BREAKER TRIGGERED: %s. Halted until %s", reason, self.halted_until)


class SLPositionSizer:
    """Calculate position size based on SL distance and intended risk.

    Phase 5 refactor: risk is tracked per signal_id so double-cancel,
    missing-cancel, and amount-mismatch bugs are impossible.  All public
    mutation methods are protected by ``threading.RLock`` because the
    engine mutates sizer state from several concurrent threads.
    """

    def __init__(
        self,
        account_balance: float,
        risk_per_trade_pct: float = 0.005,  # 0.5%
        max_lot_size: float = 1.0,
        daily_risk_cap_pct: float = 0.03,  # 3%
        min_sl_pips: float = 5.0,
        max_positions_per_symbol: int = 1,
        max_total_open_risk: float = 150.0,
    ):
        self.account_balance = account_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_lot_size = max_lot_size
        self.daily_risk_cap_pct = daily_risk_cap_pct
        self.min_sl_pips = min_sl_pips
        self.max_positions_per_symbol = max_positions_per_symbol
        self.max_total_open_risk = max_total_open_risk

        # Track daily risk used (recycling)
        self._daily_risk_used: float = 0.0
        # Phase 5: per-signal identity-keyed open positions
        self._open_positions: dict[str, float] = {}
        # Phase 5b: track symbol per position for per-symbol limits
        self._position_symbols: dict[str, str] = {}
        self._peak_balance: float = account_balance  # Track peak for account DD

        self.breaker = CircuitBreakerState()
        self._lock = threading.RLock()

    @property
    def _open_risk(self) -> float:
        """Internal scalar view of total open risk (for state persistence compat)."""
        return sum(self._open_positions.values())

    @_open_risk.setter
    def _open_risk(self, value: float) -> None:
        """Restore path only: scalar open_risk is loaded as a single legacy entry.

        Callers that set this directly (e.g. StatePersistence.restore) are
        expected to do so before identity-keyed positions exist.  If the
        legacy scalar value is non-zero we synthesise one ``_legacy_open_risk``
        entry so budget accounting remains consistent.
        """
        if value <= 0.0 and not self._open_positions:
            self._open_positions.clear()
            self._position_symbols.clear()
            return
        legacy_key = "_legacy_open_risk"
        # Replace any existing legacy entry with the restored scalar amount.
        if value > 0.0:
            self._open_positions[legacy_key] = float(value)
        else:
            self._open_positions.pop(legacy_key, None)

    @property
    def open_risk(self) -> float:
        """Total USD risk currently held in open positions."""
        with self._lock:
            return self._open_risk

    @property
    def open_positions(self) -> dict[str, float]:
        """Read-only snapshot of registered signal_id -> risk_amount."""
        with self._lock:
            return dict(self._open_positions)

    @property
    def risk_per_trade(self) -> float:
        """USD amount risked per trade."""
        return self.account_balance * self.risk_per_trade_pct

    @property
    def daily_risk_remaining(self) -> float:
        """USD risk remaining today.

        Recycling logic: open positions consume daily cap, but when they close,
        the risk budget is freed up for new trades. Realized losses stay consumed.
        """
        with self._lock:
            max_daily = self.account_balance * self.daily_risk_cap_pct
            return max(0.0, max_daily - self._daily_risk_used - self._open_risk)

    def _count_positions_for_symbol(self, symbol: str) -> int:
        """Count currently open positions for a given symbol."""
        with self._lock:
            return sum(1 for s in self._position_symbols.values() if s == symbol)

    def update_balance(self, balance: float):
        """Update account balance and track peak."""
        with self._lock:
            if balance > self._peak_balance:
                self._peak_balance = balance
            self.account_balance = balance
            # Update account DD
            if self._peak_balance > 0:
                self.breaker.account_dd_pct = (self._peak_balance - balance) / self._peak_balance

    # ── Phase 5 identity-keyed public API ─────────────────────────────

    def register(self, signal_id: str, risk_amount: float, symbol: str = "") -> None:
        """Register an open position's risk under a unique signal_id.

        Args:
            signal_id: Unique identifier for this position/signal.
            risk_amount: USD risk if SL hit.
            symbol: Trading symbol (e.g. "EURUSD"). Used for per-symbol
                concurrent position limits.
        """
        with self._lock:
            if signal_id in self._open_positions:
                raise ValueError(f"signal_id={signal_id!r} already registered")
            self._open_positions[signal_id] = float(risk_amount)
            if symbol:
                self._position_symbols[signal_id] = symbol

    def cancel(self, signal_id: str) -> None:
        """Cancel the risk reserved for ``signal_id``.

        Idempotent cancel — safe to call on unknown signal_id (race with
        late fills).  If the signal_id was never registered or was already
        cancelled/closed, the call logs a WARNING and returns gracefully
        instead of raising ``KeyError``.  This prevents the timeout →
        cancel → crash loop when a late fill arrives after the execution
        window expires.
        """
        with self._lock:
            if signal_id not in self._open_positions:
                logger.warning(
                    "cancel() called for unknown signal_id=%r — likely a race with a late fill (no-op, continuing)",
                    signal_id,
                )
                return
            del self._open_positions[signal_id]
            self._position_symbols.pop(signal_id, None)

    def close(self, signal_id: str, pnl: float = 0.0) -> None:
        """Close the position for ``signal_id`` and record PnL.

        The position's reserved risk is released (recycling).  A realized
        loss is added to ``_daily_risk_used``.  Raises ``KeyError`` for an
        unknown signal_id.
        """
        with self._lock:
            if signal_id not in self._open_positions:
                raise KeyError(f"Cannot close unknown signal_id={signal_id!r}")
            self._open_positions.pop(signal_id)
            self._position_symbols.pop(signal_id, None)

            win = pnl > 0
            if not win and pnl < 0:
                self._daily_risk_used += abs(pnl)

            self.breaker.record_trade(win)

            # Update account balance and peak from actual P&L
            new_balance = self.account_balance + pnl
            if new_balance > self._peak_balance:
                self._peak_balance = new_balance
            self.account_balance = new_balance
            if self._peak_balance > 0:
                self.breaker.account_dd_pct = (self._peak_balance - self.account_balance) / self._peak_balance

    # ── Legacy scalar API (kept for callers not yet identity-keyed) ───

    def register_open_position(self, risk_amount: float):
        """Track risk of an open position (scalar fallback).

        Generates an internal signal_id so the risk is still identity-keyed.
        """
        with self._lock:
            key = f"_legacy_{id(self)}_{len(self._open_positions)}"
            self.register(key, risk_amount)

    def cancel_position(self, risk_amount: float):
        """Cancel scalar open risk by subtracting from a legacy entry.

        Frees risk budget when a downstream component (e.g. PaperTrader)
        rejects an order after the sizer already registered open risk.
        This legacy variant adjusts the first legacy entry by the requested
        amount; if the entry would go to zero or negative it is removed.
        """
        with self._lock:
            legacy_keys = [k for k in self._open_positions if k.startswith("_legacy_")]
            if legacy_keys:
                key = legacy_keys[0]
                current = self._open_positions[key]
                if current > risk_amount + 1e-9:
                    self._open_positions[key] = current - risk_amount
                    return
                self.cancel(key)
                return
            # No legacy entries: remove any single open position to preserve
            # caller contract (used by tests and older callers).
            if self._open_positions:
                first_key = next(iter(self._open_positions))
                self.cancel(first_key)
                return
            raise KeyError("Cannot cancel position: no open positions (possible double-cancel)")

    def close_position(self, pnl: float, risk_amount: float, win: bool):
        """Handle position close — legacy scalar form.

        Attempts to find a matching identity-keyed entry by amount, otherwise
        closes the first open position so backward-compatible callers keep
        working.
        """
        with self._lock:
            # Try exact match by risk amount
            for key, amount in list(self._open_positions.items()):
                if abs(amount - risk_amount) < 1e-9:
                    self.close(key, pnl)
                    return
            # Fallback: close first open position if any
            if self._open_positions:
                first_key = next(iter(self._open_positions))
                self.close(first_key, pnl)
                return
            raise KeyError("Cannot close position: no open positions matching risk_amount")

    # ── Startup / periodic broker reconciliation ─────────────────────────

    def reconcile_with_broker(
        self,
        broker_positions: Iterable[Any],
    ) -> dict:
        """Nuke-and-rebuild :attr:`_open_positions` from broker truth.

        Why this exists (card 0e0338d4): the in-memory ``_open_positions``
        dict previously accumulated phantom entries from the legacy
        ``_legacy_open_risk`` synthesis path (the ``_open_risk`` setter
        creates a ``_legacy_open_risk`` key when state is restored with a
        non-zero ``open_risk`` scalar) and from ``register_open_position``
        callbacks that did not have a matching broker position. The result
        was ``positions_carried`` counts of 16+ when cTrader only had 4
        actual fills.

        The fix is to clear ``_open_positions`` completely and rebuild it
        from the broker's authoritative open-position list. Lower risk
        than in-place reconciliation because the broker is the source of
        truth — anything not in the broker response does not exist.

        Each rebuilt entry uses the ``seeded_{position_id}`` key so it
        cannot collide with live ``signal_id`` keys registered by
        ``BlendForwardTestRunner.on_signal``. Risk per position is computed
        from the broker-provided entry/SL using the same formula as the
        engine's ``_seed_existing_positions`` (so daily-cap accounting
        matches what the engine would have computed).

        Args:
            broker_positions: An iterable of position-like objects (any
                object exposing ``position_id``, ``symbol``, ``volume``
                or ``volume_lots``, ``entry_price`` or ``price``, and
                ``stop_loss``). Accepts both the
                ``adapters.ctrader.models.Position`` dataclass and the
                ``adapters.ctrader.account_state.Position`` dataclass —
                attribute names are read via ``getattr`` with fallbacks.

        Returns:
            Dict with::

                {
                    "before_count":    int,   # entries before reconcile
                    "after_count":     int,   # entries after reconcile
                    "seeded_count":    int,   # broker positions registered
                    "before_open_risk":float, # USD risk before reconcile
                    "after_open_risk": float, # USD risk after reconcile
                    "removed_count":   int,   # before_count - after_count
                    "diverged":        bool,  # before_count != after_count
                    "missing_position_ids": list[str],  # any that failed
                    "ran_at":          str,   # ISO8601 UTC timestamp
                }

        Thread safety: Acquires ``self._lock``. Safe to call concurrently
        with ``register``/``cancel``/``close`` — they will block briefly
        during the swap.

        Note:
            This method does NOT touch ``_daily_risk_used``. Seeded
            positions were opened on prior days so their reserved risk
            consumes today's cap (via ``open_risk``) but they should not
            be double-counted as today's realised losses.
        """
        # Lazy import to keep ``sl_position_sizer`` importable without
        # the broker/strategy stacks (used by backtests + unit tests).
        from risk.sl_position_sizer import _compute_position_risk_usd  # noqa: F401

        with self._lock:
            before_count = len(self._open_positions)
            before_open_risk = sum(self._open_positions.values())

            # Capture any position_ids we already know about (logging only).
            pre_keys = set(self._open_positions.keys())

            # ── Nuke: drop everything. Broker truth replaces local state. ──
            self._open_positions.clear()
            self._position_symbols.clear()

            # ── Rebuild: register each broker position under a synthetic
            #     ``seeded_{position_id}`` key so live signal_ids cannot
            #     collide.
            seeded_count = 0
            after_open_risk = 0.0
            missing_position_ids: list[str] = []
            for pos in broker_positions:
                position_id = getattr(pos, "position_id", None) or getattr(pos, "positionId", None)
                if position_id is None or str(position_id) == "":
                    missing_position_ids.append("<missing-id>")
                    logger.warning(
                        "reconcile_with_broker: skipping position with no id: %r",
                        pos,
                    )
                    continue

                symbol = getattr(pos, "symbol", None) or getattr(pos, "symbol_name", None) or ""
                # `volume` is lots in ctrader.models.Position; `volume_lots`
                # is lots in account_state.Position. Both acceptable.
                lots = (
                    getattr(pos, "volume", None)
                    if getattr(pos, "volume", None) is not None
                    else getattr(pos, "volume_lots", None)
                ) or 0.0
                try:
                    lots = float(lots)
                except (TypeError, ValueError):
                    lots = 0.0

                entry_price = (
                    getattr(pos, "entry_price", None)
                    if getattr(pos, "entry_price", None) is not None
                    else getattr(pos, "price", None)
                ) or 0.0
                try:
                    entry_price = float(entry_price)
                except (TypeError, ValueError):
                    entry_price = 0.0

                sl_price = getattr(pos, "stop_loss", None)
                if sl_price is None:
                    sl_price = getattr(pos, "sl", None)
                if sl_price is not None:
                    try:
                        sl_price = float(sl_price)
                    except (TypeError, ValueError):
                        sl_price = None

                risk_amount = _compute_position_risk_usd(
                    symbol=str(symbol),
                    entry_price=entry_price,
                    sl_price=sl_price,
                    lots=lots,
                )

                key = f"seeded_{position_id}"
                try:
                    self.register(key, risk_amount, symbol=str(symbol))
                except ValueError:
                    # Duplicate key — extremely unlikely (broker returned
                    # the same position_id twice) but handle it.
                    logger.warning(
                        "reconcile_with_broker: duplicate position_id=%s — skipping second registration",
                        position_id,
                    )
                    missing_position_ids.append(str(position_id))
                    continue

                seeded_count += 1
                after_open_risk += risk_amount

            after_count = len(self._open_positions)
            removed_count = before_count - after_count
            diverged = before_count != after_count

            result = {
                "before_count": before_count,
                "after_count": after_count,
                "seeded_count": seeded_count,
                "before_open_risk": float(before_open_risk),
                "after_open_risk": float(after_open_risk),
                "removed_count": removed_count,
                "diverged": diverged,
                "missing_position_ids": missing_position_ids,
                "ran_at": datetime.now(timezone.utc).isoformat(),
            }

            # Log the reconciliation at WARNING level when divergence is
            # detected (so operators notice phantoms disappearing), INFO
            # otherwise (so the no-op startup is visible in logs).
            log_level = logging.WARNING if diverged else logging.INFO
            logger.log(
                log_level,
                "SLPositionSizer.reconcile_with_broker: "
                "positions %d→%d (seeded=%d, removed=%d), "
                "open_risk $%.2f→$%.2f, diverged=%s, missing=%d, "
                "dropped_keys=%s",
                before_count,
                after_count,
                seeded_count,
                removed_count,
                before_open_risk,
                after_open_risk,
                diverged,
                len(missing_position_ids),
                sorted(pre_keys - set(self._open_positions.keys()))[:20],
            )

            return result

    def reset_daily(self, cet_date: Optional[str] = None):
        """Reset daily counters at America/Toronto midnight (FTMO spec).

        Zeros out ``_daily_risk_used`` so prior-day realized losses stop
        shrinking today's budget. Open positions are intentionally carried
        over — their reserved risk stays in ``_open_risk`` and will only
        be released when the position closes (recycling) or when the
        signal_id is cancelled.

        Args:
            cet_date: Optional ``YYYY-MM-DD`` Toronto date string for the
                new day. When omitted, the caller has already established
                the day boundary and just needs the counter reset.

        Note:
            Per FTMO rules the daily loss limit (3%) resets at America/Toronto midnight.
            Prior days' losses continue to count only toward the 10% total
            drawdown, not today's daily budget.
        """
        with self._lock:
            pre_daily_used = self._daily_risk_used
            pre_open_risk = self._open_risk
            positions_carried = len(self._open_positions)
            self._daily_risk_used = 0.0
            self.breaker.daily_dd_pct = 0.0
            logger.info(
                "SLPositionSizer.reset_daily%s: daily_used=%.2f→0.00, "
                "open_risk=%.2f (carried), positions_carried=%d, "
                "balance=%.2f",
                f" (cet_date={cet_date})" if cet_date else "",
                pre_daily_used,
                pre_open_risk,
                positions_carried,
                self.account_balance,
            )

    def calculate(
        self,
        symbol: str,
        entry_price: float,
        sl_price: float,
        profile: Union[str, object] = "sniper",
    ) -> PositionSizeResult:
        """
        Calculate position size so SL hit = intended risk.

        Args:
            symbol: Trading instrument (e.g., "EURUSD")
            entry_price: Entry price
            sl_price: Stop loss price level (determined by strategy)
            profile: "sniper" (full risk) or "swarm" (reduced risk)

        Returns:
            PositionSizeResult with lots and metadata
        """
        warnings = []

        # Normalize profile (accept both str and Profile enum)
        if hasattr(profile, "value"):
            profile = profile.value.lower()
        profile = str(profile).lower()

        # Check circuit breakers
        breaker_reason = self.breaker.check_breakers()
        if breaker_reason:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=0.0,
                sl_distance_price=0.0,
                pip_value=0.0,
                blocked=True,
                block_reason=breaker_reason,
            )

        # Get instrument spec
        spec = INSTRUMENTS.get(symbol)
        if not spec:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=0.0,
                sl_distance_price=0.0,
                pip_value=0.0,
                blocked=True,
                block_reason=f"Unknown instrument: {symbol}",
            )

        # Calculate SL distance
        sl_distance_price = abs(entry_price - sl_price)
        sl_distance_pips = sl_distance_price / spec.pip_size

        logger.debug(
            "Sizing: symbol=%s entry=%.5f sl=%.5f dist_price=%.6f dist_pips=%.1f pip_size=%.4f",
            symbol,
            entry_price,
            sl_price,
            sl_distance_price,
            sl_distance_pips,
            spec.pip_size,
        )

        # Check min SL distance
        if sl_distance_pips < self.min_sl_pips - 0.01:  # Allow tiny floating point margin
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot,
                blocked=True,
                block_reason=f"SL distance {sl_distance_pips:.1f} pips < minimum {self.min_sl_pips} pips",
            )

        # Determine risk amount based on profile
        base_risk = self.risk_per_trade
        if base_risk <= 0:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot,
                blocked=True,
                block_reason="Account risk amount is zero",
            )

        if profile not in ("sniper", "swarm"):
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot if spec else 0.0,
                blocked=True,
                block_reason=f"Unknown profile: {profile}",
            )
        if profile == "swarm":
            base_risk *= 0.5  # Swarm uses half the per-trade risk

        # ── Concurrent position limits (Phase 5b) ────────────────────
        # Max positions per symbol: prevents the 9-GBPUSD-position bug
        # where the risk engine allowed unlimited concurrent positions
        # on the same symbol.
        current_positions_for_symbol = self._count_positions_for_symbol(symbol)
        if current_positions_for_symbol >= self.max_positions_per_symbol:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot,
                blocked=True,
                block_reason=(f"Max {self.max_positions_per_symbol} position(s) already open for {symbol}"),
            )

        # Max total concurrent open risk: hard cap on sum of all open
        # position risk.  Prevents portfolio overexposure even when
        # individual trades are within budget.
        current_open_risk = self._open_risk
        if current_open_risk + base_risk > self.max_total_open_risk:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot,
                blocked=True,
                block_reason=(
                    f"Total open risk ${current_open_risk:.2f} + new "
                    f"${base_risk:.2f} = ${current_open_risk + base_risk:.2f} "
                    f"exceeds max ${self.max_total_open_risk:.2f}"
                ),
            )

        # Check daily risk cap
        if base_risk > self.daily_risk_remaining:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot,
                blocked=True,
                block_reason=f"Trade risk ${base_risk:.2f} exceeds daily remaining ${self.daily_risk_remaining:.2f}",
            )

        # Calculate lots: risk_amount / (sl_pips * pip_value_per_lot)
        lots = base_risk / (sl_distance_pips * spec.pip_value_per_lot)

        # Apply max lot cap
        if lots > self.max_lot_size:
            warnings.append(f"Lots {lots:.4f} capped to max {self.max_lot_size}")
            lots = self.max_lot_size
            # Recalculate actual risk with capped lots
            actual_risk = lots * sl_distance_pips * spec.pip_value_per_lot
        else:
            actual_risk = base_risk

        # Round to reasonable precision (0.01 lots = micro lots)
        lots = round(lots, 2)
        if lots < 0.01:
            return PositionSizeResult(
                lots=0.0,
                risk_amount=0.0,
                sl_distance_pips=sl_distance_pips,
                sl_distance_price=sl_distance_price,
                pip_value=spec.pip_value_per_lot,
                blocked=True,
                block_reason=f"Calculated lots {lots:.4f} below minimum 0.01",
            )

        return PositionSizeResult(
            lots=lots,
            risk_amount=actual_risk,
            sl_distance_pips=sl_distance_pips,
            sl_distance_price=sl_distance_price,
            pip_value=spec.pip_value_per_lot,
            warnings=warnings,
        )


# ── Module-level helpers ────────────────────────────────────────────────


def _compute_position_risk_usd(
    *,
    symbol: str,
    entry_price: float,
    sl_price: Optional[float],
    lots: float,
) -> float:
    """Compute the USD risk for a single open position.

    Mirrors the formula used by
    :meth:`SLPositionSizer.calculate` so seeded positions from the
    broker consume the same ``open_risk`` budget that a live-trade
    registration would have. Centralised here so the sizer does not
    have to expose it as a method (the helper is also called from
    :meth:`SLPositionSizer.reconcile_with_broker`).

    Args:
        symbol: Trading symbol (e.g. ``"EURUSD"``). Used to look up
            ``INSTRUMENTS`` for ``pip_size`` and ``pip_value_per_lot``.
        entry_price: Average fill price reported by the broker.
        sl_price: Stop-loss price, or ``None`` if no SL is set on the
            broker side.
        lots: Position size in lots.

    Returns:
        USD risk amount. Returns ``lots * 100.0`` as a conservative
        fallback when no SL is provided or the symbol is unknown —
        matches the engine's existing ``_seed_existing_positions``
        behaviour so seeded counts stay comparable across restart.
    """
    spec = INSTRUMENTS.get(str(symbol).upper())
    if spec is None:
        # Unknown symbol — fall back to FX defaults so an unmapped
        # broker position still contributes a reasonable risk estimate
        # rather than zero (which would silently understate open_risk).
        spec = InstrumentSpec(
            symbol=str(symbol),
            pip_size=0.0001,
            lot_size=100_000,
            pip_value_per_lot=10.0,
        )

    if sl_price is None or sl_price <= 0.0 or entry_price <= 0.0 or lots <= 0.0:
        # No SL known — use the same conservative estimate the engine
        # uses ($100 per lot) so broker positions without an SL still
        # reserve a non-zero risk budget against the daily cap.
        return float(lots) * 100.0

    price_distance = abs(float(entry_price) - float(sl_price))
    pips = price_distance / spec.pip_size if spec.pip_size > 0 else 0.0
    return float(pips) * float(lots) * spec.pip_value_per_lot
