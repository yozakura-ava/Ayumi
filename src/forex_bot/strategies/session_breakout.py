"""Session Breakout Strategy — trades breakouts of defined Asian/London/NY ranges."""

from __future__ import annotations

from datetime import datetime, timezone

from core.types import Bar, MarketState, StrategySignal, TradeDirection
from utils.pip_value import DEFAULT_PIP, pip_value_for_symbol

# ── Price / range sanity guards ────────────────────────────────────────────
# These prevent corrupted signals (e.g. from upstream unit mismatches) from
# being emitted. A range width of 4400 pips or a TP at 0.66 for a 1.32 entry
# are clear signatures of data corruption, not legitimate market conditions.

_FX_MIN_PRICE = 0.01  # Below any legitimate forex instrument
_FX_MAX_PRICE = 500.0  # Above any legitimate forex instrument (covers XAUUSD, high JPY crosses)
_MAX_RANGE_PIPS = 500  # Max 500 pips range width (catches unit-mismatch bugs)
_MAX_TP_DISTANCE_PIPS = 1000  # Max 1000 pips from entry to TP


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


def _pip_size_for_symbol(symbol: str) -> float:
    """Return pip size based on the trading symbol.

    Uses the canonical ``utils.pip_value`` lookup so that XAUUSD (gold)
    correctly returns 0.1 instead of being mis-classified as JPY by the
    old price-based heuristic.
    """
    try:
        return pip_value_for_symbol(symbol)
    except (ValueError, TypeError):
        return DEFAULT_PIP


