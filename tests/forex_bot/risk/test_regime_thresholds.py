"""Tests for regime-aware thresholds and correlation sizer integration (BQ-1240b).

Covers:
    1. RegimeAwareThresholds — per-regime threshold values
    2. TRANSITION holds previous confident regime's thresholds
    3. Exposure multiplier (0.5 in BREAKDOWN)
    4. CorrelationAwareSizer BREAKDOWN exposure reduction
    5. classify_signal validation
"""

from __future__ import annotations

import pytest
from risk.correlation_matrix import CorrelationMatrix
from risk.correlation_sizer import CorrelationAwareSizer, Direction
from risk.regime_thresholds import Regime, RegimeAwareThresholds


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_matrix(symbol_returns: dict[str, list[float]], window: int = 30) -> CorrelationMatrix:
    cm = CorrelationMatrix(window=window)
    for sym, rets in symbol_returns.items():
        cm.add_returns(sym, rets)
    cm.compute()
    return cm


def _perfectly_correlated(n: int = 60) -> dict[str, list[float]]:
    base = [0.001 * ((-1) ** i) for i in range(n)]
    return {"EURUSD": base[:], "GBPUSD": base[:]}


# --------------------------------------------------------------------------- #
# 1. Per-regime threshold values
# --------------------------------------------------------------------------- #
class TestRegimeThresholds:
    def test_stable_thresholds(self):
        """STABLE regime: ±1.5σ, corr≥0.65, ≥0.03%."""
        rat = RegimeAwareThresholds()
        t = rat.get_thresholds(Regime.STABLE)
        assert t.sigma_mult == pytest.approx(1.5)
        assert t.corr_threshold == pytest.approx(0.65)
        assert t.pip_threshold_pct == pytest.approx(0.03)

    def test_breakdown_thresholds(self):
        """BREAKDOWN regime: ±2.5σ, corr≥0.75, ≥0.08%."""
        rat = RegimeAwareThresholds()
        t = rat.get_thresholds(Regime.BREAKDOWN)
        assert t.sigma_mult == pytest.approx(2.5)
        assert t.corr_threshold == pytest.approx(0.75)
        assert t.pip_threshold_pct == pytest.approx(0.08)

    def test_thresholds_accept_string_label(self):
        """Regime can be passed as a plain string."""
        rat = RegimeAwareThresholds()
        t = rat.get_thresholds("STABLE")
        assert t.sigma_mult == pytest.approx(1.5)
        t2 = rat.get_thresholds("breakdown")
        assert t2.sigma_mult == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# 2. TRANSITION holds previous confident regime
# --------------------------------------------------------------------------- #
class TestTransitionHold:
    def test_transition_holds_stable(self):
        """After STABLE, TRANSITION should hold STABLE thresholds."""
        rat = RegimeAwareThresholds()
        rat.get_thresholds(Regime.STABLE)  # set previous
        t = rat.get_thresholds(Regime.TRANSITION)
        assert t.sigma_mult == pytest.approx(1.5)
        assert t.corr_threshold == pytest.approx(0.65)

    def test_transition_holds_breakdown(self):
        """After BREAKDOWN, TRANSITION should hold BREAKDOWN thresholds."""
        rat = RegimeAwareThresholds()
        rat.get_thresholds(Regime.BREAKDOWN)  # set previous
        t = rat.get_thresholds(Regime.TRANSITION)
        assert t.sigma_mult == pytest.approx(2.5)
        assert t.corr_threshold == pytest.approx(0.75)

    def test_transition_default_before_any_regime(self):
        """Fresh init with TRANSITION uses fallback (STABLE-equivalent)."""
        rat = RegimeAwareThresholds()
        t = rat.get_thresholds(Regime.TRANSITION)
        # Default fallback = STABLE
        assert t.sigma_mult == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# 3. Exposure multipliers
# --------------------------------------------------------------------------- #
class TestExposureMultiplier:
    def test_stable_full_exposure(self):
        rat = RegimeAwareThresholds()
        assert rat.get_exposure_multiplier(Regime.STABLE) == pytest.approx(1.0)

    def test_breakdown_half_exposure(self):
        rat = RegimeAwareThresholds()
        assert rat.get_exposure_multiplier(Regime.BREAKDOWN) == pytest.approx(0.5)

    def test_transition_full_exposure(self):
        rat = RegimeAwareThresholds()
        assert rat.get_exposure_multiplier(Regime.TRANSITION) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 4. CorrelationAwareSizer BREAKDOWN integration
