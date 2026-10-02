from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class MomentumConfig:
    atr_period: int = 14
    atr_sl_multiplier: float = 2.0
    atr_trail_multiplier: float = 1.5
    min_adx: float = 20.0
    rsi_period: int = 14
    rsi_max: float = 70.0
    rsi_min: float = 30.0
    session_filter: bool = True
    min_confidence: float = 0.50
    tp1_rr: float = 1.0
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0


EURUSD_M15_PRESETS = {
    "donchian": {
        "channel_period": 20,
        "exit_channel_period": 10,
        "momentum": MomentumConfig(
            atr_period=14,
            atr_sl_multiplier=2.0,
            atr_trail_multiplier=1.5,
            min_adx=20.0,
            rsi_period=14,
            rsi_max=70.0,
            rsi_min=30.0,
            session_filter=True,
            min_confidence=0.50,
            tp1_rr=1.0,
            tp2_rr=2.0,
            tp3_rr=3.0,
        ),
    },
    "atr_breakout": {
        "atr_period": 14,
        "breakout_multiplier": 1.5,
        "confirmation_bars": 1,
        "momentum": MomentumConfig(
            atr_period=14,
            atr_sl_multiplier=2.0,
            atr_trail_multiplier=1.5,
            min_adx=20.0,
            rsi_period=14,
            rsi_max=70.0,
            rsi_min=30.0,
            session_filter=True,
            min_confidence=0.50,
            tp1_rr=1.0,
            tp2_rr=2.0,
            tp3_rr=3.0,
        ),
    },
    "ma_trend": {
        "fast_period": 8,
        "slow_period": 21,
        "trend_ma_period": 50,
        "momentum": MomentumConfig(
            atr_period=14,
            atr_sl_multiplier=2.0,
            atr_trail_multiplier=1.5,
            min_adx=20.0,
            rsi_period=14,
            rsi_max=70.0,
            rsi_min=30.0,
            session_filter=True,
            min_confidence=0.50,
            tp1_rr=1.0,
            tp2_rr=2.0,
            tp3_rr=3.0,
        ),
    },
}


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


