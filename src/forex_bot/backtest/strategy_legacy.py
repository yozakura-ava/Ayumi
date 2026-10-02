from dataclasses import dataclass
from datetime import datetime

from .engine import Bar, MarketState, SessionType, StrategySignal, TradeDirection
from .strategies.isignal_strategy import ISignalStrategy, StrategyConfig, Tick

# Re-export for backward compatibility — all existing imports of
# ISignalStrategy from strategy_legacy continue to work.
__all__ = [
    "ISignalStrategy",
    "Tick",
    "StrategyConfig",
    "MACrossStrategy",
    "BBStrategy",
    "RSIStrategy",
    "SRBreakoutStrategy",
    "ROCMStrategy",
    "MomentumBreakoutStrategy",
    "CommodityTrendStrategy",
    "CommodityMeanReversionStrategy",
    "SupertrendRSIBlendStrategy",
    "KeltnerChannelBreakoutStrategy",
    "HighConvictionStrategy",
    "RegimeSwitchingRouter",
    "RegimeRouterConfig",
    "DEFAULT_LOD_HOD_STOP_BUFFER_PIPS",
    "apply_lod_hod_stop_buffer",
]

DEFAULT_LOD_HOD_STOP_BUFFER_PIPS = 8.0


def _pip_value_for(price: float) -> float:
    if price >= 50:
        return 0.01
    elif price >= 1:
        return 0.0001
    else:
        return 0.00000001


def _get_daily_high_low(bars: list[Bar]) -> tuple[float, float]:
    if not bars:
        return (0.0, 0.0)
    current_day = bars[-1].time.date()
    day_bars = [b for b in bars if b.time.date() == current_day]
    if not day_bars:
        return (0.0, 0.0)
    day_high = max(b.high for b in day_bars)
    day_low = min(b.low for b in day_bars)
    return (day_high, day_low)


def apply_lod_hod_stop_buffer(
    stop_loss: float,
    direction: TradeDirection,
    bars: list[Bar],
    buffer_pips: float = DEFAULT_LOD_HOD_STOP_BUFFER_PIPS,
) -> float:
    if not bars or buffer_pips <= 0:
        return stop_loss

    price = bars[-1].close
    pip = _pip_value_for(price)
    buffer_price = buffer_pips * pip
    day_high, day_low = _get_daily_high_low(bars)

    if day_high == 0.0 and day_low == 0.0:
        return stop_loss

    if direction == TradeDirection.LONG:
        lod_floor = day_low - buffer_price
        if stop_loss > lod_floor:
            return lod_floor
    elif direction == TradeDirection.SHORT:
        hod_ceiling = day_high + buffer_price
        if stop_loss < hod_ceiling:
            return hod_ceiling

    return stop_loss


# ISignalStrategy is now imported from .strategies.isignal_strategy
# This replaces the old duck-type class with a formal ABC that includes
# lifecycle hooks: initialize(), on_bar(), on_tick(), generate_signal(), shutdown()


class MACrossStrategy(ISignalStrategy):
    def __init__(self, fast_period: int = 5, slow_period: int = 13, atr_multiplier: float = 2.0):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_multiplier = atr_multiplier

    @property
    def name(self) -> str:
        return "MA Crossover"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.slow_period + 1:
            return None

        fast_ma = self._calculate_sma(state.bars, self.fast_period)
        slow_ma = self._calculate_sma(state.bars, self.slow_period)
        prev_fast_ma = self._calculate_sma(state.bars[:-1], self.fast_period)
        prev_slow_ma = self._calculate_sma(state.bars[:-1], self.slow_period)

        if fast_ma == 0 or slow_ma == 0 or prev_fast_ma == 0 or prev_slow_ma == 0:
            return None

        bullish_cross = prev_fast_ma <= prev_slow_ma and fast_ma > slow_ma
        bearish_cross = prev_fast_ma >= prev_slow_ma and fast_ma < slow_ma

        if not bullish_cross and not bearish_cross:
            return None

        direction = TradeDirection.LONG if bullish_cross else TradeDirection.SHORT
        atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
        entry = state.latest_bar.close
        sl = (
            entry - atr * self.atr_multiplier if direction == TradeDirection.LONG else entry + atr * self.atr_multiplier
        )
        risk = abs(entry - sl)
        tp1 = entry + risk * 1.0 if direction == TradeDirection.LONG else entry - risk * 1.0
        tp2 = entry + risk * 2.0 if direction == TradeDirection.LONG else entry - risk * 2.0
        tp3 = entry + risk * 3.0 if direction == TradeDirection.LONG else entry - risk * 3.0

        trend_strength = self._calculate_trend_strength(fast_ma, slow_ma)
        confidence = min(0.95, 0.50 + trend_strength * 0.45)

        rationale = (
            f"Bullish MA cross: fast={fast_ma:.5f} > slow={slow_ma:.5f}"
            if bullish_cross
            else f"Bearish MA cross: fast={fast_ma:.5f} < slow={slow_ma:.5f}"
        )

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

    def _calculate_sma(self, bars: list[Bar], period: int) -> float:
        if len(bars) < period:
            return 0.0
        return sum(b.close for b in bars[-period:]) / period

    def _calculate_trend_strength(self, fast_ma: float, slow_ma: float) -> float:
        if slow_ma == 0:
            return 0.0
        return min(1.0, abs(fast_ma - slow_ma) / slow_ma * 10)

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14


class BBStrategy(ISignalStrategy):
    def __init__(self, period: int = 20, std_dev: float = 2.0, atr_multiplier: float = 2.0):
        self.period = period
        self.std_dev = std_dev
        self.atr_multiplier = atr_multiplier

    @property
    def name(self) -> str:
        return "Bollinger Band Mean Reversion"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.period + 1:
            return None

        sma = self._calculate_sma(state.bars)
        std = self._calculate_std(state.bars, sma)
        if std == 0:
            return None

        upper_band = sma + std * self.std_dev
        lower_band = sma - std * self.std_dev
        latest = state.latest_bar

        if latest.close < lower_band:
            direction = TradeDirection.LONG
            entry = latest.close
            atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
            sl = entry - atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
            confidence = min(0.90, 0.60 + (lower_band - latest.close) / lower_band * 0.30)
            rationale = f"BB oversold: close={latest.close:.5f} < lower={lower_band:.5f}"
        elif latest.close > upper_band:
            direction = TradeDirection.SHORT
            entry = latest.close
            atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
            sl = entry + atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0
            confidence = min(0.90, 0.60 + (latest.close - upper_band) / upper_band * 0.30)
            rationale = f"BB overbought: close={latest.close:.5f} > upper={upper_band:.5f}"
        else:
            return None

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

    def _calculate_sma(self, bars: list[Bar]) -> float:
        if len(bars) < self.period:
            return 0.0
        return sum(b.close for b in bars[-self.period :]) / self.period

    def _calculate_std(self, bars: list[Bar], sma: float) -> float:
        if len(bars) < self.period:
            return 0.0
        variance = sum((b.close - sma) ** 2 for b in bars[-self.period :]) / self.period
        return variance**0.5

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14


