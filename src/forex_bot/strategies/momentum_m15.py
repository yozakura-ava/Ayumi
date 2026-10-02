"""EURUSD M15 Momentum Strategy — breakout detection with trend-following confirmation.

Combines N-bar range breakouts with EMA trend filtering and ADX strength
gating.  Designed for FTMO compliance: 0.5% per-signal risk cap and
configurable daily drawdown circuit-breaker.

Card: AYUAA-278
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.types import (
    Bar,
    MarketState,
    SessionType,
    StrategySignal,
    TradeDirection,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PREFERRED_SESSIONS: set[SessionType] = {
    SessionType.LONDON,
    SessionType.NY_AM,
}


@dataclass(frozen=True)
class MomentumM15Config:
    """Configuration for MomentumM15Strategy."""

    # Range / breakout detection
    lookback_period: int = 20  # bars for range high/low

    # Trend-following confirmation
    ema_fast: int = 8
    ema_slow: int = 21
    min_adx: float = 20.0  # ADX trend-strength gate
    adx_period: int = 14

    # Risk management (ATR-based)
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5  # SL distance = ATR * multiplier
    tp_rr: float = 2.0  # TP risk-reward ratio (entry + risk * tp_rr)

    # FTMO compliance
    risk_per_trade: float = 0.005  # 0.5% of balance per signal
    max_daily_dd: float = 0.03  # 3% max daily drawdown

    # Signal quality
    min_confidence: float = 0.50
    session_filter: bool = True

    # Account state (for position sizing)
    account_balance: float = 100_000.0


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def _calculate_atr(bars: list[Bar], period: int = 14) -> float:
    """Average True Range over the last *period* bars."""
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


def _calculate_ema(values: list[float], period: int) -> float:
    """Exponential Moving Average of *values* using standard k = 2/(N+1)."""
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k = 2.0 / (period + 1)
    # Seed with SMA of first *period* values
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _calculate_adx(bars: list[Bar], period: int = 14) -> float:
    """ADX trend-strength indicator (Wilder smoothing)."""
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
        dx_list.append(0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_list) < period:
        return 0.0
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


# ---------------------------------------------------------------------------
# FTMO position sizing
# ---------------------------------------------------------------------------


def calculate_position_size(
    account_balance: float,
    risk_per_trade: float,
    sl_distance_pips: float,
    pip_value_per_lot: float = 10.0,
) -> float:
    """Return lot size so that SL hit costs exactly *risk_per_trade* of balance.

    Parameters
    ----------
    account_balance
        Current account balance in account currency.
    risk_per_trade
        Fraction of balance to risk (e.g. 0.005 = 0.5%).
    sl_distance_pips
        Stop-loss distance in pips.
    pip_value_per_lot
        Dollar value per pip per standard lot (default $10 for EURUSD).

    Returns
    -------
    float
        Position size in lots (clamped to >= 0.01).
    """
    if sl_distance_pips <= 0 or pip_value_per_lot <= 0:
        return 0.01
    risk_amount = account_balance * risk_per_trade
    lots = risk_amount / (sl_distance_pips * pip_value_per_lot)
    return max(0.01, round(lots, 2))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class MomentumM15Strategy:
    """EURUSD M15 breakout + trend-following strategy.

    Detection logic:
    1. Compute N-bar range high/low (excluding current bar).
    2. Confirm trend direction via EMA(fast) vs EMA(slow).
    3. Gate on ADX >= min_adx for trend strength.
    4. If close breaks above range high in a bullish EMA regime → LONG.
       If close breaks below range low in a bearish EMA regime → SHORT.

    Risk:
    - SL = entry ± ATR * atr_sl_multiplier
    - TP = entry ± risk * tp_rr
    - Position size from FTMO 0.5% risk rule
    - Daily drawdown circuit-breaker
    """

    def __init__(self, config: MomentumM15Config | None = None) -> None:
        self.config = config or MomentumM15Config()
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: datetime | None = None
        self._daily_trade_count: int = 0

    @property
    def name(self) -> str:
        return "Momentum M15 (Breakout + Trend)"

    # -- public API --------------------------------------------------------

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        """Evaluate current market state and return a signal or None."""
        cfg = self.config

        # Minimum bar count
        min_bars = max(
            cfg.lookback_period + 2,
            cfg.ema_slow + 2,
            cfg.adx_period + 2,
            cfg.atr_period + 2,
        )
        if len(state.bars) < min_bars:
            return None

        # Session filter
        if cfg.session_filter and state.current_session not in _PREFERRED_SESSIONS:
            return None

        # FTMO daily drawdown circuit-breaker
        if self._is_daily_dd_breached():
            return None

        # Indicators
        atr = state.atr if state.atr > 0 else _calculate_atr(state.bars, cfg.atr_period)
        if atr <= 0:
            return None

        closes = [b.close for b in state.bars]
        ema_fast = _calculate_ema(closes, cfg.ema_fast)
        ema_slow = _calculate_ema(closes, cfg.ema_slow)
        adx = _calculate_adx(state.bars, cfg.adx_period)

        # Range (exclude current bar to avoid look-ahead)
        lookback = state.bars[-(cfg.lookback_period + 1) : -1]
        range_high = max(b.high for b in lookback)
        range_low = min(b.low for b in lookback)
        latest = state.latest_bar

        # Breakout detection
        bullish_breakout = latest.close > range_high
        bearish_breakout = latest.close < range_low

        if not bullish_breakout and not bearish_breakout:
            return None

        # Trend-following confirmation
        trend_up = ema_fast > ema_slow
        trend_down = ema_fast < ema_slow

        if bullish_breakout and not trend_up:
            return None
        if bearish_breakout and not trend_down:
            return None

        # ADX gate
        if adx < cfg.min_adx:
            return None

        # Build signal
        if bullish_breakout:
            direction = TradeDirection.LONG
            entry = latest.close
            risk = atr * cfg.atr_sl_multiplier
            sl = entry - risk
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * cfg.tp_rr
            tp3 = entry + risk * (cfg.tp_rr + 1.0)
            penetration = min((latest.close - range_high) / atr, 1.0)
            confidence = min(
                0.90,
                0.50 + penetration * 0.30 + min((adx - cfg.min_adx) / 30.0, 1.0) * 0.10,
            )
            rationale = (
                f"M15 momentum LONG: close={latest.close:.5f} > "
                f"range_high={range_high:.5f} ({cfg.lookback_period}-bar), "
                f"EMA{cfg.ema_fast}>{cfg.ema_slow}, ADX={adx:.1f}"
            )
        else:
            direction = TradeDirection.SHORT
            entry = latest.close
            risk = atr * cfg.atr_sl_multiplier
            sl = entry + risk
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * cfg.tp_rr
            tp3 = entry - risk * (cfg.tp_rr + 1.0)
            penetration = min((range_low - latest.close) / atr, 1.0)
            confidence = min(
                0.90,
                0.50 + penetration * 0.30 + min((adx - cfg.min_adx) / 30.0, 1.0) * 0.10,
            )
            rationale = (
                f"M15 momentum SHORT: close={latest.close:.5f} < "
                f"range_low={range_low:.5f} ({cfg.lookback_period}-bar), "
                f"EMA{cfg.ema_fast}<{cfg.ema_slow}, ADX={adx:.1f}"
            )

        if confidence < cfg.min_confidence:
            return None

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

    # -- FTMO tracking -----------------------------------------------------

    def record_trade_result(self, pnl: float, trade_time: datetime | None = None) -> None:
        """Record a closed trade's P&L for daily drawdown tracking.

        Should be called after every closed trade to keep the circuit-breaker
        accurate.
        """
        trade_date = (trade_time or datetime.now(timezone.utc)).date()
        if self._daily_pnl_date is None or self._daily_pnl_date.date() != trade_date:
            self._daily_pnl_date = trade_time or datetime.now(timezone.utc)
            self._daily_pnl = 0.0
            self._daily_trade_count = 0
        self._daily_pnl += pnl
        self._daily_trade_count += 1

    def get_position_size(self, sl_distance_pips: float) -> float:
        """Calculate FTMO-compliant position size for the given SL distance."""
        return calculate_position_size(
            self.config.account_balance,
            self.config.risk_per_trade,
            sl_distance_pips,
        )

    def _is_daily_dd_breached(self) -> bool:
        """True if cumulative daily loss exceeds max_daily_dd."""
        if self._daily_pnl_date is None:
            return False
        today = datetime.now(timezone.utc).date()
        if self._daily_pnl_date.date() != today:
            return False  # Reset on new day
        return self._daily_pnl < -(self.config.account_balance * self.config.max_daily_dd)

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trade_count(self) -> int:
        return self._daily_trade_count

    def reset_daily_stats(self) -> None:
        """Force-reset daily P&L tracking (e.g. on new trading day)."""
        self._daily_pnl = 0.0
        self._daily_pnl_date = None
        self._daily_trade_count = 0
