"""Spread cost defaults tests (Liora ground rule / spec §1 cross-cutting).

Pin-tests for the canonical spread / commission / slippage table so a future
edit cannot silently regress the Liora ground rule numbers.
"""

from __future__ import annotations

import dataclasses

import pytest

from forex_bot.factory.spread_costs import (
    COMMISSION_PER_LOT_USD,
    PIP_SLIPPAGE,
    SpreadCosts,
    SpreadCostTable,
    default_spread_costs,
)

# ---------------------------------------------------------------------------
# Module-level constants (Liora ground rule — never mutate)
# ---------------------------------------------------------------------------


def test_ground_rule_constants() -> None:
    assert COMMISSION_PER_LOT_USD == 3.5
    assert PIP_SLIPPAGE == 0.2


# ---------------------------------------------------------------------------
# SpreadCostTable — defaults + lookups
# ---------------------------------------------------------------------------


def test_default_table_has_canonical_pairs() -> None:
    table = default_spread_costs()
    assert set(table.symbols) == {"XAUUSD", "EURUSD", "GBPUSD", "USDJPY"}


def test_default_table_xauusd_spread() -> None:
    # Spec §1: XAUUSD 3.0 pips
    assert default_spread_costs().get("XAUUSD").spread_pips == 3.0


def test_default_table_eurusd_spread() -> None:
    # Spec §1: EURUSD 1.5 pips
    assert default_spread_costs().get("EURUSD").spread_pips == 1.5


def test_default_table_gbpusd_spread() -> None:
    # Spec §1: GBPUSD 1.5 pips
    assert default_spread_costs().get("GBPUSD").spread_pips == 1.5


def test_default_table_usdjpy_spread() -> None:
    # Spec §1: USDJPY 1.2 pips
    assert default_spread_costs().get("USDJPY").spread_pips == 1.2


def test_default_table_commission_default() -> None:
    # Commission + slippage propagate from the ground-rule module constants.
    for symbol in ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY"):
        entry = default_spread_costs().get(symbol)
        assert entry.commission_per_lot_usd == 3.5
        assert entry.slippage_pips == 0.2


def test_lookup_is_case_insensitive() -> None:
    table = default_spread_costs()
    assert table.get("xauusd").symbol.upper() == "XAUUSD"
    assert table.get("EurUsd").spread_pips == 1.5


def test_lookup_unknown_raises_keyerror() -> None:
    table = default_spread_costs()
    with pytest.raises(KeyError, match="AUDUSD"):
        table.get("AUDUSD")


def test_for_symbol_alias() -> None:
    table = default_spread_costs()
    assert table.for_symbol("XAUUSD") is table.get("XAUUSD")


# ---------------------------------------------------------------------------
# SpreadCosts dataclass validation
# ---------------------------------------------------------------------------


def test_spread_costs_rejects_negative_spread() -> None:
    with pytest.raises(ValueError, match="spread_pips"):
        SpreadCosts(symbol="X", spread_pips=-0.1)


def test_spread_costs_rejects_negative_slippage() -> None:
    with pytest.raises(ValueError, match="slippage_pips"):
        SpreadCosts(symbol="X", spread_pips=1.0, slippage_pips=-0.1)


def test_spread_costs_rejects_negative_commission() -> None:
    with pytest.raises(ValueError, match="commission_per_lot_usd"):
        SpreadCosts(symbol="X", spread_pips=1.0, commission_per_lot_usd=-1.0)


def test_spread_costs_requires_symbol() -> None:
    with pytest.raises(ValueError, match="symbol"):
        SpreadCosts(symbol="", spread_pips=1.0)


def test_spread_costs_frozen() -> None:
    entry = SpreadCosts(symbol="EURUSD", spread_pips=1.5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.spread_pips = 2.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SpreadCostTable.from_mapping — duplicate + construction rules
# ---------------------------------------------------------------------------


def test_from_mapping_duplicates_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate symbol"):
        SpreadCostTable(
            entries=(
                SpreadCosts(symbol="EURUSD", spread_pips=1.5),
                SpreadCosts(symbol="eurusd", spread_pips=1.5),  # case-insensitive dup
            )
        )


def test_from_mapping_construction() -> None:
    table = SpreadCostTable.from_mapping({"EURUSD": 1.5, "USDJPY": 1.2})
    assert set(table.symbols) == {"EURUSD", "USDJPY"}
    assert table.get("EURUSD").slippage_pips == PIP_SLIPPAGE  # default applied


def test_from_mapping_overrides_slippage() -> None:
    table = SpreadCostTable.from_mapping({"EURUSD": 1.5}, slippage_pips=0.5)
    assert table.get("EURUSD").slippage_pips == 0.5
