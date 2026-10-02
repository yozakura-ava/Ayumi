"""Position Monitor — centralized position monitoring and lifecycle management.

Tracks per-position MAE/MFE, time-based exit rules, portfolio exposure,
drawdown alerts, TP2/TP3 ratcheting, and integrates with the kill switch for
portfolio-level safety.

Thread-safe. Designed to be called on each tick from the forward test engine
or run autonomously via its background monitoring thread.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .models import Position, TradeDirection

logger = logging.getLogger(__name__)


# ── TP Ratchet Action ──────────────────────────────────────────────────────


@dataclass
class TpRatchetAction:
    """Result of one TP-level ratchet check on a position.

    Returned by ``PositionMonitor.check_tp_levels`` so callers can log the
    outcome (ratchet fired, amend failed, no-feed graceful skip) without
    having to inspect internal state.

    Attributes:
        position_id: Position whose TP-level was checked.
        level: TP level ratcheted (2 or 3).
        new_sl: Stop loss we asked the broker to set.
        new_tp: Take profit we asked the broker to set.
        direction: ``"long"`` or ``"short"`` — included for log readability.
        entry_price: Breakeven anchor for level 2 (original entry).
        amend_status: One of:
            ``"fired"``        — amend succeeded and tp_levels_fired was
                                  updated; safe to skip on subsequent ticks.
            ``"amend_failed"`` — broker rejected amend; tp_levels_fired NOT
                                  updated so the next tick will retry.
            ``"no_feed"``      — no market_feed reference was supplied to
                                  the monitor; the ratchet could not be
                                  sent (caller may fall back to manual).
            ``"no_symbol_id"`` — market_feed was supplied but could not
                                  resolve the symbol id (transient — retry).
    """

    position_id: str
    level: int
    new_sl: float
    new_tp: float
    direction: str
    entry_price: float
    amend_status: str


class PositionMonitor:
    """Centralized position monitoring and lifecycle management."""

    def __init__(
        self,
        order_manager,
        risk_guard=None,
        kill_switch=None,
        max_trade_duration_sec: float = 14400,  # 4 hours default
        check_interval_sec: float = 5.0,
        contract_sizes: dict[str, float] | None = None,
        market_feed=None,
        slot_tracker: Optional[Callable[[str, float], None]] = None,
    ):
        self._order_manager = order_manager
        self._risk_guard = risk_guard
        self._kill_switch = kill_switch
        self._max_trade_duration_sec = max_trade_duration_sec
        self._check_interval_sec = check_interval_sec
        self._contract_sizes = contract_sizes or {}  # symbol name -> contract size
        # Optional reference to OpenApiSpotFeed for issuing amend_sl_tp calls
        # when a TP2/TP3 level is crossed.  When None, check_tp_levels()
        # still detects crossings and returns ``no_feed`` actions so callers
        # can wire a fallback (manual amend, alert, etc.) without losing the
        # monitoring visibility.
        self._market_feed = market_feed
        # Card 4083ac2d-...: optional callback fired AFTER a successful
        # broker amend with (position_id, new_sl). The launcher wires this
        # to CorrelationGate.update_sl so the per-symbol at-risk cap stays
        # in sync with live SL trailing. Defensive: callback exceptions
        # are caught and logged so the trailing loop is never broken.
        self._slot_tracker = slot_tracker
        self._lock = threading.RLock()

        # Background monitoring thread
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()

        # Callbacks for alerts
        self._callbacks: dict[str, list[Callable]] = {
            "on_time_exit": [],
            "on_drawdown_warning": [],
            "on_drawdown_critical": [],
        }

    def _contract_size_for(self, symbol: str) -> float:
        """Return contract size for a symbol, defaulting to forex 100k."""
        return self._contract_sizes.get(symbol, 100_000.0)

    def set_market_feed(self, market_feed) -> None:
        """Attach (or replace) the live market feed used for amend_sl_tp calls.

        Most callers wire the feed at construction time.  This setter exists
        for the ForwardTestEngine path where ``OpenApiSpotFeed`` is created
        lazily inside ``_start_openapi_feed`` (after ``_build_components``
        has already constructed the PositionMonitor).
        """
        with self._lock:
            self._market_feed = market_feed

    def set_slot_tracker(self, slot_tracker: Optional[Callable[[str, float], None]]) -> None:
        """Attach (or replace) the SL-change observer (card 4083ac2d-...).

        The launcher wires ``CorrelationGate.update_sl`` here so the gate's
        at-risk cap stays in sync with live SL trails.  ``None`` clears the
        observer (useful for tests).  Mirrors ``set_market_feed``'s lazy-
        wiring pattern for the engine path.
        """
        with self._lock:
            self._slot_tracker = slot_tracker

    # ── Core: update_positions ─────────────────────────────────────────────

    def update_positions(self, prices: dict, bids: dict, asks: dict):
        """Called on each tick — update all positions, track MAE/MFE, check time limits.

        Args:
            prices: dict of {symbol: mid_price}
            bids: dict of {symbol: bid_price}
            asks: dict of {symbol: ask_price}
        """
        with self._lock:
            open_positions = self._order_manager.get_open_positions()
            now = datetime.now(timezone.utc)

            for position in open_positions:
                if position.symbol not in prices:
                    continue

                mid_price = prices[position.symbol]
                bid = bids.get(position.symbol, mid_price)
                ask = asks.get(position.symbol, mid_price)

                # Update MAE/MFE based on unrealized PnL
                self._update_excursions(position, bid, ask)

                # Update high/low water marks
                self._update_water_marks(position, bid, ask)

                # Update time in trade
                self._update_time_in_trade(position, now)

        # Sprint Task 1.5 (card a7b8e896): TP2/TP3 ratcheting — out of the
        # per-position loop so a single broker amend failure on one position
        # cannot block ratcheting on the others.  ``check_tp_levels`` is
        # idempotent (tp_levels_fired guards) so calling it every tick is
        # safe and cheap when no level has crossed.
        self.check_tp_levels(prices)

    def _update_excursions(self, position: Position, bid: float, ask: float):
        """Update Maximum Favorable / Adverse Excursion."""
        cs = self._contract_size_for(position.symbol)
        # Calculate current unrealized PnL for this tick
        if position.direction == TradeDirection.LONG:
            exit_price = bid if bid > 0 else position.current_price
            current_pnl = (exit_price - position.entry_price) * position.volume * cs
        else:
            exit_price = ask if ask > 0 else position.current_price
            current_pnl = (position.entry_price - exit_price) * position.volume * cs

        if current_pnl > position.max_favorable_excursion:
            position.max_favorable_excursion = current_pnl
        if current_pnl < position.max_adverse_excursion:
            position.max_adverse_excursion = current_pnl

    def _update_water_marks(self, position: Position, bid: float, ask: float):
        """Update high/low water marks for the position.

        For longs: high_water_mark = highest price seen, low_water_mark = lowest.
        For shorts: high_water_mark = lowest price seen, low_water_mark = highest.
        """
        if position.direction == TradeDirection.LONG:
            current = bid if bid > 0 else position.current_price
            if position.high_water_mark == 0.0 or current > position.high_water_mark:
                position.high_water_mark = current
            if position.low_water_mark == 0.0 or current < position.low_water_mark:
                position.low_water_mark = current
        else:
            current = ask if ask > 0 else position.current_price
            if position.high_water_mark == 0.0 or current < position.high_water_mark:
                position.high_water_mark = current
            if position.low_water_mark == 0.0 or current > position.low_water_mark:
                position.low_water_mark = current

    def _update_time_in_trade(self, position: Position, now: datetime):
        """Update time-in-trade seconds from opened_at."""
        if position.opened_at:
            if position.opened_at.tzinfo is None:
                opened = position.opened_at.replace(tzinfo=timezone.utc)
            else:
                opened = position.opened_at
            position.time_in_trade_sec = (now - opened).total_seconds()

    # ── Portfolio Summary ──────────────────────────────────────────────────

    def get_portfolio_summary(self) -> dict:
        """Return current portfolio state: total exposure, unrealized PnL, etc."""
        with self._lock:
            open_positions = self._order_manager.get_open_positions()

            total_unrealized_pnl = sum(p.unrealized_pnl for p in open_positions)
            total_notional = sum(p.volume * self._contract_size_for(p.symbol) for p in open_positions)
            positions_by_symbol: dict[str, int] = {}
            for p in open_positions:
                positions_by_symbol[p.symbol] = positions_by_symbol.get(p.symbol, 0) + 1

            largest_position = max(
                (p.volume * self._contract_size_for(p.symbol) for p in open_positions),
                default=0.0,
            )

            total_mfe = sum(p.max_favorable_excursion for p in open_positions)
            total_mae = sum(p.max_adverse_excursion for p in open_positions)

            return {
                "position_count": len(open_positions),
                "total_unrealized_pnl": round(total_unrealized_pnl, 2),
                "total_notional_exposure": round(total_notional, 2),
                "positions_by_symbol": positions_by_symbol,
                "largest_position_notional": round(largest_position, 2),
                "total_mfe": round(total_mfe, 2),
                "total_mae": round(total_mae, 2),
                "is_killed": (self._kill_switch.is_globally_killed() if self._kill_switch else False),
                "is_frozen": (self._kill_switch.is_globally_frozen() if self._kill_switch else False),
            }

    # ── Position Report ────────────────────────────────────────────────────

    def get_position_report(self, position_id: str) -> dict | None:
        """Detailed report for a single position."""
        position = self._order_manager.get_position(position_id)
        if position is None:
            return None

        return {
            "position_id": position.position_id,
            "symbol": position.symbol,
            "direction": position.direction.value,
            "volume": position.volume,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "unrealized_pnl": round(position.unrealized_pnl, 2),
            "max_favorable_excursion (MFE)": round(position.max_favorable_excursion, 2),
            "max_adverse_excursion (MAE)": round(position.max_adverse_excursion, 2),
            "high_water_mark": position.high_water_mark,
            "low_water_mark": position.low_water_mark,
            "time_in_trade_sec": round(position.time_in_trade_sec, 1),
            "status": position.status.value,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "opened_at": (position.opened_at.isoformat() if position.opened_at else None),
        }

    # ── Time Exit ──────────────────────────────────────────────────────────

    def check_time_exits(self) -> list[str]:
        """Return position IDs that have exceeded max trade duration."""
        expired: list[str] = []
        with self._lock:
            open_positions = self._order_manager.get_open_positions()
            for position in open_positions:
                if position.time_in_trade_sec >= self._max_trade_duration_sec:
                    expired.append(position.position_id)
                    logger.info(
                        "Position %s exceeded max duration (%.0fs >= %.0fs)",
                        position.position_id,
                        position.time_in_trade_sec,
                        self._max_trade_duration_sec,
                    )
        return expired

    # ── TP2/TP3 Ratcheting (Sprint Task 1.5, card a7b8e896) ────────────────
    #
    # The cTrader Open API ``ProtoOAAmendPositionSLTPReq`` only accepts a
    # single take-profit per position, so TP2/TP3 cannot be sent to the
    # broker.  Instead we store them on the Position (Task 1.3) and detect
    # crossing here so we can ratchet the broker TP/SL as price moves
    # through each level.
    #
    # Crossing logic (using mid-price from ``prices[symbol]``):
    #   LONG  — TP2 fires when ``price >= take_profit_2``; we amend
    #           ``sl=position.entry_price`` (breakeven) and ``tp=tp2``.
    #           TP3 fires when ``price >= take_profit_3``; we amend
    #           ``sl=take_profit_2`` (lock-in TP1 profit) and ``tp=tp3``.
    #   SHORT — mirror: ``price <= take_profit_2`` / ``price <= tp3``.
    #
    # Idempotency: ``Position.tp_levels_fired`` is the source of truth.  We
    # only amend and append when the level is NOT already in the list.  If
    # the amend call returns ``False`` we deliberately do NOT append — the
    # next tick will retry, so a transient broker hiccup does not silently
    # drop the ratchet.

    def check_tp_levels(self, prices: dict) -> list[TpRatchetAction]:
        """Check all open positions for TP2/TP3 crossings and amend the broker.

        Called once per tick from :meth:`update_positions`.  Returns one
        :class:`TpRatchetAction` per detected crossing (whether the amend
        succeeded or not) so callers can log / alert / drive fallback logic.

        Args:
            prices: ``{symbol: mid_price}`` — the same dict that was passed
                to ``update_positions``.  Only positions whose symbol appears
                here are evaluated; positions without a fresh price are
                skipped to avoid stale-fire on disconnected feeds.

        Returns:
            list[TpRatchetAction]: empty list when no positions needed
            ratcheting.  One entry per ratchet fired or attempted.
        """
        actions: list[TpRatchetAction] = []
        with self._lock:
            open_positions = self._order_manager.get_open_positions()

        for position in open_positions:
            price = prices.get(position.symbol)
            if price is None:
                continue

            actions.extend(self._maybe_ratchet(position, price))

        return actions

    def _maybe_ratchet(self, position: Position, price: float) -> list[TpRatchetAction]:
        """Evaluate one position against the TP2/TP3 ladder.

        Helper kept out of the lock so the broker amend (which can block for
        up to ``_AMEND_TIMEOUT_SEC``) does not hold the monitor lock against
        ``update_positions`` / ``check_time_exits`` / portfolio-summary
        callers.  The Position object is mutated only on success and the
        ``tp_levels_fired`` list is appended in-place.
        """
        actions: list[TpRatchetAction] = []

        # Defensive: if tp_levels_fired was somehow replaced by something
        # other than a list, treat it as empty so we don't crash.
        fired = position.tp_levels_fired
        if not isinstance(fired, list):
            logger.warning(
                "Position %s has non-list tp_levels_fired=%r — resetting",
                position.position_id,
                fired,
            )
            position.tp_levels_fired = []
            fired = position.tp_levels_fired

        # Breakeven anchor = ORIGINAL entry_price (not current price).
        # Using current price would defeat the purpose of the ratchet:
        # the whole point of moving SL to breakeven at TP2 is to risk-zero
        # the trade at the original entry, locking in TP1's gain.
        entry_price = position.entry_price

        for level in (2, 3):
            tp_attr = f"take_profit_{level}"
            tp_value = getattr(position, tp_attr, None)
            if tp_value is None:
                continue

            if level in fired:
                continue  # Idempotency: skip already-fired levels.

            # Crossing test (use >= / <= on mid so we fire at-or-before the
            # actual broker close, which uses bid for LONG / ask for SHORT).
            if position.direction == TradeDirection.LONG:
                crossed = price >= tp_value
            else:  # SHORT
                crossed = price <= tp_value

            if not crossed:
                continue

            # Determine target SL/TP for the broker amend.
            if level == 2:
                new_sl = entry_price  # breakeven (original entry)
                new_tp = tp_value  # TP2
            else:  # level == 3
                # Lock in TP1 profit (SL = TP2) and push TP to TP3.
                tp2 = position.take_profit_2
                if tp2 is None:
                    # Can't safely ratchet to TP3 without a TP2 anchor; skip.
                    logger.warning(
                        "Position %s: TP3 crossed but take_profit_2 is None "
                        "\u2014 cannot compute new SL, skipping ratchet",
                        position.position_id,
                    )
                    continue
                new_sl = tp2
                new_tp = tp_value

            action = self._fire_ratchet(
                position,
                level=level,
                new_sl=new_sl,
                new_tp=new_tp,
                direction=position.direction.value,
                entry_price=entry_price,
            )
            actions.append(action)

        return actions

    def _fire_ratchet(
        self,
        position: Position,
        *,
        level: int,
        new_sl: float,
        new_tp: float,
        direction: str,
        entry_price: float,
    ) -> TpRatchetAction:
        """Send the broker amend and record the outcome.

        On success: append ``level`` to ``position.tp_levels_fired`` so the
        next tick skips this level (idempotency).

        On failure (no feed, no symbol id, broker reject): leave
        ``tp_levels_fired`` untouched so the next tick retries.
        """
        if self._market_feed is None:
            logger.warning(
                "Position %s: TP%d ratchet detected but no market_feed on "
                "PositionMonitor \u2014 cannot amend SL/TP. Will retry next tick.",
                position.position_id,
                level,
            )
            return TpRatchetAction(
                position_id=position.position_id,
                level=level,
                new_sl=new_sl,
                new_tp=new_tp,
                direction=direction,
                entry_price=entry_price,
                amend_status="no_feed",
            )

        try:
            symbol_id = self._market_feed.resolve_symbol_id(position.symbol)
        except (ValueError, KeyError, AttributeError) as exc:
            logger.warning(
                "Position %s: TP%d ratchet \u2014 cannot resolve symbol_id for %r (%s); will retry next tick",
                position.position_id,
                level,
                position.symbol,
                exc,
            )
            return TpRatchetAction(
                position_id=position.position_id,
                level=level,
                new_sl=new_sl,
                new_tp=new_tp,
                direction=direction,
                entry_price=entry_price,
                amend_status="no_symbol_id",
            )

        logger.info(
            "Position %s: TP%d crossed \u2014 amending broker SL=%.5f TP=%.5f",
            position.position_id,
            level,
            new_sl,
            new_tp,
        )
        try:
            amended = self._market_feed.amend_sl_tp(
                position.position_id,
                new_sl,
                new_tp,
                symbol_id=symbol_id,
            )
        except Exception as exc:
            logger.warning(
                "Position %s: TP%d amend raised %s: %s \u2014 will retry next tick",
                position.position_id,
                level,
                type(exc).__name__,
                exc,
            )
            return TpRatchetAction(
                position_id=position.position_id,
                level=level,
                new_sl=new_sl,
                new_tp=new_tp,
                direction=direction,
                entry_price=entry_price,
                amend_status="amend_failed",
            )

        if not amended:
            logger.warning(
                "Position %s: TP%d amend returned False \u2014 will retry next tick",
                position.position_id,
                level,
            )
            return TpRatchetAction(
                position_id=position.position_id,
                level=level,
                new_sl=new_sl,
                new_tp=new_tp,
                direction=direction,
                entry_price=entry_price,
                amend_status="amend_failed",
            )

        # Success: record the fired level for idempotency.
        position.tp_levels_fired.append(level)
        # Card 4083ac2d-...: keep the position object in sync with the
        # broker's new SL and notify the optional slot tracker so the
        # per-symbol at-risk cap stays accurate after a trailing ratchet.
        # Wrapped so a buggy tracker cannot break the trailing loop.
        position.stop_loss = new_sl
        if self._slot_tracker is not None:
            try:
                self._slot_tracker(position.position_id, new_sl)
            except Exception as tracker_exc:  # noqa: BLE001
                logger.warning(
                    "Position %s: TP%d ratchet fired but slot_tracker raised %s (non-fatal)",
                    position.position_id,
                    level,
                    tracker_exc,
                )
        logger.info(
            "Position %s: TP%d ratchet FIRED (sl=%.5f tp=%.5f fired=%s)",
            position.position_id,
            level,
            new_sl,
            new_tp,
            position.tp_levels_fired,
        )
        return TpRatchetAction(
            position_id=position.position_id,
            level=level,
            new_sl=new_sl,
            new_tp=new_tp,
            direction=direction,
            entry_price=entry_price,
            amend_status="fired",
        )

    # ── Drawdown Alerts ────────────────────────────────────────────────────

    def check_drawdown_alerts(
        self,
        warning_threshold: float = 0.02,
        critical_threshold: float = 0.05,
    ) -> list[dict]:
        """Check per-position drawdown from MFE against thresholds.

        Drawdown from MFE = (MFE - current_unrealized) / |MFE| if MFE > 0
        If MFE <= 0, drawdown is measured from entry (unrealized loss).

        Returns list of alert dicts with position_id, level, drawdown, message.
        """
        alerts: list[dict] = []
        with self._lock:
            open_positions = self._order_manager.get_open_positions()
            for position in open_positions:
                mfe = position.max_favorable_excursion
                current_pnl = position.unrealized_pnl

                if mfe > 0:
                    # Give-back from peak favorable excursion
                    drawdown = (mfe - current_pnl) / mfe if mfe != 0 else 0.0
                else:
                    # Never been in profit — measure adverse excursion
                    drawdown = 0.0
                    if position.max_adverse_excursion < 0:
                        # Express as fraction of notional
                        notional = position.volume * self._contract_size_for(position.symbol)
                        if notional > 0:
                            drawdown = abs(position.max_adverse_excursion) / notional

                if drawdown >= critical_threshold:
                    alert = {
                        "position_id": position.position_id,
                        "symbol": position.symbol,
                        "level": "critical",
                        "drawdown": round(drawdown, 4),
                        "mfe": round(mfe, 2),
                        "current_pnl": round(current_pnl, 2),
                        "message": (
                            f"CRITICAL drawdown {drawdown:.1%} on {position.symbol} "
                            f"(MFE={mfe:.2f}, current={current_pnl:.2f})"
                        ),
                    }
                    alerts.append(alert)
                    self._trigger_callbacks("on_drawdown_critical", alert)
                elif drawdown >= warning_threshold:
                    alert = {
                        "position_id": position.position_id,
                        "symbol": position.symbol,
                        "level": "warning",
                        "drawdown": round(drawdown, 4),
                        "mfe": round(mfe, 2),
                        "current_pnl": round(current_pnl, 2),
                        "message": (
                            f"WARNING drawdown {drawdown:.1%} on {position.symbol} "
                            f"(MFE={mfe:.2f}, current={current_pnl:.2f})"
                        ),
                    }
                    alerts.append(alert)
                    self._trigger_callbacks("on_drawdown_warning", alert)

        # Kill switch: check portfolio-level drawdown
        self._check_portfolio_drawdown_kill()

        return alerts

    def _check_portfolio_drawdown_kill(self):
        """Activate kill switch FREEZE if portfolio unrealized drawdown breaches limits."""
        if self._kill_switch is None or self._risk_guard is None:
            return

        # Use risk_guard's drawdown calculation
        drawdown_pct = self._risk_guard.current_drawdown_pct
        if drawdown_pct >= self._risk_guard._config.total_drawdown_limit_pct:
            if not self._kill_switch.is_active():
                logger.critical(
                    "Portfolio drawdown %.2f%% >= limit %.2f%% — activating FREEZE",
                    drawdown_pct * 100,
                    self._risk_guard._config.total_drawdown_limit_pct * 100,
                )
                self._kill_switch.activate_global_freeze(
                    reason="portfolio_drawdown_breach",
                    triggered_by="position_monitor",
                )

    # ── Background Monitoring Thread ───────────────────────────────────────

    def start_monitoring(self):
        """Start background monitoring thread."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            logger.warning("Position monitor already running")
            return

        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="position-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        logger.info(
            "Position monitor started (interval=%.1fs, max_duration=%.0fs)",
            self._check_interval_sec,
            self._max_trade_duration_sec,
        )

    def stop_monitoring(self):
        """Stop background monitoring thread."""
        if self._monitor_thread is None:
            return

        self._stop_monitor.set()
        self._monitor_thread.join(timeout=10.0)
        self._monitor_thread = None
        logger.info("Position monitor stopped")

    def _monitor_loop(self):
        """Background loop: periodically check time exits and drawdown alerts."""
        while not self._stop_monitor.wait(self._check_interval_sec):
            try:
                # Check time-based exits
                expired = self.check_time_exits()
                for pid in expired:
                    self._trigger_callbacks("on_time_exit", pid)

                # Check drawdown alerts
                self.check_drawdown_alerts()

            except Exception as exc:
                logger.error("Position monitor loop error: %s", exc, exc_info=True)

    # ── Callback Registration ──────────────────────────────────────────────

    def register_callback(self, event: str, callback: Callable):
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def _trigger_callbacks(self, event: str, *args, **kwargs):
        if event in self._callbacks:
            for callback in self._callbacks[event]:
                try:
                    callback(*args, **kwargs)
                except Exception as exc:
                    logger.error("Callback error for %s: %s", event, exc)
