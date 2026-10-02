"""Regime-aware thresholds for signal validation and position sizing (BQ-1240b).

Switches validation thresholds and exposure multipliers based on the
current market regime classified by the HMM model (BQ-1240a).

Regimes:
    - STABLE:     Tight thresholds (±1.5σ, corr≥0.65, ≥0.03% move).
                  Full exposure allowed.
    - BREAKDOWN:  Wide thresholds (±2.5σ, corr≥0.75, ≥0.08% move).
                  Exposure reduced 50% to protect against regime-change
                  whipsaws and correlation breakdowns.
    - TRANSITION: Hold previous regime's thresholds (regime is uncertain).

Usage:
    from risk.regime_thresholds import RegimeAwareThresholds, Regime

    rat = RegimeAwareThresholds()
    thresholds = rat.get_thresholds(Regime.STABLE)
    exposure = rat.get_exposure_multiplier(Regime.BREAKDOWN)  # 0.5

    # Integration with CorrelationAwareSizer:
    sizer = CorrelationAwareSizer(cm)
    result = sizer.compute_adjusted_size(
        pair="EURUSD",
        direction="long",
        base_size_lots=0.50,
        base_risk_pct=0.005,
        regime=Regime.BREAKDOWN,
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    """Market regime labels (aligned with BQ-1240a HMM output)."""

    STABLE = "STABLE"
    BREAKDOWN = "BREAKDOWN"
    TRANSITION = "TRANSITION"


@dataclass(frozen=True)
class RegimeThresholds:
    """Threshold set for a single regime.

    Attributes:
        sigma_mult: Standard-deviation multiplier for signal validation.
            Signals beyond ±sigma_mult × σ are considered significant.
        corr_threshold: Minimum correlation coefficient for the
            correlation-aware sizer to treat two instruments as linked.
        pip_threshold_pct: Minimum price movement (as % of price, e.g.
            0.03 means 0.03 %) required to confirm a directional move.
    """

    sigma_mult: float
    corr_threshold: float
    pip_threshold_pct: float


# --------------------------------------------------------------------------- #
# Default threshold configuration per regime (BQ-1240b spec)
# --------------------------------------------------------------------------- #
_DEFAULT_THRESHOLDS: dict[Regime, RegimeThresholds] = {
    Regime.STABLE: RegimeThresholds(
        sigma_mult=1.5,
        corr_threshold=0.65,
        pip_threshold_pct=0.03,
    ),
    Regime.BREAKDOWN: RegimeThresholds(
        sigma_mult=2.5,
        corr_threshold=0.75,
        pip_threshold_pct=0.08,
    ),
    # TRANSITION uses held-over thresholds; this is a fallback default.
    Regime.TRANSITION: RegimeThresholds(
        sigma_mult=1.5,
        corr_threshold=0.65,
        pip_threshold_pct=0.03,
    ),
}

# Exposure multipliers per regime (applied to aggregate risk cap)
_EXPOSURE_MULTIPLIERS: dict[Regime, float] = {
    Regime.STABLE: 1.0,
    Regime.BREAKDOWN: 0.5,  # 50 % exposure reduction
    Regime.TRANSITION: 1.0,  # stay conservative = STABLE exposure
}


class RegimeAwareThresholds:
    """Provides regime-dependent thresholds and exposure multipliers.

    When the regime is TRANSITION, thresholds from the *previous*
    confident regime are held.  This prevents rapid threshold toggling
    during brief uncertainty periods.

    Parameters:
        thresholds: Optional override for default per-regime thresholds.
        exposure_multipliers: Optional override for default exposure
            multipliers per regime.
    """

    def __init__(
        self,
        thresholds: Optional[dict[Regime, RegimeThresholds]] = None,
        exposure_multipliers: Optional[dict[Regime, float]] = None,
    ) -> None:
        self._thresholds = thresholds or dict(_DEFAULT_THRESHOLDS)
        self._exposure_multipliers = exposure_multipliers or dict(
            _EXPOSURE_MULTIPLIERS,
        )
        self._previous_confident_regime: Regime = Regime.STABLE

    def get_thresholds(self, regime: Regime | str) -> RegimeThresholds:
        """Return thresholds for the given regime.

        For TRANSITION, returns the thresholds of the last confident
        regime (STABLE or BREAKDOWN) that was observed.
        """
        if isinstance(regime, str):
            regime = Regime(regime.upper())

        if regime == Regime.TRANSITION:
            held = self._thresholds[self._previous_confident_regime]
            logger.debug(
                "TRANSITION regime: holding %s thresholds (σ=%.1f, corr=%.2f)",
                self._previous_confident_regime.value,
                held.sigma_mult,
                held.corr_threshold,
            )
            return held

        # Track the last confident regime for TRANSITION hold-over
        self._previous_confident_regime = regime
        return self._thresholds[regime]

    def get_exposure_multiplier(self, regime: Regime | str) -> float:
        """Return the exposure multiplier for the given regime.

        BREAKDOWN → 0.5 (50 % reduction).
        STABLE → 1.0 (full exposure).
        TRANSITION → 1.0 (conservative, same as STABLE).
        """
        if isinstance(regime, str):
            regime = Regime(regime.upper())
        return self._exposure_multipliers[regime]

    def classify_signal(
        self,
        regime: Regime | str,
        z_score: float,
        correlation: float,
        price_move_pct: float,
    ) -> tuple[bool, str]:
        """Validate a signal against regime-specific thresholds.

        Args:
            regime: Current market regime.
            z_score: Signal strength in standard deviations (absolute value).
            correlation: Correlation coefficient with key reference instrument.
            price_move_pct: Price movement as a percentage (e.g. 0.05 = 0.05 %).

        Returns:
            (passes, reason) — True if signal clears all thresholds.
        """
        t = self.get_thresholds(regime)

        if z_score < t.sigma_mult:
            return False, (f"z_score {z_score:.2f} < {t.sigma_mult:.1f}σ threshold")
        if abs(correlation) < t.corr_threshold:
            return False, (f"correlation {abs(correlation):.2f} < {t.corr_threshold:.2f} threshold")
        if price_move_pct < t.pip_threshold_pct:
            return False, (f"price move {price_move_pct:.3f}% < {t.pip_threshold_pct:.3f}% threshold")
        return True, "pass"
