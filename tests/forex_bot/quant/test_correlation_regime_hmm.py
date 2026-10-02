"""Tests for correlation_regime_hmm.py (BQ-1240a).

Covers:
    - FeatureEngineer correctness (vol, correlation, funding)
    - HMM fit/predict (calm → STABLE, crisis → BREAKDOWN)
    - Confidence score range [0, 1]
    - Edge cases (empty, single token, NaN)
    - TRANSITION detection
    - BtcRegimeOverlay backward-compatibility
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest
from quant.correlation_regime_hmm import (
    BREAKDOWN,
    STABLE,
    TRANSITION,
    CorrelationRegimeHMM,
    FeatureEngineer,
    RegimeHistory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def calm_returns() -> pd.DataFrame:
    """Low-volatility, highly correlated returns (STABLE regime)."""
    np.random.seed(42)
    n_hours = 200
    dates = pd.date_range("2026-01-01", periods=n_hours, freq="h")
    base = np.random.normal(0.0001, 0.002, size=n_hours)
    # Three tokens highly correlated with base
    token_a = base + np.random.normal(0, 0.0005, size=n_hours)
    token_b = base + np.random.normal(0, 0.0005, size=n_hours)
    token_c = base + np.random.normal(0, 0.0005, size=n_hours)
    return pd.DataFrame(
        {"BTC": token_a, "ETH": token_b, "SOL": token_c},
        index=dates,
    )


@pytest.fixture()
def crisis_returns() -> pd.DataFrame:
    """High-volatility, decorrelated returns (BREAKDOWN regime)."""
    np.random.seed(99)
    n_hours = 200
    dates = pd.date_range("2026-03-01", periods=n_hours, freq="h")
    token_a = np.random.normal(-0.001, 0.02, size=n_hours)
    token_b = np.random.normal(0.0, 0.025, size=n_hours)
    token_c = np.random.normal(-0.002, 0.03, size=n_hours)
    return pd.DataFrame(
        {"BTC": token_a, "ETH": token_b, "SOL": token_c},
        index=dates,
    )


@pytest.fixture()
def calm_funding() -> pd.DataFrame:
    """Low funding dispersion (STABLE)."""
    np.random.seed(42)
    n_hours = 200
    dates = pd.date_range("2026-01-01", periods=n_hours, freq="h")
    return pd.DataFrame(
        {
            "BTC": np.random.normal(0.0001, 0.00005, size=n_hours),
            "ETH": np.random.normal(0.0001, 0.00005, size=n_hours),
            "SOL": np.random.normal(0.00012, 0.00005, size=n_hours),
        },
        index=dates,
    )


@pytest.fixture()
def crisis_funding() -> pd.DataFrame:
    """High funding dispersion (BREAKDOWN)."""
    np.random.seed(99)
    n_hours = 200
    dates = pd.date_range("2026-03-01", periods=n_hours, freq="h")
    return pd.DataFrame(
        {
            "BTC": np.random.normal(0.001, 0.0005, size=n_hours),
            "ETH": np.random.normal(-0.002, 0.001, size=n_hours),
            "SOL": np.random.normal(0.003, 0.0015, size=n_hours),
        },
        index=dates,
    )


@pytest.fixture()
def fitted_hmm(
    calm_returns: pd.DataFrame,
    crisis_returns: pd.DataFrame,
    calm_funding: pd.DataFrame,
    crisis_funding: pd.DataFrame,
) -> CorrelationRegimeHMM:
    """HMM fitted on combined calm + crisis data."""
    combined_returns = pd.concat([calm_returns, crisis_returns])
    combined_funding = pd.concat([calm_funding, crisis_funding])
    hmm = CorrelationRegimeHMM()
    hmm.fit(combined_returns, combined_funding)
    return hmm


# ---------------------------------------------------------------------------
# FeatureEngineer Tests
# ---------------------------------------------------------------------------


class TestFeatureEngineer:
    """Tests for FeatureEngineer.transform()."""

    def test_realized_vol_low_for_calm(
        self,
        calm_returns: pd.DataFrame,
    ) -> None:
        """Calm data should have low realized volatility."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns)
        assert not features.empty
        # Last 100 rows should be settled (not affected by warmup)
        mean_vol = features["realized_vol"].iloc[-100:].mean()
        assert mean_vol < 1.0, f"Calm vol too high: {mean_vol}"

    def test_realized_vol_high_for_crisis(
        self,
        crisis_returns: pd.DataFrame,
    ) -> None:
        """Crisis data should have high realized volatility."""
        fe = FeatureEngineer()
        features = fe.transform(crisis_returns)
        mean_vol = features["realized_vol"].iloc[-100:].mean()
        assert mean_vol > 1.0, f"Crisis vol too low: {mean_vol}"

    def test_mean_corr_high_for_calm(
        self,
        calm_returns: pd.DataFrame,
    ) -> None:
        """Calm correlated tokens should have positive mean correlation."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns)
        mean_corr = features["mean_corr"].iloc[-100:].mean()
        assert mean_corr > 0.3, f"Calm corr too low: {mean_corr}"

    def test_mean_corr_low_for_crisis(
        self,
        crisis_returns: pd.DataFrame,
    ) -> None:
        """Crisis decorrelated tokens should have low/negative correlation."""
        fe = FeatureEngineer()
        features = fe.transform(crisis_returns)
        mean_corr = features["mean_corr"].iloc[-100:].mean()
        assert mean_corr < 0.3, f"Crisis corr too high: {mean_corr}"

    def test_funding_dispersion_with_data(
        self,
        calm_returns: pd.DataFrame,
        calm_funding: pd.DataFrame,
    ) -> None:
        """Funding dispersion should be computed when funding data provided."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns, calm_funding)
        assert "funding_dispersion" in features.columns
        # Calm funding should have low dispersion
        mean_fd = features["funding_dispersion"].iloc[-100:].mean()
        assert mean_fd >= 0.0
        assert mean_fd < 0.001, f"Calm funding disp too high: {mean_fd}"

    def test_funding_dispersion_zero_without_data(
        self,
        calm_returns: pd.DataFrame,
    ) -> None:
        """Funding dispersion should be 0 when no funding data provided."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns)
        assert (features["funding_dispersion"] == 0.0).all()


# ---------------------------------------------------------------------------
# HMM fit/predict Tests
# ---------------------------------------------------------------------------


class TestCorrelationRegimeHMM:
    """Tests for CorrelationRegimeHMM fit/predict."""

    def test_fit_sets_is_fit(
        self,
        calm_returns: pd.DataFrame,
    ) -> None:
        """fit() should set is_fit property."""
        hmm = CorrelationRegimeHMM()
        assert not hmm.is_fit
        hmm.fit(calm_returns)
        assert hmm.is_fit

    def test_predict_calm_returns_stable(
        self,
        fitted_hmm: CorrelationRegimeHMM,
        calm_returns: pd.DataFrame,
        calm_funding: pd.DataFrame,
    ) -> None:
        """Calm data tail should be classified as STABLE."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns, calm_funding)
        regime, confidence = fitted_hmm.predict_current(features)
        assert regime == STABLE, f"Expected STABLE, got {regime}"
        assert 0.0 <= confidence <= 1.0

    def test_predict_crisis_returns_breakdown(
        self,
        fitted_hmm: CorrelationRegimeHMM,
        crisis_returns: pd.DataFrame,
        crisis_funding: pd.DataFrame,
    ) -> None:
        """Crisis data tail should be classified as BREAKDOWN."""
        fe = FeatureEngineer()
        features = fe.transform(crisis_returns, crisis_funding)
        regime, confidence = fitted_hmm.predict_current(features)
        assert regime == BREAKDOWN, f"Expected BREAKDOWN, got {regime}"
        assert 0.0 <= confidence <= 1.0

    def test_confidence_range(
        self,
        fitted_hmm: CorrelationRegimeHMM,
        calm_returns: pd.DataFrame,
    ) -> None:
        """Confidence should always be in [0, 1]."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns)
        regime, confidence = fitted_hmm.predict_current(features)
        assert 0.0 <= confidence <= 1.0

    def test_predict_before_fit_raises(self) -> None:
        """predict_current() before fit() should raise RuntimeError."""
        hmm = CorrelationRegimeHMM()
        with pytest.raises(RuntimeError, match="not fitted"):
            hmm.predict_current(pd.DataFrame())

    def test_predict_empty_returns_transition(self) -> None:
        """predict_current() on empty features should return TRANSITION."""
        hmm = CorrelationRegimeHMM()
        hmm.fit(pd.DataFrame(np.random.normal(0, 0.01, (50, 3))))
        regime, conf = hmm.predict_current(pd.DataFrame())
        assert regime == TRANSITION

    def test_predict_sequence_returns_list(
        self,
        fitted_hmm: CorrelationRegimeHMM,
        calm_returns: pd.DataFrame,
    ) -> None:
        """predict_sequence() should return one result per row."""
        fe = FeatureEngineer()
        features = fe.transform(calm_returns)
        results = fitted_hmm.predict_sequence(features)
        assert len(results) == len(features)
        for regime, conf in results:
            assert regime in (STABLE, BREAKDOWN, TRANSITION)
            assert 0.0 <= conf <= 1.0

    def test_deterministic_with_seed(
        self,
        calm_returns: pd.DataFrame,
        crisis_returns: pd.DataFrame,
    ) -> None:
        """Same random_state should produce identical predictions."""
        combined = pd.concat([calm_returns, crisis_returns])
        hmm1 = CorrelationRegimeHMM(random_state=42)
        hmm1.fit(combined)
        hmm2 = CorrelationRegimeHMM(random_state=42)
        hmm2.fit(combined)

        fe = FeatureEngineer()
        features = fe.transform(calm_returns)
        r1, _ = hmm1.predict_current(features)
        r2, _ = hmm2.predict_current(features)
        assert r1 == r2


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and robustness."""

    def test_empty_returns(self) -> None:
        """FeatureEngineer on empty DataFrame should return empty features."""
        fe = FeatureEngineer()
        empty = pd.DataFrame()
        features = fe.transform(empty)
        assert features.empty

    def test_single_token(self) -> None:
        """Single-token returns should not crash FeatureEngineer."""
        np.random.seed(1)
        dates = pd.date_range("2026-01-01", periods=50, freq="h")
        single = pd.DataFrame({"BTC": np.random.normal(0, 0.01, 50)}, index=dates)
        fe = FeatureEngineer()
        features = fe.transform(single)
        assert not features.empty
        # Single token → correlation is 0 (no pairs)
        assert (features["mean_corr"] == 0.0).all()

    def test_nan_values(self) -> None:
        """NaN values in returns should be handled gracefully."""
        np.random.seed(1)
        dates = pd.date_range("2026-01-01", periods=100, freq="h")
        data = np.random.normal(0, 0.01, (100, 3))
        data[10:15, 0] = np.nan  # Inject NaNs
        data[50:55, 1] = np.nan
        returns = pd.DataFrame(data, index=dates, columns=["A", "B", "C"])
        fe = FeatureEngineer()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            features = fe.transform(returns)
        assert not features.empty
        # NaNs should be filled, not propagated
        assert not features.isna().any().any()