def _calculate_adx(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
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
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx


def _calculate_sma(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _passes_session_filter(state: MarketState) -> bool:
    return state.current_session in _PREFERRED_SESSIONS


def _passes_momentum_filters(
    bars: list[Bar],
    config: MomentumConfig,
    direction: TradeDirection,
) -> bool:
    rsi = _calculate_rsi(bars, config.rsi_period)
    if rsi is not None:
        if direction == TradeDirection.LONG and rsi > config.rsi_max:
            return False
        if direction == TradeDirection.SHORT and rsi < config.rsi_min:
            return False

    adx = _calculate_adx(bars, config.atr_period)
    if adx < config.min_adx:
        return False

    return True


def _build_signal(
    direction: TradeDirection,
    entry: float,
    atr: float,
    config: MomentumConfig,
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


class DonchianBreakoutStrategy:
    def __init__(
        self,
        channel_period: int = 20,
        exit_channel_period: int = 10,
        momentum: MomentumConfig | None = None,
    ):
        self.channel_period = channel_period
        self.exit_channel_period = exit_channel_period
        self.momentum = momentum or MomentumConfig()

    @property
    def name(self) -> str:
        return "Donchian Channel Breakout"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = self.channel_period + 2
        if len(state.bars) < min_required:
            return None

        if self.momentum.session_filter and not _passes_session_filter(state):
            return None

        atr = state.atr if state.atr > 0 else _calculate_atr(state.bars, self.momentum.atr_period)

        lookback = state.bars[-(self.channel_period + 1) : -1]
        channel_high = max(b.high for b in lookback)
        channel_low = min(b.low for b in lookback)
        latest = state.latest_bar

        bullish_breakout = latest.close > channel_high
        bearish_breakout = latest.close < channel_low

        if not bullish_breakout and not bearish_breakout:
            return None

        if bullish_breakout:
            direction = TradeDirection.LONG
            if not _passes_momentum_filters(state.bars, self.momentum, direction):
                return None
            entry = latest.close
            penetration = (latest.close - channel_high) / atr if atr > 0 else 0
            confidence = min(0.90, 0.50 + min(penetration, 1.0) * 0.40)
            rationale = (
                f"Donchian bullish breakout: close={latest.close:.5f} > "
                f"channel_high={channel_high:.5f} ({self.channel_period}-bar)"
            )
        else:
            direction = TradeDirection.SHORT
            if not _passes_momentum_filters(state.bars, self.momentum, direction):
                return None
            entry = latest.close
            penetration = (channel_low - latest.close) / atr if atr > 0 else 0
            confidence = min(0.90, 0.50 + min(penetration, 1.0) * 0.40)
            rationale = (
                f"Donchian bearish breakout: close={latest.close:.5f} < "
                f"channel_low={channel_low:.5f} ({self.channel_period}-bar)"
            )

        return _build_signal(direction, entry, atr, self.momentum, confidence, rationale)


class ATRVolatilityBreakoutStrategy:
    def __init__(
        self,
        atr_period: int = 14,
        breakout_multiplier: float = 1.5,
        confirmation_bars: int = 1,
        momentum: MomentumConfig | None = None,
    ):
        self.atr_period = atr_period
        self.breakout_multiplier = breakout_multiplier
        self.confirmation_bars = confirmation_bars
        self.momentum = momentum or MomentumConfig()

    @property
    def name(self) -> str:
        return "ATR Volatility Breakout"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = self.atr_period + self.confirmation_bars + 2
        if len(state.bars) < min_required:
            return None

        if self.momentum.session_filter and not _passes_session_filter(state):
            return None

        atr = _calculate_atr(state.bars, self.atr_period)
        if atr <= 0:
            return None

        lookback = state.bars[-(self.atr_period + 1) : -1]
        ref_high = max(b.high for b in lookback)
        ref_low = min(b.low for b in lookback)

        breakout_level = atr * self.breakout_multiplier
        latest = state.latest_bar

        bullish_breakout = latest.close > ref_high + breakout_level
        bearish_breakout = latest.close < ref_low - breakout_level

        if not bullish_breakout and not bearish_breakout:
            return None

        if bullish_breakout:
            confirm = self._confirm_breakout(state.bars, ref_high + breakout_level, bullish=True)
            if not confirm:
                return None
            direction = TradeDirection.LONG
            if not _passes_momentum_filters(state.bars, self.momentum, direction):
                return None
            entry = latest.close
            confidence = min(
                0.85,
                0.50 + min((latest.close - ref_high - breakout_level) / atr, 1.0) * 0.35,
            )
            rationale = (
                f"ATR bullish breakout: close={latest.close:.5f} > "
                f"ref_high+ATR*{self.breakout_multiplier}={ref_high + breakout_level:.5f}"
            )
        else:
            confirm = self._confirm_breakout(state.bars, ref_low - breakout_level, bullish=False)
            if not confirm:
                return None
            direction = TradeDirection.SHORT
            if not _passes_momentum_filters(state.bars, self.momentum, direction):
                return None
            entry = latest.close
            confidence = min(
                0.85,
                0.50 + min((ref_low - breakout_level - latest.close) / atr, 1.0) * 0.35,
            )
            rationale = (
                f"ATR bearish breakout: close={latest.close:.5f} < "
                f"ref_low-ATR*{self.breakout_multiplier}={ref_low - breakout_level:.5f}"
            )

        return _build_signal(direction, entry, atr, self.momentum, confidence, rationale)

    def _confirm_breakout(self, bars: list[Bar], level: float, bullish: bool) -> bool:
        check_count = min(self.confirmation_bars, len(bars))
        if check_count == 0:
            return True
        confirmed = 0
        for i in range(len(bars) - check_count, len(bars)):
            if bullish and bars[i].close > level:
                confirmed += 1
            elif not bullish and bars[i].close < level:
                confirmed += 1
        return confirmed >= self.confirmation_bars


class MATrendFollowingStrategy:
    def __init__(
        self,
        fast_period: int = 8,
        slow_period: int = 21,
        trend_ma_period: int = 50,
        momentum: MomentumConfig | None = None,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_ma_period = trend_ma_period
        self.momentum = momentum or MomentumConfig()

    @property
    def name(self) -> str:
        return "MA Trend Following"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = self.trend_ma_period + 2
        if len(state.bars) < min_required:
            return None

        if self.momentum.session_filter and not _passes_session_filter(state):
            return None

        atr = state.atr if state.atr > 0 else _calculate_atr(state.bars, self.momentum.atr_period)

        closes = [b.close for b in state.bars]
        fast_ma = _calculate_sma(closes, self.fast_period)
        slow_ma = _calculate_sma(closes, self.slow_period)
        trend_ma = _calculate_sma(closes, self.trend_ma_period)

        if fast_ma == 0 or slow_ma == 0 or trend_ma == 0:
            return None

        prev_closes = [b.close for b in state.bars[:-1]]
        prev_fast_ma = _calculate_sma(prev_closes, self.fast_period)
        prev_slow_ma = _calculate_sma(prev_closes, self.slow_period)

        bullish_cross = prev_fast_ma <= prev_slow_ma and fast_ma > slow_ma
        bearish_cross = prev_fast_ma >= prev_slow_ma and fast_ma < slow_ma

        if not bullish_cross and not bearish_cross:
            return None

        if bullish_cross:
            if closes[-1] <= trend_ma:
                return None
            direction = TradeDirection.LONG
            if not _passes_momentum_filters(state.bars, self.momentum, direction):
                return None
            entry = state.latest_bar.close
            trend_strength = min(1.0, abs(fast_ma - slow_ma) / slow_ma * 100)
            confidence = min(0.90, 0.50 + trend_strength * 0.30)
            rationale = (
                f"MA bullish cross above trend: fast={fast_ma:.5f} > "
                f"slow={slow_ma:.5f}, price > trend_ma={trend_ma:.5f}"
            )
        else:
            if closes[-1] >= trend_ma:
                return None
            direction = TradeDirection.SHORT
            if not _passes_momentum_filters(state.bars, self.momentum, direction):
                return None
            entry = state.latest_bar.close
            trend_strength = min(1.0, abs(fast_ma - slow_ma) / slow_ma * 100)
            confidence = min(0.90, 0.50 + trend_strength * 0.30)
            rationale = (
                f"MA bearish cross below trend: fast={fast_ma:.5f} < "
                f"slow={slow_ma:.5f}, price < trend_ma={trend_ma:.5f}"
            )

        return _build_signal(direction, entry, atr, self.momentum, confidence, rationale)
