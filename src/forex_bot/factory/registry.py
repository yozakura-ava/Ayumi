"""Template registration for the Strategy Factory.

This module hosts the *template-side* registry — it tracks which
:class:`StrategyTemplate` subclasses the orchestrator can dispatch on.  It
is deliberately distinct from :mod:`forex_bot.strategies.registry` which
tracks concrete ``StrategyConfig`` entries: a single template may consume
many strategy configs (or none — abstract templates are fine).

In SFA-1 the default factory registry is **empty** — concrete templates
land in SFA-2.  The registry exists now so:

* SFA-1 tests can prove the registration contract works in isolation
* SFA-2 code can drop templates into ``default_factory_registry()``
  without touching the rest of the package
* the orchestrator (Phase 1 / 3 in the spec) has a stable API to call
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from forex_bot.factory.template import StrategyTemplate


class TemplateRegistryError(RuntimeError):
    """Raised on registration / lookup conflicts."""


@dataclass
class FactoryRegistry:
    """In-memory map of ``archetype_id`` → :class:`StrategyTemplate`."""

    _templates: dict[str, StrategyTemplate] = field(default_factory=dict)

    def register(self, template: StrategyTemplate, *, overwrite: bool = False) -> None:
        """Register a template under its :attr:`StrategyTemplate.archetype_id`.

        Raises
        ------
        TemplateRegistryError
            If the ``archetype_id`` is already registered and ``overwrite``
            is ``False``.
        """
        if not isinstance(template, StrategyTemplate):
            raise TemplateRegistryError(
                f"register() expects StrategyTemplate, got {type(template).__name__}"
            )
        if template.archetype_id in self._templates and not overwrite:
            raise TemplateRegistryError(
                f"archetype_id {template.archetype_id!r} already registered; "
                "pass overwrite=True to replace"
            )
        self._templates[template.archetype_id] = template

    def unregister(self, archetype_id: str) -> None:
        """Remove the template under ``archetype_id`` (no-op if missing)."""
        self._templates.pop(archetype_id, None)

    def get(self, archetype_id: str) -> StrategyTemplate:
        """Look up by archetype_id; raise :class:`TemplateRegistryError` if missing."""
        try:
            return self._templates[archetype_id]
        except KeyError as exc:
            raise TemplateRegistryError(
                f"no template registered for archetype_id {archetype_id!r} "
                f"(registered: {sorted(self._templates)!r})"
            ) from exc

    def try_get(self, archetype_id: str) -> StrategyTemplate | None:
        """Look up by archetype_id; return ``None`` if missing."""
        return self._templates.get(archetype_id)

    def has(self, archetype_id: str) -> bool:
        return archetype_id in self._templates

    def archetype_ids(self) -> tuple[str, ...]:
        """Tuple of registered archetype_ids (sorted for determinism)."""
        return tuple(sorted(self._templates))

    def all_templates(self) -> tuple[StrategyTemplate, ...]:
        """Tuple of registered templates (sorted by archetype_id)."""
        return tuple(self._templates[aid] for aid in self.archetype_ids())

    def extend(self, templates: Iterable[StrategyTemplate], *, overwrite: bool = False) -> None:
        """Bulk-register; first conflict short-circuits."""
        for template in templates:
            self.register(template, overwrite=overwrite)

    def clear(self) -> None:
        """Drop all registered templates (used by tests)."""
        self._templates.clear()


def default_factory_registry() -> FactoryRegistry:
    """Return the canonical factory template registry (empty in SFA-1).

    SFA-2 will populate this with the 5 archetype templates (momentum /
    mean_reversion / breakout / trend_following / session_based).  Keeping
    it empty here lets SFA-1 land without a half-baked template set.
    """
    return FactoryRegistry()


__all__ = [
    "FactoryRegistry",
    "TemplateRegistryError",
    "default_factory_registry",
]