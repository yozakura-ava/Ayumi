"""Candidate generator for the XAUUSD regime-gated active-blade tuning cycle.

The 24-candidate decomposition (bounded by ``docs/trading/active-blade.md``
§ Tuning Budget on branch ``autodev/one-blade-focus``):

    24 = 3 regimes × 2 sizing profiles × 4 signal-weight ratios

- **3 regimes** — trending / ranging / volatile. Each candidate carries a
  ``target_regime`` gate; the regime detector (``detect_regime_for_window``)
  labels each walk-forward window and the candidate's weighting applies when
  the detected regime matches its target.
- **2 sizing profiles** — ``fixed_fraction`` (flat 1% risk per trade) and
  ``tiered_confidence`` (confidence-tiered risk via
  ``ConfidencePositionSizer`` tiers).
- **4 signal-weight ratios** — the primary component (``ttc_xauusd`` M15)
  weighted 0.5 / 1.0 / 1.5 / 2.0× relative to the secondary components
  (``donchian_atr_trend_v2``, ``dual_tf_squeeze_pro``).

Anything larger than this grid is overfit on 4 years of M15 data; the grid
is deliberately frozen here so no caller can silently widen it.

Note: the component strategies ``donchian_atr_trend_v2`` and
``dual_tf_squeeze_pro`` are designations in the active-blade doc. The
runnable signal engine in this repo is ``TTSStrategy``; component weights
are persisted as declarative parameters in each candidate record so the
blend layer can consume them once those components land.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- Frozen tuning grid (active-blade.md § Tuning Budget) -------------------

REGIMES: tuple[str, ...] = ("trending", "ranging", "volatile")

SIZING_PROFILES: dict[str, dict[str, Any]] = {
    "fixed_fraction": {
        "description": "Flat risk fraction per trade regardless of confidence",
        "tiers": [[0.0, 1.0, 0.01]],
    },
    "tiered_confidence": {
        "description": "Confidence-tiered risk via ConfidencePositionSizer",
        "tiers": [[0.85, 1.0, 0.01], [0.70, 0.85, 0.0075], [0.55, 0.70, 0.005]],
    },
}

SIGNAL_WEIGHT_RATIOS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)

PRIMARY_COMPONENT = "ttc_xauusd"
SECONDARY_COMPONENTS = ("donchian_atr_trend_v2", "dual_tf_squeeze_pro")

# Hard gates (active-blade.md § Noise Guards / § Exit Criteria)
MAX_DD_HARD_KILL = 0.08  # -8% max drawdown on any window → candidate killed
N_WINDOWS = 4

_CYCLE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Candidate:
    """One parameter set in the 24-candidate tuning grid."""

    candidate_id: str
    target_regime: str
    sizing_profile: str
    signal_weight_ratio: float
    component_weights: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "target_regime": self.target_regime,
            "sizing_profile": self.sizing_profile,
            "sizing_tiers": SIZING_PROFILES[self.sizing_profile]["tiers"],
            "signal_weight_ratio": self.signal_weight_ratio,
            "component_weights": dict(self.component_weights),
            "hard_gates": {
                "max_dd_hard_kill": MAX_DD_HARD_KILL,
                "n_windows": N_WINDOWS,
            },
        }


def _component_weights(ratio: float) -> dict[str, float]:
    """Primary weighted ``ratio``× relative to each secondary component.

    Secondaries share the remaining weight equally normalised against the
    primary so the blend always sums to a positive, finite book.
    """
    secondary = 1.0
    total = ratio + secondary * len(SECONDARY_COMPONENTS)
    weights = {PRIMARY_COMPONENT: ratio / total}
    for comp in SECONDARY_COMPONENTS:
        weights[comp] = secondary / total
    return {k: round(v, 6) for k, v in weights.items()}


def generate_candidates() -> list[Candidate]:
    """Generate the frozen 24-candidate grid (3 regimes × 2 sizing × 4 ratios)."""
    candidates: list[Candidate] = []
    for regime in REGIMES:
        for sizing in SIZING_PROFILES:
            for ratio in SIGNAL_WEIGHT_RATIOS:
                candidates.append(
                    Candidate(
                        candidate_id=f"{regime}-{sizing}-w{ratio:g}".replace(".", "p"),
                        target_regime=regime,
                        sizing_profile=sizing,
                        signal_weight_ratio=ratio,
                        component_weights=_component_weights(ratio),
                    )
                )
    if len(candidates) != 24:
        raise RuntimeError(f"tuning grid drifted: {len(candidates)} != 24 candidates")
    return candidates
    return candidates


def candidates_for_regime(regime: str) -> list[Candidate]:
    """The 8 candidates whose target regime matches ``regime``."""
    if regime not in REGIMES:
        raise ValueError(f"unknown regime {regime!r}; expected one of {REGIMES}")
    return [c for c in generate_candidates() if c.target_regime == regime]


def candidates_manifest(regime: str | None = None) -> list[dict[str, Any]]:
    """JSON-serialisable manifest of the 24-candidate grid.

    When ``regime`` is given, the manifest still lists all 24 candidates
    (the frozen grid is never subsetted silently) but marks the 8 candidates
    of that regime as ``cycle_focus: true``.
    """
    if regime is not None and regime not in REGIMES:
        raise ValueError(f"unknown regime {regime!r}; expected one of {REGIMES}")
    return [
        {**c.to_dict(), "cycle_focus": (regime is None or c.target_regime == regime)}
        for c in generate_candidates()
    ]


def validate_cycle_id(cycle_id: str) -> str:
    """Reject cycle ids that are not safe directory names.

    Blocks path separators, ``..`` traversal, whitespace, and empty strings.
    """
    if not _CYCLE_ID_RE.match(cycle_id):
        raise ValueError(
            "cycle_id must be 1-64 chars of [a-zA-Z0-9_-] starting alphanumeric; "
            f"got {cycle_id!r}"
        )
    if cycle_id in {".", ".."}:
        raise ValueError("cycle_id must not be a path fragment")
    return cycle_id
