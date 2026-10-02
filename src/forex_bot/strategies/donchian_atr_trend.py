"""Donchian + ATR Trailing Trend Strategy.

Trend-following with adaptive ATR stop. Buys breakouts of N-period
Donchian high, sells breakouts of N-period low, trails stop by ATR.

Best on: XAUUSD (strong trends), GBPUSD (volatile swings)
Timeframes: M15, H1
"""

from __future__ import annotations

from dataclasses import dataclass

from core.types import (
    Bar,
    MarketState,
    StrategySignal,
    TradeDirection,
)


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


def _calculate_ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _calculate_adx(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period * 2 + 1:
        return 0.0
    true_ranges: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    for i in range(len(bars) - period * 2, len(bars)):
        if i > 0:
            up = bars[i].high - bars[i - 1].high
            down = bars[i - 1].low - bars[i].low
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            true_ranges.append(tr)
            pdm = up if up > down and up > 0 else 0.0
            mdm = down if down > up and down > 0 else 0.0
            plus_dms.append(pdm)
            minus_dms.append(mdm)
    if not true_ranges:
        return 0.0
    atr = sum(true_ranges[:period]) / period
    plus_di = 100.0 * (sum(plus_dms[:period]) / period) / atr if atr > 0 else 0
    minus_di = 100.0 * (sum(minus_dms[:period]) / period) / atr if atr > 0 else 0
    dx = 100.0 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
    # Smooth: simple average of DX values (simplified ADX)
    dx_values = []
    for j in range(0, len(true_ranges) - period, 1):
        seg_tr = true_ranges[j : j + period]
        seg_pdm = plus_dms[j : j + period]
        seg_mdm = minus_dms[j : j + period]
        if len(seg_tr) < period:
            break
        seg_atr = sum(seg_tr) / period
        seg_pdi = 100.0 * (sum(seg_pdm) / period) / seg_atr if seg_atr > 0 else 0
        seg_mdi = 100.0 * (sum(seg_mdm) / period) / seg_atr if seg_atr > 0 else 0
        seg_dx = 100.0 * abs(seg_pdi - seg_mdi) / (seg_pdi + seg_mdi) if (seg_pdi + seg_mdi) > 0 else 0
        dx_values.append(seg_dx)
    return sum(dx_values) / len(dx_values) if dx_values else dx


def _donchian_high(bars: list[Bar], period: int) -> float:
    """N-period Donchian channel high (excluding current bar)."""
    if len(bars) < period + 1:
        return max(b.high for b in bars) if bars else 0.0
    return max(b.high for b in bars[-(period + 1) : -1])


def _donchian_low(bars: list[Bar], period: int) -> float:
    """N-period Donchian channel low (excluding current bar)."""
    if len(bars) < period + 1:
        return min(b.low for b in bars) if bars else 0.0
    return min(b.low for b in bars[-(period + 1) : -1])


@dataclass(frozen=True)
class DonchianATRConfig:
    donchian_period: int = 20
    atr_period: int = 14
    atr_trail_multiplier: float = 2.5
    adx_threshold: float = 20.0
    ema_trend_period: int = 50
    min_confidence: float = 0.45
    cooldown_bars: int = 5
    pip_value: float = 0.0001  # forex default


class DonchianATRTrendStrategy:
    """Trend-following: Donchian breakout + ATR trailing stop."""

    def __init__(self, config: DonchianATRConfig | None = None):
        self.config = config or DonchianATRConfig()
        self._bars_since_signal: int = 999

    @property
    def name(self) -> str:
        return "Donchian ATR Trend"

    def reset(self) -> None:
        self._bars_since_signal = 999

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        bars = state.bars
        min_required = max(
            self.config.donchian_period + 1,
            self.config.ema_trend_period,
            self.config.atr_period * 2 + 1,
        )

        if len(bars) < min_required:
            return None

        # Cooldown
        if self._bars_since_signal < self.config.cooldown_bars:
            self._bars_since_signal += 1
            return None
        self._bars_since_signal += 1

        latest = bars[-1]
        dc_high = _donchian_high(bars, self.config.donchian_period)
        dc_low = _donchian_low(bars, self.config.donchian_period)
        atr = _calculate_atr(bars, self.config.atr_period)
        adx = _calculate_adx(bars, self.config.atr_period)
        closes = [b.close for b in bars]
        ema = _calculate_ema(closes, self.config.ema_trend_period)

        if atr <= 0 or ema <= 0:
            return None

        # Trend filter: ADX must be above threshold
        if adx < self.config.adx_threshold:
            return None

        direction = None
        signal_type = None

        # Long: close breaks above Donchian high AND above EMA
        if latest.close > dc_high and latest.close > ema:
            direction = TradeDirection.LONG
            signal_type = "breakout_long"
        # Short: close breaks below Donchian low AND below EMA
        elif latest.close < dc_low and latest.close < ema:
            direction = TradeDirection.SHORT
            signal_type = "breakout_short"

        if direction is None:
            return None

        # ATR-based stop loss
        if direction == TradeDirection.LONG:
            stop_loss = max(dc_low, latest.close - self.config.atr_trail_multiplier * atr)
        else:
            stop_loss = min(dc_high, latest.close + self.config.atr_trail_multiplier * atr)

        # Take profits at R multiples
        risk = abs(latest.close - stop_loss)
        if risk <= 0:
            return None

        entry = latest.close
        tp1 = entry + risk * 1.0 if direction == TradeDirection.LONG else entry - risk * 1.0
        tp2 = entry + risk * 2.0 if direction == TradeDirection.LONG else entry - risk * 2.0
        tp3 = entry + risk * 3.0 if direction == TradeDirection.LONG else entry - risk * 3.0

        # Confidence based on ADX strength and breakout distance
        breakout_dist = abs(latest.close - dc_high) if direction == TradeDirection.LONG else abs(latest.close - dc_low)
        confidence = min(
            self.config.min_confidence + (adx - self.config.adx_threshold) * 0.005 + breakout_dist / atr * 0.05,
            0.85,
        )

        rationale = (
            f"Donchian {signal_type}: close={latest.close:.5f} > DC_high={dc_high:.5f}, "
            f"ADX={adx:.1f}, ATR={atr:.5f}, EMA={ema:.5f}"
        )

        self._bars_since_signal = 0

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )
