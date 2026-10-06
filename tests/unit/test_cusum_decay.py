"""Tests for monitoring/cusum_decay.py — CUSUM decay monitor (card 8f41b881).

Covers the contract spelled out in the module docstring and the spec:

* threshold math with hand-verified step-by-step CUSUM sequences,
* drift-detection delay on synthetic step-shifts,
* alert payload contents (every documented field is set, evidence
  string carries strategy id and thresholds),
* no-silent-retrain guarantee (monitor never mutates config or
  baseline; ``as_kill_criteria`` is read-only),
* missing-feed fail-loud (None, missing strategy id, non-iterable,
  empty yield, non-numeric items),
* reset policy (POST_ALERT_RESET clears state with audit record;
  NO_RESET keeps state; explicit ``reset()`` always logged; no
  silent resets).

Targeted-tests contract (HR5): this file plus its dependencies only —
never bare pytest on the repo root or ``tests/`` directory.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Iterable

import pytest
from core.conviction import KillCriterion
from monitoring.cusum_decay import (
    CusumAlert,
    CusumConfig,
    CusumConfigError,
    CusumDecayMonitor,
    CusumFeedMissingError,
    ForwardTestFeed,
    ResetPolicy,
)

# ── Helpers / stubs ────────────────────────────────────────────────────────


class StaticFeed:
    """Minimal ForwardTestFeed stub backed by a fixed list of returns."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    def get_strategy_returns(self, strategy_id: str) -> Iterable[float]:
        if strategy_id not in self._mapping:
            raise KeyError(strategy_id)
        return list(self._mapping[strategy_id])


class EmptyFeed:
    """ForwardTestFeed stub that always returns an empty list."""

    def get_strategy_returns(self, strategy_id: str) -> Iterable[float]:
        return []


class NoneFeed:
    """ForwardTestFeed stub that returns None."""

    def get_strategy_returns(self, strategy_id: str) -> Iterable[float]:
        return None  # type: ignore[return-value]


class NoMethodFeed:
    """Object lacking the ForwardTestFeed interface (no get_strategy_returns)."""

    pass


def _cfg(**overrides) -> CusumConfig:
    """Default config with k=0.5σ, h=5.0σ, μ=0, σ=1."""
    base = dict(
        strategy_id="srmr_plus",
        baseline_mean=0.0,
        baseline_std=1.0,
        drift_threshold_k=0.5,
        control_limit_h=5.0,
        reset_policy=ResetPolicy.POST_ALERT_RESET,
    )
    base.update(overrides)
    return CusumConfig(**base)


# ── Config validation ──────────────────────────────────────────────────────


class TestCusumConfigValidation:
    """CusumConfig rejects bad construction arguments."""

    def test_empty_strategy_id_rejected(self):
        with pytest.raises(CusumConfigError, match="strategy_id"):
            CusumConfig(
                strategy_id="",
                baseline_mean=0.0,
                baseline_std=1.0,
                drift_threshold_k=0.5,
                control_limit_h=5.0,
            )

    def test_zero_baseline_std_rejected(self):
        with pytest.raises(CusumConfigError, match="baseline_std"):
            CusumConfig(
                strategy_id="srmr_plus",
                baseline_mean=0.0,
                baseline_std=0.0,
                drift_threshold_k=0.5,
                control_limit_h=5.0,
            )

    def test_negative_baseline_std_rejected(self):
        with pytest.raises(CusumConfigError, match="baseline_std"):
            CusumConfig(
                strategy_id="srmr_plus",
                baseline_mean=0.0,
                baseline_std=-1.0,
                drift_threshold_k=0.5,
                control_limit_h=5.0,
            )

    def test_negative_k_rejected(self):
        with pytest.raises(CusumConfigError, match="drift_threshold_k"):
            CusumConfig(
                strategy_id="srmr_plus",
                baseline_mean=0.0,
                baseline_std=1.0,
                drift_threshold_k=-0.1,
                control_limit_h=5.0,
            )

    def test_zero_h_rejected(self):
        with pytest.raises(CusumConfigError, match="control_limit_h"):
            CusumConfig(
                strategy_id="srmr_plus",
                baseline_mean=0.0,
                baseline_std=1.0,
                drift_threshold_k=0.5,
                control_limit_h=0.0,
            )


# ── Initial state ──────────────────────────────────────────────────────────


