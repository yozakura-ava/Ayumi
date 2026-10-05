"""Tests for :mod:`forex_bot.backtest.cpcv` (Sprint C 1b.2).

CPCV = Combinatorial Purged Cross-Validation (Bailey & López de
Prado, AFML Ch. 12).  Card 1436039b-b372-4417-a3e2-5810fe395368
delivers: N contiguous groups, C(N, k) test splits, purge train obs
whose label horizon overlaps test, apply existing embargo_bars,
aggregate to φ = C(N, k) paths.  PBO/DSR on the path distribution,
not a single walk-forward path.

All tests are pure (no Optuna, no market data fetch, no git) so they
stay in the HR5-targeted-only envelope
(``scripts/run_test_scope.sh tests/backtest/test_cpcv.py``).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from forex_bot.backtest.cpcv import (
    MAX_CPCV_PATHS,
    CPCVConfig,
    CPCVInvalidConfig,
    CPCVPBOScore,
    CPCVResult,
    CPCVTooFewBars,
    CPCVTooFewTrials,
    apply_purge_and_embargo,
    compute_dsr_cpcv,
    compute_pbo_cpcv,
    cpcv_paths_count,
    effective_k_for_max_paths,
    list_cpcv_splits,
    run_cpcv,
)
from forex_bot.factory.validation_runner import TrialReturnStore

# ---------------------------------------------------------------------------
# 1. Config validation
# ---------------------------------------------------------------------------


def test_cpcv_config_validates():
    """CPCVConfig rejects out-of-range fields."""
    # k <= 0
    with pytest.raises(CPCVInvalidConfig):
        CPCVConfig(n_groups=6, k_test_groups=0)
    # k >= N
    with pytest.raises(CPCVInvalidConfig):
        CPCVConfig(n_groups=6, k_test_groups=6)
    # N < 2
    with pytest.raises(CPCVInvalidConfig):
        CPCVConfig(n_groups=1, k_test_groups=1)
    # Negative horizon
    with pytest.raises(CPCVInvalidConfig):
        CPCVConfig(n_groups=6, k_test_groups=3, label_horizon_bars=-1)
    # Negative embargo
    with pytest.raises(CPCVInvalidConfig):
        CPCVConfig(n_groups=6, k_test_groups=3, embargo_bars=-1)
    # max_paths < 1
    with pytest.raises(CPCVInvalidConfig):
        CPCVConfig(n_groups=6, k_test_groups=3, max_paths=0)
    # Valid config
    cfg = CPCVConfig(n_groups=6, k_test_groups=3)
    assert cfg.n_groups == 6
    assert cfg.k_test_groups == 3
    assert cfg.label_horizon_bars == 0
    assert cfg.embargo_bars == 0


# ---------------------------------------------------------------------------
# 2. Combinatorics: φ = C(N, k) for N=6,k=3 (20), N=8,k=4 (70)
# ---------------------------------------------------------------------------


def test_cpcv_paths_count_matches_combinatorics():
    """φ = C(N, k) for the card's target grid (N=6,k=3) and (N=8,k=4)."""
    # Card: "N=6-8, ~70 paths" — N=8,k=4 gives 70, N=6,k=3 gives 20.
    assert cpcv_paths_count(6, 3) == 20
    assert cpcv_paths_count(8, 4) == 70
    assert cpcv_paths_count(7, 3) == 35
    # Symmetric: C(N, k) == C(N, N-k)
    assert cpcv_paths_count(8, 3) == cpcv_paths_count(8, 5)
    # Invalid (N, k)
    with pytest.raises(CPCVInvalidConfig):
        cpcv_paths_count(4, 5)


# ---------------------------------------------------------------------------
# 3. Splits are contiguous, disjoint, full-coverage
# ---------------------------------------------------------------------------