class RSIStrategy(ISignalStrategy):
    def __init__(
        self,
        period: int = 14,
        oversold: float = 35.0,
        overbought: float = 65.0,
        mid: float = 50.0,
        atr_multiplier: float = 2.0,
    ):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.mid = mid
        self.atr_multiplier = atr_multiplier

    @property
    def name(self) -> str:
        return "RSI Divergence"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.period + 2:
            return None

        rsi = self._calculate_rsi(state.bars)
        if rsi is None:
            return None

        latest = state.latest_bar

        if rsi < self.oversold:
            direction = TradeDirection.LONG
            atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
            entry = latest.close
            sl = entry - atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
            confidence = min(0.85, 0.55 + (self.oversold - rsi) / self.oversold * 0.30)
            rationale = f"RSI oversold: rsi={rsi:.1f} < {self.oversold}"
        elif rsi > self.overbought:
            direction = TradeDirection.SHORT
            atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
            entry = latest.close
            sl = entry + atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0
            confidence = min(0.85, 0.55 + (rsi - self.overbought) / (100 - self.overbought) * 0.30)
            rationale = f"RSI overbought: rsi={rsi:.1f} > {self.overbought}"
        else:
            return None

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

    def _calculate_rsi(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.period + 1:
            return None

        gains: list[float] = []
        losses: list[float] = []
        for i in range(len(bars) - self.period, len(bars)):
            change = bars[i].close - bars[i - 1].close
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))

        avg_gain = sum(gains) / self.period
        avg_loss = sum(losses) / self.period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14


class SRBreakoutStrategy(ISignalStrategy):
    def __init__(
        self,
        lookback: int = 50,
        confirmation_bars: int = 1,
        breakout_threshold: float = 0.0001,
        atr_multiplier: float = 2.0,
    ):
        self.lookback = lookback
        self.confirmation_bars = confirmation_bars
        self.breakout_threshold = breakout_threshold
        self.atr_multiplier = atr_multiplier

    @property
    def name(self) -> str:
        return "S/R Breakout"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.lookback + self.confirmation_bars:
            return None

        lookback_bars = state.bars[-self.lookback - 1 : -1]
        resistance = max(b.high for b in lookback_bars)
        support = min(b.low for b in lookback_bars)
        latest = state.latest_bar

        bullish_breakout = False
        bearish_breakout = False

        if latest.close > resistance + self.breakout_threshold:
            confirm_count = 0
            for i in range(len(state.bars) - self.confirmation_bars, len(state.bars)):
                if state.bars[i].close > resistance:
                    confirm_count += 1
            if confirm_count >= self.confirmation_bars:
                bullish_breakout = True

        if latest.close < support - self.breakout_threshold:
            confirm_count = 0
            for i in range(len(state.bars) - self.confirmation_bars, len(state.bars)):
                if state.bars[i].close < support:
                    confirm_count += 1
            if confirm_count >= self.confirmation_bars:
                bearish_breakout = True

        if not bullish_breakout and not bearish_breakout:
            return None

        if bullish_breakout:
            direction = TradeDirection.LONG
            entry = latest.close
            sl = support
            risk = abs(entry - sl)
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
            confidence = min(0.85, 0.50 + (latest.close - resistance) / resistance * 0.35)
            rationale = f"Bullish S/R breakout: close={latest.close:.5f} > resistance={resistance:.5f}"
        else:
            direction = TradeDirection.SHORT
            entry = latest.close
            sl = resistance
            risk = abs(entry - sl)
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0
            confidence = min(0.85, 0.50 + (support - latest.close) / support * 0.35)
            rationale = f"Bearish S/R breakout: close={latest.close:.5f} < support={support:.5f}"

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

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14


class ROCMStrategy(ISignalStrategy):
    def __init__(self, period: int = 12, roc_threshold: float = 0.3, atr_multiplier: float = 2.0):
        self.period = period
        self.roc_threshold = roc_threshold
        self.atr_multiplier = atr_multiplier

    @property
    def name(self) -> str:
        return "Momentum ROC"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.period + 1:
            return None

        roc = self._calculate_roc(state.bars)
        if roc is None:
            return None

        latest = state.latest_bar
        atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)

        if roc > self.roc_threshold:
            direction = TradeDirection.LONG
            entry = latest.close
            sl = entry - atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
            confidence = min(0.85, 0.50 + min(roc, 2.0) / 2.0 * 0.35)
            rationale = f"Positive momentum ROC: roc={roc:.3f}% > {self.roc_threshold}%"
        elif roc < -self.roc_threshold:
            direction = TradeDirection.SHORT
            entry = latest.close
            sl = entry + atr * self.atr_multiplier
            risk = abs(entry - sl)
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0
            confidence = min(0.85, 0.50 + min(abs(roc), 2.0) / 2.0 * 0.35)
            rationale = f"Negative momentum ROC: roc={roc:.3f}% < -{self.roc_threshold}%"
        else:
            return None

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

    def _calculate_roc(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.period + 1:
            return None
        current_close = bars[-1].close
        past_close = bars[-self.period - 1].close
        if past_close == 0:
            return None
        return ((current_close - past_close) / past_close) * 100

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14


class MomentumBreakoutStrategy(ISignalStrategy):
    """Momentum/Breakout strategy using EMA crossover with ADX trend confirmation.

    Entry signals are generated when the fast EMA crosses the slow EMA
    AND the ADX indicator is above the threshold (indicating a strong trend).
    An optional RSI filter blocks entries when RSI is in overbought/oversold zones.
    Stop loss is calculated using ATR multiplier. Take profit levels are
    set at 1R, 2R, and 3R risk multiples.

    Args:
        fast_period: Period for fast EMA (default 9).
        slow_period: Period for slow EMA (default 21).
        adx_period: Period for ADX calculation (default 14).
        adx_threshold: Minimum ADX value to confirm trend (default 25.0).
        atr_multiplier: ATR multiplier for stop loss (default 2.0).
        rsi_period: Period for RSI filter. None disables the filter (default None).
        rsi_overbought: RSI level above which longs are blocked (default 70.0).
        rsi_oversold: RSI level below which shorts are blocked (default 30.0).
        lod_hod_stop_buffer_pips: Minimum pip buffer beyond daily high/low for stops (default 8.0).
    """

    def __init__(
        self,
        fast_period: int = 9,
        slow_period: int = 21,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        atr_multiplier: float = 2.0,
        rsi_period: int | None = None,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        lod_hod_stop_buffer_pips: float = DEFAULT_LOD_HOD_STOP_BUFFER_PIPS,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.atr_multiplier = atr_multiplier
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.lod_hod_stop_buffer_pips = lod_hod_stop_buffer_pips

    @property
    def name(self) -> str:
        """Return strategy name."""
        return "Momentum Breakout"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        """Evaluate market state and generate trading signal if conditions are met.

        Args:
            state: Current market state containing OHLC bars and session info.

        Returns:
            StrategySignal if entry conditions are met (EMA crossover + ADX > threshold),
            None otherwise.
        """
        if len(state.bars) < self.slow_period + self.adx_period + 1:
            return None

        fast_ema = self._calculate_ema(state.bars, self.fast_period)
        slow_ema = self._calculate_ema(state.bars, self.slow_period)
        prev_fast_ema = self._calculate_ema(state.bars[:-1], self.fast_period)
        prev_slow_ema = self._calculate_ema(state.bars[:-1], self.slow_period)

        if fast_ema == 0 or slow_ema == 0 or prev_fast_ema == 0 or prev_slow_ema == 0:
            return None

        adx = self._calculate_adx(state.bars)
        if adx is None or adx < self.adx_threshold:
            return None

        bullish_cross = prev_fast_ema <= prev_slow_ema and fast_ema > slow_ema
        bearish_cross = prev_fast_ema >= prev_slow_ema and fast_ema < slow_ema

        if not bullish_cross and not bearish_cross:
            return None

        direction = TradeDirection.LONG if bullish_cross else TradeDirection.SHORT

        if self.rsi_period is not None:
            rsi = self._calculate_rsi(state.bars, self.rsi_period)
            if rsi is None:
                return None
            if direction == TradeDirection.LONG and rsi >= self.rsi_overbought:
                return None
            if direction == TradeDirection.SHORT and rsi <= self.rsi_oversold:
                return None
        atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
        entry = state.latest_bar.close
        sl = (
            entry - atr * self.atr_multiplier if direction == TradeDirection.LONG else entry + atr * self.atr_multiplier
        )
        sl = apply_lod_hod_stop_buffer(sl, direction, state.bars, self.lod_hod_stop_buffer_pips)
        risk = abs(entry - sl)
        tp1 = entry + risk * 1.0 if direction == TradeDirection.LONG else entry - risk * 1.0
        tp2 = entry + risk * 2.0 if direction == TradeDirection.LONG else entry - risk * 2.0
        tp3 = entry + risk * 3.0 if direction == TradeDirection.LONG else entry - risk * 3.0

        if adx >= 40:
            confidence = 0.8
        else:
            confidence = 0.6

        rationale = (
            f"Bullish EMA cross + ADX confirm: fast={fast_ema:.5f} > slow={slow_ema:.5f}, ADX={adx:.1f} > {self.adx_threshold}"  # noqa: E501
            if bullish_cross
            else f"Bearish EMA cross + ADX confirm: fast={fast_ema:.5f} < slow={slow_ema:.5f}, ADX={adx:.1f} > {self.adx_threshold}"  # noqa: E501
        )

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

    def _calculate_ema(self, bars: list[Bar], period: int) -> float:
        if len(bars) < period:
            return 0.0
        multiplier = 2.0 / (period + 1)
        ema = sum(b.close for b in bars[:period]) / period
        for bar in bars[period:]:
            ema = (bar.close - ema) * multiplier + ema
        return ema

    def _calculate_adx(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.adx_period + 1:
            return None

        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]

        plus_dm_list = []
        minus_dm_list = []
        tr_list = []

        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)

            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]

            if high_diff > low_diff and high_diff > 0:
                plus_dm_list.append(high_diff)
            else:
                plus_dm_list.append(0)
            if low_diff > high_diff and low_diff > 0:
                minus_dm_list.append(low_diff)
            else:
                minus_dm_list.append(0)

        if len(tr_list) < self.adx_period:
            return None

        tr_sum = sum(tr_list[: self.adx_period])
        plus_dm_sum = sum(plus_dm_list[: self.adx_period])
        minus_dm_sum = sum(minus_dm_list[: self.adx_period])

        if tr_sum == 0:
            return None

        plus_di = (plus_dm_sum / tr_sum) * 100
        minus_di = (minus_dm_sum / tr_sum) * 100

        if plus_di + minus_di == 0:
            return 0.0

        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

        adx = dx
        for i in range(self.adx_period, len(tr_list)):
            tr_sum = tr_sum - tr_sum / self.adx_period + tr_list[i]
            plus_dm_sum = plus_dm_sum - plus_dm_sum / self.adx_period + plus_dm_list[i]
            minus_dm_sum = minus_dm_sum - minus_dm_sum / self.adx_period + minus_dm_list[i]

            if tr_sum == 0:
                continue

            plus_di = (plus_dm_sum / tr_sum) * 100
            minus_di = (minus_dm_sum / tr_sum) * 100

            if plus_di + minus_di == 0:
                dx = 0
            else:
                dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

            adx = (adx * (self.adx_period - 1) + dx) / self.adx_period

        return adx

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14

    def _calculate_rsi(self, bars: list[Bar], period: int) -> float | None:
        if len(bars) < period + 1:
            return None
        deltas = [bars[i].close - bars[i - 1].close for i in range(1, len(bars))]
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        if avg_loss == 0:
            return 100.0
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))


