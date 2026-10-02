from typing import Protocol, runtime_checkable

from core.types import MarketState, StrategySignal


@runtime_checkable
class IStrategy(Protocol):
    """Formal protocol for all backtest-compatible strategies."""

    @property
    def name(self) -> str: ...

    def evaluate(self, state: MarketState) -> StrategySignal | None: ...
