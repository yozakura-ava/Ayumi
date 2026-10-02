import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .engine import (
    BacktestConfig,
    BacktestMetrics,
    Bar,
    ExitReason,
    MarketState,
    SimulatedTrade,
    StrategyBacktestResult,
    StrategySignal,
    TradeDirection,
    TradeOutcome,
    determine_session,
)
from .strategies import ISignalStrategy
from .trade_management import (
    ManagedTrade,
    TradeAction,
    TradeManagementConfig,
    TradeManager,
)

if TYPE_CHECKING:
    from quant.config import QuantConfig
    from quant.pipeline import QuantPipeline


@dataclass
class EnhancedTradeRecord:
    entry_bar_index: int
    exit_bar_index: int
    direction: TradeDirection
    entry_price: float
    exit_price: float
    lot_size: float
    outcome: TradeOutcome
    exit_reason: ExitReason
    entry_time: datetime
    exit_time: datetime
    confidence_score: float
    rationale: str
    profit_loss: float
    pips: float
    partial_closes: list = field(default_factory=list)
    partial_realized_pnl: float = 0.0

    def to_simulated_trade(self, risk_amount: float = 0.0) -> SimulatedTrade:
        return SimulatedTrade(
            entry_bar_index=self.entry_bar_index,
            exit_bar_index=self.exit_bar_index,
            direction=self.direction,
            entry_price=self.entry_price,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            take_profit_3=0.0,
            exit_price=self.exit_price,
            lot_size=self.lot_size,
            risk_amount=risk_amount,
            pips=self.pips,
            profit_loss=self.profit_loss,
            outcome=self.outcome,
            exit_reason=self.exit_reason,
            entry_time=self.entry_time,
            exit_time=self.exit_time,
            confidence_score=self.confidence_score,
            confluence_count=1,
            rationale=self.rationale,
            partial_closed=len(self.partial_closes) > 0,
            partial_close_price=self.partial_closes[0]["price"] if self.partial_closes else 0.0,
            partial_close_pnl=self.partial_realized_pnl,
        )


class EnhancedBacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        strategies: list[ISignalStrategy],
        tm_config: TradeManagementConfig | None = None,
        quant_config: Optional["QuantConfig"] = None,
    ):
        self.config = config
        self.strategies = strategies
        self.tm_config = tm_config or TradeManagementConfig()
        self.trade_manager = TradeManager(self.tm_config)
        self._quant_pipeline: "QuantPipeline | None" = None
        if quant_config is not None:
            from quant.config import QuantConfig as QC
            from quant.pipeline import QuantPipeline

            if not isinstance(quant_config, QC):
                raise TypeError(f"Expected QuantConfig, got {type(quant_config).__name__}")
            self._quant_pipeline = QuantPipeline(quant_config)

    def run_strategy(self, strategy: ISignalStrategy, bars: list[Bar]) -> BacktestMetrics:
        if len(bars) < self.config.min_bars_before_signal:
            raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

        self._reset()
        trade_records: list[EnhancedTradeRecord] = []
        equity_curve = [self.balance]
        open_trades: list[ManagedTrade] = []
        rejected_signals = 0

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)

            if self._quant_pipeline is not None:
                atr = self._calculate_atr(bars, i)
                self._quant_pipeline.update_bars(bar.high, bar.low, bar.close, atr)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            self._process_open_trades(open_trades, bar, i, bars, trade_records, equity_curve)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                state = MarketState(
                    bars=bars[: i + 1],
                    current_session=determine_session(bar.time),
                )
                signal = strategy.evaluate(state)
                if signal is not None and signal.confidence >= self.config.min_confidence:
                    if self._quant_pipeline is not None:
                        quant_decision = self._quant_pipeline.pre_trade_check(
                            signal_symbol=self.tm_config.pair,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss,
                            bar_time=bar.time,
                        )
                        from quant.pipeline import TradeAction as QuantTradeAction

                        if quant_decision.action == QuantTradeAction.REJECT:
                            rejected_signals += 1
                            equity_curve.append(self.balance)
                            continue
                    else:
                        quant_decision = None

                    atr = state.atr
                    entry_allowed = self.trade_manager.check_entry_allowed(
                        bar,
                        signal,
                        atr,
                        self.config.effective_spread_pips,
                        self.tm_config.pair,
                    )
                    if entry_allowed.allow_entry:
                        lot_override = (
                            quant_decision.lot_size
                            if quant_decision is not None and quant_decision.lot_size is not None
                            else None
                        )

                        trade = self._open_trade(signal, bar, i, lot_size_override=lot_override)
                        if trade is not None:
                            open_trades.append(trade)
                    else:
                        rejected_signals += 1

            equity_curve.append(self.balance)

        closed = self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close)
        trade_records.extend(closed)
        return self._calculate_metrics(trade_records, equity_curve, rejected_signals)

    def run_all_strategies(self, bars: list[Bar]) -> dict[str, StrategyBacktestResult]:
        results = {}
        for strategy in self.strategies:
            metrics = self.run_strategy(strategy, bars)
            results[strategy.name] = StrategyBacktestResult(
                strategy_name=strategy.name,
                metrics=metrics,
                last_signal=None,
            )
        return results

    def run_ab_comparison(self, strategy: ISignalStrategy, bars: list[Bar]) -> tuple[BacktestMetrics, BacktestMetrics]:
        from .multi_strategy_engine import MultiStrategyBacktestEngine

        baseline_engine = MultiStrategyBacktestEngine(self.config, [strategy])
        baseline_result = baseline_engine._run_single_strategy(strategy, bars)
        enhanced_metrics = self.run_strategy(strategy, bars)

        return baseline_result.metrics, enhanced_metrics

    def _reset(self):
        self.balance = self.config.starting_balance
        self.peak_balance = self.config.starting_balance
        self.max_drawdown = 0.0
        self.max_daily_loss = 0.0
        self.current_day = None
        self.daily_start_balance = self.config.starting_balance
        self.total_spread_cost = 0.0
        self.total_commission_cost = 0.0

    def _update_daily_tracking(self, bar_time: datetime):
        day = bar_time.date()
        if self.current_day is None:
            self.current_day = day
            self.daily_start_balance = self.balance
        elif day != self.current_day:
            daily_loss = self.daily_start_balance - self.balance
            if daily_loss > self.max_daily_loss:
                self.max_daily_loss = daily_loss
            self.current_day = day
            self.daily_start_balance = self.balance

    def _is_max_drawdown_breached(self) -> bool:
        drawdown_pct = (self.peak_balance - self.balance) / self.peak_balance
        return drawdown_pct >= self.config.max_total_drawdown_pct

    def _is_max_daily_loss_breached(self) -> bool:
        daily_loss_pct = (self.daily_start_balance - self.balance) / self.daily_start_balance
        return daily_loss_pct >= self.config.max_daily_drawdown_pct

    def _open_trade(
        self,
        signal: StrategySignal,
        bar: Bar,
        bar_index: int,
        lot_size_override: float | None = None,
    ) -> ManagedTrade | None:
        risk_amount = self.balance * self.config.risk_per_trade_pct
        risk = abs(signal.entry_price - signal.stop_loss)
        if risk == 0:
            return None

        pip_value = self._get_pip_value(signal.entry_price)
        spread_cost = self.config.spread_pips * pip_value
        slippage_cost = self.config.slippage_pips * pip_value
        total_cost = spread_cost + slippage_cost
        effective_entry = (
            signal.entry_price + total_cost
            if signal.direction == TradeDirection.LONG
            else signal.entry_price - total_cost
        )
        adjusted_risk = abs(effective_entry - signal.stop_loss)
        if adjusted_risk == 0:
            return None

        lot_size = risk_amount / adjusted_risk
        if lot_size_override is not None:
            lot_size = lot_size_override
        margin_required = lot_size * effective_entry / self.config.leverage
        if margin_required > self.balance:
            return None

        max_lot_size = self.balance * self.config.leverage / effective_entry
        lot_size = min(lot_size, max_lot_size)

        adjusted_signal = StrategySignal(
            direction=signal.direction,
            confidence=signal.confidence,
            entry_price=effective_entry,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            rationale=signal.rationale,
        )
        return self.trade_manager.open_trade(bar_index, bar, adjusted_signal, lot_size)

    def _process_open_trades(
        self,
        open_trades: list[ManagedTrade],
        bar: Bar,
        bar_index: int,
        all_bars: list[Bar],
        trade_records: list[EnhancedTradeRecord],
        equity_curve: list[float],
    ):
        to_close = []

        for trade in open_trades:
            atr = self._calculate_atr(all_bars, bar_index)
            recent_bars = all_bars[max(0, bar_index - 20) : bar_index + 1]
            result = self.trade_manager.on_bar(trade, bar, bar_index, atr, recent_bars)

            if result.action == TradeAction.CLOSE_FULL:
                pnl = self._calculate_pnl(trade, result.exit_price, trade.remaining_pct, bar.time)
                pnl += trade.partial_realized_pnl
                self.balance += pnl

                if self._quant_pipeline is not None:
                    self._quant_pipeline.on_trade_closed(pnl)

                record = EnhancedTradeRecord(
                    entry_bar_index=trade.entry_bar_index,
                    exit_bar_index=bar_index,
                    direction=trade.direction,
                    entry_price=trade.entry_price,
                    exit_price=result.exit_price,
                    lot_size=trade.lot_size,
                    outcome=self._determine_outcome(pnl),
                    exit_reason=result.reason or ExitReason.STOP_LOSS,
                    entry_time=trade.entry_time,
                    exit_time=bar.time,
                    confidence_score=trade.confidence_score,
                    rationale=trade.rationale,
                    profit_loss=pnl,
                    pips=self._calculate_pips(trade, result.exit_price),
                    partial_closes=list(trade.partial_closes),
                    partial_realized_pnl=trade.partial_realized_pnl,
                )
                trade_records.append(record)
                to_close.append(trade)
                self._update_peak_and_drawdown()
                equity_curve.append(self.balance)

            elif result.action == TradeAction.CLOSE_PARTIAL:
                partial_pnl = self._calculate_pnl(trade, result.exit_price, result.close_pct, bar.time)
                trade.partial_realized_pnl += partial_pnl
                self.balance += partial_pnl
                self._update_peak_and_drawdown()
                equity_curve.append(self.balance)

            elif result.action == TradeAction.MODIFY_SL:
                pass

        for t in to_close:
            open_trades.remove(t)

    def _close_all_open_trades(
        self,
        open_trades: list[ManagedTrade],
        bar_index: int,
        exit_time: datetime,
        exit_price: float,
    ) -> list[EnhancedTradeRecord]:
        records = []
        for trade in open_trades:
            pnl = self._calculate_pnl(trade, exit_price, trade.remaining_pct, exit_time)
            pnl += trade.partial_realized_pnl
            self.balance += pnl

            record = EnhancedTradeRecord(
                entry_bar_index=trade.entry_bar_index,
                exit_bar_index=bar_index,
                direction=trade.direction,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                lot_size=trade.lot_size,
                outcome=self._determine_outcome(pnl),
                exit_reason=ExitReason.END_OF_DATA,
                entry_time=trade.entry_time,
                exit_time=exit_time,
                confidence_score=trade.confidence_score,
                rationale=trade.rationale,
                profit_loss=pnl,
                pips=0.0,
                partial_closes=list(trade.partial_closes),
                partial_realized_pnl=trade.partial_realized_pnl,
            )
            records.append(record)
            self._update_peak_and_drawdown()
        open_trades.clear()
        return records

    def _calculate_pnl(
        self,
        trade: ManagedTrade,
        exit_price: float,
        position_pct: float,
        exit_time: datetime | None = None,
    ) -> float:
        pip_value = self._get_pip_value(trade.entry_price)
        standard_lots = trade.lot_size / self.config.units_per_lot
        spread_pips = self.config.effective_spread_pips
        commission_cost = standard_lots * self.config.commission_per_lot * position_pct
        self.total_commission_cost += commission_cost

        if self.config.round_trip_spread:
            spread_price = spread_pips * pip_value
            if trade.direction == TradeDirection.LONG:
                exit_price -= spread_price
            else:
                exit_price += spread_price
            spread_dollars = spread_pips * pip_value * trade.lot_size * position_pct
            self.total_spread_cost += spread_dollars

        slippage_price = self.config.slippage_pips * pip_value
        if trade.direction == TradeDirection.LONG:
            exit_price -= slippage_price
        else:
            exit_price += slippage_price

        holding_days = 0
        if exit_time is not None and trade.entry_time is not None:
            holding_days = (exit_time.date() - trade.entry_time.date()).days
        if holding_days > 0 and self.config.swap_per_lot_per_day != 0.0:
            swap_cost = standard_lots * self.config.swap_per_lot_per_day * holding_days * position_pct
        else:
            swap_cost = 0.0

        if trade.direction == TradeDirection.LONG:
            pips = (exit_price - trade.entry_price) / pip_value
        else:
            pips = (trade.entry_price - exit_price) / pip_value

        pnl = pips * standard_lots * pip_value * self.config.units_per_lot * position_pct
        pnl -= commission_cost
        pnl += swap_cost
        return pnl

    def _calculate_pips(self, trade: ManagedTrade, exit_price: float) -> float:
        pip_value = self._get_pip_value(trade.entry_price)
        if trade.direction == TradeDirection.LONG:
            return (exit_price - trade.entry_price) / pip_value
        else:
            return (trade.entry_price - exit_price) / pip_value

    @staticmethod
    def _determine_outcome(pnl: float) -> TradeOutcome:
        if pnl > 0.01:
            return TradeOutcome.WIN
        elif pnl < -0.01:
            return TradeOutcome.LOSS
        return TradeOutcome.BREAKEVEN

    def _update_peak_and_drawdown(self):
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        drawdown = (self.peak_balance - self.balance) / self.peak_balance
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def _calculate_metrics(
        self,
        trade_records: list[EnhancedTradeRecord],
        equity_curve: list[float],
        rejected_signals: int,
    ) -> BacktestMetrics:
        trades = [r.to_simulated_trade() for r in trade_records]
        metrics = BacktestMetrics(
            starting_balance=self.config.starting_balance,
            ending_balance=self.balance,
            total_pnl=self.balance - self.config.starting_balance,
            total_pnl_pct=(self.balance - self.config.starting_balance) / self.config.starting_balance,
            win_rate=0.0,
            total_trades=len(trades),
            winning_trades=sum(1 for t in trades if t.outcome == TradeOutcome.WIN),
            losing_trades=sum(1 for t in trades if t.outcome == TradeOutcome.LOSS),
            breakeven_trades=sum(1 for t in trades if t.outcome == TradeOutcome.BREAKEVEN),
            avg_win=0.0,
            avg_loss=0.0,
            largest_win=0.0,
            largest_loss=0.0,
            profit_factor=0.0,
            max_drawdown_pct=self.max_drawdown * 100,
            max_drawdown_dollar=self.max_drawdown * self.peak_balance,
            max_daily_loss_dollar=self.max_daily_loss,
            sharpe_ratio=0.0,
            avg_risk_reward=0.0,
            expectancy=0.0,
            avg_holding_bars=0.0,
            equity_curve=equity_curve,
            trades=trades,
            total_spread_cost=self.total_spread_cost,
            total_commission_cost=self.total_commission_cost,
            rejected_signals=rejected_signals,
        )

        if len(trades) > 0:
            wins = [t for t in trades if t.outcome == TradeOutcome.WIN]
            losses = [t for t in trades if t.outcome == TradeOutcome.LOSS]

            metrics.win_rate = metrics.winning_trades / len(trades) * 100
            metrics.avg_win = sum(t.profit_loss for t in wins) / len(wins) if wins else 0
            metrics.avg_loss = sum(t.profit_loss for t in losses) / len(losses) if losses else 0
            metrics.largest_win = max(t.profit_loss for t in wins) if wins else 0
            metrics.largest_loss = min(t.profit_loss for t in losses) if losses else 0

            total_wins = sum(t.profit_loss for t in wins)
            total_losses = abs(sum(t.profit_loss for t in losses))
            metrics.profit_factor = (
                total_wins / total_losses if total_losses > 0 else total_wins if total_wins > 0 else 0
            )

            metrics.avg_risk_reward = abs(metrics.avg_win / metrics.avg_loss) if metrics.avg_loss != 0 else 0
            metrics.expectancy = (metrics.win_rate / 100 * metrics.avg_win) - (
                (1 - metrics.win_rate / 100) * abs(metrics.avg_loss)
            )
            metrics.avg_holding_bars = sum(t.exit_bar_index - t.entry_bar_index for t in trades) / len(trades)

        metrics.sharpe_ratio = self._calculate_sharpe_ratio(equity_curve)
        return metrics

    def _calculate_sharpe_ratio(self, equity_curve: list[float]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        returns = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] != 0:
                returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        if not returns:
            return 0.0
        mean_return = sum(returns) / len(returns)
        std_dev = math.sqrt(sum((r - mean_return) ** 2 for r in returns) / len(returns))
        if std_dev == 0:
            return 999.0 if mean_return > 0 else 0.0
        return (mean_return / std_dev) * math.sqrt(252)

    @staticmethod
    def _get_pip_value(price: float) -> float:
        if price >= 50:
            return 0.01
        elif price >= 1:
            return 0.0001
        else:
            return 0.00000001

    @staticmethod
    def _calculate_atr(bars: list[Bar], current_index: int) -> float:
        lookback = min(15, current_index + 1)
        if lookback < 2:
            return 0.0001
        tr_sum = 0.0
        count = 0
        for i in range(current_index - lookback + 1, current_index + 1):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
                count += 1
        return tr_sum / count if count > 0 else 0.0001
