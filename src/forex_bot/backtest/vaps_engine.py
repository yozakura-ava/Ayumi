from __future__ import annotations

from quant.vaps import VAPSConfig, vaps_regime

from .engine import BacktestConfig, Bar
from .multi_strategy_engine import (
    MultiStrategyBacktestEngine,
    StrategyBacktestResult,
)
from .strategies import ISignalStrategy


def _compute_atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0001
    tr_sum = 0.0
    for i in range(len(bars) - period, len(bars)):
        if i > 0:
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            tr_sum += tr
    return tr_sum / period


class VAPSBacktestEngine(MultiStrategyBacktestEngine):
    def __init__(
        self,
        config: BacktestConfig,
        strategies: list[ISignalStrategy],
        vaps_config: VAPSConfig | None = None,
        atr_period: int = 14,
    ):
        super().__init__(config, strategies)
        self._vaps_config = vaps_config or VAPSConfig()
        self._atr_period = atr_period
        self._atr_history: list[float] = []
        self._all_bars: list[Bar] = []

    def run_all_strategies(self, bars: list[Bar]) -> dict[str, StrategyBacktestResult]:
        self._all_bars = bars
        self._atr_history = []
        return super().run_all_strategies(bars)

    def _open_trade(self, signal, bar: Bar, bar_index: int):
        atr = _compute_atr(self._all_bars[: bar_index + 1], self._atr_period)
        self._atr_history.append(atr)

        trade = super()._open_trade(signal, bar, bar_index)
        if trade is None:
            return None

        if len(self._atr_history) < 2:
            return trade

        _regime, _percentile, multiplier = vaps_regime(self._atr_history, config=self._vaps_config)
        trade.lot_size *= multiplier
        trade.risk_amount *= multiplier
        return trade
