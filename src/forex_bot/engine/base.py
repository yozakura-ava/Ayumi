from __future__ import annotations

import math
from datetime import date, datetime

from core.config import BacktestConfig, BacktestMetrics
from core.pip import PipCalculator
from core.spread import RealisticSpreadModel, SpreadModel
from core.types import (
    Bar,
    ExitReason,
    SimulatedTrade,
    StrategySignal,
    TradeDirection,
    TradeOutcome,
)

UNITS_PER_LOT = 100_000.0


def determine_session(time: datetime) -> str:
    from datetime import timezone

    if time.tzinfo is not None:
        time = time.astimezone(timezone.utc)
    hour = time.hour
    if 0 <= hour < 6:
        return "asian"
    if 8 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "ny_am"
    if 16 <= hour < 20:
        return "ny_pm"
    return "outside"


class EngineCore:
    def __init__(
        self,
        config: BacktestConfig,
        spread_model: SpreadModel | RealisticSpreadModel | None = None,
    ):
        self.config = config
        self.spread_model = spread_model or SpreadModel(config.spread_pips, config.slippage_pips)
        self._reset()

    def _reset(self) -> None:
        self.balance = self.config.starting_balance
        self.peak_balance = self.balance
        self.max_drawdown = 0.0
        self.current_day: date | None = None
        self.daily_start_balance = self.balance
        self.max_daily_loss = 0.0
        self.total_spread_cost = 0.0
        self.total_commission_cost = 0.0
        self.rejected_signals = 0

    def _update_daily_tracking(self, bar_time: datetime) -> None:
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

    def _update_bar_spread(self, bar: Bar) -> None:
        if isinstance(self.spread_model, RealisticSpreadModel):
            self.spread_model.set_bar_spread(bar.spread_pips)

    def _is_max_drawdown_breached(self) -> bool:
        if self.peak_balance <= 0:
            return False
        drawdown_pct = (self.peak_balance - self.balance) / self.peak_balance
        return drawdown_pct >= self.config.max_total_drawdown_pct

    def _is_max_daily_loss_breached(self) -> bool:
        if self.daily_start_balance <= 0:
            return False
        daily_loss_pct = (self.daily_start_balance - self.balance) / self.daily_start_balance
        return daily_loss_pct >= self.config.max_daily_drawdown_pct

    def _close_trade(
        self,
        trade: SimulatedTrade,
        bar_index: int,
        exit_time: datetime,
        exit_price: float,
        reason: ExitReason,
    ) -> None:
        trade.exit_bar_index = bar_index
        trade.exit_time = exit_time
        trade.exit_reason = reason

        pip_value = PipCalculator.pip_value(trade.entry_price)
        standard_lots = trade.lot_size / UNITS_PER_LOT
        commission_cost = standard_lots * self.config.commission_per_lot
        self.total_commission_cost += commission_cost

        spread_dollars = (self.spread_model.spread_pips + self.spread_model.slippage_pips) * pip_value * trade.lot_size
        self.total_spread_cost += spread_dollars

        trade.exit_price = exit_price

        holding_days = 0
        if trade.entry_time is not None:
            holding_days = (exit_time.date() - trade.entry_time.date()).days
        if holding_days > 0 and self.config.swap_per_lot_per_day != 0.0:
            swap_cost = standard_lots * self.config.swap_per_lot_per_day * holding_days
        else:
            swap_cost = 0.0

        if trade.direction == TradeDirection.LONG:
            trade.pips = (exit_price - trade.entry_price) / pip_value
        else:
            trade.pips = (trade.entry_price - exit_price) / pip_value

        trade.profit_loss = trade.pips * standard_lots * pip_value * UNITS_PER_LOT - commission_cost + swap_cost
        self.balance = max(0.0, self.balance + trade.profit_loss)

        trade.outcome = (
            TradeOutcome.WIN
            if trade.profit_loss > 0.01
            else TradeOutcome.LOSS
            if trade.profit_loss < -0.01
            else TradeOutcome.BREAKEVEN
        )

        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - self.balance) / self.peak_balance
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown

    def _close_all_open_trades(
        self,
        open_trades: list[SimulatedTrade],
        bar_index: int,
        exit_time: datetime,
        exit_price: float,
    ) -> list[SimulatedTrade]:
        closed: list[SimulatedTrade] = []
        for trade in open_trades:
            self._close_trade(trade, bar_index, exit_time, exit_price, ExitReason.END_OF_DATA)
            closed.append(trade)
        open_trades.clear()
        return closed

    def _open_trade(
        self,
        signal: StrategySignal,
        bar: Bar,
        bar_index: int,
        lot_size: float | None = None,
    ) -> SimulatedTrade | None:
        risk_amount = self.balance * self.config.risk_per_trade_pct
        if signal.is_volatile:
            risk_amount *= 0.5

        if signal.direction == TradeDirection.LONG:
            effective_entry = self.spread_model.adjust_entry_long(signal.entry_price)
        else:
            effective_entry = self.spread_model.adjust_entry_short(signal.entry_price)

        adjusted_risk = abs(effective_entry - signal.stop_loss)
        if adjusted_risk == 0:
            return None

        if lot_size is None:
            lot_size = risk_amount / adjusted_risk

        margin_required = lot_size * effective_entry / self.config.leverage
        if margin_required > self.balance:
            return None

        max_lot_size = self.balance * self.config.leverage / effective_entry
        lot_size = min(lot_size, max_lot_size)

        if lot_size <= 0:
            return None

        return SimulatedTrade(
            entry_bar_index=bar_index,
            exit_bar_index=None,
            direction=signal.direction,
            entry_price=effective_entry,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            exit_price=0.0,
            lot_size=lot_size,
            risk_amount=risk_amount,
            pips=0.0,
            profit_loss=0.0,
            outcome=TradeOutcome.OPEN,
            exit_reason=None,
            entry_time=bar.time,
            exit_time=bar.time,
            confidence_score=signal.confidence,
            confluence_count=1,
            rationale=signal.rationale,
        )

    def _calculate_metrics(
        self,
        trades: list[SimulatedTrade],
        equity_curve: list[float],
        rejected_signals: int = 0,
    ) -> BacktestMetrics:
        metrics = BacktestMetrics(
            starting_balance=self.config.starting_balance,
            ending_balance=self.balance,
            total_pnl=self.balance - self.config.starting_balance,
            total_pnl_pct=(
                (self.balance - self.config.starting_balance) / self.config.starting_balance
                if self.config.starting_balance > 0
                else 0.0
            ),
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
            max_drawdown_dollar=self.max_drawdown * self.peak_balance if self.peak_balance > 0 else 0.0,
            max_daily_loss_dollar=self.max_daily_loss,
            sharpe_ratio=0.0,
            avg_risk_reward=0.0,
            expectancy=0.0,
            avg_holding_bars=0.0,
            equity_curve=equity_curve,
            trades=trades,
            total_spread_cost=self.total_spread_cost,
            total_commission_cost=self.total_commission_cost,
            rejected_signals=self.rejected_signals,
        )

        if len(trades) > 0:
            wins = [t for t in trades if t.outcome == TradeOutcome.WIN]
            losses = [t for t in trades if t.outcome == TradeOutcome.LOSS]

            metrics.win_rate = metrics.winning_trades / len(trades) * 100
            metrics.avg_win = sum(t.profit_loss for t in wins) / len(wins) if wins else 0.0
            metrics.avg_loss = sum(t.profit_loss for t in losses) / len(losses) if losses else 0.0
            metrics.largest_win = max(t.profit_loss for t in wins) if wins else 0.0
            metrics.largest_loss = min(t.profit_loss for t in losses) if losses else 0.0

            total_wins = sum(t.profit_loss for t in wins)
            total_losses = abs(sum(t.profit_loss for t in losses))
            if total_losses > 0:
                metrics.profit_factor = total_wins / total_losses
            elif total_wins > 0:
                metrics.profit_factor = 10.0
            else:
                metrics.profit_factor = 0.0

            metrics.avg_risk_reward = abs(metrics.avg_win / metrics.avg_loss) if metrics.avg_loss != 0 else 0.0
            metrics.expectancy = (metrics.win_rate / 100 * metrics.avg_win) - (
                (1 - metrics.win_rate / 100) * abs(metrics.avg_loss)
            )
            metrics.avg_holding_bars = sum((t.exit_bar_index or 0) - t.entry_bar_index for t in trades) / len(trades)

        metrics.sharpe_ratio = self._calculate_sharpe_ratio(equity_curve)
        return metrics

    def _calculate_sharpe_ratio(self, equity_curve: list[float]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            if equity_curve[i - 1] != 0:
                returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
        if not returns:
            return 0.0
        mean_return = sum(returns) / len(returns)
        std_dev = math.sqrt(sum((r - mean_return) ** 2 for r in returns) / len(returns))
        if std_dev == 0:
            return 999.0 if mean_return > 0 else 0.0
        return (mean_return / std_dev) * math.sqrt(self.config.sharpe_annualization_factor)
