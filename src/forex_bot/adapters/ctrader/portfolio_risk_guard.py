from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import TradeDirection
from .risk_guard import (
    FTMOConfig,
    RiskGuard,
    RiskLimitResult,
)

if TYPE_CHECKING:
    from engine.protocol import CanonicalSignal

logger = logging.getLogger(__name__)


@dataclass
class StrategyRiskStats:
    strategy_id: str
    pnl: float = 0.0
    trade_count: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trade_count if self.trade_count > 0 else 0.0


class PortfolioRiskGuard:
    def __init__(
        self,
        ftmo_config: FTMOConfig | None = None,
        starting_balance: float = 100000.0,
    ):
        self._guard = RiskGuard(ftmo_config, starting_balance)
        self._starting_balance = starting_balance
        self._strategy_stats: dict[str, StrategyRiskStats] = {}

    def check_signal(self, signal: CanonicalSignal) -> RiskLimitResult:
        trade_signal = self._to_trade_signal(signal)
        return self._guard.check_signal(trade_signal)

    def check_trade_allowed(
        self,
        signal: CanonicalSignal,
        volume: float,
    ) -> RiskLimitResult:
        tp = signal.take_profit_1 or signal.entry_price
        return self._guard.check_trade_allowed(
            direction=self._convert_direction(signal.direction),
            volume=volume,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=tp,
        )

    def update_balance(self, new_balance: float) -> None:
        self._guard.update_balance(new_balance)

    def record_trade(self, strategy_id: str, pnl: float, is_win: bool) -> None:
        self._guard.record_trade(pnl, is_win)
        self._guard.record_strategy_trade(strategy_id, pnl)
        stats = self._strategy_stats.setdefault(strategy_id, StrategyRiskStats(strategy_id=strategy_id))
        stats.trade_count += 1
        stats.pnl += pnl
        if is_win:
            stats.wins += 1
        else:
            stats.losses += 1

    def get_portfolio_state(self) -> dict:
        guard_stats = self._guard.get_stats()
        return {
            "current_balance": guard_stats["current_balance"],
            "peak_balance": guard_stats["peak_balance"],
            "daily_loss_pct": guard_stats["daily_loss_pct"],
            "total_drawdown_pct": guard_stats["total_drawdown_pct"],
            "daily_trades": guard_stats["daily_trades"],
            "total_trades": guard_stats["total_trades"],
            "is_blocked": guard_stats["is_blocked"],
            "per_strategy_pnl": guard_stats["per_strategy_pnl"],
        }

    def get_per_strategy_stats(self) -> dict[str, StrategyRiskStats]:
        return dict(self._strategy_stats)

    @property
    def current_balance(self) -> float:
        return self._guard.get_stats()["current_balance"]

    @property
    def is_blocked(self) -> bool:
        return self._guard.is_blocked

    def register_circuit_breaker_callback(self, callback):
        self._guard.register_circuit_breaker_callback(callback)

    def reset(self):
        self._guard.reset_circuit_breaker()
        self._guard.reset_daily_tracking()
        self._strategy_stats.clear()

    @staticmethod
    def _to_trade_signal(signal: CanonicalSignal):
        from .models import CTraderTradeSignal

        return CTraderTradeSignal(
            symbol=signal.symbol,
            direction=PortfolioRiskGuard._convert_direction(signal.direction),
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1 or 0.0,
            take_profit_2=signal.take_profit_2 or 0.0,
            take_profit_3=signal.take_profit_3 or 0.0,
            volume=0.1,
            confidence=signal.confidence,
            rationale=signal.rationale,
            strategy_id=signal.strategy_id,
        )

    @staticmethod
    def _convert_direction(direction) -> TradeDirection:
        if isinstance(direction, TradeDirection):
            return direction
        value = direction.value if hasattr(direction, "value") else str(direction)
        try:
            return TradeDirection(value)
        except ValueError:
            return TradeDirection.NEUTRAL
