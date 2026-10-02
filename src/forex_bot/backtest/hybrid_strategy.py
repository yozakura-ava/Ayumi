from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .engine import Bar, StrategySignal, TradeDirection
from .ict_smc.confluence_engine import SignalConfluenceEngine
from .ict_smc.h4_context import H4ContextModule
from .ict_smc.models import ConfluenceSignal, ICTMarketState


class QuantFilterName(Enum):
    VOLATILITY = "volatility"
    TREND = "trend"
    H4_ALIGNMENT = "h4_alignment"


@dataclass
class HybridConfig:
    min_confidence: float = 0.5
    volatility_threshold_percentile: float = 30.0
    trend_threshold_adx: float = 20.0
    enabled_filters: tuple[QuantFilterName, ...] = (
        QuantFilterName.VOLATILITY,
        QuantFilterName.TREND,
        QuantFilterName.H4_ALIGNMENT,
    )


@dataclass
class RejectionMetrics:
    total_evaluated: int = 0
    rejected_by_confidence: int = 0
    rejected_by_volatility: int = 0
    rejected_by_trend: int = 0
    rejected_by_h4_alignment: int = 0
    passed: int = 0

    @property
    def rejection_rate(self) -> float:
        if self.total_evaluated == 0:
            return 0.0
        return (self.total_evaluated - self.passed) / self.total_evaluated

    def to_dict(self) -> dict[str, int]:
        return {
            "total_evaluated": self.total_evaluated,
            "rejected_by_confidence": self.rejected_by_confidence,
            "rejected_by_volatility": self.rejected_by_volatility,
            "rejected_by_trend": self.rejected_by_trend,
            "rejected_by_h4_alignment": self.rejected_by_h4_alignment,
            "passed": self.passed,
        }


class HybridStrategy:
    def __init__(
        self,
        config: HybridConfig | None = None,
        ict_engine: SignalConfluenceEngine | None = None,
    ):
        self.config = config or HybridConfig()
        self._ict_engine = ict_engine or SignalConfluenceEngine()
        self._h4_module = H4ContextModule()
        self.metrics = RejectionMetrics()

    @property
    def name(self) -> str:
        return "Hybrid ICT/SMC + Quantitative Filter"

    def evaluate(
        self,
        ict_state: ICTMarketState,
        h4_bars: list[Bar] | None = None,
        atr_series: list[float] | None = None,
        high_series: list[float] | None = None,
        low_series: list[float] | None = None,
        close_series: list[float] | None = None,
        bar_time: datetime | None = None,
    ) -> StrategySignal | None:
        self.metrics.total_evaluated += 1

        ict_signal = self._ict_engine.evaluate(ict_state, h4_bars=h4_bars)
        if ict_signal is None:
            return None

        if ict_signal.confidence_score < self.config.min_confidence:
            self.metrics.rejected_by_confidence += 1
            return None

        quant_ok = self._run_quant_filters(
            ict_signal,
            ict_state,
            h4_bars=h4_bars,
            atr_series=atr_series,
            high_series=high_series,
            low_series=low_series,
            close_series=close_series,
            bar_time=bar_time,
        )
        if not quant_ok:
            return None

        self.metrics.passed += 1
        return self._convert_signal(ict_signal)

    def _run_quant_filters(
        self,
        signal: ConfluenceSignal,
        ict_state: ICTMarketState,
        h4_bars: list[Bar] | None = None,
        atr_series: list[float] | None = None,
        high_series: list[float] | None = None,
        low_series: list[float] | None = None,
        close_series: list[float] | None = None,
        bar_time: datetime | None = None,
    ) -> bool:
        enabled = set(self.config.enabled_filters)

        if QuantFilterName.VOLATILITY in enabled:
            if not self._check_volatility(atr_series):
                self.metrics.rejected_by_volatility += 1
                return False

        if QuantFilterName.TREND in enabled:
            if not self._check_trend(high_series, low_series, close_series):
                self.metrics.rejected_by_trend += 1
                return False

        if QuantFilterName.H4_ALIGNMENT in enabled:
            if not self._check_h4_alignment(signal, h4_bars, ict_state.latest_bar.close, ict_state.atr):
                self.metrics.rejected_by_h4_alignment += 1
                return False

        return True

    def _check_volatility(self, atr_series: list[float] | None) -> bool:
        if atr_series is None or len(atr_series) < 2:
            return True

        window = atr_series[-50:]
        current = atr_series[-1]
        rank = sum(1 for v in window if v < current)
        tied = sum(1 for v in window if v == current)
        rank += tied // 2
        percentile = (rank / len(window)) * 100.0

        return percentile >= self.config.volatility_threshold_percentile

    def _check_trend(
        self,
        high: list[float] | None,
        low: list[float] | None,
        close: list[float] | None,
    ) -> bool:
        if high is None or low is None or close is None:
            return True
        if len(high) < 15 or len(low) < 15 or len(close) < 15:
            return True

        from quant.regime import trend_regime

        result = trend_regime(high, low, close)
        return result.adx_value >= self.config.trend_threshold_adx

    def _check_h4_alignment(
        self,
        signal: ConfluenceSignal,
        h4_bars: list[Bar] | None,
        current_price: float,
        atr: float,
    ) -> bool:
        if h4_bars is None or len(h4_bars) < 10:
            return True
        if atr == 0:
            return True

        h4_context = self._h4_module.analyze(h4_bars, current_price, atr)

        if signal.direction == TradeDirection.LONG:
            return h4_context.bullish_score >= h4_context.bearish_score
        return h4_context.bearish_score >= h4_context.bullish_score

    @staticmethod
    def _convert_signal(ict_signal: ConfluenceSignal) -> StrategySignal:
        return StrategySignal(
            direction=ict_signal.direction,
            confidence=ict_signal.confidence_score,
            entry_price=ict_signal.entry_price,
            stop_loss=ict_signal.stop_loss,
            take_profit_1=ict_signal.take_profit_1,
            take_profit_2=ict_signal.take_profit_2,
            take_profit_3=ict_signal.take_profit_3,
            rationale=ict_signal.rationale,
        )

    def reset_metrics(self) -> None:
        self.metrics = RejectionMetrics()
