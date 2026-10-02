"""Feature drift detection — Layer 1: PSI/KS distribution monitor.

Monitors distribution shift in signal_engine features using:
- PSI (Population Stability Index) — bin-level distribution comparison
- KS (Kolmogorov-Smirnov) test — non-parametric distribution test via scipy.stats

Usage:
    from srf.drift_monitor import DriftMonitor

    monitor = DriftMonitor(reference_df, features=["ict_confluence_score", ...])
    result = monitor.check_drift(current_df)
    for r in result.alerts():
        print(r)

Threshold defaults:
- PSI > 0.2  → significant drift
- KS p-value < 0.05 → distribution mismatch
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PSI_THRESHOLD = 0.2
DEFAULT_KS_ALPHA = 0.05
DEFAULT_N_BINS = 10
DEFAULT_EPS = 1e-6  # floor for bin proportions to avoid log(0) / div-by-zero


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftResult:
    """Single-feature drift assessment."""

    feature: str
    psi: float
    ks_statistic: float
    ks_pvalue: float
    psi_threshold: float
    ks_alpha: float

    @property
    def psi_drift(self) -> bool:
        """True if PSI exceeds the configured threshold."""
        return self.psi > self.psi_threshold

    @property
    def ks_drift(self) -> bool:
        """True if KS test rejects the null hypothesis (distributions differ)."""
        return self.ks_pvalue < self.ks_alpha

    @property
    def is_drifted(self) -> bool:
        """True if either PSI or KS indicates drift."""
        return self.psi_drift or self.ks_drift

    def severity(self) -> str:
        """Human-readable severity bucket based on PSI.

        Industry conventions:
          PSI < 0.1  → no significant shift
          0.1–0.25  → moderate shift, investigate
          > 0.25    → significant shift, action required
        """
        if self.psi < 0.1:
            return "none"
        elif self.psi < 0.25:
            return "moderate"
        else:
            return "high"

    def __str__(self) -> str:
        flag = "⚠️ DRIFT" if self.is_drifted else "✅ OK"
        return (
            f"{flag}  {self.feature}: "
            f"PSI={self.psi:.4f} (>{self.psi_threshold}) "
            f"KS={self.ks_statistic:.4f} (p={self.ks_pvalue:.4f}, α={self.ks_alpha}) "
            f"severity={self.severity()}"
        )


@dataclass
class DriftReport:
    """Aggregate drift report across all monitored features."""

    results: list[DriftResult] = field(default_factory=list)

    def alerts(self) -> list[DriftResult]:
        """Return only results where drift was detected."""
        return [r for r in self.results if r.is_drifted]

    @property
    def has_drift(self) -> bool:
        return any(r.is_drifted for r in self.results)

    def summary(self) -> str:
        n_total = len(self.results)
        n_drift = len(self.alerts())
        lines = [
            f"Drift Report: {n_drift}/{n_total} features drifted",
        ]
        if n_drift > 0:
            lines.append("  Alerts:")
            for r in self.alerts():
                lines.append(f"    {r}")
        else:
            lines.append("  All features stable.")
        return "\n".join(lines)

    def __iter__(self):
        return iter(self.results)


# ---------------------------------------------------------------------------
# PSI computation
# ---------------------------------------------------------------------------


def compute_psi(
    reference: pd.Series | np.ndarray,
    current: pd.Series | np.ndarray,
    n_bins: int = DEFAULT_N_BINS,
    eps: float = DEFAULT_EPS,
) -> float:
    """Compute Population Stability Index (PSI).

    PSI measures how much a variable's distribution has shifted
    between a reference period and a current period.

    Formula:
        PSI = Σ (cᵢ - rᵢ) × ln(cᵢ / rᵢ)

    where rᵢ and cᵢ are the proportions of observations in bin i
    for the reference and current distributions respectively.

    Parameters
    ----------
    reference : array-like
        Reference (expected / training-time) distribution.
    current : array-like
        Current (observed / production) distribution.
    n_bins : int
        Number of bins for the histogram. Default 10.
    eps : float
        Floor value for proportions to avoid log(0) and division by zero.

    Returns
    -------
    float
        PSI value. Interpretation:
        < 0.1  → no significant shift
        0.1–0.25 → moderate shift
        > 0.25 → significant shift
    """
    ref = _to_numpy(reference)
    cur = _to_numpy(current)

    if len(ref) == 0 or len(cur) == 0:
        raise ValueError("reference and current must be non-empty")

    # Use reference distribution to define bin edges, then extend to ±inf
    # so all current values fall into a bin.
    _, edges = np.histogram(ref, bins=n_bins)
    edges[0] = -np.inf
    edges[-1] = np.inf

    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)

    ref_prop = ref_counts / len(ref)
    cur_prop = cur_counts / len(cur)

    # Floor to avoid log(0)
    ref_prop = np.maximum(ref_prop, eps)
    cur_prop = np.maximum(cur_prop, eps)

    psi = float(np.sum((cur_prop - ref_prop) * np.log(cur_prop / ref_prop)))
    return psi


# ---------------------------------------------------------------------------
# KS test wrapper
# ---------------------------------------------------------------------------


def compute_ks_test(
    reference: pd.Series | np.ndarray,
    current: pd.Series | np.ndarray,
) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov test.

    Parameters
    ----------
    reference : array-like
        Reference distribution.
    current : array-like
        Current distribution.

    Returns
    -------
    (statistic, pvalue)
        statistic — KS test statistic (max CDF difference).
        pvalue — two-tailed p-value. Reject H₀ (same distribution) if < α.
    """
    ref = _to_numpy(reference)
    cur = _to_numpy(current)

    if len(ref) == 0 or len(cur) == 0:
        raise ValueError("reference and current must be non-empty")

    result = sp_stats.ks_2samp(ref, cur)
    return float(result.statistic), float(result.pvalue)


