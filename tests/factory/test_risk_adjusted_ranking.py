"""Tests for :mod:`forex_bot.factory.risk_adjusted_ranking` (Sprint C 1b.3).

Coverage matrix (card cc90a6b6):

* **BH threshold math** — known-equivalence rejects pure noise at the
  documented BH inequality ``p_(k) <= alpha * k / m`` (Benjamini &
  Hochberg 1995, Section 3).
* **Monotonic p-value ordering** — q-values are monotonically
  non-decreasing from the largest to the smallest (Storey 2002).
* **Tie handling** — tied p-values share rejection status (BH §3) and
  the rank output remains deterministic.
* **Empty/degenerate trial sets** — short/zero-variance/NaN trials emit
  ``bh_reject=True`` and are demoted below BH-rejected strategies; an
  empty input returns ``[]``.
* **Ranking consumes TrialReturnStore** —
  :func:`rank_from_trial_return_store` walks a real
  :class:`TrialReturnStore` instance and emits per-cell rankings whose
  ``candidate_id``s match the columns the store recorded.
* **Raw-return sort path removed/raises** — the previous tournament
  ``rank_scorecard_rows`` function is now a loud-failure shim that
  raises :exc:`RawReturnSortRemoved` (and its downstream callers
  migrate to the factory module).

All tests are pure (no Optuna, no market data fetch, no git) so they
stay in the HR5-targeted-only envelope (``scripts/run_test_scope.sh``).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pytest

from forex_bot.factory.risk_adjusted_ranking import (
    DEFAULT_FDR_ALPHA,
    MIN_CELL_TRIALS,
    MIN_TRIAL_BARS,
    RawReturnSortRemoved,
    RiskAdjustedRankingError,
    benjamini_hochberg,
    one_sample_t_pvalue,
    rank_candidates_by_trial_returns,
    rank_from_trial_return_store,
)
from forex_bot.factory.validation_runner import TrialReturnStore

# ── Helpers ──────────────────────────────────────────────────────────────


def _rng(seed: int) -> np.random.Generator:
    """Deterministic RNG so the BH math tests are reproducible."""
    return np.random.default_rng(seed)


def _returns_with_mean(
    mean: float,
    *,
    std: float = 0.01,
    n: int = 200,
    seed: int = 0,
) -> np.ndarray:
    """Per-bar returns with the requested mean and std (no autocorrelation)."""
    return _rng(seed).normal(loc=mean, scale=std, size=n)


# ── BH threshold math ─────────────────────────────────────────────────────


class TestBHThresholdMath:
    """Known-equivalence tests against the BH step-up inequality."""

    def test_no_rejection_when_all_pvalues_above_line(self) -> None:
        # m=5, alpha=0.05 -> line is (0.01, 0.02, 0.03, 0.04, 0.05).
        # All five p-values are above the line at their rank, so BH
        # rejects nothing.  This is the "all noise" case — the ranker
        # must NOT promote any of them.
        p = [0.06, 0.07, 0.08, 0.09, 0.10]
        res = benjamini_hochberg(p, alpha=0.05)
        assert res.n_tests == 5
        assert res.n_rejected == 0
        assert res.threshold_index == -1
        assert not res.rejected.any()
        # q-values fall back to raw p-values when no rejection.
        np.testing.assert_allclose(res.p_adjusted, p)

    def test_full_rejection_when_all_pvalues_below_line(self) -> None:
        # m=5, alpha=0.05 -> line is (0.01, 0.02, 0.03, 0.04, 0.05).
        # All five p-values are well below the line at their rank.
        p = [1e-6, 1e-6, 1e-6, 1e-6, 1e-6]
        res = benjamini_hochberg(p, alpha=0.05)
        assert res.n_rejected == 5
        assert res.threshold_index == 4
        assert res.rejected.all()
        # q-values are clamped to <= alpha=0.05 by the Storey step.
        assert (res.p_adjusted <= 0.05).all()

    def test_partial_rejection_with_known_stepup(self) -> None:
        # m=5, alpha=0.05 -> line is (0.01, 0.02, 0.03, 0.04, 0.05).
        # Sorted ascending: 0.001, 0.02, 0.04, 0.06, 0.10
        #   p_(1)=0.001 <= 0.01 ✓
        #   p_(2)=0.02  <= 0.02 ✓ (boundary, step-up accepts ties)
        #   p_(3)=0.04  <= 0.03 ✗  -> threshold cuts at k=2.
        # BH rejects the first 2; the rest are not discoveries.
        p = [0.04, 0.10, 0.001, 0.06, 0.02]
        res = benjamini_hochberg(p, alpha=0.05)
        assert res.n_rejected == 2
        assert res.threshold_index == 1
        # Rejection mask back-projected to original indices.
        # Sorted order is (2, 4, 0, 3, 1); threshold=1 means rejected = {2, 4}.
        assert sorted(np.flatnonzero(res.rejected).tolist()) == [2, 4]
        # Non-rejected stay with their raw scaled p-value.
        assert not res.rejected[0]
        assert not res.rejected[1]
        assert not res.rejected[3]

    def test_alpha_validation(self) -> None:
        with pytest.raises(RiskAdjustedRankingError):
            benjamini_hochberg([0.1, 0.2], alpha=0.0)
        with pytest.raises(RiskAdjustedRankingError):
            benjamini_hochberg([0.1, 0.2], alpha=-0.1)
        with pytest.raises(RiskAdjustedRankingError):
            benjamini_hochberg([0.1, 0.2], alpha=1.5)

    def test_alpha_one_accepts_conforming_pvalues(self) -> None:
        # alpha=1.0 is the legal upper bound.  The BH inequality at
        # rank k becomes ``p_(k) <= k / m`` — NOT ``p_(k) <= 1``.
        # For full rejection the sorted p-values must grow at most
        # linearly with rank: ``p_(k) <= k/m``.  A vector of p-values
        # smaller than that bound at every rank therefore triggers
        # full rejection at alpha=1.0.
        p = [0.01, 0.5, 0.9]
        res = benjamini_hochberg(p, alpha=1.0)
        assert res.n_rejected == 3
        # The same p-values at alpha=0.5 (threshold = (0.167, 0.333,
        # 0.5)): only p_(1)=0.01 passes; p_(2)=0.5 fails the line at
        # 0.333, so the step-up cuts at k=1.  This is the standard
        # monotone-step property of BH.
        res_strict = benjamini_hochberg(p, alpha=0.5)
        assert res_strict.n_rejected == 1
        assert res_strict.threshold_index == 0

    def test_empty_input_returns_empty_result(self) -> None:
        res = benjamini_hochberg([])
        assert res.n_tests == 0
        assert res.n_rejected == 0
        assert res.threshold_index == -1
        assert res.rejected.size == 0
        assert res.p_adjusted.size == 0


# ── Monotonic p-value ordering ────────────────────────────────────────────


class TestMonotonicPValueOrdering:
    """q-values are monotonically non-decreasing from largest to smallest."""

    def test_p_adjusted_monotonic_when_rejections_occur(self) -> None:
        # Mix of low and high p-values so BH rejects the low ones.
        p = [
            1e-7, 1e-5, 1e-3, 5e-2, 0.15, 0.30, 0.55, 0.80,
        ]
        res = benjamini_hochberg(p, alpha=0.05)
        # p_adjusted is indexed by the original input order, so monotonicity
        # is checked on the *sorted-by-rank* view: q_(k) is non-decreasing in k.
        # Reconstruct q in sorted order.
        order = np.argsort(p, kind="mergesort")
        q_sorted = res.p_adjusted[order]
        # Monotonic non-decreasing from the smallest to the largest rank.
        diffs = np.diff(q_sorted)
        assert (diffs >= -1e-12).all(), (
            f"q-values must be monotonic non-decreasing in rank; "
            f"diffs={diffs.tolist()}"
        )

    def test_q_values_bound_input_pvalues_on_rejected(self) -> None:
        # For each rejected hypothesis, q_i <= alpha (Storey's bound).
        p = [1e-6, 5e-6, 1e-4, 0.02, 0.03, 0.5]
        res = benjamini_hochberg(p, alpha=0.05)
        rejected_q = res.p_adjusted[res.rejected]
        assert rejected_q.size > 0
        assert (rejected_q <= 0.05 + 1e-12).all()

    def test_q_values_clamped_to_unit_interval(self) -> None:
        # Even for huge p-values that cannot be rejected, q-values
        # must stay within [0, 1] (the Storey BF cap cannot exceed 1).
        p = [1e-30, 1e-10, 0.99, 0.999999]
        res = benjamini_hochberg(p, alpha=0.05)
        assert (res.p_adjusted >= 0.0).all()
        assert (res.p_adjusted <= 1.0).all()


# ── Tie handling ──────────────────────────────────────────────────────────


class TestTieHandling:
    """Tied p-values share rejection status; rank output is deterministic."""

    def test_ties_share_rejection_status(self) -> None:
        # m=4, alpha=0.05 -> line is (0.0125, 0.025, 0.0375, 0.05).
        # Tied at 0.02 and at 0.04.  Sorted: (0.01, 0.02, 0.02, 0.04)
        #   k=1: 0.01 <= 0.0125 ✓
        #   k=2: 0.02 <= 0.025  ✓
        #   k=3: 0.02 <= 0.0375 ✓ (BH steps on sorted values, not unique ranks)
        #   k=4: 0.04 <= 0.05   ✓
        # All four rejected.
        p = [0.02, 0.04, 0.01, 0.02]
        res = benjamini_hochberg(p, alpha=0.05)
        assert res.n_rejected == 4
        assert res.rejected[0]  # 0.02 — tied, rejected
        assert res.rejected[1]  # 0.04 — tied, rejected
        assert res.rejected[2]  # 0.01 — rejected
        assert res.rejected[3]  # 0.02 — tied, rejected

    def test_ties_breaking_line_does_not_split_rejection(self) -> None:
        # m=5, alpha=0.05 -> line is (0.01, 0.02, 0.03, 0.04, 0.05).
        # Sorted: (0.005, 0.005, 0.045, 0.045, 0.05)
        #   k=1: 0.005 <= 0.01 ✓
        #   k=2: 0.005 <= 0.02 ✓
        #   k=3: 0.045 > 0.03 ✗ -> threshold at k=2.
        # BH rejects only the first two (both 0.005s).
        p = [0.005, 0.045, 0.045, 0.005, 0.05]
        res = benjamini_hochberg(p, alpha=0.05)
        assert res.n_rejected == 2
        assert res.threshold_index == 1
        # The two 0.005s are both rejected; the 0.045s and the 0.05 are not.
        rejected_indices = set(np.flatnonzero(res.rejected).tolist())
        assert rejected_indices == {0, 3}

    def test_ranking_is_deterministic_with_ties(self) -> None:
        # Identical return series should sort by candidate_id ASC.
        # Use the SAME underlying array (not sequential draws from the
        # same RNG, which would produce slightly different means) so
        # all three candidates truly tie on the t-statistic.
        rng = _rng(7)
        shared = rng.normal(0.001, 0.01, size=64)
        returns = [
            ("zeta", shared.copy()),
            ("alpha", shared.copy()),
            ("mike", shared.copy()),
        ]
        a = rank_candidates_by_trial_returns(returns)
        b = rank_candidates_by_trial_returns(list(reversed(returns)))
        # Identical inputs (modulo order) produce identical ranked output
        # because the ranker sorts deterministically.
        ids_a = [r.candidate_id for r in a]
        ids_b = [r.candidate_id for r in b]
        assert ids_a == ids_b == ["alpha", "mike", "zeta"]


# ── Empty / degenerate trial sets ─────────────────────────────────────────


class TestEmptyDegenerateTrialSets:
    """Short / zero-variance / NaN trial series are demoted, not silently ranked."""

    def test_empty_input_returns_empty_list(self) -> None:
        assert rank_candidates_by_trial_returns([]) == []

    def test_short_trial_series_marked_bh_reject(self) -> None:
        # Below MIN_TRIAL_BARS — the ranker cannot compute a t-stat
        # and must NOT silently emit p=0.5 (or worse, p=0.0) for a
        # 4-bar trial that happens to be all positive.
        too_short = np.array([0.01, 0.02, 0.015, 0.005])
        # Add a second, evaluable trial so the cell itself is NOT
        # flagged as degenerate (cell-level takes precedence and would
        # mask the per-trial degeneracy message we want to assert on).
        guard = _returns_with_mean(0.001, std=0.01, n=64, seed=99)
        res = rank_candidates_by_trial_returns(
            [("cand_short", too_short), ("cand_long", guard)],
            alpha=0.05,
        )
        # Find the short candidate in the (now 2-row) output.
        short = next(r for r in res if r.candidate_id == "cand_short")
        assert short.bh_reject is True
        assert short.bh_rejected is False
        assert short.is_discovery is False
        assert "degenerate" in short.notes or "var" in short.notes

    def test_zero_variance_series_marked_bh_reject(self) -> None:
        # A flat trial series is degenerate — its mean is exact but its
        # std is 0, so the t-stat is undefined.  p=1.0 is the right
        # answer but the ranker must surface the degeneracy, not the
        # BH decision, so downstream gates can fail loud.
        flat = np.zeros(MIN_TRIAL_BARS + 4)
        # Pair with an evaluable trial so the per-trial degeneracy
        # note (which mentions ``var``) is what surfaces.
        guard = _returns_with_mean(0.001, std=0.01, n=64, seed=98)
        res = rank_candidates_by_trial_returns(
            [("cand_flat", flat), ("cand_long", guard)],
            alpha=0.05,
        )
        flat_row = next(r for r in res if r.candidate_id == "cand_flat")
        assert flat_row.bh_reject is True
        assert flat_row.bh_rejected is False
        assert "var" in flat_row.notes

    def test_nan_series_marked_bh_reject(self) -> None:
        nan_arr = np.full(MIN_TRIAL_BARS + 4, np.nan)
        guard = _returns_with_mean(0.001, std=0.01, n=64, seed=97)
        res = rank_candidates_by_trial_returns(
            [("cand_nan", nan_arr), ("cand_long", guard)],
            alpha=0.05,
        )
        nan_row = next(r for r in res if r.candidate_id == "cand_nan")
        assert nan_row.bh_reject is True
        assert nan_row.bh_rejected is False

    def test_one_sample_t_pvalue_returns_one_for_degenerate(self) -> None:
        assert one_sample_t_pvalue(np.array([1.0, 2.0])) == 1.0  # too short
        assert one_sample_t_pvalue(np.zeros(20)) == 1.0  # zero var
        nan_arr = np.full(20, np.nan)
        assert one_sample_t_pvalue(nan_arr) == 1.0  # NaN
        # Empty input
        assert one_sample_t_pvalue(np.array([])) == 1.0

    def test_one_sample_t_pvalue_low_for_strong_positive_mean(self) -> None:
        # 200-bar series with mean=0.005 and std=0.01 -> t ~ 7, p ~ 0.
        returns = _returns_with_mean(0.005, std=0.01, n=200, seed=11)
        p = one_sample_t_pvalue(returns)
        assert 0.0 <= p < 1e-6

    def test_one_sample_t_pvalue_high_for_zero_mean(self) -> None:
        # 200-bar series with mean=0, std=0.01 -> t ~ 0, p ~ 1.
        returns = _returns_with_mean(0.0, std=0.01, n=200, seed=12)
        p = one_sample_t_pvalue(returns)
        assert p > 0.5

    def test_cell_with_fewer_than_min_trials_marks_all_reject(self) -> None:
        # MIN_CELL_TRIALS is 2 — a cell with one trial cannot engage
        # BH.  The ranker must surface that as a per-candidate bh_reject
        # so downstream promotion gates fail loud.
        single = _returns_with_mean(0.005, std=0.01, n=64, seed=13)
        res = rank_candidates_by_trial_returns(
            [("solo", single)],
            alpha=0.05,
        )
        assert len(res) == 1
        assert res[0].bh_reject is True
        assert "1 trial" in res[0].notes

    def test_bh_reject_strategies_rank_below_bh_rejected(self) -> None:
        # Two strategies: one strong positive-mean series, one
        # degenerate short series.  The strong one should rank above
        # the degenerate one even if the degenerate one's raw mean
        # happens to be huge (it doesn't here, but the test asserts
        # the structural invariant that rejects are below rejections).
        strong = _returns_with_mean(0.01, std=0.005, n=120, seed=21)
        weak = _returns_with_mean(0.001, std=0.005, n=120, seed=22)
        res = rank_candidates_by_trial_returns(
            [
                ("degen", np.array([0.01, 0.02])),  # degenerate: too short
                ("real", strong),
                ("noise", weak),
            ],
            alpha=0.05,
        )
        # degen is bh_reject=True and ranks last.
        assert res[-1].candidate_id == "degen"
        assert res[-1].bh_reject is True
        # The other two are evaluated by BH; "real" should outrank "noise"
        # because its positive mean produces a smaller p-value.
        eval_ids = [r.candidate_id for r in res if not r.bh_reject]
        assert eval_ids == ["real", "noise"]


# ── TrialReturnStore consumption ──────────────────────────────────────────


class _RecordingReturnStore(TrialReturnStore):
    """Test double — same shape as TrialReturnStore but exposes the
    candidate id that was used in :meth:`record_with_id`."""

    def __init__(self) -> None:
        super().__init__()
        self.ids_by_cell: dict[tuple[str, str, str], list[str]] = {}

    def record_with_id(
        self,
        key: tuple[str, str, str],
        candidate_id: str,
        returns: np.ndarray,
    ) -> None:
        """Record one trial, attaching a stable candidate_id."""
        self.ids_by_cell.setdefault(key, []).append(candidate_id)
        self.record(key, returns)


class TestRankingConsumesTrialReturnStore:
    """``rank_from_trial_return_store`` walks a real :class:`TrialReturnStore`."""

    def _build_store(self) -> _RecordingReturnStore:
        store = _RecordingReturnStore()
        cell_a = ("trend_follow", "EURUSD", "H1")
        cell_b = ("mean_revert", "GBPUSD", "M15")
        # Cell A: 4 trials; one strong, one weak, two noise.
        store.record_with_id(
            cell_a, "trend_strong",
            _returns_with_mean(0.008, std=0.005, n=120, seed=31),
        )
        store.record_with_id(
            cell_a, "trend_weak",
            _returns_with_mean(0.001, std=0.005, n=120, seed=32),
        )
        store.record_with_id(
            cell_a, "trend_noise1",
            _returns_with_mean(0.0, std=0.01, n=120, seed=33),
        )
        store.record_with_id(
            cell_a, "trend_noise2",
            _returns_with_mean(0.0, std=0.01, n=120, seed=34),
        )
        # Cell B: 2 trials; both modest.
        store.record_with_id(
            cell_b, "mr_a",
            _returns_with_mean(0.004, std=0.005, n=120, seed=41),
        )
        store.record_with_id(
            cell_b, "mr_b",
            _returns_with_mean(0.002, std=0.005, n=120, seed=42),
        )
        return store

    def test_bridge_returns_one_entry_per_cell(self) -> None:
        store = self._build_store()
        rankings = rank_from_trial_return_store(store)
        assert set(rankings.keys()) == {
            ("trend_follow", "EURUSD", "H1"),
            ("mean_revert", "GBPUSD", "M15"),
        }

    def test_bridge_emits_strong_candidate_as_top_discovery(self) -> None:
        store = self._build_store()
        ids_per_cell: dict[tuple[str, str, str], Sequence[str]] = {
            cell: list(ids) for cell, ids in store.ids_by_cell.items()
        }
        rankings = rank_from_trial_return_store(
            store,
            candidate_ids_per_cell=ids_per_cell,
        )
        cell_a_rank = rankings[("trend_follow", "EURUSD", "H1")]
        assert cell_a_rank[0].candidate_id == "trend_strong"
        assert cell_a_rank[0].is_discovery is True

    def test_bridge_preserves_candidate_ids_from_store(self) -> None:
        store = self._build_store()
        ids_per_cell: dict[tuple[str, str, str], Sequence[str]] = {
            cell: list(ids) for cell, ids in store.ids_by_cell.items()
        }
        rankings = rank_from_trial_return_store(
            store,
            candidate_ids_per_cell=ids_per_cell,
        )
        # All candidate_ids in the ranking match the ids recorded in
        # the store — the bridge does NOT silently fall back to
        # positional labels when ids are supplied.
        all_ids_ranked = {
            r.candidate_id
            for rows in rankings.values()
            for r in rows
        }
        all_ids_recorded = {
            cid for ids in store.ids_by_cell.values() for cid in ids
        }
        assert all_ids_ranked == all_ids_recorded

    def test_bridge_handles_empty_cells(self) -> None:
        store = TrialReturnStore()
        # No records — bridge must return an empty dict rather than
        # raising (the empty-store case is a legitimate "no trials yet"
        # state and the orchestrator wants to skip the ranking step).
        assert rank_from_trial_return_store(store) == {}

    def test_bridge_handles_id_length_mismatch_by_falling_back(self) -> None:
        # If the caller supplies ids whose length does not match the
        # matrix column count, the bridge falls back to positional
        # labels so the ranking is still defined.  No silent NaNs.
        store = self._build_store()
        rankings = rank_from_trial_return_store(
            store,
            # Wrong length on cell A (3 ids for 4 columns).
            candidate_ids_per_cell={
                ("trend_follow", "EURUSD", "H1"): ["a", "b", "c"],
                ("mean_revert", "GBPUSD", "M15"): ["x", "y"],
            },
        )
        # Cell A: positional fallback (trial_0..trial_3), Cell B: matched.
        cell_a_ids = {r.candidate_id for r in rankings[("trend_follow", "EURUSD", "H1")]}
        assert cell_a_ids == {f"trial_{i}" for i in range(4)}
        cell_b_ids = {r.candidate_id for r in rankings[("mean_revert", "GBPUSD", "M15")]}
        assert cell_b_ids == {"x", "y"}

    def test_ranker_never_consumes_aggregate_metrics(self) -> None:
        # Defensive: the ranker takes per-trial *series* (np.ndarray,
        # not aggregate scalars).  Passing a Python float must surface
        # as a degenerate trial, not as "the strategy had a great
        # mean so BH should reject it".
        res = rank_candidates_by_trial_returns(
            [("float_metric", 0.5)],  # type: ignore[arg-type,list-item]
            alpha=0.05,
        )
        assert res[0].bh_reject is True
        assert res[0].bh_rejected is False


# ── Raw-return sort path: removed ──────────────────────────────────────────────


class TestRawReturnSortRemoved:
    """The deprecated tournament ``rank_scorecard_rows`` path raises."""

    def test_rank_scorecard_rows_raises_raw_return_sort_removed(self) -> None:
        # The legacy function lived in ``tournament.scorecard``.  It
        # was a single-metric sort on ``return_pct`` that ignored
        # multiple-testing corrections.  Sprint C 1b.3 removes it; this
        # test pins the loud-failure shim so silent regressions are
        # caught.
        from tournament.scorecard import (
            ScorecardRow,
            rank_scorecard_rows,
        )

        rows = [
            ScorecardRow(
                strategy_id="legit",
                symbol="EURUSD",
                timeframe="H1",
                return_pct=10.0,
                max_dd_pct=2.0,
                daily_dd_breaches=0,
                total_dd_breaches=0,
                trade_count=100,
                source="unit",
            ),
        ]
        with pytest.raises(RawReturnSortRemoved):
            rank_scorecard_rows(rows)

    def test_raw_return_sort_error_subclasses_risk_adjusted(self) -> None:
        # The error must subclass the module-level base so callers can
        # catch the broader category if they want to upgrade to the
        # new ranker.
        with pytest.raises(RiskAdjustedRankingError):
            from tournament.scorecard import (
                ScorecardRow,
                rank_scorecard_rows,
            )

            rank_scorecard_rows(
                [
                    ScorecardRow(
                        strategy_id="x",
                        symbol="EURUSD",
                        timeframe="H1",
                        return_pct=1.0,
                        max_dd_pct=0.0,
                        daily_dd_breaches=0,
                        total_dd_breaches=0,
                        trade_count=1,
                        source="unit",
                    )
                ]
            )


# ── Integration with the factory package ──────────────────────────────────


class TestFactoryExports:
    """The new module is reachable via the public factory API."""

    def test_symbols_reachable_from_factory(self) -> None:
        # Same import path used by callers — guards against accidental
        # rename of the public symbols.
        from forex_bot.factory import risk_adjusted_ranking as rar

        assert rar.benjamini_hochberg is benjamini_hochberg
        assert rar.rank_candidates_by_trial_returns is rank_candidates_by_trial_returns
        assert rar.rank_from_trial_return_store is rank_from_trial_return_store
        assert rar.RawReturnSortRemoved is RawReturnSortRemoved

    def test_default_fdr_alpha_is_05(self) -> None:
        # Pin the default — downstream callers (e.g. a future
        # ``runner.rank``) may rely on the constant value.
        assert DEFAULT_FDR_ALPHA == 0.05

    def test_min_trial_bars_constant_pinned(self) -> None:
        # Pin MIN_TRIAL_BARS so the t-statistic guard is not silently
        # weakened by a future refactor.
        assert MIN_TRIAL_BARS == 8
        assert MIN_CELL_TRIALS == 2
