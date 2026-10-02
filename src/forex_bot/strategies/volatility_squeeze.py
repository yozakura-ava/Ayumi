from __future__ import annotations

from dataclasses import dataclass
from typing import List

from core.types import (
    Bar,
    MarketState,
    SessionType,
    StrategySignal,
    TradeDirection,
)

_PREFERRED_SESSIONS: set[SessionType] = {
    SessionType.LONDON,
    SessionType.NY_AM,
}


def _calculate_rsi(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(bars)):
        change = bars[i].close - bars[i - 1].close
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass(frozen=True)
class VolatilitySqueezeConfig:
    bb_period: int = 20
    bb_std_dev: float = 1.8
    kc_period: int = 20
    kc_atr_multiplier: float = 1.8
    squeeze_threshold: float = 0.0
    min_squeeze_bars: int = 2
    ema_period: int = 20
    adx_period: int = 14
    rsi_period: int = 14
    adx_min: float = 15.0
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    tp1_rr: float = 1.0
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0
    session_filter: bool = True
    min_confidence: float = 0.40
    squeeze_release_mode: str = "any_release"


GBPJPY_H1_PRESET = VolatilitySqueezeConfig(
    bb_period=20,
    bb_std_dev=2.0,
    kc_period=20,
    kc_atr_multiplier=2.0,
    squeeze_threshold=0.0,
    min_squeeze_bars=3,
    ema_period=20,
    adx_period=14,
    adx_min=20,
    atr_period=14,
    atr_sl_multiplier=1.5,
    tp1_rr=1.0,
    tp2_rr=2.0,
    tp3_rr=3.0,
    session_filter=True,
    squeeze_release_mode="moderate",
)

EURUSD_H1_PRESET = VolatilitySqueezeConfig(
    bb_period=20,
    bb_std_dev=2.0,
    kc_period=20,
    kc_atr_multiplier=2.0,
    squeeze_threshold=0.0,
    min_squeeze_bars=2,
    ema_period=20,
    adx_period=14,
    adx_min=18,
    atr_period=14,
    atr_sl_multiplier=1.5,
    tp1_rr=1.0,
    tp2_rr=2.0,
    tp3_rr=3.0,
    session_filter=True,
    squeeze_release_mode="moderate",
)

XAUUSD_H1_PRESET = VolatilitySqueezeConfig(
    bb_period=20,
    bb_std_dev=2.5,
    kc_period=20,
    kc_atr_multiplier=2.5,
    squeeze_threshold=0.0,
    min_squeeze_bars=2,
    ema_period=20,
    adx_period=14,
    adx_min=20,
    atr_period=14,
    atr_sl_multiplier=2.0,
    tp1_rr=1.0,
    tp2_rr=2.0,
    tp3_rr=3.0,
    session_filter=False,
)

USDJPY_H1_PRESET = VolatilitySqueezeConfig(
    bb_period=20,
    bb_std_dev=2.0,
    kc_period=20,
    kc_atr_multiplier=1.5,
    squeeze_threshold=0.0,
    min_squeeze_bars=3,
    ema_period=50,
    adx_period=14,
    adx_min=22,
    atr_period=14,
    atr_sl_multiplier=1.5,
    tp1_rr=1.0,
    tp2_rr=2.0,
    tp3_rr=3.0,
    session_filter=True,
)


