from dataclasses import dataclass, field

from core.types import SimulatedTrade


@dataclass
class BacktestConfig:
    starting_balance: float = 100_000.0
    risk_per_trade_pct: float = 0.005
    max_daily_drawdown_pct: float = 0.05
    max_total_drawdown_pct: float = 0.10
    spread_pips: float = 1.5
    commission_per_lot: float = 3.5
    leverage: int = 100
    min_confidence: float = 0.50
    min_confluences: int = 2
    min_risk_reward: float = 1.5
    max_open_trades: int = 3
    min_bars_before_signal: int = 50
    partial_close_enabled: bool = True
    trailing_stop_enabled: bool = True
    regime_filter_enabled: bool = False
    slippage_pips: float = 0.5
    swap_per_lot_per_day: float = -3.5
    pair: str = "EURUSD"
    sharpe_annualization_factor: float = 252.0


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
    equity_curve: list[float] = field(default_factory=list)
    trades: list[SimulatedTrade] = field(default_factory=list)
    total_spread_cost: float = 0.0
    total_commission_cost: float = 0.0
    rejected_signals: int = 0
