"""Registry-backed pass-through template (SFA-3 — tournament front door).

A thin :class:`StrategyTemplate` whose ``build_strategy`` delegates to a
caller-supplied factory.  This is the bridge between tournament
survivors (which already have a working strategy instance via
:func:`tournament.harness._build_strategy_instance`) and the factory
validation pipeline (``ValidationRunner.run_batch``).

Why a pass-through?
--------------------
The factory spec (§7 Phase 1) lands the *wiring* before the per-archetype
templates.  Tournament survivors ARE concrete strategies — adding a
template per archetype before this card would inflate scope without
unblocking the front-door contract.  This template lets the front door
emit ``CandidateSpec`` rows that the runner can consume as-is, then cells
loop back to real per-archetype templates once those land.

Contract
--------
* ``param_space == ()`` — no Optuna sampling (the bridge is identity-only).
* ``default_params == {}`` — single deterministic candidate per survivor.
* ``build_strategy(params, pair)`` — delegates to ``self._builder(params, pair)``.
* ``regime_filter()`` returns ``None`` (no gating — the validator's own
  per-regime logic is the authoritative gate per spec §4.3).
* The template is **frozen-equivalent** via ``object.__setattr__`` so it
  round-trips through ``pickle``/``copy`` like every other template.

Validation runner hook
----------------------
``ValidationRunner.run_one`` calls ``build_strategy_from_template`` which
calls :meth:`RegistryBackedTemplate.build_strategy`.  The runner never
inspects :attr:`param_space` so an empty tuple is sufficient (Optuna is
not invoked).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from forex_bot.factory.template import ParamSpec, StrategyTemplate


class RegistryBackedTemplate(StrategyTemplate):
    """Pass-through template backed by a caller-supplied strategy factory.

    Parameters
    ----------
    archetype_id
        Stable identifier (typically ``"registry_<strategy_id>"``).
    description
        Free-text description for logs / reports.
    default_pairs, default_timeframes
        Pairs / timeframes the front door plans to evaluate against
        (informational — the runner does not enforce).
    regime_affinity
        Subset of ``REGIME_LABELS``.  ``REGIME_LABELS`` (all four) is the
        safe default; tighten once per-archetype scores are known.
    builder
        ``Callable[[Mapping[str, Any], str], Any]`` that takes
        ``(params, pair)`` and returns a strategy-like object (``.name`` +
        ``.evaluate()``).  Typically a closure around
        :func:`tournament.harness._build_strategy_instance`.
    """

    def __init__(
        self,
        *,
        archetype_id: str,
        description: str,
        default_pairs: tuple[str, ...],
        default_timeframes: tuple[str, ...],
        regime_affinity: tuple[str, ...],
        builder: Callable[[Mapping[str, Any], str], Any],
    ) -> None:
        # StrategyTemplate is a frozen dataclass + ABC.  We bypass the
        # frozen guard exactly once during construction (the dataclass
        # invariants are honoured — see __post_init__ in the base).
        object.__setattr__(self, "archetype_id", archetype_id)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "default_pairs", default_pairs)
        object.__setattr__(self, "default_timeframes", default_timeframes)
        object.__setattr__(self, "regime_affinity", regime_affinity)
        # ``__post_init__`` on StrategyTemplate validates the five
        # declared fields — re-run it manually (frozen ABC doesn't
        # call it from __init__).
        StrategyTemplate.__post_init__(self)
        self._builder = builder

    # ── template primitives ──────────────────────────────────────────────

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        """Empty parameter space — identity-only, no Optuna sampling."""
        return ()

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        """Delegate to the caller-supplied ``builder(params, pair)``.

        Validation runner contract: returns a duck-typed strategy with
        ``.name`` and ``.evaluate()``.  The builder closure must honour
        that contract — the bridge's :func:`_looks_like_strategy` check
        catches non-conforming builds and surfaces them as
        :class:`BridgeError`.
        """
        # Defensive copy so callers can mutate ``params`` downstream
        # without aliasing into our state.
        return self._builder(dict(params), pair)

    def default_params(self) -> dict[str, Any]:
        """Identity baseline — single deterministic candidate per survivor."""
        return {}

    def regime_filter(self) -> tuple[str, ...] | None:
        """No filter — the validator's regime logic is authoritative (§4.3)."""
        return None


__all__ = ["RegistryBackedTemplate"]