def test_cpcv_splits_are_contiguous_and_disjoint():
    """Per-path test is the union of k group blocks; train = complement."""
    rng = np.random.default_rng(seed=42)
    T, N_trials = 60, 3  # 60 bars / 6 groups → 10 bars per group
    matrix = rng.standard_normal((T, N_trials))
    cfg = CPCVConfig(n_groups=6, k_test_groups=3)
    splits = list_cpcv_splits(matrix, cfg)
    assert len(splits) == cpcv_paths_count(6, 3) == 20
    seen_path_ids = set()
    group_size = T // cfg.n_groups  # 10
    for s in splits:
        # path_id is monotonic and unique
        assert s.path_id not in seen_path_ids
        seen_path_ids.add(s.path_id)
        # Test and (raw) train partition [0, usable_T)
        test_set = set(int(i) for i in s.test_indices.tolist())
        train_set = set(int(i) for i in s.train_indices_raw.tolist())
        assert test_set.isdisjoint(train_set)
        assert len(test_set) + len(train_set) == T
        # Test groups are a subset of size k=3
        assert len(s.test_group_indices) == cfg.k_test_groups
        # Per-group block in test_indices is contiguous.  Test_indices
        # is the sorted concatenation of the k test-group blocks, so the
        # only diffs > 1 occur at the boundary between two blocks.
        ts = s.test_indices
        assert ts.size == group_size * cfg.k_test_groups
        # Slice the test fold into per-group blocks (sorted groups) and
        # assert each block is contiguous.
        sorted_test_groups = sorted(s.test_group_indices)
        for idx, g in enumerate(sorted_test_groups):
            block_start = idx * group_size
            block_end = (idx + 1) * group_size
            block = ts[block_start:block_end]
            expected = np.arange(g * group_size, (g + 1) * group_size, dtype=int)
            assert np.array_equal(block, expected), (
                f"per-group block {idx} for group {g} is not contiguous"
            )


# ---------------------------------------------------------------------------
# 4. Purge correctness (label horizon)
# ---------------------------------------------------------------------------


def test_cpcv_purge_drops_label_overlap():
    """Train obs whose label horizon reaches into test are dropped."""
    # Test window: bars 20..29 (10 bars)
    # label_horizon_bars = 5 → drop train bars [16..19] (i.e. 20-5..19)
    #                              because label[i, i+5) overlaps test[20..29]
    # Train candidate: bars [0..19, 30..49]
    train = list(range(0, 20)) + list(range(30, 50))
    test = list(range(20, 30))
    purged = apply_purge_and_embargo(
        train,
        test,
        label_horizon_bars=5,
        embargo_bars=0,
    )
    purged_set = set(int(i) for i in purged.tolist())
    # Pre-purge left side is [0..19]; bars [16,17,18,19] have i + 5 > 20
    for i in range(16, 20):
        assert i not in purged_set, f"bar {i} should have been purged (i+5 > 20)"
    # Bars [0..15] survive purge (i + 5 <= 20)
    for i in range(0, 16):
        assert i in purged_set, f"bar {i} should survive purge"
    # Right side [30..49] untouched
    for i in range(30, 50):
        assert i in purged_set, f"bar {i} should survive purge"


# ---------------------------------------------------------------------------
# 5. Embargo correctness (after purge)
# ---------------------------------------------------------------------------


def test_cpcv_embargo_drops_after_test_set():
    """Embargo drops additional embargo_bars obs to the left of test_min - H."""
    # Test: bars 20..29 (10 bars), test_min = 20.
    # H = 2 → after purge left side is [0..18] (i + 2 ≤ 20 → i ≤ 18).
    # embargo = 3 → on top of purge, drop 3 more: bars [16, 17, 18] dropped → left side becomes [0..15].
    train = list(range(0, 20)) + list(range(30, 50))
    test = list(range(20, 30))
    purged = apply_purge_and_embargo(
        train,
        test,
        label_horizon_bars=2,
        embargo_bars=3,
    )
    purged_set = set(int(i) for i in purged.tolist())
    # 16, 17, 18 dropped by embargo (the 3 leftmost train bars adjacent to test)
    for i in range(16, 19):
        assert i not in purged_set, f"bar {i} should have been embargoed"
    # 15 and below survive
    for i in range(0, 16):
        assert i in purged_set, f"bar {i} should survive"
    # Right side unchanged
    for i in range(30, 50):
        assert i in purged_set


