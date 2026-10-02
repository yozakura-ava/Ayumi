"""Behavioral policy that scales trade size based on streak and drawdown.

Phase 1b of the Brain + Behavioral Policy Consolidation sprint
(2026-07-10). Phase 1a already shipped :class:`core.conviction.ConvictionVector`
and :class:`core.conviction.KillCriterion` (commit 7a14664). Phase 1c
(:class:`policy.kill_criteria.KillCriteria`) consumes the
:class:`KillCriterion` rows. This module exposes
:class:`BehavioralPolicy` and :class:`BehavioralResult`.

Council decision (2026-07-10, Kaito / Nora / Ren / Sora): the multiplier
is **hard-clamped to** ``[min_multiplier, max_multiplier]`` (default
``[0.25, 1.0]``) and may NEVER amplify past the base size. The ceiling
is fixed at ``1.0`` in the council-approved config; even if a caller
overrides ``max_multiplier`` it must not exceed ``1.0`` for production
deployments.

The policy is pure: no I/O, no logger side-effects, no time-of-day
dependencies beyond what the caller passes in via ``context``. Same
inputs ⇒ same outputs. This keeps it safe to unit-test and to call
from the live trade loop or the backtest engine without environment
plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BehavioralPolicy", "BehavioralResult"]


# ---------------------------------------------------------------------------
# Defaults — council-approved Phase 1b values (2026-07-10)
# ---------------------------------------------------------------------------
# Streak: 3 losses halves size, 5 losses hits the 0.25 floor.
DEFAULT_STREAK_LOSS_THRESHOLDS: dict[int, float] = {3: 0.5, 5: 0.25}
# Drawdown: >3% halves size, >5% hits the 0.25 floor. Strict ``>`` so
# a 3.00% drawdown does not yet trigger the cooldown.
DEFAULT_DD_THRESHOLDS: dict[float, float] = {3.0: 0.5, 5.0: 0.25}

# Multiplier floor/ceiling. The ceiling is 1.0 — behavioral policy NEVER
# amplifies a base size, only dampens it.
DEFAULT_MIN_MULTIPLIER: float = 0.25
DEFAULT_MAX_MULTIPLIER: float = 1.0


@dataclass
class BehavioralResult:
    """Outcome of a :meth:`BehavioralPolicy.evaluate` call.

    Attributes
    ----------
    multiplier:
        Final size multiplier in ``[min_multiplier, max_multiplier]``
        (default ``[0.25, 1.0]``). Callers apply it as
        ``sized_amount = base_size * result.multiplier``.
    adjustments:
        Human-readable strings describing every penalty that fired.
        Empty list when no penalty applied (multiplier stayed at 1.0).
    """

    multiplier: float
    adjustments: list[str] = field(default_factory=list)


class BehavioralPolicy:
    """Cooldown / size-scaling policy driven by streak and drawdown.

    Configuration (all optional, passed via the ``config`` dict):

    ``streak_loss_thresholds``
        Mapping of ``consecutive_losses`` → ``multiplier``. When the
        current streak is at or above a key, that multiplier becomes
        a candidate. The lowest (most restrictive) candidate wins.
        Default: ``{3: 0.5, 5: 0.25}``.
    ``dd_thresholds``
        Mapping of ``daily_drawdown_pct`` (strict ``>``) → ``multiplier``.
        Same "lowest matching wins" rule. Default: ``{3.0: 0.5, 5.0: 0.25}``.
    ``min_multiplier``
        Floor for the final multiplier. Default ``0.25``.
    ``max_multiplier``
        Ceiling for the final multiplier. Default ``1.0`` (never
        amplifies the base size).

    Evaluation order:

    1. Start at multiplier = 1.0.
    2. If ``consecutive_losses`` reaches any streak threshold, apply
       the lowest matching multiplier (call it ``streak_m``).
    3. If ``daily_drawdown_pct`` strictly exceeds any DD threshold,
       apply the lowest matching multiplier (call it ``dd_m``).
    4. ``multiplier = min(streak_m, dd_m)`` — most restrictive wins.
    5. Clamp to ``[min_multiplier, max_multiplier]``.
    6. Record every applied penalty in ``adjustments`` with a
       human-readable string for logs and dashboards.

    The policy is stateless across calls — every ``evaluate`` is
    independent. Build one instance at startup and reuse it.
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # Shallow-copy the threshold dicts so a caller mutating their
        # own dict afterwards cannot change policy state mid-flight.
        # Values are floats, keys are ints/floats — both immutable.
        self.streak_loss_thresholds: dict[int, float] = dict(
            cfg.get("streak_loss_thresholds", DEFAULT_STREAK_LOSS_THRESHOLDS)
        )
        self.dd_thresholds: dict[float, float] = dict(cfg.get("dd_thresholds", DEFAULT_DD_THRESHOLDS))
        self.min_multiplier: float = float(cfg.get("min_multiplier", DEFAULT_MIN_MULTIPLIER))
        self.max_multiplier: float = float(cfg.get("max_multiplier", DEFAULT_MAX_MULTIPLIER))

    def evaluate(self, base_size: float, context: dict) -> BehavioralResult:
        """Compute the size multiplier for the current trading context.

        Parameters
        ----------
        base_size:
            The pre-policy base size in lots / units. Accepted for API
            symmetry with downstream sizing code; not used to scale the
            multiplier (the multiplier itself is bounded by the
            ``[min_multiplier, max_multiplier]`` clamp).
        context:
            Dict with optional keys:
              - ``consecutive_losses`` (int, default 0): current losing
                streak length.
              - ``daily_drawdown_pct`` (float, default 0.0): today's
                drawdown as a positive percentage.
              - ``session_type`` (str, optional): accepted for forward
                compatibility with Phase 2 / Phase 3 session-aware
                scaling; ignored here.

        Returns
        -------
        :class:`BehavioralResult` with ``multiplier`` in
        ``[min_multiplier, max_multiplier]`` and a list of
        human-readable adjustments describing each penalty that fired.
        """
        consecutive_losses = int(context.get("consecutive_losses", 0) or 0)
        daily_drawdown_pct = float(context.get("daily_drawdown_pct", 0.0) or 0.0)
        # session_type intentionally unused in Phase 1b — accepted so
        # callers can forward it without branching. Phase 2 may use it.
        _ = context.get("session_type")  # noqa: F841 — reserved for Phase 2

        adjustments: list[str] = []

        # --- Streak check -------------------------------------------------
        # Apply the lowest multiplier among thresholds whose key is
        # reached by the current streak. >= comparison: hitting the
        # threshold exactly counts as triggering it.
        streak_matching = [
            mult for threshold, mult in self.streak_loss_thresholds.items() if consecutive_losses >= threshold
        ]
        if streak_matching:
            streak_multiplier = min(streak_matching)
            adjustments.append(f"Streak cooldown: {consecutive_losses} consecutive losses → {streak_multiplier}×")
        else:
            streak_multiplier = 1.0

        # --- Drawdown check ----------------------------------------------
        # Strict ``>``: a drawdown sitting exactly on a threshold does
        # not yet trigger the cooldown. This is the council-approved
        # Phase 1b semantics (matching the example in the spec).
        dd_matching = [mult for threshold, mult in self.dd_thresholds.items() if daily_drawdown_pct > threshold]
        if dd_matching:
            dd_multiplier = min(dd_matching)
            adjustments.append(f"DD cooldown: {daily_drawdown_pct:.1f}% daily drawdown → {dd_multiplier}×")
        else:
            dd_multiplier = 1.0

        # --- Combine + clamp ---------------------------------------------
        # Most restrictive (lowest) wins. Then clamp to the configured
        # band so neither penalties nor config typos can drag the
        # multiplier outside the council-approved bounds.
        multiplier = min(streak_multiplier, dd_multiplier)
        multiplier = max(self.min_multiplier, min(self.max_multiplier, multiplier))

        return BehavioralResult(multiplier=multiplier, adjustments=adjustments)
