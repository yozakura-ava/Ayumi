"""Conviction vector and kill-criterion types for the behavioral policy layer.

Phase 1a of the Brain + Behavioral Policy Consolidation sprint
(2026-07-10). Phase 1b (BehavioralPolicy) and Phase 1c (KillCriteria)
import KillCriterion from this module. The ConvictionVector.final_score
property preserves backward compatibility with legacy consumers that
expect a single weighted aggregate.

These types are pure data containers — no I/O, no imports from backtest
or forward_test_engine modules, no runtime side effects beyond clamping
in ConvictionVector.__post_init__.
"""

from dataclasses import dataclass
from typing import ClassVar

__all__ = ["ConvictionVector", "KillCriterion"]


def _clamp_unit(value: float) -> float:
    """Clamp a single score into [0.0, 1.0].

    NaN and infinities are coerced to 0.0 so downstream weighting
    stays finite and well-defined. Negative inputs clamp to 0.0;
    inputs above 1.0 clamp to 1.0.
    """
    if value != value:  # NaN check (NaN != NaN)
        return 0.0
    if value == float("inf"):
        return 1.0
    if value == float("-inf"):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@dataclass
class ConvictionVector:
    """Multi-component conviction score for a single signal.

    Each component lives in [0.0, 1.0] after construction (clamped in
    __post_init__). The weighted aggregate exposed via ``final_score``
    is the legacy single-number summary that older policy consumers
    read; newer consumers should branch on the components directly.

    Components
    ----------
    strategy_score:
        Raw strategy confidence for the signal that fired.
    confluence_score:
        Agreement across co-firing strategies.
    regime_fit:
        How well the active strategy fits the current market regime.
    freshness:
        Decay factor for the signal's age (1.0 = brand new, 0.0 = stale).
    """

    strategy_score: float
    confluence_score: float
    regime_fit: float
    freshness: float

    # Class-level so callers can override the weights before reading
    # final_score (assign to ``ConvictionVector.WEIGHTS`` to swap the
    # weighting globally; existing instances pick up the change via
    # attribute lookup). Sum is 1.0 by default; override at your own
    # risk — non-normalized weights will pull final_score outside
    # [0.0, 1.0]. Declared ``ClassVar`` so dataclasses treats it as a
    # class attribute rather than a per-instance field with a mutable
    # default (which would raise ``ValueError`` at class-creation time).
    WEIGHTS: ClassVar[dict[str, float]] = {
        "strategy_score": 0.40,
        "confluence_score": 0.25,
        "regime_fit": 0.25,
        "freshness": 0.10,
    }

    def __post_init__(self) -> None:
        self.strategy_score = _clamp_unit(self.strategy_score)
        self.confluence_score = _clamp_unit(self.confluence_score)
        self.regime_fit = _clamp_unit(self.regime_fit)
        self.freshness = _clamp_unit(self.freshness)

    @property
    def final_score(self) -> float:
        """Weighted aggregate over the (clamped) component scores.

        Returns a value in [0.0, 1.0] when ``WEIGHTS`` sums to 1.0 and
        every input has been clamped. Uses the class-level ``WEIGHTS``
        mapping so tests and callers can swap the weighting without
        subclassing.
        """
        components = {
            "strategy_score": self.strategy_score,
            "confluence_score": self.confluence_score,
            "regime_fit": self.regime_fit,
            "freshness": self.freshness,
        }
        return sum(components[name] * self.WEIGHTS[name] for name in components)


@dataclass
class KillCriterion:
    """Single result row from a kill-criteria evaluation.

    Captures one named check (spread, adx_range, session_window, ...)
    including the measured value, the threshold it was compared against,
    whether the criterion triggered (i.e. the signal should be killed),
    and a human-readable evidence string for logs and dashboards.

    Phase 1c (KillCriteria) builds ``list[KillCriterion]`` rows and
    exposes convenience helpers (count triggered, names of triggered
    criteria, etc.) on top of these dataclasses.
    """

    name: str
    triggered: bool
    value: float
    threshold: float
    evidence: str

    def __str__(self) -> str:
        status = "FAIL" if self.triggered else "PASS"
        return f"{self.name}: {status} value={self.value:.4f} threshold={self.threshold:.4f} ({self.evidence})"
