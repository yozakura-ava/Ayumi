"""Integration tests for FilterChain with ORB, Trend, ATR, and FVG filters.

Tests the full chain lifecycle: config-driven construction, short-circuit
evaluation, ORB IFilter compatibility, and strategies.yaml parsing.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from signal_engine.filters.atr_filter import ATRFilter
from signal_engine.filters.filter_chain import FilterChain, build_chain_from_config
from signal_engine.filters.fvg_filter import FVGFilter
from signal_engine.filters.trend_filter import TrendFilter
from signal_engine.orb_filter import OpeningRange, ORBFilter

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_opening_range():
    """A valid London opening range for testing."""
    return OpeningRange(
        session="LONDON",
        high=1.2650,
        low=1.2600,
        open_time=datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        avg_volume=1500.0,
    )


@pytest.fixture
def strategies_yaml_path():
    """Path to the live strategies.yaml."""
    return Path(__file__).parent.parent / "src" / "forex_bot" / "config" / "strategies.yaml"


# ── Test 1: Default chain (trend → atr → fvg) ─────────────────────────────


def test_default_chain_constructs_with_three_filters():
    """FilterChain with no args creates trend, atr, fvg in priority order."""
    chain = FilterChain()
    names = [getattr(f, "name", type(f).__name__) for f in chain.filters]
    assert names == ["trend", "atr", "fvg"], f"Expected [trend, atr, fvg], got {names}"


# ── Test 2: ORB filter as IFilter in chain ─────────────────────────────────


def test_orb_filter_chain_integration_pass(sample_opening_range):
    """ORB filter added to chain passes a strong breakout signal."""
    chain = FilterChain(filters=[])  # empty chain
    orb = ORBFilter(min_score_threshold=0.3)
    chain.add(orb)

    # Entry well above range high → strong breakout
    result = chain.evaluate(
        signal_direction="LONG",
        entry_price=1.2700,
        opening_range=sample_opening_range,
        current_volume=2000.0,
    )
    assert result is True, f"Expected strong breakout to pass, last_result={chain.last_result}"


def test_orb_filter_chain_integration_reject(sample_opening_range):
    """ORB filter rejects an inside-range signal."""
    chain = FilterChain(filters=[])
    orb = ORBFilter(min_score_threshold=0.3)
    chain.add(orb)

    # Entry inside the range → not a breakout, score should be low
    result = chain.evaluate(
        signal_direction="LONG",
        entry_price=1.2625,  # mid-range
        opening_range=sample_opening_range,
        current_volume=1000.0,
    )
    assert result is False, "Expected inside-range signal to be rejected"
    assert chain.last_result.filter_name == "orb"


# ── Test 3: ORB filter fails open without range ────────────────────────────


def test_orb_filter_fails_open_without_range():
    """ORB filter passes when no opening range is provided."""
    chain = FilterChain(filters=[])
    chain.add(ORBFilter(min_score_threshold=0.3))

    result = chain.evaluate(
        signal_direction="LONG",
        entry_price=1.2500,
        opening_range=None,
        current_volume=0.0,
    )
    assert result is True, "ORB filter should fail open when no range is available"


# ── Test 4: build_chain_from_config with ORB enabled ───────────────────────


def test_build_chain_from_config_all_enabled():
    """build_chain_from_config creates a chain with all 4 filters when enabled."""
    config = {
        "trend": {"enabled": True, "ema_fast_period": 9, "ema_slow_period": 21},
        "atr": {"enabled": True},
        "fvg": {"enabled": True},
        "orb": {"enabled": True, "min_score_threshold": 0.25},
    }
    chain = build_chain_from_config(config)
    names = [getattr(f, "name", type(f).__name__) for f in chain.filters]
    assert "trend" in names
    assert "atr" in names
    assert "fvg" in names
    assert "orb" in names
    assert len(chain.filters) == 4


def test_build_chain_from_config_orb_disabled():
    """build_chain_from_config skips ORB when disabled."""
    config = {
        "trend": {"enabled": True},
        "atr": {"enabled": True},
        "fvg": {"enabled": True},
        "orb": {"enabled": False},
    }
    chain = build_chain_from_config(config)
    names = [getattr(f, "name", type(f).__name__) for f in chain.filters]
    assert "orb" not in names
    assert len(chain.filters) == 3


# ── Test 5: strategies.yaml filters section parses correctly ───────────────


def test_strategies_yaml_has_filters_section(strategies_yaml_path):
    """The live strategies.yaml contains a well-formed filters section."""
    with open(strategies_yaml_path) as f:
        config = yaml.safe_load(f)

    assert "filters" in config, "strategies.yaml missing 'filters' section"
    filters = config["filters"]

    assert "trend" in filters
    assert filters["trend"]["enabled"] is True

    assert "orb" in filters
    assert filters["orb"]["enabled"] is True
    assert "min_score_threshold" in filters["orb"]


def test_strategies_yaml_builds_working_chain(strategies_yaml_path):
    """Building a chain from the live strategies.yaml produces a functional chain."""
    with open(strategies_yaml_path) as f:
        config = yaml.safe_load(f)

    chain = build_chain_from_config(config["filters"])
    assert len(chain.filters) >= 3, f"Expected at least 3 filters, got {len(chain.filters)}"


# ── Test 6: Priority ordering in chain ─────────────────────────────────────


def test_orb_filter_priority_is_after_fvg():
    """ORB filter (priority 40) runs after FVG (30), ATR (20), Trend (10)."""
    chain = FilterChain(filters=[])
    chain.add(TrendFilter())
    chain.add(ATRFilter())
    chain.add(FVGFilter())
    chain.add(ORBFilter())

    priorities = [getattr(f, "priority", 50) for f in chain.filters]
    assert priorities == sorted(priorities), "Filters not in ascending priority order"
    assert priorities[-1] == 40, f"ORB should be last (40), got {priorities[-1]}"


# ── Test 7: Short-circuit behavior with ORB ────────────────────────────────


def test_short_circuit_does_not_reach_orb_when_trend_rejects():
    """If TrendFilter rejects, ORB never runs (short-circuit)."""
    chain = FilterChain(filters=[])
    chain.add(TrendFilter())
    chain.add(ORBFilter(min_score_threshold=0.3))

    # Trend filter: LONG but EMA bearish → trend rejects
    result = chain.evaluate(
        signal_direction="LONG",
        ema_fast=1.2400,
        ema_slow=1.2500,  # bearish: fast below slow
        entry_price=1.2700,
        opening_range=None,
    )

    assert result is False
    assert chain.last_result.filter_name == "trend"
