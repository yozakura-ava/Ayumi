from __future__ import annotations

from typing import TYPE_CHECKING

from core.types import Bar, StrategySignal

if TYPE_CHECKING:
    from backtest.trade_management import (
        TradeManagementConfig,
    )


class TradeManagementMixin:
    def __init__(self, tm_config: "TradeManagementConfig | None" = None):
        self._trade_manager = None
        self._tm_config = tm_config

    def _ensure_trade_manager(self) -> None:
        if self._trade_manager is None:
            from backtest.trade_management import (
                TradeManagementConfig,
                TradeManager,
            )

            config = self._tm_config or TradeManagementConfig()
            self._trade_manager = TradeManager(config)

    def check_entry_allowed(
        self,
        bar: Bar,
        signal: StrategySignal,
        atr: float,
        spread_pips: float,
        pair: str,
    ) -> object:
        self._ensure_trade_manager()
        return self._trade_manager.check_entry_allowed(bar, signal, atr, spread_pips, pair)
