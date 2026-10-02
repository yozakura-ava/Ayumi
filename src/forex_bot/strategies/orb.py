"""ORB (Opening Range Breakout) Strategy — full tradeable strategy module.

Implements a configurable ORB strategy that:
1. Builds the opening range for a given session (London, NY, Asian)
2. Enters on breakout above/below the range high/low
3. Uses ATR-buffered stop-loss
4. Scales out at 1R / 2R / 3R with partial exits
5. Enforces one signal per direction per session per day

This is distinct from ``signal_engine/orb_filter.py`` which is a signal
scoring/prioritization filter. This module is a self-contained strategy
implementing the ``evaluate(state: MarketState) -> StrategySignal | None``
contract used by the backtest harness.

Configuration (via constructor ``config`` dict):
    name: Display name
    session: "london", "ny", or "asian"
    range_start_hour / range_end_hour: UTC hours defining the range window
    trade_start_hour / trade_end_hour: UTC hours defining the trade window
    breakout_buffer_pips: min penetration beyond OR edge
    min_range_pips / max_range_pips: valid range width bounds
    atr_period: lookback for ATR
    sl_atr_multiplier: ATR multiplier for stop-loss distance
    direction_filter: "long", "short", or "both"
    min_range_bars: minimum bars in range window
    h4_trend_filter: require H4 EMA50 alignment
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.types import Bar, MarketState, StrategySignal, TradeDirection
from utils.pip_value import DEFAULT_PIP, pip_value_for_symbol

logger = logging.getLogger(__name__)

# ── Price / range sanity guards ────────────────────────────────────────────
_FX_MIN_PRICE = 0.01
_FX_MAX_PRICE = 500.0
_MAX_RANGE_PIPS = 500
_MAX_TP_DISTANCE_PIPS = 1000


# ── ATR helper ─────────────────────────────────────────────────────────────


def _calculate_atr(bars: list[Bar], period: int = 14) -> float:
    """Compute the Average True Range over the last *period* bars."""
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


def _pip_size_for_symbol(symbol: str) -> float:
    """Return pip size for the given symbol via utils.pip_value."""
    try:
        return pip_value_for_symbol(symbol)
    except (ValueError, TypeError):
        return DEFAULT_PIP


def _ema(values: list[float], period: int) -> float | None:
    """Simple EMA — returns None if insufficient data."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema


# ── Default session configs ────────────────────────────────────────────────

SESSION_DEFAULTS: dict[str, dict] = {
    "london": {
        "range_start_hour": 7,
        "range_end_hour": 8,  # 60-min range
        "trade_start_hour": 8,
        "trade_end_hour": 16,
    },
    "ny": {
        "range_start_hour": 12,
        "range_end_hour": 13,  # 60-min range (adjustable to 30 min)
        "trade_start_hour": 13,
        "trade_end_hour": 21,
    },
    "asian": {
        "range_start_hour": 0,
        "range_end_hour": 1,  # 60-min range
        "trade_start_hour": 1,
        "trade_end_hour": 6,
    },
}


# ── Strategy ───────────────────────────────────────────────────────────────