# --------------------------------------------------------------------------- #
class TestSizerBreakdownIntegration:
    def test_breakdown_reduces_effective_cap(self):
        """In BREAKDOWN, the aggregate risk cap is halved."""
        cm = _build_matrix(_perfectly_correlated())
        sizer = CorrelationAwareSizer(
            cm,
            per_trade_risk_pct=0.005,
            aggregate_risk_pct=0.01,
        )

        # Register one position to consume part of the budget
        sizer.register_position("strat_a", "EURUSD", Direction.LONG, size_lots=0.50, risk_pct=0.005)

        # Without regime: remaining = 0.01 - 0.005 = 0.005 → fits
        result_normal = sizer.compute_adjusted_size(
            pair="GBPUSD",
            direction="long",
            base_size_lots=0.50,
            base_risk_pct=0.005,
        )
        assert not result_normal.blocked
        assert result_normal.scale_factor == pytest.approx(1.0)

        # With BREAKDOWN: effective cap = 0.005, remaining = 0.005 - 0.005 = 0 → blocked
        result_bd = sizer.compute_adjusted_size(
            pair="GBPUSD",
            direction="long",
            base_size_lots=0.50,
            base_risk_pct=0.005,
            regime="BREAKDOWN",
        )
        assert result_bd.blocked
        assert any("BREAKDOWN" in w for w in result_bd.warnings)

    def test_breakdown_halves_available_budget(self):
        """BREAKDOWN with no existing positions still has 50% cap."""
        cm = _build_matrix(_perfectly_correlated())
        sizer = CorrelationAwareSizer(
            cm,
            per_trade_risk_pct=0.005,
            aggregate_risk_pct=0.01,
        )

        sizer.register_position("strat_a", "EURUSD", Direction.LONG, size_lots=0.30, risk_pct=0.003)

        # Normal: remaining = 0.01 - 0.003 = 0.007
        # BREAKDOWN: effective cap = 0.005, remaining = 0.005 - 0.003 = 0.002
        # scale = 0.002 / 0.005 = 0.4
        result = sizer.compute_adjusted_size(
            pair="GBPUSD",
            direction="long",
            base_size_lots=0.50,
            base_risk_pct=0.005,
            regime="BREAKDOWN",
        )
        assert not result.blocked
        assert result.scale_factor == pytest.approx(0.4, abs=0.01)

    def test_no_regime_preserves_original_behavior(self):
        """Passing regime=None (or omitting) should behave exactly as before."""
        cm = _build_matrix(_perfectly_correlated())
        sizer = CorrelationAwareSizer(cm)

        result = sizer.compute_adjusted_size(
            pair="EURUSD",
            direction="long",
            base_size_lots=0.50,
            base_risk_pct=0.005,
        )
        assert result.scale_factor == pytest.approx(1.0)
        assert not any("BREAKDOWN" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# 5. classify_signal validation
# --------------------------------------------------------------------------- #
class TestClassifySignal:
    def test_signal_passes_stable(self):
        rat = RegimeAwareThresholds()
        passes, reason = rat.classify_signal(
            regime=Regime.STABLE,
            z_score=2.0,
            correlation=0.80,
            price_move_pct=0.05,
        )
        assert passes
        assert reason == "pass"

    def test_signal_fails_sigma_stable(self):
        rat = RegimeAwareThresholds()
        passes, reason = rat.classify_signal(
            regime=Regime.STABLE,
            z_score=1.0,  # below 1.5σ
            correlation=0.80,
            price_move_pct=0.05,
        )
        assert not passes
        assert "z_score" in reason

    def test_signal_fails_corr_stable(self):
        rat = RegimeAwareThresholds()
        passes, reason = rat.classify_signal(
            regime=Regime.STABLE,
            z_score=2.0,
            correlation=0.50,  # below 0.65
            price_move_pct=0.05,
        )
        assert not passes
        assert "correlation" in reason

    def test_breakdown_requires_higher_sigma(self):
        """BREAKDOWN needs 2.5σ — a 2.0σ signal that passes STABLE should fail."""
        rat = RegimeAwareThresholds()
        passes_stable, _ = rat.classify_signal(
            regime=Regime.STABLE,
            z_score=2.0,
            correlation=0.80,
            price_move_pct=0.05,
        )
        passes_bd, reason_bd = rat.classify_signal(
            regime=Regime.BREAKDOWN,
            z_score=2.0,
            correlation=0.80,
            price_move_pct=0.05,
        )
        assert passes_stable
        assert not passes_bd
        assert "z_score" in reason_bd
