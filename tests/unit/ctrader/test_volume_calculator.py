"""Unit tests for VolumeCalculator.

All tests construct SymbolInfo objects directly — no cTrader connection required.
"""

import os
import sys

import pytest

# Ensure src/forex_bot is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))

from adapters.ctrader.market_data_feed import SymbolInfo
from adapters.ctrader.volume_calculator import VolumeCalculator

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def symbols():
    """Minimal symbol table: 1=EURUSD (forex), 2=BTCUSD (crypto), 3=USDJPY."""
    return {
        1: SymbolInfo(
            symbol_id=1,
            name="EUR/USD",
            digits=5,
            lot_size=100_000,
            min_volume=1_000,
            max_volume=10_000_000,
            step_volume=1_000,
        ),
        2: SymbolInfo(
            symbol_id=2,
            name="BTC/USD",
            digits=2,
            lot_size=100,
            min_volume=1,
            max_volume=100_000,
            step_volume=1,
        ),
        3: SymbolInfo(
            symbol_id=3,
            name="USD/JPY",
            digits=3,
            lot_size=100_000,
        ),
    }


@pytest.fixture
def calc(symbols):
    return VolumeCalculator(symbols)


# ── Lots ↔ Volume conversion ────────────────────────────────────────────────


def test_forex_lots_to_volume(calc):
    """1. EURUSD 1.0 lot → 100 000 raw volume."""
    assert calc.lots_to_volume(1, 1.0) == 100_000


def test_forex_volume_to_lots(calc):
    """2. EURUSD 100 000 raw → 1.0 lots."""
    assert calc.volume_to_lots(1, 100_000) == 1.0


def test_crypto_lots_to_volume(calc):
    """3. BTCUSD 1.0 lot → 100 raw volume."""
    assert calc.lots_to_volume(2, 1.0) == 100


def test_crypto_volume_to_lots(calc):
    """4. BTCUSD 100 raw → 1.0 lots."""
    assert calc.volume_to_lots(2, 100) == 1.0


# ── Round-trip ──────────────────────────────────────────────────────────────


def test_round_trip_forex(calc):
    """5. Round-trip lots→volume→lots for forex is identity."""
    for lots in [0.01, 0.1, 0.5, 1.0, 2.5]:
        vol = calc.lots_to_volume(1, lots)
        back = calc.volume_to_lots(1, vol)
        assert back == pytest.approx(lots, abs=1e-9), f"Round-trip failed for {lots} lots"


def test_round_trip_crypto(calc):
    """6. Round-trip lots→volume→lots for crypto is identity."""
    for lots in [0.01, 1.0, 10.0]:
        vol = calc.lots_to_volume(2, lots)
        back = calc.volume_to_lots(2, vol)
        assert back == pytest.approx(lots, abs=1e-9), f"Round-trip failed for {lots} lots"


# ── Volume validation ───────────────────────────────────────────────────────


def test_validate_below_min(calc):
    """7. Volume below min_volume → (False, reason contains 'min')."""
    ok, reason = calc.validate_volume(1, 500)  # min is 1_000
    assert ok is False
    assert "min" in reason.lower()


def test_validate_above_max(calc):
    """8. Volume above max_volume → (False, reason contains 'max')."""
    ok, reason = calc.validate_volume(1, 20_000_000)  # max is 10_000_000
    assert ok is False
    assert "max" in reason.lower()


def test_validate_bad_step(calc):
    """9. Volume not aligned to step → (False, reason contains 'step')."""
    # EURUSD step=1000; 1500 is not a multiple
    ok, reason = calc.validate_volume(1, 1_500)
    assert ok is False
    assert "step" in reason.lower()


def test_validate_ok(calc):
    """10. Valid volume → (True, 'OK')."""
    ok, reason = calc.validate_volume(1, 5_000)  # within range, aligned to step
    assert ok is True
    assert reason == "OK"


# ── Price decoding ──────────────────────────────────────────────────────────


def test_price_forex_5digit(calc):
    """11. EURUSD digits=5, raw 109450 → 1.09450."""
    assert calc.price_from_raw(1, 109_450) == pytest.approx(1.09450)


def test_price_crypto_2digit(calc):
    """12. BTCUSD digits=2, raw 674525 → 6745.25."""
    assert calc.price_from_raw(2, 674_525) == pytest.approx(6745.25)


def test_price_jpy_3digit(calc):
    """13. USDJPY digits=3, raw 156780 → 156.780."""
    assert calc.price_from_raw(3, 156_780) == pytest.approx(156.780)


# ── Error / fallback handling ───────────────────────────────────────────────


def test_unknown_symbol_lots(calc):
    """14. Unknown symbol_id on lots_to_volume → ValueError."""
    with pytest.raises(ValueError, match="Unknown symbol_id"):
        calc.lots_to_volume(999, 1.0)


def test_unknown_symbol_price(calc):
    """15. Unknown symbol_id on price_from_raw → falls back to /100_000."""
    result = calc.price_from_raw(999, 100_000)
    assert result == pytest.approx(1.0)


# ── Defaults ────────────────────────────────────────────────────────────────


def test_default_lot_size():
    """16. SymbolInfo default lot_size is 100_000."""
    sym = SymbolInfo(symbol_id=0, name="X")
    assert sym.lot_size == 100_000


def test_crypto_volume_not_forex(calc):
    """17. BTCUSD 0.01 lots → 1 raw (not 1000 which would be forex)."""
    assert calc.lots_to_volume(2, 0.01) == 1