def _ema(values: list[float], period: int) -> float | None:
    """Calculate EMA from a list of values. Returns None if insufficient data."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    # Start with SMA of first `period` values
    ema = sum(values[:period]) / period
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema


class SessionBreakoutStrategy:
    """Trades breakouts above/below a defined session range.

    Config keys:
        name: Display name
        range_start_hour / range_end_hour: UTC hours defining the range window
        trade_start_hour / trade_end_hour: UTC hours defining the trade window
        min_range_pips / max_range_pips: valid range width bounds
        buffer_pips: extra buffer beyond range high/low for breakout confirmation
        sl_atr_multiplier: ATR multiplier for stop-loss distance
        atr_period: lookback for ATR calculation
        min_range_bars: minimum bars required in range window
        time_exit_hours: (reserved) hours after entry to force exit
        h4_trend_filter: if True, only trade in direction of H4 50 EMA
    """

    def __init__(self, config: dict):
        self._name = config["name"]
        self.range_start_hour: int = config["range_start_hour"]
        self.range_end_hour: int = config["range_end_hour"]
        self.trade_start_hour: int = config["trade_start_hour"]
        self.trade_end_hour: int = config["trade_end_hour"]
        self.min_range_pips: float = config.get("min_range_pips", 20)
        self.max_range_pips: float = config.get("max_range_pips", 80)
        self.buffer_pips: float = config.get("buffer_pips", 3)
        self.sl_atr_multiplier: float = config.get("sl_atr_multiplier", 2.0)
        self.atr_period: int = config.get("atr_period", 14)
        self.min_range_bars: int = config.get("min_range_bars", 20)
        self.time_exit_hours: int = config.get("time_exit_hours", 2)
        self.h4_trend_filter: bool = config.get("h4_trend_filter", False)

        # Cache: {(date_string, symbol): {"high": float, "low": float, "valid": bool}}
        self._range_cache: dict[tuple[str, str], dict] = {}

        # Fired signals: {(date_string, symbol, direction): True}
        self._fired_signals: dict[tuple[str, str, str], bool] = {}

    @property
    def name(self) -> str:
        return self._name

    # ── Range helpers ──────────────────────────────────────────────────────

    def _bar_in_range_window(self, bar_time: datetime) -> bool:
        """Check if bar falls within the range definition window (UTC).

        Handles midnight wrap: e.g. range_start=21, range_end=0 means
        hours 21, 22, 23 are in range (not hour 0 — that's trade window).
        For non-wrapping: start <= hour < end.
        """
        hour = bar_time.hour
        start = self.range_start_hour
        end = self.range_end_hour

        if start < end:
            # Normal window (e.g. 0–8, 8–13)
            return start <= hour < end
        else:
            # Wrapping window (e.g. 21–0 means hours 21, 22, 23)
            return hour >= start or hour < end

    def _compute_range(self, bars: list[Bar], date_str: str, symbol: str) -> dict | None:
        """Compute session range from bars in the range window. Returns None on failure."""
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
        """Resample M15 bars into ~H4 bars and compute 50 EMA.

        Returns LONG if price > EMA, SHORT if price < EMA, None if insufficient data.
        """
        if len(bars) < 200:
            return None

        # Resample last 200 M15 bars into ~50 H4 candles (groups of 16 M15 bars = 4h)
        chunk_size = 16
        h4_closes: list[float] = []
        for i in range(0, len(bars), chunk_size):
            chunk = bars[i : i + chunk_size]
            if len(chunk) < 4:  # skip tiny chunks
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
        if len(state.bars) < self.min_range_bars + 10:
            return None

        latest = state.latest_bar
        bar_time = latest.time

        # Ensure UTC
        if bar_time.tzinfo is not None:
            bar_time = bar_time.astimezone(timezone.utc)

        bar_hour = bar_time.hour
        date_str = bar_time.strftime("%Y-%m-%d")
        symbol = getattr(state, "symbol", "")

        # 1. Check if we're in the trade window
        if not (self.trade_start_hour <= bar_hour < self.trade_end_hour):
            return None

        # 2. Compute / retrieve cached range
        range_data = self._compute_range(state.bars, date_str, symbol)
        if range_data is None or not range_data["valid"]:
            return None

        range_high = range_data["high"]
        range_low = range_data["low"]
        pip = _pip_size_for_symbol(symbol)
        buffer_price = self.buffer_pips * pip

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

        # 5. One signal per direction per session per day
        fired_key = (date_str, symbol, direction.value)
        if fired_key in self._fired_signals:
            return None

        # 6. Optional H4 trend filter
        if self.h4_trend_filter:
            h4_dir = self._h4_trend_direction(state.bars)
            if h4_dir is not None and h4_dir != direction:
                return None

        # 7. Calculate ATR for stop-loss
        atr = _calculate_atr(state.bars, self.atr_period)
        if atr <= 0:
            return None

        # 8. Calculate levels
        range_width = range_high - range_low
        entry = latest.close
        sl_distance = atr * self.sl_atr_multiplier

        if direction == TradeDirection.LONG:
            stop_loss = entry - sl_distance
            take_profit_1 = entry + 1.5 * range_width
            take_profit_2 = entry + 2.0 * range_width
            take_profit_3 = entry + 3.0 * range_width
        else:
            stop_loss = entry + sl_distance
            take_profit_1 = entry - 1.5 * range_width
            take_profit_2 = entry - 2.0 * range_width
            take_profit_3 = entry - 3.0 * range_width

        # 9. Price sanity guards (reject corrupted signals from unit mismatches)
        range_width_pips = range_width / pip
        if range_width_pips > _MAX_RANGE_PIPS:
            return None

        for tp in (take_profit_1, take_profit_2, take_profit_3):
            if tp < _FX_MIN_PRICE or tp > _FX_MAX_PRICE:
                return None

        tp_distance_pips = abs(take_profit_3 - entry) / pip
        if tp_distance_pips > _MAX_TP_DISTANCE_PIPS:
            return None

        # 10. Confidence based on breakout strength
        if bullish_breakout:
            penetration = (latest.close - range_high - buffer_price) / atr
        else:
            penetration = (range_low - buffer_price - latest.close) / atr
        confidence = min(0.90, 0.55 + min(penetration, 1.0) * 0.35)

        # 11. Mark as fired
        self._fired_signals[fired_key] = True

        direction_word = "bullish" if bullish_breakout else "bearish"
        rationale = (
            f"Session Breakout {direction_word}: close={latest.close:.5f} "
            f"breaks range [{range_low:.5f}-{range_high:.5f}] "
            f"width={range_data['width_pips']:.1f}pips "
            f"buffer={self.buffer_pips}pips"
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
