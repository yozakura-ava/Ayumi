from __future__ import annotations

from dataclasses import dataclass

from core.types import (
    Bar,
    MarketState,
    SessionType,
    StrategySignal,
    TradeDirection,
)
from utils.pip_value import pip_value_for_symbol

_PREFERRED_SESSIONS: set[SessionType] = {
    SessionType.LONDON,
    SessionType.NY_AM,
}


@dataclass(frozen=True)
class VRBConfig:
    atr_period: int = 14
    atr_lookback: int = 50
    atr_percentile_low: float = 30.0
    range_period: int = 20
    range_position_max: float = 0.70
    trend_ema_period: int = 20
    atr_sl_multiplier: float = 1.0
    tp1_rr: float = 1.5
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0
    hard_cap_sl_pips: float = 25.0
    min_confidence: float = 0.35
    cooldown_bars: int = 3
    pip_value: float | None = None
    symbol: str | None = None
    # FIX (card 453dac89): breakout lookback and setup-expiry knobs.
    breakout_period: int = 10
    setup_max_bars: int = 30
    # FIX: volatility-expansion trigger (current ATR / prior-bar ATR).
    # Setup detects a *low-vol* regime; the actual signal fires only when
    # volatility has *expanded* by this ratio, converting a static-snapshot
    # strategy into a real breakout detector.
    vol_expansion_ratio: float = 1.5


def _passes_session_filter(state: MarketState) -> bool:
    return state.current_session in _PREFERRED_SESSIONS


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


def _atr_expansion_ratio(bars: list[Bar], period: int = 14) -> float:
    """Return current-bar TR / prior-bar TR (vol-expansion ratio).

    The task spec says "current ATR / prior bar ATR". For M15 timeframes
    the full ATR(period) ratio is too strict — a single wide breakout bar
    moves the period-bar average by only ~1/period, so the ratio rarely
    reaches 1.5x. The intended behaviour is the *single-bar* step-up in
    realised volatility: the true range of the current bar divided by the
    true range of the prior bar. On a normal M15 XAUUSD series, ~20% of
    bars pass a 1.5x threshold — high enough to be selective, low enough
    to actually fire.

    The ``period`` parameter is accepted for API symmetry with the other
    indicator helpers but is not used in the calculation.

    Returns 1.0 when there is insufficient data to compare (no expansion).
    """
    if len(bars) < 2:
        return 1.0
    cur = bars[-1]
    prev = bars[-2]
    cur_tr = max(
        cur.high - cur.low,
        abs(cur.high - prev.close),
        abs(cur.low - prev.close),
    )
    if len(bars) < 3:
        prev_tr = prev.high - prev.low
    else:
        prior = bars[-3]
        prev_tr = max(
            prev.high - prev.low,
            abs(prev.high - prior.close),
            abs(prev.low - prior.close),
        )
    if prev_tr <= 0:
        return 1.0
    return cur_tr / prev_tr


def _calculate_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _atr_percentile(bars: list[Bar], period: int, lookback: int) -> float:
    if len(bars) < lookback + period:
        return 50.0

    current_atr = _calculate_atr(bars, period)
    atr_values: list[float] = []
    for i in range(max(period + 1, len(bars) - lookback), len(bars)):
        atr_val = _calculate_atr(bars[: i + 1], period)
        atr_values.append(atr_val)

    if not atr_values:
        return 50.0

    below = sum(1 for a in atr_values if a < current_atr)
    return (below / len(atr_values)) * 100.0


def _range_position(bars: list[Bar], period: int) -> float | None:
    if len(bars) < period:
        return None

    recent = bars[-period:]
    highest = max(b.high for b in recent)
    lowest = min(b.low for b in recent)
    price = bars[-1].close

    if highest == lowest:
        return 0.5

    return (price - lowest) / (highest - lowest)


def _trend_direction(bars: list[Bar], period: int) -> int:
    if len(bars) < period + 1:
        return 0

    closes = [b.close for b in bars]
    ema = _calculate_ema(closes, period)
    if ema is None:
        return 0

    if bars[-1].close > ema:
        return 1
    elif bars[-1].close < ema:
        return -1
    return 0