class CommodityTrendStrategy(ISignalStrategy):
    """Trend following strategy for commodity markets using EMA crossover and ADX confirmation.

    Entry rules:
        - EMA 20/50 bullish crossover (fast EMA crosses above slow EMA)
        - ADX > threshold (default 25) confirms trend strength

    Exit rules:
        - Stop loss: 1.75x ATR (gold ATR is higher than forex)
        - Take profit: 2-3x risk
        - Confidence scales with ADX value

    Designed for XAUUSD H1 with gold's higher ATR characteristics.
    """

    def __init__(
        self,
        fast_ema_period: int = 20,
        slow_ema_period: int = 50,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        atr_multiplier: float = 1.75,
        risk_reward_ratio: float = 2.0,
    ):
        self.fast_ema_period = fast_ema_period
        self.slow_ema_period = slow_ema_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.atr_multiplier = atr_multiplier
        self.risk_reward_ratio = risk_reward_ratio

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14

    @property
    def name(self) -> str:
        return "Commodity Trend Following"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.slow_ema_period + self.adx_period + 1:
            return None

        fast_ema = self._calculate_ema(state.bars, self.fast_ema_period)
        slow_ema = self._calculate_ema(state.bars, self.slow_ema_period)
        prev_fast_ema = self._calculate_ema(state.bars[:-1], self.fast_ema_period)
        prev_slow_ema = self._calculate_ema(state.bars[:-1], self.slow_ema_period)

        if fast_ema == 0 or slow_ema == 0 or prev_fast_ema == 0 or prev_slow_ema == 0:
            return None

        adx = self._calculate_adx(state.bars)
        if adx is None or adx < self.adx_threshold:
            return None

        bullish_cross = prev_fast_ema <= prev_slow_ema and fast_ema > slow_ema
        bearish_cross = prev_fast_ema >= prev_slow_ema and fast_ema < slow_ema

        if not bullish_cross and not bearish_cross:
            return None

        direction = TradeDirection.LONG if bullish_cross else TradeDirection.SHORT
        atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
        entry = state.latest_bar.close
        sl = (
            entry - atr * self.atr_multiplier if direction == TradeDirection.LONG else entry + atr * self.atr_multiplier
        )
        risk = abs(entry - sl)
        tp1 = (
            entry + risk * self.risk_reward_ratio
            if direction == TradeDirection.LONG
            else entry - risk * self.risk_reward_ratio
        )
        tp2 = (
            entry + risk * self.risk_reward_ratio * 2.0
            if direction == TradeDirection.LONG
            else entry - risk * self.risk_reward_ratio * 2.0
        )
        tp3 = (
            entry + risk * self.risk_reward_ratio * 3.0
            if direction == TradeDirection.LONG
            else entry - risk * self.risk_reward_ratio * 3.0
        )

        confidence = min(0.90, 0.50 + (adx - self.adx_threshold) / 100 * 0.40)
        rationale = (
            f"Bullish EMA cross + ADX confirmed: fast={fast_ema:.5f} > slow={slow_ema:.5f}, ADX={adx:.1f}"
            if bullish_cross
            else f"Bearish EMA cross + ADX confirmed: fast={fast_ema:.5f} < slow={slow_ema:.5f}, ADX={adx:.1f}"
        )

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

    def _calculate_ema(self, bars: list[Bar], period: int) -> float:
        if len(bars) < period:
            return 0.0
        multiplier = 2.0 / (period + 1)
        sma = sum(b.close for b in bars[:period]) / period
        ema = sma
        for bar in bars[period:]:
            ema = (bar.close - ema) * multiplier + ema
        return ema

    def _calculate_adx(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.adx_period + 1:
            return None

        tr_list: list[float] = []
        plus_dm_list: list[float] = []
        minus_dm_list: list[float] = []

        for i in range(1, len(bars)):
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            tr_list.append(tr)

            high_diff = bars[i].high - bars[i - 1].high
            low_diff = bars[i - 1].low - bars[i].low

            if high_diff > low_diff and high_diff > 0:
                plus_dm_list.append(high_diff)
            else:
                plus_dm_list.append(0.0)
            if low_diff > high_diff and low_diff > 0:
                minus_dm_list.append(low_diff)
            else:
                minus_dm_list.append(0.0)

        if len(tr_list) < self.adx_period:
            return None

        smoothed_tr = sum(tr_list[: self.adx_period])
        smoothed_plus_dm = sum(plus_dm_list[: self.adx_period])
        smoothed_minus_dm = sum(minus_dm_list[: self.adx_period])

        if smoothed_tr == 0:
            return 0.0

        plus_di = (smoothed_plus_dm / smoothed_tr) * 100
        minus_di = (smoothed_minus_dm / smoothed_tr) * 100

        if plus_di + minus_di == 0:
            return 0.0

        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

        adx = dx
        for i in range(self.adx_period, len(tr_list)):
            if smoothed_tr == 0:
                continue

            smoothed_tr = smoothed_tr - smoothed_tr / self.adx_period + tr_list[i]
            smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / self.adx_period + plus_dm_list[i]
            smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / self.adx_period + minus_dm_list[i]

            if smoothed_tr == 0:
                continue

            plus_di = (smoothed_plus_dm / smoothed_tr) * 100
            minus_di = (smoothed_minus_dm / smoothed_tr) * 100

            if plus_di + minus_di == 0:
                dx = 0.0
            else:
                dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

            adx = (adx * (self.adx_period - 1) + dx) / self.adx_period

        return adx


class CommodityMeanReversionStrategy(ISignalStrategy):
    """Mean reversion strategy for commodities using Bollinger Bands and RSI.

    Entry rules:
        - Long: Price below lower Bollinger Band + RSI < 30 (oversold) + bullish reversal candle
        - Short: Price above upper Bollinger Band + RSI > 70 (overbought) + bearish reversal candle

    Exit rules:
        - Stop loss: Beyond outer Bollinger Band by 0.5x ATR
        - Take profit: tp1 = Middle Band (mean reversion target), tp2/tp3 = 2x/3x risk for scaling
        - Confidence based on RSI deviation from thresholds

    Designed for XAUUSD H1 with gold's higher ATR characteristics.
    """

    def __init__(
        self,
        bb_period: int = 20,
        bb_std_dev: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        atr_multiplier: float = 2.0,
    ):
        self.bb_period = bb_period
        self.bb_std_dev = bb_std_dev
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_multiplier = atr_multiplier

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < 15:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - 14, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / 14

    @property
    def name(self) -> str:
        return "Commodity Mean Reversion"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.bb_period + 1:
            return None

        sma = self._calculate_sma(state.bars)
        std = self._calculate_std(state.bars, sma)
        if std == 0:
            return None

        upper_band = sma + std * self.bb_std_dev
        lower_band = sma - std * self.bb_std_dev
        middle_band = sma
        latest = state.latest_bar

        rsi = self._calculate_rsi(state.bars)
        if rsi is None:
            return None

        atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)

        if latest.close < lower_band and rsi < self.rsi_oversold:
            if not self._is_bullish_reversal(latest):
                return None
            direction = TradeDirection.LONG
            entry = latest.close
            sl = lower_band - atr * 0.5
            band_distance = lower_band - entry
            risk = max(abs(entry - sl), band_distance * 0.5)
            tp1 = min(middle_band, entry + risk * 1.0)
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
            confidence = min(0.90, 0.55 + (self.rsi_oversold - rsi) / self.rsi_oversold * 0.35)
            rationale = f"BB oversold + RSI oversold + bullish reversal: close={latest.close:.5f} < lower={lower_band:.5f}, RSI={rsi:.1f}"  # noqa: E501
        elif latest.close > upper_band and rsi > self.rsi_overbought:
            if not self._is_bearish_reversal(latest):
                return None
            direction = TradeDirection.SHORT
            entry = latest.close
            sl = upper_band + atr * 0.5
            band_distance = entry - upper_band
            risk = max(abs(entry - sl), band_distance * 0.5)
            tp1 = max(middle_band, entry - risk * 1.0)
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0
            confidence = min(
                0.90,
                0.55 + (rsi - self.rsi_overbought) / (100 - self.rsi_overbought) * 0.35,
            )
            rationale = (
                f"BB overbought + RSI overbought: close={latest.close:.5f} > upper={upper_band:.5f}, RSI={rsi:.1f}"  # noqa: E501
            )
        else:
            return None

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

    def _calculate_sma(self, bars: list[Bar]) -> float:
        if len(bars) < self.bb_period:
            return 0.0
        return sum(b.close for b in bars[-self.bb_period :]) / self.bb_period

    def _calculate_std(self, bars: list[Bar], sma: float) -> float:
        if len(bars) < self.bb_period:
            return 0.0
        variance = sum((b.close - sma) ** 2 for b in bars[-self.bb_period :]) / self.bb_period
        return variance**0.5

    def _calculate_rsi(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.rsi_period + 1:
            return None

        gains: list[float] = []
        losses: list[float] = []
        for i in range(len(bars) - self.rsi_period, len(bars)):
            change = bars[i].close - bars[i - 1].close
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))

        avg_gain = sum(gains) / self.rsi_period
        avg_loss = sum(losses) / self.rsi_period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _is_bullish_reversal(self, bar: Bar) -> bool:
        candle_range = bar.high - bar.low
        if candle_range == 0:
            return False
        midpoint = bar.low + candle_range * 0.5
        return bar.close > midpoint

    def _is_bearish_reversal(self, bar: Bar) -> bool:
        candle_range = bar.high - bar.low
        if candle_range == 0:
            return False
        midpoint = bar.low + candle_range * 0.5
        return bar.close < midpoint


