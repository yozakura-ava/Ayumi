"""London Breakout + Retest Strategy (XAUUSD-tuned).

Detects Asian session range, waits for London open breakout,
enters on retest of the broken level within 4 hours.

Best on: XAUUSD M15 (primary), EURUSD M15
"""

from __future__ import annotations

from dataclasses import dataclass

from core.types import (
    Bar,
    MarketState,
    StrategySignal,
    TradeDirection,
)
from utils.pip_value import DEFAULT_PIP, pip_value_for_symbol


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


def _pip_size(symbol_hint: str | None = "") -> float:
    """Symbol-aware pip size via the shared utility.

    Kept as a thin wrapper for backward compat — delegates to
    ``utils.pip_value.pip_value_for_symbol`` so the canonical lookup
    lives in one place. Lenient on empty/None input (returns
    ``DEFAULT_PIP``) to match the historical behavior of this helper
    when no symbol was provided.
    """
    if not symbol_hint:
        # Legacy behavior: empty/None hint → standard forex pip.
        return DEFAULT_PIP
    return pip_value_for_symbol(symbol_hint)


@dataclass(frozen=True)
class LondonBreakoutConfig:
    # Session boundaries (UTC hours)
    asian_start_utc: int = 0
    asian_end_utc: int = 7  # Asian session ends, London opens
    trade_start_utc: int = 7
    trade_end_utc: int = 11  # Last retest window: 4h after London open

    # Range filters
    min_asian_range_pips: float = 8.0
    max_asian_range_pips: float = 60.0
    buffer_pips: float = 3.0  # breakout buffer above/below Asian range

    # ATR / SL
    atr_period: int = 14
    retest_tolerance_atr: float = 1.0

    # Risk
    min_confidence: float = 0.45
    cooldown_bars: int = 20

    # Symbol for pip sizing
    symbol: str = "XAUUSD"


class LondonBreakoutRetestStrategy:
    """London Breakout + Retest: Asian range → London breakout → retest entry."""

    def __init__(self, config: LondonBreakoutConfig | None = None):
        self.config = config or LondonBreakoutConfig()
        self._bars_since_signal: int = 999
        self._asian_high: float | None = None
        self._asian_low: float | None = None
        self._asian_date: int | None = None  # day of month for Asian session
        self._breakout_dir: int | None = None  # 1=long breakout, -1=short breakout
        self._breakout_bar_idx: int = -1

    @property
    def name(self) -> str:
        return "London Breakout Retest"

    def reset(self) -> None:
        self._bars_since_signal = 999
        self._asian_high = None
        self._asian_low = None
        self._asian_date = None
        self._breakout_dir = None
        self._breakout_bar_idx = -1

    def _compute_asian_range(self, bars: list[Bar], target_day: int) -> tuple[float, float] | None:
        """Compute Asian session high/low for the target day."""
        asian_high = None
        asian_low = None

        for b in bars:
            if b.time.day != target_day:
                continue
            hour = b.time.hour
            if self.config.asian_start_utc <= hour < self.config.asian_end_utc:
                if asian_high is None:
                    asian_high = b.high
                    asian_low = b.low
                else:
                    asian_high = max(asian_high, b.high)
                    asian_low = min(asian_low, b.low)

        if asian_high is None or asian_low is None:
            return None
        return (asian_high, asian_low)

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        bars = state.bars
        min_required = max(self.config.atr_period + 1, 50)

        if len(bars) < min_required:
            return None

        # Cooldown
        if self._bars_since_signal < self.config.cooldown_bars:
            self._bars_since_signal += 1
            return None
        self._bars_since_signal += 1

        latest = bars[-1]
        hour = latest.time.hour
        day = latest.time.day

        # Reset for new day
        if self._asian_date != day:
            self._asian_date = day
            self._asian_high = None
            self._asian_low = None
            self._breakout_dir = None
            self._breakout_bar_idx = -1

        # Compute Asian range from accumulated bars
        if self._asian_high is None or self._asian_low is None:
            result = self._compute_asian_range(bars, day)
            if result is None:
                return None
            self._asian_high, self._asian_low = result

        asian_high = self._asian_high
        asian_low = self._asian_low
        pip = _pip_size(self.config.symbol)
        range_pips = (asian_high - asian_low) / pip

        # Validate range
        if range_pips < self.config.min_asian_range_pips:
            return None
        if range_pips > self.config.max_asian_range_pips:
            return None

        buffer = self.config.buffer_pips * pip

        # Only trade during London hours
        if hour < self.config.trade_start_utc or hour >= self.config.trade_end_utc:
            return None

        atr = _calculate_atr(bars, self.config.atr_period)
        if atr <= 0:
            return None

        bar_idx = len(bars) - 1

        # Phase 1: Detect breakout
        if self._breakout_dir is None:
            if latest.close > asian_high + buffer:
                self._breakout_dir = 1  # long breakout
                self._breakout_bar_idx = bar_idx
            elif latest.close < asian_low - buffer:
                self._breakout_dir = -1  # short breakout
                self._breakout_bar_idx = bar_idx
            return None

        # Phase 2: Wait for retest (within 4 hours = 16 M15 bars)
        bars_since_breakout = bar_idx - self._breakout_bar_idx
        if bars_since_breakout > 16:
            # Expired — no retest within window
            return None

        direction = None
        signal_type = None

        if self._breakout_dir == 1:  # Long breakout — wait for retest of asian_high
            # Price pulled back near asian_high (within retest_tolerance_atr)
            if latest.low <= asian_high + self.config.retest_tolerance_atr * atr:
                if latest.close > asian_high:
                    # Retested and held above — enter long
                    direction = TradeDirection.LONG
                    signal_type = "retest_long"

        elif self._breakout_dir == -1:  # Short breakout — wait for retest of asian_low
            if latest.high >= asian_low - self.config.retest_tolerance_atr * atr:
                if latest.close < asian_low:
                    direction = TradeDirection.SHORT
                    signal_type = "retest_short"

        if direction is None:
            return None

        # Entry and risk management
        entry = latest.close

        if direction == TradeDirection.LONG:
            stop_loss = asian_low - atr * 0.5  # below Asian low
        else:
            stop_loss = asian_high + atr * 0.5  # above Asian high

        risk = abs(entry - stop_loss)
        if risk <= 0:
            return None

        tp1 = entry + risk * 1.0 if direction == TradeDirection.LONG else entry - risk * 1.0
        tp2 = entry + risk * 2.0 if direction == TradeDirection.LONG else entry - risk * 2.0
        tp3 = entry + risk * 3.0 if direction == TradeDirection.LONG else entry - risk * 3.0

        # Confidence based on range size and breakout direction alignment
        confidence = self.config.min_confidence
        range_atr_ratio = (asian_high - asian_low) / atr
        confidence += min(range_atr_ratio * 0.03, 0.15)  # tighter range = cleaner setup
        confidence = min(confidence, 0.80)

        rationale = (
            f"London {signal_type}: Asian range={asian_low:.5f}-{asian_high:.5f} "
            f"({range_pips:.0f}p), retest at bar +{bars_since_breakout}, "
            f"ATR={atr:.5f}, entry={entry:.5f}"
        )

        self._bars_since_signal = 0
        # Reset breakout to prevent multiple entries on same day
        self._breakout_dir = None

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
