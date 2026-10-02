"""Production forward test runner wiring the full Ayumi signal pipeline."""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from common.logging_config import setup_logging
from confidence.engine import ConfidenceEngine
from confidence.gates import GateConfig
from orchestrator.signal_orchestrator import (
    OrchestratedOrder,
    OrchestratorTradeSignal,
    SignalOrchestrator,
)
from orchestrator.strategy_adapter import StrategyAdapter
from risk.edge_telemetry import EdgeTelemetryTracker

# Import the canonical trading-date helper from ftmo_guard — do NOT duplicate it.
# The daily reset boundary is FTMO-defined: America/Toronto midnight (Eastern).
from risk.ftmo_guard import _trading_date
from risk.profile_router import ProfileRouter
from risk.regime_thresholds import Regime, RegimeAwareThresholds
from risk.sl_position_sizer import SLPositionSizer
from risk.state_persistence import StatePersistence

# Phase 0 forward-test diagnostics — mirror PaperTrader wiring.
# The blend-mode pipeline does NOT route through PaperTrader.process_signal
# (the adapter short-circuits in blend mode), so we wire the recorder
# directly into the runner to keep signal_stats.jsonl up to date.
from signal_engine.signal_stats import SignalRecord, SignalStatsRecorder

logger = logging.getLogger("ayumi.forward_test")

_DEFAULT_STATS_LOG_PATH = "data/signal_stats.jsonl"