# ---------------------------------------------------------------------------
# RegimeHistory Tests
# ---------------------------------------------------------------------------


class TestRegimeHistory:
    """Tests for RegimeHistory transition tracking."""

    def test_no_transitions_initially(self) -> None:
        """RegimeHistory starts with zero transitions."""
        history = RegimeHistory()
        assert history.transition_count == 0

    def test_records_transitions(self) -> None:
        """RegimeHistory records regime changes."""
        history = RegimeHistory()
        ts = pd.Timestamp("2026-01-01")
        history.record(ts, STABLE, 0.9)
        history.record(ts, STABLE, 0.85)  # Same → no transition
        assert history.transition_count == 0
        history.record(ts, BREAKDOWN, 0.8)  # Change → transition
        assert history.transition_count == 1
        history.record(ts, STABLE, 0.75)  # Change → transition
        assert history.transition_count == 2

    def test_stable_ratio(self) -> None:
        """stable_ratio returns fraction of transitions to STABLE."""
        history = RegimeHistory()
        ts = pd.Timestamp("2026-01-01")
        history.record(ts, STABLE, 0.9)
        history.record(ts, BREAKDOWN, 0.8)
        history.record(ts, STABLE, 0.75)
        assert history.transition_count == 2
        assert history.stable_ratio() == 0.5


# ---------------------------------------------------------------------------
# BtcRegimeOverlay Integration Tests
# ---------------------------------------------------------------------------


class TestBtcRegimeOverlayIntegration:
    """Tests that BtcRegimeOverlay HMM integration is backward-compatible."""

    def test_overlay_without_hmm(self) -> None:
        """BtcRegimeOverlay works without hmm_signal_source (backward-compat)."""
        from quant.btc_regime_overlay import BtcRegimeOverlay

        # Use non-existent path to test graceful fallback
        overlay = BtcRegimeOverlay(path="/nonexistent/path.jsonl")
        regime = overlay.regime_at_timestamp(1700000000000)
        assert regime == "neutral"

    def test_overlay_accepts_hmm_signal_source(
        self,
        fitted_hmm: CorrelationRegimeHMM,
    ) -> None:
        """BtcRegimeOverlay accepts hmm_signal_source without breaking."""
        from quant.btc_regime_overlay import BtcRegimeOverlay

        overlay = BtcRegimeOverlay(hmm_signal_source=fitted_hmm)
        assert overlay._hmm_source is fitted_hmm

        # regime_with_hmm should return a string
        result = overlay.regime_with_hmm(1700000000000)
        assert isinstance(result, str)
        assert result in (
            "neutral",
            "stable",
            "breakdown",
            "high_vol",
            "trending_up",
            "trending_down",
        )
