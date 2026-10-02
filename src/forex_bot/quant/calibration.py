"""calibration.py — Calibration + Brier-score analysis for binary signal systems.

For binary signal systems (win / loss), calibration answers the directly
actionable question: **does a 70% confidence signal actually win 70% of the
time?** This is more directly actionable than ICIR for binary outcomes, and
it decomposes into three interpretable components (Murphy 1973; Brier 1950).

The Brier score and its decomposition are well-known tools from the
probabilistic forecasting literature:

    BS = (1/N) * Σ (p_i - o_i)²

where ``p_i`` is the predicted probability and ``o_i ∈ {0, 1}`` is the
actual outcome (1 = win, 0 = loss). Brier score is mean squared error of
predicted probability versus actual outcome, so lower is better. A perfectly
calibrated system has Brier score equal to the **uncertainty** term
(``ō * (1 - ō)`` where ``ō`` is the base win rate).

Brier decomposition (Murphy 1973):

    BS = reliability - resolution + uncertainty

    reliability = (1/N) * Σ_k n_k * (f_k - ō_k)²
        — calibration within each bin (lower = better calibrated).
    resolution = (1/N) * Σ_k n_k * (ō_k - ō)²
        — ability to distinguish wins from losses (higher = sharper).
    uncertainty = ō * (1 - ō)
        — inherent uncertainty in the base rate (constant per dataset).

A well-calibrated system with high resolution (sharp separation of wins
from losses) will have a Brier score *lower* than uncertainty. A perfectly
calibrated, perfectly resolving system has BS = 0. A constant prediction
``p = ō`` has BS = uncertainty (no resolution, zero reliability cost).

Integration with walk-forward evaluation
----------------------------------------
``evaluate_calibration`` accepts per-trade confidences and outcomes as
explicit parameters, because ``WalkForwardResults`` (both variants in
``oos_gate.py`` and ``walk_forward.py``) currently stores either per-window
returns or aggregate metrics, not per-trade confidence data. When confidence
data is wired into the walk-forward pipeline, the integration card can
populate these parameters directly from per-trade signal records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .oos_gate import WalkForwardResults

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CalibrationBin:
    """One bin of the calibration curve.

    Attributes:
        bin_lower: lower edge of the confidence bin (inclusive).
        bin_upper: upper edge of the confidence bin (exclusive, except the
            last bin which is inclusive on the upper edge so confidence=1.0
            is included).
        count: number of signals falling in this bin.
        mean_confidence: mean predicted probability in the bin. ``NaN`` when
            ``count == 0`` (no signals to average).
        actual_win_rate: empirical win rate in the bin. ``NaN`` when
            ``count == 0``.
        brier_contribution: this bin's contribution to the overall Brier
            score: ``count * (mean_confidence - actual_win_rate)² / N``
            where ``N`` is the total signal count. ``0.0`` when
            ``count == 0``.
    """

    bin_lower: float
    bin_upper: float
    count: int
    mean_confidence: float
    actual_win_rate: float
    brier_contribution: float


@dataclass
class CalibrationReport:
    """Full calibration analysis of a binary signal system.

    Attributes:
        brier_score: overall Brier score, lower = better.
        calibration_curve: list of :class:`CalibrationBin`, one per bin.
        reliability: (1/N) * Σ_k n_k * (f_k - ō_k)². Lower is better —
            measures how well predicted probabilities match observed win
            rates within each bin.
        resolution: (1/N) * Σ_k n_k * (ō_k - ō)². Higher is better —
            measures how sharply the system separates wins from losses.
        uncertainty: ō * (1 - ō) where ō is the overall win rate. This is
            the Brier score of a constant prediction ``p = ō``; it's the
            floor a perfectly-calibrated system can reach.
        n_signals: total number of signals in the input. Convenience for
            downstream consumers.
        base_win_rate: overall empirical win rate (``ō``). Convenience.
    """

    brier_score: float
    calibration_curve: list[CalibrationBin] = field(default_factory=list)
    reliability: float = 0.0
    resolution: float = 0.0
    uncertainty: float = 0.0
    n_signals: int = 0
    base_win_rate: float = 0.0


# ---------------------------------------------------------------------------
# Core computations
# ---------------------------------------------------------------------------


def brier_score(
    predicted_probs: Sequence[float],
    actual_outcomes: Sequence[float],
) -> float:
    """Mean squared error of predicted probability versus actual outcome.

    Formula: ``BS = (1/N) * Σ (p_i - o_i)²``

    Both inputs must have the same length. ``actual_outcomes`` should be a
    binary sequence (0s and 1s); the formula works for continuous outcomes
    in [0, 1] but the Brier decomposition is only valid for binary outcomes.

    Args:
        predicted_probs: predicted win probabilities in [0, 1].
        actual_outcomes: actual outcomes (1 = win, 0 = loss).

    Returns:
        The Brier score as a non-negative float. ``0.0`` for perfect
        prediction; ``1.0`` for perfectly wrong (e.g., predict 1 when
        outcome is 0 or vice versa).

    Raises:
        ValueError: if inputs are empty or have different lengths.
    """
    p = np.asarray(predicted_probs, dtype=float)
    o = np.asarray(actual_outcomes, dtype=float)

    if p.size == 0 or o.size == 0:
        raise ValueError("brier_score requires at least one prediction/observation")
    if p.shape != o.shape:
        raise ValueError(f"predicted_probs and actual_outcomes must have the same shape, got {p.shape} vs {o.shape}")

    return float(np.mean((p - o) ** 2))


def _build_bin_edges(n_bins: int) -> np.ndarray:
    """Construct ``n_bins + 1`` bin edges spanning [0, 1].

    The last edge is ``1.0`` so confidence=1.0 is included in the final bin.
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # Ensure the last edge is exactly 1.0 to catch confidences of 1.0.
    edges[-1] = 1.0
    return edges


