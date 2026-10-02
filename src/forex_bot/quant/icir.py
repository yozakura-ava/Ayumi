"""
icir.py — Information Coefficient Information Ratio (ICIR) computations.

This module implements the pure math for evaluating whether a strategy's
*forecast* (continuous confidence score) is predictive of the *outcome*
(realized R-multiple), and whether that skill is persistent across
walk-forward (WF) windows.

Definitions (canonical references: Grinold & Kahn, Active Portfolio
Management; MicroAlphas "Information Coefficient guide"; see
``docs/research/icir-research-2026-07-08.md``):

* **IC (Information Coefficient)** — Spearman rank correlation between the
  signal's confidence scores and realized R-multiples. Computed per
  evaluation period; for Ayumi, evaluation period = one WF window.
* **ICIR** — time-series Sharpe of the IC vector: ``mean(IC) / std(IC)``.
  Measures persistence of skill across evaluation periods. For 30+
  periods the value is statistically meaningful; below 10 it is noise.
* **Confidence tiers** — quality annotation of the ICIR estimate based on
  the number of evaluation periods:
    - ``n < 10`` → ``'low'`` (essentially noise)
    - ``10 <= n < 30`` → ``'medium'``
    - ``n >= 30`` → ``'high'``

Design notes
------------
* Functions are pure (no I/O). ``evaluate_icir`` accepts pre-loaded WF
  results; the cohort dashboard in ``scripts/generate_cohort_dashboard.py``
  is responsible for reading JSONL files.
* NaN-safe: returns NaN for uncomputable cases (insufficient data, all
  identical values, constant forecasts). Callers should check the
  ``confidence`` tier to interpret NaN as expected vs. unexpected.
* Spearman is used (not Pearson) for robustness to outliers and because
  confidence is rank-monotonic with edge at most desks.

See Also
--------
* :mod:`quant.icir_monitor` — live rolling 30/60/90-day ICIR during
  forward test.
* :mod:`quant.dsr_integration` — deflated-Sharpe companion filter.
* ``docs/research/icir-research-2026-07-08.md`` — full research note.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
from scipy.stats import spearmanr

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Confidence tier thresholds based on ``n_windows`` / ``n_periods``.
#: Numbers mirror ``docs/research/icir-research-2026-07-08.md`` §3.3.
CONF_HIGH_MIN_N = 30
CONF_MEDIUM_MIN_N = 10

#: Forward-test decay threshold. Below this rolling 30-day ICIR the
#: :class:`quant.icir_monitor.IcirMonitor` raises ``decay_alert=True``.
DECAY_ALERT_THRESHOLD = 0.3

#: Minimum number of weekly/daily evaluation periods before any
#: rolling ICIR is considered meaningful (otherwise returns NaN).
MIN_PERIODS_FOR_ICIR = 3

#: Minimum number of observations within a single evaluation period
#: required to compute a stable Spearman IC.
MIN_OBS_FOR_IC = 3

#: Tolerance below which std is treated as 0 (constant IC vector).
#: See :func:`_safe_std` for rationale.
_STD_EPSILON = 1e-12


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IcirResult:
    """Output of :func:`icir` — time-series persistence of IC across windows."""

    #: Mean IC across the supplied windows. NaN if uncomputable.
    mean_ic: float
    #: Sample standard deviation of IC (ddof=1). NaN if uncomputable.
    std_ic: float
    #: ``mean_ic / std_ic``. NaN if std is zero or undetermined.
    icir: float
    #: Number of evaluation periods (IC observations).
    n_windows: int
    #: Confidence tier: ``'low'`` (``n < 10``), ``'medium'`` (``10..29``),
    #: ``'high'`` (``>= 30``). ``'low'`` when uncomputable.
    confidence: str
    #: Optional human-readable note explaining NaN / underpopulated result.
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (NaN preserved as ``None``)."""
        return {
            "mean_ic": _nan_to_none(self.mean_ic),
            "std_ic": _nan_to_none(self.std_ic),
            "icir": _nan_to_none(self.icir),
            "n_windows": self.n_windows,
            "confidence": self.confidence,
            "note": self.note,
        }


