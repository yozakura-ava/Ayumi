from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VolatilityRegime(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"


class TrendDirection(Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    NEUTRAL = "neutral"


class SessionName(Enum):
    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "new_york"
    CLOSE = "close"


_SESSION_WINDOWS: dict[SessionName, dict] = {
    SessionName.ASIA: {
        "hours": (0, 7),
        "vol_multiplier": 0.7,
    },
    SessionName.LONDON: {
        "hours": (7, 12),
        "vol_multiplier": 1.0,
    },
    SessionName.NEW_YORK: {
        "hours": (12, 17),
        "vol_multiplier": 1.1,
    },
    SessionName.CLOSE: {
        "hours": (17, 24),
        "vol_multiplier": 0.5,
    },
}


@dataclass(frozen=True)
class VolatilityRegimeResult:
    regime: VolatilityRegime
    percentile: float
    atr_value: float


@dataclass(frozen=True)
class TrendRegimeResult:
    adx_value: float
    direction: TrendDirection
    ma_slope: float


@dataclass(frozen=True)
class SessionRegimeResult:
    session: SessionName
    vol_multiplier: float


@dataclass(frozen=True)
class CombinedRegime:
    volatility: VolatilityRegimeResult
    trend: TrendRegimeResult
    session: SessionRegimeResult
    confidence: float


def volatility_regime(
    atr_series: list[float],
    lookback: int = 50,
) -> VolatilityRegimeResult:
    if not atr_series:
        return VolatilityRegimeResult(
            regime=VolatilityRegime.NORMAL,
            percentile=50.0,
            atr_value=0.0,
        )

    window = atr_series[-lookback:]
    current_atr = atr_series[-1]

    sorted_window = sorted(window)
    rank = sum(1 for v in sorted_window if v < current_atr)
    tied = sum(1 for v in sorted_window if v == current_atr)
    rank += tied // 2
    percentile = (rank / len(sorted_window)) * 100.0

    if percentile < 25:
        regime = VolatilityRegime.LOW
    elif percentile < 75:
        regime = VolatilityRegime.NORMAL
    elif percentile < 90:
        regime = VolatilityRegime.HIGH
    else:
        regime = VolatilityRegime.EXTREME

    return VolatilityRegimeResult(
        regime=regime,
        percentile=percentile,
        atr_value=current_atr,
    )


def trend_regime(
    high: list[float],
    low: list[float],
    close: list[float],
    adx_period: int = 14,
) -> TrendRegimeResult:
    if len(high) < adx_period + 1 or len(low) < adx_period + 1 or len(close) < adx_period + 1:
        return TrendRegimeResult(
            adx_value=0.0,
            direction=TrendDirection.NEUTRAL,
            ma_slope=0.0,
        )

    n = len(close)

    true_ranges: list[float] = []
    for i in range(1, n):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        true_ranges.append(tr)

    plus_dms: list[float] = []
    minus_dms: list[float] = []
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    smoothed_tr = sum(true_ranges[:adx_period])
    smoothed_plus_dm = sum(plus_dms[:adx_period])
    smoothed_minus_dm = sum(minus_dms[:adx_period])

    for i in range(adx_period, len(true_ranges)):
        smoothed_tr = smoothed_tr - (smoothed_tr / adx_period) + true_ranges[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / adx_period) + plus_dms[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / adx_period) + minus_dms[i]

    if smoothed_tr == 0:
        plus_di = 0.0
        minus_di = 0.0
    else:
        plus_di = 100.0 * (smoothed_plus_dm / smoothed_tr)
        minus_di = 100.0 * (smoothed_minus_dm / smoothed_tr)

    di_sum = plus_di + minus_di
    if di_sum == 0:
        adx_value = 0.0
    else:
        adx_value = 100.0 * (abs(plus_di - minus_di) / di_sum)

    if adx_value > 25:
        direction = TrendDirection.TRENDING
    elif adx_value < 20:
        direction = TrendDirection.RANGING
    else:
        direction = TrendDirection.NEUTRAL

    if len(close) < 2:
        ma_slope = 0.0
    else:
        recent = close[-adx_period:] if len(close) >= adx_period else close
        ma_slope = (recent[-1] - recent[0]) / len(recent)

    return TrendRegimeResult(
        adx_value=adx_value,
        direction=direction,
        ma_slope=ma_slope,
    )


def session_regime(
    hour: int,
    day_of_week: int,
) -> SessionRegimeResult:
    if hour >= 24:
        hour = 23

    session = SessionName.CLOSE
    for s, cfg in _SESSION_WINDOWS.items():
        start, end = cfg["hours"]
        if start <= hour < end:
            session = s
            break

    vol_multiplier = _SESSION_WINDOWS[session]["vol_multiplier"]

    if day_of_week >= 5:
        vol_multiplier *= 0.5

    return SessionRegimeResult(
        session=session,
        vol_multiplier=vol_multiplier,
    )


def combined_regime(
    vol_regime: VolatilityRegimeResult,
    trend_regime: TrendRegimeResult,
    session_regime: SessionRegimeResult,
) -> CombinedRegime:
    vol_weight = 0.4
    trend_weight = 0.4
    session_weight = 0.2

    vol_score = {
        VolatilityRegime.LOW: 0.5,
        VolatilityRegime.NORMAL: 1.0,
        VolatilityRegime.HIGH: 0.7,
        VolatilityRegime.EXTREME: 0.3,
    }[vol_regime.regime]

    trend_score = {
        TrendDirection.TRENDING: 1.0,
        TrendDirection.RANGING: 0.5,
        TrendDirection.NEUTRAL: 0.7,
    }[trend_regime.direction]

    session_score = min(session_regime.vol_multiplier, 1.5) / 1.5

    confidence = vol_weight * vol_score + trend_weight * trend_score + session_weight * session_score
    confidence = max(0.0, min(1.0, confidence))

    return CombinedRegime(
        volatility=vol_regime,
        trend=trend_regime,
        session=session_regime,
        confidence=confidence,
    )
