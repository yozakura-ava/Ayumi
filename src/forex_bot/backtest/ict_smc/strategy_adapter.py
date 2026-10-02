from __future__ import annotations

from ..engine import MarketState, StrategySignal
from ..strategies import ISignalStrategy
from .confluence_engine import SignalConfluenceEngine
from .models import ICTMarketState


class ICTSMCStrategy(ISignalStrategy):
    """Adapter that wraps SignalConfluenceEngine as an ISignalStrategy for the backtest harness."""

    def __init__(self, **engine_kwargs):
        self._engine = SignalConfluenceEngine(**engine_kwargs)

    @property
    def name(self) -> str:
        return "ICT/SMC Confluence"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        ict_state = ICTMarketState(bars=state.bars)
        ict_state.current_session = state.current_session

        confluence = self._engine.evaluate(ict_state)
        if confluence is None:
            return None

        return StrategySignal(
            direction=confluence.direction,
            confidence=confluence.confidence_score,
            entry_price=confluence.entry_price,
            stop_loss=confluence.stop_loss,
            take_profit_1=confluence.take_profit_1,
            take_profit_2=confluence.take_profit_2,
            take_profit_3=confluence.take_profit_3,
            rationale=confluence.rationale,
        )