@dataclass(frozen=True)
class EvaluateIcirResult:
    """Output of :func:`evaluate_icir` — applied across WF results."""

    icir: float
    mean_ic: float
    std_ic: float
    n_windows: int
    confidence: str
    #: IC observations (one per window) for time-series inspection;
    #: may be NaN entries for windows with insufficient data.
    per_window_ic: tuple[float, ...]
    #: Number of windows that contributed a real IC (had >= MIN_OBS_FOR_IC).
    n_windows_with_ic: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "icir": _nan_to_none(self.icir),
            "mean_ic": _nan_to_none(self.mean_ic),
            "std_ic": _nan_to_none(self.std_ic),
            "n_windows": self.n_windows,
            "confidence": self.confidence,
            "per_window_ic": [_nan_to_none(x) for x in self.per_window_ic],
            "n_windows_with_ic": self.n_windows_with_ic,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nan_to_none(x: float | None) -> Any:
    """Convert NaN / ±inf to ``None`` for JSON; pass other floats through.

    ±infinity are not valid JSON literals, so we collapse them to ``None``
    to keep the output strictly JSON. Callers can detect "very large"
    ICIR by checking ``std_ic == 0`` together with ``mean_ic`` sign.
    """
    if x is None:
        return None
    if isinstance(x, (int, float)) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


# Kept for backward-compat with any external callers that imported the
# old single-purpose helper name.
def _nan_to_none_single(x: float) -> Any:
    if x is None:
        return None
    if isinstance(x, (int, float)) and math.isnan(x):
        return None
    return x


def _to_float_list(values: Iterable[Any]) -> list[float]:
    """Coerce an iterable of ints/floats/None to a list of floats (NaN for None)."""
    out: list[float] = []
    for v in values:
        if v is None:
            out.append(math.nan)
        else:
            out.append(float(v))
    return out


def _confidence_tier(n: int) -> str:
    """Map ``n`` evaluation periods to a confidence tier label."""
    if n >= CONF_HIGH_MIN_N:
        return "high"
    if n >= CONF_MEDIUM_MIN_N:
        return "medium"
    return "low"


def _safe_std(arr: np.ndarray) -> float:
    """Sample std (ddof=1); returns NaN when uncomputable or all-equal.

    Treats std values below :data:`_STD_EPSILON` as 0 (constant IC).
    Catches the floating-point artifact where ``[0.05] * 6`` produces a
    non-zero std of ~1e-17 due to representation error in the binary
    float. Without this guard, ``mean/std`` blows up to 1e16 and ICIR
    becomes meaningless. Empirical threshold chosen to be orders of
    magnitude smaller than the smallest IC a strategy could realistically
    produce (typical IC magnitudes are > 1e-4).
    """
    if arr.size < 2:
        return math.nan
    std = float(np.std(arr, ddof=1))
    if math.isnan(std):
        return math.nan
    if std < _STD_EPSILON:
        return 0.0
    return std


#: Tolerance below which std is treated as 0 (constant IC vector).
#: See :func:`_safe_std` for rationale.
_STD_EPSILON = 1e-12


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def information_coefficient(
    confidences: Sequence[float],
    r_multiples: Sequence[float],
) -> float:
    """Spearman rank IC between signal confidence and realized R-multiple.

    Parameters
    ----------
    confidences, r_multiples:
        Equal-length sequences of forecasts and outcomes. May be Python
        lists, tuples, or ``numpy.ndarray`` of floats.

    Returns
    -------
    float
        Spearman correlation coefficient in [-1, +1]. Returns NaN when:

        * either sequence is empty,
        * lengths differ,
        * fewer than :data:`MIN_OBS_FOR_IC` observations (Spearman needs
          at least 3 to produce a stable estimate),
        * all confidence values or all R-multiples are identical (zero
          variance → undefined correlation),
        * any NaN in either sequence.
    """
    if confidences is None or r_multiples is None:
        return math.nan
    if len(confidences) != len(r_multiples):
        return math.nan
    if len(confidences) < MIN_OBS_FOR_IC:
        return math.nan

    c = np.asarray(confidences, dtype=float)
    r = np.asarray(r_multiples, dtype=float)
    if c.ndim != 1 or r.ndim != 1:
        return math.nan
    if c.size != r.size:
        return math.nan
    if np.any(np.isnan(c)) or np.any(np.isnan(r)):
        return math.nan

    # If either array has zero variance, Spearman is mathematically undefined.
    # scipy.stats.spearmanr raises in this case on newer versions; guard it.
    if float(np.std(c)) == 0.0 or float(np.std(r)) == 0.0:
        return math.nan

    result = spearmanr(c, r)
    # scipy <1.11 returns a namedtuple (SpearmanrResult) which has .correlation;
    # newer scipy returns a similar object. Guard both shapes.
    rho = getattr(result, "correlation", None)
    if rho is None:
        rho = getattr(result, "statistic", None)
    if rho is None:
        # Bare float fallback (very old scipy or array-shaped output).
        try:
            rho = float(result[0])
        except (TypeError, IndexError):
            return math.nan
    rho_f = float(rho)
    if math.isnan(rho_f):
        return math.nan
    # Clamp to [-1, 1] to guard against floating-point overshoot from scipy.
    return max(-1.0, min(1.0, rho_f))


