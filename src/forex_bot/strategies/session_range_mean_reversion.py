from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from config.sessions import SessionRangeHours
from core.types import (
    Bar,
    MarketState,
    SessionType,
    StrategySignal,
    TradeDirection,
)

NO_SIGNAL = None


@dataclass(frozen=True)
class SessionRangeMRConfig:
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 2.0
    rsi_period: int = 14
    rsi_long_level: float = 30.0
    rsi_short_level: float = 70.0
    session_range_min_pips: float = 25.0
    entry_near_extreme_pips: float = 15.0
    hard_cap_sl_pips: float = 30.0
    tp1_rr: float = 1.0
    tp2_rr: float = 1.5
    ema_trend_period: int = 50
    use_session_range_sl: bool = True
    session_range_sl_fraction: float = 0.6
    pip_value: float | None = None


_ASIAN_START = SessionRangeHours.ASIAN_START
_ASIAN_END = SessionRangeHours.ASIAN_END
_EARLY_LONDON_END = SessionRangeHours.EARLY_LONDON_END
_LONDON_START = SessionRangeHours.LONDON_START
_LONDON_END = SessionRangeHours.LONDON_END
_NY_OPEN_START = SessionRangeHours.NY_OPEN_START
_NY_OPEN_END = SessionRangeHours.NY_OPEN_END
_LONDON_NY_OVERLAP_START = SessionRangeHours.LONDON_NY_OVERLAP_START
_LONDON_NY_OVERLAP_END = SessionRangeHours.LONDON_NY_OVERLAP_END
_NY_CLOSE_START = SessionRangeHours.NY_CLOSE_START
_NY_CLOSE_END = SessionRangeHours.NY_CLOSE_END

_DEFAULT_PIP = 0.0001
_JPY_PIP = 0.01
_MIN_SL_PIPS = 5.0


def _pip_value_for_price(price: float) -> float:
    if price >= 50:
        return _JPY_PIP
    return _DEFAULT_PIP


def _get_bar_session(bar_time: datetime) -> SessionType:
    utc_hour = bar_time.hour
    if _ASIAN_START.hour <= utc_hour < _ASIAN_END.hour:
        return SessionType.ASIAN
    if _LONDON_START.hour <= utc_hour < _LONDON_END.hour:
        return SessionType.LONDON
    if _NY_OPEN_START.hour <= utc_hour < _NY_OPEN_END.hour:
        return SessionType.NY_AM
    if _LONDON_NY_OVERLAP_START.hour <= utc_hour < _LONDON_NY_OVERLAP_END.hour:
        return SessionType.NY_AM
    if _NY_CLOSE_START.hour <= utc_hour < _NY_CLOSE_END.hour:
        return SessionType.NY_PM
    return SessionType.OUTSIDE


def _is_in_asian_or_early_london(state: MarketState) -> bool:
    utc_hour = state.latest_bar.time.hour
    return _ASIAN_START.hour <= utc_hour <= _ASIAN_END.hour or _LONDON_START.hour <= utc_hour <= _EARLY_LONDON_END.hour


def _is_in_london_ny_overlap(state: MarketState) -> bool:
    utc_hour = state.latest_bar.time.hour
    return _LONDON_NY_OVERLAP_START.hour <= utc_hour < _LONDON_NY_OVERLAP_END.hour


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


def _calculate_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _get_trend_bias(bars: list[Bar], period: int = 50) -> str | None:
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


