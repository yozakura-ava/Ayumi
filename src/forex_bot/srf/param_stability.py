"""SRF Parameter Stability module.

Analyzes optimization landscape to detect overfitting via parameter
instability: heatmap generation, plateau detection, neighbor robustness,
cross-window rank correlation, coefficient of variation, and
perturbation-sweep overfit-spike detection.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class StabilityResult:
    """Parameter stability analysis result."""

    cv: float  # Coefficient of variation of performance
    is_stable: bool  # True if CV < threshold
    plateau_score: float  # 0 = spike, 1 = broad plateau
    neighbor_correlation: float  # Spearman correlation of neighbor performance
    cross_window_rank_correlation: float  # Mean Spearman rank correlation of params
    heatmap: np.ndarray | None = None  # [n_perturbations, n_params] performance grid
    detail: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "cv": float(self.cv),
            "is_stable": bool(self.is_stable),
            "plateau_score": float(self.plateau_score),
            "neighbor_correlation": float(self.neighbor_correlation),
            "cross_window_rank_correlation": float(self.cross_window_rank_correlation),
        }


@dataclass
class PerturbationStabilityResult:
    """Result of post-selection perturbation sweep for overfit spike detection.

    A high ``stability_score`` (close to 1.0) means small parameter changes
    preserve performance — the config sits on a broad plateau.  A low score
    (below ``spike_threshold``) indicates a narrow performance spike,
    classic overfitting.
    """

    stability_score: float  # Fraction of perturbations retaining ≥80% of peak
    is_overfit_spike: bool  # True if score < spike_threshold
    peak_performance: float  # Performance at best_params (unperturbed)
    n_perturbations: int  # Total perturbation evaluations
    n_retained: int  # Perturbations that retained ≥ threshold
    per_param: dict[str, float]  # Per-parameter retention fraction
    heatmap: np.ndarray | None = None  # [n_fractions, n_params] performance grid
    detail: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "stability_score": float(self.stability_score),
            "is_overfit_spike": bool(self.is_overfit_spike),
            "peak_performance": float(self.peak_performance),
            "n_perturbations": int(self.n_perturbations),
            "n_retained": int(self.n_retained),
            "per_param": {k: float(v) for k, v in self.per_param.items()},
        }


# ═══════════════════════════════════════════════════════════════════════════
# Core analyses
# ═══════════════════════════════════════════════════════════════════════════


def coefficient_of_variation(window_performances: Sequence[float]) -> float:
    """CV of a performance metric across walk-forward windows.

    Parameters
    ----------
    window_performances : Sharpe (or PF, expectancy) per window

    Returns
    -------
    CV = std / |mean|.  Lower = more stable.
    """
    arr = np.asarray(window_performances, dtype=float)
    if len(arr) < 2:
        return 0.0
    mean = np.mean(arr)
    if abs(mean) < 1e-12:
        return float("inf") if np.std(arr) > 0 else 0.0
    return float(np.std(arr, ddof=1) / abs(mean))


def plateau_detection(
    param_values: np.ndarray,
    performance: np.ndarray,
    threshold: float = 0.95,
) -> float:
    """Detect whether best performance is a spike or on a broad plateau.

    Parameters
    ----------
    param_values : [N, D] array of parameter sets (N trials, D dimensions)
    performance : [N] array of performance metric (higher = better)
    threshold : fraction of best performance that counts as "on plateau"

    Returns
    -------
    Plateau score in [0, 1].  1 = broad plateau, 0 = sharp spike.
    """
    if len(performance) == 0:
        return 0.0
    best = float(np.max(performance))
    if abs(best) < 1e-12:
        return 0.0
    on_plateau = performance >= threshold * best
    return float(np.mean(on_plateau))


def neighbor_robustness(
    param_values: np.ndarray,
    performance: np.ndarray,
    k: int = 5,
) -> float:
    """Check if nearby parameter sets perform similarly.

    For each trial, finds its k nearest neighbors in parameter space
    and computes the Spearman rank correlation of their performances.

    Returns
    -------
    Mean Spearman correlation in [-1, 1].  High = neighbors perform similarly.
    """
    from scipy.stats import spearmanr

    N = len(performance)
    if N < k + 1:
        return 0.0

    # Normalize parameters to [0, 1]
    pmin = param_values.min(axis=0)
    pmax = param_values.max(axis=0)
    prange = np.where(pmax > pmin, pmax - pmin, 1.0)
    normalized = (param_values - pmin) / prange

    correlations = []
    for i in range(N):
        dists = np.sqrt(np.sum((normalized - normalized[i]) ** 2, axis=1))
        dists[i] = float("inf")  # exclude self
        neighbor_idx = np.argsort(dists)[:k]
        neighbor_perf = performance[neighbor_idx]
        center_perf = performance[i]

        # Correlation between distance and performance difference
        neighbor_dists = dists[neighbor_idx]
        perf_diffs = np.abs(neighbor_perf - center_perf)
        if len(neighbor_dists) > 2 and np.std(perf_diffs) > 0:
            r, _ = spearmanr(neighbor_dists, perf_diffs)
            if r is not None and math.isfinite(r):
                correlations.append(-r)  # negative because close distance should = small diff

    if not correlations:
        return 0.0
    return float(np.mean(correlations))


def cross_window_rank_correlation(
    window_results: list[dict[str, Any]],
) -> float:
    """Mean Spearman rank correlation of parameter performance across windows.

    Parameters
    ----------
    window_results : list of dicts, each containing 'params' (dict) and 'performance'

    Returns
    -------
    Mean pairwise Spearman correlation. High = consistent param ranking.
    """
    from scipy.stats import spearmanr

    if len(window_results) < 2:
        return 1.0

    # Extract parameter names
    param_names = sorted(window_results[0]["params"].keys())

    # Build performance vectors per window (indexed by parameter name)
    window_perf_vectors = []
    for w in window_results:
        vec = [w["params"].get(name, 0.0) for name in param_names]
        window_perf_vectors.append(np.array(vec))

    # Pairwise Spearman
    correlations = []
    for i in range(len(window_perf_vectors)):
        for j in range(i + 1, len(window_perf_vectors)):
            if np.std(window_perf_vectors[i]) > 0 and np.std(window_perf_vectors[j]) > 0:
                r, _ = spearmanr(window_perf_vectors[i], window_perf_vectors[j])
                if r is not None and math.isfinite(r):
                    correlations.append(r)

    if not correlations:
        return 0.0
    return float(np.mean(correlations))


# ═══════════════════════════════════════════════════════════════════════════
# Perturbation sweep (overfit spike detection)
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_PERTURBATION_FRACTIONS: tuple[float, ...] = (
    -0.20,
    -0.10,
    -0.05,
    0.05,
    0.10,
    0.20,
)
"""Standard perturbation levels: ±5%, ±10%, ±20% of each parameter value."""


def perturbation_stability_score(
    evaluate_fn: callable,
    best_params: dict[str, float],
    *,
    perturbation_fractions: tuple[float, ...] = DEFAULT_PERTURBATION_FRACTIONS,
    retention_threshold: float = 0.80,
    spike_threshold: float = 0.50,
) -> PerturbationStabilityResult:
    """Run a perturbation sweep and compute overfit-spike stability score.

    For each parameter in ``best_params``, perturbs its value by each fraction
    in ``perturbation_fractions`` (one parameter at a time) and evaluates
    performance.  Computes the fraction of all perturbations that retain at
    least ``retention_threshold`` (default 80%) of peak performance.

    If the resulting score is below ``spike_threshold`` (default 0.5), the
    configuration is flagged as an overfit spike — it sits on a narrow
    performance peak that small parameter changes destroy.

    Parameters
    ----------
    evaluate_fn : callable(dict[str, float]) -> float
        Returns a performance metric (higher = better, e.g. Sharpe or profit factor).
    best_params : winning parameter set from optuna/backtest.
    perturbation_fractions : relative perturbation levels (default ±5/10/20%).
    retention_threshold : fraction of peak performance that counts as "retained".
    spike_threshold : scores below this flag as overfit spike.

    Returns
    -------
    PerturbationStabilityResult with score, flag, and per-parameter detail.
    """
    param_names = sorted(best_params.keys())
    n_perturb = len(perturbation_fractions)
    n_params = len(param_names)

    # Peak (unperturbed) performance
    peak = float(evaluate_fn(best_params))
    if abs(peak) < 1e-12:
        return PerturbationStabilityResult(
            stability_score=0.0,
            is_overfit_spike=True,
            peak_performance=0.0,
            n_perturbations=n_perturb * n_params,
            n_retained=0,
            per_param={name: 0.0 for name in param_names},
            detail="Peak performance ≈ 0 — cannot compute stability.",
        )

    heatmap = np.full((n_perturb, n_params), np.nan)
    per_param: dict[str, float] = {}
    total_retained = 0
    total_evaluated = 0

    for j, pname in enumerate(param_names):
        retained_for_param = 0
        evaluated_for_param = 0
        for i, frac in enumerate(perturbation_fractions):
            params = best_params.copy()
            params[pname] = best_params[pname] * (1.0 + frac)
            try:
                perf = float(evaluate_fn(params))
                heatmap[i, j] = perf
                evaluated_for_param += 1
                total_evaluated += 1
                if perf >= retention_threshold * peak:
                    retained_for_param += 1
                    total_retained += 1
            except Exception:
                logger.warning(
                    "Perturbation eval failed for %s=%s*%.2f",
                    pname,
                    best_params[pname],
                    1 + frac,
                )

        per_param[pname] = retained_for_param / evaluated_for_param if evaluated_for_param > 0 else 0.0

    score = total_retained / total_evaluated if total_evaluated > 0 else 0.0
    is_spike = score < spike_threshold

    detail_parts = [f"{name}={ratio:.0%}" for name, ratio in per_param.items()]
    detail = (
        f"stability_score={score:.3f} "
        f"({'OVERFIT SPIKE' if is_spike else 'stable'}), "
        f"peak={peak:.4f}, retained={total_retained}/{total_evaluated}, "
        f"per_param: {', '.join(detail_parts)}"
    )

    return PerturbationStabilityResult(
        stability_score=score,
        is_overfit_spike=is_spike,
        peak_performance=peak,
        n_perturbations=total_evaluated,
        n_retained=total_retained,
        per_param=per_param,
        heatmap=heatmap,
        detail=detail,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Heatmap generation
# ═══════════════════════════════════════════════════════════════════════════


def generate_heatmap(
    evaluate_fn: callable,
    best_params: dict[str, float],
    perturbation_fractions: tuple[float, ...] = (-0.2, -0.1, 0.0, 0.1, 0.2),
) -> np.ndarray:
    """Generate performance heatmap by perturbing best params.

    Parameters
    ----------
    evaluate_fn : callable(dict[str, float]) -> float  (returns performance metric)
    best_params : best parameter set
    perturbation_fractions : perturbation levels relative to each param value

    Returns
    -------
    [len(perturbations), n_params] array of performance values.
    Column j corresponds to perturbing param j only.
    """
    param_names = sorted(best_params.keys())
    n_perturb = len(perturbation_fractions)
    n_params = len(param_names)
    heatmap = np.full((n_perturb, n_params), np.nan)

    for j, pname in enumerate(param_names):
        for i, frac in enumerate(perturbation_fractions):
            params = best_params.copy()
            params[pname] = best_params[pname] * (1.0 + frac)
            try:
                heatmap[i, j] = float(evaluate_fn(params))
            except Exception:
                logger.warning(
                    "Evaluation failed for %s=%s*%.2f",
                    pname,
                    best_params[pname],
                    1 + frac,
                )

    return heatmap


# ═══════════════════════════════════════════════════════════════════════════
# Full stability assessment
# ═══════════════════════════════════════════════════════════════════════════


def assess_stability(
    window_performances: Sequence[float],
    param_values: np.ndarray,
    performance: np.ndarray,
    window_results: list[dict[str, Any]] | None = None,
    *,
    cv_threshold: float = 0.3,
    plateau_threshold: float = 0.95,
) -> StabilityResult:
    """Full parameter stability assessment.

    Parameters
    ----------
    window_performances : per-window Sharpe (or equivalent) values
    param_values : [N, D] array of all trial parameter sets
    performance : [N] array of trial performances
    window_results : optional, for cross-window rank correlation
    cv_threshold : max acceptable CV (default 0.3)
    plateau_threshold : fraction of best for plateau detection

    Returns
    -------
    StabilityResult with all metrics.
    """
    cv = coefficient_of_variation(window_performances)
    is_stable = cv < cv_threshold
    plateau = plateau_detection(param_values, performance, plateau_threshold)

    try:
        neighbor = neighbor_robustness(param_values, performance)
    except Exception:
        neighbor = 0.0
        logger.warning("Neighbor robustness computation failed")

    if window_results and len(window_results) >= 2:
        try:
            cwrc = cross_window_rank_correlation(window_results)
        except Exception:
            cwrc = 0.0
            logger.warning("Cross-window rank correlation computation failed")
    else:
        cwrc = 1.0  # default to stable if insufficient data

    detail = (
        f"CV={cv:.3f} ({'stable' if is_stable else 'unstable'}), "
        f"plateau={plateau:.3f}, neighbor_corr={neighbor:.3f}, "
        f"cross_window={cwrc:.3f}"
    )

    return StabilityResult(
        cv=cv,
        is_stable=is_stable,
        plateau_score=plateau,
        neighbor_correlation=neighbor,
        cross_window_rank_correlation=cwrc,
        detail=detail,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Self-test: synthetic overfit vs stable configs
# ═══════════════════════════════════════════════════════════════════════════


def _self_test() -> None:
    """Validate perturbation_stability_score with synthetic configs.

    Run directly:  python -m forex_bot.srf.param_stability --self-test
    """
    # ── Synthetic overfit config: sharp Gaussian peak ────────────────
    # Performance drops to ~0 with even 5% perturbation.
    overfit_best = {"threshold": 0.50, "period": 14.0}

    def overfit_eval(params: dict[str, float]) -> float:
        import math

        t = params["threshold"]
        p = params["period"]
        # Narrow Gaussian: width ≈ 2% of value
        dt = (t - 0.50) / 0.01
        dp = (p - 14.0) / 0.28
        return 3.0 * math.exp(-(dt**2 + dp**2))

    overfit_result = perturbation_stability_score(overfit_eval, overfit_best)
    assert overfit_result.is_overfit_spike, (  # noqa: S101 — self-test invariant; debug-only result check, intentionally silenced under `python -O`
        f"Overfit config should be flagged as spike, got score={overfit_result.stability_score:.3f}"
    )
    print(f"  [PASS] Overfit config flagged: score={overfit_result.stability_score:.3f}")

    # ── Synthetic stable config: broad plateau ───────────────────────
    # Performance stays high across ±20% perturbations.
    stable_best = {"threshold": 0.50, "period": 14.0}

    def stable_eval(params: dict[str, float]) -> float:
        t = params["threshold"]
        p = params["period"]
        # Broad Gaussian: width ≈ 50% of value
        dt = (t - 0.50) / 0.25
        dp = (p - 14.0) / 7.0
        return 2.0 * math.exp(-(dt**2 + dp**2) * 0.5)

    stable_result = perturbation_stability_score(stable_eval, stable_best)
    assert not stable_result.is_overfit_spike, (  # noqa: S101 — self-test invariant; debug-only result check, intentionally silenced under `python -O`
        f"Stable config should NOT be flagged as spike, got score={stable_result.stability_score:.3f}"
    )
    print(f"  [PASS] Stable config passes: score={stable_result.stability_score:.3f}")

    # ── Per-parameter breakdown ──────────────────────────────────────
    for name, ratio in stable_result.per_param.items():
        assert ratio >= 0.5, (  # noqa: S101 — self-test invariant; debug-only per-param retention check, intentionally silenced under `python -O`
            f"Stable param '{name}' retention {ratio:.0%} should be ≥50%"
        )
    print(f"  [PASS] Per-param breakdown: {stable_result.per_param}")

    print("\nAll self-tests passed.")


if __name__ == "__main__":
    import sys

    if "--self-test" in sys.argv:
        _self_test()
    else:
        print("Usage: python -m forex_bot.srf.param_stability --self-test")
