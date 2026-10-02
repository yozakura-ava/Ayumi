"""PSI (Population Stability Index) drift detector.

Measures distribution shift between a baseline (expected) and current
(actual) period for feature values. Used in the alpha-decay pipeline
to detect when signal feature distributions have drifted enough to
degrade model performance.

PSI Interpretation
------------------
- PSI < 0.10  → no significant change
- 0.10 ≤ PSI < 0.25 → minor shift (monitor)
- PSI ≥ 0.25  → major shift (retrain)

The alert threshold defaults to 0.2 (significant shift) per the
alpha-decay pipeline spec (SRB-AYU-001).

References
----------
- SRB-AYU-001: Walk-forward alpha decay pipeline
- Original BQ items: BQ-1154, BQ-1139
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLD = 0.2  # Alert when PSI exceeds this
DEFAULT_N_BINS = 10  # Number of bins for distribution comparison
DEFAULT_WINDOW_SIZE = 1000  # Rolling window size for continuous tracking
EPSILON = 1e-6  # Floor to prevent division by zero / log(0)

# Severity bands
PSI_OK = 0.10
PSI_MINOR = 0.25


def _severity(psi: float) -> str:
    """Classify PSI into severity bands."""
    if psi < PSI_OK:
        return "none"
    if psi < PSI_MINOR:
        return "minor"
    return "major"


def compute_psi(
    expected: Sequence[float],
    actual: Sequence[float],
    n_bins: int = DEFAULT_N_BINS,
) -> float:
    """Compute the Population Stability Index between two distributions.

    Parameters
    ----------
    expected : baseline distribution (training / reference period).
    actual   : current distribution (live / test period).
    n_bins   : number of equal-width bins for the histogram comparison.

    Returns
    -------
    float – PSI value (≥ 0).  ``0.0`` means identical distributions.

    Raises
    ------
    ValueError – if either input is empty or ``n_bins < 2``.
    """
    if n_bins < 2:
        raise ValueError(f"n_bins must be ≥ 2, got {n_bins}")
    if len(expected) == 0:
        raise ValueError("expected distribution is empty")
    if len(actual) == 0:
        raise ValueError("actual distribution is empty")

    # Determine bin edges from the *expected* (baseline) distribution so
    # that the reference proportions stay stable across comparisons.
    exp_sorted = sorted(expected)
    act_sorted = sorted(actual)

    lo = exp_sorted[0]
    hi = exp_sorted[-1]

    # If all expected values are identical, use a tiny range around them
    if hi == lo:
        lo -= EPSILON
        hi += EPSILON

    bin_width = (hi - lo) / n_bins
    edges = [lo + i * bin_width for i in range(n_bins + 1)]
    # Force the last edge to +inf so nothing escapes the top bin
    edges[-1] = math.inf
    # Force the first edge to -inf so nothing escapes the bottom bin
    edges[0] = -math.inf

    # ── Count expected per bin ──────────────────────────────────────────────
    exp_counts = [0] * n_bins
    for v in exp_sorted:
        idx = _bin_index(v, edges, n_bins)
        exp_counts[idx] += 1

    # ── Count actual per bin ────────────────────────────────────────────────
    act_counts = [0] * n_bins
    for v in act_sorted:
        idx = _bin_index(v, edges, n_bins)
        act_counts[idx] += 1

    # ── PSI summation ──────────────────────────────────────────────────────
    n_exp = len(exp_sorted)
    n_act = len(act_sorted)

    psi = 0.0
    for i in range(n_bins):
        exp_pct = exp_counts[i] / n_exp
        act_pct = act_counts[i] / n_act

        # Floor both to EPSILON to avoid log(0) / division by zero
        exp_pct = max(exp_pct, EPSILON)
        act_pct = max(act_pct, EPSILON)

        psi += (act_pct - exp_pct) * math.log(act_pct / exp_pct)

    return psi


def _bin_index(value: float, edges: list[float], n_bins: int) -> int:
    """Find the bin index for *value* given *edges*.

    Edges are sorted ascending with edges[0] = -inf and edges[-1] = +inf.
    """
    # Binary search: find the first edge[i+1] that value < edges[i+1]
    lo, hi = 0, n_bins - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if value < edges[mid + 1]:
            hi = mid
        else:
            lo = mid + 1
    return lo


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class PSIAlert:
    """Single PSI drift alert for one feature."""

    feature: str
    psi: float
    threshold: float
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    severity: str = field(default="none")

    def __post_init__(self) -> None:
        if self.severity == "none":
            self.severity = _severity(self.psi)

    @property
    def is_alert(self) -> bool:
        """True when PSI exceeds the alert threshold."""
        return self.psi >= self.threshold


@dataclass
class FeatureWindow:
    """Rolling window of values for a single feature."""

    values: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_WINDOW_SIZE))

    def add(self, items: Sequence[float]) -> None:
        """Append a batch of values to the window."""
        self.values.extend(items)

    def snapshot(self) -> list[float]:
        """Return current window contents as a list."""
        return list(self.values)

    @property
    def count(self) -> int:
        return len(self.values)


# ── Detector ────────────────────────────────────────────────────────────────


class PSIDriftDetector:
    """Multi-feature PSI drift detector with rolling-window tracking.

    Usage
    -----
    ::

        detector = PSIDriftDetector(threshold=0.2, n_bins=10)

        # Set baseline from training data
        detector.set_baseline("win_rate", [0.4, 0.45, 0.5, ...])
        detector.set_baseline("sharpe", [1.2, 1.5, 0.9, ...])

        # Check drift against current data
        alerts = detector.check_all({
            "win_rate": [0.35, 0.38, 0.30, ...],
            "sharpe":   [0.6, 0.8, 0.5, ...],
        })

        # Continuous rolling tracking
        detector.update_rolling("win_rate", [0.35, 0.38])
        detector.update_rolling("sharpe", [0.6, 0.8])
    """

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        n_bins: int = DEFAULT_N_BINS,
        window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> None:
        if threshold <= 0:
            raise ValueError(f"threshold must be > 0, got {threshold}")
        if n_bins < 2:
            raise ValueError(f"n_bins must be ≥ 2, got {n_bins}")

        self.threshold = threshold
        self.n_bins = n_bins
        self.window_size = window_size

        # Baseline distributions keyed by feature name
        self._baselines: dict[str, list[float]] = {}

        # Rolling windows for continuous tracking
        self._windows: dict[str, FeatureWindow] = {}

        # History of generated alerts
        self._alert_history: list[PSIAlert] = []

    # ── Baseline management ────────────────────────────────────────────────

    def set_baseline(self, feature: str, values: Sequence[float]) -> None:
        """Set the baseline (expected) distribution for a feature."""
        if len(values) == 0:
            raise ValueError(f"baseline values for '{feature}' is empty")
        self._baselines[feature] = list(values)
        logger.debug(
            "Baseline set for '%s' (%d values)",
            feature,
            len(values),
        )

    def has_baseline(self, feature: str) -> bool:
        return feature in self._baselines

    # ── Drift checking ─────────────────────────────────────────────────────

    def check_drift(
        self,
        feature: str,
        actual: Sequence[float],
    ) -> PSIAlert:
        """Check a single feature for drift against its baseline.

        Returns
        -------
        PSIAlert with the computed PSI, severity, and alert flag.
        """
        if feature not in self._baselines:
            raise KeyError(f"No baseline set for feature '{feature}'. Call set_baseline() first.")

        psi = compute_psi(
            self._baselines[feature],
            actual,
            n_bins=self.n_bins,
        )
        alert = PSIAlert(
            feature=feature,
            psi=psi,
            threshold=self.threshold,
        )

        if alert.is_alert:
            logger.warning(
                "PSI drift detected for '%s': %.4f (threshold %.2f, severity=%s)",
                feature,
                psi,
                self.threshold,
                alert.severity,
            )

        self._alert_history.append(alert)
        return alert

    def check_all(
        self,
        actuals: dict[str, Sequence[float]],
    ) -> list[PSIAlert]:
        """Check multiple features at once.

        Only features with a pre-set baseline are checked. Missing
        baselines are logged at WARNING level and skipped.
        """
        alerts: list[PSIAlert] = []
        for feature, values in actuals.items():
            if not self.has_baseline(feature):
                logger.warning("No baseline for '%s', skipping", feature)
                continue
            alerts.append(self.check_drift(feature, values))
        return alerts

    # ── Rolling window tracking ────────────────────────────────────────────

    def update_rolling(self, feature: str, values: Sequence[float]) -> None:
        """Push new values into the rolling window for a feature."""
        if feature not in self._windows:
            self._windows[feature] = FeatureWindow(
                values=deque(maxlen=self.window_size),
            )
        self._windows[feature].add(values)

    def check_rolling_drift(self, feature: str) -> PSIAlert | None:
        """Check drift using rolling window as the actual distribution.

        Returns ``None`` if the rolling window is empty.
        """
        if feature not in self._windows:
            return None
        window = self._windows[feature].snapshot()
        if not window:
            return None
        return self.check_drift(feature, window)

    # ── Accessors ──────────────────────────────────────────────────────────

    @property
    def alerts(self) -> list[PSIAlert]:
        """All alerts generated since the detector was created."""
        return list(self._alert_history)

    @property
    def tracked_features(self) -> list[str]:
        """Features with a baseline set."""
        return sorted(self._baselines.keys())

    def reset_alerts(self) -> None:
        """Clear alert history (does not affect baselines or windows)."""
        self._alert_history.clear()
