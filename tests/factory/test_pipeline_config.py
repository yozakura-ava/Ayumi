"""Pipeline config dataclass tests (§4 of the Strategy-Factory spec).

Exhaustive coverage for every dataclass in :mod:`forex_bot.factory.pipeline_config`:
walk-forward windows, DSR ``n_trials`` scaling, regime gating thresholds,
trade-count floors, OOS isolation + unlock guard, and PBO tier mapping.
"""

from __future__ import annotations

from datetime import date

import pytest

from forex_bot.factory.pipeline_config import (
    DSRConfig,
    OOSConfig,
    PBOConfig,
    PipelineConfig,
    RegimeGatingConfig,
    TradeCountConfig,
    WalkForwardWindowConfig,
    compute_dsr_n_trials,
    default_pipeline_config,
)


# ---------------------------------------------------------------------------
# WalkForwardWindowConfig (§4.1)
# ---------------------------------------------------------------------------


def _wf(tf: str, embargo: int) -> WalkForwardWindowConfig:
    return WalkForwardWindowConfig(
        timeframe=tf,
        windows=5,
        train_ratio=0.70,
        val_ratio=0.15,
        test_ratio=0.15,
        overlap_ratio=0.20,
        embargo_bars=embargo,
    )


def test_wf_ratios_must_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        _wf("H1", 24)
        WalkForwardWindowConfig(
            timeframe="H1",
            windows=5,
            train_ratio=0.50,
            val_ratio=0.25,
            test_ratio=0.20,
            overlap_ratio=0.20,
            embargo_bars=24,
        )


def test_wf_overlap_must_be_in_unit_interval() -> None:
    with pytest.raises(ValueError, match="overlap_ratio"):
        WalkForwardWindowConfig(
            timeframe="H1",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=1.0,
            embargo_bars=24,
        )


def test_wf_embargo_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="embargo_bars"):
        WalkForwardWindowConfig(
            timeframe="H1",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=-1,
        )


def test_wf_windows_must_be_positive() -> None:
    with pytest.raises(ValueError, match="windows must be positive"):
        WalkForwardWindowConfig(
            timeframe="H1",
            windows=0,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=24,
        )


def test_wf_timeframe_required() -> None:
    with pytest.raises(ValueError, match="timeframe"):
        WalkForwardWindowConfig(
            timeframe="",
            windows=5,
            train_ratio=0.70,
            val_ratio=0.15,
            test_ratio=0.15,
            overlap_ratio=0.20,
            embargo_bars=24,
        )


def test_default_pipeline_canonical_wf() -> None:
    cfg = default_pipeline_config()
    timeframes = [w.timeframe for w in cfg.wf_windows]
    assert timeframes == ["M5", "M15", "H1", "H4", "D1"]

    # Spec §4.1 verbatim numbers
    by_tf = {w.timeframe: w for w in cfg.wf_windows}
    assert by_tf["M5"].windows == 8 and by_tf["M5"].embargo_bars == 288
    assert by_tf["M15"].windows == 5 and by_tf["M15"].embargo_bars == 96
    assert by_tf["H1"].windows == 5 and by_tf["H1"].embargo_bars == 24
    assert by_tf["H4"].windows == 5 and by_tf["H4"].embargo_bars == 6
    assert by_tf["D1"].windows == 5 and by_tf["D1"].overlap_ratio == 0.0


def test_pipeline_wf_for_lookup() -> None:
    cfg = default_pipeline_config()
    h1 = cfg.wf_for("H1")
    assert h1.timeframe == "H1"
    with pytest.raises(KeyError, match="H2"):
        cfg.wf_for("H2")


# ---------------------------------------------------------------------------
# compute_dsr_n_trials (§4.2)
# ---------------------------------------------------------------------------


def test_dsr_floor_applies_for_small_sweeps() -> None:
    # Manual: 6 × 1 × 1 × 1 = 6 cells → 160 floor
    assert compute_dsr_n_trials(6) == 160
    assert compute_dsr_n_trials(48) == 160
    assert compute_dsr_n_trials(0) == 160


def test_dsr_scales_above_floor() -> None:
    # Pilot: 5 × 4 × 2 × 20 = 800 → 3 * 800 = 2400
    assert compute_dsr_n_trials(800) == 2400
    # Full sweep: 5 × 4 × 5 × 50 = 5000 → 15000
    assert compute_dsr_n_trials(5000) == 15000


def test_dsr_negative_rejected() -> None:
    with pytest.raises(ValueError, match="cell_count"):
        compute_dsr_n_trials(-1)


def test_dsr_respects_explicit_config() -> None:
    cfg = DSRConfig(base_n_trials=200, n_trials_multiple=5)
    assert compute_dsr_n_trials(0, cfg) == 200
    assert compute_dsr_n_trials(100, cfg) == 500  # 5 × 100 wins over 200