def calibration_curve(
    confidences: Sequence[float],
    outcomes: Sequence[float],
    n_bins: int = 10,
) -> list[CalibrationBin]:
    """Bucket signals by confidence and compare predicted vs actual win rate.

    Uses uniform-width bins spanning [0, 1] (the natural range for predicted
    probabilities). Each bin is left-inclusive, right-exclusive, except the
    last bin which is inclusive on both edges so a confidence of exactly
    1.0 is included. Empty bins are returned with ``count=0`` and
    ``mean_confidence=actual_win_rate=NaN``.

    Args:
        confidences: predicted win probabilities in [0, 1].
        outcomes: actual outcomes (1 = win, 0 = loss).
        n_bins: number of bins (default 10). Must be >= 1.

    Returns:
        List of :class:`CalibrationBin` of length ``n_bins``.

    Raises:
        ValueError: if inputs are empty, have different lengths, or
            ``n_bins < 1``.
    """
    conf = np.asarray(confidences, dtype=float)
    out = np.asarray(outcomes, dtype=float)

    if conf.size == 0 or out.size == 0:
        raise ValueError("calibration_curve requires at least one confidence/observation")
    if conf.shape != out.shape:
        raise ValueError(f"confidences and outcomes must have the same shape, got {conf.shape} vs {out.shape}")
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")

    edges = _build_bin_edges(n_bins)
    n_total = conf.size

    # Assign each confidence to a 0-indexed bin.
    # np.digitize with right=False returns the 1-indexed bin position
    # (bins[i-1] <= x < bins[i]); subtract 1 for 0-indexed bins. Clamp to
    # [0, n_bins-1] so confidence==1.0 (which would otherwise return
    # n_bins+1) lands in the last bin.
    bin_idx = np.digitize(conf, edges, right=False) - 1
    bin_idx = np.clip(bin_idx, 0, n_bins - 1)

    bins: list[CalibrationBin] = []
    for k in range(n_bins):
        mask = bin_idx == k
        n_k = int(mask.sum())
        if n_k == 0:
            bins.append(
                CalibrationBin(
                    bin_lower=float(edges[k]),
                    bin_upper=float(edges[k + 1]),
                    count=0,
                    mean_confidence=float("nan"),
                    actual_win_rate=float("nan"),
                    brier_contribution=0.0,
                )
            )
            continue

        mean_conf = float(conf[mask].mean())
        actual_wr = float(out[mask].mean())
        bin_bs = (mean_conf - actual_wr) ** 2
        brier_contrib = float(n_k) * bin_bs / float(n_total)

        bins.append(
            CalibrationBin(
                bin_lower=float(edges[k]),
                bin_upper=float(edges[k + 1]),
                count=n_k,
                mean_confidence=mean_conf,
                actual_win_rate=actual_wr,
                brier_contribution=brier_contrib,
            )
        )

    return bins


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------


