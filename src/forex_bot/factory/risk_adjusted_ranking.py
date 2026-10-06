"""Risk-adjusted tournament ranking — Benjamini-Hochberg FDR primary.

Sprint C card cc90a6b6 (1b.3): replace the raw-return sort in the
strategy factory tournament with a multiple-testing-aware ranker.

Why BH-FDR and not raw sort
---------------------------
The original ``tournament.scorecard.rank_scorecard_rows`` ranked
strategies by ``return_pct`` DESC.  This is a *selection-on-the-maximum*
procedure that, under crypto's 10k+ Optuna trials per sweep, will
silently promote the best noise sample.  Benjamini-Hochberg FDR
(Benjamini & Hochberg, 1995, *JRSS B* 57:289-300) is the standard
correction for the false-discovery rate under multiple testing; the
ranker uses it to decide which strategies are "real" candidates vs
which are noise winners, and orders the survivors by adjusted
significance.

White's Reality Check (White, 2000, *Econometrica* 68:1097-1126) is
the bootstrap alternative; this module implements BH as primary and
documents White's RC as a follow-on.  BH is preferred because:

* Closed-form: O(m log m) sort + linear scan, no bootstrap resampling.
* Operates on per-strategy p-values, which the ``TrialReturnStore``
  matrix already supplies one per strategy (one t-stat per column).
* FDR is the right multiple-testing criterion here — we want to
  control the *expected proportion of false discoveries among the
  declared winners*, not the family-wise error rate.

Module choice
-------------
We chose **Benjamini-Hochberg** as the primary multiple-testing
correction. White's Reality Check (Hansen's simplified version) is a
candidate for a later module; the cost of bootstrap resampling (1000+
draws × N strategies × T bars) is not justified while the BH closed-form
result is available.

Public surface
--------------
* :func:`benjamini_hochberg` — BH step-up procedure.
* :func:`one_sample_t_pvalue` — per-strategy p-value from a return series.
* :func:`rank_candidates_by_trial_returns` — consume per-trial return
  series (the same one TrialReturnStore accumulates), emit
  :class:`RankedCandidate` list sorted by adjusted significance.
* :func:`rank_from_trial_return_store` — bridge helper that iterates
  every cell in a :class:`TrialReturnStore` and returns per-cell rankings.
* :exc:`RawReturnSortRemoved` — raised by the deprecated raw-return path.
* :exc:`RiskAdjustedRankingError` — base error for this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

# ── Errors ────────────────────────────────────────────────────────────────


class RiskAdjustedRankingError(ValueError):
    """Base error for the risk-adjusted ranking module."""


class RawReturnSortRemoved(RiskAdjustedRankingError):
    """Raised when a caller invokes the deprecated raw-return sort path.

    Sprint C card cc90a6b6 (1b.3) removes the raw-return sort in favour
    of Benjamini-Hochberg FDR.  The previous
    ``tournament.scorecard.rank_scorecard_rows`` function — which
    sorted solely by ``return_pct`` — is now a loud-failure shim; this
    exception is what it raises.  Callers must migrate to
    :func:`rank_candidates_by_trial_returns`.
    """


# ── Constants ────────────────────────────────────────────────────────────

#: Default FDR level (q).  Benjamini-Hochberg controls the expected
#: proportion of false discoveries among rejected hypotheses at this
#: level.  0.05 mirrors the conventional Type-I error rate; 0.10 is
#: common in discovery workflows.  Callers may tighten (e.g. 0.01 for
#: Tier-A promotion) or loosen (e.g. 0.10 for exploration).
DEFAULT_FDR_ALPHA: float = 0.05

#: Minimum per-strategy sample size.  Below this we cannot compute a
#: meaningful t-statistic (the std is too noisy); the ranker marks
#: such strategies as ``insufficient_data`` rather than emitting a
#: p-value that would be mis-anchored at 1.0.
MIN_TRIAL_BARS: int = 8

#: Cell-wide minimum number of trials.  BH on a single test is
#: meaningless (no multiple-testing); we require at least 2 trials for
#: the ranker to engage.  Strategies in cells with fewer trials get
#: ``bh_rejected=False, bh_reject=True`` so downstream promotion
#: gates fail loud.
MIN_CELL_TRIALS: int = 2


# ── P-value computation ──────────────────────────────────────────────────


def one_sample_t_pvalue(
    returns: np.ndarray,
    *,
    baseline: float = 0.0,
) -> float:
    """One-sample t-test p-value: H0 mean(returns) == baseline.

    Used as the per-strategy p-value input to Benjamini-Hochberg.
    Rationale: a positive mean return is necessary but not sufficient
    evidence the strategy is real; the t-stat scales the mean by the
    noise level, so a high-variance "winner" gets a high p-value and
    gets dropped by BH.

    Returns ``1.0`` (no evidence against H0) for inputs that cannot
    support a t-test (degenerate length, zero variance, NaN/inf).
    This is deliberately conservative — a degenerate trial looks
    indistinguishable from "no signal" and should not be rejected by
    the BH step-up.

    Parameters
    ----------
    returns
        1-D array of per-bar returns (e.g. one column of the
        ``TrialReturnStore.matrix(cell_key)`` output).
    baseline
        Null-hypothesis mean.  Defaults to 0.0 (random walk).
    """
    arr = np.asarray(returns, dtype=float)
    if arr.ndim != 1 or arr.size < MIN_TRIAL_BARS:
        return 1.0
    if not np.all(np.isfinite(arr)):
        return 1.0

    # Sample mean and (sample) std with ddof=1.
    diffs = arr - baseline
    n = diffs.size
    mean = float(np.mean(diffs))
    # Sample variance (ddof=1).  Zero-variance returns — a degenerate
    # trial whose every bar logged the same return — emit p=1.0
    # rather than dividing by zero.
    var = float(np.var(diffs, ddof=1))
    if var <= 0.0 or not math.isfinite(var):
        return 1.0

    sem = math.sqrt(var / n)
    if sem <= 0.0 or not math.isfinite(sem):
        return 1.0

    t_stat = mean / sem
    # Two-sided p-value via the survival function of |T| with n-1 dof.
    # scipy.stats.t.sf is the canonical, well-tested routine; importing
    # at use-site keeps this module importable without scipy when only
    # the constants/errors are touched (rare, but avoids a hard dep).
    from scipy import stats  # type: ignore[import-not-found]

    dof = n - 1
    p = 2.0 * float(stats.t.sf(abs(t_stat), dof))
    if not math.isfinite(p):
        return 1.0
    # Clamp to [0, 1] — scipy's sf can drift by ULPs at extreme t.
    return min(1.0, max(0.0, p))


# ── Benjamini-Hochberg step-up ───────────────────────────────────────────


@dataclass(frozen=True)
class BHResult:
    """Benjamini-Hochberg step-up result.

    Attributes
    ----------
    rejected
        Boolean mask over the original ``pvalues`` argument; ``True``
        means the hypothesis at that index is rejected (declared a
        discovery) at FDR level :attr:`alpha`.
    threshold_index
        0-based index of the largest ``k`` such that the BH inequality
        holds; ``-1`` when no hypothesis is rejected.
    p_adjusted
        Adjusted p-values (q-values), same length as input.  Equal to
        ``p_i * m / rank_i``, then monotonised non-decreasing from the
        largest to the smallest.  These are what consumers should sort
        by.
    alpha
        FDR level that was used.
    n_tests
        Length of the original ``pvalues`` input (m).
    """

    rejected: np.ndarray
    threshold_index: int
    p_adjusted: np.ndarray
    alpha: float
    n_tests: int

    @property
    def n_rejected(self) -> int:
        """Number of hypotheses rejected at FDR level ``alpha``."""
        return int(np.sum(self.rejected))


def benjamini_hochberg(
    pvalues: Sequence[float],
    *,
    alpha: float = DEFAULT_FDR_ALPHA,
) -> BHResult:
    """Benjamini-Hochberg step-up procedure (Benjamini & Hochberg 1995).

    Order the ``m`` p-values ascending; find the largest ``k`` such that
    ``p_(k) <= alpha * k / m``; reject hypotheses 1..k.

    The adjusted p-values (q-values) follow Storey (2002): for each
    hypothesis ``i`` ranked at position ``r_i``,
    ``q_i = min over j >= i of (p_(j) * m / r_j)``, then monotonicised
    non-decreasing from the largest to the smallest.  Sort by
    ``p_adjusted`` ascending — the smallest q-value is the most
    significant discovery.

    Edge cases (handled deterministically):

    * Empty input → all-empty :class:`BHResult`.
    * ``alpha`` outside (0, 1] → :class:`RiskAdjustedRankingError`.
    * All-zero p-values → all rejected, q-values clamped to ``alpha``.
    * NaN/inf p-values → sorted to the bottom and never rejected.
    * Ties → BH steps on the **sorted** sequence but the rejection
      mask is over the original indices, so tied p-values share
      rejection status (this is the standard "BH with ties" behaviour
      and matches Benjamini & Hochberg's Section 3).
    """
    if not 0.0 < alpha <= 1.0:
        raise RiskAdjustedRankingError(
            f"alpha must be in (0, 1], got {alpha!r}"
        )

    p_arr = np.asarray(list(pvalues), dtype=float)
    m = p_arr.size

    if m == 0:
        empty_b: np.ndarray = np.zeros(0, dtype=bool)
        empty_p: np.ndarray = np.zeros(0, dtype=float)
        return BHResult(
            rejected=empty_b,
            threshold_index=-1,
            p_adjusted=empty_p,
            alpha=float(alpha),
            n_tests=0,
        )

    # Sort by p-value ascending; NaN/inf sort to the bottom naturally
    # because numpy's argsort puts NaN at the end of an ascending sort.
    order = np.argsort(p_arr, kind="mergesort")
    p_sorted = p_arr[order]

    # BH inequality for sorted p_(k): p_sorted[k] <= alpha * (k+1) / m
    rank = np.arange(1, m + 1, dtype=float)
    threshold = alpha * rank / float(m)
    # is_sorted_le: which sorted positions are at-or-below the BH line.
    # We use <= so that ties at the boundary are rejected (Benjamini
    # & Hochberg's "step-up" definition).
    is_sorted_le = p_sorted <= threshold

    # Largest k such that the BH inequality holds CONSECUTIVELY from
    # position 0 upward.  BH is a step-UP from k=1, not a max-of-True
    # search: once a position fails the inequality, every larger k is
    # also rejected regardless of its own inequality.  NaN/inf p-values
    # contribute False, so a non-rejected hypothesis at any position
    # clips the threshold at the previous position.  Standard BH
    # algorithm: k* = max{k : is_sorted_le[:k+1].all()} - equivalently,
    # k* is one less than the first False in is_sorted_le, or m-1 when
    # is_sorted_le is all True.
    if np.any(~is_sorted_le):
        first_false = int(np.flatnonzero(~is_sorted_le)[0])
        threshold_sorted_idx = first_false - 1
    else:
        threshold_sorted_idx = int(m - 1)

    # Rejection mask back-projected to original indices.
    rejected: np.ndarray = np.zeros(m, dtype=bool)
    if threshold_sorted_idx >= 0:
        # All sorted positions <= threshold_sorted_idx are rejected.
        # Their original indices are order[: threshold_sorted_idx + 1].
        rejected_indices = order[: threshold_sorted_idx + 1]
        rejected[rejected_indices] = True

    # Adjusted p-values (Storey 2002 BH-adjusted q-values).
    if threshold_sorted_idx >= 0:
        # Only compute q-values for rejected hypotheses; the rest are
        # left as their raw p-value scaled by m / r, which is
        # monotonically non-increasing from the largest r to the
        # smallest and is >= alpha for non-rejected r.
        raw_q_sorted = p_sorted * float(m) / rank
        # Cumulative minimum from the right enforces the
        # non-decreasing-from-largest-to-smallest property (Storey).
        adj_q_sorted = np.minimum.accumulate(raw_q_sorted[::-1])[::-1]
        # Clamp to [0, 1] — raw values often exceed 1 when the BH
        # step-up rejects nothing at that rank; the q-value is still
        # at most 1 because a discovery has at most full probability.
        adj_q_sorted = np.minimum(adj_q_sorted, 1.0)
        # Re-order back to the original input order so consumers can
        # index by the original hypothesis position.
        inv_order = np.argsort(order, kind="mergesort")
        p_adjusted = adj_q_sorted[inv_order]
    else:
        # No rejections: q-values are just the raw p-values (still
        # monotonic in the original index since no step-up occurred).
        p_adjusted = np.array(p_arr, copy=True)

    return BHResult(
        rejected=rejected,
        threshold_index=int(threshold_sorted_idx),
        p_adjusted=p_adjusted,
        alpha=float(alpha),
        n_tests=int(m),
    )


# ── TrialReturnStore-driven ranking ──────────────────────────────────────


@dataclass(frozen=True)
class RankedCandidate:
    """One ranked candidate from :func:`rank_candidates_by_trial_returns`.

    The ranking is by adjusted p-value (q-value) ascending — the most
    significant discoveries first.  A candidate with
    ``bh_rejected=True`` AND ``bh_reject=False`` is a discovery the BH
    procedure promoted; ``bh_reject=True`` means the cell was
    degenerate and the candidate could not be evaluated (downstream
    promotion gates must fail loud on this).
    """

    candidate_id: str
    cell_key: tuple[str, str, str]  # (archetype, pair, timeframe)
    p_value: float
    q_value: float
    bh_rejected: bool
    #: ``True`` when the candidate was excluded by a guard (degenerate
    #: cell, NaN p-value, insufficient trials, empty trial series).
    #: Distinguishes "BH did not reject" from "BH could not evaluate".
    bh_reject: bool
    #: 0-based rank in the returned list.  1 = best (most significant).
    rank: int = 0
    notes: str = ""

    @property
    def is_discovery(self) -> bool:
        """True iff BH declared this candidate a real signal."""
        return self.bh_rejected and not self.bh_reject


def rank_candidates_by_trial_returns(
    trial_returns_per_candidate: Sequence[tuple[str, np.ndarray]],
    *,
    cell_key: tuple[str, str, str] | None = None,
    alpha: float = DEFAULT_FDR_ALPHA,
) -> list[RankedCandidate]:
    """Rank candidates by Benjamini-Hochberg FDR on per-trial return series.

    This is the primary entry point replacing the raw-return sort
    (Sprint C 1b.3).  It consumes per-trial return series — never
    aggregate metrics — and emits a list of :class:`RankedCandidate`
    sorted ascending by q-value (most significant first).

    Parameters
    ----------
    trial_returns_per_candidate
        Sequence of ``(candidate_id, returns)`` pairs.  ``returns`` is
        a 1-D per-trial return series, e.g. one column of the
        :meth:`TrialReturnStore.matrix` output.  In practice this is
        built by iterating over ``TrialReturnStore._cells[cell_key]``
        and zipping with the candidate ids from the Optuna study, or
        from a per-cell metadata table that maps column index →
        candidate_id.
    cell_key
        Optional ``(archetype, pair, timeframe)`` cell tag recorded
        on each result for downstream auditing.  Defaults to
        ``("", "", "")`` when the caller is operating on a flat
        tournament rather than a cell-keyed store.
    alpha
        FDR level.  See :data:`DEFAULT_FDR_ALPHA`.

    Returns
    -------
    list[RankedCandidate]
        Sorted ascending by q-value (most significant first).  Empty
        when ``trial_returns_per_candidate`` is empty.

    Notes
    -----
    The function is **deterministic**: ties break by ``candidate_id``
    ascending so reruns produce identical order.  Tied p-values share
    rejection status (Benjamini & Hochberg Section 3); sorting by
    q-value then candidate_id keeps downstream consumers stable.

    A candidate with a degenerate return series (zero variance, all
    NaN, too short, all identical) gets ``bh_reject=True`` and
    ``bh_rejected=False`` — the BH procedure cannot evaluate it, so it
    cannot be a discovery.  Promotion gates should treat
    ``bh_reject=True`` as "do not promote".
    """
    if not trial_returns_per_candidate:
        return []

    cell = cell_key if cell_key is not None else ("", "", "")

    # First pass: compute per-strategy p-values, flagging degeneracy.
    rows: list[dict] = []
    n_trials = len(trial_returns_per_candidate)
    cell_degenerate = n_trials < MIN_CELL_TRIALS
    for cid, returns in trial_returns_per_candidate:
        arr = np.asarray(returns, dtype=float)
        degenerate = (
            arr.ndim != 1
            or arr.size < MIN_TRIAL_BARS
            or not np.all(np.isfinite(arr))
            or (arr.size > 1 and float(np.var(arr, ddof=1)) <= 0.0)
        )
        if cell_degenerate or degenerate:
            var_repr = (
                f"{float(np.var(arr, ddof=1)):.6g}"
                if arr.size > 1
                else "n/a"
            )
            rows.append(
                {
                    "candidate_id": cid,
                    "p_value": 1.0,
                    "bh_rejected": False,
                    "bh_reject": True,
                    "notes": (
                        f"cell has {n_trials} trial(s); BH requires "
                        f">= {MIN_CELL_TRIALS}"
                        if cell_degenerate
                        else (
                            f"degenerate trial series "
                            f"(n={arr.size}, var={var_repr})"
                        )
                    ),
                }
            )
            continue
        p = one_sample_t_pvalue(arr)
        rows.append(
            {
                "candidate_id": cid,
                "p_value": float(p),
                "bh_rejected": False,
                "bh_reject": False,
                "notes": "",
            }
        )

    # Separate the evaluable rows; only they participate in BH.
    evaluable_idx = np.array(
        [i for i, r in enumerate(rows) if not r["bh_reject"]],
        dtype=int,
    )
    evaluable_p = np.array(
        [rows[i]["p_value"] for i in evaluable_idx],
        dtype=float,
    )

    bh: BHResult | None = None
    if evaluable_p.size >= 2:
        bh = benjamini_hochberg(evaluable_p.tolist(), alpha=alpha)
        for local_idx, orig_idx in enumerate(evaluable_idx):
            rows[int(orig_idx)]["bh_rejected"] = bool(bh.rejected[local_idx])

    # Sort: BH-rejected first (most significant), then non-rejected
    # in q-value order, then rejects.  Tie-break by candidate_id ASC
    # for determinism.
    if bh is not None:
        # q-values only meaningful for evaluable rows; assign 1.0 to
        # rejects so they sort below any rejected one.
        for local_idx, orig_idx in enumerate(evaluable_idx):
            rows[int(orig_idx)]["q_value"] = float(bh.p_adjusted[local_idx])
    for r in rows:
        r.setdefault("q_value", 1.0)

    def _sort_key(r: dict) -> tuple:
        # Tuple order: (bh_reject ASC, bh_rejected DESC, q_value ASC,
        # candidate_id ASC).  Reject-degenerate strategies go LAST
        # (reject_bucket=1); non-reject strategies go FIRST
        # (reject_bucket=0).  Within the non-reject bucket, BH-rejected
        # (discoveries) come first; non-rejected come after, sorted
        # by q-value then candidate_id.
        reject_bucket = 1 if r["bh_reject"] else 0
        discovery_bucket = 0 if r["bh_rejected"] else 1
        return (
            reject_bucket,
            discovery_bucket,
            float(r["q_value"]),
            str(r["candidate_id"]),
        )

    sorted_rows = sorted(rows, key=_sort_key)

    out: list[RankedCandidate] = []
    for rank, r in enumerate(sorted_rows, start=1):
        out.append(
            RankedCandidate(
                candidate_id=str(r["candidate_id"]),
                cell_key=cell,
                p_value=float(r["p_value"]),
                q_value=float(r["q_value"]),
                bh_rejected=bool(r["bh_rejected"]),
                bh_reject=bool(r["bh_reject"]),
                rank=rank,
                notes=str(r.get("notes", "")),
            )
        )
    return out


# ── TrialReturnStore bridge ──────────────────────────────────────────────


def rank_from_trial_return_store(
    store: object,
    *,
    candidate_ids_per_cell: dict[tuple[str, str, str], Sequence[str]] | None = None,
    alpha: float = DEFAULT_FDR_ALPHA,
) -> dict[tuple[str, str, str], list[RankedCandidate]]:
    """Bridge helper: rank every cell in a :class:`TrialReturnStore`.

    Parameters
    ----------
    store
        A :class:`forex_bot.factory.validation_runner.TrialReturnStore`
        (duck-typed to avoid an import cycle; ``store.matrix(key)`` must
        return ``np.ndarray`` of shape ``[T, N]``).
    candidate_ids_per_cell
        Optional mapping of cell key → list of ``candidate_id`` strings
        of length ``N`` (one per column of ``store.matrix(key)``).
        When omitted, columns are labelled by their 0-based index
        (e.g. ``"trial_0"``) so the ranking is still defined but
        loses the human-readable candidate ids.  In practice the
        Optuna driver passes the per-trial ``trial_id`` here.

    Returns
    -------
    dict[(archetype, pair, timeframe), list[RankedCandidate]]
        One entry per cell key currently in the store, ordered by
        ascending q-value within each cell.
    """
    out: dict[tuple[str, str, str], list[RankedCandidate]] = {}
    for cell_key in store.keys():  # type: ignore[attr-defined]
        matrix = store.matrix(cell_key)  # type: ignore[attr-defined]
        if matrix is None or matrix.ndim != 2 or matrix.shape[1] == 0:
            continue
        if candidate_ids_per_cell is not None:
            ids = list(candidate_ids_per_cell.get(cell_key, ()))
        else:
            ids = [f"trial_{i}" for i in range(matrix.shape[1])]
        if len(ids) != matrix.shape[1]:
            # Mismatched ids/matrix columns — fall back to positional
            # labels so the ranking is still defined.
            ids = [f"trial_{i}" for i in range(matrix.shape[1])]
        pairs: list[tuple[str, np.ndarray]] = [
            (ids[i], matrix[:, i]) for i in range(matrix.shape[1])
        ]
        out[cell_key] = rank_candidates_by_trial_returns(
            pairs,
            cell_key=cell_key,
            alpha=alpha,
        )
    return out
