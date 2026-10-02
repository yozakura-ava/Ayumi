"""Feature importance drift detection — Layer 2: Permutation importance monitor.

Monitors predictive contribution decay of individual features using
permutation importance (model-agnostic, no retraining required).

Design:
    Uses sklearn.inspection.permutation_importance under the hood.
    Maintains a reference importance baseline and compares current
    importance against it to detect features whose predictive power
    is declining.

Layer 3 (SHAP) is integrated as an optional on-demand check that
gracefully degrades when the ``shap`` library is not installed.

Usage:
    from srf.permutation_drift import PermutationDriftMonitor

    monitor = PermutationDriftMonitor(
        model=trained_model,
        eval_fn="neg_mean_squared_error",  # or any sklearn scorer
        reference_X=X_ref,
        reference_y=y_ref,
        features=["f1", "f2", ...],
    )
    report = monitor.check_importance_drift(X_current, y_current)
    for alert in report.alerts():
        print(alert)

Threshold defaults (from SRB-AYU-002):
    Importance decay > 50% from baseline → yellow alert
    Importance near zero or negative → red alert (feature hurts model)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DECAY_THRESHOLD = 0.50  # 50% drop from reference → yellow alert
DEFAULT_RED_ZONE_IMPORTANCE = 0.01  # Near-zero importance → red alert
DEFAULT_N_REPEATS = 5  # Permutation repeats per feature
DEFAULT_RANDOM_STATE = 42


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportanceResult:
    """Single-feature permutation importance assessment."""

    feature: str
    reference_importance: float
    current_importance: float
    reference_std: float
    current_std: float
    decay_threshold: float
    red_zone: float

    @property
    def delta(self) -> float:
        """Absolute change in importance (current - reference)."""
        return self.current_importance - self.reference_importance

    @property
    def pct_decay(self) -> float:
        """Fractional decay from reference. Positive = decayed.

        Returns 0.0 if reference importance ≤ 0 (can't compute ratio).
        """
        if self.reference_importance <= 0:
            return 0.0
        return 1.0 - (self.current_importance / self.reference_importance)

    @property
    def is_declining(self) -> bool:
        """True if importance has decayed beyond the threshold."""
        return self.pct_decay > self.decay_threshold

    @property
    def is_red_zone(self) -> bool:
        """True if current importance is at or below the red-zone floor."""
        return self.current_importance <= self.red_zone

    @property
    def is_negative(self) -> bool:
        """True if permutation actually improved the model (feature hurts)."""
        return self.current_importance < 0

    def severity(self) -> str:
        """Severity bucket: red, yellow, or clear."""
        if self.is_negative or self.is_red_zone:
            return "red"
        if self.is_declining:
            return "yellow"
        return "clear"

    def __str__(self) -> str:
        flag = "🔴" if self.severity() == "red" else ("⚠️" if self.severity() == "yellow" else "✅")
        return (
            f"{flag}  {self.feature}: "
            f"ref={self.reference_importance:.4f} → cur={self.current_importance:.4f} "
            f"(decay={self.pct_decay:.1%}, Δ={self.delta:+.4f}) "
            f"severity={self.severity()}"
        )


@dataclass
class ImportanceReport:
    """Aggregate permutation importance drift report."""

    results: list[ImportanceResult] = field(default_factory=list)

    def alerts(self) -> list[ImportanceResult]:
        """Return only results where importance is declining or in red zone."""
        return [r for r in self.results if r.is_declining or r.is_red_zone or r.is_negative]

    def red_alerts(self) -> list[ImportanceResult]:
        """Return only red-zone results (feature may need replacement)."""
        return [r for r in self.results if r.is_negative or r.is_red_zone]

    def yellow_alerts(self) -> list[ImportanceResult]:
        """Return only yellow alerts (importance decaying but not critical)."""
        return [r for r in self.results if r.is_declining and not r.is_red_zone and not r.is_negative]

    @property
    def has_drift(self) -> bool:
        return len(self.alerts()) > 0

    def summary(self) -> str:
        n_total = len(self.results)
        n_alert = len(self.alerts())
        n_red = len(self.red_alerts())
        n_yellow = len(self.yellow_alerts())
        lines = [
            f"Importance Drift Report: {n_alert}/{n_total} features affected ({n_red} red, {n_yellow} yellow)",
        ]
        if n_red > 0:
            lines.append("  🔴 Red alerts (consider replacement):")
            for r in self.red_alerts():
                lines.append(f"    {r}")
        if n_yellow > 0:
            lines.append("  ⚠️ Yellow alerts (investigate decay):")
            for r in self.yellow_alerts():
                lines.append(f"    {r}")
        if n_alert == 0:
            lines.append("  All features stable.")
        return "\n".join(lines)

    def __iter__(self):
        return iter(self.results)


# ---------------------------------------------------------------------------
# SHAP integration (Layer 3 — optional, gracefully degrades)
# ---------------------------------------------------------------------------


class ShapDriftChecker:
    """Layer 3: SHAP-based drift detection.

    Requires the ``shap`` library. Use ``is_available()`` before calling.

    When shap is not installed, methods return informative placeholders
    instead of raising ImportError, allowing Layer 1 + Layer 2 to run
    independently.
    """

    @staticmethod
    def is_available() -> bool:
        """Check if the shap library is installed."""
        try:
            # Side-effect import: only used to probe optional dependency availability.
            import shap  # noqa: F401

            return True
        except ImportError:
            return False

    @staticmethod
    def compute_shap_drift(
        model: Any,
        reference_X: pd.DataFrame | np.ndarray,
        current_X: pd.DataFrame | np.ndarray,
        features: Sequence[str] | None = None,
    ) -> dict[str, dict[str, float]]:
        """Compute SHAP distribution drift between reference and current data.

        For each feature, computes the Wasserstein distance between
        the reference SHAP value distribution and the current one.

        Parameters
        ----------
        model : fitted model with predict or predict_proba
        reference_X : baseline feature data (DataFrame recommended).
        current_X : current feature data.
        features : feature names (inferred from DataFrame columns if None).

        Returns
        -------
        dict
            ``{feature: {"wasserstein": float, "ref_mean": float, "cur_mean": float}}``

        Raises
        ------
        ImportError
            If shap is not installed.
        """
        if not ShapDriftChecker.is_available():
            raise ImportError(
                "shap library is not installed. Install with: pip install shap. "
                "Layer 3 (SHAP drift) is optional — Layer 1 (PSI) and Layer 2 "
                "(permutation importance) work without it."
            )

        import shap
        from scipy.stats import wasserstein_distance

        ref = _to_dataframe(reference_X, features)
        cur = _to_dataframe(current_X, features)

        # Use TreeExplainer for tree models, KernelExplainer otherwise
        explainer = shap.Explainer(model, ref)
        ref_shap = explainer(ref)
        cur_shap = explainer(cur)

        result: dict[str, dict[str, float]] = {}
        for i, feat in enumerate(ref.columns):
            ref_vals = ref_shap.values[:, i] if ref_shap.values.ndim > 1 else ref_shap.values
            cur_vals = cur_shap.values[:, i] if cur_shap.values.ndim > 1 else cur_shap.values

            ref_flat = np.asarray(ref_vals).ravel()
            cur_flat = np.asarray(cur_vals).ravel()

            result[str(feat)] = {
                "wasserstein": float(wasserstein_distance(ref_flat, cur_flat)),
                "ref_mean": float(np.mean(ref_flat)),
                "cur_mean": float(np.mean(cur_flat)),
            }

        return result


# ---------------------------------------------------------------------------
# PermutationDriftMonitor
# ---------------------------------------------------------------------------


class PermutationDriftMonitor:
    """Permutation importance drift monitor for ML model features.

    Computes permutation importance on reference data, then compares
    against current data to detect features whose predictive contribution
    is decaying.

    Parameters
    ----------
    model : fitted estimator
        A sklearn-compatible model with ``predict`` or ``predict_proba``.
    scoring : str or callable
        Scoring metric (passed to ``permutation_importance``).
        E.g. ``"neg_mean_squared_error"``, ``"r2"``, ``"accuracy"``.
    reference_X : DataFrame or array
        Reference (baseline) feature data.
    reference_y : array-like
        Reference target values.
    features : list[str] or None
        Feature names. Inferred from DataFrame columns if None.
    decay_threshold : float
        Fractional decay to trigger yellow alert. Default 0.50 (50%).
    red_zone : float
        Importance at or below which a feature is in red zone. Default 0.01.
    n_repeats : int
        Number of permutation repeats per feature. Default 5.
    random_state : int
        Random seed for reproducibility. Default 42.

    Example
    -------
    >>> from sklearn.linear_model import LinearRegression
    >>> X = pd.DataFrame({"f1": np.random.randn(200), "f2": np.random.randn(200)})
    >>> y = 2 * X["f1"] + 0.5 * X["f2"] + np.random.randn(200) * 0.1
    >>> model = LinearRegression().fit(X, y)
    >>> mon = PermutationDriftMonitor(model, "r2", X, y, features=["f1", "f2"])
    >>> report = mon.check_importance_drift(X, y)
    >>> print(report.summary())
    """

    def __init__(
        self,
        model: Any,
        scoring: str | Callable,
        reference_X: pd.DataFrame | np.ndarray,
        reference_y: pd.Series | np.ndarray,
        features: Sequence[str] | None = None,
        decay_threshold: float = DEFAULT_DECAY_THRESHOLD,
        red_zone: float = DEFAULT_RED_ZONE_IMPORTANCE,
        n_repeats: int = DEFAULT_N_REPEATS,
        random_state: int = DEFAULT_RANDOM_STATE,
    ) -> None:
        self.model = model
        self.scoring = scoring
        self.features = list(features) if features is not None else _infer_features(reference_X)
        self.decay_threshold = decay_threshold
        self.red_zone = red_zone
        self.n_repeats = n_repeats
        self.random_state = random_state

        # Compute reference importance immediately
        ref_X_df = _to_dataframe(reference_X, self.features)
        self._reference_y = np.asarray(reference_y)

        self._reference_importance = self._compute_importance(ref_X_df, self._reference_y)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_importance_drift(
        self,
        current_X: pd.DataFrame | np.ndarray,
        current_y: pd.Series | np.ndarray,
        features: Sequence[str] | None = None,
    ) -> ImportanceReport:
        """Compare current feature importance against reference baseline.

        Parameters
        ----------
        current_X : DataFrame or array
            Current feature data.
        current_y : array-like
            Current target values.
        features : subset of features to check, or None for all.

        Returns
        -------
        ImportanceReport
        """
        cur_X_df = _to_dataframe(current_X, self.features)
        cur_y = np.asarray(current_y)

        current_importance = self._compute_importance(cur_X_df, cur_y)

        feats = list(features) if features is not None else self.features
        results: list[ImportanceResult] = []

        for f in feats:
            ref_imp = self._reference_importance.get(f, {})
            cur_imp = current_importance.get(f, {})

            ref_mean = ref_imp.get("mean", 0.0)
            ref_std = ref_imp.get("std", 0.0)
            cur_mean = cur_imp.get("mean", 0.0)
            cur_std = cur_imp.get("std", 0.0)

            results.append(
                ImportanceResult(
                    feature=f,
                    reference_importance=ref_mean,
                    current_importance=cur_mean,
                    reference_std=ref_std,
                    current_std=cur_std,
                    decay_threshold=self.decay_threshold,
                    red_zone=self.red_zone,
                )
            )

        return ImportanceReport(results=results)

    def compute_importance(
        self,
        X: pd.DataFrame | np.ndarray,
        y: pd.Series | np.ndarray,
    ) -> dict[str, dict[str, float]]:
        """Compute permutation importance for given data.

        Returns
        -------
        dict
            ``{feature: {"mean": float, "std": float}}``
        """
        return self._compute_importance(_to_dataframe(X, self.features), np.asarray(y))

    @property
    def reference_importance(self) -> dict[str, dict[str, float]]:
        """Reference baseline importance per feature."""
        return dict(self._reference_importance)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_importance(
        self,
        X: pd.DataFrame,
        y: np.ndarray,
    ) -> dict[str, dict[str, float]]:
        """Run sklearn permutation_importance and parse results."""
        if X.empty or len(y) == 0:
            raise ValueError("X and y must be non-empty")

        if len(X) != len(y):
            raise ValueError(f"X has {len(X)} samples but y has {len(y)}")

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*Found large numbers.*",
            )
            result = permutation_importance(
                estimator=self.model,
                X=X,
                y=y,
                scoring=self.scoring,
                n_repeats=self.n_repeats,
                random_state=self.random_state,
                n_jobs=1,
            )

        importance: dict[str, dict[str, float]] = {}
        for i, feat in enumerate(X.columns):
            importance[str(feat)] = {
                "mean": float(result.importances_mean[i]),
                "std": float(result.importances_std[i]),
            }

        return importance


# ---------------------------------------------------------------------------
# Composite drift monitor (Layer 1 + Layer 2 convenience)
# ---------------------------------------------------------------------------


class CompositeDriftMonitor:
    """Combines Layer 1 (PSI/KS distribution drift) with Layer 2 (permutation
    importance drift) into a single check.

    This provides the integration point recommended by SRB-AYU-002:
    run both layers and produce a unified report.

    Parameters
    ----------
    drift_monitor : DriftMonitor
        Layer 1 distribution drift monitor (from srf.drift_monitor).
    importance_monitor : PermutationDriftMonitor
        Layer 2 permutation importance monitor.

    Example
    -------
    >>> from srf.drift_monitor import DriftMonitor
    >>> from srf.permutation_drift import CompositeDriftMonitor
    >>> l1 = DriftMonitor(ref_df, features=["f1", "f2"])
    >>> l2 = PermutationDriftMonitor(model, "r2", ref_X, ref_y)
    >>> composite = CompositeDriftMonitor(l1, l2)
    >>> result = composite.check_all(current_df, current_X, current_y)
    """

    def __init__(
        self,
        drift_monitor: Any,
        importance_monitor: PermutationDriftMonitor,
    ) -> None:
        self.drift_monitor = drift_monitor
        self.importance_monitor = importance_monitor

    def check_all(
        self,
        current_dist_df: pd.DataFrame,
        current_X: pd.DataFrame | np.ndarray,
        current_y: pd.Series | np.ndarray,
    ) -> "CompositeReport":
        """Run both Layer 1 and Layer 2 drift checks.

        Parameters
        ----------
        current_dist_df : DataFrame
            Current feature data for distribution drift (Layer 1).
        current_X : DataFrame or array
            Current feature data for permutation importance (Layer 2).
        current_y : array-like
            Current target values.

        Returns
        -------
        CompositeReport
        """
        dist_report = self.drift_monitor.check_drift(current_dist_df)
        imp_report = self.importance_monitor.check_importance_drift(current_X, current_y)

        return CompositeReport(
            distribution=dist_report,
            importance=imp_report,
        )


@dataclass
class CompositeReport:
    """Combined report from Layer 1 (distribution) and Layer 2 (importance)."""

    distribution: Any  # DriftReport from drift_monitor.py
    importance: ImportanceReport

    @property
    def has_any_drift(self) -> bool:
        """True if either distribution or importance drift is detected."""
        return self.distribution.has_drift or self.importance.has_drift

    def all_alerts(self) -> list[str]:
        """Return human-readable alert strings from both layers."""
        alerts: list[str] = []

        for r in self.distribution.alerts():
            alerts.append(f"[DIST] {r}")

        for r in self.importance.alerts():
            alerts.append(f"[IMP]  {r}")

        return alerts

    def summary(self) -> str:
        lines = [
            "=== Composite Drift Report ===",
            "",
            self.distribution.summary(),
            "",
            self.importance.summary(),
            "",
        ]

        # Surface features flagged by BOTH layers (highest priority)
        dist_features = {r.feature for r in self.distribution.alerts()}
        imp_features = {r.feature for r in self.importance.alerts()}
        both = dist_features & imp_features

        if both:
            lines.append(f"⚠️  Features flagged by BOTH layers: {sorted(both)}")
            lines.append("   These features have both distribution shift AND")
            lines.append("   importance decay — highest priority for review.")
        elif not self.has_any_drift:
            lines.append("✅  No drift detected across either layer.")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_dataframe(
    data: pd.DataFrame | np.ndarray,
    features: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Ensure input is a DataFrame with named columns."""
    if isinstance(data, pd.DataFrame):
        return data

    arr = np.asarray(data)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)

    if features is not None:
        cols = list(features)
        if len(cols) != arr.shape[1]:
            # Pad or truncate column names
            if len(cols) < arr.shape[1]:
                cols = cols + [f"feature_{i}" for i in range(len(cols), arr.shape[1])]
            else:
                cols = cols[: arr.shape[1]]
    else:
        cols = [f"feature_{i}" for i in range(arr.shape[1])]

    return pd.DataFrame(arr, columns=cols)


def _infer_features(data: pd.DataFrame | np.ndarray) -> list[str]:
    """Infer feature names from DataFrame columns or generate defaults."""
    if isinstance(data, pd.DataFrame):
        return list(data.columns)
    arr = np.asarray(data)
    if arr.ndim == 1:
        return ["feature_0"]
    return [f"feature_{i}" for i in range(arr.shape[1])]