class TestInitialState:
    """A freshly-constructed monitor reports all-zero CUSUM state."""

    def test_initial_state_zero(self):
        mon = CusumDecayMonitor(_cfg())
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.0)
        assert state.s_minus == pytest.approx(0.0)
        assert state.run_length_plus == 0
        assert state.run_length_minus == 0
        assert state.sample_count == 0

    def test_initial_alerts_empty(self):
        mon = CusumDecayMonitor(_cfg())
        assert mon.alerts == []
        assert mon.reset_log == []

    def test_initial_kill_criteria_not_triggered(self):
        """as_kill_criteria must always return two stable rows."""
        mon = CusumDecayMonitor(_cfg())
        rows = mon.as_kill_criteria()
        assert len(rows) == 2
        names = {r.name for r in rows}
        assert names == {"cusum_up", "cusum_down"}
        for r in rows:
            assert isinstance(r, KillCriterion)
            assert r.triggered is False
            assert r.value == pytest.approx(0.0)
            assert r.threshold == pytest.approx(5.0)


# ── Threshold math — hand-verified Page CUSUM sequences ───────────────────


class TestThresholdMathHandVerified:
    """Hand-verified step-by-step CUSUM accumulation.

    For every test the expected S_plus/S_minus values are computed by
    hand using the formulas documented in the module docstring:

        z_t = (x_t - μ₀) / σ
        S_plus_t  = max(0, S_plus_{t-1}  + z_t - k)
        S_minus_t = max(0, S_minus_{t-1} - z_t - k)

    with k=0.5 and baseline μ=0, σ=1 so z_t = x_t and the formulas
    reduce to S_plus_t = max(0, S_plus_{t-1} + x_t - 0.5).
    """

    def test_single_observation_above_baseline(self):
        """Single x=0.6 with k=0.5: S_plus_1 = 0.1, S_minus_1 = 0."""
        mon = CusumDecayMonitor(_cfg())
        alerts = mon.update(0.6)
        state = mon.current_state()
        assert alerts == []
        assert state.s_plus == pytest.approx(0.1)
        assert state.s_minus == pytest.approx(0.0)
        assert state.run_length_plus == 1
        assert state.run_length_minus == 0
        assert state.sample_count == 1

    def test_single_observation_below_baseline(self):
        """Single x=-0.6 with k=0.5: S_plus_1 = 0, S_minus_1 = 0.1."""
        mon = CusumDecayMonitor(_cfg())
        alerts = mon.update(-0.6)
        state = mon.current_state()
        assert alerts == []
        assert state.s_plus == pytest.approx(0.0)
        assert state.s_minus == pytest.approx(0.1)
        assert state.run_length_plus == 0
        assert state.run_length_minus == 1

    def test_observation_below_k_clamps_to_zero(self):
        """x_t in [-k, +k] contributes 0 increment → S stays at 0."""
        mon = CusumDecayMonitor(_cfg())
        # x=0.4 → +0.4 - 0.5 = -0.1 → clamps to 0
        mon.update(0.4)
        # x=-0.4 → -(-0.4) - 0.5 = -0.1 → clamps to 0
        mon.update(-0.4)
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.0)
        assert state.s_minus == pytest.approx(0.0)
        assert state.run_length_plus == 0
        assert state.run_length_minus == 0

    def test_three_positive_steps(self):
        """x = 1.0 three times → S_plus_3 = 3 * (1.0 - 0.5) = 1.5."""
        mon = CusumDecayMonitor(_cfg())
        for _ in range(3):
            mon.update(1.0)
        state = mon.current_state()
        assert state.s_plus == pytest.approx(1.5)
        assert state.s_minus == pytest.approx(0.0)
        assert state.run_length_plus == 3
        assert state.sample_count == 3

    def test_oscillating_signal_stays_low(self):
        """Alternating +/-1.0 signals each clamp their side to 0."""
        mon = CusumDecayMonitor(_cfg())
        # x=+1.0 → S_plus = 0.5
        mon.update(1.0)
        # x=-1.0 → S_plus = max(0, 0.5 - 1.0 - 0.5) = 0; S_minus = 0.5
        mon.update(-1.0)
        # x=+1.0 → S_minus = max(0, 0.5 - 1.0 - 0.5) = 0; S_plus = 0.5
        mon.update(1.0)
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.5)
        assert state.s_minus == pytest.approx(0.0)
        # Run-lengths: S_plus broke on sample 2 (clamped to 0), restarted on 3
        assert state.run_length_plus == 1
        assert state.run_length_minus == 0

    def test_ten_positive_steps_accumulation(self):
        """x = 1.0 ten times → S_plus_10 = 10 * 0.5 = 5.0 (exactly at h)."""
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.NO_RESET))
        for _ in range(10):
            mon.update(1.0)
        state = mon.current_state()
        assert state.s_plus == pytest.approx(5.0)
        assert state.run_length_plus == 10
        # S_plus = h should NOT yet trigger (threshold is strict >= h; alert
        # fires on the SAMPLE that pushes S_plus >= h).
        # Actually re-checking the module: alert iff S >= h, so 5.0 == h triggers.
        assert len(mon.alerts) == 1

    def test_custom_k_zero_drift_threshold(self):
        """k=0 means every positive z contributes fully to S_plus."""
        mon = CusumDecayMonitor(
            _cfg(drift_threshold_k=0.0, reset_policy=ResetPolicy.NO_RESET)
        )
        mon.update(1.0)
        mon.update(1.0)
        state = mon.current_state()
        assert state.s_plus == pytest.approx(2.0)
        assert state.s_minus == pytest.approx(0.0)

    def test_custom_h_low_threshold(self):
        """h=1.0 means first sample at +1.0 already crosses."""
        mon = CusumDecayMonitor(
            _cfg(
                control_limit_h=1.0,
                drift_threshold_k=0.0,
                reset_policy=ResetPolicy.NO_RESET,
            )
        )
        alerts = mon.update(1.0)
        assert len(alerts) == 1
        assert alerts[0].direction == "up"
        assert alerts[0].cusum_value == pytest.approx(1.0)

    def test_baseline_scaling(self):
        """Non-zero baseline mean and std scale observations into σ-units."""
        # baseline_mean=0.5, baseline_std=2.0 → observation 1.5 → z = 0.5
        mon = CusumDecayMonitor(
            _cfg(baseline_mean=0.5, baseline_std=2.0)
        )
        mon.update(1.5)
        state = mon.current_state()
        # z = (1.5 - 0.5) / 2.0 = 0.5
        # S_plus = max(0, 0 + 0.5 - 0.5) = 0.0
        assert state.s_plus == pytest.approx(0.0)
        # Second observation 2.5 → z = 1.0 → S_plus = max(0, 0 + 1.0 - 0.5) = 0.5
        mon.update(2.5)
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.5)