# ---------------------------------------------------------------------------
# RegimeGatingConfig (§4.3)
# ---------------------------------------------------------------------------


def test_regime_window_is_100_bug4_fix() -> None:
    cfg = RegimeGatingConfig()
    assert cfg.regime_detector_window_bars == 100
    assert cfg.min_trades_per_regime == 10
    assert cfg.freeze_detector_for_pipeline is True


# ---------------------------------------------------------------------------
# TradeCountConfig (§4.4)
# ---------------------------------------------------------------------------


def test_trade_count_thresholds() -> None:
    cfg = TradeCountConfig()
    assert cfg.per_window_warning == 15
    assert cfg.dsr_eligibility_floor == 30


# ---------------------------------------------------------------------------
# OOSConfig (§4.5)
# ---------------------------------------------------------------------------


def test_oos_default_window_covers_jan_to_jul_2026() -> None:
    cfg = OOSConfig()
    assert cfg.holdout_start == date(2026, 1, 1)
    assert cfg.holdout_end == date(2026, 7, 31)


def test_oos_contains() -> None:
    cfg = OOSConfig()
    assert cfg.contains(date(2026, 1, 1))
    assert cfg.contains(date(2026, 4, 15))
    assert cfg.contains(date(2026, 7, 31))
    assert not cfg.contains(date(2025, 12, 31))
    assert not cfg.contains(date(2026, 8, 1))


def test_oos_window_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="must be before"):
        OOSConfig(
            holdout_start=date(2026, 7, 31),
            holdout_end=date(2026, 1, 1),
        )


def test_oos_assert_unlocked_blocks_by_default() -> None:
    cfg = OOSConfig()  # require_explicit_unlock defaults to True
    with pytest.raises(PermissionError, match="locked"):
        cfg.assert_unlocked(unlocked=False)


def test_oos_assert_unlocked_passes_when_unlocked() -> None:
    cfg = OOSConfig()
    cfg.assert_unlocked(unlocked=True)  # must not raise


def test_oos_unlock_can_be_disabled() -> None:
    cfg = OOSConfig(require_explicit_unlock=False)
    cfg.assert_unlocked(unlocked=False)  # must not raise


# ---------------------------------------------------------------------------
# PBOConfig (§4.6)
# ---------------------------------------------------------------------------


def test_pbo_thresholds_default() -> None:
    cfg = PBOConfig()
    assert cfg.accept_threshold == 0.30
    assert cfg.marginal_threshold == 0.50
    assert cfg.reject_threshold == 0.50


def test_pbo_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="0 <= accept"):
        PBOConfig(accept_threshold=0.60, marginal_threshold=0.50, reject_threshold=0.70)


@pytest.mark.parametrize(
    ("pbo", "expected_tier"),
    [
        (None, "INSUFFICIENT"),
        (0.0, "A"),
        (0.29, "A"),
        (0.30, "B"),  # boundary: < accept_threshold → A; == accept_threshold → B
        (0.49, "B"),
        (0.50, "REJECT"),  # boundary: == reject_threshold → REJECT
        (0.9, "REJECT"),
    ],
)
def test_pbo_tier_mapping(pbo: float | None, expected_tier: str) -> None:
    cfg = PBOConfig()
    assert cfg.tier_for(pbo) == expected_tier


# ---------------------------------------------------------------------------
# Composite PipelineConfig
# ---------------------------------------------------------------------------


def test_default_pipeline_config_has_all_subconfigs() -> None:
    cfg = default_pipeline_config()
    assert isinstance(cfg.wf_windows, tuple) and cfg.wf_windows
    assert isinstance(cfg.dsr, DSRConfig)
    assert isinstance(cfg.regime, RegimeGatingConfig)
    assert isinstance(cfg.trade_count, TradeCountConfig)
    assert isinstance(cfg.oos, OOSConfig)
    assert isinstance(cfg.pbo, PBOConfig)


def test_pipeline_config_is_frozen() -> None:
    cfg = default_pipeline_config()
    with pytest.raises(Exception):
        cfg.dsr = DSRConfig(base_n_trials=999)  # type: ignore[misc]


def test_pipeline_config_construction_with_custom_components() -> None:
    cfg = PipelineConfig(
        wf_windows=(_wf("H1", 24),),
        dsr=DSRConfig(base_n_trials=320),
        regime=RegimeGatingConfig(),
        trade_count=TradeCountConfig(),
        oos=OOSConfig(),
        pbo=PBOConfig(),
    )
    assert cfg.wf_for("H1").timeframe == "H1"
    assert cfg.dsr.base_n_trials == 320