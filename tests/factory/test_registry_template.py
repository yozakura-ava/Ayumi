"""Registry-backed pass-through template tests (SFA-3).

The :class:`RegistryBackedTemplate` is the thin bridge between
``tournament.STRATEGY_CLASS_MAP`` and the factory validation runner.
These tests verify:

* Construction sets the five frozen fields correctly.
* :attr:`param_space` is empty (identity-only, no Optuna sampling).
* :meth:`build_strategy` delegates to the caller-supplied builder.
* :meth:`default_params` and :meth:`regime_filter` return the documented
  shapes.
* The :class:`StrategyTemplate` invariants hold (no unknown regimes,
  non-empty pairs / timeframes).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
for _p in (
    str(_HERE.parents[2] / "src"),
    str(_HERE.parents[2] / "src" / "forex_bot"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from forex_bot.factory.template import REGIME_LABELS, StrategyTemplate  # noqa: E402
from forex_bot.factory.templates import RegistryBackedTemplate  # noqa: E402


def _factory_builder():
    """Return a closure that builds a sentinel strategy for ``strategy_id``."""
    def _build(params, pair):
        class _S:
            name = "sentinel"

            def evaluate(self, state):  # noqa: ARG002
                return None

        return _S()

    return _build


def test_construction_sets_archetype_id() -> None:
    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="alpha pass-through",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_factory_builder(),
    )
    assert t.archetype_id == "registry_alpha"
    assert t.description == "alpha pass-through"
    assert t.default_pairs == ("EURUSD",)
    assert t.default_timeframes == ("H1",)
    assert t.regime_affinity == REGIME_LABELS


def test_param_space_is_empty() -> None:
    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_factory_builder(),
    )
    assert t.param_space == ()


def test_default_params_is_empty_dict() -> None:
    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_factory_builder(),
    )
    assert t.default_params() == {}


def test_regime_filter_is_none() -> None:
    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_factory_builder(),
    )
    assert t.regime_filter() is None


def test_build_strategy_delegates_to_builder() -> None:
    captured: dict[str, list[tuple[dict[str, object], str]]] = {"calls": []}

    def _builder(params, pair):
        captured["calls"].append((dict(params), pair))
        return "sentinel-strategy"

    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_builder,
    )
    out = t.build_strategy({}, "EURUSD")
    assert out == "sentinel-strategy"
    assert captured["calls"] == [({}, "EURUSD")]


def test_build_strategy_does_not_alias_params() -> None:
    """Caller mutating params must not bleed back into the builder."""
    captured = {}

    def _builder(params, pair):
        captured["received"] = dict(params)
        return None

    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_builder,
    )
    caller_params: dict[str, object] = {"k": 1}
    t.build_strategy(caller_params, "EURUSD")
    caller_params["k"] = 999  # mutate after the call
    assert captured["received"] == {"k": 1}


def test_construction_rejects_unknown_regimes() -> None:
    with pytest.raises(ValueError, match="unknown regimes"):
        RegistryBackedTemplate(
            archetype_id="registry_alpha",
            description="",
            default_pairs=("EURUSD",),
            default_timeframes=("H1",),
            regime_affinity=("TRENDING", "BOGUS"),
            builder=_factory_builder(),
        )


def test_construction_rejects_empty_pairs() -> None:
    with pytest.raises(ValueError, match="default_pairs must be non-empty"):
        RegistryBackedTemplate(
            archetype_id="registry_alpha",
            description="",
            default_pairs=(),
            default_timeframes=("H1",),
            regime_affinity=REGIME_LABELS,
            builder=_factory_builder(),
        )


def test_construction_rejects_empty_timeframes() -> None:
    with pytest.raises(ValueError, match="default_timeframes must be non-empty"):
        RegistryBackedTemplate(
            archetype_id="registry_alpha",
            description="",
            default_pairs=("EURUSD",),
            default_timeframes=(),
            regime_affinity=REGIME_LABELS,
            builder=_factory_builder(),
        )


def test_construction_rejects_empty_archetype_id() -> None:
    with pytest.raises(ValueError, match="archetype_id must be a non-empty string"):
        RegistryBackedTemplate(
            archetype_id="",
            description="",
            default_pairs=("EURUSD",),
            default_timeframes=("H1",),
            regime_affinity=REGIME_LABELS,
            builder=_factory_builder(),
        )


def test_is_strategy_template_subclass() -> None:
    t = RegistryBackedTemplate(
        archetype_id="registry_alpha",
        description="",
        default_pairs=("EURUSD",),
        default_timeframes=("H1",),
        regime_affinity=REGIME_LABELS,
        builder=_factory_builder(),
    )
    assert isinstance(t, StrategyTemplate)