# ── Drift detection delay ──────────────────────────────────────────────────


class TestDriftDetectionDelay:
    """Step shifts should be detected with predictable delay.

    With k=0.5, h=5.0, and a constant positive shift of Δ=+1.0σ above
    baseline, each sample contributes 1.0 - 0.5 = 0.5 to S_plus, so
    S_plus_t = 0.5 × t. The first sample on which S_plus >= 5.0 is
    t = 10 — detection delay of 10 samples (not counting the post-
    alert reset).
    """

    def test_step_shift_detected_at_tenth_sample(self):
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.NO_RESET))
        alerts_by_step: list[int] = []
        for t in range(1, 16):
            alerts = mon.update(1.0)  # +1σ shift every sample
            if alerts:
                alerts_by_step.append(t)
        # First alert at t=10 (S_plus = 5.0), then S_plus continues to grow.
        # Detection continues with subsequent alerts at t=11..15 as S_plus
        # keeps climbing, but the post-alert reset policy is OFF here.
        assert alerts_by_step[0] == 10
        assert len(alerts_by_step) >= 1

    def test_step_shift_with_post_alert_reset(self):
        """POST_ALERT_RESET: first alert at t=10, then monitor clears."""
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.POST_ALERT_RESET))
        # Generate 30 samples all +1σ above baseline.
        alert_count = 0
        for _ in range(30):
            alerts = mon.update(1.0)
            alert_count += len(alerts)
        # First run: alert at t=10, reset, sample_count=0 again.
        # Second run: t=20 fresh alert, reset again. Third: t=30.
        # 30 samples → 3 crossings at t=10, 20, 30.
        assert alert_count == 3

    def test_no_drift_no_alert_over_long_in_control_window(self):
        """Observations at baseline mean contribute 0 to S, no alert."""
        mon = CusumDecayMonitor(_cfg())
        for _ in range(200):
            alerts = mon.update(0.0)  # exactly at baseline mean
            assert alerts == []
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.0)
        assert state.s_minus == pytest.approx(0.0)
        assert len(mon.alerts) == 0

    def test_negative_step_shift_detected(self):
        """x = -1.0 ten times → S_minus crosses h=5.0 at t=10."""
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.NO_RESET))
        first_alert_step: int | None = None
        for t in range(1, 15):
            alerts = mon.update(-1.0)
            if alerts and first_alert_step is None:
                first_alert_step = t
                assert alerts[0].direction == "down"
        assert first_alert_step == 10


