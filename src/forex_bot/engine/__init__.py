"""Engine module. Only ForwardTestEngine (adapters/ctrader/forward_test_engine.py) is canonical.
Deprecated engines (MultiStrategyOrchestrator, TradingOrchestrator) moved to _deprecated/ on 2026-07-08."""

from engine.base import EngineCore
from engine.engine import BacktestEngine
from engine.mixins import CombinedSignalMixin, CombineMethod, ProgressiveSLMixin
from engine.trade_mgmt import TradeManagementMixin

__all__ = [
    "BacktestEngine",
    "CombinedSignalMixin",
    "CombineMethod",
    "EngineCore",
    "ProgressiveSLMixin",
    "TradeManagementMixin",
]
