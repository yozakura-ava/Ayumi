from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

from config.sessions import (
    DSTAwareKillzoneHours,
    KillzoneHours,
    get_killzone_hours_for_date,
)
from core.types import (
    Bar,
    MarketState,
    SessionType,
    StrategySignal,
    TradeDirection,
)
from utils.pip_value import pip_value_for_symbol


@dataclass(frozen=True)
class KillzoneMomentumConfig:
    # Symbol for pip-size resolution. Defaults to XAUUSD since this
    # strategy is primarily registered for gold (see strategies/registry.py).
    symbol: str = "XAUUSD"
    # Tuned per research §A.4 (strategy-optimization-research.md)
    atr_period: int = 14
    atr_breakout_multiplier: float = 0.3  # was 0.5 — catch cleaner (smaller) breakouts
    ema_trend_period: int = 50
    rsi_period: int = 14
    min_session_range_pips: float = 8.0  # was 12.0 — allow quieter sessions
    hard_cap_sl_pips: float = 35.0
    atr_sl_multiplier: float = 1.5
    retest_tolerance_atr: float = 1.0  # was 0.5 — allow retest up to 1×ATR from breakout
    tp1_rr: float = 1.0
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0
    adx_period: int = 14
    adx_threshold: float = 15.0  # was 20.0 — 20 too restrictive on M5; 15 still requires mild trend
    min_bars_for_setup: int = 40  # was 80 — cut setup time without sacrificing indicator stability
    breakout_lookback_bars: int = 12  # was 6 — more bars for breakout to develop on H1

    # Per-pair/per-timeframe presets per research §A.4.
    # The default values above target FX H1. For M5 XAUUSD, use
    # ``KillzoneMomentumConfig.m5_xauusd()`` which raises session-range
    # threshold to 25 pips (gold M5 has larger sessions) and shortens
    # breakout lookback to 8 bars (M5 resolves breakouts faster).
    @classmethod
    def h1_fx(cls) -> "KillzoneMomentumConfig":
        """Default preset — FX H1 (EURUSD, GBPUSD)."""
        return cls()

    @classmethod
    def m5_xauusd(cls) -> "KillzoneMomentumConfig":
        """M5 XAUUSD preset per research §A.4.

        Differences from H1 FX defaults:
        - ``min_session_range_pips`` 8.0 → 25.0 (gold M5 sessions are wider)
        - ``breakout_lookback_bars`` 12 → 8 (M5 resolves breakouts faster)
        - ``adx_threshold`` 15.0 → 20.0 (gold M5 noise is higher; stricter gate)
        - ``min_bars_for_setup`` stays at 40 (still appropriate for M5)
        """
        return cls(
            min_session_range_pips=25.0,
            breakout_lookback_bars=8,
            adx_threshold=20.0,
        )


# Static fallback hours (used when date context is unavailable)
_LONDON_OPEN_START = KillzoneHours.LONDON_OPEN_START
_LONDON_OPEN_END = KillzoneHours.LONDON_OPEN_END
_NY_OPEN_START = KillzoneHours.NY_OPEN_START
_NY_OPEN_END = KillzoneHours.NY_OPEN_END
_OVERLAP_START = KillzoneHours.OVERLAP_START
_OVERLAP_END = KillzoneHours.OVERLAP_END

_ASIAN_START_HOUR = 0
_ASIAN_END_HOUR = 7
_LONDON_START_HOUR = 7
_LONDON_END_HOUR = 12

_MIN_SL_PIPS = 5.0


def _resolve_kz_hours(d: date | None = None) -> DSTAwareKillzoneHours:
    """Return DST-aware killzone hours for the given date (or static fallback)."""
    if d is not None:
        return get_killzone_hours_for_date(d)
    # Fallback: return the static defaults as a DSTAwareKillzoneHours
    return DSTAwareKillzoneHours(
        LONDON_OPEN_START=_LONDON_OPEN_START,
        LONDON_OPEN_END=_LONDON_OPEN_END,
        NY_OPEN_START=_NY_OPEN_START,
        NY_OPEN_END=_NY_OPEN_END,
        OVERLAP_START=_OVERLAP_START,
        OVERLAP_END=_OVERLAP_END,
    )