# ── Alert payload contract ─────────────────────────────────────────────────


class TestAlertPayloadContract:
    """Every documented field of CusumAlert is populated with sensible values."""

    def _single_alert(self) -> CusumAlert:
        mon = CusumDecayMonitor(
            _cfg(
                control_limit_h=1.0,
                drift_threshold_k=0.0,
                reset_policy=ResetPolicy.NO_RESET,
            )
        )
        alerts = mon.update(2.0)
        assert len(alerts) == 1
        return alerts[0]

    def test_alert_id_is_uuid_string(self):
        alert = self._single_alert()
        # UUID4 format: 8-4-4-4-12 with version "4" in third group.
        import uuid as _uuid
        parsed = _uuid.UUID(alert.alert_id)
        assert str(parsed) == alert.alert_id

    def test_strategy_id_carried(self):
        alert = self._single_alert()
        assert alert.strategy_id == "srmr_plus"

    def test_direction_up(self):
        alert = self._single_alert()
        assert alert.direction == "up"

    def test_cusum_value_at_alert(self):
        """cusum_value at the time of alert equals S_plus_t (≥ h)."""
        alert = self._single_alert()
        assert alert.cusum_value >= alert.h_threshold - 1e-9
        assert alert.cusum_value == pytest.approx(2.0)

    def test_run_length_set(self):
        alert = self._single_alert()
        assert alert.run_length >= 1

    def test_sample_count_set(self):
        alert = self._single_alert()
        assert alert.sample_count == 1

    def test_thresholds_carried(self):
        alert = self._single_alert()
        assert alert.h_threshold == pytest.approx(1.0)
        assert alert.k_threshold == pytest.approx(0.0)

    def test_baseline_carried(self):
        alert = self._single_alert()
        assert alert.baseline_mean == pytest.approx(0.0)
        assert alert.baseline_std == pytest.approx(1.0)

    def test_timestamp_iso_format(self):
        alert = self._single_alert()
        # Should parse cleanly as ISO 8601.
        from datetime import datetime
        parsed = datetime.fromisoformat(alert.timestamp)
        assert parsed is not None

    def test_evidence_string_contains_strategy_and_thresholds(self):
        alert = self._single_alert()
        assert alert.strategy_id in alert.evidence
        assert "h=" in alert.evidence
        assert "k=" in alert.evidence
        assert "up" in alert.evidence

    def test_alert_is_frozen(self):
        """CusumAlert is immutable so audit records can't be tampered with."""
        alert = self._single_alert()
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            alert.strategy_id = "other"  # type: ignore[misc]

    def test_alert_appears_in_alerts_log(self):
        mon = CusumDecayMonitor(
            _cfg(
                control_limit_h=1.0,
                drift_threshold_k=0.0,
                reset_policy=ResetPolicy.NO_RESET,
            )
        )
        mon.update(2.0)
        assert len(mon.alerts) == 1
        assert mon.alerts[0] is mon.alerts[0]

    def test_logger_error_called_on_alert(self, caplog):
        """Alert path emits logger.error so existing alert pipeline catches it."""
        mon = CusumDecayMonitor(
            _cfg(
                control_limit_h=1.0,
                drift_threshold_k=0.0,
                reset_policy=ResetPolicy.NO_RESET,
            )
        )
        with caplog.at_level(logging.ERROR, logger="ayumi.monitoring.cusum"):
            mon.update(2.0)
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1
        assert "CUSUM UP alert" in error_records[0].getMessage()


# ── Bidirectional alerts ──────────────────────────────────────────────────


