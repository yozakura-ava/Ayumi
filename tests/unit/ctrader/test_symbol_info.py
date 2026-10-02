"""Unit tests for SymbolInfo dataclass."""

import os
import sys

# Ensure src/forex_bot is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))

from adapters.ctrader.market_data_feed import SymbolInfo


def test_defaults_forex():
    """1. Default SymbolInfo has forex-standard lot_size and digits."""
    sym = SymbolInfo(symbol_id=1, name="EUR/USD")
    assert sym.lot_size == 100_000
    assert sym.digits == 5


def test_crypto_config():
    """2. BTCUSD SymbolInfo with lot_size=100."""
    sym = SymbolInfo(symbol_id=2, name="BTC/USD", lot_size=100, digits=2)
    assert sym.lot_size == 100
    assert sym.digits == 2


def test_contract_size_property():
    """3. contract_size property equals float(lot_size)."""
    sym = SymbolInfo(symbol_id=1, name="EUR/USD", lot_size=100_000)
    assert sym.contract_size == 100_000.0
    assert isinstance(sym.contract_size, float)


def test_volume_constraints():
    """4. min/max/step volume are independently settable."""
    sym = SymbolInfo(
        symbol_id=1,
        name="EUR/USD",
        min_volume=5_000,
        max_volume=5_000_000,
        step_volume=5_000,
    )
    assert sym.min_volume == 5_000
    assert sym.max_volume == 5_000_000
    assert sym.step_volume == 5_000