# ---------------------------------------------------------------------------
# 6. No-op pass-through when horizon = embargo = 0
# ---------------------------------------------------------------------------


def test_cpcv_purge_embargo_disabled_passes_through():
    """label_horizon_bars=0 + embargo_bars=0 → purged_train == train (set-equal)."""
    train = list(range(0, 30)) + list(range(50, 80))
    test = list(range(30, 50))
    purged = apply_purge_and_embargo(
        train,
        test,
        label_horizon_bars=0,
        embargo_bars=0,
    )
    assert set(int(i) for i in purged.tolist()) == set(train)


# ---------------------------------------------------------------------------
# 7. PBO aggregation across φ paths
# ---------------------------------------------------------------------------


def test_cpcv_pbo_aggregates_path_breaches():
    """compute_pbo_cpcv returns PBO ∈ [0,1] with n_paths = φ."""
    rng = np.random.default_rng(seed=2026)
    # High-signal matrix: column 0 is best in IS, also best in OOS → low PBO
    T, N_trials = 120, 4
    matrix = np.zeros((T, N_trials))
    base = rng.standard_normal((T, 1)) * 0.5
    # Strategy 0: real positive drift
    matrix[:, 0] = base.flatten() + 0.05
    # Strategies 1-3: pure noise
    matrix[:, 1:] = rng.standard_normal((T, 3)) * 0.5
    cfg = CPCVConfig(n_groups=6, k_test_groups=3, label_horizon_bars=2, embargo_bars=2)
    score = compute_pbo_cpcv(matrix, cfg)
    assert isinstance(score, CPCVPBOScore)
    assert score.n_paths == cpcv_paths_count(6, 3) == 20
    assert 0.0 <= score.pbo <= 1.0
    assert score.n_breaches + (score.n_paths - score.n_breaches) == score.n_paths
    # PBO should be modest on a clear signal — check it's < 0.5
    assert score.pbo < 0.5, f"expected low PBO on clear-signal matrix, got {score.pbo}"


def test_cpcv_pbo_high_for_pure_noise():
    """PBO trends toward 0.5 when IS signal is pure noise (no real edge)."""
    rng = np.random.default_rng(seed=7)
    T, N_trials = 240, 8
    matrix = rng.standard_normal((T, N_trials)) * 0.5  # pure noise, all identical
    cfg = CPCVConfig(n_groups=8, k_test_groups=4, label_horizon_bars=0, embargo_bars=0)
    score = compute_pbo_cpcv(matrix, cfg)
    assert isinstance(score, CPCVPBOScore)
    # Pure noise → PBO near 0.5 (best IS is random)
    assert 0.3 <= score.pbo <= 0.7


# ---------------------------------------------------------------------------
# 8. DSR on the per-path OOS Sharpe distribution
# ---------------------------------------------------------------------------


def test_cpcv_dsr_in_path_distribution():
    """compute_dsr_cpcv uses per-path best-IS OOS Sharpe distribution."""
    rng = np.random.default_rng(seed=11)
    T, N_trials = 240, 4
    matrix = np.zeros((T, N_trials))
    base = rng.standard_normal((T, 1)) * 0.5
    matrix[:, 0] = base.flatten() + 0.05
    matrix[:, 1:] = rng.standard_normal((T, 3)) * 0.5
    cfg = CPCVConfig(n_groups=8, k_test_groups=4, label_horizon_bars=0, embargo_bars=0)
    dsr = compute_dsr_cpcv(matrix, cfg)
    assert dsr is not None
    assert dsr.n_paths == cpcv_paths_count(8, 4) == 70
    assert dsr.effective_n_trials == 70
    assert math.isfinite(dsr.dsr)
    # std should be positive on this signal
    assert dsr.std_oos_sharpe > 0


# ---------------------------------------------------------------------------
# 9. TrialReturnStore integration (the merged real-trial PBO source)
# ---------------------------------------------------------------------------