def _get_bar_session(bar_time: datetime) -> SessionType:
    utc_hour = bar_time.hour
    if _ASIAN_START_HOUR <= utc_hour < _ASIAN_END_HOUR:
        return SessionType.ASIAN
    if _LONDON_START_HOUR <= utc_hour < _LONDON_END_HOUR:
        return SessionType.LONDON
    if 12 <= utc_hour < 16:
        return SessionType.NY_AM
    if 16 <= utc_hour < 20:
        return SessionType.NY_PM
    return SessionType.OUTSIDE


def _is_killzone(state: MarketState) -> bool:
    """Check if the bar is within any killzone using STATIC hours.

    This function preserves backward compatibility with existing test expectations.
    For DST-aware killzone detection, use ``_is_killzone_dst()``.
    """
    utc_hour = state.latest_bar.time.hour
    return (
        _LONDON_OPEN_START.hour <= utc_hour < _LONDON_OPEN_END.hour
        or _NY_OPEN_START.hour <= utc_hour < _NY_OPEN_END.hour
        or _OVERLAP_START.hour <= utc_hour < _OVERLAP_END.hour
    )


def _is_killzone_dst(state: MarketState) -> bool:
    """Check if the bar is within any killzone using DST-aware hours."""
    kz = _resolve_kz_hours(state.latest_bar.time.date())
    utc_hour = state.latest_bar.time.hour
    return (
        kz.LONDON_OPEN_START.hour <= utc_hour < kz.LONDON_OPEN_END.hour
        or kz.NY_OPEN_START.hour <= utc_hour < kz.NY_OPEN_END.hour
        or kz.OVERLAP_START.hour <= utc_hour < kz.OVERLAP_END.hour
    )


def _get_killzone_name(state: MarketState) -> Optional[str]:
    """Get killzone name using STATIC hours.

    This function preserves backward compatibility with existing test expectations.
    For DST-aware killzone detection, use ``_get_killzone_name_dst()``.
    """
    utc_hour = state.latest_bar.time.hour
    if _LONDON_OPEN_START.hour <= utc_hour < _LONDON_OPEN_END.hour:
        return "london_open"
    if _NY_OPEN_START.hour <= utc_hour < _NY_OPEN_END.hour:
        return "ny_open"
    if _OVERLAP_START.hour <= utc_hour < _OVERLAP_END.hour:
        return "overlap"
    return None


def _get_killzone_name_dst(state: MarketState) -> Optional[str]:
    """Get killzone name using DST-aware hours."""
    kz = _resolve_kz_hours(state.latest_bar.time.date())
    utc_hour = state.latest_bar.time.hour
    if kz.LONDON_OPEN_START.hour <= utc_hour < kz.LONDON_OPEN_END.hour:
        return "london_open"
    if kz.NY_OPEN_START.hour <= utc_hour < kz.NY_OPEN_END.hour:
        return "ny_open"
    if kz.OVERLAP_START.hour <= utc_hour < kz.OVERLAP_END.hour:
        return "overlap"
    return None


def _calculate_atr(bars: List[Bar], period: int = 14) -> float:
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


def _calculate_ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _calculate_rsi(bars: List[Bar], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(len(bars) - period, len(bars)):
        change = bars[i].close - bars[i - 1].close
        gains.append(change if change > 0 else 0.0)
        losses.append(abs(change) if change < 0 else 0.0)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calculate_adx(bars: List[Bar], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None

    tr_list: List[float] = []
    plus_dm_list: List[float] = []
    minus_dm_list: List[float] = []

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

    if len(tr_list) < period:
        return None

    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])

    if smoothed_tr == 0:
        return 0.0

    plus_di = (smoothed_plus_dm / smoothed_tr) * 100
    minus_di = (smoothed_minus_dm / smoothed_tr) * 100

    if plus_di + minus_di == 0:
        return 0.0

    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100

    adx = dx
    for i in range(period, len(tr_list)):
        if smoothed_tr == 0:
            continue
        smoothed_tr = smoothed_tr - smoothed_tr / period + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / period + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / period + minus_dm_list[i]
        if smoothed_tr == 0:
            continue
        plus_di = (smoothed_plus_dm / smoothed_tr) * 100
        minus_di = (smoothed_minus_dm / smoothed_tr) * 100
        if plus_di + minus_di == 0:
            dx = 0.0
        else:
            dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        adx = (adx * (period - 1) + dx) / period

    return adx


