from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backtest.engine import Bar


class VolatilityRegime(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


class TrendDirection(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class MarketRegime(Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


@dataclass(frozen=True)
class TimeframeRegime:
    volatility: VolatilityRegime
    volatility_percentile: float
    trend: TrendDirection
    adx: float
    regime: MarketRegime
    atr: float


@dataclass(frozen=True)
class MultiTimeframeRegime:
    h4: TimeframeRegime
    h1: TimeframeRegime
    m15: TimeframeRegime
    confluence_score: float
    aligned_direction: TrendDirection


@dataclass(frozen=True)
class MTFRegimeConfig:
    min_confluence: float = 0.6
    require_h4_alignment: bool = True
    allowed_regimes: frozenset[MarketRegime] = frozenset({MarketRegime.TRENDING, MarketRegime.RANGING})
    atr_period: int = 14
    atr_lookback: int = 50
    adx_period: int = 14
    adx_trending_threshold: float = 25.0
    adx_ranging_threshold: float = 20.0
    vol_percentile_low: float = 25.0
    vol_percentile_normal: float = 75.0
    vol_percentile_high: float = 90.0


def compute_atr(high: list[float], low: list[float], close: list[float], period: int = 14) -> float:
    if len(high) < period + 1 or len(low) < period + 1 or len(close) < period + 1:
        return 0.0001
    tr_sum = 0.0
    for i in range(len(high) - period, len(high)):
        if i > 0:
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
            tr_sum += tr
    return tr_sum / period


def compute_adx(
    high: list[float], low: list[float], close: list[float], period: int = 14
) -> tuple[float, float, float]:
    if len(high) < period + 1 or len(low) < period + 1 or len(close) < period + 1:
        return 0.0, 0.0, 0.0

    n = len(close)
    true_ranges: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []

    for i in range(1, n):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        true_ranges.append(tr)
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    if len(true_ranges) < period:
        return 0.0, 0.0, 0.0

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus_dm = sum(plus_dms[:period])
    smoothed_minus_dm = sum(minus_dms[:period])

    for i in range(period, len(true_ranges)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + true_ranges[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dms[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dms[i]

    if smoothed_tr == 0:
        return 0.0, 0.0, 0.0

    plus_di = 100.0 * (smoothed_plus_dm / smoothed_tr)
    minus_di = 100.0 * (smoothed_minus_dm / smoothed_tr)
    di_sum = plus_di + minus_di

    if di_sum == 0:
        adx_value = 0.0
    else:
        adx_value = 100.0 * (abs(plus_di - minus_di) / di_sum)

    return adx_value, plus_di, minus_di


def volatility_regime_atr(
    atr_series: list[float],
    lookback: int = 50,
    thresholds: MTFRegimeConfig | None = None,
) -> tuple[VolatilityRegime, float, float]:
    if thresholds is None:
        thresholds = MTFRegimeConfig()

    if not atr_series:
        return VolatilityRegime.NORMAL, 50.0, 0.0

    window = atr_series[-lookback:]
    current_atr = atr_series[-1]

    sorted_window = sorted(window)
    rank = sum(1 for v in sorted_window if v < current_atr)
    tied = sum(1 for v in sorted_window if v == current_atr)
    rank += tied // 2
    percentile = (rank / len(sorted_window)) * 100.0

    if percentile < thresholds.vol_percentile_low:
        regime = VolatilityRegime.LOW
    elif percentile < thresholds.vol_percentile_normal:
        regime = VolatilityRegime.NORMAL
    elif percentile < thresholds.vol_percentile_high:
        regime = VolatilityRegime.HIGH
    else:
        regime = VolatilityRegime.EXTREME

    return regime, percentile, current_atr


def detect_regime(
    bars: list[Bar],
    config: MTFRegimeConfig | None = None,
) -> TimeframeRegime:
    if config is None:
        config = MTFRegimeConfig()

    if len(bars) < config.atr_period + 1:
        return TimeframeRegime(
            volatility=VolatilityRegime.NORMAL,
            volatility_percentile=50.0,
            trend=TrendDirection.NEUTRAL,
            adx=0.0,
            regime=MarketRegime.RANGING,
            atr=0.0,
        )

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    atr = compute_atr(highs, lows, closes, config.atr_period)
    atr_series = []
    for i in range(config.atr_period, len(bars)):
        window_highs = highs[i - config.atr_period : i]
        window_lows = lows[i - config.atr_period : i]
        window_closes = closes[i - config.atr_period : i]
        window_atr = compute_atr(window_highs, window_lows, window_closes, config.atr_period)
        atr_series.append(window_atr)

    vol_regime, vol_pct, _ = volatility_regime_atr(atr_series, config.atr_lookback, config)
    adx, plus_di, minus_di = compute_adx(highs, lows, closes, config.adx_period)

    if adx > config.adx_trending_threshold:
        trend = MarketRegime.TRENDING
    elif adx < config.adx_ranging_threshold:
        trend = MarketRegime.RANGING
    else:
        trend = MarketRegime.RANGING

    if plus_di > minus_di:
        direction = TrendDirection.BULLISH
    elif minus_di > plus_di:
        direction = TrendDirection.BEARISH
    else:
        direction = TrendDirection.NEUTRAL

    if vol_regime == VolatilityRegime.EXTREME:
        market_regime = MarketRegime.VOLATILE
    elif trend == MarketRegime.TRENDING:
        market_regime = MarketRegime.TRENDING
    else:
        market_regime = MarketRegime.RANGING

    return TimeframeRegime(
        volatility=vol_regime,
        volatility_percentile=vol_pct,
        trend=direction,
        adx=adx,
        regime=market_regime,
        atr=atr,
    )


def compute_confluence(regimes: list[TimeframeRegime]) -> tuple[float, TrendDirection]:
    if not regimes:
        return 0.0, TrendDirection.NEUTRAL

    directions = [r.trend for r in regimes]
    bullish_count = sum(1 for d in directions if d == TrendDirection.BULLISH)
    bearish_count = sum(1 for d in directions if d == TrendDirection.BEARISH)

    if bullish_count == len(regimes):
        aligned = TrendDirection.BULLISH
        confluence = 1.0
    elif bearish_count == len(regimes):
        aligned = TrendDirection.BEARISH
        confluence = 1.0
    elif bullish_count >= 2 and bearish_count <= 1:
        aligned = TrendDirection.BULLISH
        confluence = 0.7
    elif bearish_count >= 2 and bullish_count <= 1:
        aligned = TrendDirection.BEARISH
        confluence = 0.7
    elif bullish_count > bearish_count:
        aligned = TrendDirection.BULLISH
        confluence = 0.5
    elif bearish_count > bullish_count:
        aligned = TrendDirection.BEARISH
        confluence = 0.5
    else:
        aligned = TrendDirection.NEUTRAL
        confluence = 0.3

    return confluence, aligned


def detect_multi_timeframe_regime(
    h4_bars: list[Bar],
    h1_bars: list[Bar],
    m15_bars: list[Bar],
    config: MTFRegimeConfig | None = None,
) -> MultiTimeframeRegime:
    if config is None:
        config = MTFRegimeConfig()

    h4_regime = detect_regime(h4_bars, config)
    h1_regime = detect_regime(h1_bars, config)
    m15_regime = detect_regime(m15_bars, config)

    confluence, aligned = compute_confluence([h4_regime, h1_regime, m15_regime])

    return MultiTimeframeRegime(
        h4=h4_regime,
        h1=h1_regime,
        m15=m15_regime,
        confluence_score=confluence,
        aligned_direction=aligned,
    )


class MTFRegimeFilter:
    def __init__(self, config: MTFRegimeConfig | None = None):
        self.config = config or MTFRegimeConfig()

    def evaluate(
        self,
        state,  # MarketState
        h4_bars: list[Bar],
        h1_bars: list[Bar],
        m15_bars: list[Bar],
    ) -> bool:
        mtf_regime = detect_multi_timeframe_regime(h4_bars, h1_bars, m15_bars, self.config)

        if mtf_regime.confluence_score < self.config.min_confluence:
            return False

        if self.config.require_h4_alignment:
            if mtf_regime.h4.trend == TrendDirection.NEUTRAL:
                return False

        if mtf_regime.h4.regime not in self.config.allowed_regimes:
            return False

        return True

    def get_confidence(
        self,
        state,  # MarketState
        h4_bars: list[Bar],
        h1_bars: list[Bar],
        m15_bars: list[Bar],
    ) -> float:
        mtf_regime = detect_multi_timeframe_regime(h4_bars, h1_bars, m15_bars, self.config)
        return mtf_regime.confluence_score

    def get_regime(self, h4_bars: list[Bar], h1_bars: list[Bar], m15_bars: list[Bar]) -> MultiTimeframeRegime:
        return detect_multi_timeframe_regime(h4_bars, h1_bars, m15_bars, self.config)