class ORBStrategy:
    """Opening Range Breakout strategy.

    Config keys (all optional except ``name`` and ``session``):
        name: Display name
        session: "london", "ny", or "asian"
        range_start_hour / range_end_hour: UTC hours (override session default)
        trade_start_hour / trade_end_hour: UTC hours (override session default)
        breakout_buffer_pips: min penetration beyond OR edge (default 2.0)
        min_range_pips: minimum range width (default 5.0)
        max_range_pips: maximum range width (default 40.0)
        atr_period: ATR lookback (default 14)
        sl_atr_multiplier: SL = OR edge ± mult × ATR (default 1.5)
        direction_filter: "long", "short", or "both" (default "both")
        min_range_bars: min bars to form a range (default 4)
        h4_trend_filter: require H4 EMA50 alignment (default False)
        rr_tp1 / rr_tp2 / rr_tp3: R-multiples for partial exits
    """

    def __init__(self, config: dict) -> None:
        self._name = config.get("name", "ORB")
        session = config.get("session", "london").lower()
        defaults = SESSION_DEFAULTS.get(session, SESSION_DEFAULTS["london"])

        self.session = session
        self.range_start_hour: int = config.get("range_start_hour", defaults["range_start_hour"])
        self.range_end_hour: int = config.get("range_end_hour", defaults["range_end_hour"])
        self.trade_start_hour: int = config.get("trade_start_hour", defaults["trade_start_hour"])
        self.trade_end_hour: int = config.get("trade_end_hour", defaults["trade_end_hour"])

        self.breakout_buffer_pips: float = config.get("breakout_buffer_pips", 2.0)
        self.min_range_pips: float = config.get("min_range_pips", 5.0)
        self.max_range_pips: float = config.get("max_range_pips", 40.0)
        self.atr_period: int = config.get("atr_period", 14)
        self.sl_atr_multiplier: float = config.get("sl_atr_multiplier", 1.5)
        self.direction_filter: str = config.get("direction_filter", "both").lower()
        self.min_range_bars: int = config.get("min_range_bars", 4)
        self.h4_trend_filter: bool = config.get("h4_trend_filter", False)

        # Partial exit R-multiples (used for TP levels)
        self.rr_tp1: float = config.get("rr_tp1", 1.0)
        self.rr_tp2: float = config.get("rr_tp2", 2.0)
        self.rr_tp3: float = config.get("rr_tp3", 3.0)

        # Cache: {(date_string, symbol): {"high": float, "low": float, "valid": bool, "width_pips": float}}
        self._range_cache: dict[tuple[str, str], dict | None] = {}

        # Fired signals: {(date_string, symbol, direction): True}
        self._fired_signals: dict[tuple[str, str, str], bool] = {}

    @property
    def name(self) -> str:
        return self._name

    def reset(self) -> None:
        """Clear caches for a fresh run."""
        self._range_cache.clear()
        self._fired_signals.clear()

    # ── Range window helpers ───────────────────────────────────────────────

    def _bar_in_range_window(self, bar_time: datetime) -> bool:
        """Check if bar falls within the range definition window (UTC)."""
        hour = bar_time.hour
        start = self.range_start_hour
        end = self.range_end_hour

        if start < end:
            return start <= hour < end
        else:
            # Wrapping window (e.g. 23–2)
            return hour >= start or hour < end

    def _compute_range(self, bars: list[Bar], date_str: str, symbol: str) -> dict | None:
        """Compute opening range from bars in the range window."""
        cache_key = (date_str, symbol)
        if cache_key in self._range_cache:
            return self._range_cache[cache_key]

        range_bars = [b for b in bars if self._bar_in_range_window(b.time)]

        if len(range_bars) < self.min_range_bars:
            self._range_cache[cache_key] = None
            return None

        range_high = max(b.high for b in range_bars)
        range_low = min(b.low for b in range_bars)
        pip = _pip_size_for_symbol(symbol)
        range_width_pips = (range_high - range_low) / pip

        valid = self.min_range_pips <= range_width_pips <= self.max_range_pips

        result = {
            "high": range_high,
            "low": range_low,
            "width_pips": range_width_pips,
            "valid": valid,
        }
        self._range_cache[cache_key] = result
        return result

    # ── H4 trend filter ───────────────────────────────────────────────────

    def _h4_trend_direction(self, bars: list[Bar]) -> TradeDirection | None:
        """Resample M15 bars into ~H4 and compute 50 EMA direction."""
        if len(bars) < 200:
            return None

        chunk_size = 16  # 16 × M15 = 4h
        h4_closes: list[float] = []
        for i in range(0, len(bars), chunk_size):
            chunk = bars[i : i + chunk_size]
            if len(chunk) < 4:
                continue
            h4_closes.append(chunk[-1].close)

        if len(h4_closes) < 50:
            return None

        ema_val = _ema(h4_closes, 50)
        if ema_val is None:
            return None

        current_price = bars[-1].close
        if current_price > ema_val:
            return TradeDirection.LONG
        elif current_price < ema_val:
            return TradeDirection.SHORT
        return None

    # ── Main evaluate ─────────────────────────────────────────────────────

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        """Evaluate the strategy on the current market state.

        Returns a ``StrategySignal`` on breakout, or ``None``.
        """
        if len(state.bars) < self.min_range_bars + 10:
            return None

        latest = state.latest_bar
        bar_time = latest.time

        if bar_time.tzinfo is not None:
            bar_time = bar_time.astimezone(timezone.utc)

        bar_hour = bar_time.hour
        date_str = bar_time.strftime("%Y-%m-%d")
        symbol = getattr(state, "symbol", "")

        # 1. Must be inside the trade window
        if not (self.trade_start_hour <= bar_hour < self.trade_end_hour):
            return None

        # 2. Compute / retrieve cached range
        range_data = self._compute_range(state.bars, date_str, symbol)
        if range_data is None or not range_data["valid"]:
            return None

        range_high = range_data["high"]
        range_low = range_data["low"]
        pip = _pip_size_for_symbol(symbol)
        buffer_price = self.breakout_buffer_pips * pip

        # 3. Check breakout
        bullish_breakout = latest.close > range_high + buffer_price
        bearish_breakout = latest.close < range_low - buffer_price

        if not bullish_breakout and not bearish_breakout:
            return None

        # 4. Determine direction
        if bullish_breakout:
            direction = TradeDirection.LONG
        else:
            direction = TradeDirection.SHORT

        # 5. Direction filter
        if self.direction_filter != "both" and direction.value != self.direction_filter:
            return None

        # 6. One signal per direction per session per day
        fired_key = (date_str, symbol, direction.value)
        if fired_key in self._fired_signals:
            return None

        # 7. Optional H4 trend filter
        if self.h4_trend_filter:
            h4_dir = self._h4_trend_direction(state.bars)
            if h4_dir is not None and h4_dir != direction:
                return None

        # 8. Calculate ATR for stop-loss
        atr = _calculate_atr(state.bars, self.atr_period)
        if atr <= 0:
            return None

        # 9. Calculate levels — ATR-buffered SL, R-multiple TPs
        entry = latest.close
        sl_distance = atr * self.sl_atr_multiplier
        range_width = range_high - range_low

        if direction == TradeDirection.LONG:
            stop_loss = entry - sl_distance
            risk = entry - stop_loss
            take_profit_1 = entry + self.rr_tp1 * risk
            take_profit_2 = entry + self.rr_tp2 * risk
            take_profit_3 = entry + self.rr_tp3 * risk
        else:
            stop_loss = entry + sl_distance
            risk = stop_loss - entry
            take_profit_1 = entry - self.rr_tp1 * risk
            take_profit_2 = entry - self.rr_tp2 * risk
            take_profit_3 = entry - self.rr_tp3 * risk

        # 10. Price sanity guards
        range_width_pips = range_width / pip
        if range_width_pips > _MAX_RANGE_PIPS:
            return None

        for tp in (take_profit_1, take_profit_2, take_profit_3):
            if tp < _FX_MIN_PRICE or tp > _FX_MAX_PRICE:
                return None

        tp_distance_pips = abs(take_profit_3 - entry) / pip
        if tp_distance_pips > _MAX_TP_DISTANCE_PIPS:
            return None

        # 11. Confidence based on breakout strength
        if bullish_breakout:
            penetration = (latest.close - range_high - buffer_price) / atr
        else:
            penetration = (range_low - buffer_price - latest.close) / atr
        confidence = min(0.90, 0.55 + min(penetration, 1.0) * 0.35)

        # 12. Mark as fired
        self._fired_signals[fired_key] = True

        direction_word = "bullish" if bullish_breakout else "bearish"
        rationale = (
            f"ORB {self.session} {direction_word}: close={latest.close:.5f} "
            f"breaks range [{range_low:.5f}-{range_high:.5f}] "
            f"width={range_data['width_pips']:.1f}pips "
            f"buffer={self.breakout_buffer_pips}pips "
            f"ATR={atr:.5f} SL_mult={self.sl_atr_multiplier}"
        )

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            take_profit_3=take_profit_3,
            rationale=rationale,
        )
