from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backtest.ict_smc.confluence_engine import SignalConfluenceEngine
from backtest.ict_smc.h4_context import H4ContextModule
from backtest.ict_smc.models import ICTMarketState
from core.types import (
    MarketState,
    StrategySignal,
)
from strategies.session_range_mean_reversion import (
    SessionRangeMeanReversionStrategy,
    SessionRangeMRConfig,
)


@dataclass(frozen=True)
class ICTFilterConfig:
    min_confluence_confidence: float = 0.40
    require_structure_alignment: bool = False
    require_order_block: bool = False
    require_fvg: bool = False
    require_liquidity_sweep: bool = False
    require_pd_zone: bool = False
    min_confluence_count: int = 0
    use_h4_context: bool = True


class SessionRangeMRWithICTFilter:
    def __init__(
        self,
        mr_config: Optional[SessionRangeMRConfig] = None,
        ict_config: Optional[ICTFilterConfig] = None,
        ict_engine: Optional[SignalConfluenceEngine] = None,
    ):
        self._mr = SessionRangeMeanReversionStrategy(config=mr_config)
        self._ict_config = ict_config or ICTFilterConfig()
        self._ict_engine = ict_engine or SignalConfluenceEngine(min_confidence=0.30)
        self._h4_module = H4ContextModule()
        self._total_evaluated = 0
        self._passed_ict_filter = 0
        self._rejected_ict_filter = 0
        self._mr_signals = 0

    @property
    def name(self) -> str:
        return "Session-Range MR + ICT Filter"

    @property
    def filter_stats(self) -> dict:
        return {
            "total_evaluated": self._total_evaluated,
            "mr_signals": self._mr_signals,
            "passed_ict": self._passed_ict_filter,
            "rejected_ict": self._rejected_ict_filter,
            "ict_pass_rate": (self._passed_ict_filter / self._mr_signals if self._mr_signals > 0 else 0.0),
        }

    def reset_stats(self) -> None:
        self._total_evaluated = 0
        self._passed_ict_filter = 0
        self._rejected_ict_filter = 0
        self._mr_signals = 0

    def evaluate(self, state: MarketState) -> Optional[StrategySignal]:
        self._total_evaluated += 1

        mr_signal = self._mr.evaluate(state)
        if mr_signal is None:
            return None

        self._mr_signals += 1

        ict_state = ICTMarketState(bars=state.bars)
        ict_state.current_session = state.current_session

        h4_bars = state.bars if self._ict_config.use_h4_context else None
        ict_signal = self._ict_engine.evaluate(ict_state, h4_bars=h4_bars)

        if ict_signal is None:
            self._rejected_ict_filter += 1
            return None

        if ict_signal.confidence_score < self._ict_config.min_confluence_confidence:
            self._rejected_ict_filter += 1
            return None

        if self._ict_config.require_structure_alignment:
            if ict_state.structure_bias != mr_signal.direction:
                self._rejected_ict_filter += 1
                return None

        if self._ict_config.require_order_block:
            if not ict_signal.has_order_block:
                self._rejected_ict_filter += 1
                return None

        if self._ict_config.require_fvg:
            if not ict_signal.has_fvg:
                self._rejected_ict_filter += 1
                return None

        if self._ict_config.require_liquidity_sweep:
            if not ict_signal.has_liquidity_sweep:
                self._rejected_ict_filter += 1
                return None

        if self._ict_config.require_pd_zone:
            if not ict_signal.has_premium_discount_confluence:
                self._rejected_ict_filter += 1
                return None

        if self._ict_config.min_confluence_count > 0:
            if ict_signal.confluence_count < self._ict_config.min_confluence_count:
                self._rejected_ict_filter += 1
                return None

        self._passed_ict_filter += 1

        combined_confidence = min(
            0.95,
            0.4 * mr_signal.confidence + 0.6 * ict_signal.confidence_score,
        )

        rationale = (
            f"{mr_signal.rationale}\n"
            f"ICT confluence: {ict_signal.confidence_score:.2f}, "
            f"count={ict_signal.confluence_count}, "
            f"structure={'aligned' if ict_state.structure_bias == mr_signal.direction else 'neutral'}\n"
            f"{ict_signal.rationale}"
        )

        return StrategySignal(
            direction=mr_signal.direction,
            confidence=combined_confidence,
            entry_price=mr_signal.entry_price,
            stop_loss=mr_signal.stop_loss,
            take_profit_1=mr_signal.take_profit_1,
            take_profit_2=mr_signal.take_profit_2,
            take_profit_3=mr_signal.take_profit_3,
            rationale=rationale,
        )