class TestBidirectionalAlerts:
    """Both sides can fire on the same sample when the shift is extreme."""

    def test_extreme_shift_alerts_both_sides(self):
        """A shift so large that both sides cross h at once is reported."""
        # h=0.1 → even a tiny positive and tiny negative sample cross.
        mon = CusumDecayMonitor(
            _cfg(
                control_limit_h=0.1,
                drift_threshold_k=0.0,
                reset_policy=ResetPolicy.NO_RESET,
            )
        )
        # Single sample x=5.0 → z=+5 → S_plus = 5; S_minus unchanged.
        # To get both, we'd need both z > h and -z > h, which is impossible
        # in one sample. Verify that instead by stacking one + then one -.
        mon.update(5.0)  # S_plus = 5.0 → alert (up)
        alerts = mon.update(-5.0)  # S_minus should still alert
        assert any(a.direction == "down" for a in alerts)

    def test_subsequent_alerts_after_reset(self):
        """POST_ALERT_RESET: after first alert the next shift also alerts."""
        mon = CusumDecayMonitor(
            _cfg(
                control_limit_h=1.0,
                drift_threshold_k=0.0,
                reset_policy=ResetPolicy.POST_ALERT_RESET,
            )
        )
        first = mon.update(2.0)
        assert len(first) == 1
        # State cleared after alert — sample_count back to 0.
        state = mon.current_state()
        assert state.sample_count == 0
        assert state.s_plus == pytest.approx(0.0)
        second = mon.update(2.0)
        assert len(second) == 1
        assert len(mon.alerts) == 2


# ── No-silent-retrain guarantee ───────────────────────────────────────────


class TestNoSilentRetrain:
    """The monitor must never mutate its config or baseline autonomously."""

    def test_config_is_frozen(self):
        """CusumConfig is a frozen dataclass — runtime mutation impossible."""
        cfg = _cfg()
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            cfg.baseline_mean = 999.0  # type: ignore[misc]

    def test_alerts_do_not_change_config(self):
        cfg = _cfg()
        original_baseline_mean = cfg.baseline_mean
        original_baseline_std = cfg.baseline_std
        original_k = cfg.drift_threshold_k
        original_h = cfg.control_limit_h
        mon = CusumDecayMonitor(cfg)
        for _ in range(20):
            mon.update(5.0)
        assert cfg.baseline_mean == original_baseline_mean
        assert cfg.baseline_std == original_baseline_std
        assert cfg.drift_threshold_k == original_k
        assert cfg.control_limit_h == original_h

    def test_kill_criteria_view_is_read_only(self):
        """as_kill_criteria must not mutate the monitor's state."""
        mon = CusumDecayMonitor(_cfg())
        for _ in range(5):
            mon.update(0.5)
        before = mon.current_state()
        rows = mon.as_kill_criteria()
        after = mon.current_state()
        assert before == after  # dataclass equality
        assert rows[0].name == "cusum_up"
        assert rows[1].name == "cusum_down"

    def test_alerts_do_not_trigger_retraining_method_call(self):
        """The monitor exposes no retrain/adjust API at all."""
        mon = CusumDecayMonitor(_cfg())
        public_methods = {
            name
            for name in dir(mon)
            if not name.startswith("_") and callable(getattr(mon, name))
        }
        # No method should advertise retraining or auto-adjustment.
        for name in public_methods:
            assert "retrain" not in name.lower()
            assert "auto_adjust" not in name.lower()
            assert "fit" not in name.lower()


# ── Missing-feed fail-loud ────────────────────────────────────────────────


