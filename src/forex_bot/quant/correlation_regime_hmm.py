"""Correlation Regime HMM (BQ-1240a).

2-state GaussianHMM (STABLE/BREAKDOWN) on hourly crypto returns.
Uses diagonal covariance for numerical stability.

Features:
    - Realized volatility (24h rolling, annualized)
    - Mean pairwise cross-token correlation
    - Funding rate dispersion (std across tokens)

Usage:
    from quant.correlation_regime_hmm import CorrelationRegimeHMM, FeatureEngineer

    fe = FeatureEngineer()
    features = fe.transform(returns_df, funding_df)
    hmm = CorrelationRegimeHMM()
    hmm.fit(returns_df, funding_df)
    regime, confidence = hmm.predict_current(features)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

logger = logging.getLogger(__name__)

# Regime labels
STABLE = "STABLE"
BREAKDOWN = "BREAKDOWN"
TRANSITION = "TRANSITION"

# Posterior probability threshold for confident classification
_CONFIDENCE_THRESHOLD = 0.65

# Annualization factor for hourly returns (sqrt of 8760 hours/year)
_ANNUALIZATION = np.sqrt(8760)


@dataclass
class RegimeTransition:
    """Single regime transition event."""

    timestamp: pd.Timestamp
    from_regime: str
    to_regime: str
    confidence: float


@dataclass
class RegimeHistory:
    """Tracks regime transitions over time."""

    transitions: list[RegimeTransition] = field(default_factory=list)
    _last_regime: str | None = None

    def record(
        self,
        timestamp: pd.Timestamp,
        regime: str,
        confidence: float,
    ) -> None:
        """Record a regime observation, logging transitions."""
        if self._last_regime is not None and regime != self._last_regime:
            self.transitions.append(
                RegimeTransition(
                    timestamp=timestamp,
                    from_regime=self._last_regime,
                    to_regime=regime,
                    confidence=confidence,
                ),
            )
        self._last_regime = regime

    @property
    def transition_count(self) -> int:
        """Total number of regime transitions observed."""
        return len(self.transitions)

    def stable_ratio(self) -> float:
        """Fraction of transitions that entered STABLE (0.0–1.0)."""
        if not self.transitions:
            return 0.0
        return sum(1 for t in self.transitions if t.to_regime == STABLE) / len(
            self.transitions,
        )


class FeatureEngineer:
    """Compute HMM input features from market data.

    Features produced:
        - ``realized_vol``: 24h rolling volatility × sqrt(8760) (annualized)
        - ``mean_corr``: mean pairwise Pearson correlation across tokens
        - ``funding_dispersion``: std of funding rates across tokens
    """

    def __init__(self, vol_window: int = 24) -> None:
        self.vol_window = vol_window

    def transform(
        self,
        returns_df: pd.DataFrame,
        funding_df: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Transform raw returns and funding data into HMM features.

        Args:
            returns_df: DataFrame with datetime index, columns are tokens,
                values are hourly returns (float).
            funding_df: Optional DataFrame with same structure as returns_df,
                containing funding rates. If None, funding_dispersion = 0.

        Returns:
            DataFrame with columns: realized_vol, mean_corr, funding_dispersion.
        """
        if returns_df.empty:
            return pd.DataFrame(
                columns=["realized_vol", "mean_corr", "funding_dispersion"],
            )

        # Realized volatility: cross-sectional mean of per-token rolling vol
        per_token_vol = returns_df.rolling(
            window=self.vol_window,
            min_periods=1,
        ).std()
        # Mean across tokens → single vol series, annualized
        realized_vol = per_token_vol.mean(axis=1) * _ANNUALIZATION

        # Mean pairwise correlation across tokens (rolling 24h)
        mean_corr = self._rolling_mean_correlation(returns_df, self.vol_window)

        # Funding dispersion: std across tokens at each timestamp
        if funding_df is not None and not funding_df.empty:
            funding_dispersion = funding_df.std(axis=1).fillna(0.0)
        else:
            funding_dispersion = pd.Series(
                0.0,
                index=returns_df.index,
            )

        features = pd.DataFrame(
            {
                "realized_vol": realized_vol.fillna(0.0),
                "mean_corr": mean_corr.fillna(0.0),
                "funding_dispersion": funding_dispersion.fillna(0.0),
            },
            index=returns_df.index,
        )

        return features

    @staticmethod
    def _rolling_mean_correlation(
        returns_df: pd.DataFrame,
        window: int,
    ) -> pd.Series:
        """Compute rolling mean pairwise correlation across tokens.

        For each window, computes the mean of all unique pairwise Pearson
        correlations between tokens.
        """
        n_tokens = returns_df.shape[1]

        if n_tokens < 2:
            return pd.Series(0.0, index=returns_df.index)

        # Rolling correlation matrix
        rolling_corr = returns_df.rolling(window=window, min_periods=2).corr()

        # Extract upper triangle mean (excluding diagonal)
        result = []
        for ts in returns_df.index:
            if ts not in rolling_corr.index.get_level_values(0):
                result.append(0.0)
                continue
            corr_matrix = rolling_corr.loc[ts]
            if corr_matrix.empty or corr_matrix.shape[0] < 2:
                result.append(0.0)
                continue
            # Upper triangle indices (excluding diagonal)
            upper_mask = np.triu(
                np.ones(corr_matrix.shape, dtype=bool),
                k=1,
            )
            upper_vals = corr_matrix.values[upper_mask]
            # Filter NaN
            valid = upper_vals[~np.isnan(upper_vals)]
            result.append(float(np.mean(valid)) if len(valid) > 0 else 0.0)

        return pd.Series(result, index=returns_df.index)


