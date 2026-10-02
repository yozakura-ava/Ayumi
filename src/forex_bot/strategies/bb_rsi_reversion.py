"""BB+RSI Mean Reversion strategy.

.. deprecated:: 2026-07-13
    Per research §A.6 (strategy-optimization-research.md), this strategy has
    PF < 0.3 across all symbols/timeframes. Root causes:
    1. Confidence formula is *inverse* to mean-reversion logic (higher RSI
       distance = higher confidence, but extreme RSI in trend = continuation).
    2. TP at BB middle is too tight — win/loss asymmetry can't exceed 0.5.
    3. require_low_volatility filter excludes the conditions where mean
       reversion actually works (post-spike conditions).

    Replacement: Dual-timeframe Squeeze Pro (dual_df_squeeze_pro.py) per §B.2.
    This file is kept for reference but should not be registered in new sweeps.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import List

from backtest.strategies.isignal_strategy import ISignalStrategy
from core.types import (
    Bar,
    MarketState,
    StrategySignal,
    TradeDirection,
)

# Module-level deprecation flag
DEPRECATED = True


@dataclass(frozen=True)
class BBRSIConfig:
    bb_period: int = 20
    bb_std_dev: float = 2.0
    rsi_period: int = 14
    rsi_long_level: float = 30.0
    rsi_short_level: float = 70.0
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    tp1_rr: float = 1.0
    tp2_rr: float = 1.5
    ema_trend_period: int = 50
    adx_period: int = 14
    adx_max_threshold: float = 25.0
    pip_value: float | None = None
    require_low_volatility: bool = True
    atr_sma_period: int = 20


_DEFAULT_PIP = 0.0001


def _pip_for(price: float) -> float:
    if price >= 50:
        return 0.01
    return _DEFAULT_PIP


def _sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def _std(values: List[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    subset = values[-period:]
    mean = sum(subset) / period
    variance = sum((v - mean) ** 2 for v in subset) / period
    return variance**0.5


def _atr(bars: List[Bar], period: int = 14) -> float:
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


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0.0)
        losses.append(-diff if diff < 0 else 0.0)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger_bands(closes: List[float], period: int, std_dev: float) -> tuple[float, float, float]:
    sma = _sma(closes, period)
    std = _std(closes, period)
    upper = sma + std * std_dev
    lower = sma - std * std_dev
    return upper, sma, lower


def _ema(values: List[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    multiplier = 2.0 / (period + 1)
    ema_val = _sma(values[:period], period)
    for v in values[period:]:
        ema_val = (v - ema_val) * multiplier + ema_val
    return ema_val


def _adx(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period * 2 + 1:
        return 0.0
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    trs: list[float] = []

    for i in range(1, len(bars)):
        high = bars[i].high
        low = bars[i].low
        prev_high = bars[i - 1].high
        prev_low = bars[i - 1].low
        prev_close = bars[i - 1].close

        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

        plus_dm = high - prev_high
        minus_dm = prev_low - low
        if plus_dm < 0:
            plus_dm = 0.0
        if minus_dm < 0:
            minus_dm = 0.0
        if plus_dm > minus_dm:
            minus_dm = 0.0
        else:
            plus_dm = 0.0

        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    if len(trs) < period:
        return 0.0

    smoothed_tr = sum(trs[:period])
    smoothed_plus = sum(plus_dms[:period])
    smoothed_minus = sum(minus_dms[:period])

    dx_values: list[float] = []
    for i in range(period, len(trs)):
        smoothed_tr = smoothed_tr - smoothed_tr / period + trs[i]
        smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dms[i]
        smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dms[i]

        if smoothed_tr == 0:
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx = 100.0 * abs(plus_di - minus_di) / di_sum
        dx_values.append(dx)

    if len(dx_values) < period:
        return 0.0
    adx_val = sum(dx_values[:period]) / period
    for v in dx_values[period:]:
        adx_val = (adx_val * (period - 1) + v) / period
    return adx_val


def _is_low_volatility(bars: List[Bar], atr_period: int, atr_sma_period: int) -> bool:
    if len(bars) < atr_period + atr_sma_period + 1:
        return False
    atr_values: list[float] = []
    for i in range(atr_period, len(bars)):
        atr_values.append(_atr(bars[: i + 1], atr_period))
    if len(atr_values) < atr_sma_period:
        return False
    current_atr = atr_values[-1]
    atr_sma = _sma(atr_values, atr_sma_period)
    return current_atr < atr_sma


def _is_trading_session(bar_time) -> bool:
    h = bar_time.hour
    return 7 <= h < 21


class BBRSIMeanReversion(ISignalStrategy):
    """Deprecated. See module docstring for details."""

    def __init__(self, config: BBRSIConfig | None = None):
        warnings.warn(
            "BBRSIMeanReversion is deprecated (PF < 0.3 across all symbols). "
            "Use Dual-timeframe Squeeze Pro instead. "
            "See docs/research/strategy-optimization-research.md §A.6.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.config = config or BBRSIConfig()

    @property
    def name(self) -> str:
        return "BB+RSI Mean Reversion"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        cfg = self.config
        min_required = max(
            cfg.bb_period + 5,
            cfg.rsi_period + 5,
            cfg.atr_period + 5,
            cfg.ema_trend_period + 1,
            cfg.adx_period * 2 + 1,
        )
        if len(state.bars) < min_required:
            return None

        latest = state.latest_bar
        if not _is_trading_session(latest.time):
            return None

        bars = state.bars
        closes = [b.close for b in bars]

        bb_upper, bb_middle, bb_lower = _bollinger_bands(closes, cfg.bb_period, cfg.bb_std_dev)
        rsi = _rsi(closes, cfg.rsi_period)
        atr = _atr(bars, cfg.atr_period)
        adx = _adx(bars, cfg.adx_period)
        ema = _ema(closes, cfg.ema_trend_period)  # noqa: F841 — strategy live path; dead indicator preserved

        if adx > cfg.adx_max_threshold:
            return None

        if cfg.require_low_volatility:
            if not _is_low_volatility(bars, cfg.atr_period, cfg.atr_sma_period):
                return None

        pip = cfg.pip_value or _pip_for(latest.close)
        bb_width_pips = (bb_upper - bb_lower) / pip  # noqa: F841 — strategy live path; dead indicator preserved

        direction = None
        rationale = ""

        long_condition = latest.low <= bb_lower and rsi < cfg.rsi_long_level

        short_condition = latest.high >= bb_upper and rsi > cfg.rsi_short_level

        if long_condition:
            direction = TradeDirection.LONG
            rationale = (
                f"BB+RSI long: low={latest.low:.5f} <= lower_BB={bb_lower:.5f}, "
                f"RSI={rsi:.1f}<{cfg.rsi_long_level}, ADX={adx:.1f}<{cfg.adx_max_threshold}"
            )
        elif short_condition:
            direction = TradeDirection.SHORT
            rationale = (
                f"BB+RSI short: close={latest.close:.5f} >= upper_BB={bb_upper:.5f}, "
                f"RSI={rsi:.1f}>{cfg.rsi_short_level}, ADX={adx:.1f}<{cfg.adx_max_threshold}"
            )

        if direction is None:
            return None

        risk = atr * cfg.atr_sl_multiplier
        if risk <= 0:
            return None

        if direction == TradeDirection.LONG:
            sl = latest.close - risk
            tp1 = latest.close + risk * cfg.tp1_rr
            tp2 = latest.close + risk * cfg.tp2_rr
        else:
            sl = latest.close + risk
            tp1 = latest.close - risk * cfg.tp1_rr
            tp2 = latest.close - risk * cfg.tp2_rr

        rsi_distance = 0.0
        if direction == TradeDirection.LONG:
            rsi_distance = cfg.rsi_long_level - rsi
        else:
            rsi_distance = rsi - cfg.rsi_short_level

        # Confidence peaks at moderate RSI distance (5–15) and decreases
        # for both minimal and extreme readings.  Extreme RSI in a low-ADX
        # market is still mean-revertible, but extreme RSI near the ADX
        # ceiling signals trend continuation — we already filter by ADX
        # above, so the penalty here is a secondary safety net.
        if rsi_distance <= 5:
            confidence = 0.50 + rsi_distance / 25.0  # 0.50 → 0.70
        elif rsi_distance <= 15:
            confidence = 0.70  # peak band
        else:
            confidence = 0.70 - min((rsi_distance - 15) / 50.0, 0.20)  # 0.70 → 0.50
        confidence = max(0.40, min(confidence, 0.90))

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=latest.close,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp2,
            rationale=rationale,
        )