def _calculate_session_range(
    bars: List[Bar], session_type: SessionType, reference_day: date
) -> tuple[float, float, float]:
    session_bars: List[Bar] = []
    for b in bars:
        if b.time.date() != reference_day:
            continue
        if _get_bar_session(b.time) == session_type:
            session_bars.append(b)

    if not session_bars:
        return 0.0, 0.0, 0.0

    high = max(b.high for b in session_bars)
    low = min(b.low for b in session_bars)
    mean = sum(b.close for b in session_bars) / len(session_bars)
    return high, low, mean


def _find_previous_trading_day(bars: List[Bar], current_day: date) -> Optional[date]:
    seen_days: set[date] = set()
    for b in bars:
        d = b.time.date()
        if d < current_day:
            seen_days.add(d)
    if not seen_days:
        return None
    return max(seen_days)


def _get_trend_direction(bars: List[Bar], period: int = 50) -> Optional[str]:
    if len(bars) < period + 1:
        return None
    closes = [b.close for b in bars]
    ema = _calculate_ema(closes, period)
    if ema is None:
        return None
    price = closes[-1]
    if price > ema:
        return "long"
    return "short"


def _detect_prior_breakout(
    bars: List[Bar],
    range_high: float,
    range_low: float,
    lookback: int,
    atr: float,
    breakout_mult: float,
) -> Optional[str]:
    breakout_dist = atr * breakout_mult
    for i in range(max(0, len(bars) - lookback), len(bars) - 1):
        bar = bars[i]
        if bar.high > range_high + breakout_dist:
            return "long"
        if bar.low < range_low - breakout_dist:
            return "short"
    return None


def _is_bullish_rejection_bar(bar: Bar) -> bool:
    candle_range = bar.high - bar.low
    if candle_range <= 0:
        return False
    body = bar.close - bar.open
    if body <= 0:
        return False
    upper_wick = bar.high - bar.close
    lower_wick = bar.open - bar.low
    return body > upper_wick and body > lower_wick


def _is_bearish_rejection_bar(bar: Bar) -> bool:
    candle_range = bar.high - bar.low
    if candle_range <= 0:
        return False
    body = bar.open - bar.close
    if body <= 0:
        return False
    upper_wick = bar.high - bar.open
    lower_wick = bar.close - bar.low
    return body > upper_wick and body > lower_wick


