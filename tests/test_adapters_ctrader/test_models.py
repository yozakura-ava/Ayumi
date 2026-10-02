"""Tests for adapters.ctrader.models pip convention correctness.

Verifies that:
1. XAUUSD pip_size = 0.1 (canonical) in SYMBOL_METADATA
2. get_symbol_info() delegates pip_size to utils.pip_value.pip_value_for_symbol()
3. Lot-size math is invariant across convention changes (math cancels)
4. Convention drift is detected between modules
"""

import os
import sys

import pytest

# Add src to path so imports work without full package install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "forex_bot"))

from adapters.ctrader.models import SYMBOL_METADATA, get_symbol_info
from utils.pip_value import pip_value_for_symbol


class TestXAUUUDPipConvention:
    """XAUUSD must use pip_size=0.1 (canonical cTrader gold pip)."""

    def test_symbol_metadata_xauusd_pip_size(self):
        """SYMBOL_METADATA XAUUSD pip_size must be 0.1, not 0.01."""
        info = SYMBOL_METADATA["XAUUSD"]
        assert info.pip_size == 0.1, f"XAUUSD pip_size should be 0.1 (canonical), got {info.pip_size}"

    def test_symbol_metadata_xauusd_pip_value(self):
        """SYMBOL_METADATA XAUUSD pip_value_per_lot must be 10.0."""
        info = SYMBOL_METADATA["XAUUSD"]
        assert info.pip_value_per_lot == 10.0, f"XAUUSD pip_value_per_lot should be 10.0, got {info.pip_value_per_lot}"

    def test_get_symbol_info_xauusd_pip_size(self):
        """get_symbol_info('XAUUSD') must return canonical pip_size=0.1."""
        info = get_symbol_info("XAUUSD")
        assert info.pip_size == 0.1

    def test_get_symbol_info_delegates_to_canonical(self):
        """get_symbol_info() pip_size must match pip_value_for_symbol() for all known symbols."""
        for symbol in SYMBOL_METADATA:
            info = get_symbol_info(symbol)
            canonical = pip_value_for_symbol(symbol)
            assert info.pip_size == canonical, (
                f"{symbol}: get_symbol_info pip_size={info.pip_size} != pip_value_for_symbol={canonical}"
            )

    def test_get_symbol_info_unknown_symbol_uses_canonical(self):
        """Unknown symbols should still get canonical pip_size from pip_value_for_symbol()."""
        info = get_symbol_info("USDZAR")
        canonical = pip_value_for_symbol("USDZAR")
        assert info.pip_size == canonical

    def test_get_symbol_info_case_insensitive(self):
        """get_symbol_info should handle case-insensitive symbol names."""
        lower = get_symbol_info("xauusd")
        upper = get_symbol_info("XAUUSD")
        assert lower.pip_size == upper.pip_size == 0.1


class TestLotSizeInvariance:
    """Verify that lot-size math is invariant across pip convention changes.

    The key property: lot_size = risk / (sl_pips * pip_value_per_lot)
    If pip_size is 10x larger, sl_pips is 10x smaller, but pip_value_per_lot
    is 10x larger, so the product (sl_pips * pip_value_per_lot) is unchanged.
    """

    def test_xauusd_lot_size_same_regardless_of_convention(self):
        """Same XAUUSD trade produces same lot size with old or new convention.

        Old (wrong): pip_size=0.01, pip_value_per_lot=1.0
        New (correct): pip_size=0.1, pip_value_per_lot=10.0

        For entry=2000, SL=1995, risk=$50:
        Old: sl_distance = 5, sl_pips = 5/0.01 = 500, lot = 50/(500*1.0) = 0.10
        New: sl_distance = 5, sl_pips = 5/0.1 = 50,  lot = 50/(50*10.0) = 0.10
        """
        risk_amount = 50.0
        entry_price = 2000.0
        sl_price = 1995.0
        sl_distance = abs(entry_price - sl_price)

        # Old (wrong) convention
        old_pip_size = 0.01
        old_pip_value = 1.0
        old_sl_pips = sl_distance / old_pip_size
        old_lot = risk_amount / (old_sl_pips * old_pip_value)

        # New (correct) convention
        new_pip_size = 0.1
        new_pip_value = 10.0
        new_sl_pips = sl_distance / new_pip_size
        new_lot = risk_amount / (new_sl_pips * new_pip_value)

        assert old_lot == pytest.approx(new_lot, rel=1e-9), (
            f"Lot size changed: old={old_lot}, new={new_lot} — math should cancel"
        )

    def test_forex_pair_lot_size_unchanged(self):
        """EURUSD lot size must be identical (convention didn't change for FX)."""
        risk_amount = 50.0
        entry_price = 1.0850
        sl_price = 1.0840
        sl_distance = abs(entry_price - sl_price)

        info = get_symbol_info("EURUSD")
        sl_pips = sl_distance / info.pip_size
        lot = risk_amount / (sl_pips * info.pip_value_per_lot)

        # Expected: sl_distance=0.0010, pip_size=0.0001, sl_pips=10
        # lot = 50 / (10 * 10.0) = 0.50
        assert lot == pytest.approx(0.50, rel=1e-6)


class TestConventionDrift:
    """Convention drift detector: catches if XAUUSD pip_size diverges across modules."""

    def test_no_pip_size_drift_between_modules(self):
        """XAUUSD pip_size must be identical in models.py and sl_position_sizer.py."""
        from risk.sl_position_sizer import INSTRUMENTS

        models_info = get_symbol_info("XAUUSD")
        sizer_spec = INSTRUMENTS["XAUUSD"]

        assert models_info.pip_size == sizer_spec.pip_size, (
            f"Drift detected: models.py pip_size={models_info.pip_size} vs "
            f"sl_position_sizer pip_size={sizer_spec.pip_size}"
        )

    def test_all_symbols_match_canonical(self):
        """Every symbol in SYMBOL_METADATA must have pip_size matching pip_value_for_symbol()."""
        from utils.pip_value import pip_value_for_symbol

        for symbol, info in SYMBOL_METADATA.items():
            canonical = pip_value_for_symbol(symbol)
            assert info.pip_size == canonical, (
                f"{symbol}: SYMBOL_METADATA pip_size={info.pip_size} != canonical {canonical}"
            )