# ---------------------------------------------------------------------------
# DriftMonitor
# ---------------------------------------------------------------------------


class DriftMonitor:
    """Distribution drift monitor for signal_engine features.

    Holds reference distributions and compares current data against them.

    Parameters
    ----------
    reference : pd.DataFrame
        Reference (training / baseline) feature data.
    features : list[str] or None
        Feature column names to monitor. If None, all numeric columns
        in *reference* are used.
    psi_threshold : float
        PSI value above which a feature is flagged as drifted. Default 0.2.
    ks_alpha : float
        Significance level for the KS test. Default 0.05.
    n_bins : int
        Number of bins for PSI histogram. Default 10.

    Example
    -------
    >>> import pandas as pd
    >>> ref = pd.DataFrame({"f1": np.random.randn(1000), "f2": np.random.randn(1000)})
    >>> cur = pd.DataFrame({"f1": np.random.randn(500), "f2": np.random.randn(500) * 2})
    >>> mon = DriftMonitor(ref, features=["f1", "f2"])
    >>> report = mon.check_drift(cur)
    >>> _ = [print(r) for r in report]
    """

    def __init__(
        self,
        reference: pd.DataFrame,
        features: Sequence[str] | None = None,
        psi_threshold: float = DEFAULT_PSI_THRESHOLD,
        ks_alpha: float = DEFAULT_KS_ALPHA,
        n_bins: int = DEFAULT_N_BINS,
    ) -> None:
        if reference.empty:
            raise ValueError("reference DataFrame must not be empty")

        if features is None:
            features = list(reference.select_dtypes(include=[np.number]).columns)

        if not features:
            raise ValueError("no numeric features found in reference data")

        # Validate that all requested features exist in reference
        missing = [f for f in features if f not in reference.columns]
        if missing:
            raise KeyError(f"features not in reference: {missing}")

        # Drop NaN from reference per feature
        self._reference: dict[str, np.ndarray] = {}
        for f in features:
            col = reference[f].dropna().to_numpy(dtype=float)
            if len(col) == 0:
                raise ValueError(f"feature '{f}' has no non-NaN values in reference")
            self._reference[f] = col

        self.features: list[str] = list(features)
        self.psi_threshold = psi_threshold
        self.ks_alpha = ks_alpha
        self.n_bins = n_bins

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_drift(
        self,
        current: pd.DataFrame,
        features: Sequence[str] | None = None,
    ) -> DriftReport:
        """Compare current feature distributions against reference.

        Parameters
        ----------
        current : pd.DataFrame
            Current feature data (must contain the monitored columns).
        features : list[str] or None
            Subset of features to check. If None, all monitored features.

        Returns
        -------
        DriftReport
        """
        feats = list(features) if features is not None else self.features

        # Validate current data has requested features
        missing = [f for f in feats if f not in current.columns]
        if missing:
            raise KeyError(f"features not in current data: {missing}")

        results: list[DriftResult] = []
        for f in feats:
            ref_arr = self._reference[f]
            cur_series = current[f].dropna()
            if cur_series.empty:
                # No current data — can't compute drift, return NaN result
                results.append(
                    DriftResult(
                        feature=f,
                        psi=float("nan"),
                        ks_statistic=float("nan"),
                        ks_pvalue=float("nan"),
                        psi_threshold=self.psi_threshold,
                        ks_alpha=self.ks_alpha,
                    )
                )
                continue

            cur_arr = cur_series.to_numpy(dtype=float)
            psi = compute_psi(ref_arr, cur_arr, n_bins=self.n_bins)
            ks_stat, ks_p = compute_ks_test(ref_arr, cur_arr)

            results.append(
                DriftResult(
                    feature=f,
                    psi=psi,
                    ks_statistic=ks_stat,
                    ks_pvalue=ks_p,
                    psi_threshold=self.psi_threshold,
                    ks_alpha=self.ks_alpha,
                )
            )

        return DriftReport(results=results)

    def add_reference_samples(self, feature: str, samples: np.ndarray) -> None:
        """Append additional reference samples for a feature.

        Useful for incrementally building the baseline.
        """
        if feature not in self._reference:
            raise KeyError(f"unknown feature '{feature}'")
        samples = np.asarray(samples, dtype=float)
        self._reference[feature] = np.concatenate([self._reference[feature], samples])

    @property
    def reference_sizes(self) -> dict[str, int]:
        """Sample counts per feature in the reference set."""
        return {f: len(arr) for f, arr in self._reference.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_numpy(data: pd.Series | np.ndarray | Sequence) -> np.ndarray:
    """Convert input to a 1-D float numpy array, dropping NaN."""
    if isinstance(data, pd.Series):
        arr = data.dropna().to_numpy(dtype=float)
    elif isinstance(data, np.ndarray):
        arr = data[~np.isnan(data)].astype(float)
    else:
        arr = np.asarray(data, dtype=float)
        arr = arr[~np.isnan(arr)]
    return arr
