"""ScalperStrategy — TTC-based VWAP rejection scalper for the Ayumi backtest engine.

Rules (TTC Scalping from TBD Forex Adaptation):
  Timeframe:     M5/M15
  Sessions:      Any active kill zone (Asia/London/NY/overlap)
  Instruments:   EURUSD, USDJPY
  Min R:R:       2:1
  Indicators:    VWAP(15-bar) + 50 EMA + RSI(7)

Entry Logic:
  1. Session filter: Only trade during active kill zones.
  2. Trend filter: 50 EMA on M15 — trade only in direction of EMA slope.
  3. Entry signal: Price rejects off VWAP in direction of EMA trend.
     - Long:  price above 50 EMA, pulls back to VWAP, RSI(7) in 50-70, candle closes above VWAP
     - Short: price below 50 EMA, pulls up to VWAP, RSI(7) in 30-50, candle closes below VWAP
  4. Stop: VWAP ± 0.15% buffer
  5. Target: 2x stop distance
  6. Time limit: Exit at 12PM ET
  7. Max 1 trade per pair per session
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Optional

import numpy as np

from ..engine import MarketState, StrategySignal, TradeDirection
from ..strategy_legacy import ISignalStrategy

logger = logging.getLogger(__name__)

try:
    import pytz

    _ET = pytz.timezone("America/New_York")
except ImportError:
    _ET = None

try:
    from signal_engine import SessionAnalyzer
except ImportError:
    SessionAnalyzer = None


class ScalperStrategy(ISignalStrategy):
    """TTC VWAP rejection scalper for London-NY overlap sessions."""

    VWAP_PERIOD = 15  # bars for VWAP calculation
    EMA_PERIOD = 50  # 50 EMA for trend direction
    RSI_PERIOD = 7  # RSI period
    STOP_VWAP_PCT = 0.0025  # 0.25% from VWAP for stop loss
    RR_RATIO = 2.0  # minimum risk-reward
    BASE_CONFIDENCE = 0.35
    MIN_HISTORY_BARS = 55  # need at least 50 bars for EMA

    # Kill zones in UTC (EDT = UTC-4, EST = UTC-5)
    # Asia KZ:    ET 00:00-02:00 = UTC 04:00-06:00 (EDT)
    # London KZ:  ET 08:00-10:00 = UTC 12:00-14:00 (EDT)
    # NY KZ:      ET 13:30-15:00 = UTC 17:30-19:00 (EDT)
    # LON-NY:     ET 08:00-12:00 = UTC 12:00-16:00 (EDT)
    KILL_ZONES = [
        (time(4, 0), time(6, 0)),  # Asia KZ
        (time(12, 0), time(14, 0)),  # London KZ
        (time(17, 30), time(19, 0)),  # NY KZ
        (time(12, 0), time(16, 0)),  # London-NY overlap
    ]

    def __init__(self, symbol: str = "EURUSD", timeframe: str = "M15"):
        self.symbol = symbol
        self.timeframe = timeframe
        self._session_analyzer = SessionAnalyzer() if SessionAnalyzer else None

        # Mutable state
        self._last_signal_bar: int = -999
        self._ema_values: np.ndarray | None = None
        self._prev_close_above_vwap: bool | None = None  # for cross detection
        self._prev_close_below_vwap: bool | None = None

    @property
    def name(self) -> str:
        return f"Scalper {self.symbol}"

    def initialize(self, config: dict | None = None) -> None:
        """Initialize the scalper strategy."""
        super().initialize(config)
        logger.info(
            "ScalperStrategy initialized: symbol=%s timeframe=%s",
            self.symbol,
            self.timeframe,
        )

    def reset(self) -> None:
        self._last_signal_bar = -999

        self._ema_values = None
        self._prev_close_above_vwap = None
        self._prev_close_below_vwap = None

    def shutdown(self) -> None:
        """Clean up after scalper run."""
        logger.info(
            "ScalperStrategy shutdown: %s (processed %d bars)",
            self.symbol,
            self._bars_processed,
        )
        super().shutdown()

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        bars = state.bars
        if len(bars) < self.MIN_HISTORY_BARS:
            return None

        latest = bars[-1]
        bar_idx = len(bars) - 1

        # ── 1. Session filter: Any active kill zone ──
        if not self._in_kill_zone(latest.time):
            return None

        # Cooldown: at least 1 bar between signals
        if bar_idx - self._last_signal_bar < 1:
            return None

        # ── Compute indicators ──
        closes = np.array([b.close for b in bars])

        # VWAP (simple 15-bar average as proxy)
        vwap_window = closes[-self.VWAP_PERIOD :]
        vwap = float(np.mean(vwap_window))

        # 50 EMA
        ema = self._ema(closes, self.EMA_PERIOD)
        current_ema = ema[-1]
        if np.isnan(current_ema):
            return None

        # EMA slope direction (compare current vs 5 bars ago)
        slope_lookback = 5
        if len(ema) < slope_lookback + 1:
            return None
        prev_ema = ema[-(slope_lookback + 1)]
        if np.isnan(prev_ema):
            return None
        ema_slope_up = current_ema > prev_ema
        ema_slope_down = current_ema < prev_ema

        # RSI(7)
        rsi = self._rsi(closes, self.RSI_PERIOD)
        current_rsi = rsi[-1]
        if np.isnan(current_rsi):
            return None

        # ── 2. Trend filter: trade only in direction of 50 EMA slope ──
        # ── 3. Entry signal: VWAP rejection in trend direction ──
        # Any VWAP touch — no momentum threshold required
        close_above_vwap = latest.close > vwap
        close_below_vwap = latest.close < vwap

        direction: Optional[str] = None

        if ema_slope_up and latest.close > current_ema:
            # Trend is up — look for long VWAP rejection
            # Price was above VWAP, pulled back to VWAP area, RSI(7) 50-70,
            # current candle closes above VWAP
            if (
                self._prev_close_above_vwap is not None
                and self._prev_close_above_vwap
                and not close_above_vwap
                and close_above_vwap  # recovered — close above VWAP
                and 50 < current_rsi < 70
            ):
                direction = "long"
            # Simpler: just require close above VWAP with RSI confirmation
            elif close_above_vwap and 50 < current_rsi < 70:
                # Check if price recently touched VWAP (within 3 bars)
                recent_touched_vwap = False
                for b in bars[-4:-1]:
                    if abs(b.close - vwap) / vwap < 0.0003:  # within ~3 pips
                        recent_touched_vwap = True
                        break
                    if (b.low <= vwap * 1.0003) and (b.close >= vwap):
                        recent_touched_vwap = True
                        break
                if recent_touched_vwap:
                    direction = "long"

        elif ema_slope_down and latest.close < current_ema:
            # Trend is down — look for short VWAP rejection
            if (
                self._prev_close_below_vwap is not None
                and self._prev_close_below_vwap
                and not close_below_vwap
                and close_below_vwap
                and 30 < current_rsi < 50
            ):
                direction = "short"
            elif close_below_vwap and 30 < current_rsi < 50:
                recent_touched_vwap = False
                for b in bars[-4:-1]:
                    if abs(b.close - vwap) / vwap < 0.0003:
                        recent_touched_vwap = True
                        break
                    if (b.high >= vwap * 0.9997) and (b.close <= vwap):
                        recent_touched_vwap = True
                        break
                if recent_touched_vwap:
                    direction = "short"

        # Update cross tracking state
        self._prev_close_above_vwap = close_above_vwap
        self._prev_close_below_vwap = close_below_vwap

        if direction is None:
            return None

        # ── 4. Stop: VWAP ± 0.15% buffer ──
        stop_distance = vwap * self.STOP_VWAP_PCT
        entry = latest.close

        if direction == "long":
            stop_loss = vwap - stop_distance
            take_profit = entry + stop_distance * self.RR_RATIO
            trade_dir = TradeDirection.LONG
        else:
            stop_loss = vwap + stop_distance
            take_profit = entry - stop_distance * self.RR_RATIO
            trade_dir = TradeDirection.SHORT

        # Sanity check SL/TP placement
        if direction == "long":
            if stop_loss >= entry or take_profit <= entry:
                return None
        else:
            if stop_loss <= entry or take_profit >= entry:
                return None

        # ── Mark state ──
        self._last_signal_bar = bar_idx
        rationale = (
            f"TTC Scalper {direction} @ {latest.close:.5f}, "
            f"VWAP={vwap:.5f}, EMA50={current_ema:.5f}, "
            f"EMA_slope={'up' if ema_slope_up else 'down'}, "
            f"RSI(7)={current_rsi:.1f}"
        )

        return StrategySignal(
            direction=trade_dir,
            confidence=self.BASE_CONFIDENCE,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit_1=take_profit,
            take_profit_2=take_profit,
            take_profit_3=take_profit,
            rationale=rationale,
        )

    def _in_kill_zone(self, utc_dt: datetime) -> bool:
        """Check if time is within any active kill zone (ET-based)."""
        if _ET is not None:
            if utc_dt.tzinfo is None:
                try:
                    utc_dt = pytz.utc.localize(utc_dt)
                except Exception:
                    logger.warning("Cannot localize datetime for kill zone check: %s", utc_dt)
                    return False
            et = utc_dt.astimezone(_ET)
            et_time = et.time()
            # Kill zones in ET
            zones = [
                (time(0, 0), time(2, 0)),  # Asia KZ
                (time(8, 0), time(10, 0)),  # London KZ
                (time(13, 30), time(15, 0)),  # NY KZ
                (time(8, 0), time(12, 0)),  # London-NY overlap
            ]
            for start, end in zones:
                if start <= et_time < end:
                    return True
            return False

        # Fallback: use UTC approximations (EDT = UTC-4)
        t = utc_dt.time()
        for start, end in self.KILL_ZONES:
            if start <= t < end:
                return True
        return False

    def _get_et_date(self, utc_dt: datetime) -> str:
        """Get the ET date string for session tracking."""
        if _ET is not None:
            if utc_dt.tzinfo is None:
                try:
                    utc_dt = pytz.utc.localize(utc_dt)
                except Exception:
                    logger.warning("Cannot localize datetime for ET date: %s", utc_dt)
                    return str(utc_dt.date())
            et = utc_dt.astimezone(_ET)
            return str(et.date())
        return str(utc_dt.date())

    @staticmethod
    def _ema(data: np.ndarray, period: int) -> np.ndarray:
        """Compute EMA."""
        if len(data) < period:
            return np.full_like(data, np.nan)
        alpha = 2.0 / (period + 1)
        ema = np.copy(data)
        ema[:period] = np.nan
        ema[period] = np.mean(data[: period + 1])
        for i in range(period + 1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]
        return ema

    @staticmethod
    def _rsi(data: np.ndarray, period: int = 7) -> np.ndarray:
        """Compute RSI with given period."""
        rsi = np.full_like(data, np.nan, dtype=float)
        if len(data) < period + 1:
            return rsi
        delta = np.diff(data)
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.mean(gain[:period])
        avg_loss = np.mean(loss[:period])
        if avg_loss == 0:
            rsi[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[period] = 100.0 - (100.0 / (1.0 + rs))
        for i in range(period, len(delta)):
            avg_gain = (avg_gain * (period - 1) + gain[i]) / period
            avg_loss = (avg_loss * (period - 1) + loss[i]) / period
            if avg_loss == 0:
                rsi[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
        return rsi