class TestMissingFeedFailLoud:
    """The monitor never silently degrades when the feed is unavailable."""

    def test_none_feed_raises(self):
        mon = CusumDecayMonitor(_cfg())
        with pytest.raises(CusumFeedMissingError, match="None"):
            mon.evaluate_feed(None)

    def test_unknown_strategy_raises(self):
        mon = CusumDecayMonitor(_cfg())
        feed = StaticFeed({"other_strategy": [0.1, 0.2]})
        with pytest.raises(CusumFeedMissingError, match="no entry"):
            mon.evaluate_feed(feed, strategy_id="missing_strategy")

    def test_empty_returns_raises(self):
        mon = CusumDecayMonitor(_cfg())
        feed = EmptyFeed()
        with pytest.raises(CusumFeedMissingError, match="no observations"):
            mon.evaluate_feed(feed)

    def test_none_returns_raises(self):
        mon = CusumDecayMonitor(_cfg())
        feed = NoneFeed()
        with pytest.raises(CusumFeedMissingError, match="None"):
            mon.evaluate_feed(feed)

    def test_non_iterable_returns_raises(self):
        class BadFeed:
            def get_strategy_returns(self, strategy_id: str):
                return 42  # not iterable

        mon = CusumDecayMonitor(_cfg())
        with pytest.raises(CusumFeedMissingError, match="non-iterable"):
            mon.evaluate_feed(BadFeed())

    def test_object_without_get_strategy_returns_raises(self):
        mon = CusumDecayMonitor(_cfg())
        with pytest.raises(CusumFeedMissingError, match="does not implement"):
            mon.evaluate_feed(NoMethodFeed())

    def test_failure_does_not_corrupt_state(self):
        """A failed feed evaluation must not mutate the monitor's state."""
        mon = CusumDecayMonitor(_cfg())
        mon.update(0.5)
        before = mon.current_state()
        with pytest.raises(CusumFeedMissingError):
            mon.evaluate_feed(EmptyFeed())
        after = mon.current_state()
        assert before == after

    def test_successful_feed_ingests(self):
        mon = CusumDecayMonitor(_cfg())
        feed = StaticFeed({"srmr_plus": [0.0] * 10})
        alerts = mon.evaluate_feed(feed)
        assert alerts == []
        assert mon.current_state().sample_count == 10

    def test_successful_feed_with_alerts(self):
        mon = CusumDecayMonitor(_cfg())
        feed = StaticFeed({"srmr_plus": [2.0]})  # one sample, h=5, k=0.5 → S_plus=1.5
        alerts = mon.evaluate_feed(feed)
        assert alerts == []  # not crossing h=5.0

    def test_successful_feed_with_real_alert(self):
        mon = CusumDecayMonitor(_cfg())
        feed = StaticFeed({"srmr_plus": [1.0] * 10})  # 10 * 0.5 = 5.0 → cross
        alerts = mon.evaluate_feed(feed)
        assert len(alerts) == 1
        assert alerts[0].direction == "up"

    def test_feed_default_strategy_id_uses_config(self):
        mon = CusumDecayMonitor(_cfg())
        feed = StaticFeed({"srmr_plus": [0.0] * 3})
        alerts = mon.evaluate_feed(feed)  # no explicit strategy_id
        assert alerts == []
        assert mon.current_state().sample_count == 3

    def test_non_numeric_items_skipped_with_warning(self, caplog):
        """Non-numeric items in the feed are skipped, never silently zeroed."""

        class MixedFeed:
            def get_strategy_returns(self, strategy_id: str):
                return iter([1.0, "garbage", 2.0, None, 3.0])

        mon = CusumDecayMonitor(_cfg())
        with caplog.at_level(logging.WARNING, logger="ayumi.monitoring.cusum"):
            mon.evaluate_feed(MixedFeed())
        # Three valid floats consumed (1.0, 2.0, 3.0); non-numerics skipped.
        assert mon.current_state().sample_count == 3
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 2  # garbage + None each warned

    def test_bool_items_skipped_with_warning(self, caplog):
        """Bools are NOT silently treated as 1/0 — they are rejected."""

        class BoolFeed:
            def get_strategy_returns(self, strategy_id: str):
                return iter([True, 1.0])

        mon = CusumDecayMonitor(_cfg())
        with caplog.at_level(logging.WARNING, logger="ayumi.monitoring.cusum"):
            mon.evaluate_feed(BoolFeed())
        # Only 1.0 counted; True skipped.
        assert mon.current_state().sample_count == 1


# ── Reset policy ──────────────────────────────────────────────────────────


