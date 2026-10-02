from __future__ import annotations

from typing import TYPE_CHECKING

from core.config import BacktestConfig, BacktestMetrics
from core.spread import RealisticSpreadModel, SpreadModel
from core.types import Bar, MarketState, SimulatedTrade, StrategySignal
from engine.base import EngineCore, determine_session
from engine.mixins import CombinedSignalMixin, CombineMethod, ProgressiveSLMixin
from engine.trade_mgmt import TradeManagementMixin
from policy.behavioral import BehavioralPolicy
from policy.kill_criteria import KillCriteriaChecker

if TYPE_CHECKING:
    from core.protocol import IStrategy

    try:
        from backtest.trade_management import TradeManagementConfig
    except ImportError:
        TradeManagementConfig = None

    try:
        from quant.config import QuantConfig
        from quant.pipeline import QuantPipeline
    except ImportError:
        QuantConfig = None
        QuantPipeline = None


class BacktestEngine(EngineCore, ProgressiveSLMixin, TradeManagementMixin, CombinedSignalMixin):
    def __init__(
        self,
        config: BacktestConfig,
        strategies: list["IStrategy"],
        trade_mgmt_config: "TradeManagementConfig | None" = None,
        quant_config: "QuantConfig | None" = None,
        spread_model: SpreadModel | RealisticSpreadModel | None = None,
    ):
        EngineCore.__init__(self, config, spread_model)
        ProgressiveSLMixin.__init__(self, config)
        TradeManagementMixin.__init__(self, trade_mgmt_config)
        self.strategies = strategies
        self._quant_pipeline = None

        # Phase 3 parity: kill criteria + behavioral policy (matches live
        # ForwardTestEngine wiring, commit 679245c).
        self._kill_criteria_checker = KillCriteriaChecker(global_config={"max_spread_bps": 2.0})
        self._behavioral_policy = BehavioralPolicy()
        self._consecutive_losses: int = 0
        self._pending_lot_multiplier: float = 1.0

        if quant_config is not None:
            from quant.config import QuantConfig as QC
            from quant.pipeline import QuantPipeline

            if not isinstance(quant_config, QC):
                raise TypeError(f"Expected QuantConfig, got {type(quant_config).__name__}")
            self._quant_pipeline = QuantPipeline(quant_config)

    def run_single(self, strategy: "IStrategy", bars: list[Bar]) -> BacktestMetrics:
        if len(bars) < self.config.min_bars_before_signal:
            raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

        self._reset()
        trades: list[SimulatedTrade] = []
        equity_curve = [self.balance]
        open_trades: list[SimulatedTrade] = []

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)
            self._update_bar_spread(bar)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            trades_before = len(trades)
            self._check_open_trades(open_trades, bar, i, trades, equity_curve)
            self._update_loss_streak(trades, trades_before)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bar.time),
                )
                signal = strategy.evaluate(state)

                if signal is not None and self._passes_filters(signal):
                    signal = self._apply_policy_gates(signal, bar, strategy.name)
                    if signal is None:
                        self.rejected_signals += 1
                        continue
                    trade = self._open_trade(signal, bar, i)
                    if trade is not None:
                        if self._pending_lot_multiplier < 1.0:
                            trade.lot_size *= self._pending_lot_multiplier
                        open_trades.append(trade)
                    else:
                        self.rejected_signals += 1
                    self._pending_lot_multiplier = 1.0

            equity_curve.append(self.balance)

        trades.extend(self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close))
        return self._calculate_metrics(trades, equity_curve)

    def run_all(self, bars: list[Bar]) -> dict[str, BacktestMetrics]:
        results: dict[str, BacktestMetrics] = {}
        for strategy in self.strategies:
            metrics = self.run_single(strategy, bars)
            results[strategy.name] = metrics
        return results

    def run_combined(
        self,
        bars: list[Bar],
        method: CombineMethod = CombineMethod.WEIGHTED,
    ) -> BacktestMetrics:
        if len(bars) < self.config.min_bars_before_signal:
            raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

        self._reset()
        trades: list[SimulatedTrade] = []
        equity_curve = [self.balance]
        open_trades: list[SimulatedTrade] = []

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)
            self._update_bar_spread(bar)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            trades_before = len(trades)
            self._check_open_trades(open_trades, bar, i, trades, equity_curve)
            self._update_loss_streak(trades, trades_before)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bar.time),
                )

                all_signals: list[StrategySignal] = []
                for strategy in self.strategies:
                    signal = strategy.evaluate(state)
                    if signal is not None:
                        all_signals.append(signal)

                if all_signals:
                    combined = self._combine_signals(
                        all_signals,
                        method=method,
                        min_confidence=self.config.min_confidence,
                    )
                    if combined is not None:
                        combined = self._apply_policy_gates(combined, bar, "combined")
                        if combined is None:
                            self.rejected_signals += 1
                            continue
                        trade = self._open_trade(combined, bar, i)
                        if trade is not None:
                            if self._pending_lot_multiplier < 1.0:
                                trade.lot_size *= self._pending_lot_multiplier
                            open_trades.append(trade)
                        else:
                            self.rejected_signals += 1
                        self._pending_lot_multiplier = 1.0
                    else:
                        self.rejected_signals += 1

            equity_curve.append(self.balance)

        trades.extend(self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close))
        return self._calculate_metrics(trades, equity_curve)

    def _passes_filters(self, signal: StrategySignal) -> bool:
        return signal.confidence >= self.config.min_confidence

    # ------------------------------------------------------------------
    # Phase 3 parity: policy gates (kill criteria + behavioral sizing)
    # ------------------------------------------------------------------

    def _apply_policy_gates(
        self,
        signal: StrategySignal,
        bar: Bar,
        strategy_name: str,
    ) -> StrategySignal | None:
        """Apply kill criteria and behavioral policy gates.

        Returns the signal if it passes kill criteria, or None if killed.
        The behavioral multiplier is stored on ``self._pending_lot_multiplier``
        for the caller to apply to the resulting trade's ``lot_size`` after
        ``_open_trade`` returns.

        Note: ``StrategySignal`` in the backtest engine has no ``volume`` or
        ``symbol`` fields (unlike the live engine's signal type). We adapt
        by using ``self.config.pair`` for symbol and applying the multiplier
        to ``trade.lot_size`` post-open rather than ``signal.volume``.
        """
        self._pending_lot_multiplier = 1.0

        # ── Kill criteria gate ──────────────────────────────────────
        hour_utc = bar.time.hour if hasattr(bar.time, "hour") else 0
        spread_bps = float(getattr(self, "_current_spread", 0.0) or 0.0)
        kc_context = {
            "symbol": self.config.pair,
            "spread_bps": spread_bps,
            "hour_utc": hour_utc,
            "adx": 0.0,  # ADX not available in backtest
            "confluence_score": 0.0,
            "strategy_name": strategy_name,
        }
        kc_results = self._kill_criteria_checker.check(kc_context)
        if KillCriteriaChecker.any_triggered(kc_results):
            return None

        # ── Behavioral policy sizing ─────────────────────────────────
        daily_dd_pct = 0.0
        daily_start = float(getattr(self, "daily_start_balance", 0.0) or 0.0)
        if daily_start > 0:
            daily_dd_pct = max(0.0, (daily_start - self.balance) / daily_start * 100)
        bp_context = {
            "consecutive_losses": self._consecutive_losses,
            "daily_drawdown_pct": daily_dd_pct,
        }
        bp_result = self._behavioral_policy.evaluate(0.0, bp_context)
        self._pending_lot_multiplier = bp_result.multiplier

        return signal

    def _update_loss_streak(
        self,
        trades: list[SimulatedTrade],
        trades_before: int,
    ) -> None:
        """Update ``_consecutive_losses`` from newly closed trades.

        Called after ``_check_open_trades`` on each bar. Compares the
        trades list length before/after to detect newly closed positions
        and updates the loss streak accordingly.
        """
        for t in trades[trades_before:]:
            if t.profit_loss < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0