def brier_decomposition(
    confidences: Sequence[float],
    outcomes: Sequence[float],
    n_bins: int = 10,
) -> tuple[float, float, float]:
    """Compute (reliability, resolution, uncertainty).

    The classic Murphy (1973) decomposition of the Brier score for binary
    outcomes:

        BS = reliability - resolution + uncertainty

    The terms are computed on uniform-width confidence bins:

        reliability = (1/N) * Σ_k n_k * (f_k - ō_k)²
        resolution  = (1/N) * Σ_k n_k * (ō_k - ō)²
        uncertainty = ō * (1 - ō)

    where ``f_k`` is the mean predicted probability in bin ``k`` and
    ``ō_k`` is the empirical win rate in bin ``k``, and ``ō`` is the
    overall empirical win rate.

    Args:
        confidences: predicted win probabilities in [0, 1].
        outcomes: actual outcomes (1 = win, 0 = loss).
        n_bins: number of bins for the decomposition (default 10).

    Returns:
        ``(reliability, resolution, uncertainty)`` tuple.

    Raises:
        ValueError: if inputs are empty, mismatched, or ``n_bins < 1``.
    """
    conf = np.asarray(confidences, dtype=float)
    out = np.asarray(outcomes, dtype=float)
    if conf.size == 0 or out.size == 0:
        raise ValueError("brier_decomposition requires at least one confidence/observation")
    if conf.shape != out.shape:
        raise ValueError(f"confidences and outcomes must have the same shape, got {conf.shape} vs {out.shape}")
    if n_bins < 1:
        raise ValueError(f"n_bins must be >= 1, got {n_bins}")

    n = int(conf.size)
    base_rate = float(out.mean())  # ō
    uncertainty = base_rate * (1.0 - base_rate)  # ō * (1 - ō)

    bins = calibration_curve(confidences, outcomes, n_bins=n_bins)

    reliability = 0.0
    resolution = 0.0
    for b in bins:
        if b.count == 0:
            continue
        n_k = float(b.count)
        reliability += n_k * (b.mean_confidence - b.actual_win_rate) ** 2
        resolution += n_k * (b.actual_win_rate - base_rate) ** 2
    reliability /= n
    resolution /= n

    return float(reliability), float(resolution), float(uncertainty)


# ---------------------------------------------------------------------------
# Walk-forward integration
# ---------------------------------------------------------------------------


def evaluate_calibration(
    wf_results: WalkForwardResults,
    confidences: Sequence[float] | None = None,
    outcomes: Sequence[float] | None = None,
    n_bins: int = 10,
) -> CalibrationReport:
    """Evaluate calibration of a walk-forward evaluation.

    Because ``WalkForwardResults`` (in ``oos_gate.py``) does not yet carry
    per-trade confidence data, this function accepts confidence and outcome
    sequences as explicit parameters. Integration with the walk-forward
    pipeline (populating these from per-trade signal records) is a separate
    card.

    Outcomes can be derived from per-trade returns: a return > 0 is a win
    (1), a return ≤ 0 is a loss (0). When only ``wf_results`` is given
    (no explicit ``outcomes``), outcomes are derived this way. The
    ``confidences`` parameter is **required** — there is no way to derive
    it from a ``WalkForwardResults`` object that only carries returns.

    Args:
        wf_results: walk-forward evaluation results. Used for context
            (strategy_name, pair, etc.) and, when ``outcomes`` is not given,
            to derive per-trade outcomes from per-window returns.
        confidences: per-trade predicted win probabilities in [0, 1].
            Required. Length must match the total trade count of
            ``wf_results`` (or the length of ``outcomes`` if both are given).
        outcomes: per-trade actual outcomes (1 = win, 0 = loss). When
            ``None``, derived from ``wf_results.per_window_trade_returns``
            (return > 0 → 1, return ≤ 0 → 0).
        n_bins: number of bins for the calibration curve (default 10).

    Returns:
        :class:`CalibrationReport` with the overall Brier score, the
        per-bin calibration curve, and the Brier decomposition
        (reliability, resolution, uncertainty).

    Raises:
        ValueError: if ``confidences`` is not provided, if the input
            sequences have inconsistent lengths, or if there are zero
            signals to evaluate.
    """
    if confidences is None:
        raise ValueError(
            "confidences is required: WalkForwardResults does not currently "
            "carry per-trade confidence data. Pass it as a parameter; the "
            "integration card will populate it from per-trade signal records."
        )

    if outcomes is None:
        # Derive outcomes from per-window returns. The wf_results contract
        # (oos_gate.py) flattens per-window trade returns into one long
        # series; we do the same here for outcome derivation.
        flat_returns: list[float] = []
        for window in wf_results.per_window_trade_returns:
            flat_returns.extend(window)
        if not flat_returns:
            raise ValueError(
                "Cannot derive outcomes: wf_results has no per-trade returns. Provide outcomes explicitly."
            )
        outcomes = [1 if r > 0.0 else 0 for r in flat_returns]

    bs = brier_score(confidences, outcomes)
    curve = calibration_curve(confidences, outcomes, n_bins=n_bins)
    reliability, resolution, uncertainty = brier_decomposition(confidences, outcomes, n_bins=n_bins)

    n_signals = int(np.asarray(outcomes).size)
    base_rate = float(np.mean(outcomes))

    return CalibrationReport(
        brier_score=bs,
        calibration_curve=curve,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        n_signals=n_signals,
        base_win_rate=base_rate,
    )


__all__ = [
    "CalibrationBin",
    "CalibrationReport",
    "brier_score",
    "brier_decomposition",
    "calibration_curve",
    "evaluate_calibration",
]