class CorrelationRegimeHMM:
    """2-state GaussianHMM for crypto market regime detection.

    States:
        - State 0 → STABLE (low volatility, high correlation)
        - State 1 → BREAKDOWN (high volatility, correlation breakdown)

    When posterior probability < 0.65, regime is TRANSITION.
    """

    def __init__(
        self,
        n_states: int = 2,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD,
        random_state: int = 42,
    ) -> None:
        self.n_states = n_states
        self.confidence_threshold = confidence_threshold
        self.random_state = random_state
        self._model: GaussianHMM | None = None
        self._feature_engineer = FeatureEngineer()
        self._is_fit = False

        # Map HMM state indices to regime labels (assigned after fit)
        self._state_labels: list[str] = []

    def fit(
        self,
        returns_df: pd.DataFrame,
        funding_df: pd.DataFrame | None = None,
    ) -> "CorrelationRegimeHMM":
        """Fit the HMM on historical returns and funding data.

        Args:
            returns_df: DataFrame with datetime index, token columns,
                hourly returns.
            funding_df: Optional funding rates DataFrame (same structure).

        Returns:
            self (for chaining).
        """
        features = self._feature_engineer.transform(returns_df, funding_df)

        if features.empty:
            raise ValueError("Cannot fit HMM on empty features")

        X = features.values.astype(np.float64)

        # Replace any inf/nan
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        self._model = GaussianHMM(
            n_components=self.n_states,
            covariance_type="diag",
            random_state=self.random_state,
            n_iter=100,
        )
        self._model.fit(X)

        # Assign state labels: the state with higher mean vol is BREAKDOWN
        means = self._model.means_  # shape (n_states, n_features)
        # Feature 0 is realized_vol
        vol_by_state = means[:, 0]
        if vol_by_state[0] <= vol_by_state[-1]:
            # State 0 = lower vol = STABLE, last state = BREAKDOWN
            self._state_labels = [STABLE, BREAKDOWN]
            if self.n_states > 2:
                self._state_labels = (
                    [
                        STABLE,
                    ]
                    + [TRANSITION] * (self.n_states - 2)
                    + [BREAKDOWN]
                )
        else:
            self._state_labels = [BREAKDOWN, STABLE]
            if self.n_states > 2:
                self._state_labels = (
                    [
                        BREAKDOWN,
                    ]
                    + [TRANSITION] * (self.n_states - 2)
                    + [STABLE]
                )

        self._is_fit = True
        logger.info(
            "HMM fit complete. State means (vol): %s. Labels: %s",
            vol_by_state.tolist(),
            self._state_labels,
        )
        return self

    def predict_current(
        self,
        features: pd.DataFrame,
    ) -> tuple[Literal["STABLE", "BREAKDOWN", "TRANSITION"], float]:
        """Predict current regime from feature DataFrame.

        Args:
            features: DataFrame from FeatureEngineer.transform().

        Returns:
            (regime_label, confidence) where confidence is the max
            posterior probability [0.0, 1.0].
        """
        if not self._is_fit or self._model is None:
            raise RuntimeError("HMM not fitted. Call fit() first.")

        if features.empty:
            return TRANSITION, 0.0

        X = features.values.astype(np.float64)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # Get posterior probabilities for each state
        posteriors = self._model.predict_proba(X)  # shape (n_samples, n_states)
        last_posteriors = posteriors[-1]  # most recent observation

        best_state = int(np.argmax(last_posteriors))
        confidence = float(last_posteriors[best_state])

        if confidence < self.confidence_threshold:
            return TRANSITION, confidence

        return self._state_labels[best_state], confidence  # type: ignore[return-value]

    def predict_sequence(
        self,
        features: pd.DataFrame,
    ) -> list[tuple[str, float]]:
        """Predict regime for each row in features.

        Args:
            features: DataFrame from FeatureEngineer.transform().

        Returns:
            List of (regime, confidence) tuples, one per row.
        """
        if not self._is_fit or self._model is None:
            raise RuntimeError("HMM not fitted. Call fit() first.")

        if features.empty:
            return []

        X = features.values.astype(np.float64)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        posteriors = self._model.predict_proba(X)

        results: list[tuple[str, float]] = []
        for row_posteriors in posteriors:
            best_state = int(np.argmax(row_posteriors))
            confidence = float(row_posteriors[best_state])
            if confidence < self.confidence_threshold:
                results.append((TRANSITION, confidence))
            else:
                results.append((self._state_labels[best_state], confidence))

        return results

    @property
    def is_fit(self) -> bool:
        """Whether the HMM has been fitted."""
        return self._is_fit
