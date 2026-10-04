"""Bridge tests — wire the existing strategies.registry through the factory.

Acceptance target: ``registry strategies loadable through bridge unchanged``.
These tests do the minimum I/O required to prove it:

* :func:`build_strategy_from_template` rejects non-templates, bad params,
  and non-ISignalStrategy returns.
* :func:`build_strategies_for_registry` walks the canonical 15-strategy
  registry using :class:`RegistryStrategyTemplate`.
* :class:`RegistryStrategyTemplate` honours ``regime_affinity`` per
  archetype (spec §3.1) and returns an ``ISignalStrategy``.

The bridge is the only place in SFA-1 that imports strategy classes —
when ``mtf_filtered_momentum`` / ``session_range_mr_ict_filtered`` /
``usdjpy_d1_trend`` fail to import or have no concrete class, the bridge
raises a clear :class:`BridgeError` instead of fabricating an instance.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

import pytest
from strategies.registry import StrategyConfig, default_registry

from forex_bot.factory.bridge import (
    REGISTRY_STRATEGY_BUILDERS,
    BridgeError,
    RegistryStrategyTemplate,
    build_default_registry_strategy,
    build_strategies_for_registry,
    build_strategy_from_template,
    looks_like_strategy,
)
from forex_bot.factory.template import (
    ARCHETYPE_AFFINITY,
    CHOPPY,
    QUIET,
    TRENDING,
    VOLATILE,
    ParamSpec,
    StrategyTemplate,
)

# ---------------------------------------------------------------------------
# Negative-test fixture: a template that returns a non-ISignalStrategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BadTemplate(StrategyTemplate):
    archetype_id: str = "bad"
    description: str = "returns a non-strategy"
    default_pairs: tuple[str, ...] = ("EURUSD",)
    default_timeframes: tuple[str, ...] = ("H1",)
    regime_affinity: tuple[str, ...] = (TRENDING,)

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return ()

    def default_params(self) -> dict[str, Any]:
        return {}

    def regime_filter(self) -> tuple[str, ...] | None:
        return self.regime_affinity

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        return ("not-a-strategy",)


# ---------------------------------------------------------------------------
# build_strategy_from_template — single-cell bridge
# ---------------------------------------------------------------------------


def test_build_strategy_rejects_non_template() -> None:
    with pytest.raises(BridgeError, match="must be a StrategyTemplate"):
        build_strategy_from_template("not-a-template", {}, "EURUSD")  # type: ignore[arg-type]


def test_build_strategy_rejects_bad_params() -> None:
    template = RegistryStrategyTemplate(default_registry().get("srmr_plus"))
    with pytest.raises(BridgeError, match="rejected params"):
        build_strategy_from_template(template, {"extra": 1}, "EURUSD")


def test_build_strategy_rejects_non_isignal_return() -> None:
    template = _BadTemplate()
    with pytest.raises(BridgeError, match="expected ISignalStrategy"):
        build_strategy_from_template(template, {}, "EURUSD")


# ---------------------------------------------------------------------------
# RegistryStrategyTemplate — every simple-constructor registry entry loads
# ---------------------------------------------------------------------------


_SIMPLE_REGISTRY_IDS = [
    "srmr_plus",
    "bb_rsi_reversion",
    "killzone_momentum",
    "momentum",
    "session_range_mean_reversion",
    "volatility_squeeze",
    "ttc_xauusd",
    "donchian_atr_trend_v2",
    "dual_tf_squeeze_pro",
    "session_breakout_london",
    "session_breakout_ny",
    "session_breakout_asian",
]


@pytest.mark.parametrize("strategy_id", _SIMPLE_REGISTRY_IDS)
def test_registry_strategy_template_loads_simple_constructors(strategy_id: str) -> None:
    """Each ``strategy_id`` resolves to a strategy-like object through the bridge."""
    reg = default_registry()
    config = reg.get(strategy_id)
    assert config is not None, f"strategy_id {strategy_id!r} not in default_registry"

    template = RegistryStrategyTemplate(config)
    pair = config.symbols[0] if config.symbols else "EURUSD"
    strategy = build_strategy_from_template(template, {}, pair)
    assert looks_like_strategy(strategy), (
        f"bridge returned non-strategy object for {strategy_id!r}: {strategy!r}"
    )
    # Round-trip identity: build_default_registry_strategy must agree
    direct = build_default_registry_strategy(strategy_id, pair)
    assert looks_like_strategy(direct)


def test_mtf_filtered_momentum_builder_loads() -> None:
    """Composite strategy with inner strategy wired through bridge."""
    reg = default_registry()
    template = RegistryStrategyTemplate(reg.get("mtf_filtered_momentum"))
    strategy = build_strategy_from_template(template, {}, "EURUSD")
    assert looks_like_strategy(strategy)


def test_session_range_mr_ict_filtered_builder_loads() -> None:
    reg = default_registry()
    template = RegistryStrategyTemplate(reg.get("session_range_mr_ict_filtered"))
    strategy = build_strategy_from_template(template, {}, "EURUSD")
    assert looks_like_strategy(strategy)


def test_usdjpy_d1_trend_placeholder_rejected() -> None:
    """No concrete class for this strategy_id → :class:`BridgeError`."""
    reg = default_registry()
    template = RegistryStrategyTemplate(reg.get("usdjpy_d1_trend"))
    with pytest.raises(BridgeError, match="no concrete strategy class"):
        build_strategy_from_template(template, {}, "USDJPY")


def test_unknown_strategy_id_rejected() -> None:
    with pytest.raises(BridgeError, match="no default builder"):
        build_default_registry_strategy("not-a-real-id", "EURUSD")


# ---------------------------------------------------------------------------
# Regime affinity wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy_type", "expected_affinity"),
    [
        ("momentum", (TRENDING, VOLATILE)),
        ("mean_reversion", (QUIET, CHOPPY)),
        ("breakout", (VOLATILE, TRENDING)),
        ("trend", (TRENDING,)),
    ],
)
def test_registry_strategy_template_affinity(
    strategy_type: str, expected_affinity: tuple[str, ...]
) -> None:
    base = default_registry().get_all_active()[0]
    test_cfg = replace(base, strategy_id=f"test_{strategy_type}", strategy_type=strategy_type)
    template = RegistryStrategyTemplate(test_cfg)
    assert template.regime_affinity == expected_affinity
    assert template.regime_filter() == expected_affinity
    # And the spec §3.1 archetype affinity table is the single source:
    assert ARCHETYPE_AFFINITY[strategy_type] == expected_affinity


def test_unknown_strategy_type_falls_back_to_all_regimes() -> None:
    base = default_registry().get_all_active()[0]
    test_cfg = replace(base, strategy_id="weird_type", strategy_type="nonexistent_archetype")
    template = RegistryStrategyTemplate(test_cfg)
    # Falls back to "no filter" (all four regime labels) for unknown types.
    assert template.regime_affinity == (TRENDING, CHOPPY, VOLATILE, QUIET)


# ---------------------------------------------------------------------------
# build_strategies_for_registry — multi-cell bridge
# ---------------------------------------------------------------------------


def test_build_strategies_for_registry_walks_every_active_strategy() -> None:
    """Every loadable strategy_id in the canonical registry loads through the bridge.

    ``usdjpy_d1_trend`` is a registry-only entry with no concrete strategy
    class — it is expected to raise :class:`BridgeError`.  We exercise the
    walk with the placeholder excluded so the multi-cell bridge runs end
    to end on the 14 implementable strategies.
    """
    from dataclasses import replace

    reg = default_registry()
    expected = {cfg.strategy_id for cfg in reg.get_all_active()}

    # Build a filtered registry that drops the placeholder entry.
    filtered = type(reg)()
    for cfg in reg.get_all_active():
        if cfg.strategy_id == "usdjpy_d1_trend":
            continue
        filtered.register(replace(cfg))

    out = build_strategies_for_registry(filtered, RegistryStrategyTemplate)
    assert all(looks_like_strategy(v) for v in out.values())
    assert set(out.keys()) == {cfg.strategy_id for cfg in filtered.get_all_active()}

    # The registry builders map MUST have an entry for every active strategy.
    missing = expected - set(REGISTRY_STRATEGY_BUILDERS)
    assert not missing, f"missing builders for strategy_ids: {sorted(missing)!r}"


def test_build_strategies_for_registry_uses_first_symbol_by_default() -> None:
    reg = default_registry()
    cfg = reg.get("srmr_plus")
    assert cfg is not None
    small = type(reg)()
    small.register(cfg)

    out = build_strategies_for_registry(small, RegistryStrategyTemplate)
    assert "srmr_plus" in out
    assert looks_like_strategy(out["srmr_plus"])


def test_build_strategies_for_registry_respects_explicit_pair() -> None:
    reg = default_registry()
    cfg = reg.get("srmr_plus")
    assert cfg is not None
    small = type(reg)()
    small.register(cfg)

    out = build_strategies_for_registry(
        small,
        RegistryStrategyTemplate,
        pair="GBPUSD",
    )
    assert "srmr_plus" in out
    assert looks_like_strategy(out["srmr_plus"])


def test_build_strategies_for_registry_rejects_empty_symbols() -> None:
    """StrategyConfig with no symbols + no explicit pair → BridgeError."""
    reg = default_registry()
    cfg = reg.get("srmr_plus")
    assert cfg is not None
    bad_cfg = replace(cfg, symbols=[])  # type: ignore[arg-type]
    small = type(reg)()
    small.register(bad_cfg)

    with pytest.raises(BridgeError, match="no symbols"):
        build_strategies_for_registry(small, RegistryStrategyTemplate)


# ---------------------------------------------------------------------------
# RegistryStrategyTemplate — direct property sanity checks
# ---------------------------------------------------------------------------


def test_registry_template_param_space_empty_in_sfa1() -> None:
    """Identity template in SFA-1 — Optuna knobs land in SFA-2."""
    cfg = default_registry().get("srmr_plus")
    assert cfg is not None
    template = RegistryStrategyTemplate(cfg)
    assert template.param_space == ()
    assert template.default_params() == {}


def test_registry_template_uppercases_pairs() -> None:
    cfg = default_registry().get("srmr_plus")
    assert cfg is not None
    lower_cfg = replace(cfg, symbols=["eurusd", "gbpusd"])  # type: ignore[arg-type]
    template = RegistryStrategyTemplate(lower_cfg)
    assert template.default_pairs == ("EURUSD", "GBPUSD")


def test_registry_template_exposes_strategy_id_and_config() -> None:
    cfg = default_registry().get("srmr_plus")
    assert cfg is not None
    template = RegistryStrategyTemplate(cfg)
    assert template.strategy_id == "srmr_plus"
    assert template.strategy_config is cfg


# ---------------------------------------------------------------------------
# StrategyConfig type import — kept for the replace() helper sanity check
# ---------------------------------------------------------------------------


def test_strategy_config_is_dataclass() -> None:
    """Sanity: replace() above requires :class:`StrategyConfig` to be a dataclass."""
    assert hasattr(StrategyConfig, "__dataclass_fields__")