def _calculate_rsi(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
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


def _calculate_session_range(
    bars: list[Bar], session_type: SessionType, reference_day: date | None = None
) -> tuple[float, float, float]:
    if not bars:
        return 0.0, 0.0, 0.0

    if reference_day is None:
        reference_day = bars[-1].time.date()

    session_bars: list[Bar] = []
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


def _build_signal(
    direction: TradeDirection,
    entry: float,
    atr: float,
    config: SessionRangeMRConfig,
    session_range_price: float,
    rationale: str,
    pip_value: float,
    spread_price: float = 0.0,
) -> StrategySignal | None:
    """Build a StrategySignal with TP anchored to the broker fill price.

    The strategy's ``entry`` is the mid-price (Bar.close). For BUY orders the
    broker fills at ASK = mid + half-spread; for SELL at BID = mid - half-spread.
    TP/SL are computed relative to the fill price so the broker accepts the
    order (TP > entry for BUY, TP < entry for SELL) — see card f2317859 for
    the TRADING_BAD_STOPS incident on XAUUSD where mid-anchored TP landed
    below the ASK fill.

    Args:
        spread_price: full bid/ask spread in price units (e.g. 0.04 for
            4-pip XAUUSD spread with pip_value=0.01). Default 0.0 keeps
            legacy behavior (TP/SL relative to mid) for callers that
            don't supply bar-level spread info.
    """
    if atr <= 0:
        return None

    if config.use_session_range_sl and session_range_price > 0:
        sl_distance = min(
            session_range_price * config.session_range_sl_fraction,
            config.hard_cap_sl_pips * pip_value,
        )
    else:
        sl_distance = min(atr * config.atr_sl_multiplier, config.hard_cap_sl_pips * pip_value)

    if sl_distance <= 0:
        return None

    # Enforce minimum SL distance (5 pips) to prevent tiny stops
    min_sl = _MIN_SL_PIPS * pip_value
    if sl_distance < min_sl:
        sl_distance = min_sl

    # Anchor TP baseline to the broker fill price (ASK for BUY, BID for SELL)
    # so the broker always sees TP > fill for BUY / TP < fill for SELL.
    # SL stays anchored to mid (entry) — it's already well past the fill on
    # the correct side as long as sl_distance > half-spread.
    half_spread = spread_price / 2.0
    if direction == TradeDirection.LONG:
        sl = entry - sl_distance
        tp_baseline = entry + half_spread  # ASK
    else:
        sl = entry + sl_distance
        tp_baseline = entry - half_spread  # BID

    risk = sl_distance
    tp1 = tp_baseline + risk * config.tp1_rr if direction == TradeDirection.LONG else tp_baseline - risk * config.tp1_rr
    tp2 = tp_baseline + risk * config.tp2_rr if direction == TradeDirection.LONG else tp_baseline - risk * config.tp2_rr

    # Guard clause: ensure TP direction is consistent with trade direction
    # relative to the strategy's mid-price entry. Should be impossible with
    # the fill-price anchoring above, but defense-in-depth for edge cases
    # (zero/negative spread, NaN, sl_distance exactly at half-spread).
    if direction == TradeDirection.LONG and tp1 <= entry:
        return None
    if direction == TradeDirection.SHORT and tp1 >= entry:
        return None

    return StrategySignal(
        direction=direction,
        confidence=0.70,
        entry_price=entry,
        stop_loss=sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        take_profit_3=tp2,
        rationale=rationale,
    )


class SessionRangeMeanReversionStrategy:
    def __init__(self, config: SessionRangeMRConfig | None = None):
        self.config = config or SessionRangeMRConfig()

    @property
    def name(self) -> str:
        return "Session-Range Mean Reversion"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = max(
            self.config.atr_period + self.config.rsi_period + 2,
            self.config.ema_trend_period + 1,
        )
        if len(state.bars) < min_required:
            return None

        if not _is_in_asian_or_early_london(state):
            return None

        if _is_in_london_ny_overlap(state):
            return None

        atr = _calculate_atr(state.bars, self.config.atr_period)
        if atr <= 0:
            return None

        latest = state.latest_bar
        current_day = latest.time.date()

        prev_day = self._find_previous_trading_day(state.bars, current_day)

        session_high, session_low, session_mean = _calculate_session_range(
            state.bars, SessionType.LONDON, reference_day=prev_day
        )
        if session_high == 0:
            session_high, session_low, session_mean = _calculate_session_range(
                state.bars, SessionType.NY_AM, reference_day=prev_day
            )

        session_range_price = session_high - session_low
        pip = self.config.pip_value if self.config.pip_value is not None else _pip_value_for_price(latest.close)
        session_range_width = session_range_price / pip
        if session_range_width < self.config.session_range_min_pips:
            return None

        price = latest.close

        rsi = _calculate_rsi(state.bars, self.config.rsi_period)
        if rsi is None:
            return None

        entry_near_extreme_pips = self.config.entry_near_extreme_pips * pip

        # Compute the bar's bid/ask spread in price units so TP can be anchored
        # to the broker fill price (ASK for BUY, BID for SELL). Without this,
        # mid-anchored TP can land below the ASK fill on wide-spread symbols
        # (e.g. XAUUSD) and get rejected with TRADING_BAD_STOPS — see card
        # f2317859 for the 2026-07-17 00:15:25 incident.
        spread_price = latest.spread_pips * pip

        if price <= session_low + entry_near_extreme_pips and rsi < self.config.rsi_long_level:
            direction = TradeDirection.LONG
            rationale = (
                f"Session range MR long: price={price:.5f} near session low={session_low:.5f}, "
                f"RSI={rsi:.1f}, range={session_range_width:.1f} pips"
            )
            return _build_signal(
                direction,
                price,
                atr,
                self.config,
                session_range_price,
                rationale,
                pip,
                spread_price=spread_price,
            )

        if price >= session_high - entry_near_extreme_pips and rsi > self.config.rsi_short_level:
            direction = TradeDirection.SHORT
            rationale = (
                f"Session range MR short: price={price:.5f} near session high={session_high:.5f}, "
                f"RSI={rsi:.1f}, range={session_range_width:.1f} pips"
            )
            return _build_signal(
                direction,
                price,
                atr,
                self.config,
                session_range_price,
                rationale,
                pip,
                spread_price=spread_price,
            )

        return None

    @staticmethod
    def _find_previous_trading_day(bars: list[Bar], current_day: date) -> date | None:
        seen_days: set[date] = set()
        for b in bars:
            d = b.time.date()
            if d < current_day:
                seen_days.add(d)
        if not seen_days:
            return None
        return max(seen_days)


@dataclass(frozen=True)
class SessionRangeMRWithRegimeFilterConfig:
    adx_period: int = 14
    adx_skip_threshold: float = 30.0
    adx_transition_low: float = 20.0
    transition_min_confidence: float = 0.65
    base_min_confidence: float = 0.50
    regime_confidence_multiplier: float = 0.95


class SessionRangeMRWithRegimeFilter:
    def __init__(
        self,
        config: SessionRangeMRWithRegimeFilterConfig | None = None,
        base_config: SessionRangeMRConfig | None = None,
    ):
        self.config = config or SessionRangeMRWithRegimeFilterConfig()
        self.mr_strategy = SessionRangeMeanReversionStrategy(base_config)

    @property
    def name(self) -> str:
        return "Session-Range MR with Regime Filter"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        base_signal = self.mr_strategy.evaluate(state)
        if base_signal is None or base_signal.direction is None:
            return None

        adx = self._calculate_adx(state.bars)
        min_required = max(
            self.config.adx_period * 2 + 1,
            self.mr_strategy.config.atr_period + self.mr_strategy.config.rsi_period + 2,
            self.mr_strategy.config.ema_trend_period + 1,
        )
        if len(state.bars) < min_required:
            return None

        if adx > self.config.adx_skip_threshold:
            return None

        if adx > self.config.adx_transition_low:
            if base_signal.confidence < self.config.transition_min_confidence:
                return None

        if base_signal.confidence < self.config.base_min_confidence:
            return None

        adjusted_confidence = base_signal.confidence * self.config.regime_confidence_multiplier
        return StrategySignal(
            direction=base_signal.direction,
            confidence=adjusted_confidence,
            entry_price=base_signal.entry_price,
            stop_loss=base_signal.stop_loss,
            take_profit_1=base_signal.take_profit_1,
            take_profit_2=base_signal.take_profit_2,
            take_profit_3=base_signal.take_profit_3,
            rationale=f"[RegimeFilter ADX={adx:.1f}] {base_signal.rationale}",
        )

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

        dx_list: list[float] = [dx]
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

        if len(dx_list) < period:
            return 0.0

        adx = sum(dx_list[:period]) / period
        for dx in dx_list[period:]:
            adx = (adx * (period - 1) + dx) / period

        return adx
