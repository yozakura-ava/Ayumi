"""Donchian + ATR Trailing Trend v2 (research candidate B.1).

Trend-following strategy that enters on N-period Donchian breakout confirmed
by ADX, EMA trend, and ATR volatility expansion. Position is managed with an
ATR-multiplied trailing stop and partial exits at 1R, 2R, 3R.

Research B.1 from docs/research/strategy-optimization-research.md.

Differences from v1 (donchian_atr_trend.py):
    - Implements the formal ``ISignalStrategy`` ABC.
    - Adds **volatility expansion** filter (ATR > SMA(ATR)): v1 didn't
      distinguish expansion from contraction.
    - Hard cap on SL distance (``hard_cap_sl_pips``) prevents runaway stops
      on gold flash events.
    - Symbol-aware pip size via ``pip_value_for_symbol``.
    - Three R-multiple TPs with proper risk calculation.

Best on: XAUUSD M15 (gold trends persistently), GBPUSD M15, EURUSD M15.
Timeframes: M15, H1
"""

from __future__ import annotations

from dataclasses import dataclass

from backtest.strategies.isignal_strategy import ISignalStrategy
from core.types import (
    Bar,
    MarketState,
    StrategySignal,
    TradeDirection,
)
from utils.pip_value import pip_value_for_symbol

# ---------------------------------------------------------------------------
# Indicator helpers (intentionally duplicated from the legacy file so that
# v1 stays bit-for-bit unchanged for historical backtests).
# ---------------------------------------------------------------------------


