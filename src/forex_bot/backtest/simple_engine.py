import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from core.types import ExitReason, TradeDirection


class TradeOutcome(Enum):
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    OPEN = "open"


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread_pips: float = 0.0

    @property
    def period(self):
        return BarPeriod.H1


class BarPeriod:
    M15 = None
    H1 = None
    H4 = None
    D1 = None

    def __init__(self, minutes: int):
        self.minutes = minutes

    @classmethod
    def _init(cls):
        cls.M15 = cls(15)
        cls.H1 = cls(60)
        cls.H4 = cls(240)
        cls.D1 = cls(1440)


BarPeriod._init()


@dataclass
class BacktestConfig:
    starting_balance: float = 10000.0
    risk_per_trade_pct: float = 0.01
    max_daily_drawdown_pct: float = 0.02
    max_total_drawdown_pct: float = 0.05
    spread_pips: float = 0.5
    commission_per_lot: float = 3.5
    leverage: int = 100
    min_confidence: float = 0.50
    min_confluences: int = 1
    min_risk_reward: float = 1.0
    max_open_trades: int = 1
    min_bars_before_signal: int = 30
    partial_close_enabled: bool = True
    partial_close_at_rr: float = 1.0
    partial_close_pct: float = 0.5
    trailing_stop_enabled: bool = False
    trailing_stop_atr_multiplier: float = 1.0
    regime_filter_enabled: bool = True
    news_volatility_filter_enabled: bool = True
    round_trip_spread: bool = True
    slippage_pips: float = 0.2
    swap_per_lot_per_day: float = -2.0
    pair: str = ""
    sizing_mode: any = None
    position_sizing_config: any = None

    @property
    def units_per_lot(self) -> float:
        return 100000.0

    @property
    def effective_spread_pips(self) -> float:
        if self.spread_pips > 0:
            return self.spread_pips
        if self.pair:
            return PAIR_SPREAD_PIPS.get(self.pair.upper(), DEFAULT_SPREAD_PIPS)
        return DEFAULT_SPREAD_PIPS


PAIR_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 1.5,
    "GBPUSD": 1.5,
    "USDJPY": 1.5,
    "AUDUSD": 1.5,
    "NZDUSD": 1.5,
    "USDCAD": 1.5,
    "USDCHF": 1.5,
    "GBPJPY": 3.0,
    "EURJPY": 2.0,
    "AUDJPY": 2.0,
    "EURGBP": 1.5,
    "EURAUD": 2.0,
    "GBPAUD": 2.5,
    "GBPCAD": 2.5,
    "EURNZD": 2.5,
    "GBPNZD": 3.0,
    "XAUUSD": 2.5,
    "XAGUSD": 3.0,
}

DEFAULT_SPREAD_PIPS = 1.5


@dataclass
class BacktestMetrics:
    starting_balance: float
    ending_balance: float
    total_pnl: float
    total_pnl_pct: float
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    profit_factor: float
    max_drawdown_pct: float
    max_drawdown_dollar: float
    max_daily_loss_dollar: float
    sharpe_ratio: float
    avg_risk_reward: float
    expectancy: float
    avg_holding_bars: float
    equity_curve: list[float]
    trades: list
    total_spread_cost: float
    total_commission_cost: float
    rejected_signals: int

    def print_report(self):
        print("\n" + "=" * 50)
        print("       BASELINE STRATEGY BACKTEST RESULTS")
        print("=" * 50)
        print(f"\n  Starting Balance:    ${self.starting_balance:.2f}")
        print(f"  Ending Balance:      ${self.ending_balance:.2f}")
        print(f"  Total P&L:           ${self.total_pnl:.2f} ({self.total_pnl_pct:.2f}%)")
        print(f"\n  Total Trades:        {self.total_trades}")
        print(f"  Winning:             {self.winning_trades}")
        print(f"  Losing:              {self.losing_trades}")
        print(f"  Breakeven:           {self.breakeven_trades}")
        print(f"  Rejected Signals:    {self.rejected_signals}")
        print(f"\n  Win Rate:            {self.win_rate:.1f}%")
        print(f"  Avg Win:             ${self.avg_win:.2f}")
        print(f"  Avg Loss:            ${self.avg_loss:.2f}")
        print(f"  Largest Win:         ${self.largest_win:.2f}")
        print(f"  Largest Loss:       ${self.largest_loss:.2f}")
        print(f"  Profit Factor:       {self.profit_factor:.2f}")
        print(f"  Expectancy:          ${self.expectancy:.2f}")
        print(f"  Avg R:R:             {self.avg_risk_reward:.2f}")
        print(f"\n  Max Drawdown:        {self.max_drawdown_pct:.2f}% (${self.max_drawdown_dollar:.2f})")
        print(f"  Max Daily Loss:      ${self.max_daily_loss_dollar:.2f}")
        print(f"  Sharpe Ratio:        {self.sharpe_ratio:.2f}")
        print(f"  Avg Holding Bars:    {self.avg_holding_bars:.0f}")
        print(f"\n  Spread Cost:         ${self.total_spread_cost:.2f}")
        print(f"  Commission Cost:     ${self.total_commission_cost:.2f}")
        print()


