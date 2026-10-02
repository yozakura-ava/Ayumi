"""SRF PBO (Probability of Backtest Overfitting) via CSCV.

Implements Combinatorially Symmetric Cross-Validation from
Bailey & López de Prado (2014):
https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf

The PBO is the probability that the best in-sample strategy ranks below
the median out-of-sample.  High PBO ⟹ overfit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from itertools import combinations
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_N_BLOCKS = 16  # max blocks for CSCV (C(32,16)/2 ≈ 300M — too many)
MAX_COMBINATIONS = 65536  # hard cap for tractability
PERIODS_PER_YEAR = 252


# ═══════════════════════════════════════════════════════════════════════════
# Result structure
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class PBOScore:
    """PBO score for a study of N strategies."""

    pbo: float  # Probability of Backtest Overfitting [0, 1]
    logit: float  # λ = ln(PBO / (1 - PBO))
    ci_lower: float  # 95% CI lower bound on PBO
    ci_upper: float  # 95% CI upper bound on PBO
    n_strategies: int  # N
    n_periods: int  # T
    n_combinations: int  # Number of IS/OOS splits evaluated
    n_blocks: int  # S (blocks per half)

    def is_overfit(self, threshold: float = 0.5) -> bool:
        """True if PBO exceeds threshold (default 50%)."""
        return self.pbo > threshold

    def summary(self) -> dict:
        return {
            "pbo": float(self.pbo),
            "logit": float(self.logit),
            "ci_lower": float(self.ci_lower),
            "ci_upper": float(self.ci_upper),
            "n_strategies": self.n_strategies,
            "n_periods": self.n_periods,
            "n_combinations": self.n_combinations,
            "n_blocks": self.n_blocks,
        }


# ═══════════════════════════════════════════════════════════════════════════
# CSCV algorithm
# ═══════════════════════════════════════════════════════════════════════════


def _sharpe_ratio(returns: np.ndarray) -> np.ndarray:
    """Compute per-column Sharpe ratio for a [T, N] matrix.

    Returns array of shape [N].
    """
    mean = np.mean(returns, axis=0)
    std = np.std(returns, axis=0, ddof=1)
    std = np.where(std == 0, 1e-12, std)
    return mean / std


def compute_pbo(
    strategy_returns: np.ndarray | Sequence[Sequence[float]],
    *,
    n_blocks: int = DEFAULT_N_BLOCKS,
    max_combinations: int = MAX_COMBINATIONS,
) -> PBOScore:
    """Compute PBO via Combinatorially Symmetric Cross-Validation.

    Parameters
    ----------
    strategy_returns : array-like, shape [T, N]
        Matrix of strategy returns where T = number of time periods,
        N = number of strategies (e.g., Optuna trials).
    n_blocks : int
        Number of equal-sized blocks to split T into.  The CSCV
        considers all C(2S, S) / 2 combinations of S blocks as IS
        and the remaining S as OOS.  Capped at 16 for tractability.
    max_combinations : int
        Hard cap on number of combinations to evaluate.  If C(2S, S)/2
        exceeds this, S is reduced.

    Returns
    -------
    PBOScore with pbo, logit, confidence interval, and metadata.
    """
    R = np.asarray(strategy_returns, dtype=float)
    if R.ndim != 2:
        raise ValueError(f"Expected 2D array [T, N], got {R.ndim}D")

    T, N = R.shape
    if N < 2:
        raise ValueError("Need at least 2 strategies to compute PBO")
    if T < 4:
        raise ValueError("Need at least 4 time periods")

    # Adjust n_blocks so combinations are tractable
    S = min(n_blocks, T // 2)
    while S > 1:
        from math import comb

        n_comb = comb(2 * S, S) // 2
        if n_comb <= max_combinations:
            break
        S -= 1

    if S < 1:
        S = 1

    # Split into 2S blocks of equal size
    block_size = T // (2 * S)
    if block_size == 0:
        block_size = 1
        S = T // 2

    # Trim excess periods
    usable_T = block_size * 2 * S
    R = R[:usable_T, :]

    # Build block indices
    blocks = [R[i * block_size : (i + 1) * block_size, :] for i in range(2 * S)]

    # Enumerate IS/OOS combinations
    all_indices = list(range(2 * S))
    from math import comb

    # To avoid double-counting, only consider combos where the smallest
    # element of the IS set < smallest element of the OOS set.
    breach_count = 0
    evaluated = 0

    for is_blocks in combinations(all_indices, S):
        oos_blocks = tuple(i for i in all_indices if i not in is_blocks)
        # Symmetric dedup: skip if oos_blocks < is_blocks lexicographically
        if oos_blocks < is_blocks:
            continue

        # Build IS and OOS return matrices
        is_returns = np.vstack([blocks[i] for i in is_blocks])
        oos_returns = np.vstack([blocks[i] for i in oos_blocks])

        # Compute Sharpe per strategy in IS and OOS
        is_sharpe = _sharpe_ratio(is_returns)
        oos_sharpe = _sharpe_ratio(oos_returns)

        # Find best IS strategy
        best_is_idx = int(np.argmax(is_sharpe))

        # Check if best IS strategy ranks below median OOS
        median_oos = np.median(oos_sharpe)
        if oos_sharpe[best_is_idx] < median_oos:
            breach_count += 1

        evaluated += 1

    if evaluated == 0:
        logger.warning("No CSCV combinations evaluated (T=%d, N=%d, S=%d)", T, N, S)
        return PBOScore(
            pbo=0.5,
            logit=0.0,
            ci_lower=0.0,
            ci_upper=1.0,
            n_strategies=N,
            n_periods=T,
            n_combinations=0,
            n_blocks=S,
        )

    pbo = breach_count / evaluated

    # Logit transform
    # Clamp to avoid log(0)
    pbo_clamped = max(min(pbo, 1.0 - 1e-10), 1e-10)
    logit = math.log(pbo_clamped / (1.0 - pbo_clamped))

    # Confidence interval via normal approximation on logit scale
    # SE(λ) ≈ sqrt(1/n_combos + 1/(n_breaches) + 1/(n_combos - n_breaches))
    n_b = max(breach_count, 1)
    n_nb = max(evaluated - breach_count, 1)
    se_logit = math.sqrt(1.0 / evaluated + 1.0 / n_b + 1.0 / n_nb)
    z = 1.96  # 95% CI

    ci_lower_logit = logit - z * se_logit
    ci_upper_logit = logit + z * se_logit

    # Transform back to probability
    ci_lower = 1.0 / (1.0 + math.exp(-ci_lower_logit))
    ci_upper = 1.0 / (1.0 + math.exp(-ci_upper_logit))

    return PBOScore(
        pbo=pbo,
        logit=logit,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        n_strategies=N,
        n_periods=T,
        n_combinations=evaluated,
        n_blocks=S,
    )


# ═══════════════════════════════════════════════════════════════════════════
# DuckDB storage
# ═══════════════════════════════════════════════════════════════════════════


def store_pbo_score(conn, study_id: str, score: PBOScore) -> None:
    """Store PBO score for a study in DuckDB.

    Writes to the ``study_ledger`` table by updating the completed_at
    and storing the PBO as a JSON annotation in a dedicated column
    (or in metrics_summary if the schema supports it).

    For the current schema, we store into ``monte_carlo_samples`` with
    a synthetic run_id of ``"study:{study_id}"`` to avoid schema changes.
    """
    run_id = f"study:{study_id}"
    rows = [
        (run_id, 0, "pbo", score.pbo, score.ci_lower, score.ci_upper),
        (run_id, 0, "pbo_logit", score.logit, None, None),
        (run_id, 0, "pbo_n_combinations", float(score.n_combinations), None, None),
        (run_id, 0, "pbo_n_strategies", float(score.n_strategies), None, None),
    ]
    conn.executemany(
        "INSERT INTO monte_carlo_samples VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    logger.info("Stored PBO score %.4f for study %s", score.pbo, study_id)