def test_cpcv_integrates_with_trial_return_store():
    """TrialReturnStore.matrix() is the [T, N_trials] source — CPCV consumes it unchanged."""
    store = TrialReturnStore()
    cell = TrialReturnStore.cell_key("arch1", "EURUSD", "H1")
    rng = np.random.default_rng(seed=99)
    # Record 3 trials of length 240
    for _ in range(3):
        trial = rng.standard_normal(240) * 0.5
        store.record(cell, trial)
    matrix = store.matrix(cell)
    assert matrix is not None
    assert matrix.shape == (240, 3)
    cfg = CPCVConfig(n_groups=6, k_test_groups=3, label_horizon_bars=2, embargo_bars=2)
    result = run_cpcv(matrix, cfg)
    assert isinstance(result, CPCVResult)
    assert result.matrix_shape == (240, 3)
    # 20 paths × purge + embargo should evaluate successfully
    assert result.n_paths() == 20
    assert result.pbo is not None
    assert result.dsr is not None


# ---------------------------------------------------------------------------
# 10. Input matrix validation
# ---------------------------------------------------------------------------


def test_cpcv_validates_input_matrix():
    """Matrix [T, N] with N<2 or T<4 raises the right error."""
    # N < 2
    one_col = np.zeros((100, 1))
    with pytest.raises(CPCVTooFewTrials):
        list_cpcv_splits(one_col, CPCVConfig(n_groups=6, k_test_groups=3))
    # T < 4
    too_short = np.zeros((2, 3))
    with pytest.raises(CPCVTooFewBars):
        list_cpcv_splits(too_short, CPCVConfig(n_groups=6, k_test_groups=3))


# ---------------------------------------------------------------------------
# 11. MAX_CPCV_PATHS greedy reduction
# ---------------------------------------------------------------------------


def test_cpcv_phi_fits_max_paths():
    """When C(N, k) > MAX_CPCV_PATHS, runner reduces k greedily to fit."""
    # C(20, 10) = 184756 > 65536.  Greedy reduction:
    #   C(20, 9) = 167960  > 65536, k=9→8
    #   C(20, 8) = 125970  > 65536, k=8→7
    #   C(20, 7) = 77520   > 65536, k=7→6
    #   C(20, 6) = 38760  ≤ 65536, stop → k_eff = 6.
    cfg = CPCVConfig(n_groups=20, k_test_groups=10, max_paths=MAX_CPCV_PATHS)
    assert cfg.effective_k == 6
    assert cpcv_paths_count(20, cfg.effective_k) == 38760
    # effective_k_for_max_paths helper agrees
    assert effective_k_for_max_paths(20, 10, MAX_CPCV_PATHS) == 6
    # Run on a small matrix — must evaluate without error
    rng = np.random.default_rng(seed=314)
    matrix = rng.standard_normal((400, 3))  # 20 bars per group
    splits = list_cpcv_splits(matrix, cfg)
    assert len(splits) == cpcv_paths_count(20, 6)


# ---------------------------------------------------------------------------
# Bonus: end-to-end smoke on the card's target grid
# ---------------------------------------------------------------------------


def test_cpcv_card_target_grid_n6_to_n8():
    """Card target: N=6..8 → 20..70 paths.  Run all three sizes on a 240-bar matrix."""
    rng = np.random.default_rng(seed=20261005)
    T, N_trials = 240, 5
    matrix = rng.standard_normal((T, N_trials)) * 0.5
    # Card's pipeline_config embargo_bars per TF (H1=24, H4=6, D1=1) — verify
    # the largest (H1=24) yields sensible purged-train sizes.
    for n, k, horizon, embargo in [(6, 3, 2, 24), (7, 4, 1, 6), (8, 4, 1, 1)]:
        cfg = CPCVConfig(
            n_groups=n,
            k_test_groups=k,
            label_horizon_bars=horizon,
            embargo_bars=embargo,
        )
        result = run_cpcv(matrix, cfg)
        assert result.n_paths() == cpcv_paths_count(n, k)
        assert result.pbo is not None
        assert result.dsr is not None