@dataclass
class SimulatedTrade:
    entry_bar_index: int
    exit_bar_index: int
    direction: TradeDirection
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    exit_price: float
    lot_size: float
    risk_amount: float
    pips: float
    profit_loss: float
    outcome: TradeOutcome
    exit_reason: ExitReason
    entry_time: datetime
    exit_time: datetime
    confidence_score: float
    confluence_count: int
    rationale: str
    partial_closed: bool = False
    partial_close_price: float = 0.0
    partial_close_pnl: float = 0.0


class SimpleBacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.balance = config.starting_balance
        self.peak_balance = config.starting_balance
        self.max_drawdown = 0.0
        self.current_day: date | None = None
        self.daily_start_balance = config.starting_balance
        self.max_daily_loss = 0.0
        self.total_spread_cost = 0.0
        self.total_commission_cost = 0.0

    def run(self, bars: list[Bar]) -> BacktestMetrics:
        if len(bars) < self.config.min_bars_before_signal:
            raise ValueError(f"Need at least {self.config.min_bars_before_signal} bars")

        self._reset()
        trades: list[SimulatedTrade] = []
        equity_curve = [self.balance]
        open_trades: list[SimulatedTrade] = []
        rejected_signals = 0

        for i in range(len(bars)):
            bar = bars[i]
            self._update_daily_tracking(bar.time)

            if self.balance <= 0:
                break
            if self._is_max_drawdown_breached():
                break
            if self._is_max_daily_loss_breached():
                continue

            self._check_open_trades(open_trades, bar, i, trades, equity_curve)

            if len(open_trades) < self.config.max_open_trades and i >= self.config.min_bars_before_signal:
                pass

            equity_curve.append(self.balance)

        self._close_all_open_trades(open_trades, len(bars) - 1, bars[-1].time, bars[-1].close)
        return self._calculate_metrics(trades, equity_curve, rejected_signals)

    def _reset(self):
        self.balance = self.config.starting_balance
        self.peak_balance = self.config.starting_balance
        self.max_drawdown = 0.0
        self.current_day = None
        self.daily_start_balance = self.config.starting_balance
        self.max_daily_loss = 0.0
        self.total_spread_cost = 0.0
        self.total_commission_cost = 0.0
        self.current_day = None
        self.daily_start_balance = self.config.starting_balance

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

    def _check_open_trades(
        self,
        open_trades: list[SimulatedTrade],
        bar: Bar,
        bar_index: int,
        closed_trades: list[SimulatedTrade],
        equity_curve: list[float],
    ):
        to_close = []

        for trade in open_trades:
            self._progressive_sl_update(trade, bar)
            hit, exit_price, reason = self._check_trade_exit(trade, bar)
            if hit:
                self._close_trade(trade, bar_index, bar.time, exit_price, reason)
                closed_trades.append(trade)
                to_close.append(trade)
                equity_curve.append(self.balance)

        for t in to_close:
            open_trades.remove(t)

    def _progressive_sl_update(self, trade: SimulatedTrade, bar: Bar) -> None:
        if not hasattr(trade, "_sl_moved_to_be"):
            trade._sl_moved_to_be = False
            trade._sl_moved_to_tp1 = False

        pip_size = self._get_pip_value(trade.entry_price)

        if trade.direction == TradeDirection.LONG:
            if bar.high >= trade.take_profit_2 and not trade._sl_moved_to_tp1:
                trade.stop_loss = trade.take_profit_1
                trade._sl_moved_to_tp1 = True
                trade._sl_moved_to_be = True
            elif bar.high >= trade.take_profit_1 and not trade._sl_moved_to_be:
                trade.stop_loss = max(trade.stop_loss, trade.entry_price + pip_size)
                trade._sl_moved_to_be = True
        else:
            if bar.low <= trade.take_profit_2 and not trade._sl_moved_to_tp1:
                trade.stop_loss = trade.take_profit_1
                trade._sl_moved_to_tp1 = True
                trade._sl_moved_to_be = True
            elif bar.low <= trade.take_profit_1 and not trade._sl_moved_to_be:
                trade.stop_loss = min(trade.stop_loss, trade.entry_price - pip_size)
                trade._sl_moved_to_be = True

    def _check_trade_exit(self, trade: SimulatedTrade, bar: Bar):
        if trade.direction == TradeDirection.LONG:
            if bar.low <= trade.stop_loss:
                return (True, trade.stop_loss, ExitReason.STOP_LOSS)
            if bar.high >= trade.take_profit_3:
                return (True, trade.take_profit_3, ExitReason.TAKE_PROFIT_3)
            if bar.high >= trade.take_profit_2:
                return (True, trade.take_profit_2, ExitReason.TAKE_PROFIT_2)
        else:
            if bar.high >= trade.stop_loss:
                return (True, trade.stop_loss, ExitReason.STOP_LOSS)
            if bar.low <= trade.take_profit_3:
                return (True, trade.take_profit_3, ExitReason.TAKE_PROFIT_3)
            if bar.low <= trade.take_profit_2:
                return (True, trade.take_profit_2, ExitReason.TAKE_PROFIT_2)
        return (False, 0, ExitReason.STOP_LOSS)

    def _close_trade(
        self,
        trade: SimulatedTrade,
        bar_index: int,
        exit_time: datetime,
        exit_price: float,
        reason: ExitReason,
    ):
        trade.exit_bar_index = bar_index
        trade.exit_time = exit_time
        trade.exit_reason = reason

        pip_value = self._get_pip_value(trade.entry_price)
        standard_lots = trade.lot_size / self.config.units_per_lot
        spread_pips = self.config.effective_spread_pips
        commission_cost = standard_lots * self.config.commission_per_lot
        self.total_commission_cost += commission_cost

        if self.config.round_trip_spread:
            spread_price = spread_pips * pip_value
            if trade.direction == TradeDirection.LONG:
                exit_price -= spread_price
            else:
                exit_price += spread_price
            spread_dollars = spread_pips * pip_value * trade.lot_size
            self.total_spread_cost += spread_dollars
        else:
            spread_dollars = spread_pips * pip_value * trade.lot_size
            self.total_spread_cost += spread_dollars

        slippage_price = self.config.slippage_pips * pip_value
        if trade.direction == TradeDirection.LONG:
            exit_price -= slippage_price
        else:
            exit_price += slippage_price

        trade.exit_price = exit_price

        holding_days = (exit_time.date() - trade.entry_time.date()).days
        if holding_days > 0 and self.config.swap_per_lot_per_day != 0.0:
            swap_cost = standard_lots * self.config.swap_per_lot_per_day * holding_days
        else:
            swap_cost = 0.0

        if trade.direction == TradeDirection.LONG:
            trade.pips = (exit_price - trade.entry_price) / pip_value
        else:
            trade.pips = (trade.entry_price - exit_price) / pip_value

        trade.profit_loss = (
            trade.pips * standard_lots * pip_value * self.config.units_per_lot - commission_cost + swap_cost
        )
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
        drawdown = (self.peak_balance - self.balance) / self.peak_balance if self.peak_balance > 0 else 0.0
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def _close_all_open_trades(
        self,
        open_trades: list[SimulatedTrade],
        bar_index: int,
        exit_time: datetime,
        exit_price: float,
    ) -> list[SimulatedTrade]:
        closed = []
        for trade in open_trades:
            self._close_trade(
                trade,
                bar_index,
                exit_time,
                exit_price,
                ExitReason.END_OF_DATA,
            )
            closed.append(trade)
        open_trades.clear()
        return closed

    def _calculate_metrics(
        self,
        trades: list[SimulatedTrade],
        equity_curve: list[float],
        rejected_signals: int,
    ) -> BacktestMetrics:
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
