"""SimpleRSIThresholdStrategy — minimal RSI threshold crossover strategy.

Built for execution-path validation only. Not for profit.

Logic:
    - Compute RSI (Wilder's smoothing) on close prices over `rsi_period` bars.
    - Detect a *cross* of the threshold (not just "is in zone"):
        * LONG  signal: prev_rsi >= oversold AND current_rsi <  oversold
        * SHORT signal: prev_rsi <= overbought AND current_rsi > overbought
    - A cross guarantees we don't fire repeatedly while RSI remains in zone.
    - Stop loss = 2 * ATR from entry. TPs at 1R / 2R / 3R.

Interface mirrors other strategies in this folder:
    - Subclasses ``backtest.strategy_legacy.ISignalStrategy``
    - ``name`` property returns the human-readable strategy name
    - ``evaluate(state: MarketState) -> StrategySignal | None``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from backtest.strategies.isignal_strategy import ISignalStrategy
from core.types import Bar, MarketState, StrategySignal, TradeDirection


@dataclass(frozen=True)
class RSIThresholdConfig:
    rsi_period: int = 14
    oversold: float = 20.0
    overbought: float = 80.0
    atr_period: int = 14
    atr_sl_multiplier: float = 2.0


def _rsi_wilder(closes: List[float], period: int) -> Optional[float]:
    """Wilder's smoothing RSI. Returns None until enough data is available.

    Requires ``period + 1`` closes to seed the smoothing (one change per bar
    for `period` bars, plus the seed for avg_gain/avg_loss).
    """
    if len(closes) < period + 1:
        return None

    # Seed the smoothed averages with a simple mean of the first `period` changes
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else 0.0)
        losses.append(-change if change < 0 else 0.0)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Smooth over the remaining changes
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(bars: List[Bar], period: int) -> float:
    """Simple ATR over the last `period` bars. Returns 0.0001 fallback if too few bars."""
    if len(bars) < period + 1:
        return 0.0001
    tr_sum = 0.0
    count = 0
    for i in range(len(bars) - period, len(bars)):
        if i > 0:
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            tr_sum += tr
            count += 1
    return tr_sum / count if count > 0 else 0.0001


class SimpleRSIThresholdStrategy(ISignalStrategy):
    """Minimal RSI threshold crossover strategy.

    Default config: rsi_period=14, oversold=20, overbought=80, sl=2*ATR.
    Fires exactly once per threshold cross (not on every bar in zone).
    """

    def __init__(self, config: Optional[RSIThresholdConfig] = None):
        self.config = config or RSIThresholdConfig()

    @property
    def name(self) -> str:
        return "Simple RSI Threshold"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        cfg = self.config

        # Need rsi_period + 1 for the initial seed, plus 1 more for "prev_rsi" on the cross check
        min_required = cfg.rsi_period + 2
        if len(state.bars) < min_required:
            return None

        closes = [b.close for b in state.bars]

        # Compute RSI for the current window and the window one bar shorter
        current_rsi = _rsi_wilder(closes, cfg.rsi_period)
        prev_rsi = _rsi_wilder(closes[:-1], cfg.rsi_period)

        if current_rsi is None or prev_rsi is None:
            return None

        # Cross detection: only fire on the bar that crosses the threshold
        crossed_below_oversold = prev_rsi >= cfg.oversold and current_rsi < cfg.oversold
        crossed_above_overbought = prev_rsi <= cfg.overbought and current_rsi > cfg.overbought

        if not crossed_below_oversold and not crossed_above_overbought:
            return None

        latest = state.latest_bar
        entry = latest.close
        atr = state.atr if state.atr > 0 else _atr(state.bars, cfg.atr_period)
        if atr <= 0:
            return None

        if crossed_below_oversold:
            direction = TradeDirection.LONG
            sl = entry - atr * cfg.atr_sl_multiplier
            rationale = (
                f"RSI crossed below oversold: prev_rsi={prev_rsi:.2f} >= {cfg.oversold}, "
                f"rsi={current_rsi:.2f} < {cfg.oversold}"
            )
        else:
            direction = TradeDirection.SHORT
            sl = entry + atr * cfg.atr_sl_multiplier
            rationale = (
                f"RSI crossed above overbought: prev_rsi={prev_rsi:.2f} <= {cfg.overbought}, "
                f"rsi={current_rsi:.2f} > {cfg.overbought}"
            )

        risk = abs(entry - sl)
        if risk <= 0:
            return None

        if direction == TradeDirection.LONG:
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
        else:
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0

        # Modest confidence — distance from threshold, normalized
        if direction == TradeDirection.LONG:
            distance = cfg.oversold - current_rsi  # positive when below
        else:
            distance = current_rsi - cfg.overbought  # positive when above
        confidence = max(0.40, min(0.75, 0.55 + distance / 100.0))

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )
