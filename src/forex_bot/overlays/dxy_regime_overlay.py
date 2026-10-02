"""DXY Macro Regime Confidence Overlay.

Classifies the DXY (US Dollar Index) macro regime and produces a
confidence multiplier for forex trade signals. When the DXY is trending
against a signal's direction, confidence is reduced; when it aligns,
confidence is preserved or slightly boosted.

Regimes:
    - trending_up:   DXY rising (dollar strengthening)
    - trending_down: DXY falling (dollar weakening)
    - high_vol:      DXY volatile / unstable
    - neutral:       DXY flat or data unavailable

Usage:
    from overlays.dxy_regime_overlay import DxyRegimeOverlay

    overlay = DxyRegimeOverlay()
    regime = overlay.classify(dxy_bars)
    mult = overlay.confidence_multiplier(regime, TradeDirection.LONG)
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# SMA and ATR lookback windows
_DEFAULT_TREND_PERIOD = 20
_DEFAULT_ATR_PERIOD = 14
_DEFAULT_VOL_PERCENTILE = 75.0

# Confidence multipliers by regime × direction
# LONG on non-USD pair: hurts when dollar rising (trending_up)
# SHORT on non-USD pair: hurts when dollar falling (trending_down)
_MULTIPLIERS: dict[str, dict[str, float]] = {
    "trending_up": {"LONG": 0.85, "SHORT": 1.10},
    "trending_down": {"LONG": 1.10, "SHORT": 0.85},
    "high_vol": {"LONG": 0.90, "SHORT": 0.90},
    "neutral": {"LONG": 1.0, "SHORT": 1.0},
}


@dataclass(frozen=True)
class DxyBar:
    """Minimal OHLC bar for DXY data."""

    time_ms: int
    open: float
    high: float
    low: float
    close: float


class DxyRegimeOverlay:
    """DXY-based macro regime classifier for forex signal confidence.

    Accepts DXY OHLC bars and classifies the current regime using
    SMA trend direction + ATR percentile volatility. Degrades
    gracefully to ``neutral`` when insufficient data is provided.
    """

    def __init__(
        self,
        trend_period: int = _DEFAULT_TREND_PERIOD,
        atr_period: int = _DEFAULT_ATR_PERIOD,
        vol_percentile: float = _DEFAULT_VOL_PERCENTILE,
    ) -> None:
        self.trend_period = trend_period
        self.atr_period = atr_period
        self.vol_percentile = vol_percentile

    # ------------------------------------------------------------------
    # Regime classification
    # ------------------------------------------------------------------

    def classify(self, bars: Sequence[DxyBar]) -> str:
        """Classify the DXY regime from recent OHLC bars.

        Returns one of: trending_up, trending_down, high_vol, neutral.
        Requires at least ``trend_period`` bars for trend classification
        and ``atr_period * 2`` bars for volatility percentile.
        """
        if len(bars) < max(self.trend_period, self.atr_period + 1):
            logger.debug(
                "DXY overlay: insufficient bars (%d < %d), neutral",
                len(bars),
                max(self.trend_period, self.atr_period + 1),
            )
            return "neutral"

        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]

        # Trend via SMA slope
        sma_now = sum(closes[-self.trend_period :]) / self.trend_period
        if len(closes) >= self.trend_period * 2:
            sma_prev = sum(closes[-self.trend_period * 2 : -self.trend_period]) / self.trend_period
        else:
            sma_prev = sum(closes[: self.trend_period]) / self.trend_period

        sma_change = (sma_now - sma_prev) / sma_prev if sma_prev > 0 else 0.0

        # Volatility via ATR percentile
        atr_now = self._atr(highs, lows, closes, self.atr_period)
        atr_series = self._atr_series(highs, lows, closes, self.atr_period)
        is_high_vol = self._is_high_vol(atr_series, atr_now)

        # Classification logic
        if is_high_vol:
            return "high_vol"

        threshold = 0.001  # 0.1% SMA change to confirm trend
        if sma_change > threshold:
            return "trending_up"
        if sma_change < -threshold:
            return "trending_down"
        return "neutral"

    # ------------------------------------------------------------------
    # Confidence multiplier
    # ------------------------------------------------------------------

    def confidence_multiplier(self, regime: str, direction: str) -> float:
        """Get confidence multiplier for a signal direction under a regime.

        Args:
            regime: One of trending_up, trending_down, high_vol, neutral.
            direction: "LONG" or "SHORT".

        Returns:
            Multiplier in range [0.85, 1.10]. Neutral regime always
            returns 1.0.
        """
        dir_key = direction.upper()
        if dir_key not in ("LONG", "SHORT"):
            logger.warning("DXY overlay: unknown direction '%s', mult=1.0", direction)
            return 1.0

        regime_map = _MULTIPLIERS.get(regime)
        if regime_map is None:
            logger.warning("DXY overlay: unknown regime '%s', mult=1.0", regime)
            return 1.0

        return regime_map.get(dir_key, 1.0)

    # ------------------------------------------------------------------
    # Combined helper
    # ------------------------------------------------------------------

    def adjust_confidence(
        self,
        confidence: float,
        bars: Sequence[DxyBar],
        direction: str,
    ) -> float:
        """Classify regime from bars and apply multiplier to confidence.

        This is the main entry point for strategies: pass in the signal
        confidence, DXY bars, and trade direction to get the adjusted
        confidence value.
        """
        regime = self.classify(bars)
        mult = self.confidence_multiplier(regime, direction)
        adjusted = confidence * mult
        # Clamp to [0.0, 1.0]
        return max(0.0, min(1.0, adjusted))

    # ------------------------------------------------------------------
    # Internal calculations
    # ------------------------------------------------------------------

    @staticmethod
    def _sma(values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    @staticmethod
    def _atr(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        period: int,
    ) -> float:
        """Compute current ATR over the last ``period`` bars."""
        if len(closes) < period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(len(closes) - period, len(closes)):
            if i > 0:
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                tr_sum += tr
        return tr_sum / period if period > 0 else 0.0

    @staticmethod
    def _atr_series(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        period: int,
    ) -> list[float]:
        """Compute rolling ATR series for percentile calculation."""
        if len(closes) < period + 1:
            return []
        tr_values: list[float] = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_values.append(tr)

        atrs: list[float] = []
        for i in range(period, len(tr_values) + 1):
            window = tr_values[i - period : i]
            atrs.append(sum(window) / period)
        return atrs

    def _is_high_vol(
        self,
        atr_series: list[float],
        current_atr: float,
    ) -> bool:
        """Check if current ATR is above the configured percentile."""
        if not atr_series or current_atr <= 0:
            return False
        sorted_series = sorted(atr_series)
        rank = sum(1 for v in sorted_series if v < current_atr)
        tied = sum(1 for v in sorted_series if v == current_atr)
        rank += tied // 2
        percentile = (rank / len(sorted_series)) * 100.0
        return percentile >= self.vol_percentile
