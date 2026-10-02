from __future__ import annotations

from backtest.strategies import ISignalStrategy
from core.types import Bar, MarketState, StrategySignal, TradeDirection
from quant.bar_resample import resample_bars
from quant.mtf_regime import (
    MTFRegimeConfig,
    MTFRegimeFilter,
    MultiTimeframeRegime,
    TrendDirection,
)


class MTFFilteredMomentumStrategy(ISignalStrategy):
    def __init__(
        self,
        inner_strategy: ISignalStrategy,
        regime_config: MTFRegimeConfig | None = None,
        mtf_bars_source_minutes: int = 15,
        confidence_boost: float = 0.0,
        direction_filter: bool = True,
    ):
        self.inner = inner_strategy
        self.filter = MTFRegimeFilter(regime_config)
        self.source_minutes = mtf_bars_source_minutes
        self.confidence_boost = confidence_boost
        self.direction_filter = direction_filter

    @property
    def name(self) -> str:
        return f"MTF-Filtered {self.inner.name}"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        h4_bars, h1_bars, m15_bars = self._build_mtf_bars(state.bars)
        if not m15_bars:
            return None

        if not self.filter.evaluate(state, h4_bars, h1_bars, m15_bars):
            return None

        signal = self.inner.evaluate(state)
        if signal is None:
            return None

        if self.direction_filter:
            regime = self.filter.get_regime(h4_bars, h1_bars, m15_bars)
            if not self._direction_aligned(signal.direction, regime):
                return None

        if self.confidence_boost > 0:
            confidence = self.filter.get_confidence(state, h4_bars, h1_bars, m15_bars)
            boost = self.confidence_boost * confidence
            boosted = min(signal.confidence + boost, 0.95)
            signal = StrategySignal(
                direction=signal.direction,
                confidence=boosted,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit_1=signal.take_profit_1,
                take_profit_2=signal.take_profit_2,
                take_profit_3=signal.take_profit_3,
                rationale=f"[MTF] {signal.rationale}",
            )

        return signal

    def _build_mtf_bars(self, bars: list[Bar]) -> tuple[list[Bar], list[Bar], list[Bar]]:
        if self.source_minutes == 15:
            m15_bars = bars
        else:
            m15_bars = resample_bars(bars, 15)

        h1_bars = resample_bars(m15_bars, 60)
        h4_bars = resample_bars(h1_bars, 240)

        return h4_bars, h1_bars, m15_bars

    @staticmethod
    def _direction_aligned(direction: TradeDirection, regime: MultiTimeframeRegime) -> bool:
        if regime.aligned_direction == TrendDirection.NEUTRAL:
            return False
        if direction == TradeDirection.LONG:
            return regime.aligned_direction == TrendDirection.BULLISH
        if direction == TradeDirection.SHORT:
            return regime.aligned_direction == TrendDirection.BEARISH
        return False
