"""Template contract tests (§3.2 — plug-in interface).

Each test exercises one aspect of the :class:`StrategyTemplate` ABC:
the frozen dataclass invariants, the :class:`ParamSpec` validation, the
abstract method enforcement, and the helper accessors (validate_params /
to_optuna_spec).

These tests are intentionally pure — no market data, no Optuna, no
strategy classes.  Anything heavy belongs in :mod:`tests.factory.test_bridge`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from forex_bot.factory.template import (
    ARCHETYPE_AFFINITY,
    CHOPPY,
    QUIET,
    REGIME_LABELS,
    TRENDING,
    VOLATILE,
    ParamKind,
    ParamSpec,
    StrategyTemplate,
)


# ---------------------------------------------------------------------------
# Test fixtures — minimal concrete subclasses of StrategyTemplate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubTemplate(StrategyTemplate):
    archetype_id: str = "stub"
    description: str = "stub template"
    default_pairs: tuple[str, ...] = ("EURUSD",)
    default_timeframes: tuple[str, ...] = ("H1",)
    regime_affinity: tuple[str, ...] = (TRENDING,)

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return (
            ParamSpec("roc_period", ParamKind.INT, low=5, high=30, step=1),
            ParamSpec("adx_min", ParamKind.FLOAT, low=15.0, high=30.0),
            ParamSpec("session", ParamKind.CATEGORICAL, choices=("asia", "london", "ny")),
        )

    def default_params(self) -> dict[str, Any]:
        return {"roc_period": 14, "adx_min": 20.0, "session": "london"}

    def regime_filter(self) -> tuple[str, ...] | None:
        return self.regime_affinity

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        # No ISignalStrategy in this test — return a sentinel to prove the
        # contract is wired.  Real builds live in test_bridge.py.
        return ("strategy", self.archetype_id, dict(params), pair)


@dataclass(frozen=True)
class _NoFilterTemplate(_StubTemplate):
    """Same as :class:`_StubTemplate` but with ``regime_filter = None``."""

    def regime_filter(self) -> tuple[str, ...] | None:
        return None


# ---------------------------------------------------------------------------
# StrategyTemplate ABC enforcement
# ---------------------------------------------------------------------------


def test_strategy_template_is_abstract() -> None:
    """The base ABC cannot be instantiated directly."""
    with pytest.raises(TypeError):
        StrategyTemplate(  # type: ignore[abstract]
            archetype_id="x",
            description="",
            default_pairs=("EURUSD",),
            default_timeframes=("H1",),
            regime_affinity=(TRENDING,),
        )


def test_archetype_id_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="archetype_id"):
        _StubTemplate(archetype_id="")


def test_unknown_regime_rejected() -> None:
    with pytest.raises(ValueError, match="unknown regimes"):
        _StubTemplate(regime_affinity=("BOGUS",))


def test_default_pairs_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="default_pairs"):
        _StubTemplate(default_pairs=())


def test_default_timeframes_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="default_timeframes"):
        _StubTemplate(default_timeframes=())


def test_template_is_hashable() -> None:
    """Frozen dataclass → usable as a registry key."""
    tpl = _StubTemplate()
    assert hash(tpl) == hash(tpl)
    assert tpl in {tpl}


# ---------------------------------------------------------------------------
# ParamSpec validation
# ---------------------------------------------------------------------------


def test_paramspec_int_requires_bounds() -> None:
    with pytest.raises(ValueError, match="requires low and high"):
        ParamSpec("x", ParamKind.INT)


def test_paramspec_int_low_must_be_le_high() -> None:
    with pytest.raises(ValueError, match="low="):
        ParamSpec("x", ParamKind.INT, low=30, high=5)


def test_paramspec_int_step_must_be_positive() -> None:
    with pytest.raises(ValueError, match="step must be positive"):
        ParamSpec("x", ParamKind.INT, low=5, high=30, step=0)


def test_paramspec_categorical_requires_choices() -> None:
    with pytest.raises(ValueError, match="categorical requires non-empty"):
        ParamSpec("x", ParamKind.CATEGORICAL)


def test_paramspec_name_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="name"):
        ParamSpec("", ParamKind.INT, low=5, high=30)


def test_paramspec_int_ignores_choices() -> None:
    """choices is silently ignored for INT/FLOAT kinds (validated by kind)."""
    spec = ParamSpec("x", ParamKind.INT, low=5, high=30, choices=("a", "b"))
    assert spec.choices == ("a", "b")  # preserved on the dataclass


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def test_param_names_property() -> None:
    assert _StubTemplate().param_names == ("roc_period", "adx_min", "session")


def test_validate_params_returns_coerced_dict() -> None:
    tpl = _StubTemplate()
    coerced = tpl.validate_params(
        {"roc_period": 14.0, "adx_min": 20, "session": "london"}
    )
    assert coerced == {"roc_period": 14, "adx_min": 20.0, "session": "london"}
    # Coerced types:
    assert isinstance(coerced["roc_period"], int)
    assert isinstance(coerced["adx_min"], float)


def test_validate_params_rejects_missing() -> None:
    tpl = _StubTemplate()
    with pytest.raises(ValueError, match="missing"):
        tpl.validate_params({"roc_period": 14, "adx_min": 20.0})


def test_validate_params_rejects_extra() -> None:
    tpl = _StubTemplate()
    with pytest.raises(ValueError, match="extra"):
        tpl.validate_params(
            {
                "roc_period": 14,
                "adx_min": 20.0,
                "session": "london",
                "nonsense": True,
            }
        )


def test_validate_params_rejects_invalid_categorical() -> None:
    tpl = _StubTemplate()
    with pytest.raises(ValueError, match="not in choices"):
        tpl.validate_params(
            {"roc_period": 14, "adx_min": 20.0, "session": "tokyo"}
        )


def test_to_optuna_spec_matches_kind() -> None:
    spec = _StubTemplate().to_optuna_spec()
    assert spec["roc_period"]["kind"] == "int"
    assert spec["roc_period"]["low"] == 5
    assert spec["roc_period"]["high"] == 30
    assert spec["roc_period"]["step"] == 1

    assert spec["adx_min"]["kind"] == "float"
    assert spec["adx_min"]["low"] == 15.0
    assert spec["adx_min"]["high"] == 30.0

    assert spec["session"]["kind"] == "categorical"
    assert spec["session"]["choices"] == ("asia", "london", "ny")


# ---------------------------------------------------------------------------
# Regime affinity map (spec §3.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("archetype", "expected"),
    [
        ("momentum", (TRENDING, VOLATILE)),
        ("mean_reversion", (QUIET, CHOPPY)),
        ("breakout", (VOLATILE, TRENDING)),
        ("trend_following", (TRENDING,)),
        ("session_based", REGIME_LABELS),
    ],
)
def test_archetype_affinity_matches_spec(archetype: str, expected: tuple[str, ...]) -> None:
    assert ARCHETYPE_AFFINITY[archetype] == expected


def test_regime_labels_canonical() -> None:
    """All four labels present and frozen-order stable."""
    assert REGIME_LABELS == (TRENDING, CHOPPY, VOLATILE, QUIET)


# ---------------------------------------------------------------------------
# build_strategy + regime_filter dispatch
# ---------------------------------------------------------------------------


def test_build_strategy_returns_contract_value() -> None:
    tpl = _StubTemplate()
    result = tpl.build_strategy({"roc_period": 10, "adx_min": 25.0, "session": "ny"}, "EURUSD")
    assert result[0] == "strategy"
    assert result[1] == "stub"
    assert result[3] == "EURUSD"


def test_regime_filter_returns_affinity() -> None:
    assert _StubTemplate().regime_filter() == (TRENDING,)


def test_regime_filter_none_means_no_filter() -> None:
    assert _NoFilterTemplate().regime_filter() is None