def icir(ic_values: Sequence[float]) -> dict[str, Any]:
    """ICIR (time-series Sharpe of the IC vector) plus diagnostics.

    Parameters
    ----------
    ic_values:
        IC observations, one per evaluation window. NaN entries are
        excluded from the mean/std calculation but counted in
        ``n_windows`` (so callers can see the underlying evidence base).

    Returns
    -------
    dict
        ``{'icir': float | None, 'mean_ic': float | None,
           'std_ic': float | None, 'n_windows': int,
           'confidence': 'low'|'medium'|'high', 'note': str}``

        ``icir = mean(IC) / std(IC)`` with sample std (ddof=1).

        When there are fewer than :data:`MIN_PERIODS_FOR_ICIR` *non-NaN*
        observations, ``icir`` is reported as ``None`` and the note
        explains why.
    """
    vals = _to_float_list(ic_values)
    n_total = len(vals)

    if n_total == 0:
        return IcirResult(
            mean_ic=math.nan,
            std_ic=math.nan,
            icir=math.nan,
            n_windows=0,
            confidence="low",
            note="no IC observations supplied",
        ).to_dict()

    arr = np.asarray(vals, dtype=float)
    valid_mask = ~np.isnan(arr)
    valid = arr[valid_mask]
    n_valid = int(valid.size)

    if n_valid < MIN_PERIODS_FOR_ICIR:
        return IcirResult(
            mean_ic=math.nan,
            std_ic=math.nan,
            icir=math.nan,
            n_windows=n_valid,
            confidence=_confidence_tier(n_valid),
            note=(f"only {n_valid} valid IC observations (need >= {MIN_PERIODS_FOR_ICIR}); not enough for ICIR"),
        ).to_dict()

    mean_ic = float(np.mean(valid))
    std_ic = _safe_std(valid)
    if std_ic is None or (not math.isnan(std_ic) and std_ic == 0.0):
        # Zero variance across windows. Three sub-cases:
        #  - mean near 0: no signal at all → ICIR NaN.
        #  - mean positive: very strong consistent edge → ICIR = +inf
        #  - mean negative: very strong consistent anti-edge → ICIR = -inf
        # Callers should treat ±inf as "consistent" and a finite large
        # number as the goal; production triage compares the magnitude.
        if abs(mean_ic) < _STD_EPSILON:
            icir_value: float = math.nan
            constant_note = "IC vector has zero variance and mean near zero; no signal. ICIR undefined."
        else:
            icir_value = math.copysign(math.inf, mean_ic)
            constant_note = (
                f"IC vector has zero variance at mean={mean_ic:.6f}; "
                "skill is consistent. ICIR is infinite — the IC value is "
                "stable across windows, which is the strongest-skill regime. "
                "Compare magnitudes to other streams rather than treating "
                "this as a numerical error."
            )
        return IcirResult(
            mean_ic=mean_ic,
            std_ic=0.0,
            icir=icir_value,
            n_windows=n_valid,
            confidence=_confidence_tier(n_valid),
            note=constant_note,
        ).to_dict()

    period_icir = mean_ic / std_ic
    return IcirResult(
        mean_ic=mean_ic,
        std_ic=float(std_ic),
        icir=float(period_icir),
        n_windows=n_valid,
        confidence=_confidence_tier(n_valid),
        note="",
    ).to_dict()