class SupertrendRSIBlendStrategy(ISignalStrategy):
    def __init__(
        self,
        supertrend_period: int = 14,
        supertrend_multiplier: float = 2.5,
        rsi_period: int = 14,
        rsi_threshold: float = 50.0,
        atr_min_pips: float = 3.0,
        atr_period: int = 14,
        adx_period: int = 14,
        adx_min: float = 18.0,
        atr_min_chop: float = 2.0,
        sl_atr_multiplier: float = 1.5,
        hard_cap_pips: float = 40.0,
        tp1_atr: float = 1.5,
        tp2_atr: float = 2.5,
        time_exit_bars: int = 20,
    ):
        self.supertrend_period = supertrend_period
        self.supertrend_multiplier = supertrend_multiplier
        self.rsi_period = rsi_period
        self.rsi_threshold = rsi_threshold
        self.atr_min_pips = atr_min_pips
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.adx_min = adx_min
        self.atr_min_chop = atr_min_chop
        self.sl_atr_multiplier = sl_atr_multiplier
        self.hard_cap_pips = hard_cap_pips
        self.tp1_atr = tp1_atr
        self.tp2_atr = tp2_atr
        self.time_exit_bars = time_exit_bars

    @property
    def name(self) -> str:
        return "Supertrend RSI Blend"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_bars = max(self.supertrend_period, self.rsi_period, self.adx_period) + 5
        if len(state.bars) < min_bars:
            return None

        atr = self._calculate_atr(state.bars)
        atr_pips = atr * 10000

        if atr_pips < self.atr_min_chop:
            return None

        adx = self._calculate_adx(state.bars)
        if adx is not None and adx < self.adx_min:
            consecutive_low_adx = 0
            for i in range(len(state.bars) - 3, len(state.bars)):
                check_adx = self._calculate_adx(state.bars[: i + 1])
                if check_adx is not None and check_adx < self.adx_min:
                    consecutive_low_adx += 1
            if consecutive_low_adx >= 3:
                return None

        if atr_pips < self.atr_min_pips:
            return None

        supertrend_value, prev_supertrend_value = self._calculate_supertrend(state.bars)
        if supertrend_value is None or prev_supertrend_value is None:
            return None

        rsi = self._calculate_rsi(state.bars)
        if rsi is None:
            return None

        prev_rsi_values = self._get_prev_rsi_values(state.bars)

        latest = state.latest_bar

        long_conditions = (
            prev_supertrend_value < 0
            and supertrend_value > 0
            and rsi > self.rsi_threshold
            and any(r < self.rsi_threshold for r in prev_rsi_values)
        )

        short_conditions = (
            prev_supertrend_value > 0
            and supertrend_value < 0
            and rsi < self.rsi_threshold
            and any(r > self.rsi_threshold for r in prev_rsi_values)
        )

        if not long_conditions and not short_conditions:
            return None

        direction = TradeDirection.LONG if long_conditions else TradeDirection.SHORT
        entry = latest.close

        sl_distance = atr * self.sl_atr_multiplier
        sl_distance_pips = sl_distance * 10000
        if sl_distance_pips > self.hard_cap_pips:
            sl_distance = self.hard_cap_pips / 10000

        sl = entry - sl_distance if direction == TradeDirection.LONG else entry + sl_distance
        risk = abs(entry - sl)

        tp1 = entry + risk * self.tp1_atr if direction == TradeDirection.LONG else entry - risk * self.tp1_atr
        tp2 = entry + risk * self.tp2_atr if direction == TradeDirection.LONG else entry - risk * self.tp2_atr
        tp3 = entry + risk * 3.0 if direction == TradeDirection.LONG else entry - risk * 3.0

        confidence = min(0.85, 0.55 + abs(rsi - self.rsi_threshold) / 50 * 0.30)

        if long_conditions:
            rationale = (
                f"Supertrend Long flip + RSI confirm: ST={supertrend_value:.5f}, RSI={rsi:.1f} > {self.rsi_threshold}"  # noqa: E501
            )
        else:
            rationale = (
                f"Supertrend Short flip + RSI confirm: ST={supertrend_value:.5f}, RSI={rsi:.1f} < {self.rsi_threshold}"  # noqa: E501
            )

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

    def _calculate_supertrend(self, bars: list[Bar]) -> tuple[float | None, float | None]:
        if len(bars) < self.supertrend_period + 1:
            return None, None

        atr_values: list[float] = []
        for i in range(len(bars)):
            if i < self.atr_period:
                atr_values.append(0.0001)
            else:
                tr_sum = 0.0
                for j in range(i - self.atr_period + 1, i + 1):
                    if j > 0:
                        tr = max(
                            bars[j].high - bars[j].low,
                            max(
                                abs(bars[j].high - bars[j - 1].close),
                                abs(bars[j].low - bars[j - 1].close),
                            ),
                        )
                        tr_sum += tr
                atr_values.append(tr_sum / self.atr_period)

        hl2_list = [(b.high + b.low) / 2 for b in bars]
        upper_band_list = [0.0] * len(bars)
        lower_band_list = [0.0] * len(bars)
        supertrend_list = [0.0] * len(bars)

        for i in range(len(bars)):
            atr = atr_values[i]

            if i < self.supertrend_period:
                upper_band_list[i] = hl2_list[i] + atr * self.supertrend_multiplier
                lower_band_list[i] = hl2_list[i] - atr * self.supertrend_multiplier
                supertrend_list[i] = 1.0
                continue

            hl2 = hl2_list[i]
            upper_band_list[i] = hl2 + atr * self.supertrend_multiplier
            lower_band_list[i] = hl2 - atr * self.supertrend_multiplier

            prev_upper = upper_band_list[i - 1]
            prev_lower = lower_band_list[i - 1]

            prev_supertrend = supertrend_list[i - 1]

            if prev_supertrend == 1.0:
                upper_band_list[i] = prev_upper
                lower_band_list[i] = max(lower_band_list[i], prev_lower)
                if bars[i].close < lower_band_list[i]:
                    supertrend_list[i] = -1.0
                else:
                    supertrend_list[i] = 1.0
            else:
                upper_band_list[i] = min(upper_band_list[i], prev_upper)
                lower_band_list[i] = prev_lower
                if bars[i].close > upper_band_list[i]:
                    supertrend_list[i] = 1.0
                else:
                    supertrend_list[i] = -1.0

        if len(bars) < 2:
            return supertrend_list[-1], None

        return supertrend_list[-1], supertrend_list[-2]

    def _calculate_rsi(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.rsi_period + 1:
            return None

        gains: list[float] = []
        losses: list[float] = []
        for i in range(len(bars) - self.rsi_period, len(bars)):
            change = bars[i].close - bars[i - 1].close
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))

        avg_gain = sum(gains) / self.rsi_period
        avg_loss = sum(losses) / self.rsi_period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _get_prev_rsi_values(self, bars: list[Bar]) -> list[float]:
        prev_rsi_values = []
        for offset in range(1, min(4, len(bars))):
            window_bars = bars[:-offset]
            if len(window_bars) >= self.rsi_period + 1:
                rsi = self._calculate_rsi(window_bars)
                if rsi is not None:
                    prev_rsi_values.append(rsi)
        return prev_rsi_values

    def _calculate_adx(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.adx_period + 1:
            return None

        tr_list: list[float] = []
        plus_dm_list: list[float] = []
        minus_dm_list: list[float] = []

        for i in range(1, len(bars)):
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            tr_list.append(tr)

            high_diff = bars[i].high - bars[i - 1].high
            low_diff = bars[i - 1].low - bars[i].low

            if high_diff > low_diff and high_diff > 0:
                plus_dm_list.append(high_diff)
            else:
                plus_dm_list.append(0.0)
            if low_diff > high_diff and low_diff > 0:
                minus_dm_list.append(low_diff)
            else:
                minus_dm_list.append(0.0)

        if len(tr_list) < self.adx_period:
            return None

        smoothed_tr = sum(tr_list[: self.adx_period])
        smoothed_plus_dm = sum(plus_dm_list[: self.adx_period])
        smoothed_minus_dm = sum(minus_dm_list[: self.adx_period])

        if smoothed_tr == 0:
            return 0.0

        plus_di = (smoothed_plus_dm / smoothed_tr) * 100
        minus_di = (smoothed_minus_dm / smoothed_tr) * 100

        if plus_di + minus_di == 0:
            return 0.0

        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

        adx = dx
        for i in range(self.adx_period, len(tr_list)):
            if smoothed_tr == 0:
                continue

            smoothed_tr = smoothed_tr - smoothed_tr / self.adx_period + tr_list[i]
            smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / self.adx_period + plus_dm_list[i]
            smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / self.adx_period + minus_dm_list[i]

            if smoothed_tr == 0:
                continue

            plus_di = (smoothed_plus_dm / smoothed_tr) * 100
            minus_di = (smoothed_minus_dm / smoothed_tr) * 100

            if plus_di + minus_di == 0:
                dx = 0.0
            else:
                dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

            adx = (adx * (self.adx_period - 1) + dx) / self.adx_period

        return adx

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < self.atr_period + 1:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - self.atr_period, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / self.atr_period


class KeltnerChannelBreakoutStrategy(ISignalStrategy):
    """Keltner Channel Breakout strategy using EMA-based channels with ATR bands.

    Entry signals are generated when price closes outside the Keltner Channel
    (above upper band for long, below lower band for short) with confirmation
    from ADX trend strength, EMA slope direction, ATR volatility threshold,
    and volume confirmation.

    Stop loss is 1.5x ATR with a hard cap of 40 pips. Take profit levels
    are set at 2.0x ATR (TP1, 33%), 3.0x ATR (TP2, 33%), 3.0x risk (TP3, 34%).

    Args:
        ema_period: Period for EMA middle line (default 20).
        atr_period: Period for ATR calculation (default 14).
        atr_multiplier: ATR multiplier for channel bands (default 1.5).
        atr_min_pips: Minimum ATR in pips for volatility filter (default 10).
        adx_period: Period for ADX calculation (default 14).
        adx_threshold: Minimum ADX value to confirm trend (default 15.0).
        volume_ma_period: Period for volume moving average (default 20).
        sl_atr_multiplier: ATR multiplier for stop loss (default 1.5).
        sl_max_pips: Hard cap on stop loss in pips (default 40.0).
        tp1_atr_multiplier: ATR multiplier for TP1 (default 2.0).
        tp2_atr_multiplier: ATR multiplier for TP2 (default 3.0).
    """

    def __init__(
        self,
        ema_period: int = 20,
        atr_period: int = 14,
        atr_multiplier: float = 1.5,
        atr_min_pips: float = 2.0,
        adx_period: int = 14,
        adx_threshold: float = 12.0,
        volume_ma_period: int = 20,
        use_volume_filter: bool = False,
        sl_atr_multiplier: float = 1.5,
        sl_max_pips: float = 40.0,
        tp1_atr_multiplier: float = 2.0,
        tp2_atr_multiplier: float = 3.0,
        lod_hod_stop_buffer_pips: float = DEFAULT_LOD_HOD_STOP_BUFFER_PIPS,
    ):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.atr_min_pips = atr_min_pips
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.volume_ma_period = volume_ma_period
        self.use_volume_filter = use_volume_filter
        self.sl_atr_multiplier = sl_atr_multiplier
        self.sl_max_pips = sl_max_pips
        self.tp1_atr_multiplier = tp1_atr_multiplier
        self.tp2_atr_multiplier = tp2_atr_multiplier
        self.lod_hod_stop_buffer_pips = lod_hod_stop_buffer_pips

    @property
    def name(self) -> str:
        return "Keltner Channel Breakout"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_bars = (
            max(
                self.ema_period,
                self.atr_period,
                self.adx_period * 2 + 1,
                self.volume_ma_period,
            )
            + 2
        )
        if len(state.bars) < min_bars:
            return None

        close = state.latest_bar.close
        prev_close = state.bars[-2].close

        middle = self._calculate_ema(state.bars, self.ema_period)
        prev_middle = self._calculate_ema(state.bars[:-1], self.ema_period)
        if middle == 0 or prev_middle == 0:
            return None

        atr = self._calculate_atr(state.bars, self.atr_period)
        if atr <= 0:
            return None

        pip_value = self._get_pip_value(close)
        atr_pips = atr / pip_value
        if atr_pips < self.atr_min_pips:
            return None

        upper = middle + self.atr_multiplier * atr
        lower = middle - self.atr_multiplier * atr
        prev_atr = self._calculate_atr(state.bars[:-1], self.atr_period)
        prev_upper = prev_middle + self.atr_multiplier * prev_atr
        prev_lower = prev_middle - self.atr_multiplier * prev_atr

        adx = self._calculate_adx(state.bars)
        if adx is None or adx < self.adx_threshold:
            return None

        if self.use_volume_filter:
            volume = state.latest_bar.volume
            if volume <= 0:
                return None
            vol_ma = self._calculate_volume_ma(state.bars)
            if vol_ma <= 0 or volume < vol_ma:
                return None

        ema_rising = middle > prev_middle
        ema_falling = middle < prev_middle

        long_breakout = prev_close <= prev_upper and close > upper and ema_rising
        short_breakout = prev_close >= prev_lower and close < lower and ema_falling

        if not long_breakout and not short_breakout:
            return None

        direction = TradeDirection.LONG if long_breakout else TradeDirection.SHORT

        sl_distance = self.sl_atr_multiplier * atr
        sl_pips = sl_distance / pip_value
        if sl_pips > self.sl_max_pips:
            sl_distance = self.sl_max_pips * pip_value

        entry = close
        if direction == TradeDirection.LONG:
            sl = entry - sl_distance
            tp1 = entry + self.tp1_atr_multiplier * atr
            tp2 = entry + self.tp2_atr_multiplier * atr
        else:
            sl = entry + sl_distance
            tp1 = entry - self.tp1_atr_multiplier * atr
            tp2 = entry - self.tp2_atr_multiplier * atr

        sl = apply_lod_hod_stop_buffer(sl, direction, state.bars, self.lod_hod_stop_buffer_pips)

        risk = abs(entry - sl)
        if direction == TradeDirection.LONG:
            tp3 = entry + risk * 3.0
        else:
            tp3 = entry - risk * 3.0

        confidence = 0.6
        if adx >= 40:
            confidence = 0.8
        elif adx >= 30:
            confidence = 0.7

        rationale = (
            f"Long KC breakout: close={close:.5f} > upper={upper:.5f}, "
            f"EMA={middle:.5f} rising, ADX={adx:.1f}, ATR(pips)={atr_pips:.1f}"
            if long_breakout
            else f"Short KC breakout: close={close:.5f} < lower={lower:.5f}, "
            f"EMA={middle:.5f} falling, ADX={adx:.1f}, ATR(pips)={atr_pips:.1f}"
        )

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

    def _calculate_ema(self, bars: list[Bar], period: int) -> float:
        if len(bars) < period:
            return 0.0
        multiplier = 2.0 / (period + 1)
        ema = sum(b.close for b in bars[:period]) / period
        for bar in bars[period:]:
            ema = (bar.close - ema) * multiplier + ema
        return ema

    def _calculate_atr(self, bars: list[Bar], period: int) -> float:
        if len(bars) < period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(len(bars) - period, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / period

    def _calculate_adx(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.adx_period * 2 + 1:
            return None

        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]

        plus_dm_list = []
        minus_dm_list = []
        tr_list = []

        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)

            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]

            if high_diff > low_diff and high_diff > 0:
                plus_dm_list.append(high_diff)
            else:
                plus_dm_list.append(0)
            if low_diff > high_diff and low_diff > 0:
                minus_dm_list.append(low_diff)
            else:
                minus_dm_list.append(0)

        if len(tr_list) < self.adx_period:
            return None

        tr_sum = sum(tr_list[: self.adx_period])
        plus_dm_sum = sum(plus_dm_list[: self.adx_period])
        minus_dm_sum = sum(minus_dm_list[: self.adx_period])

        if tr_sum == 0:
            return None

        plus_di = (plus_dm_sum / tr_sum) * 100
        minus_di = (minus_dm_sum / tr_sum) * 100

        if plus_di + minus_di == 0:
            return 0.0

        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

        adx = dx
        for i in range(self.adx_period, len(tr_list)):
            tr_sum = tr_sum - tr_sum / self.adx_period + tr_list[i]
            plus_dm_sum = plus_dm_sum - plus_dm_sum / self.adx_period + plus_dm_list[i]
            minus_dm_sum = minus_dm_sum - minus_dm_sum / self.adx_period + minus_dm_list[i]

            if tr_sum == 0:
                continue

            plus_di = (plus_dm_sum / tr_sum) * 100
            minus_di = (minus_dm_sum / tr_sum) * 100

            if plus_di + minus_di == 0:
                dx = 0
            else:
                dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

            adx = (adx * (self.adx_period - 1) + dx) / self.adx_period

        return adx

    def _calculate_volume_ma(self, bars: list[Bar]) -> float:
        if len(bars) < self.volume_ma_period:
            return 0.0
        recent = bars[-self.volume_ma_period :]
        return sum(b.volume for b in recent) / len(recent)

    @staticmethod
    def _get_pip_value(price: float) -> float:
        if price >= 50:
            return 0.01
        elif price >= 1:
            return 0.0001
        else:
            return 0.00000001


