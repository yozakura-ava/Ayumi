"""Tests for the deferred SFA-1 ``usdjpy_d1_trend`` strategy class (SFA-2).

Verifies:

* :class:`USDJPYD1TrendStrategy` exposes ``.name`` and ``.evaluate``
  (bridge duck-type check).
* :func:`build_default_registry_strategy` now resolves the SFA-1
  placeholder (no more :class:`BridgeError`).
* ``RegistryStrategyTemplate`` builds a working instance for
  ``strategy_id == "usdjpy_d1_trend"``.
* The strategy's :attr:`regime_affinity` is ``(TRENDING,)`` (spec §3.1
  trend archetype).
* Lifecycle hooks (``initialize``, ``on_bar``, ``generate_signal``)
  don't crash on small histories and produce ``None`` until enough
  data has accumulated.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from backtest.engine import Bar
from strategies.registry import default_registry

from forex_bot.factory.bridge import (
    REGISTRY_STRATEGY_BUILDERS,
    RegistryStrategyTemplate,
    build_default_registry_strategy,
    build_strategy_from_template,
    looks_like_strategy,
)
from forex_bot.factory.strategies import (
    USDJPYD1TrendConfig,
    USDJPYD1TrendStrategy,
)
from forex_bot.factory.template import TRENDING

# ---------------------------------------------------------------------------
# Concrete strategy class — direct unit tests
# ---------------------------------------------------------------------------


def test_strategy_exposes_name_attribute() -> None:
    assert isinstance(USDJPYD1TrendStrategy.name, str)
    assert USDJPYD1TrendStrategy.name == "usdjpy_d1_trend"


def test_strategy_default_construction_uses_defaults() -> None:
    """Zero-arg construction matches what the bridge's default builder calls."""
    s = USDJPYD1TrendStrategy()
    assert isinstance(s.config, USDJPYD1TrendConfig)
    assert s.config.sma_period == 50
    assert s.config.donchian_period == 20


def test_strategy_with_custom_config() -> None:
    cfg = USDJPYD1TrendConfig(sma_period=30, rsi_min=55.0, rsi_max=65.0)
    s = USDJPYD1TrendStrategy(config=cfg)
    assert s.config.sma_period == 30
    assert s.config.rsi_min == 55.0


def test_strategy_is_duck_type_strategy() -> None:
    """Bridge accepts ``.name`` + ``.evaluate()`` surface."""
    s = USDJPYD1TrendStrategy()
    assert looks_like_strategy(s)


def test_strategy_initialize_resets_state() -> None:
    s = USDJPYD1TrendStrategy()
    s._closes.append(1.0)
    s.initialize()
    assert s._closes == []


def test_strategy_on_bar_appends() -> None:
    s = USDJPYD1TrendStrategy()
    bar = Bar(
        time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        open=1.0,
        high=1.01,
        low=0.99,
        close=1.005,
        volume=100.0,
    )
    s.on_bar(bar)
    assert s._closes == [1.005]
    assert s._highs == [1.01]
    assert s._lows == [0.99]


def test_strategy_generate_signal_none_until_enough_data() -> None:
    """Returns ``None`` until ``sma_period`` bars have accumulated."""
    s = USDJPYD1TrendStrategy()  # default sma_period=50
    for i in range(40):
        s.on_bar(
            Bar(
                time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                open=1.0,
                high=1.01,
                low=0.99,
                close=1.0 + i * 0.0001,
                volume=100.0,
            )
        )
    assert s.generate_signal() is None


def test_strategy_shutdown_is_noop() -> None:
    """``shutdown()`` is a noop — must not raise on a fresh instance.

    The base class signature is ``-> None``; mypy's ``func-returns-value``
    rule forbids accessing the return value, so we verify the noop
    behaviour by simply invoking the method (no exception is success).
    """
    s = USDJPYD1TrendStrategy()
    s.shutdown()


def test_strategy_evaluate_accepts_state_like_object() -> None:
    """``evaluate(state)`` is the backtest-engine entry point."""
    s = USDJPYD1TrendStrategy()

    class _State:
        def __init__(self, bar: Bar) -> None:
            self.bar = bar

    state = _State(
        Bar(
            time=datetime(2025, 1, 1, tzinfo=timezone.utc),
            open=1.0,
            high=1.01,
            low=0.99,
            close=1.005,
            volume=100.0,
        )
    )
    # Returns None because not enough history yet (50 bars needed).
    assert s.evaluate(state) is None
    assert s._closes == [1.005]


# ---------------------------------------------------------------------------
# Bridge integration — deferred placeholder now resolves
# ---------------------------------------------------------------------------


def test_usdjpy_d1_trend_placeholder_now_resolves() -> None:
    """SFA-2: ``build_default_registry_strategy`` succeeds (no BridgeError)."""
    s = build_default_registry_strategy("usdjpy_d1_trend", "USDJPY")
    assert looks_like_strategy(s)
    assert s.name == "usdjpy_d1_trend"


def test_usdjpy_d1_trend_via_registry_template() -> None:
    """RegistryStrategyTemplate.build_strategy returns the concrete class."""
    reg = default_registry()
    template = RegistryStrategyTemplate(reg.get("usdjpy_d1_trend"))
    s = build_strategy_from_template(template, {}, "USDJPY")
    assert looks_like_strategy(s)
    assert s.name == "usdjpy_d1_trend"


def test_usdjpy_d1_trend_has_trending_affinity() -> None:
    """Spec §3.1 trend_following archetype → ``(TRENDING,)``."""
    cfg = default_registry().get("usdjpy_d1_trend")
    assert cfg is not None
    template = RegistryStrategyTemplate(cfg)
    assert template.regime_affinity == (TRENDING,)
    assert template.regime_filter() == (TRENDING,)


def test_usdjpy_d1_trend_builder_registered() -> None:
    """``REGISTRY_STRATEGY_BUILDERS`` includes the deferred id."""
    assert "usdjpy_d1_trend" in REGISTRY_STRATEGY_BUILDERS


def test_walk_strategies_for_registry_includes_usdjpy_d1_trend() -> None:
    """The deferred entry now participates in ``build_strategies_for_registry``."""
    from forex_bot.factory.bridge import build_strategies_for_registry

    reg = default_registry()
    out = build_strategies_for_registry(reg, RegistryStrategyTemplate)
    assert "usdjpy_d1_trend" in out
    assert looks_like_strategy(out["usdjpy_d1_trend"])


# ---------------------------------------------------------------------------
# Per-archetype affinity mapping (sanity)
# ---------------------------------------------------------------------------


def test_strategy_type_trend_maps_to_trending_affinity() -> None:
    """``strategy_type='trend'`` alias maps to ``trend_following`` affinity."""
    base = default_registry().get_all_active()[0]
    test_cfg = replace(base, strategy_id="trend_test", strategy_type="trend")
    template = RegistryStrategyTemplate(test_cfg)
    assert template.regime_affinity == (TRENDING,)