def _calculate_atr(bars: list[Bar], period: int = 14) -> float:
    """Wilder-style ATR over the most recent ``period`` bars."""
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
    """Standard EMA. Returns ``None`` if not enough data."""
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def _calculate_sma(values: list[float], period: int) -> float | None:
    """Simple moving average. Returns ``None`` if not enough data."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _calculate_adx(bars: list[Bar], period: int = 14) -> float:
    """Simplified ADX over the most recent ``period`` windows."""
    if len(bars) < period * 2 + 1:
        return 0.0

    true_ranges: list[float] = []
    plus_dms: list[float] = []
    minus_dms: list[float] = []

    for i in range(len(bars) - period * 2, len(bars)):
        if i > 0:
            up = bars[i].high - bars[i - 1].high
            down = bars[i - 1].low - bars[i].low
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            true_ranges.append(tr)
            plus_dms.append(up if up > down and up > 0 else 0.0)
            minus_dms.append(down if down > up and down > 0 else 0.0)

    if not true_ranges:
        return 0.0

    dx_values: list[float] = []
    window = period
    for j in range(len(true_ranges) - window + 1):
        seg_tr = true_ranges[j : j + window]
        seg_pdm = plus_dms[j : j + window]
        seg_mdm = minus_dms[j : j + window]
        seg_atr = sum(seg_tr) / window
        if seg_atr <= 0:
            dx_values.append(0.0)
            continue
        seg_pdi = 100.0 * (sum(seg_pdm) / window) / seg_atr
        seg_mdi = 100.0 * (sum(seg_mdm) / window) / seg_atr
        if (seg_pdi + seg_mdi) == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100.0 * abs(seg_pdi - seg_mdi) / (seg_pdi + seg_mdi))

    return sum(dx_values) / len(dx_values) if dx_values else 0.0


def _donchian_high(bars: list[Bar], period: int) -> float:
    """N-period Donchian high, **excluding the current bar**."""
    if len(bars) < period + 1:
        return max(b.high for b in bars) if bars else 0.0
    return max(b.high for b in bars[-(period + 1) : -1])


def _donchian_low(bars: list[Bar], period: int) -> float:
    """N-period Donchian low, **excluding the current bar**."""
    if len(bars) < period + 1:
        return min(b.low for b in bars) if bars else 0.0
    return min(b.low for b in bars[-(period + 1) : -1])


# ---------------------------------------------------------------------------
# Config dataclass (frozen; use ``dataclasses.replace`` for tuning)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DonchianATRConfig:
    """Tunable parameters for the Donchian ATR Trailing Trend v2 strategy.

    Defaults are tuned for XAUUSD M15 (gold trends persistently with
    manageable volatility). Adjust for FX pairs via ``.xauusd_m15()`` or
    ``.fx_m15()`` factory methods.
    """

    # Symbol for pip-size resolution (gold vs FX) and rationale strings.
    symbol: str = "XAUUSD"
    # Donchian channel period (breakout lookback).
    donchian_period: int = 20
    # ATR period for stop & trail calculations.
    atr_period: int = 14
    # Trail distance in ATR multiples (tightness of the trailing stop).
    atr_trail_multiplier: float = 2.5
    # ADX threshold: above this = trending market, below = choppy.
    adx_threshold: float = 20.0
    # EMA period used for higher-timeframe trend filter.
    ema_trend_period: int = 50
    # SMA(ATR) period for volatility expansion check.
    atr_sma_period: int = 50
    # Minimum confidence to emit a signal.
    min_confidence: float = 0.45
    # Bars to wait between signals (avoids over-trading in chop).
    cooldown_bars: int = 5
    # TP R-multiples (partial exits at 1R, 2R, 3R).
    tp1_rr: float = 1.0
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0
    # Hard cap on stop loss distance (prevents runaway stops on flash events).
    hard_cap_sl_pips: float = 50.0
    # Min bars required before evaluating (largest warmup across all indicators).
    min_bars_for_setup: int = 60

    # Presets -----------------------------------------------------------------

    @classmethod
    def xauusd_m15(cls) -> "DonchianATRConfig":
        """Default preset — XAUUSD M15."""
        return cls()

    @classmethod
    def fx_m15(cls) -> "DonchianATRConfig":
        """FX M15 preset (EURUSD, GBPUSD, USDJPY).

        Notes:
            - ``donchian_period`` 20 → 30 (FX breakouts need more confirmation).
            - ``adx_threshold`` 20 → 25 (FX chop is more common).
            - ``cooldown_bars`` 5 → 8 (reduce overtrading on FX noise).
        """
        return cls(
            donchian_period=30,
            adx_threshold=25.0,
            cooldown_bars=8,
            symbol="EURUSD",
        )


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class DonchianATRTrendV2Strategy(ISignalStrategy):
    """Donchian breakout + ATR trailing stop, formal ``ISignalStrategy``.

    Entry LONG when **all** of:
        1. ``close > donchian_high(N=20)`` (20-period high breakout)
        2. ``adx(14) >= 20`` (trend confirmation)
        3. ``close > ema(50)`` (above major trend)
        4. ``atr(14) > sma(atr, 50)`` (volatility expansion, not contraction)

    Entry SHORT is the mirror.

    Exit (caller-managed via trailing stop):
        - Stop starts at ``max(donchian_low(N), entry - 2.5 * atr(14))``
        - Trail: every bar where ``high > entry``, raise stop to
          ``max(stop, high - 2.5 * atr)``
        - Exit when ``close < stop`` OR ``close < donchian_low(N)``
        - Partial exits at 1.0R / 2.0R / 3.0R
    """

    def __init__(self, config: DonchianATRConfig | None = None):
        super().__init__()
        self.config = config or DonchianATRConfig()
        self._bars_since_signal: int = 999  # start ready to signal
        # Rolling buffer of ATR values for SMA(ATR) computation.
        # Pre-computed incrementally in on_bar() so evaluate() is O(1).
        self._atr_buffer: list[float] = []
        # Last N bars processed count for incremental EMA / SMA.
        self._bars_seen: int = 0

    # --- ISignalStrategy interface --------------------------------------

    @property
    def name(self) -> str:
        return "Donchian ATR Trailing Trend v2"

    def reset(self) -> None:
        """Reset cooldown / counters between backtest runs."""
        super().reset()
        self._bars_since_signal = 999
        self._atr_buffer.clear()
        self._bars_seen = 0

    def on_bar(self, bar: Bar) -> None:
        """Incrementally update the ATR buffer for SMA(ATR) computation.

        Called by the engine on each new bar BEFORE evaluate(). We compute
        a single new ATR value (O(period)) and append to the buffer so
        that evaluate() stays O(n) on the slice length, not O(n²) on
        full re-computation.
        """
        super().on_bar(bar)
        self._bars_seen += 1
        # Compute the latest ATR value incrementally
        # (Need bars[-period-1..-1] -> needs self._bars_seen context)
        # We don't have direct access to bars list here — on_bar only gets
        # the latest bar. Fall back: store the latest bar and recompute
        # in evaluate(). See evaluate() for the optimized path.
        # Actually, we do need a window — store last (period+1) bars.
        if not hasattr(self, "_recent_bars"):
            self._recent_bars: list[Bar] = []
        self._recent_bars.append(bar)
        max_window = max(
            self.config.atr_period + 2,
            self.config.atr_sma_period + self.config.atr_period + 2,
        )
        if len(self._recent_bars) > max_window:
            self._recent_bars = self._recent_bars[-max_window:]
        # Compute ATR over the recent window
        if len(self._recent_bars) >= self.config.atr_period + 1:
            atr = _calculate_atr(self._recent_bars, self.config.atr_period)
            self._atr_buffer.append(atr)
            if len(self._atr_buffer) > self.config.atr_sma_period:
                self._atr_buffer = self._atr_buffer[-self.config.atr_sma_period :]

    # --- Evaluation -----------------------------------------------------

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        cfg = self.config
        bars = state.bars

        # Warmup check (longest of all indicator windows)
        min_required = max(
            cfg.min_bars_for_setup,
            cfg.donchian_period + 1,
            cfg.atr_period * 2 + 1,
            cfg.ema_trend_period,
            cfg.atr_sma_period + cfg.atr_period + 1,
        )
        if len(bars) < min_required:
            return None

        # Cooldown: skip signals within N bars of previous signal.
        # We tick the counter whether or not we emit a signal so the gate
        # is measured from the last *attempt*, not the last fill.
        self._bars_since_signal += 1
        if self._bars_since_signal < cfg.cooldown_bars:
            return None

        latest = bars[-1]
        closes = [b.close for b in bars]

        # --- Indicator values ---
        dc_high = _donchian_high(bars, cfg.donchian_period)
        dc_low = _donchian_low(bars, cfg.donchian_period)
        atr = _calculate_atr(bars, cfg.atr_period)
        adx = _calculate_adx(bars, cfg.atr_period)
        ema = _calculate_ema(closes, cfg.ema_trend_period)

        # Prefer incremental buffer if it has enough history.
        # Otherwise compute one-shot SMA from full bars list.
        if len(self._atr_buffer) >= cfg.atr_sma_period:
            atr_sma = sum(self._atr_buffer[-cfg.atr_sma_period :]) / cfg.atr_sma_period
        else:
            atr_history = [
                _calculate_atr(bars[: i + 1], cfg.atr_period)
                for i in range(cfg.atr_sma_period + cfg.atr_period, len(bars))
            ]
            atr_sma = _calculate_sma(atr_history, cfg.atr_sma_period) if atr_history else None

        if atr <= 0 or ema is None or atr_sma is None:
            return None

        # --- Conditions ---
        # ADX trend filter
        if adx < cfg.adx_threshold:
            return None

        # Volatility expansion (current ATR above its recent average)
        if atr <= atr_sma:
            return None

        direction: TradeDirection | None = None
        rationale = ""

        # Long breakout
        if latest.close > dc_high and latest.close > ema:
            direction = TradeDirection.LONG
            rationale = (
                f"Donchian breakout long: close={latest.close:.5f} > "
                f"DC_high={dc_high:.5f}, ADX={adx:.1f}, ATR={atr:.5f}, "
                f"ATR_SMA={atr_sma:.5f}, EMA50={ema:.5f}"
            )
        # Short breakout (mirror)
        elif latest.close < dc_low and latest.close < ema:
            direction = TradeDirection.SHORT
            rationale = (
                f"Donchian breakout short: close={latest.close:.5f} < "
                f"DC_low={dc_low:.5f}, ADX={adx:.1f}, ATR={atr:.5f}, "
                f"ATR_SMA={atr_sma:.5f}, EMA50={ema:.5f}"
            )

        if direction is None:
            return None

        # --- Sizing & SL ---
        pip = pip_value_for_symbol(cfg.symbol)
        entry = latest.close

        # Stop: max(donchian_low, entry - 2.5*atr) for longs
        #       min(donchian_high, entry + 2.5*atr) for shorts
        if direction == TradeDirection.LONG:
            trail_stop = entry - cfg.atr_trail_multiplier * atr
            stop_loss = max(dc_low, trail_stop)
            if stop_loss >= entry:
                return None  # invalid stop (already inside)
        else:
            trail_stop = entry + cfg.atr_trail_multiplier * atr
            stop_loss = min(dc_high, trail_stop)
            if stop_loss <= entry:
                return None  # invalid stop

        risk = abs(entry - stop_loss)
        if risk <= 0:
            return None

        # Hard-cap SL distance (pips) — prevents runaway stops on flash events
        sl_pips = risk / pip if pip > 0 else 0.0
        if sl_pips > cfg.hard_cap_sl_pips:
            # Tighten the stop to the cap. This sacrifices a bit of risk
            # headroom but keeps the position sizing sane.
            capped_risk = cfg.hard_cap_sl_pips * pip
            if direction == TradeDirection.LONG:
                stop_loss = entry - capped_risk
            else:
                stop_loss = entry + capped_risk
            risk = capped_risk

        # --- TPs at R multiples ---
        if direction == TradeDirection.LONG:
            tp1 = entry + risk * cfg.tp1_rr
            tp2 = entry + risk * cfg.tp2_rr
            tp3 = entry + risk * cfg.tp3_rr
        else:
            tp1 = entry - risk * cfg.tp1_rr
            tp2 = entry - risk * cfg.tp2_rr
            tp3 = entry - risk * cfg.tp3_rr

        # --- Confidence ---
        # Base = min_confidence. Boost by ADX strength (above threshold) and
        # by breakout distance (in ATR units). Cap at 0.85 to avoid overconfidence.
        breakout_dist = abs(entry - dc_high) if direction == TradeDirection.LONG else abs(entry - dc_low)
        adx_boost = max(0.0, adx - cfg.adx_threshold) * 0.005
        vol_boost = max(0.0, (atr - atr_sma) / atr_sma) * 0.20 if atr_sma > 0 else 0.0
        dist_boost = (breakout_dist / atr) * 0.05 if atr > 0 else 0.0
        confidence = min(
            cfg.min_confidence + adx_boost + vol_boost + dist_boost,
            0.85,
        )
        confidence = max(cfg.min_confidence, confidence)

        # Reset cooldown only when we actually emit.
        self._bars_since_signal = 0

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