class HighConvictionStrategy(ISignalStrategy):
    def __init__(
        self,
        trend_lookback: int = 20,
        swing_lookback: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        atr_percentile_lookback: int = 100,
        atr_percentile_threshold: float = 0.60,
        sl_atr_mult: float = 3.0,
        tp_atr_mult: float = 6.0,
        allowed_sessions: list[str] | None = None,
        min_confluences: int = 5,
        source_timeframe_minutes: int = 240,
        max_trades_per_week: int = 1,
    ):
        self.trend_lookback = trend_lookback
        self.swing_lookback = swing_lookback
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.atr_percentile_lookback = atr_percentile_lookback
        self.atr_percentile_threshold = atr_percentile_threshold
        self.sl_atr_mult = sl_atr_mult
        self.tp_atr_mult = tp_atr_mult
        if allowed_sessions is None:
            self.allowed_sessions = {SessionType.LONDON, SessionType.NY_AM}
        else:
            self.allowed_sessions = {SessionType(s) for s in allowed_sessions}
        self.min_confluences = min_confluences
        self.source_timeframe_minutes = source_timeframe_minutes
        self.max_trades_per_week = max_trades_per_week
        self._last_trade_time: datetime | None = None

    @property
    def name(self) -> str:
        return "High Conviction"

    def reset(self) -> None:
        self._last_trade_time = None

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        d1_bars = self._resample_to_daily(state.bars)
        min_bars = (
            max(
                self.trend_lookback * 6,
                self.swing_lookback,
                self.rsi_period,
                self.atr_period,
                self.atr_percentile_lookback,
            )
            + 5
        )
        if len(state.bars) < min_bars:
            return None
        if len(d1_bars) < self.trend_lookback + 2:
            return None

        if self.max_trades_per_week > 0 and not self._can_trade_this_week(state):
            return None

        atr = self._calculate_atr(state.bars, self.atr_period)
        if atr <= 0:
            return None

        trend_dir = self._detect_d1_trend(d1_bars)
        if trend_dir is None:
            return None

        confluences = 0
        labels = []

        if state.current_session in self.allowed_sessions:
            confluences += 1
            labels.append(f"session={state.current_session.value}")

        if self._atr_in_upper_percentile(state.bars, atr):
            confluences += 1
            labels.append(f"ATR={atr * 10000:.1f}p")

        if self._at_pullback_level(state.bars, trend_dir):
            confluences += 1
            labels.append("pullback")

        momentum_shift = self._detect_momentum_shift(state.bars)
        if momentum_shift == trend_dir:
            confluences += 1
            labels.append("momentum shift")

        if confluences < self.min_confluences:
            return None

        self._last_trade_time = state.latest_bar.time

        entry = state.latest_bar.close
        sl_distance = atr * self.sl_atr_mult
        sl = entry - sl_distance if trend_dir == TradeDirection.LONG else entry + sl_distance
        risk = abs(entry - sl)

        rr_ratio = self.tp_atr_mult / self.sl_atr_mult
        tp2 = entry + risk * rr_ratio if trend_dir == TradeDirection.LONG else entry - risk * rr_ratio

        tp3 = entry + risk * 3.0 if trend_dir == TradeDirection.LONG else entry - risk * 3.0

        confidence = 0.75
        rationale = (
            f"High Conviction {trend_dir.value}: "
            f"D1 trend={'bullish' if trend_dir == TradeDirection.LONG else 'bearish'}, " + ", ".join(labels)
        )

        return StrategySignal(
            direction=trend_dir,
            confidence=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp2 * 0.5 + sl * 0.5,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )

    def _can_trade_this_week(self, state: MarketState) -> bool:
        if self._last_trade_time is None:
            return True
        now = state.latest_bar.time
        days_since = (now.date() - self._last_trade_time.date()).days
        if days_since >= 7:
            return True
        if now.weekday() < self._last_trade_time.weekday() or days_since >= 4:
            return True
        return False

    def _resample_to_daily(self, bars: list[Bar]) -> list[Bar]:

        if not bars:
            return []
        grouped: dict = {}
        for bar in bars:
            day_key = bar.time.date()
            if day_key not in grouped:
                grouped[day_key] = []
            grouped[day_key].append(bar)
        result = []
        for day in sorted(grouped.keys()):
            group = grouped[day]
            result.append(
                Bar(
                    time=group[0].time,
                    open=group[0].open,
                    high=max(b.high for b in group),
                    low=min(b.low for b in group),
                    close=group[-1].close,
                    volume=sum(b.volume for b in group),
                )
            )
        return result

    def _detect_d1_trend(self, d1_bars: list[Bar]) -> TradeDirection | None:
        if len(d1_bars) < self.trend_lookback:
            return None
        recent = d1_bars[-self.trend_lookback :]
        closes = [b.close for b in recent]
        bullish = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
        bearish = len(closes) - 1 - bullish
        net_move = closes[-1] - closes[0]
        if net_move > 0 and bullish >= self.trend_lookback * 0.65:
            return TradeDirection.LONG
        if net_move < 0 and bearish >= self.trend_lookback * 0.65:
            return TradeDirection.SHORT
        return None

    def _at_pullback_level(self, bars: list[Bar], trend_dir: TradeDirection) -> bool:
        if len(bars) < self.swing_lookback:
            return False
        lookback = bars[-self.swing_lookback :]
        recent = bars[-5:]
        if trend_dir == TradeDirection.LONG:
            swing_lows = sorted(set(b.low for b in lookback))
            if len(swing_lows) < 5:
                return False
            support_level = swing_lows[len(swing_lows) // 4]
            return any(abs(b.low - support_level) < support_level * 0.001 for b in recent)
        else:
            swing_highs = sorted(set(b.high for b in lookback), reverse=True)
            if len(swing_highs) < 5:
                return False
            resistance_level = swing_highs[len(swing_highs) // 4]
            return any(abs(b.high - resistance_level) < resistance_level * 0.001 for b in recent)

    def _detect_momentum_shift(self, bars: list[Bar]) -> TradeDirection | None:
        if len(bars) < self.rsi_period + 2:
            return None
        rsi = self._calculate_rsi(bars)
        prev_rsi = self._calculate_rsi(bars[:-1])
        if rsi is None or prev_rsi is None:
            return None
        if prev_rsi < 50 and rsi > 50:
            return TradeDirection.LONG
        if prev_rsi > 50 and rsi < 50:
            return TradeDirection.SHORT
        closes = [b.close for b in bars[-5:]]
        if len(closes) < 5:
            return None
        momentum = closes[-1] - closes[0]
        prev_momentum = closes[-2] - closes[-5] if len(closes) >= 5 else 0
        if prev_momentum < 0 and momentum > 0:
            return TradeDirection.LONG
        if prev_momentum > 0 and momentum < 0:
            return TradeDirection.SHORT
        return None

    def _atr_in_upper_percentile(self, bars: list[Bar], current_atr: float) -> bool:
        n = min(self.atr_percentile_lookback, len(bars) - self.atr_period)
        if n < 10:
            return True
        atr_values = []
        for i in range(n):
            start = len(bars) - self.atr_period - 1 - i
            if start < 0:
                break
            atr_val = self._calculate_atr(bars[: len(bars) - i], self.atr_period)
            if atr_val > 0:
                atr_values.append(atr_val)
        if len(atr_values) < 10:
            return True
        atr_values.sort()
        rank = sum(1 for v in atr_values if v <= current_atr)
        percentile = rank / len(atr_values)
        return percentile >= self.atr_percentile_threshold

    def _calculate_atr(self, bars: list[Bar], period: int) -> float:
        if len(bars) < period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(len(bars) - period, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / period

    def _calculate_rsi(self, bars: list[Bar]) -> float | None:
        if len(bars) < self.rsi_period + 1:
            return None
        deltas = [bars[i].close - bars[i - 1].close for i in range(1, len(bars))]
        recent = deltas[-self.rsi_period :]
        gains = [d for d in recent if d > 0]
        losses = [-d for d in recent if d < 0]
        avg_gain = sum(gains) / self.rsi_period if gains else 0
        avg_loss = sum(losses) / self.rsi_period if losses else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True)
class RegimeRouterConfig:
    adx_trend_threshold: float = 25.0
    adx_strong_trend_threshold: float = 40.0
    adx_range_threshold: float = 20.0
    atr_volatility_percentile: float = 75.0
    atr_lookback: int = 50
    adx_period: int = 14
    trending_size_multiplier: float = 1.0
    ranging_size_multiplier: float = 1.0
    volatile_size_multiplier: float = 0.5
    transition_size_multiplier: float = 0.5
    min_confidence: float = 0.55


def _default_trending_strategies() -> list[ISignalStrategy]:
    return [MomentumBreakoutStrategy(fast_period=9, slow_period=21, adx_threshold=25.0)]


def _default_ranging_strategies() -> list[ISignalStrategy]:
    from strategies.session_range_mean_reversion import (
        SessionRangeMeanReversionStrategy,
    )

    return [SessionRangeMeanReversionStrategy()]


def _default_volatile_strategies() -> list[ISignalStrategy]:
    from strategies.volatility_squeeze import VolatilitySqueezeStrategy

    return [VolatilitySqueezeStrategy()]


class RegimeSwitchingRouter(ISignalStrategy):
    def __init__(
        self,
        trending_strategies: list[ISignalStrategy] | None = None,
        ranging_strategies: list[ISignalStrategy] | None = None,
        volatile_strategies: list[ISignalStrategy] | None = None,
        transition_strategies: list[ISignalStrategy] | None = None,
        config: RegimeRouterConfig | None = None,
    ):
        self.config = config or RegimeRouterConfig()
        self.trending_strategies = trending_strategies or _default_trending_strategies()
        self.ranging_strategies = ranging_strategies or _default_ranging_strategies()
        self.volatile_strategies = volatile_strategies or _default_volatile_strategies()
        self.transition_strategies = transition_strategies or []
        self._current_regime: str = "neutral"

    @property
    def name(self) -> str:
        return "Regime-Switching Router"

    @property
    def current_regime(self) -> str:
        return self._current_regime

    def reset(self) -> None:
        for strategy in (
            self.trending_strategies + self.ranging_strategies + self.volatile_strategies + self.transition_strategies
        ):
            if hasattr(strategy, "reset") and callable(strategy.reset):
                strategy.reset()
        self._current_regime = "neutral"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.config.adx_period + self.config.atr_lookback + 1:
            return None

        regime, confidence, size_mult = self._detect_regime(state)
        self._current_regime = regime

        strategies = self._get_strategies_for_regime(regime)
        if not strategies:
            return None

        best_signal: StrategySignal | None = None
        for strategy in strategies:
            signal = strategy.evaluate(state)
            if signal is not None and signal.confidence >= self.config.min_confidence:
                if best_signal is None or signal.confidence > best_signal.confidence:
                    best_signal = signal

        if best_signal is None:
            return None

        scaled_confidence = best_signal.confidence * size_mult
        if scaled_confidence < self.config.min_confidence:
            return None

        return StrategySignal(
            direction=best_signal.direction,
            confidence=min(scaled_confidence, 0.95),
            entry_price=best_signal.entry_price,
            stop_loss=best_signal.stop_loss,
            take_profit_1=best_signal.take_profit_1,
            take_profit_2=best_signal.take_profit_2,
            take_profit_3=best_signal.take_profit_3,
            rationale=f"[{regime}] {best_signal.rationale}",
        )

    def _detect_regime(self, state: MarketState) -> tuple[str, float, float]:
        adx = self._calculate_adx(state.bars)
        atr_percentile = self._calculate_atr_percentile(state.bars)

        is_trending = adx > self.config.adx_trend_threshold
        is_strong_trending = adx > self.config.adx_strong_trend_threshold
        is_ranging = adx < self.config.adx_range_threshold
        is_volatile = atr_percentile > self.config.atr_volatility_percentile

        if is_strong_trending:
            regime = "trending"
            size_mult = self.config.trending_size_multiplier
        elif is_volatile and not is_ranging:
            regime = "volatile"
            size_mult = self.config.volatile_size_multiplier
        elif is_trending:
            regime = "trending"
            size_mult = self.config.trending_size_multiplier
        elif is_ranging:
            regime = "ranging"
            size_mult = self.config.ranging_size_multiplier
        else:
            regime = "transition"
            size_mult = self.config.transition_size_multiplier

        raw_confidence = 0.0
        if is_strong_trending or is_trending:
            raw_confidence = min(adx / 50.0, 1.0)
        elif is_ranging:
            raw_confidence = min(
                (self.config.adx_range_threshold - adx) / self.config.adx_range_threshold,
                1.0,
            )
        elif is_volatile:
            raw_confidence = min(atr_percentile / 100.0, 1.0)
        else:
            raw_confidence = 0.5

        return regime, raw_confidence, size_mult

    def _get_strategies_for_regime(self, regime: str) -> list[ISignalStrategy]:
        if regime == "trending":
            return self.trending_strategies
        elif regime == "ranging":
            return self.ranging_strategies
        elif regime == "volatile":
            return self.volatile_strategies
        else:
            return self.transition_strategies

    def _calculate_adx(self, bars: list[Bar]) -> float:
        period = self.config.adx_period
        if len(bars) < period * 2 + 1:
            return 0.0

        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]

        plus_dm_list: list[float] = []
        minus_dm_list: list[float] = []
        tr_list: list[float] = []

        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)

            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]

            plus_dm = high_diff if (high_diff > low_diff and high_diff > 0) else 0.0
            minus_dm = low_diff if (low_diff > high_diff and low_diff > 0) else 0.0
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        if len(tr_list) < period:
            return 0.0

        tr_sum = sum(tr_list[:period])
        plus_dm_sum = sum(plus_dm_list[:period])
        minus_dm_sum = sum(minus_dm_list[:period])

        if tr_sum == 0:
            return 0.0

        plus_di = (plus_dm_sum / tr_sum) * 100
        minus_di = (minus_dm_sum / tr_sum) * 100

        if plus_di + minus_di == 0:
            dx = 0.0
        else:
            dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

        adx = dx
        dx_list: list[float] = []
        for i in range(period, len(tr_list)):
            tr_sum = tr_sum - tr_sum / period + tr_list[i]
            plus_dm_sum = plus_dm_sum - plus_dm_sum / period + plus_dm_list[i]
            minus_dm_sum = minus_dm_sum - minus_dm_sum / period + minus_dm_list[i]

            if tr_sum == 0:
                dx_list.append(0.0)
                continue

            plus_di = (plus_dm_sum / tr_sum) * 100
            minus_di = (minus_dm_sum / tr_sum) * 100
            if plus_di + minus_di == 0:
                dx_list.append(0.0)
            else:
                dx_list.append(100.0 * (abs(plus_di - minus_di) / (plus_di + minus_di)))

        for d in dx_list:
            adx = (adx * (period - 1) + d) / period

        return adx

    def _calculate_atr_percentile(self, bars: list[Bar]) -> float:
        lookback = self.config.atr_lookback
        if len(bars) < lookback + 1:
            return 50.0

        atr_values: list[float] = []
        for i in range(1, len(bars)):
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            atr_values.append(tr)

        if len(atr_values) < lookback:
            return 50.0

        window = atr_values[-lookback:]
        current = window[-1]
        rank = sum(1 for v in window if v < current)
        tied = sum(1 for v in window if v == current)
        rank += tied // 2
        return (rank / len(window)) * 100.0