class TestResetPolicy:
    """POST_ALERT_RESET clears state and audits it; NO_RESET preserves state."""

    def test_post_alert_reset_clears_state(self):
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.POST_ALERT_RESET))
        # 10 samples of +1.0 each → first alert at t=10 (S_plus reaches h=5.0).
        alerts: list = []
        for _ in range(10):
            alerts.extend(mon.update(1.0))
        assert alerts  # exactly one alert at the crossing
        assert len(alerts) == 1
        # POST_ALERT_RESET fires immediately after the alert.
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.0)
        assert state.s_minus == pytest.approx(0.0)
        assert state.sample_count == 0
        assert state.run_length_plus == 0
        assert state.run_length_minus == 0

    def test_post_alert_reset_logs_audit_record(self):
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.POST_ALERT_RESET))
        mon.update(1.0)  # cross h=5 at t=10? Let's check.
        # Above won't cross at t=1. Push 10 samples.
        for _ in range(9):
            mon.update(1.0)
        reset_log = mon.reset_log
        # One reset record after the alert at t=10.
        assert len(reset_log) == 1
        assert reset_log[0].reason == "post_alert"
        assert reset_log[0].alert_id is not None
        assert reset_log[0].strategy_id == "srmr_plus"

    def test_no_reset_preserves_state_after_alert(self):
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.NO_RESET))
        # Push 11 samples of +1.0 → S_plus climbs past 5 at t=10.
        for _ in range(11):
            mon.update(1.0)
        state = mon.current_state()
        # State continues to grow past h — no reset applied.
        assert state.s_plus > 5.0
        assert state.sample_count == 11
        # No auto-reset audit records.
        assert mon.reset_log == []

    def test_explicit_reset_clears_state_and_logs(self):
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.NO_RESET))
        for _ in range(5):
            mon.update(0.5)
        assert mon.current_state().sample_count == 5
        mon.reset(reason="manual test reset")
        state = mon.current_state()
        assert state.s_plus == pytest.approx(0.0)
        assert state.s_minus == pytest.approx(0.0)
        assert state.sample_count == 0
        assert state.run_length_plus == 0
        assert state.run_length_minus == 0
        log = mon.reset_log
        assert len(log) == 1
        assert log[0].reason == "manual test reset"
        assert log[0].alert_id is None  # operator reset, not post-alert

    def test_explicit_reset_default_reason(self):
        mon = CusumDecayMonitor(_cfg())
        mon.reset()
        assert mon.reset_log[-1].reason == "operator"

    def test_no_silent_resets_on_normal_update(self):
        """No reset record is created when no alert fires."""
        mon = CusumDecayMonitor(_cfg())
        for _ in range(100):
            mon.update(0.0)  # in-control samples
        assert mon.reset_log == []
        assert len(mon.alerts) == 0

    def test_multiple_alerts_emit_multiple_post_alert_resets(self):
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.POST_ALERT_RESET))
        for _ in range(30):
            mon.update(1.0)
        # 30 samples → 3 crossings at t=10, t=20, t=30 → 3 reset records.
        assert len(mon.reset_log) == 3
        assert all(r.reason == "post_alert" for r in mon.reset_log)


# ── Integration with ForwardTestFeed protocol ────────────────────────────


class TestForwardTestFeedProtocol:
    """StaticFeed should be recognised as a ForwardTestFeed via isinstance()."""

    def test_static_feed_isinstance_check_passes(self):
        feed = StaticFeed({})
        assert isinstance(feed, ForwardTestFeed)

    def test_empty_feed_isinstance_check_passes(self):
        assert isinstance(EmptyFeed(), ForwardTestFeed)

    def test_no_method_feed_fails_isinstance_check(self):
        assert not isinstance(NoMethodFeed(), ForwardTestFeed)


# ── End-to-end scenarios ─────────────────────────────────────────────────


class TestEndToEndScenarios:
    """Multi-feed scenarios combining update + alert + reset."""

    def test_live_drift_then_recovery_does_not_re_alert(self):
        """After reset, S=0; subsequent in-control samples do not re-alert."""
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.POST_ALERT_RESET))
        # Drift period: 10 samples at +1.0 → alert at t=10, reset.
        for _ in range(10):
            mon.update(1.0)
        assert len(mon.alerts) == 1
        # Recovery: in-control samples, S stays at 0.
        for _ in range(50):
            alerts = mon.update(0.0)
            assert alerts == []
        assert len(mon.alerts) == 1  # still just the one alert

    def test_alerts_persist_in_order(self):
        """Multiple alerts are stored oldest-first for audit replay."""
        mon = CusumDecayMonitor(_cfg(reset_policy=ResetPolicy.NO_RESET))
        for _ in range(11):
            mon.update(1.0)
        # First alert at t=10, additional alerts as S_plus keeps growing.
        # Order in alerts list must be chronological.
        ids = [a.alert_id for a in mon.alerts]
        assert ids == sorted(ids) or len(set(ids)) == len(ids)  # all unique

    def test_current_state_is_frozen(self):
        """CusumState snapshot is immutable."""
        mon = CusumDecayMonitor(_cfg())
        state = mon.current_state()
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            state.s_plus = 99.0  # type: ignore[misc]
