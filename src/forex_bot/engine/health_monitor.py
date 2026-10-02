"""Health monitor — structured health reporting for the forward test.

Emits [B5 Health] and [S1 Health] log entries on a timer.
Keeps the same tag convention as the old code (Amendment A6)
so existing log parsers keep working.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("ayumi.forward_test")

_DEFAULT_INTERVAL_SEC = 60


class HealthMonitor:
    """Structured health reporting for the forward test.

    Replaces the inline health-logging loop that previously lived in
    ``scripts/launch_blend_forward_test.py``.

    Attach subsystems via :meth:`attach`, then call :meth:`start` to begin
    periodic ``[B5 Health]`` and ``[S1 Health]`` logging on a daemon thread.
    """

    def __init__(self, interval_seconds: int = _DEFAULT_INTERVAL_SEC):
        self._interval = interval_seconds
        self._session: Any = None
        self._market_data_feed: Any = None
        self._order_gateway: Any = None
        self._position_tracker: Any = None
        self._strategies: dict[str, Any] | None = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._start_time: float = 0.0

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def attach(
        self,
        session: Any = None,
        market_data_feed: Any = None,
        order_gateway: Any = None,
        position_tracker: Any = None,
        strategies: dict[str, Any] | None = None,
    ) -> "HealthMonitor":
        """Attach subsystems to monitor.  Returns *self* for chaining."""
        if session is not None:
            self._session = session
        if market_data_feed is not None:
            self._market_data_feed = market_data_feed
        if order_gateway is not None:
            self._order_gateway = order_gateway
        if position_tracker is not None:
            self._position_tracker = position_tracker
        if strategies is not None:
            self._strategies = strategies
        return self

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread (idempotent)."""
        if self._thread is not None:
            return
        self._start_time = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="b5-health-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("[B5 Health] Health monitor started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Stop the daemon thread (idempotent)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        logger.info("[B5 Health] Health monitor stopped")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._emit_health()
            except Exception as exc:
                logger.warning("[B5 Health] Error logging health: %s", exc)

    def _emit_health(self) -> None:
        """Emit one round of [B5 Health] + [S1 Health] log lines."""
        uptime = time.monotonic() - self._start_time

        # -- Gather counters ------------------------------------------------
        ticks = self._safe_attr(self._market_data_feed, "ticks_received", 0)
        tps = self._safe_attr(self._market_data_feed, "ticks_per_second", 0.0)
        bars = self._safe_attr(self._market_data_feed, "bars_built", 0)
        signals = self._safe_attr(self._market_data_feed, "signals_generated", 0)

        paper_trades = self._safe_attr(self._market_data_feed, "paper_trades", 0)
        live_fills = self._safe_attr(self._order_gateway, "live_fills", 0)
        balance = self._safe_attr(self._market_data_feed, "balance", 0.0)

        # -- Core [B5 Health] line (Amendment A6 format) -------------------
        logger.info(
            "[B5 Health] ticks=%d tps=%.2f bars=%d signals=%d paper_trades=%d live_fills=%d balance=%.2f uptime=%.0fs",
            ticks,
            tps,
            bars,
            signals,
            paper_trades,
            live_fills,
            balance,
            uptime,
        )

        # -- Additional subsystem checks (NEW) ------------------------------
        extras: list[str] = []

        if self._order_gateway is not None:
            pending = self._safe_attr(self._order_gateway, "pending_orders", 0)
            extras.append(f"pending_orders={pending}")
            # Card 18b74ea7: surface the session-conflict counter so
            # operators see ALREADY_LOGGED_IN events that arrive with empty
            # clientOrderId (and were previously silently dropped). The
            # counter lives on OpenApiSpotFeed (which is the live-mode
            # order_gateway). Attribute name mirrors the spot feed's
            # private counter (``_order_error_session_conflict_count``)
            # so existing ``_safe_attr`` lookups Just Work.
            # Note: MagicMock instances return a MagicMock (not the
            # default) for any attribute access, which breaks numeric
            # comparisons. Guard with isinstance so test scaffolding that
            # uses ``_make_mock(live_fills=2)`` does not crash.
            session_conflict_raw = self._safe_attr(self._order_gateway, "_order_error_session_conflict_count", 0)
            session_conflict = session_conflict_raw if isinstance(session_conflict_raw, int) else 0
            if session_conflict > 0:
                extras.append(f"order_error_session_conflict={session_conflict}")
            # Card ce6de98d (E): surface the unmatched_late_fills counter
            # so operators see how often broker events arrive after the
            # late-fill registry's 120s grace window. The DROP warning was
            # always logged; the counter is additive so the B5 health line
            # shows the rate at which broker events arrive too late to be
            # matched.
            unmatched_late_raw = self._safe_attr(self._order_gateway, "_unmatched_late_fills_count", 0)
            unmatched_late = unmatched_late_raw if isinstance(unmatched_late_raw, int) else 0
            if unmatched_late > 0:
                extras.append(f"unmatched_late_fills={unmatched_late}")
            # Card 8ad140c5 finding #5 (sprint reina-2026-08-18-106):
            # surface signals_indeterminate in the ACTIVE blend health
            # path so operators see live-order INDETERMINATE outcomes
            # (timeout with reason=indeterminate_awaiting_event) without
            # grepping logs. The counter lives on the ForwardTestEngine
            # (``_health.signals_indeterminate``) which is exposed as
            # ``_order_gateway`` here for legacy v2 launchers; the ACTIVE
            # blend launcher (forward_test_engine.py) writes the same
            # counter into the heartbeat JSON via _write_heartbeat. We
            # surface it here too so the B5 health line stays consistent
            # across both launcher paths.
            signals_indeterminate_raw = self._safe_attr(self._order_gateway, "_signals_indeterminate", 0)
            signals_indeterminate = signals_indeterminate_raw if isinstance(signals_indeterminate_raw, int) else 0
            if signals_indeterminate > 0:
                extras.append(f"signals_indeterminate={signals_indeterminate}")

        session_state = None
        if self._session is not None:
            session_state = self._safe_attr(self._session, "state", None)
            if session_state is not None:
                extras.append(f"session_state={session_state}")

        if self._position_tracker is not None:
            open_positions = self._safe_attr(self._position_tracker, "open_positions", 0)
            extras.append(f"open_positions={open_positions}")

        if extras:
            logger.info("[B5 Health] %s", " ".join(extras))

        # -- Warning: session not subscribed after grace period -------------
        if session_state is not None and str(session_state) != "SessionState.SUBSCRIBED" and uptime > 60.0:
            logger.warning(
                "[B5 Health] session_state=%s after %.0fs — expected SUBSCRIBED",
                session_state,
                uptime,
            )

        # -- Per-strategy [S1 Health] lines --------------------------------
        if self._strategies:
            now = time.monotonic()
            for sname in sorted(self._strategies):
                info = self._strategies[sname]
                evals = self._safe_val(info, "evals", 0)
                no_signal = self._safe_val(info, "no_signal", 0)
                last_eval_monotonic = self._safe_val(info, "last_eval_monotonic", now)
                last_ago = max(0.0, now - last_eval_monotonic)
                logger.info(
                    "[S1 Health] %s: evals=%d no_signal=%d last=%.0fs ago",
                    sname,
                    evals,
                    no_signal,
                    last_ago,
                )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_attr(obj: Any, attr: str, default: Any) -> Any:
        """Read *attr* from *obj* safely — returns *default* on any failure."""
        if obj is None:
            return default
        try:
            return getattr(obj, attr, default)
        except Exception:
            return default

    @staticmethod
    def _safe_val(obj: Any, key: str, default: Any) -> Any:
        """Read *key* from a dict or object — returns *default* on failure."""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        try:
            return getattr(obj, key, default)
        except Exception:
            return default
