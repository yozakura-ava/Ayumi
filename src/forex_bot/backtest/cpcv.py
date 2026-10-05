"""Combinatorial Purged Cross-Validation (CPCV) runner.

Implements Combinatorial Purged Cross-Validation from Bailey & López de Prado,
*Advances in Financial Machine Learning* (Ch. 12), and the purge + embargo
mechanics from Ch. 4 + Ch. 7 of the same book.

Why CPCV and not just CSCV?
----------------------------
CSCV (Bailey & López de Prado, 2014, ``forex_bot.srf.pbo.compute_pbo``)
splits the [T, N_trials] matrix into ``2S`` blocks and enumerates
``C(2S, S) / 2`` IS/OOS combinations.  Blocks are sampled contiguously
but the combination logic treats them as opaque cells — there is no
notion of label-horizon leakage between IS and OOS.

CPCV (AFML Ch. 12) is stricter:

* Time is contiguous: ``N`` groups of ``≈ T/N`` bars, test fold is the
  union of exactly ``k`` of those groups.  Train = complement.
* **Purge**: training observations whose label horizon ``[i, i+H)``
  overlaps the test bars are dropped — this is the AFML Ch. 4 mechanism
  that CSCV does not implement.
* **Embargo**: an additional ``embargo_bars`` zone of training bars
  adjacent to the test edges are dropped — AFML Ch. 7.  This is the
  existing ``embargo_bars`` from
  :class:`forex_bot.factory.pipeline_config.WalkForwardWindowConfig`
  (M5=288, M15=96, H1=24, H4=6, D1=1) re-used here, so CPCV picks up
  the same per-TF embargo policy as the walk-forward runner without
  duplicating it.
* Path count: ``φ = C(N, k)`` — for ``N=6, k=3`` that's 20 paths, for
  ``N=8, k=4`` that's 70 paths, matching the card's
  "~70 paths starting N=6-8" target.

Integration with ``TrialReturnStore``
-------------------------------------
``forex_bot.factory.validation_runner.TrialReturnStore.matrix`` already
returns the ``[T, N_trials]`` per-bar per-strategy return series that
CSCV consumes (card ``4309d26b-fb6f-4748-a84a-d82c54b3e898`` merged at
``cf8a7a64``).  CPCV reads the same matrix — no duplicate collection
path — and produces a per-path train/test view of it.  The
``run_cpcv`` function accepts the matrix directly, so callers can
hand it the ``TrialReturnStore.matrix(cell_key)`` output unchanged.

PBO and DSR on the path distribution
------------------------------------
A single CPCV run yields ``φ`` IS/OOS pairs.  We compute per-path:

* ``is_sharpe[j]`` — Sharpe of strategy ``j`` on purged-train bars.
* ``oos_sharpe[j]`` — Sharpe of strategy ``j`` on test bars.
* ``breach`` — the best-IS strategy's OOS Sharpe is below the OOS
  median (i.e. the backtest is overfit on this path).

``compute_pbo_cpcv`` aggregates: ``PBO = Σ breaches / φ``.  Lower is
better; ``PBO > 0.5`` is the rejection threshold inherited from CSCV.

``compute_dsr_cpcv`` aggregates the OOS Sharpe distribution of the
best-IS strategy across all paths: deflates by the expected maximum
of ``φ`` iid normal draws with the empirical std.  Effective number of
trials = ``φ``.

Compatibility
-------------
This module is a *sibling* of ``forex_bot.srf.pbo.compute_pbo``; the
existing CSCV path on :class:`ValidationRunner` is unchanged so no
in-flight verdict semantics shift.  Callers opt into CPCV by calling
:func:`run_cpcv` / :func:`compute_pbo_cpcv` directly; the factory
runner exposes a ``cpcv_run_cpcv`` bridge helper that consumes the
same ``TrialReturnStore`` instance.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from itertools import combinations
from typing import Iterator, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ── Defaults / constants ────────────────────────────────────────────────

#: Hard cap on the number of CPCV paths.  Matches the CSCV cap in
#: :mod:`forex_bot.srf.pbo` (``MAX_COMBINATIONS = 65536``) so the two
#: runners share a tractability contract.
MAX_CPCV_PATHS: int = 65536

#: Lower bound on the per-group bar count.  Below this the group is
#: too thin to evaluate a Sharpe ratio reliably (std floors at ~2).
MIN_GROUP_BARS: int = 4

#: Std floor for Sharpe computation (mirrors :func:`srf.pbo._sharpe_ratio`).
SHARPE_STD_FLOOR: float = 1e-12

#: String constant surfaced when the matrix is too small to evaluate
#: (mirrors :data:`forex_bot.factory.validation_runner.PBO_CEILING_NOT_APPLICABLE`).
CPCV_NOT_APPLICABLE: str = "NOT_APPLICABLE"


# ── Errors ───────────────────────────────────────────────────────────────


class CPCVError(ValueError):
    """Base error for the CPCV module."""


class CPCVTooFewBars(CPCVError):
    """Raised when ``T < N_groups`` (cannot form N contiguous groups)."""


class CPCVTooFewTrials(CPCVError):
    """Raised when the input matrix has fewer than 2 strategy columns."""


class CPCVInvalidConfig(CPCVError):
    """Raised when :class:`CPCVConfig` fields violate the invariants."""


# ── Config + result dataclasses ─────────────────────────────────────────


@dataclass(frozen=True)
class CPCVConfig:
    """CPCV configuration.

    Attributes
    ----------
    n_groups
        Number of contiguous groups (``N``).  Card default: 6–8.
    k_test_groups
        Number of groups that form the test fold (``k``).  Cardinality
        of each combination is ``C(N, k)``.  Symmetric choice:
        ``k = N // 2``.
    label_horizon_bars
        Forward-looking label horizon ``H`` in bars.  Train bars whose
        label spans ``[i, i+H)`` overlap with the test bars are purged.
        ``0`` disables purging (degenerate / sanity-test mode).
    embargo_bars
        Additional pre-test bars dropped on top of the purge.  AFML
        Ch. 7.  Wired from
        :class:`forex_bot.factory.pipeline_config.WalkForwardWindowConfig.embargo_bars`
        per timeframe.
    max_paths
        Hard cap on the number of enumerated paths.  When
        ``C(N, k) > max_paths`` we greedily reduce ``k`` until the cap
        is satisfied and log a warning.
    """

    n_groups: int
    k_test_groups: int
    label_horizon_bars: int = 0
    embargo_bars: int = 0
    max_paths: int = MAX_CPCV_PATHS

    def __post_init__(self) -> None:
        if self.n_groups < 2:
            raise CPCVInvalidConfig(
                f"n_groups must be >= 2 (got {self.n_groups})"
            )
        if self.k_test_groups < 1 or self.k_test_groups >= self.n_groups:
            raise CPCVInvalidConfig(
                f"k_test_groups must be in [1, N) — got k={self.k_test_groups}, N={self.n_groups}"
            )
        if self.label_horizon_bars < 0:
            raise CPCVInvalidConfig(
                f"label_horizon_bars must be >= 0 (got {self.label_horizon_bars})"
            )
        if self.embargo_bars < 0:
            raise CPCVInvalidConfig(
                f"embargo_bars must be >= 0 (got {self.embargo_bars})"
            )
        if self.max_paths < 1:
            raise CPCVInvalidConfig(
                f"max_paths must be >= 1 (got {self.max_paths})"
            )

    @property
    def effective_k(self) -> int:
        """``k`` after greedy reduction under :attr:`max_paths`."""
        from math import comb

        n = self.n_groups
        k = self.k_test_groups
        while k > 1 and comb(n, k) > self.max_paths:
            k -= 1
        return k


@dataclass(frozen=True)
class CPCVSplit:
    """One CPCV path's split specification.

    Indices are 0-based into the usable bars ``[0, usable_T)``.
    """

    path_id: int
    test_group_indices: tuple[int, ...]
    test_indices: np.ndarray
    train_indices_raw: np.ndarray  # complement of test, pre-purge
    train_indices: np.ndarray  # post purge + embargo


@dataclass(frozen=True)
class CPCVPBOScore:
    """PBO score computed from a CPCV path distribution."""

    pbo: float
    n_paths: int
    n_breaches: int
    n_groups: int
    k_test_groups: int
    n_periods: int
    n_strategies: int

    def is_overfit(self, threshold: float = 0.5) -> bool:
        """``True`` when ``pbo > threshold`` (default 50 %, mirrors CSCV)."""
        return self.pbo > threshold

    def summary(self) -> dict:
        return {
            "pbo": float(self.pbo),
            "n_paths": int(self.n_paths),
            "n_breaches": int(self.n_breaches),
            "n_groups": int(self.n_groups),
            "k_test_groups": int(self.k_test_groups),
            "n_periods": int(self.n_periods),
            "n_strategies": int(self.n_strategies),
        }


@dataclass(frozen=True)
class CPCVDSRScore:
    """DSR computed from the per-path best-IS OOS Sharpe distribution."""

    dsr: float
    mean_oos_sharpe: float
    std_oos_sharpe: float
    expected_max_oos_sharpe: float
    n_paths: int
    effective_n_trials: int

    def summary(self) -> dict:
        return {
            "dsr": float(self.dsr),
            "mean_oos_sharpe": float(self.mean_oos_sharpe),
            "std_oos_sharpe": float(self.std_oos_sharpe),
            "expected_max_oos_sharpe": float(self.expected_max_oos_sharpe),
            "n_paths": int(self.n_paths),
            "effective_n_trials": int(self.effective_n_trials),
        }


@dataclass(frozen=True)
class CPCVResult:
    """Full CPCV run output."""

    splits: tuple[CPCVSplit, ...]
    pbo: CPCVPBOScore | None
    dsr: CPCVDSRScore | None
    config: CPCVConfig
    matrix_shape: tuple[int, int]

    def n_paths(self) -> int:
        return len(self.splits)


# ── Combinatorics helpers ────────────────────────────────────────────────


def cpcv_paths_count(n: int, k: int) -> int:
    """Return ``C(N, k)`` — the number of CPCV paths for given ``(N, k)``.

    This is the ``φ`` in the card's brief ("~70 paths" for N=8, k=4).
    """
    from math import comb

    if n < 0 or k < 0 or k > n:
        raise CPCVInvalidConfig(f"Invalid (N, k) for path count: ({n}, {k})")
    return comb(n, k)


def effective_k_for_max_paths(n: int, k: int, max_paths: int) -> int:
    """Greedy reduction of ``k`` until ``C(N, k) ≤ max_paths``.

    Mirrors :attr:`CPCVConfig.effective_k` for callers that don't want
    to construct a full config.
    """
    from math import comb

    if max_paths < 1:
        raise CPCVInvalidConfig(f"max_paths must be >= 1 (got {max_paths})")
    while k > 1 and comb(n, k) > max_paths:
        k -= 1
    return k


# ── Purge + embargo mechanics ────────────────────────────────────────────


def apply_purge_and_embargo(
    train_indices: Sequence[int],
    test_indices: Sequence[int],
    *,
    label_horizon_bars: int,
    embargo_bars: int,
) -> np.ndarray:
    """Return train indices after purge + embargo.

    **Purge** (AFML Ch. 4): drop training bars whose label horizon
    ``[i, i + H)`` overlaps the test set.

    **Embargo** (AFML Ch. 7): drop additional ``embargo_bars`` to the
    left of each test interval, on top of the purge zone.  After both,
    the leftmost usable train bar is at index
    ``(interval_start) - H - embargo_bars``.

    **Scattered test intervals.**  A CPCV path picks ``k`` groups out of
    ``N`` contiguous groups — those groups need not be adjacent.  The
    test fold is a (possibly scattered) union of contiguous intervals,
    so the purge + embargo machinery is applied per-interval.  Train
    bars to the *right* of every test interval are kept (labels look
    forward, so they never overlap a test interval).  No symmetric
    post-test embargo is applied (the CPCV path is a forward walk; the
    right side is "future" data that the model hasn't been trained on
    for this path).

    Parameters
    ----------
    train_indices
        Raw training bar indices (sorted, unique).
    test_indices
        Test bar indices (sorted, unique).  Must be disjoint from
        ``train_indices``.  May be scattered across multiple contiguous
        intervals.
    label_horizon_bars
        ``H`` from :attr:`CPCVConfig.label_horizon_bars`.
    embargo_bars
        ``embargo_bars`` from :attr:`CPCVConfig.embargo_bars`.

    Returns
    -------
    np.ndarray
        Sorted 1-D array of train indices after purge + embargo.
    """
    train = np.asarray(sorted(set(int(i) for i in train_indices)), dtype=int)
    test = np.asarray(sorted(set(int(i) for i in test_indices)), dtype=int)
    if train.size == 0:
        return train
    if test.size == 0:
        return train

    intervals = _contiguous_intervals(test)
    if not intervals:
        return train

    # Drop any train bar that sits inside a test interval — should not
    # happen by construction (train/test groups are disjoint) but the
    # guard lets a misconfigured split return an empty purged-train so
    # downstream PBO/DSR can short-circuit ``NOT_APPLICABLE`` rather
    # than emit a misleading number.
    keep_mask = np.ones(train.size, dtype=bool)
    for a, b in intervals:
        inside = (train >= a) & (train <= b)
        keep_mask &= ~inside

    if label_horizon_bars == 0 and embargo_bars == 0:
        # No-op path — return the train set with any inside-test
        # indices removed (which by construction should be none).
        return train[keep_mask]

    # Per-interval purge + embargo on the LEFT side of each interval.
    # Train bar ``i`` is dropped iff ``i < a`` AND ``i > a - H - embargo``.
    drop_zone_width = label_horizon_bars + embargo_bars
    for a, _b in intervals:
        if drop_zone_width <= 0:
            continue
        cutoff = a - drop_zone_width
        in_drop_zone = (train > cutoff) & (train < a)
        keep_mask &= ~in_drop_zone

    return train[keep_mask]


def _contiguous_intervals(indices: np.ndarray) -> list[tuple[int, int]]:
    """Split a sorted integer array into ``(start, end)`` inclusive
    contiguous intervals.

    Empty input → empty list.  Single bar → ``[(idx, idx)]``.  The
    output is sorted and non-overlapping.
    """
    if indices.size == 0:
        return []
    out: list[tuple[int, int]] = []
    start = int(indices[0])
    prev = start
    for v in indices[1:]:
        v = int(v)
        if v == prev + 1:
            prev = v
            continue
        out.append((start, prev))
        start = v
        prev = v
    out.append((start, prev))
    return out


# ── Split enumeration ───────────────────────────────────────────────────


def _usable_T(matrix: np.ndarray, n_groups: int) -> int:
    """Number of bars the CPCV runner actually uses.

    Drops trailing bars so each group has the same size; preserves the
    contiguous-group invariant.  If the matrix is shorter than
    ``n_groups * MIN_GROUP_BARS`` the runner raises
    :class:`CPCVTooFewBars`.
    """
    T = int(matrix.shape[0])
    if T < n_groups * MIN_GROUP_BARS:
        raise CPCVTooFewBars(
            f"Need T >= N * MIN_GROUP_BARS = {n_groups * MIN_GROUP_BARS} "
            f"bars, got T={T}"
        )
    group_size = T // n_groups
    return group_size * n_groups


def _validate_matrix(matrix: np.ndarray, *, min_strategies: int = 2) -> None:
    arr = np.asarray(matrix, dtype=float)
    if arr.ndim != 2:
        raise CPCVError(f"Expected 2D matrix [T, N], got {arr.ndim}D")
    T, N = arr.shape
    if T < 4:
        raise CPCVTooFewBars(f"Need T >= 4 bars, got T={T}")
    if N < min_strategies:
        raise CPCVTooFewTrials(
            f"Need at least {min_strategies} strategy trials, got N={N}"
        )


def generate_cpcv_splits(
    matrix: np.ndarray,
    config: CPCVConfig,
) -> Iterator[CPCVSplit]:
    """Yield :class:`CPCVSplit` for every ``C(N, k)`` path.

    Each split's :attr:`CPCVSplit.train_indices` is post purge +
    embargo — that's the index set callers use for the IS matrix.
    """
    _validate_matrix(matrix)
    R = np.asarray(matrix, dtype=float)
    usable_T = _usable_T(R, config.n_groups)
    group_size = usable_T // config.n_groups
    k_eff = config.effective_k
    if k_eff != config.k_test_groups:
        logger.warning(
            "CPCV: C(N=%d, k=%d)=%d exceeds max_paths=%d; reducing k to %d "
            "(effective φ = %d)",
            config.n_groups,
            config.k_test_groups,
            cpcv_paths_count(config.n_groups, config.k_test_groups),
            config.max_paths,
            k_eff,
            cpcv_paths_count(config.n_groups, k_eff),
        )

    # Pre-compute per-group index ranges once.
    group_ranges = [
        np.arange(g * group_size, (g + 1) * group_size, dtype=int)
        for g in range(config.n_groups)
    ]
    # Complement of a test-group set is just the other groups.
    all_groups = set(range(config.n_groups))

    path_id = 0
    for test_groups in combinations(range(config.n_groups), k_eff):
        test_groups_t = tuple(int(g) for g in test_groups)
        test_indices = np.concatenate([group_ranges[g] for g in test_groups_t])
        train_groups = sorted(all_groups - set(test_groups_t))
        train_indices_raw = np.concatenate([group_ranges[g] for g in train_groups])
        # Cast to ``list[int]`` so mypy is happy with the
        # ``Sequence[int]`` signature on ``apply_purge_and_embargo``
        # without polluting the public API with a numpy dependency.
        train_indices = apply_purge_and_embargo(
            train_indices_raw.tolist(),
            test_indices.tolist(),
            label_horizon_bars=config.label_horizon_bars,
            embargo_bars=config.embargo_bars,
        )
        yield CPCVSplit(
            path_id=path_id,
            test_group_indices=test_groups_t,
            test_indices=np.asarray(test_indices, dtype=int),
            train_indices_raw=np.asarray(train_indices_raw, dtype=int),
            train_indices=np.asarray(train_indices, dtype=int),
        )
        path_id += 1


def list_cpcv_splits(matrix: np.ndarray, config: CPCVConfig) -> list[CPCVSplit]:
    """Materialise all CPCV splits into a list (for small N, k)."""
    return list(generate_cpcv_splits(matrix, config))


# ── Sharpe helper ───────────────────────────────────────────────────────


def _sharpe_ratio_per_col(returns: np.ndarray) -> np.ndarray:
    """Per-column (per-strategy) Sharpe on a [T, N] matrix.

    Floors ``std`` at :data:`SHARPE_STD_FLOOR` to avoid 0/0 — mirrors
    :func:`forex_bot.srf.pbo._sharpe_ratio`.  NaNs (from all-NaN
    columns) propagate; callers handle via :func:`numpy.nanmedian` /
    ``nansum`` as appropriate.
    """
    mean = np.nanmean(returns, axis=0)
    std = np.nanstd(returns, axis=0, ddof=1)
    std = np.where((std == 0) | np.isnan(std), SHARPE_STD_FLOOR, std)
    return mean / std


# ── PBO + DSR on the CPCV path distribution ─────────────────────────────


def compute_pbo_cpcv(
    matrix: np.ndarray,
    config: CPCVConfig,
) -> CPCVPBOScore | None:
    """PBO from a CPCV run on ``matrix`` of shape ``[T, N_trials]``.

    Per path: compute IS Sharpe on purged-train, OOS Sharpe on test.
    A path "breaches" if the best-IS strategy's OOS Sharpe is below
    the OOS median.  ``PBO = Σ breaches / φ``.

    Returns ``None`` when no paths can be evaluated (insufficient
    purged-train length on every path).  The verdict then carries
    ``CPCV_NOT_APPLICABLE`` — mirrors the
    :data:`forex_bot.factory.validation_runner.PBO_CEILING_NOT_APPLICABLE`
    sentinel so downstream ceilings can branch uniformly.
    """
    _validate_matrix(matrix)
    R = np.asarray(matrix, dtype=float)
    T, N = R.shape

    breaches = 0
    evaluated = 0
    for split in generate_cpcv_splits(R, config):
        train_idx = split.train_indices
        test_idx = split.test_indices
        if train_idx.size < 2 or test_idx.size < 2:
            continue
        is_returns = R[train_idx, :]
        oos_returns = R[test_idx, :]
        if is_returns.shape[0] < 2 or oos_returns.shape[0] < 2:
            continue
        is_sharpe = _sharpe_ratio_per_col(is_returns)
        oos_sharpe = _sharpe_ratio_per_col(oos_returns)
        if not np.isfinite(is_sharpe).any() or not np.isfinite(oos_sharpe).any():
            continue
        best_is_idx = int(np.argmax(is_sharpe))
        median_oos = float(np.nanmedian(oos_sharpe))
        if oos_sharpe[best_is_idx] < median_oos:
            breaches += 1
        evaluated += 1

    if evaluated == 0:
        return None

    return CPCVPBOScore(
        pbo=breaches / evaluated,
        n_paths=evaluated,
        n_breaches=breaches,
        n_groups=config.n_groups,
        k_test_groups=config.effective_k,
        n_periods=T,
        n_strategies=N,
    )


def compute_dsr_cpcv(
    matrix: np.ndarray,
    config: CPCVConfig,
) -> CPCVDSRScore | None:
    """DSR on the per-path best-IS OOS Sharpe distribution.

    For each path we record the OOS Sharpe of the strategy that had
    the highest IS Sharpe on the purged train.  ``φ`` values → deflates
    by the expected maximum of ``φ`` iid normals with the empirical
    std (``E[max] ≈ σ · Φ⁻¹(1 - 1/φ)``, asymptotic).

    Returns ``None`` when fewer than 2 paths are usable.
    """
    _validate_matrix(matrix)
    R = np.asarray(matrix, dtype=float)
    T, N = R.shape

    best_oos: list[float] = []
    for split in generate_cpcv_splits(R, config):
        train_idx = split.train_indices
        test_idx = split.test_indices
        if train_idx.size < 2 or test_idx.size < 2:
            continue
        is_returns = R[train_idx, :]
        oos_returns = R[test_idx, :]
        if is_returns.shape[0] < 2 or oos_returns.shape[0] < 2:
            continue
        is_sharpe = _sharpe_ratio_per_col(is_returns)
        oos_sharpe = _sharpe_ratio_per_col(oos_returns)
        if not np.isfinite(is_sharpe).any() or not np.isfinite(oos_sharpe).any():
            continue
        best_is_idx = int(np.argmax(is_sharpe))
        best_oos.append(float(oos_sharpe[best_is_idx]))

    if len(best_oos) < 2:
        return None

    arr = np.asarray(best_oos, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    if std == 0 or not math.isfinite(std):
        return CPCVDSRScore(
            dsr=0.0,
            mean_oos_sharpe=mean,
            std_oos_sharpe=std,
            expected_max_oos_sharpe=mean,
            n_paths=len(best_oos),
            effective_n_trials=len(best_oos),
        )
    n = len(best_oos)
    # E[max of n iid N(0,1)] ≈ Φ⁻¹(1 - 1/n) for n >= 2.
    expected_max_z = _normal_quantile(1.0 - 1.0 / n)
    expected_max = std * expected_max_z
    dsr = (mean - expected_max) / std
    return CPCVDSRScore(
        dsr=float(dsr),
        mean_oos_sharpe=mean,
        std_oos_sharpe=std,
        expected_max_oos_sharpe=float(expected_max),
        n_paths=n,
        effective_n_trials=n,
    )


def _normal_quantile(p: float) -> float:
    """Inverse normal CDF (probit).  Implemented locally to avoid a
    SciPy dependency for the simple ``E[max]`` calculation.

    Uses the rational approximation from Abramowitz & Stegun 26.2.23
    (``Rational approximation for the normal distribution function``),
    accurate to ~4.5e-4 — well within the precision needs of a
    CPCV-DSR sanity check.  Inputs outside ``[1e-10, 1 - 1e-10]`` are
    clamped so the asymptotic ``E[max]`` is finite for any ``n``.
    """
    if p <= 1e-10:
        return -6.0
    if p >= 1.0 - 1e-10:
        return 6.0
    # Abramowitz & Stegun 26.2.23
    t = math.sqrt(-2.0 * math.log(min(p, 1.0 - p)))
    c0 = 2.515517
    c1 = 0.802853
    c2 = 0.010328
    d1 = 1.432788
    d2 = 0.189269
    d3 = 0.001308
    z = t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)
    return z if p > 0.5 else -z


# ── Top-level runner ─────────────────────────────────────────────────────


def run_cpcv(
    matrix: np.ndarray,
    config: CPCVConfig,
    *,
    compute_pbo: bool = True,
    compute_dsr: bool = True,
) -> CPCVResult:
    """Run a full CPCV analysis on ``matrix``.

    This is the single entry point the factory runner calls.  It
    materialises all splits, then computes PBO and/or DSR on the path
    distribution.  Split enumeration is O(φ * N_groups) memory; for
    ``N=8, k=4, T≈5000`` this is ~70 splits × ~625 bars per group →
    negligible.  For larger N/k the caller should chunk via
    :func:`generate_cpcv_splits`.

    Parameters
    ----------
    matrix
        ``[T, N_trials]`` per-bar per-strategy return matrix.  Pass
        :meth:`forex_bot.factory.validation_runner.TrialReturnStore.matrix`
        output directly — no copy, no reshape.
    config
        :class:`CPCVConfig` instance.
    compute_pbo, compute_dsr
        Toggle each aggregation independently.  Both default ``True``
        to match the card's "PBO/DSR on the CPCV path distribution"
        requirement.

    Returns
    -------
    :class:`CPCVResult` carrying all splits and the requested scores.
    """
    _validate_matrix(matrix)
    R = np.asarray(matrix, dtype=float)
    splits = tuple(generate_cpcv_splits(R, config))
    pbo_score = compute_pbo_cpcv(R, config) if compute_pbo else None
    dsr_score = compute_dsr_cpcv(R, config) if compute_dsr else None
    return CPCVResult(
        splits=splits,
        pbo=pbo_score,
        dsr=dsr_score,
        config=config,
        matrix_shape=(R.shape[0], R.shape[1]),
    )


# ── Public TrialReturnStore bridge helper ────────────────────────────────


def cpcv_from_trial_store(
    matrix_or_none: np.ndarray | None,
    config: CPCVConfig,
) -> CPCVResult | None:
    """Bridge helper: run CPCV on the :class:`TrialReturnStore.matrix` output.

    Returns ``None`` when the cell has fewer than 2 trials (mirrors
    :meth:`forex_bot.factory.validation_runner.ValidationRunner._compute_pbo`
    short-circuit semantics).  Callers that want to surface the
    absence of evidence as ``CPCV_NOT_APPLICABLE`` should translate
    ``None`` to that string at the verdict layer.
    """
    if matrix_or_none is None:
        return None
    try:
        return run_cpcv(matrix_or_none, config)
    except CPCVError:
        return None


__all__ = [
    "CPCVConfig",
    "CPCVDSRScore",
    "CPCVError",
    "CPCVInvalidConfig",
    "CPCVPBOScore",
    "CPCVResult",
    "CPCVSplit",
    "CPCVTooFewBars",
    "CPCVTooFewTrials",
    "CPCV_NOT_APPLICABLE",
    "MAX_CPCV_PATHS",
    "MIN_GROUP_BARS",
    "SHARPE_STD_FLOOR",
    "apply_purge_and_embargo",
    "compute_dsr_cpcv",
    "compute_pbo_cpcv",
    "cpcv_from_trial_store",
    "cpcv_paths_count",
    "effective_k_for_max_paths",
    "generate_cpcv_splits",
    "list_cpcv_splits",
    "run_cpcv",
]
