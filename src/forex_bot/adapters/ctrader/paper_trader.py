import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, Optional

# Phase 0 forward-test diagnostics — see signal_engine/signal_stats.py
from signal_engine.signal_stats import SignalRecord, SignalStatsRecorder

from .kill_switch import KillSwitchManager
from .models import CTraderTradeSignal, Order, Position, TradeDirection
from .order_manager import OrderExecutionResult, OrderManager, PositionSizeConfig
from .risk_guard import FTMOConfig, RiskGuard

if TYPE_CHECKING:
    from .api_client import cTraderAPIClient


logger = logging.getLogger(__name__)


@dataclass
class PaperTradeResult:
    success: bool
    signal: CTraderTradeSignal
    order: Order | None = None
    position: Position | None = None
    rejection_reason: str = ""
    risk_guard_result: Any | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    slippage_applied: float = 0.0


@dataclass
class PaperTradingStats:
    total_signals_processed: int = 0
    trades_executed: int = 0
    trades_rejected: int = 0
    signals_blocked_by_risk: int = 0
    current_balance: float = 100000.0
    starting_balance: float = 100000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


class PaperTrader:
    def __init__(
        self,
        ftmo_config: FTMOConfig | None = None,
        position_config: PositionSizeConfig | None = None,
        starting_balance: float = 100000.0,
        api_client: Optional["cTraderAPIClient"] = None,
        state_path: str | None = None,
        stats_log_path: str | None = None,
    ):
        self._ftmo_config = ftmo_config or FTMOConfig()
        self._position_config = position_config or PositionSizeConfig()
        self._api_client = api_client
        self._live_mode_enabled = api_client is not None and not api_client.is_paper_mode
        self._order_manager = OrderManager(self._position_config, api_client=api_client)
        self._risk_guard = RiskGuard(
            self._ftmo_config,
            starting_balance,
            state_path=state_path or "data/state/risk_guard_state.json",
        )
        self._starting_balance = starting_balance
        self._current_balance = starting_balance
        self._lock = RLock()
        self._stats = PaperTradingStats(starting_balance=starting_balance, current_balance=starting_balance)
        self._trade_history: list[PaperTradeResult] = []
        self._callbacks: list[tuple[str, Callable]] = []
        self._running = False
        self._last_update: datetime | None = None
        self._kill_switch = KillSwitchManager()
        # Phase 0: lazy-initialised signal-stats recorder and the
        # position_id -> signal_id correlation map. Populated when a
        # trade opens, consumed when the position closes.
        self._stats_recorder: SignalStatsRecorder | None = None
        self._stats_log_path: str = stats_log_path or "data/signal_stats.jsonl"
        self._position_signal_id: dict[str, str] = {}

    @property
    def is_live_mode(self) -> bool:
        return self._live_mode_enabled

    def process_signal(
        self,
        signal: CTraderTradeSignal,
        spread: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> PaperTradeResult:
        # Council audit (FTMO-risk): risk check and order execution must be
        # atomic. The entire method body is under self._lock (RLock), which
        # serializes risk-guard checks → kill-switch gate → order submission.
        # No separate lock acquisition exists between check and execute.
        with self._lock:
            self._stats.total_signals_processed += 1

            risk_result = self._risk_guard.check_signal(signal)
            if not risk_result.allowed:
                self._stats.signals_blocked_by_risk += 1
                logger.warning(f"Signal blocked by risk guard: {risk_result.message}")
                return PaperTradeResult(
                    success=False,
                    signal=signal,
                    rejection_reason=risk_result.message,
                    risk_guard_result=risk_result,
                )

            # Phase 0 (card 047cd91d): when the caller (typically
            # ``BlendForwardTestRunner`` wrapping an ``OrchestratedOrder``
            # into a ``CTraderTradeSignal``) already supplied a sized
            # ``volume``, treat it as canonical and skip the
            # ``OrderManager.calculate_position_size`` recomputation.
            # The previous path always recomputed, which produced a 2×
            # lot mismatch for SWARM-profile signals (orchestrator: 0.05
            # lots at half risk, paper trade: 0.10 lots at full risk).
            supplied_volume = getattr(signal, "volume", None)
            if supplied_volume is not None and float(supplied_volume) > 0.0:
                volume = float(supplied_volume)
                logger.info(
                    "[PAPER] Using orchestrator-sized volume=%.4f (skipping recompute)",
                    volume,
                )
            else:
                volume = self._order_manager.calculate_position_size(
                    self._current_balance,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.symbol,
                )

            trade_check = self._risk_guard.check_trade_allowed(
                direction=signal.direction,
                volume=volume,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit_1,
                account_balance=self._current_balance,
                symbol=signal.symbol,
            )
            if not trade_check.allowed:
                self._stats.signals_blocked_by_risk += 1
                logger.warning(f"Trade blocked by risk: {trade_check.message}")
                return PaperTradeResult(
                    success=False,
                    signal=signal,
                    rejection_reason=trade_check.message,
                    risk_guard_result=trade_check,
                )

            # Kill switch second gate — blocks order execution even if signal passed evaluation
            if self._kill_switch.is_globally_killed():
                logger.warning(
                    "Order blocked by kill switch: %s",
                    self._kill_switch.get_status().get("reason", "active"),
                )
                return PaperTradeResult(
                    success=False,
                    signal=signal,
                    rejection_reason="kill_switch_active",
                )

            trade_result = self._execute_order(
                signal=signal,
                volume=volume,
                spread=spread,
                bid=bid,
                ask=ask,
            )

            if trade_result.success:
                self._stats.trades_executed += 1
                position = trade_result.position
                if position:
                    self._current_balance += position.unrealized_pnl
                    self._stats.current_balance = self._current_balance
                    self._risk_guard.update_balance(self._current_balance)

                result = PaperTradeResult(
                    success=True,
                    signal=signal,
                    order=trade_result.order,
                    position=position,
                    risk_guard_result=risk_result,
                    slippage_applied=trade_result.slippage_applied,
                )
                self._trade_history.append(result)
                logger.info(
                    f"[PAPER] Executed: {signal.direction.value} {volume} {signal.symbol} @ {signal.entry_price}"
                )
                # Phase 0: record the open line for the signal-stats log
                # and remember the correlation between this position and
                # the signal id so the close hook in close_position() can
                # write the matching outcome row.
                try:
                    recorder = self._get_stats_recorder()
                    position_id = (
                        position.position_id
                        if position
                        else (trade_result.order.order_id if trade_result.order else "")
                    )
                    signal_id = position_id or signal.strategy_id or ""
                    if position_id and signal_id:
                        self._position_signal_id[position_id] = signal_id
                    if signal_id:
                        recorder.record_signal(
                            SignalRecord(
                                signal_id=signal_id,
                                timestamp=signal.timestamp.isoformat() if signal.timestamp else "",
                                strategy=signal.strategy_id or "unknown",
                                symbol=signal.symbol,
                                direction="BUY" if signal.direction == TradeDirection.LONG else "SELL",
                                confidence=float(signal.confidence),
                                rationale_tags=[signal.rationale] if signal.rationale else [],
                                confluence_score=0.0,
                                lots=float(volume),
                                entry_price=float(signal.entry_price),
                                sl_price=float(signal.stop_loss),
                                tp_price=float(signal.take_profit_1),
                            )
                        )
                except Exception as _stats_exc:  # noqa: BLE001
                    logger.warning("Signal-stats record_signal failed (non-fatal): %s", _stats_exc)
                self._trigger_callback("on_trade_executed", result)
            else:
                self._stats.trades_rejected += 1
                result = PaperTradeResult(
                    success=False,
                    signal=signal,
                    rejection_reason=trade_result.rejection_reason
                    or trade_result.error_message
                    or "Order execution failed",
                    risk_guard_result=risk_result,
                )
                logger.warning(
                    "PaperTrader rejected %s %s %s: rejection_reason=%s error=%s",
                    signal.symbol,
                    signal.direction.value,
                    volume,
                    trade_result.rejection_reason,
                    trade_result.error_message,
                )
                self._trade_history.append(result)

            return result

    def _execute_order(
        self,
        signal: CTraderTradeSignal,
        volume: float,
        spread: float = 0.0,
        bid: float = 0.0,
        ask: float = 0.0,
    ) -> OrderExecutionResult:
        # Sprint Task 1.4: forward TP2/TP3 to OrderManager so the resulting
        # Position has all three TP levels populated. OrderManager methods
        # accept take_profit_2/take_profit_3 as optional kwargs (default None)
        # so signals with only TP1 defined continue to work.
        if self.is_live_mode:
            return self._order_manager.execute_live_order(
                symbol=signal.symbol,
                direction=signal.direction,
                volume=volume,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit_1,
                take_profit_2=signal.take_profit_2,
                take_profit_3=signal.take_profit_3,
                comment=signal.rationale,
            )
        return self._order_manager.execute_paper_order(
            symbol=signal.symbol,
            direction=signal.direction,
            volume=volume,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            comment=signal.rationale,
            spread=spread,
            bid=bid,
            ask=ask,
        )

    def set_api_client(self, api_client: Optional["cTraderAPIClient"]):
        self._api_client = api_client
        self._live_mode_enabled = api_client is not None and not api_client.is_paper_mode
        self._order_manager.set_api_client(api_client)

    def update_market_prices(self, prices: dict, bids: dict | None = None, asks: dict | None = None):
        bids = bids or {}
        asks = asks or {}
        with self._lock:
            total_unrealized = 0.0
            # Snapshot open positions before updating — update_position may
            # close some of them via SL/TP, and we need to detect that.
            open_positions = self._order_manager.get_open_positions()
            for position in open_positions:
                if position.symbol in prices:
                    current_price = prices[position.symbol]
                    # Forward bid/ask verbatim from the caller. Do NOT fall
                    # back to current_price here: OrderManager._check_stop_loss_hit
                    # treats a zero/missing bid (LONG) or ask (SHORT) as a
                    # "no quote" sentinel and skips the SL evaluation rather
                    # than firing on the bar mid. Synthesising bid=ask=
                    # current_price caused false SL triggers when only a mid
                    # price was supplied (card 9f051898 — bid/ask side-selection
                    # fix). The previous mid-fallback lived under card 9310bdd0
                    # and addressed a different symptom (silently skipped
                    # checks); the SL guard now distinguishes missing-quote
                    # from real-side-crossed correctly.
                    bid = bids.get(position.symbol, 0)
                    ask = asks.get(position.symbol, 0)

                    self._order_manager.update_position(position.position_id, current_price, bid=bid, ask=ask)
                    updated_pos = self._order_manager.get_position(position.position_id)
                    if updated_pos and updated_pos.status.is_closed:
                        # Position was auto-closed by SL/TP inside
                        # OrderManager.update_position.  Propagate the
                        # realized P&L and risk-guard update that would
                        # normally happen in PaperTrader.close_position().
                        self._stats.realized_pnl += updated_pos.closed_pnl
                        is_win = updated_pos.closed_pnl > 0
                        self._risk_guard.record_trade(updated_pos.closed_pnl, is_win)

                        # Phase 0: signal-stats close record.
                        try:
                            signal_id = self._position_signal_id.pop(updated_pos.position_id, updated_pos.position_id)
                            if signal_id:
                                outcome = self._map_close_reason_to_outcome(
                                    reason=getattr(updated_pos, "close_reason", "") or "",
                                    position_status=getattr(updated_pos.status, "value", ""),
                                    is_win=is_win,
                                )
                                opened_at = getattr(updated_pos, "opened_at", None)
                                closed_at = getattr(updated_pos, "closed_at", None) or datetime.utcnow()
                                time_to_close = 0
                                if opened_at is not None:
                                    time_to_close = max(
                                        0,
                                        int((closed_at - opened_at).total_seconds()),
                                    )
                                self._get_stats_recorder().record_outcome(
                                    signal_id=signal_id,
                                    outcome=outcome,
                                    pips=float(updated_pos.closed_pnl),
                                    time_to_close=time_to_close,
                                )
                        except Exception as _stats_exc:  # noqa: BLE001
                            logger.warning(
                                "Signal-stats record_outcome failed (non-fatal): %s",
                                _stats_exc,
                            )

                        self._trigger_callback("on_position_closed", updated_pos)
                        logger.info(f"[PAPER] Auto-closed: {updated_pos.symbol} PnL: {updated_pos.closed_pnl:.2f}")
                    elif updated_pos:
                        total_unrealized += updated_pos.unrealized_pnl

            self._stats.unrealized_pnl = total_unrealized
            self._current_balance = self._starting_balance + self._stats.realized_pnl + total_unrealized
            self._stats.current_balance = self._current_balance
            self._risk_guard.update_balance(self._current_balance)
            self._last_update = datetime.utcnow()

    def close_position(self, position_id: str, exit_price: float, reason: str = "manual") -> bool:
        with self._lock:
            position = self._order_manager.close_position(position_id, exit_price, reason)
            if position:
                self._stats.realized_pnl += position.closed_pnl
                # NOTE: Do NOT add closed_pnl to _current_balance here.
                # _current_balance was set by the last update_market_prices()
                # call to (starting_balance + realized_pnl + total_unrealized),
                # which already included this position's unrealised P&L.  Adding
                # closed_pnl here double-counts.  The next update_market_prices()
                # tick will recompute _current_balance correctly with the updated
                # realized_pnl and the reduced total_unrealized (position removed).
                self._stats.current_balance = self._current_balance

                is_win = position.closed_pnl > 0
                self._risk_guard.record_trade(position.closed_pnl, is_win)

                # Phase 0: write the close row to the signal-stats log
                # so hit-rate / avg-pips / time-to-close have outcome
                # data to aggregate. The signal id was remembered when
                # the position opened (see process_signal). Failures
                # here are non-fatal: the trade itself is already done.
                try:
                    signal_id = self._position_signal_id.pop(position_id, position_id)
                    if signal_id:
                        # Map position_status / close reason to one of
                        # the SignalStatsRecorder outcome tokens. The
                        # paper trader doesn't have a direct TP/SL hit
                        # signal at this layer — we infer from the
                        # reason field set by the order manager.
                        outcome = self._map_close_reason_to_outcome(
                            reason=reason,
                            position_status=getattr(position.status, "value", ""),
                            is_win=is_win,
                        )
                        # Best-effort time-to-close calculation.
                        opened_at = getattr(position, "opened_at", None)
                        closed_at = getattr(position, "closed_at", None) or datetime.utcnow()
                        time_to_close = 0
                        if opened_at is not None:
                            time_to_close = max(
                                0,
                                int((closed_at - opened_at).total_seconds()),
                            )
                        # Pips realised (best effort). We don't have
                        # pip_size here; emit the raw PnL divided by
                        # volume as a coarse proxy and let the
                        # aggregator / dashboard refine it later.
                        pips = position.closed_pnl
                        self._get_stats_recorder().record_outcome(
                            signal_id=signal_id,
                            outcome=outcome,
                            pips=float(pips),
                            time_to_close=int(time_to_close),
                        )
                except Exception as _stats_exc:  # noqa: BLE001
                    logger.warning(
                        "Signal-stats record_outcome failed (non-fatal): %s",
                        _stats_exc,
                    )

                self._trigger_callback("on_position_closed", position)
                logger.info(f"[PAPER] Closed: {position.symbol} @ {exit_price}, PnL: {position.closed_pnl:.2f}")
                return True
            return False

    def close_all_positions(self, exit_price: float, reason: str = "force_close"):
        with self._lock:
            positions = self._order_manager.get_open_positions()
            for position in positions:
                self.close_position(position.position_id, exit_price, reason)

    def get_open_positions(self) -> list[Position]:
        return self._order_manager.get_open_positions()

    def get_stats(self) -> PaperTradingStats:
        with self._lock:
            return PaperTradingStats(
                total_signals_processed=self._stats.total_signals_processed,
                trades_executed=self._stats.trades_executed,
                trades_rejected=self._stats.trades_rejected,
                signals_blocked_by_risk=self._stats.signals_blocked_by_risk,
                current_balance=self._current_balance,
                starting_balance=self._starting_balance,
                realized_pnl=self._stats.realized_pnl,
                unrealized_pnl=self._stats.unrealized_pnl,
            )

    def get_risk_guard_stats(self) -> dict:
        return self._risk_guard.get_stats()

    def reset(self):
        with self._lock:
            self._current_balance = self._starting_balance
            self._stats = PaperTradingStats(
                starting_balance=self._starting_balance,
                current_balance=self._starting_balance,
            )
            self._trade_history.clear()
            self._risk_guard.reset_circuit_breaker()
            self._risk_guard.reset_daily_tracking()
            logger.info("[PAPER] Trading reset")

    def clear_stuck_positions(self) -> int:
        with self._lock:
            positions = self._order_manager.get_open_positions()
            count = len(positions)
            for position in positions:
                exit_price = position.current_price or position.entry_price
                self._order_manager.close_position(position.position_id, exit_price, "synthetic_cleanup")
                closed = self._order_manager.get_position(position.position_id)
                if closed and closed.status.is_closed:
                    self._stats.realized_pnl += closed.closed_pnl
            self._current_balance = self._starting_balance + self._stats.realized_pnl
            self._stats.current_balance = self._current_balance
            logger.info(f"[PAPER] Cleared {count} stuck position(s)")
            return count

    def register_callback(self, event: str, callback: Callable):
        if event not in ["on_trade_executed", "on_position_closed"]:
            raise ValueError(f"Unknown event: {event}")
        self._callbacks.append((event, callback))

    def _get_stats_recorder(self) -> SignalStatsRecorder:
        """Lazy-init the SignalStatsRecorder on first use.

        Kept off the hot path (constructor) so existing tests that
        don't touch the stats log don't pay any startup cost and so
        the forward-test process can be killed before the first
        signal is recorded without leaving a half-initialised file.
        """
        if self._stats_recorder is None:
            self._stats_recorder = SignalStatsRecorder(log_path=self._stats_log_path)
        return self._stats_recorder

    @staticmethod
    def _map_close_reason_to_outcome(
        reason: str,
        position_status: str,
        is_win: bool,
    ) -> str:
        """Map a close reason / status to a SignalStatsRecorder outcome.

        Falls back to ``manual_close`` when the cause is ambiguous so
        we never silently drop a close. The order manager usually sets
        the reason to one of ``tp_hit`` / ``sl_hit`` / ``timeout_close``
        / ``synthetic_cleanup`` — pass those through where they match.
        """
        reason_lc = (reason or "").lower()
        status_lc = (position_status or "").lower()
        # Direct reason hits first (highest fidelity).
        for token in ("tp_hit", "sl_hit", "manual_close", "expired", "timeout_close"):
            if token in reason_lc:
                return token
        # Map position status values that survived as enum strings.
        if "tp" in status_lc:
            return "tp_hit"
        if "sl" in status_lc:
            return "sl_hit"
        if "timeout" in status_lc:
            return "timeout_close"
        # Infer from PnL when nothing more specific is available.
        return "tp_hit" if is_win else "sl_hit"

    def _trigger_callback(self, event: str, *args, **kwargs):
        for evt, callback in self._callbacks:
            if evt == event:
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Callback error for {event}: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def balance(self) -> float:
        return self._current_balance