def evaluate_icir(wf_results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate ICIR across walk-forward results.

    Parameters
    ----------
    wf_results:
        Sequence of per-window result dicts. For each window, the
        function looks for confidence/R-multiple data in this order:

        1. ``confidences`` + ``r_multiples`` — explicit per-trade lists.
           Computes a true Spearman IC for that window. Needs
           ``>= MIN_OBS_FOR_IC`` items.
        2. ``mean_confidence`` + ``mean_r_multiple`` — pre-aggregated
           window scalars. Produces a single cross-section of one
           observation; IC is undefined so the window contributes only
           to the IC-via-surrogate path described below.
        3. Neither → the window is recorded but contributes NaN to the
           IC series.

        The first (true per-trade) path is the recommended one
        (``docs/research/icir-research-2026-07-08.md`` §7.4).

        When the entire input lacks per-trade data the result has
        ``n_windows_with_ic=0`` and confidence='low' with a note
        explaining why ICIR is NaN; this is the expected outcome with
        the current aggregate-only WF reports.

    Returns
    -------
    dict
        ``EvaluateIcirResult.to_dict()`` shape — see above.
    """
    wf_list = list(wf_results) if wf_results is not None else []
    if not wf_list:
        return EvaluateIcirResult(
            icir=math.nan,
            mean_ic=math.nan,
            std_ic=math.nan,
            n_windows=0,
            confidence="low",
            per_window_ic=(),
            n_windows_with_ic=0,
            note=(
                "no WF window observations supplied; ICIR requires "
                "per-window (confidences, r_multiples) pairs to compute "
                "a cross-sectional Spearman IC (see "
                "docs/research/icir-research-2026-07-08.md §7.4). For "
                "aggregate-only WF reports, ICIR stays null until the "
                "panel construction pipeline lands."
            ),
        ).to_dict()

    per_window_ic: list[float] = []
    n_with_ic = 0
    n_aggregate_only = 0
    aggregate_confs: list[float] = []
    aggregate_rs: list[float] = []

    for idx, window in enumerate(wf_list):  # noqa: B007
        if not isinstance(window, dict):
            per_window_ic.append(math.nan)
            continue

        # Path 1: per-trade data
        if "confidences" in window and "r_multiples" in window:
            try:
                ic = information_coefficient(window["confidences"], window["r_multiples"])
            except Exception:  # noqa: BLE001 — never let one window kill the run
                ic = math.nan
            if not math.isnan(ic):
                n_with_ic += 1
            per_window_ic.append(ic)
            continue

        # Path 2: per-window aggregates — cannot compute true cross-sectional
        # IC from a single observation. Record for transparency.
        mean_conf = window.get("mean_confidence")
        mean_r = window.get("mean_r_multiple")
        if (
            mean_conf is not None
            and mean_r is not None
            and not (isinstance(mean_conf, float) and math.isnan(mean_conf))
            and not (isinstance(mean_r, float) and math.isnan(mean_r))
        ):
            aggregate_confs.append(float(mean_conf))
            aggregate_rs.append(float(mean_r))
            n_aggregate_only += 1
            per_window_ic.append(math.nan)
            continue

        # Path 3: nothing usable in this window.
        per_window_ic.append(math.nan)

    if n_with_ic == 0:
        note_parts = [f"{n_aggregate_only} windows with aggregate-only data"]
        if n_aggregate_only == 0:
            note_parts.append("no per-trade confidence/R-multiple available")
        note_parts.append(
            "ICIR requires per-trade confidence/R-multiple per WF window "
            "to compute a true cross-sectional IC (see doc §7.4); aggregate "
            "data yields < MIN_OBS_FOR_IC observations per window"
        )
        return EvaluateIcirResult(
            icir=math.nan,
            mean_ic=math.nan,
            std_ic=math.nan,
            n_windows=len(per_window_ic),
            confidence="low",
            per_window_ic=tuple(per_window_ic),
            n_windows_with_ic=0,
            note="; ".join(note_parts),
        ).to_dict()

    # We have at least some per-trade IC observations. Compute ICIR over them.
    summary = icir(per_window_ic)
    note = summary.get("note", "")
    return EvaluateIcirResult(
        icir=float(summary["icir"]) if summary["icir"] is not None else math.nan,
        mean_ic=float(summary["mean_ic"]) if summary["mean_ic"] is not None else math.nan,
        std_ic=float(summary["std_ic"]) if summary["std_ic"] is not None else math.nan,
        # ``n_windows`` is the total window count in the input;
        # ``n_windows_with_ic`` is the count of windows that contributed
        # a real IC observation (per-trade data was usable).
        n_windows=len(per_window_ic),
        confidence=str(summary["confidence"]),
        per_window_ic=tuple(per_window_ic),
        n_windows_with_ic=n_with_ic,
        note=note,
    ).to_dict()


__all__ = [
    "CONF_HIGH_MIN_N",
    "CONF_MEDIUM_MIN_N",
    "DECAY_ALERT_THRESHOLD",
    "MIN_PERIODS_FOR_ICIR",
    "MIN_OBS_FOR_IC",
    "EvaluateIcirResult",
    "IcirResult",
    "evaluate_icir",
    "icir",
    "information_coefficient",
]