class BlendForwardTestRunner:
    """Production forward test runner wiring the full Ayumi signal pipeline.

    Wires: confidence engine → profile router → position sizer → orchestrator.
    Handles state persistence across restarts via StatePersistence.

    Supports registering ISignalStrategy instances (e.g. TTCXAUUSDStrategy)
    which are evaluated on each bar and routed through the blend pipeline.
    """

    """Production forward test runner wiring the full Ayumi signal pipeline.

    Wires: confidence engine → profile router → position sizer → orchestrator.
    Handles state persistence across restarts via StatePersistence.
    """

    def __init__(self, config: dict):
        """
        config keys:
        - account_balance: float
        - risk_per_trade_pct: float (default 0.005)
        - daily_risk_cap_pct: float (default 0.03)
        - max_sniper: int (default 3)
        - max_swarm: int (default 5)
        - spread_pips: dict per symbol
        - atr_cache_path: str
        - state_path: str
        - log_level: str
        - stats_log_path: str (default ``data/signal_stats.jsonl``) —
          JSONL destination for the runner-attached signal-stats recorder.
          Override (typically for tests) by passing ``stats_log_path`` in
          the config dict or by setting the ``STATS_LOG_PATH`` env var.
        """
        self._config = config
        self._balance = config["account_balance"]

        # Setup logging
        setup_logging(level=config.get("log_level", "INFO"))

        # Phase 0 forward-test diagnostics: the SignalStatsRecorder mirrors
        # what PaperTrader does in non-blend mode. The blend-mode signal
        # pipeline does NOT route through PaperTrader (the adapter short-
        # circuits in blend mode and returns the trade signal directly),
        # so without this wiring the JSONL file goes silent the moment
        # blend_mode=True is enabled. Lazy-init keeps unit tests that
        # never invoke on_signal() free of file-system side effects.
        self._stats_log_path: str = (
            config.get("stats_log_path") or os.environ.get("STATS_LOG_PATH") or _DEFAULT_STATS_LOG_PATH
        )
        self._stats_recorder: SignalStatsRecorder | None = None

        # Build pipeline components
        spread_pips = config.get("spread_pips", {})
        gate_config = GateConfig(
            default_max_spread=2.0,
            symbol_max_spreads=spread_pips,
        )
        self._engine = ConfidenceEngine(gate_config)

        self._router = ProfileRouter(
            sniper_threshold=0.70,
            swarm_threshold=0.40,
            max_sniper=config.get("max_sniper", 3),
            max_swarm=config.get("max_swarm", 5),
        )

        self._sizer = SLPositionSizer(
            account_balance=self._balance,
            risk_per_trade_pct=config.get("risk_per_trade_pct", 0.005),
            daily_risk_cap_pct=config.get("daily_risk_cap_pct", 0.03),
        )

        self._adapter = StrategyAdapter()
        self._orchestrator = SignalOrchestrator(
            confidence_engine=self._engine,
            profile_router=self._router,
            position_sizer=self._sizer,
            account_balance=self._balance,
        )

        # Phase 4: Regime-aware risk sizing
        self._regime_thresholds = RegimeAwareThresholds()
        self._current_regime: Regime = Regime.STABLE
        self._regime_history: list = []

        # Phase 4.2: Edge telemetry (R-multiple expectancy per strategy × symbol)
        self._edge_tracker = EdgeTelemetryTracker()

        # State persistence
        self._persistence = StatePersistence(
            state_path=config.get("state_path", "data/risk_state.json"),
        )

        # Registered strategies (ISignalStrategy instances)
        self._strategies: list = []

        # Track open positions for fill handling
        self._open_positions: dict[str, dict] = {}
        self._current_day: Optional[str] = None

        # Phase 6A: position_id → signal_id mapping for close() wiring.
        # Populated by register_position_mapping() when the engine links
        # a PaperTrader position_id to the blend_runner's signal_id.
        self._position_id_to_signal_id: dict[str, str] = {}
        self._signal_id_to_position_id: dict[str, str] = {}
        self._unmapped_signal_ids: deque[str] = deque()
        self._mapped_signal_ids: set[str] = set()

        # The engine normally creates the PaperTrader after this runner.  A
        # caller may still bind one explicitly (or pass it in config) so a
        # missing Position can be mirrored at the existing mapping boundary.
        self._paper_trader = config.get("paper_trader")

    def get_edge_tracker(self) -> EdgeTelemetryTracker:
        """Return the EdgeTelemetryTracker instance for this runner.

        Card d8c2a10b: the launch script's periodic health loop calls
        ``write_state_snapshot()`` on the tracker to reconcile the canonical
        observability path (data/edge_telemetry_state.json) with the
        operational cadence. Returning the live instance avoids the loader
        from re-reading the trade JSONL and prevents double-counting.
        """
        return self._edge_tracker

    # ------------------------------------------------------------------
    # Phase 0 forward-test diagnostics (signal_stats wiring)
    # ------------------------------------------------------------------
    def _get_stats_recorder(self) -> SignalStatsRecorder:
        """Lazy-init the SignalStatsRecorder on first use.

        Mirrors :meth:`PaperTrader._get_stats_recorder` (non-blend path).
        Kept off the constructor so existing tests that don't touch the
        stats log don't pay any startup cost and so the runner can be
        instantiated before the first signal without leaving a half-
        initialised file on disk. The recorder opens with ``append``
        semantics via :meth:`SignalStatsRecorder._append_line` (atomic
        temp + ``os.replace``) so a simulated clean process restart that
        re-instantiates against the same ``log_path`` will append cleanly
        rather than truncate.
        """
        if self._stats_recorder is None:
            self._stats_recorder = SignalStatsRecorder(
                log_path=self._stats_log_path,
            )
        return self._stats_recorder

    def start(self) -> None:
        """Initialize all components, restore state, start logging."""
        self._persistence.restore(self._sizer)
        self._balance = self._sizer.account_balance
        self._orchestrator.update_balance(self._balance)
        logger.info(
            "BlendForwardTestRunner started — balance=$%.2f",
            self._balance,
        )

    # ── Broker reconciliation (card 0e0338d4) ─────────────────────────

    def reconcile_with_broker(self, broker_positions) -> dict:
        """Nuke-and-rebuild sizer ``_open_positions`` from broker truth.

        Fixes the phantom-positions bug where the sizer reported
        ``positions_carried=16`` while cTrader only had 4 actual fills.
        Delegates to :meth:`SLPositionSizer.reconcile_with_broker`.

        Accepts any iterable of position-like objects (the engine
        passes the list returned by ``OpenApiSpotFeed.reconcile()``).

        Returns the reconciliation result dict (before/after counts,
        divergence flag, etc). Callers (launch script) log it and may
        use it for health endpoints.
        """
        result = self._sizer.reconcile_with_broker(broker_positions)
        # Mirror the new open_risk into the orchestrator's view of the
        # account — the sizer is the source of truth for open_risk, so
        # any downstream consumer that caches the old value must refresh.
        try:
            self._orchestrator.update_balance(self._sizer.account_balance)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(
                "reconcile_with_broker: orchestrator.update_balance failed (non-fatal): %s",
                exc,
            )
        return result

    def _check_daily_reset(self, timestamp: datetime) -> None:
        """Reset daily risk cap on day boundaries.

        Uses the Toronto date (FTMO spec) — not the timestamp's naive date — so
        that the daily budget rolls over at America/Toronto midnight regardless
        of which timezone the timestamp is recorded in.
        """
        trading_day = _trading_date(timestamp)
        if self._current_day and trading_day != self._current_day:
            pre_daily = self._sizer._daily_risk_used
            pre_open = self._sizer.open_risk
            positions_carried = len(self._sizer.open_positions)
            self._sizer.reset_daily(cet_date=trading_day)
            logger.info(
                "Daily risk cap reset — new trading day: %s (daily_used=%.2f→0.00, "
                "open_risk=%.2f, positions_carried=%d)",
                trading_day,
                pre_daily,
                pre_open,
                positions_carried,
            )
        self._current_day = trading_day

    def daily_reset(self, now: Optional[datetime] = None) -> None:
        """Public daily reset — safe to call from engine scheduler.

        Logs pre-reset and post-reset values so operators can verify
        the daily counter was zeroed while open positions are carried over.
        Uses the Toronto date (FTMO spec) so the rollover boundary matches
        :meth:`_check_daily_reset` and ``FTMOGuard``.
        """
        sizer = self._sizer
        pre_daily = sizer._daily_risk_used
        pre_open = sizer.open_risk
        positions_carried = len(sizer.open_positions)
        if now is None:
            now = datetime.now(timezone.utc)
        trading_day = _trading_date(now)
        sizer.reset_daily(cet_date=trading_day)
        self._current_day = trading_day
        logger.info(
            "Daily risk reset: daily_used=%.2f→0.00, open_risk=%.2f, positions_carried=%d, trading_day=%s",
            pre_daily,
            pre_open,
            positions_carried,
            trading_day,
        )

    def make_signal_id(self, signal: OrchestratorTradeSignal) -> str:
        """Build the canonical signal_id used by on_signal() to register
        a position with the sizer.

        Public helper so callers (launcher, engine, scripts) can use
        the SAME identity for cancel/close without duplicating the
        construction pattern.  Previously each caller built the id
        independently and could drift out of sync.
        """
        return signal.strategy_id + "_" + str(signal.timestamp.timestamp())

    def register_strategy(self, strategy) -> None:
        """Register an ISignalStrategy (e.g. TTCXAUUSDStrategy) for evaluation.

        Registered strategies are evaluated on each bar via evaluate_bars().
        When a strategy produces a signal, it is routed through on_signal().
        """
        self._strategies.append(strategy)
        logger.info(
            "Strategy registered: %s",
            getattr(strategy, "name", type(strategy).__name__),
        )

    def evaluate_bars(self, bars: list, latest_bar=None) -> list:
        """Evaluate all registered strategies on the latest bar data.

        Args:
            bars: List of bar objects (must have .time, .open, .high, .low, .close).
            latest_bar: The most recent bar (optional, defaults to bars[-1]).

        Returns:
            List of OrchestratedOrder results for signals that were generated.
        """
        orders = []
        if not self._strategies:
            return orders

        bar = latest_bar or (bars[-1] if bars else None)
        if not bar:
            return orders

        # Phase 4: Update market regime from recent bars
        self.update_regime(bars)

        # Build a simple state object that strategies can evaluate
        state = type(
            "BarState",
            (),
            {
                "bars": bars,
                "latest_bar": bar,
                "symbol": getattr(bar, "symbol", "XAUUSD"),
            },
        )()

        for strategy in self._strategies:
            try:
                result = strategy.evaluate(state)
            except Exception as exc:
                logger.warning(
                    "Strategy %s raised during evaluate: %s",
                    getattr(strategy, "name", "?"),
                    exc,
                )
                continue

            if result is None:
                continue

            # Convert strategy result to signal_data dict for on_signal
            signal_data = self._strategy_result_to_dict(result, bar)
            if signal_data is None:
                continue

            strategy_id = getattr(strategy, "name", strategy.__class__.__name__)
            order = self.on_signal(strategy_id, signal_data)
            orders.append(order)

        return orders

    @staticmethod
    def _strategy_result_to_dict(result, bar) -> dict | None:
        """Convert a strategy evaluate() result to a signal_data dict.

        Handles common result types: dict, dataclass, or object with attributes.
        """
        if result is None:
            return None

        # Extract spread from bar (core.types.Bar has spread_pips)
        bar_spread = getattr(bar, "spread_pips", 0.0)

        if isinstance(result, dict):
            # Ensure spread is present; bar value is the fallback
            if "spread" not in result:
                result["spread"] = bar_spread
            return result

        # Try dataclass-style or object attribute access
        as_dict = {}
        for key in (
            "symbol",
            "direction",
            "entry_price",
            "stop_loss",
            "take_profit",
            "confidence",
        ):
            val = getattr(result, key, None)
            if val is not None:
                as_dict[key] = val

        # Fall back to bar values for required fields
        as_dict.setdefault("symbol", getattr(bar, "symbol", "XAUUSD"))
        as_dict.setdefault("direction", getattr(result, "direction", "LONG"))
        as_dict.setdefault("entry_price", getattr(bar, "close", 0.0))
        as_dict.setdefault("stop_loss", getattr(result, "stop_loss", 0.0))
        as_dict.setdefault("take_profit", getattr(result, "take_profit", 0.0))
        as_dict.setdefault("confidence", getattr(result, "confidence", 0.5))

        # Always include spread so downstream gates receive actual data
        as_dict.setdefault("spread", bar_spread)

        if not as_dict.get("entry_price") or not as_dict.get("stop_loss"):
            return None

        return as_dict

    def update_regime(self, bars: list) -> None:
        """Classify current market regime from recent bars (ATR-based heuristic).

        Simple volatility-based detection:
        - Compute recent range (high-low) as proxy for ATR
        - Compare to rolling mean: if current > 2x mean → BREAKDOWN
        - If between 1x and 2x → TRANSITION
        - Below 1x → STABLE
        """
        if len(bars) < 20:
            return  # Not enough data

        recent = bars[-20:]
        ranges = [getattr(b, "high", 0) - getattr(b, "low", 0) for b in recent]
        if not ranges or max(ranges) == 0:
            return

        current_range = ranges[-1]
        avg_range = sum(ranges) / len(ranges)
        if avg_range == 0:
            return

        ratio = current_range / avg_range
        if ratio >= 2.0:
            new_regime = Regime.BREAKDOWN
        elif ratio >= 1.5:
            new_regime = Regime.TRANSITION
        else:
            new_regime = Regime.STABLE

        if new_regime != self._current_regime:
            logger.info(
                "Regime shift: %s → %s (range ratio %.2f)",
                self._current_regime.value,
                new_regime.value,
                ratio,
            )
            self._current_regime = new_regime
            self._regime_history.append((new_regime.value, ratio))

    def on_signal(self, strategy_id: str, signal_data: dict) -> OrchestratedOrder:
        """Handle incoming strategy signal through full pipeline."""
        signal = self._adapter.adapt_signal(strategy_id, signal_data)
        self._check_daily_reset(signal.timestamp)
        order = self._orchestrator.process_signal(signal)

        if not order.rejected:
            # Phase 4: Apply regime-aware exposure multiplier
            exposure_mult = self._regime_thresholds.get_exposure_multiplier(self._current_regime)

            # Phase 4.2: Apply edge-based risk multiplier
            edge_mult = self._edge_tracker.get_risk_multiplier(strategy_id, signal.symbol)

            combined_mult = exposure_mult * edge_mult
            if combined_mult < 1.0:
                order.risk_amount *= combined_mult
                order.lots *= combined_mult
                logger.info(
                    "Regime+edge sizing: %s %.0f%% × %.1f%% = %.0f%% exposure (risk=$%.2f lots=%.4f)",
                    self._current_regime.value,
                    exposure_mult * 100,
                    edge_mult * 100,
                    combined_mult * 100,
                    order.risk_amount,
                    order.lots,
                )
            elif edge_mult > 1.0:
                order.risk_amount *= edge_mult
                order.lots *= edge_mult
                logger.info(
                    "Edge sizing: %.1fx multiplier (high edge, risk=$%.2f lots=%.4f)",
                    edge_mult,
                    order.risk_amount,
                    order.lots,
                )

            # Register position tracking under a unique signal_id
            signal_id = self.make_signal_id(signal)
            self._open_positions[signal_id] = {
                "order": order,
                "risk_amount": order.risk_amount,
            }
            self._sizer.register(signal_id, order.risk_amount)
            self._queue_signal_for_mapping(signal_id)
            logger.info(
                "Order accepted: %s %s %.4f lots risk=$%.2f",
                signal.symbol,
                signal.direction,
                order.lots,
                order.risk_amount,
            )
            # Phase 0 forward-test diagnostics: write the open row to the
            # JSONL signal-stats log. This closes the writer-silence bug
            # that left ``data/signal_stats.jsonl`` untouched in blend mode
            # (the cTrader adapter short-circuits before PaperTrader in
            # blend mode, so without this hook no caller ever invokes
            # ``record_signal``). Failures here are non-fatal — the trade
            # itself is already accepted and registered.
            try:
                recorder = self._get_stats_recorder()
                recorder.record_signal(
                    SignalRecord(
                        signal_id=signal_id,
                        timestamp=signal.timestamp.isoformat() if signal.timestamp else "",
                        strategy=strategy_id,
                        symbol=signal.symbol,
                        direction=str(signal.direction),
                        confidence=float(signal.confidence),
                        rationale_tags=[signal.metadata.get("rationale", "")]
                        if isinstance(signal.metadata, dict)
                        else [],
                        confluence_score=0.0,
                        lots=float(order.lots),
                        entry_price=float(signal.entry_price),
                        sl_price=float(signal.stop_loss),
                        tp_price=float(getattr(signal, "take_profit", 0.0)),
                    )
                )
            except Exception as _stats_exc:  # noqa: BLE001
                logger.warning(
                    "Signal-stats record_signal failed (non-fatal): %s",
                    _stats_exc,
                )
        else:
            logger.info(
                "Order rejected: %s — %s",
                signal.symbol,
                order.rejection_reason,
            )
            # Phase 0 forward-test diagnostics: write the rejection row to
            # the JSONL signal-stats log. Mirrors the rejection branch in
            # ``_execute_signal_live`` so the aggregator's rejection_rate
            # is non-zero whenever the blend runner rejects a signal.
            try:
                recorder = self._get_stats_recorder()
                signal_id = self.make_signal_id(signal)
                recorder.record_rejection(
                    signal_id=signal_id,
                    rejection_reason=str(order.rejection_reason or ""),
                    error_code="",
                )
            except Exception as _stats_exc:  # noqa: BLE001
                logger.warning(
                    "Signal-stats record_rejection failed (non-fatal): %s",
                    _stats_exc,
                )

        return order

    def cancel_risk(self, signal_id: str, risk_amount: float) -> None:
        """Free risk budget when a sized order is rejected downstream."""
        resolved_signal_id = self._resolve_registered_signal_id(signal_id)
        self._forget_signal_mapping(resolved_signal_id)
        self._sizer.cancel(resolved_signal_id)
        logger.info(
            "Risk cancelled: signal_id=%s risk=$%.2f freed, daily remaining=$%.2f",
            resolved_signal_id,
            risk_amount,
            self._sizer.daily_risk_remaining,
        )

    def bind_paper_trader(self, paper_trader) -> None:
        """Bind the PaperTrader used by the blend execution path.

        The normal engine creates the PaperTrader after the runner, so this
        optional binding keeps the mapping boundary usable for tests and for
        callers that construct the runner in a different order.
        """
        self._paper_trader = paper_trader

    def _queue_signal_for_mapping(self, signal_id: str) -> None:
        """Queue one accepted signal whose execution identity is not known yet."""
        if signal_id in self._mapped_signal_ids or signal_id in self._unmapped_signal_ids:
            return
        self._unmapped_signal_ids.append(signal_id)

    def _forget_signal_mapping(self, signal_id: str) -> None:
        """Remove one signal's execution mapping after cancel/close."""
        self._mapped_signal_ids.discard(signal_id)
        self._signal_id_to_position_id.pop(signal_id, None)
        try:
            self._unmapped_signal_ids.remove(signal_id)
        except ValueError:
            pass

    def _resolve_registered_signal_id(self, signal_id: str) -> str:
        """Resolve a display/default alias to its registered canonical ID.

        The launcher may pass the strategy display name (for example,
        ``"TTC XAUUSD M15_<timestamp>"``) to ``cancel_risk``.  The blend
        runner stores the normalized ID, so a close or cancel must perform the
        same identity repair at its boundary instead of leaking risk.
        """
        identity = str(signal_id)
        if identity in self._open_positions:
            return identity
        try:
            sizer_open = self._sizer.open_positions
        except AttributeError:
            return identity
        if isinstance(sizer_open, dict) and identity in sizer_open:
            return identity
        try:
            requested_timestamp = float(identity.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            return identity

        matches: list[str] = []
        for candidate in self._unmapped_signal_ids:
            if not isinstance(sizer_open, dict) or candidate not in sizer_open:
                continue
            try:
                candidate_timestamp = float(candidate.rsplit("_", 1)[-1])
            except (TypeError, ValueError):
                continue
            if abs(candidate_timestamp - requested_timestamp) <= 1.0:
                matches.append(candidate)
        if len(matches) == 1:
            return matches[0]
        return identity

    def _resolve_unmapped_signal_id(self) -> str | None:
        """Return the oldest accepted signal that can back a synthetic ID.

        Paper-mode callbacks have historically carried a default timestamp
        identity, while the sizer uses the blend runner's canonical signal ID.
        Keep the resolution deterministic and prefer a signal that is still
        reserved in the sizer, so cancelled/rejected orders cannot be mapped.
        """
        pending = [sid for sid in self._unmapped_signal_ids if sid in self._open_positions]
        if not pending:
            return None

        try:
            sizer_open = self._sizer.open_positions
        except AttributeError:
            sizer_open = {}
        if isinstance(sizer_open, dict) and sizer_open:
            for signal_id in pending:
                if signal_id in sizer_open and signal_id not in self._mapped_signal_ids:
                    return signal_id
        if len(pending) == 1 and not sizer_open:
            return pending[0]
        return None

    def _bind_position_mapping(self, position_id: str, signal_id: str) -> None:
        """Persist both directions of a resolved position/signal mapping."""
        old_position_id = self._signal_id_to_position_id.get(signal_id)
        if old_position_id is not None and old_position_id != position_id:
            self._position_id_to_signal_id.pop(old_position_id, None)
        self._position_id_to_signal_id[position_id] = signal_id
        self._signal_id_to_position_id[signal_id] = position_id
        self._mapped_signal_ids.add(signal_id)
        try:
            self._unmapped_signal_ids.remove(signal_id)
        except ValueError:
            pass

    @staticmethod
    def _resolve_mirror_direction(
        signal,
        TradeDirectionCls,
        SignalDirectionCls,
    ):
        """Map an orchestrator signal's direction to a TradeDirection.

        The orchestrator's ``OrchestratorTradeSignal.direction`` is the
        uppercase string ``"LONG"`` or ``"SHORT"`` (see
        ``strategy_adapter.adapt_signal``).  Older adapters also emit the
        ``signal_validator.Direction`` enum.  A blind
        ``str(signal.direction).upper()`` round-trip into ``TradeDirection``
        always raised ``ValueError`` because ``TradeDirection`` is a
        ``StrEnum`` with lowercase values (``"long"``/``"short"``); the
        previous fallback silently coerced every SHORT into LONG, reversing
        the order-manager's SL/TP semantics and the eventual PnL sign.

        Returns the matching ``TradeDirection`` member, or ``None`` when the
        direction cannot be interpreted (callers must then skip the mirror).
        """
        raw = getattr(signal, "direction", None)
        if raw is None:
            return None
        # Direct TradeDirection (e.g. tests that pass the enum in directly).
        if isinstance(raw, TradeDirectionCls):
            if raw == TradeDirectionCls.NEUTRAL:
                return None
            return raw
        # signal_validator.Direction enum — compare by enum identity.
        if isinstance(raw, SignalDirectionCls):
            if raw == SignalDirectionCls.SHORT:
                return TradeDirectionCls.SHORT
            if raw == SignalDirectionCls.LONG:
                return TradeDirectionCls.LONG
            return None
        # String form — uppercase or lowercase, possibly with surrounding
        # whitespace.  Anything else is treated as unparseable.
        if isinstance(raw, str):
            normalized = raw.strip().upper()
            if normalized in {"LONG", "BUY"}:
                return TradeDirectionCls.LONG
            if normalized in {"SHORT", "SELL"}:
                return TradeDirectionCls.SHORT
            return None
        return None

    def _mirror_position_to_order_manager(self, position_id: str, signal_id: str) -> None:
        """Mirror a blend-accepted order when the paper position is absent.

        PaperTrader normally creates the Position before its callback reaches
        this method.  The defensive branch preserves the established
        close-on-SL/TP behavior for alternate construction orders and keeps
        the order-manager state in the same shape as a normal paper fill.
        """
        paper_trader = self._paper_trader
        if paper_trader is None:
            return
        order_manager = getattr(paper_trader, "_order_manager", None)
        positions = getattr(order_manager, "_positions", None)
        if not isinstance(positions, dict) or position_id in positions:
            return

        tracked = self._open_positions.get(signal_id)
        if not isinstance(tracked, dict):
            return
        order = tracked.get("order")
        signal = getattr(order, "signal", None)
        if order is None or signal is None:
            return

        from adapters.ctrader.models import Position, TradeDirection
        from signal_validator import Direction as SignalDirection

        direction = self._resolve_mirror_direction(signal, TradeDirection, SignalDirection)
        if direction is None:
            logger.warning(
                "Mirrored blend position %s has unparseable direction=%r; "
                "skipping order-manager mirror to avoid reversed SL/TP semantics",
                position_id,
                getattr(signal, "direction", None),
            )
            return

        volume = float(getattr(order, "lots", 0.0) or 0.0)
        entry_price = float(getattr(signal, "entry_price", 0.0) or 0.0)
        if volume <= 0 or entry_price <= 0:
            return

        position = Position(
            position_id=position_id,
            symbol=str(signal.symbol),
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=getattr(signal, "stop_loss", None),
            take_profit=getattr(signal, "take_profit", None),
        )
        lock = getattr(order_manager, "_lock", None)
        if lock is None:
            positions[position_id] = position
        else:
            with lock:
                positions[position_id] = position
        logger.info(
            "Mirrored blend position %s to PaperTrader order manager for signal_id=%s",
            position_id,
            signal_id,
        )

    def register_position_mapping(
        self,
        position_id: str,
        signal_id: str,
        paper_trader=None,
    ) -> None:
        """Map an execution position to its canonical blend signal ID.

        The callback may supply a synthetic/default identity (for example, a
        paper ``CTraderTradeSignal`` timestamp created after the blend signal).
        In that case, resolve the oldest still-open accepted blend order before
        recording the position mapping.  A bound PaperTrader is used only to
        restore a missing order-manager Position; existing fills are never
        overwritten.
        """
        if paper_trader is not None:
            self.bind_paper_trader(paper_trader)

        raw_signal_id = str(signal_id)
        resolved_signal_id = raw_signal_id
        if raw_signal_id not in self._open_positions:
            resolved_signal_id = self._resolve_unmapped_signal_id()
        if resolved_signal_id is None:
            logger.warning(
                "Could not resolve blend signal_id for position_id=%s; callback_identity=%s",
                position_id,
                raw_signal_id,
            )
            self._position_id_to_signal_id[position_id] = raw_signal_id
            return

        self._bind_position_mapping(position_id, resolved_signal_id)
        self._mirror_position_to_order_manager(position_id, resolved_signal_id)

    def on_fill(self, order_id: str, fill_price: float, pnl: float) -> None:
        """Handle position fill/close — update sizer state and persist.

        Args:
            order_id: The signal_id used when registering the position
                      (``strategy_id + "_" + timestamp``).
            fill_price: Closing price (used for logging only).
            pnl: Realized PnL from the close.
        """
        order_id = self._resolve_registered_signal_id(order_id)
        pos = self._open_positions.pop(order_id, None)

        try:
            self._sizer.close(order_id, pnl)
        except KeyError:
            if pos is not None:
                # Restore the popped entry so a retry can find it
                self._open_positions[order_id] = pos
            logger.warning("on_fill: signal_id %s not registered in sizer", order_id)
            return

        # The close completed, so release the execution mapping only after
        # sizer.close() succeeds. A retry can then use the restored runner entry.
        self._forget_signal_mapping(order_id)

        self._balance = self._sizer.account_balance
        self._orchestrator.update_balance(self._balance)

        symbol = pos["order"].signal.symbol if pos else "unknown"
        strategy_id = pos["order"].signal.strategy_id if pos else "unknown"
        risk_amount = pos["risk_amount"] if pos else 0.0
        logger.info(
            "Position closed: signal_id=%s symbol=%s pnl=%.2f open_risk=%.2f",
            order_id,
            symbol,
            pnl,
            self._sizer.open_risk,
        )

        # Phase 4.2: Record edge telemetry
        if strategy_id != "unknown" and risk_amount > 0:
            self._edge_tracker.record_close(
                strategy_id=strategy_id,
                symbol=symbol,
                risk_amount=risk_amount,
                pnl=pnl,
                signal_id=order_id,
            )

        # Phase 0 forward-test diagnostics: write the close row to the
        # JSONL signal-stats log so the aggregator's hit_rate, avg_pips,
        # and avg_time_to_close have outcome data to consume. Mirrors
        # ``PaperTrader.close_position``. Failures are non-fatal — the
        # sizer + telemetry update is already done by this point.
        try:
            recorder = self._get_stats_recorder()
            outcome = "tp_hit" if pnl > 0 else "sl_hit"
            # Coarse time_to_close: if the orchestrator metadata carries
            # an opened_at we use it; otherwise emit 0 (the dashboard
            # tolerates 0 and shows it as "unknown").
            opened_at = None
            closed_at = datetime.now(timezone.utc)
            if pos and isinstance(pos.get("order"), OrchestratedOrder):
                _sig = getattr(pos["order"], "signal", None)
                if _sig and getattr(_sig, "timestamp", None):
                    opened_at = _sig.timestamp
            time_to_close = 0
            if opened_at is not None:
                try:
                    time_to_close = max(
                        0,
                        int((closed_at - opened_at).total_seconds()),
                    )
                except Exception:  # noqa: BLE001
                    time_to_close = 0
            recorder.record_outcome(
                signal_id=order_id,
                outcome=outcome,
                pips=float(pnl),
                time_to_close=int(time_to_close),
            )
        except Exception as _stats_exc:  # noqa: BLE001
            logger.warning(
                "Signal-stats record_outcome failed (non-fatal): %s",
                _stats_exc,
            )

        # Persist state after fill
        self._persistence.save(self._sizer)

    def close_position(self, position_id: str, pnl: float) -> None:
        """Close a position by its position_id, resolving the signal_id.

        This is the primary close entry point for the engine, which tracks
        positions by their PaperTrader-assigned ``position_id``.  The
        mapping from ``position_id`` to ``signal_id`` is established via
        :meth:`register_position_mapping` when the trade is first opened.

        Falls back to using ``position_id`` directly as the signal_id if
        no mapping exists (backward-compat for tests / direct callers).
        """
        signal_id = self._position_id_to_signal_id.pop(position_id, None)
        if signal_id is None:
            # No mapping registered — fall back to using position_id as-is
            signal_id = position_id

        signal_id = self._resolve_registered_signal_id(signal_id)
        self.on_fill(signal_id, fill_price=0.0, pnl=pnl)

    def stop(self) -> None:
        """Persist state and shutdown cleanly."""
        self._persistence.save(self._sizer)
        logger.info("BlendForwardTestRunner stopped — balance=$%.2f", self._balance)

    def health_snapshot(self) -> dict:
        """Return a dict of key blend-runner metrics for health reporting.

        Consumed by the launch script's ``write_forward_test_health_json``
        and Hayate's daily audit so downstream readers get a single,
        consistent snapshot rather than reaching into private attrs.

        Keys:
          - open_positions: count of currently-open positions
          - open_risk: total dollar risk across open positions
          - daily_risk_used: risk consumed today
          - daily_risk_remaining: risk budget left today
          - balance: current account balance
          - total_closed: lifetime closed positions (via sizer)
        """
        sizer = self._sizer
        return {
            "open_positions": len(sizer.open_positions),
            "open_risk": round(sizer.open_risk, 2),
            "daily_risk_used": round(sizer._daily_risk_used, 2),
            "daily_risk_remaining": round(sizer.daily_risk_remaining, 2),
            "balance": round(self._balance, 2),
            "total_closed": getattr(sizer, "_total_closed", 0),
        }