class KillzoneMomentumStrategy:
    def __init__(self, config: Optional[KillzoneMomentumConfig] = None):
        self.config = config or KillzoneMomentumConfig()

    @property
    def name(self) -> str:
        return "Killzone Momentum"

    def evaluate(self, state: MarketState) -> Optional[StrategySignal]:
        min_required = self.config.min_bars_for_setup
        if len(state.bars) < min_required:
            return None

        if not _is_killzone_dst(state):
            return None

        kz_name = _get_killzone_name_dst(state)
        if kz_name is None:
            return None

        latest = state.latest_bar
        pip = pip_value_for_symbol(self.config.symbol)
        current_day = latest.time.date()

        atr = _calculate_atr(state.bars, self.config.atr_period)
        if atr <= 0:
            return None

        prior_session_range_high = 0.0
        prior_session_range_low = 0.0
        session_range_width = 0.0

        if kz_name == "london_open":
            prev_day = _find_previous_trading_day(state.bars, current_day)
            if prev_day is None:
                return None
            high, low, _ = _calculate_session_range(state.bars, SessionType.ASIAN, prev_day)
            if high == 0:
                return None
            prior_session_range_high = high
            prior_session_range_low = low
            session_range_width = (high - low) / pip

        elif kz_name == "ny_open":
            high, low, _ = _calculate_session_range(state.bars, SessionType.LONDON, current_day)
            if high == 0:
                prev_day = _find_previous_trading_day(state.bars, current_day)
                if prev_day is None:
                    return None
                high, low, _ = _calculate_session_range(state.bars, SessionType.LONDON, prev_day)
                if high == 0:
                    return None
            prior_session_range_high = high
            prior_session_range_low = low
            session_range_width = (high - low) / pip

        elif kz_name == "overlap":
            high_london, low_london, _ = _calculate_session_range(state.bars, SessionType.LONDON, current_day)
            if high_london == 0:
                prev_day = _find_previous_trading_day(state.bars, current_day)
                if prev_day is None:
                    return None
                high_london, low_london, _ = _calculate_session_range(state.bars, SessionType.LONDON, prev_day)
            prior_session_range_high = high_london
            prior_session_range_low = low_london
            session_range_width = (high_london - low_london) / pip

        if session_range_width < self.config.min_session_range_pips:
            return None

        price = latest.close

        adx = _calculate_adx(state.bars, self.config.adx_period)
        if adx is not None and adx < self.config.adx_threshold:
            return None

        trend = _get_trend_direction(state.bars, self.config.ema_trend_period)
        if trend is None:
            return None

        breakout_direction = _detect_prior_breakout(
            state.bars,
            prior_session_range_high,
            prior_session_range_low,
            self.config.breakout_lookback_bars,
            atr,
            self.config.atr_breakout_multiplier,
        )

        if breakout_direction is None:
            return None

        if breakout_direction != trend:
            return None

        retest_tolerance = atr * self.config.retest_tolerance_atr

        direction: Optional[TradeDirection] = None
        rationale = ""

        if (
            breakout_direction == "long"
            and trend == "long"
            and prior_session_range_high - retest_tolerance <= price <= prior_session_range_high + retest_tolerance * 2
            and _is_bullish_rejection_bar(latest)
        ):
            direction = TradeDirection.LONG
            rationale = (
                f"KZ {kz_name} retest long: price={price:.5f} retests range high={prior_session_range_high:.5f}, "
                f"ATR={atr:.5f}, ADX={adx:.1f}, range={session_range_width:.1f}p"
            )

        elif (
            breakout_direction == "short"
            and trend == "short"
            and prior_session_range_low - retest_tolerance * 2 <= price <= prior_session_range_low + retest_tolerance
            and _is_bearish_rejection_bar(latest)
        ):
            direction = TradeDirection.SHORT
            rationale = (
                f"KZ {kz_name} retest short: price={price:.5f} retests range low={prior_session_range_low:.5f}, "
                f"ATR={atr:.5f}, ADX={adx:.1f}, range={session_range_width:.1f}p"
            )

        if direction is None:
            return None

        # H4 cross-timeframe filter: when H4 bars are available, require
        # the H4 candle direction to align with the breakout direction.
        # If h4_bars is None or empty (backward-compatible), skip this filter.
        # Use getattr() so a future type-divergence on h4_bars surfaces as a
        # real AttributeError rather than silently killing the strategy via
        # the swallowed-exception path. See card f30917a6-a8e9-465c-8032-
        # 1230e9b858cc — the duplicate MarketState in backtest.types caused
        # exactly that silent-kill failure.
        h4_bars = getattr(state, "h4_bars", None)
        if h4_bars and len(h4_bars) >= 2:
            h4_trend = _get_trend_direction(h4_bars, min(10, len(h4_bars) - 1))
            if h4_trend is not None and h4_trend != breakout_direction:
                return None

        entry = price
        sl_distance = min(
            atr * self.config.atr_sl_multiplier,
            self.config.hard_cap_sl_pips * pip,
        )

        if sl_distance <= 0:
            return None

        # Enforce minimum SL distance (5 pips) to prevent tiny stops
        min_sl = _MIN_SL_PIPS * pip
        if sl_distance < min_sl:
            sl_distance = min_sl

        sl = entry - sl_distance if direction == TradeDirection.LONG else entry + sl_distance
        risk = sl_distance

        tp1 = (
            entry + risk * self.config.tp1_rr if direction == TradeDirection.LONG else entry - risk * self.config.tp1_rr
        )
        tp2 = (
            entry + risk * self.config.tp2_rr if direction == TradeDirection.LONG else entry - risk * self.config.tp2_rr
        )
        tp3 = (
            entry + risk * self.config.tp3_rr if direction == TradeDirection.LONG else entry - risk * self.config.tp3_rr
        )

        confidence = 0.65
        if adx is not None and adx >= 30:
            confidence = 0.75
        if adx is not None and adx >= 40:
            confidence = 0.85

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