def _calculate_sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _calculate_ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _calculate_std(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    subset = values[-period:]
    mean = sum(subset) / period
    variance = sum((v - mean) ** 2 for v in subset) / period
    return variance**0.5


def _calculate_atr(bars: list[Bar], period: int = 14) -> float:
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


def _calculate_adx(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period * 2 + 1:
        return 0.0
    n = len(bars)
    true_ranges: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    for i in range(1, n):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        true_ranges.append(tr)
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    if len(true_ranges) < period:
        return 0.0

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus_dm = sum(plus_dms[:period])
    smoothed_minus_dm = sum(minus_dms[:period])

    dx_list: list[float] = []
    for i in range(period, len(true_ranges)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + true_ranges[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dms[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dms[i]

        if smoothed_tr == 0:
            dx_list.append(0.0)
            continue
        plus_di = 100.0 * (smoothed_plus_dm / smoothed_tr)
        minus_di = 100.0 * (smoothed_minus_dm / smoothed_tr)
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0.0)
        else:
            dx_list.append(100.0 * (abs(plus_di - minus_di) / di_sum))

    if len(dx_list) < period:
        return 0.0

    adx = sum(dx_list[:period]) / period
    for i in range(period, len(dx_list)):
        adx = (adx * (period - 1) + dx_list[i]) / period

    return adx


def _calculate_bollinger_bands(bars: list[Bar], period: int, std_dev: float) -> tuple[float, float, float]:
    closes = [b.close for b in bars]
    sma = _calculate_sma(closes, period)
    std = _calculate_std(closes, period)
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    return upper, sma, lower


def _calculate_keltner_channels(bars: list[Bar], period: int, atr_multiplier: float) -> tuple[float, float, float]:
    closes = [b.close for b in bars]
    ema = _calculate_ema(closes, period)
    atr = _calculate_atr(bars, period)
    upper = ema + atr * atr_multiplier
    lower = ema - atr * atr_multiplier
    return upper, ema, lower


def _detect_squeeze_duration(
    bars: list[Bar],
    bb_period: int,
    bb_std_dev: float,
    kc_period: int,
    kc_atr_multiplier: float,
) -> int:
    if len(bars) < max(bb_period, kc_period) + 2:
        return 0

    count = 0
    for i in range(len(bars)):
        slice_bars = bars[: i + 1]
        if len(slice_bars) < max(bb_period, kc_period) + 1:
            continue
        bb_upper, _, bb_lower = _calculate_bollinger_bands(slice_bars, bb_period, bb_std_dev)
        kc_upper, _, kc_lower = _calculate_keltner_channels(slice_bars, kc_period, kc_atr_multiplier)
        if bb_upper <= kc_upper and bb_lower >= kc_lower:
            count += 1
        else:
            count = 0
    return count


def _passes_session_filter(state: MarketState) -> bool:
    if state.current_session is None:
        return True
    return state.current_session in _PREFERRED_SESSIONS


def _build_signal(
    direction: TradeDirection,
    entry: float,
    atr: float,
    config: VolatilitySqueezeConfig,
    confidence: float,
    rationale: str,
) -> StrategySignal | None:
    if atr <= 0:
        return None

    risk = atr * config.atr_sl_multiplier
    if direction == TradeDirection.LONG:
        sl = entry - risk
        tp1 = entry + risk * config.tp1_rr
        tp2 = entry + risk * config.tp2_rr
        tp3 = entry + risk * config.tp3_rr
    else:
        sl = entry + risk
        tp1 = entry - risk * config.tp1_rr
        tp2 = entry - risk * config.tp2_rr
        tp3 = entry - risk * config.tp3_rr

    if confidence < config.min_confidence:
        return None

    return StrategySignal(
        direction=direction,
        confidence=min(confidence, 0.95),
        entry_price=entry,
        stop_loss=sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp3,
        rationale=rationale,
    )


class VolatilitySqueezeStrategy:
    def __init__(self, config: VolatilitySqueezeConfig | None = None):
        self.config = config or VolatilitySqueezeConfig()
        self._squeeze_bar_count: int = 0
        self._was_in_squeeze: bool = False

    @property
    def name(self) -> str:
        return "Volatility Squeeze Breakout"

    def reset(self) -> None:
        self._squeeze_bar_count = 0
        self._was_in_squeeze = False

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = (
            max(self.config.bb_period, self.config.kc_period, self.config.ema_period) + self.config.adx_period + 5
        )

        if len(state.bars) < min_required:
            return None

        if self.config.session_filter and not _passes_session_filter(state):
            return None

        bars = state.bars
        bb_upper, bb_middle, bb_lower = _calculate_bollinger_bands(bars, self.config.bb_period, self.config.bb_std_dev)
        kc_upper, kc_middle, kc_lower = _calculate_keltner_channels(
            bars, self.config.kc_period, self.config.kc_atr_multiplier
        )

        in_squeeze = bb_upper <= kc_upper and bb_lower >= kc_lower
        squeeze_just_released = self._was_in_squeeze and not in_squeeze

        if in_squeeze:
            self._squeeze_bar_count += 1
        else:
            if self._squeeze_bar_count >= self.config.min_squeeze_bars:
                squeeze_just_released = True
            self._squeeze_bar_count = 0

        self._was_in_squeeze = in_squeeze

        latest = state.latest_bar
        closes = [b.close for b in bars]
        ema = _calculate_ema(closes, self.config.ema_period)
        if ema == 0:
            return None

        atr = state.atr if state.atr > 0 else _calculate_atr(bars, self.config.atr_period)

        adx = _calculate_adx(bars, self.config.adx_period)

        direction = None
        signal_type = None
        squeeze_duration = self._squeeze_bar_count

        # FIX (card 453dac89): Original code required `adx >= 20` while IN a
        # squeeze, but ADX during a squeeze is structurally < 20 by definition —
        # that's what makes it a squeeze. The strategy therefore never traded.
        #
        # The TTM Squeeze hypothesis fires on the *first* bar where BBs are
        # outside KCs (John Carter, "Mastering the Trade", 2005). The release
        # of the squeeze IS the signal — ADX is dropped because it is impossible
        # to satisfy during the regime we are trying to detect.
        #
        # The original secondary "breakout" branch (`in_squeeze and
        # latest.close > kc_upper`) was also dead code: `in_squeeze` requires
        # `bb_upper <= kc_upper`, which is impossible when the latest close
        # exceeds kc_upper (bb_upper rides the close). Removed.
        if squeeze_just_released:
            # FIX (card 269887b8): Previous code required `close > kc_upper
            # AND close > ema` for long (and `close < kc_lower AND
            # close < ema` for short). On a real-world squeeze release the
            # close typically clears the EMA yet stays *inside* the
            # keltner channel — close rarely exceeds kc_upper on the
            # release bar. Result: zero trades across all 9 SRF cells
            # (EURUSD/GBPUSD/XAUUSD × M5/M15/H1) even after the
            # ADX-gate removal (card 453dac89).
            #
            # TTM Squeeze (John Carter, "Mastering the Trade", 2005)
            # fires on release + momentum direction. Direction comes
            # from close vs ema, not from close vs kc_upper.
            if latest.close > ema:
                direction = TradeDirection.LONG
                signal_type = "release"
            elif latest.close < ema:
                direction = TradeDirection.SHORT
                signal_type = "release"

        if direction is None:
            return None

        rsi = _calculate_rsi(bars, self.config.rsi_period)
        if direction == TradeDirection.LONG and rsi >= 70:
            return None
        if direction == TradeDirection.SHORT and rsi <= 30:
            return None

        confidence = 0.60
        extra_bars = max(0, squeeze_duration - self.config.min_squeeze_bars)
        confidence += min(extra_bars * 0.05, 0.15)
        if adx > 30:
            confidence += 0.10

        if direction == TradeDirection.LONG:
            rationale = (
                f"Squeeze {signal_type} long: close={latest.close:.5f} > "
                f"EMA={ema:.5f}, "
                f"squeeze_bars={squeeze_duration}, ADX={adx:.1f}"
            )
        else:
            rationale = (
                f"Squeeze {signal_type} short: close={latest.close:.5f} < "
                f"EMA={ema:.5f}, "
                f"squeeze_bars={squeeze_duration}, ADX={adx:.1f}"
            )

        entry = latest.close
        return _build_signal(direction, entry, atr, self.config, confidence, rationale)
