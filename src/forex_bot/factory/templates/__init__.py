"""Strategy Factory templates (SFA-3 — registry-backed pass-through).

SFA-1 shipped the :class:`forex_bot.factory.template.StrategyTemplate`
contract; SFA-2 populated the deferred ``usdjpy_d1_trend`` strategy
implementation.  SFA-3 adds the *registry-backed pass-through* template
that the tournament front door uses to wire existing
``tournament.STRATEGY_CLASS_MAP`` strategies into factory validation
without inventing new archetypes (the per-archetype templates land in
later sprints per spec §7 Phase 2).

Public surface
--------------
* :class:`RegistryBackedTemplate` — a :class:`StrategyTemplate` whose
  ``build_strategy`` delegates to a caller-supplied factory.  Empty
  parameter space (``param_space == ()``) so Optuna can sweep on
  ``default_params == {}`` without growing the cell matrix.
"""
from __future__ import annotations

from forex_bot.factory.templates.registry_template import RegistryBackedTemplate

__all__ = ["RegistryBackedTemplate"]
