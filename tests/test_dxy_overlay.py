"""Tests for DXY macro regime confidence overlay.

Covers: regime classification (4 regimes), confidence multiplier
(directional mapping), edge cases (insufficient data, empty bars),
and SRMR+ integration.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/forex_bot is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "forex_bot"))

from backtest.engine import StrategySignal, TradeDirection  # noqa: E402
from overlays.dxy_regime_overlay import DxyBar, DxyRegimeOverlay  # noqa: E402, I001
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dxy_bars(
    n: int = 30,
    start_price: float = 104.0,
    trend: float = 0.0,
    noise: float = 0.05,
    seed: int = 42,
) -> list[DxyBar]:
    """Generate n synthetic DXY bars with optional trend and noise."""
    import random

    rng = random.Random(seed)  # noqa: S311
    bars: list[DxyBar] = []
    price = start_price
    for i in range(n):
        o = price
        change = trend + rng.uniform(-noise, noise)
        c = o + change
        h = max(o, c) + rng.uniform(0, noise * 0.5)
        lo = min(o, c) - rng.uniform(0, noise * 0.5)
        bars.append(DxyBar(time_ms=i * 3600_000, open=o, high=h, low=lo, close=c))
        price = c
    return bars


# ---------------------------------------------------------------------------
# Regime Classification Tests
# ---------------------------------------------------------------------------


class TestClassify:
    def test_trending_up(self):
        """DXY rising consistently → trending_up."""
        bars = make_dxy_bars(n=40, start_price=104.0, trend=0.05, noise=0.01)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        regime = overlay.classify(bars)
        assert regime == "trending_up", f"Expected trending_up, got {regime}"

    def test_trending_down(self):
        """DXY falling consistently → trending_down."""
        bars = make_dxy_bars(n=40, start_price=104.0, trend=-0.05, noise=0.01)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        regime = overlay.classify(bars)
        assert regime == "trending_down", f"Expected trending_down, got {regime}"

    def test_neutral_flat(self):
        """DXY flat → neutral."""
        bars = make_dxy_bars(n=40, start_price=104.0, trend=0.0, noise=0.005)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        regime = overlay.classify(bars)
        assert regime == "neutral", f"Expected neutral, got {regime}"

    def test_high_volatility(self):
        """DXY with massive oscillations (no drift) → high_vol."""
        # Build oscillating bars: price swings ±2.0 around 104.0
        bars: list[DxyBar] = []
        price = 104.0
        for i in range(40):
            o = price
            # Alternate up/down to cancel directional trend
            change = 2.0 if i % 2 == 0 else -2.0
            c = o + change
            h = max(o, c) + 0.1
            lo = min(o, c) - 0.1
            bars.append(DxyBar(time_ms=i * 3600_000, open=o, high=h, low=lo, close=c))
            price = c
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14, vol_percentile=50.0)
        regime = overlay.classify(bars)
        assert regime == "high_vol", f"Expected high_vol, got {regime}"

    def test_insufficient_bars_returns_neutral(self):
        """Fewer bars than required → neutral."""
        bars = make_dxy_bars(n=5, trend=0.1)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        regime = overlay.classify(bars)
        assert regime == "neutral"

    def test_empty_bars_returns_neutral(self):
        """No bars at all → neutral."""
        overlay = DxyRegimeOverlay()
        assert overlay.classify([]) == "neutral"


# ---------------------------------------------------------------------------
# Confidence Multiplier Tests
# ---------------------------------------------------------------------------


class TestConfidenceMultiplier:
    def setup_method(self):
        self.overlay = DxyRegimeOverlay()

    def test_long_trending_up_reduced(self):
        """Dollar rising hurts LONG signals on non-USD pairs."""
        mult = self.overlay.confidence_multiplier("trending_up", "LONG")
        assert mult < 1.0
        assert mult == pytest.approx(0.85)

    def test_short_trending_up_boosted(self):
        """Dollar rising supports SHORT signals on non-USD pairs."""
        mult = self.overlay.confidence_multiplier("trending_up", "SHORT")
        assert mult > 1.0
        assert mult == pytest.approx(1.10)

    def test_long_trending_down_boosted(self):
        """Dollar falling supports LONG signals on non-USD pairs."""
        mult = self.overlay.confidence_multiplier("trending_down", "LONG")
        assert mult > 1.0
        assert mult == pytest.approx(1.10)

    def test_short_trending_down_reduced(self):
        """Dollar falling hurts SHORT signals on non-USD pairs."""
        mult = self.overlay.confidence_multiplier("trending_down", "SHORT")
        assert mult < 1.0
        assert mult == pytest.approx(0.85)

    def test_high_vol_reduces_both(self):
        """High volatility reduces confidence for both directions."""
        assert self.overlay.confidence_multiplier("high_vol", "LONG") < 1.0
        assert self.overlay.confidence_multiplier("high_vol", "SHORT") < 1.0

    def test_neutral_no_change(self):
        """Neutral regime preserves original confidence."""
        assert self.overlay.confidence_multiplier("neutral", "LONG") == 1.0
        assert self.overlay.confidence_multiplier("neutral", "SHORT") == 1.0

    def test_unknown_direction_defaults_to_1(self):
        """Unknown direction returns neutral multiplier."""
        assert self.overlay.confidence_multiplier("neutral", "SIDEWAYS") == 1.0

    def test_unknown_regime_defaults_to_1(self):
        """Unknown regime returns neutral multiplier."""
        assert self.overlay.confidence_multiplier("mars_regime", "LONG") == 1.0


# ---------------------------------------------------------------------------
# Adjust Confidence Integration Tests
# ---------------------------------------------------------------------------


class TestAdjustConfidence:
    def test_applies_multiplier_end_to_end(self):
        """Full pipeline: bars → regime → multiplier → adjusted confidence."""
        bars = make_dxy_bars(n=40, trend=0.05, noise=0.01)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        original = 0.70
        adjusted = overlay.adjust_confidence(original, bars, "LONG")
        # trending_up + LONG → 0.85 multiplier
        assert adjusted < original
        assert adjusted == pytest.approx(original * 0.85, abs=0.01)

    def test_clamps_to_max_1(self):
        """Confidence cannot exceed 1.0 after adjustment."""
        bars = make_dxy_bars(n=40, trend=-0.05, noise=0.01)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        adjusted = overlay.adjust_confidence(0.95, bars, "LONG")
        assert adjusted <= 1.0

    def test_neutral_preserves_confidence(self):
        """Flat DXY data preserves original confidence."""
        bars = make_dxy_bars(n=40, trend=0.0, noise=0.005)
        overlay = DxyRegimeOverlay(trend_period=20, atr_period=14)
        original = 0.65
        adjusted = overlay.adjust_confidence(original, bars, "LONG")
        assert adjusted == pytest.approx(original, abs=0.01)


# ---------------------------------------------------------------------------
# SRMR+ Integration Tests
# ---------------------------------------------------------------------------


class TestSRMRPlusIntegration:
    def test_overlay_disabled_by_default(self):
        """Without dxy_overlay=True, strategy has no overlay."""
        strategy = SRMRPlusStrategy()
        assert strategy._dxy_overlay is None

    def test_overlay_enabled(self):
        """With dxy_overlay=True, strategy initializes overlay."""
        config = SRMRPlusConfig(dxy_overlay=True)
        strategy = SRMRPlusStrategy(config)
        assert strategy._dxy_overlay is not None

    def test_apply_dxy_overlay_no_signal(self):
        """None signal passes through unchanged."""
        config = SRMRPlusConfig(dxy_overlay=True)
        strategy = SRMRPlusStrategy(config)
        assert strategy.apply_dxy_overlay(None, []) is None

    def test_apply_dxy_overlay_no_bars(self):
        """Signal passes through when no DXY bars provided."""
        config = SRMRPlusConfig(dxy_overlay=True)
        strategy = SRMRPlusStrategy(config)
        signal = StrategySignal(
            direction=TradeDirection.LONG,
            confidence=0.70,
            entry_price=1.0850,
            stop_loss=1.0820,
            take_profit_1=1.0880,
            take_profit_2=1.0900,
            take_profit_3=1.0900,
            rationale="test",
        )
        result = strategy.apply_dxy_overlay(signal, None)
        assert result is signal  # unchanged

    def test_apply_dxy_overlay_adjusts_confidence(self):
        """Signal confidence is adjusted when DXY bars are provided."""
        config = SRMRPlusConfig(dxy_overlay=True)
        strategy = SRMRPlusStrategy(config)
        signal = StrategySignal(
            direction=TradeDirection.LONG,
            confidence=0.70,
            entry_price=1.0850,
            stop_loss=1.0820,
            take_profit_1=1.0880,
            take_profit_2=1.0900,
            take_profit_3=1.0900,
            rationale="test signal",
        )
        # Trending up bars → LONG confidence reduced
        dxy_bars = make_dxy_bars(n=40, trend=0.05, noise=0.01)
        result = strategy.apply_dxy_overlay(signal, dxy_bars)
        assert result is not None
        assert result.confidence < 0.70
        assert "DXY adj" in result.rationale