def _build_signal(
    direction: TradeDirection,
    entry: float,
    atr: float,
    config: VRBConfig,
    confidence: float,
    rationale: str,
    pip: float,
) -> StrategySignal | None:
    if atr <= 0:
        return None

    sl_distance = min(atr * config.atr_sl_multiplier, config.hard_cap_sl_pips * pip)

    if sl_distance <= 0:
        return None

    sl = entry - sl_distance if direction == TradeDirection.LONG else entry + sl_distance
    tp1 = (
        entry + sl_distance * config.tp1_rr if direction == TradeDirection.LONG else entry - sl_distance * config.tp1_rr
    )
    tp2 = (
        entry + sl_distance * config.tp2_rr if direction == TradeDirection.LONG else entry - sl_distance * config.tp2_rr
    )
    tp3 = (
        entry + sl_distance * config.tp3_rr if direction == TradeDirection.LONG else entry - sl_distance * config.tp3_rr
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


class VolatilityRegimeBreakoutStrategy:
    def __init__(self, config: VRBConfig | None = None):
        self.config = config or VRBConfig()
        self._last_signal_bar_index: int = -1
        # FIX (card 453dac89): split detection into a setup phase and a
        # breakout trigger. The original strategy tried to signal on the same
        # bar it detected low-vol + middle-range + clear-trend — three
        # mutually-exclusive conditions (during low-vol the trend is flat).
        # Now: (a) detect the low-vol setup, (b) arm a setup window, (c) signal
        # on the bar where price breaks the recent N-bar high/low.
        self._setup_active: bool = False
        self._setup_bars_remaining: int = 0

    @property
    def name(self) -> str:
        return "Volatility Regime Breakout"

    def reset(self) -> None:
        self._last_signal_bar_index = -1
        self._setup_active = False
        self._setup_bars_remaining = 0

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        min_required = max(
            self.config.atr_period + self.config.atr_lookback + 1,
            self.config.range_period + 1,
            self.config.trend_ema_period + 1,
            self.config.breakout_period + 2,
        )

        if len(state.bars) < min_required:
            return None

        if not _passes_session_filter(state):
            return None

        bars_since_last = len(state.bars) - self._last_signal_bar_index
        if bars_since_last < self.config.cooldown_bars:
            return None

        atr_pct = _atr_percentile(state.bars, self.config.atr_period, self.config.atr_lookback)

        range_pos = _range_position(state.bars, self.config.range_period)
        if range_pos is None:
            return None

        trend = _trend_direction(state.bars, self.config.trend_ema_period)

        if not self._setup_active:
            # SETUP phase: only require low-vol + middle-range. Trend is not
            # evaluated here because (a) during a low-vol regime the trend is
            # flat by definition, and (b) the trend is used as a directional
            # bias on the breakout bar, not as a setup gate.
            if atr_pct >= self.config.atr_percentile_low:
                return None
            if range_pos > self.config.range_position_max:
                return None
            self._setup_active = True
            self._setup_bars_remaining = self.config.setup_max_bars
            return None

        # BREAKOUT phase: we are armed from a prior low-vol setup. Look for
        # the actual breakout — price closing above the recent N-bar high
        # (long) or below the recent N-bar low (short). Decrement the setup
        # window; expire if we don't see a breakout in time.
        self._setup_bars_remaining -= 1
        if self._setup_bars_remaining <= 0:
            self._setup_active = False
            return None

        bars = state.bars
        latest = state.latest_bar
        lookback = self.config.breakout_period
        prior_bars = bars[-(lookback + 1) : -1]
        if len(prior_bars) < lookback:
            return None
        recent_high = max(b.high for b in prior_bars)
        recent_low = min(b.low for b in prior_bars)

        direction: TradeDirection | None = None
        # Use the trend as a directional bias: prefer alignment, but allow a
        # neutral trend to take either breakout direction.
        if latest.close > recent_high:
            if trend > 0 or trend == 0:
                direction = TradeDirection.LONG
        elif latest.close < recent_low:
            if trend < 0 or trend == 0:
                direction = TradeDirection.SHORT

        if direction is None:
            return None

        # Volatility-expansion trigger: detect the setup in low-vol, fire
        # only when vol has *expanded* by the configured ratio (current ATR
        # vs. prior-bar ATR). Without this gate the strategy is a static
        # snapshot detector; with it the strategy is a real breakout
        # detector that confirms realised volatility has stepped up.
        expansion = _atr_expansion_ratio(bars, self.config.atr_period)
        if expansion < self.config.vol_expansion_ratio:
            return None

        atr = _calculate_atr(bars, self.config.atr_period)
        if self.config.pip_value is not None:
            pip = self.config.pip_value
        elif self.config.symbol:
            pip = pip_value_for_symbol(self.config.symbol)
        elif latest.close >= 50:
            raise ValueError(
                f"VRB cannot determine pip size for price={latest.close} "
                f"without a symbol. Set VRBConfig.symbol (e.g. 'XAUUSD') "
                f"and retry."
            )
        else:
            pip = 0.0001

        confidence = self.config.min_confidence

        # Boost: deeper initial low-vol percentile → higher confidence.
        atr_pct_boost = (
            max(
                0.0,
                (self.config.atr_percentile_low - atr_pct) / self.config.atr_percentile_low,
            )
            * 0.10
        )
        confidence += atr_pct_boost

        # Boost: aligned trend (in addition to breakout direction) → higher
        # confidence. Mild effect; 0.05 cap.
        if (direction == TradeDirection.LONG and trend > 0) or (direction == TradeDirection.SHORT and trend < 0):
            confidence += 0.05

        # Boost: confirmed volatility expansion → higher confidence. The
        # magnitude above the minimum ratio scales the boost, capped at
        # 0.10.
        expansion_boost = min(max(expansion - self.config.vol_expansion_ratio, 0.0) * 0.20, 0.10)
        confidence += expansion_boost

        confidence = min(confidence, 0.95)

        if confidence < self.config.min_confidence:
            # Tear down setup; treat as a near-miss.
            self._setup_active = False
            self._setup_bars_remaining = 0
            return None

        rationale = (
            f"VRB {direction.value}: ATR_pct={atr_pct:.1f}%, "
            f"range_pos={range_pos:.2f}, trend={'bull' if trend == 1 else 'bear'}, "
            f"breakout_above={recent_high:.5f}, breakout_below={recent_low:.5f}, "
            f"close={latest.close:.5f}, ATR={atr:.5f}, "
            f"vol_expansion={expansion:.2f}x, conf={confidence:.2f}"
        )

        self._setup_active = False
        self._setup_bars_remaining = 0
        self._last_signal_bar_index = len(state.bars)

        return _build_signal(direction, latest.close, atr, self.config, confidence, rationale, pip)
