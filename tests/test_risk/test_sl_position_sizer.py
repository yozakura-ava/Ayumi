"""Tests for risk.sl_position_sizer pip convention correctness.

Verifies that:
1. XAUUSD InstrumentSpec uses canonical pip_size=0.1
2. Lot-size math produces same results as the old convention (math cancels)
3. _compute_position_risk_usd uses correct pip values
"""

import os
import sys

import pytest

# Add src to path so imports work without full package install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "forex_bot"))

from risk.sl_position_sizer import (
    INSTRUMENTS,
    SLPositionSizer,
    _compute_position_risk_usd,
)


class TestXAUUUDInstrumentSpec:
    """XAUUSD spec must match canonical convention."""

    def test_xauusd_pip_size(self):
        """XAUUSD pip_size must be 0.1 (canonical)."""
        spec = INSTRUMENTS["XAUUSD"]
        assert spec.pip_size == 0.1, f"XAUUSD pip_size should be 0.1, got {spec.pip_size}"

    def test_xauusd_pip_value_per_lot(self):
        """XAUUSD pip_value_per_lot must be 10.0."""
        spec = INSTRUMENTS["XAUUSD"]
        assert spec.pip_value_per_lot == 10.0, f"XAUUSD pip_value_per_lot should be 10.0, got {spec.pip_value_per_lot}"

    def test_xauusd_lot_size(self):
        """XAUUSD lot_size must be 100 (1 lot = 100 oz)."""
        spec = INSTRUMENTS["XAUUSD"]
        assert spec.lot_size == 100

    def test_all_forex_pairs_unchanged(self):
        """Standard FX pairs should still have correct pip_size=0.0001."""
        for sym in ("EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD"):
            spec = INSTRUMENTS[sym]
            assert spec.pip_size == 0.0001, f"{sym} pip_size changed: {spec.pip_size}"

    def test_usdjpy_pip_size(self):
        """USDJPY pip_size must be 0.01."""
        spec = INSTRUMENTS["USDJPY"]
        assert spec.pip_size == 0.01


class TestLotSizeInvariance:
    """Verify lot-size math invariance for XAUUSD.

    The sizing formula: lots = risk / (sl_pips * pip_value_per_lot)
    With old convention (pip=0.01, value=1.0): sl_pips = dist/0.01, product = dist/0.01 * 1.0
    With new convention (pip=0.1, value=10.0): sl_pips = dist/0.1, product = dist/0.1 * 10.0
    Both products are identical → same lot size. This is the "math cancels" property.
    """

    def test_xauusd_lot_invariance(self):
        """SLPositionSizer.calculate produces same lot size for XAUUSD."""
        sizer = SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=1.0,
            daily_risk_cap_pct=0.03,
        )

        # Typical XAUUSD trade: entry=2000, SL=1995
        result = sizer.calculate("XAUUSD", entry_price=2000.0, sl_price=1995.0)

        assert not result.blocked, f"Should not be blocked: {result.block_reason}"
        assert result.lots > 0

        # Manual check: risk = 10000 * 0.005 = $50
        # sl_distance = 2000 - 1995 = $5
        # sl_pips = 5 / 0.1 = 50 pips
        # lots = 50 / (50 * 10.0) = 0.10
        assert result.lots == pytest.approx(0.10, rel=1e-6), f"Expected ~0.10 lots, got {result.lots}"

    def test_xauusd_lot_same_as_old_convention(self):
        """Manual calculation with old convention gives same lot size."""
        risk = 50.0  # $50 risk
        entry = 2000.0
        sl = 1995.0
        sl_distance = abs(entry - sl)

        # Old convention
        old_pips = sl_distance / 0.01
        old_lot = risk / (old_pips * 1.0)

        # New convention (what INSTRUMENTS now uses)
        spec = INSTRUMENTS["XAUUSD"]
        new_pips = sl_distance / spec.pip_size
        new_lot = risk / (new_pips * spec.pip_value_per_lot)

        assert old_lot == pytest.approx(new_lot, rel=1e-9)


class TestComputePositionRiskUSD:
    """Verify _compute_position_risk_usd uses correct pip values."""

    def test_xauusd_risk_with_sl(self):
        """Risk calculation for XAUUSD with SL uses canonical pip values."""
        risk = _compute_position_risk_usd(
            symbol="XAUUSD",
            entry_price=2000.0,
            sl_price=1995.0,
            lots=0.10,
        )
        # sl_distance = 5, pips = 5/0.1 = 50, risk = 50 * 0.10 * 10.0 = 50.0
        assert risk == pytest.approx(50.0, rel=1e-6)

    def test_xauusd_risk_no_sl_fallback(self):
        """Without SL, falls back to lots * 100."""
        risk = _compute_position_risk_usd(
            symbol="XAUUSD",
            entry_price=2000.0,
            sl_price=None,
            lots=0.10,
        )
        assert risk == pytest.approx(10.0, rel=1e-6)

    def test_eurusd_risk_unchanged(self):
        """EURUSD risk calculation is unaffected by XAUUSD fix."""
        risk = _compute_position_risk_usd(
            symbol="EURUSD",
            entry_price=1.0850,
            sl_price=1.0840,
            lots=0.50,
        )
        # sl_distance = 0.0010, pips = 0.0010/0.0001 = 10
        # risk = 10 * 0.50 * 10.0 = 50.0
        assert risk == pytest.approx(50.0, rel=1e-6)
