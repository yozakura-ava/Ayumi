"""Regression tests for the blend-path lot/P&L unit mix (card 047cd91d).

Card: ``047cd91d-51b1-46f1-b8a9-dd1add80338a``

Two compounding bugs caused the orchestrator→order-manager P&L to be
off by ~1000× on the blend execution path:

1. ``OrderManager._close_position`` and ``update_position`` defaulted
   ``contract_size=100_000.0`` (FX standard lot units) instead of
   looking up the symbol's actual ``contract_size`` (e.g. 100 oz/lot
   for XAUUSD). Result: P&L on XAUUSD was 1000× too large.
2. ``PaperTrader.process_signal`` always recomputed the trade volume
   via ``OrderManager.calculate_position_size`` with full-risk
   defaults, overriding the orchestrator's canonical SWARM-profile
   sizing. Result: orchestrator said 0.05 lots, paper trader
   executed 0.10 lots.

These tests pin the canonical math for the blend path so the regression
cannot silently recur:

* Sniper sizing: XAUUSD entry=3355.30/sl=3350.37/risk=$50 → lots ≈ 0.10
* SL-close P&L on XAUUSD uses ``contract_size=100`` (not 100_000)
* Both ``SHORT`` and ``LONG`` paths covered
* ``PaperTrader`` honours ``signal.volume`` when supplied by the
  orchestrator (no silent override to 0.10 lots for SWARM-profile
  signals)
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure src/forex_bot is importable (mirrors other ctrader tests).
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "src", "forex_bot"),
)

from adapters.ctrader.models import TradeDirection, get_symbol_info
from adapters.ctrader.order_manager import OrderManager
from adapters.ctrader.paper_trader import PaperTrader
from risk.sl_position_sizer import SLPositionSizer

# ── Helpers ─────────────────────────────────────────────────────────────────


def _xauusd_spec():
    """Canonical XAUUSD contract metadata."""
    info = get_symbol_info("XAUUSD")
    assert info.pip_size == pytest.approx(0.1), (
        f"XAUUSD pip_size drifted to {info.pip_size}; canonical is 0.1 "
        "(see utils.pip_value.pip_value_for_symbol)"
    )
    assert info.pip_value_per_lot == pytest.approx(10.0)
    assert info.contract_size == pytest.approx(100.0)
    return info


# ── Test 1: Sniper sizing on XAUUSD yields canonical lots ──────────────────


class TestXAUUSDSniperSizing:
    """Pin the canonical ``$50 risk / (49.3 pips × $10/pip/lot) = 0.10 lots``.

    This is the exact AC scenario from card ``047cd91d``: XAUUSD
    entry=3355.30, sl=3350.37, sniper profile (full risk $50).
    """

    def test_xauusd_sniper_lots_match_canonical_formula(self):
        sizer = SLPositionSizer(
            account_balance=10000.0,
            risk_per_trade_pct=0.005,  # 0.5% × $10k = $50 base risk
            max_lot_size=1.0,
            daily_risk_cap_pct=0.03,
        )
        result = sizer.calculate(
            symbol="XAUUSD",
            entry_price=3355.30,
            sl_price=3350.37,
            profile="sniper",
        )

        assert not result.blocked, f"Sizing blocked: {result.block_reason}"
        # 50 / (49.3 × 10) = 0.1014 lots → 0.10 after round-to-2dp.
        assert result.sl_distance_pips == pytest.approx(49.3, abs=0.1)
        assert result.lots == pytest.approx(0.10, abs=0.005), (
            f"Expected lots≈0.10, got {result.lots}; "
            "the canonical $50/49.3pips/$10 formula must hold for XAUUSD"
        )
        assert result.risk_amount == pytest.approx(50.0, abs=0.5)

    def test_xauusd_swarm_lots_are_half_sniper(self):
        """SWARM profile (the harness path) uses half the per-trade risk."""
        sizer = SLPositionSizer(
            account_balance=10000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=1.0,
            daily_risk_cap_pct=0.03,
        )
        sniper = sizer.calculate("XAUUSD", 3355.30, 3350.37, profile="sniper")
        swarm = sizer.calculate("XAUUSD", 3355.30, 3350.37, profile="swarm")
        assert not sniper.blocked and not swarm.blocked
        # 0.05 / 0.10 = 0.5 (swarm = half sniper lots).
        assert swarm.lots == pytest.approx(sniper.lots * 0.5, abs=0.005)


# ── Test 2: OrderManager uses symbol contract_size, not 100k default ───────


class TestOrderManagerUsesSymbolContractSize:
    """Pin ``contract_size`` lookup so XAUUSD P&L is not 1000× inflated."""

    def _open_position(self, om: OrderManager, direction: TradeDirection) -> str:
        """Open a 0.10-lot XAUUSD position via the paper order path."""
        result = om.execute_paper_order(
            symbol="XAUUSD",
            direction=direction,
            volume=0.10,
            entry_price=3355.30,
            stop_loss=3350.37 if direction == TradeDirection.LONG else 3360.37,
            take_profit=3400.00 if direction == TradeDirection.LONG else 3300.00,
            spread=0.0,
            bid=3355.30,
            ask=3355.30,
        )
        assert result.success, f"Setup failed: {result.error_message}"
        pos = result.position
        assert pos is not None
        return pos.position_id

    def test_xauusd_short_sl_close_uses_contract_size_100(self):
        """SHORT: P&L on SL hit must use XAUUSD contract_size=100.

        For a SHORT opened at 3355.30 with SL 3360.37:
            pnl = (entry - exit) × lots × contract_size
                = (3355.30 - 3360.37) × 0.10 × 100
                = -5.07 × 0.10 × 100
                = -$50.70   (≈ -$50 expected)
        With the buggy ``contract_size=100_000`` default the P&L
        would have been -$50,700 — a 1000× distortion.
        """
        om = OrderManager()
        pos_id = self._open_position(om, TradeDirection.SHORT)

        # Tick: SL hit (ask ≥ SL for SHORT).
        om.update_position(
            pos_id,
            current_price=3360.37,
            bid=3360.37,
            ask=3360.37,
        )

        pos = om.get_position(pos_id)
        assert pos is not None
        assert pos.status.name == "CLOSED"
        # Tolerance ±$1 to absorb slippage/slippage-model rounding in
        # ``execute_paper_order`` (paper fill price may shift SL exit by
        # a fraction of a pip).
        assert pos.closed_pnl == pytest.approx(-50.70, abs=1.0), (
            f"XAUUSD SHORT SL-close P&L must use contract_size=100; "
            f"got {pos.closed_pnl:.2f} (expected ~-$50). "
            f"If you see ~-$50,000 the contract_size default regressed."
        )
        # And not the 1000× inflation.
        assert pos.closed_pnl > -1000.0, (
            f"P&L is implausibly large: {pos.closed_pnl:.2f}; "
            "the contract_size bug has regressed"
        )

    def test_xauusd_long_sl_close_uses_contract_size_100(self):
        """LONG: P&L on SL hit must use XAUUSD contract_size=100.

        For a LONG opened at 3355.30 with SL 3350.37:
            pnl = (exit - entry) × lots × contract_size
                = (3350.37 - 3355.30) × 0.10 × 100
                = -4.93 × 0.10 × 100
                = -$49.30
        """
        om = OrderManager()
        pos_id = self._open_position(om, TradeDirection.LONG)

        om.update_position(
            pos_id,
            current_price=3350.37,
            bid=3350.37,
            ask=3350.37,
        )

        pos = om.get_position(pos_id)
        assert pos is not None
        assert pos.status.name == "CLOSED"
        assert pos.closed_pnl == pytest.approx(-49.30, abs=1.0), (
            f"XAUUSD LONG SL-close P&L must use contract_size=100; "
            f"got {pos.closed_pnl:.2f} (expected ~-$49)"
        )

    def test_resolve_contract_size_prefers_caller_value(self):
        """Explicit contract_size wins over symbol canonical."""
        om = OrderManager()
        # Caller passes 250_000 explicitly — must NOT be overridden.
        assert om._resolve_contract_size("XAUUSD", 250_000.0) == 250_000.0
        assert om._resolve_contract_size("EURUSD", 50_000.0) == 50_000.0

    def test_resolve_contract_size_falls_back_to_symbol(self):
        """None → symbol-canonical lookup."""
        om = OrderManager()
        assert om._resolve_contract_size("XAUUSD", None) == pytest.approx(100.0)
        assert om._resolve_contract_size("EURUSD", None) == pytest.approx(100_000.0)

    def test_close_position_with_explicit_contract_size_uses_it(self):
        """Regression: explicit contract_size still wins on close_position."""
        om = OrderManager()
        pos_id = self._open_position(om, TradeDirection.LONG)
        # Explicitly force FX default → P&L must reflect 100_000.
        closed = om.close_position(
            pos_id,
            exit_price=3360.00,
            reason="manual",
            contract_size=100_000.0,
        )
        assert closed is not None
        # (3360.00 - 3355.30) × 0.10 × 100_000 = $4.70 × 10_000 = $47,000
        assert closed.closed_pnl == pytest.approx(47_000.0, abs=10.0)


# ── Test 3: PaperTrader respects orchestrator-supplied volume ──────────────


class TestPaperTraderRespectsOrchestratorVolume:
    """Pin the paper-trader path so it does NOT silently override lots.

    Bug B (047cd91d): ``PaperTrader.process_signal`` previously always
    recomputed volume via ``OrderManager.calculate_position_size``,
    overriding the orchestrator's SWARM-profile 0.05 lots with full-
    risk 0.10 lots. After the fix, when ``signal.volume`` is set the
    paper trader uses it as canonical.
    """

    def _make_paper_trader(self, tmp_path) -> PaperTrader:
        # Use a fresh PaperTrader with no risk-guard state file so the
        # tests are deterministic and isolated.
        return PaperTrader(
            starting_balance=10000.0,
            state_path=str(tmp_path / "_test_paper_trader_state.json"),
        )

    def _make_xauusd_signal(self, volume, direction: TradeDirection):
        from adapters.ctrader.models import CTraderTradeSignal

        return CTraderTradeSignal(
            strategy_id="t",
            symbol="XAUUSD",
            direction=direction,
            entry_price=3355.30,
            stop_loss=3350.37 if direction == TradeDirection.LONG else 3360.37,
            take_profit_1=3400.00 if direction == TradeDirection.LONG else 3300.00,
            take_profit_2=None,
            take_profit_3=None,
            confidence=0.55,
            rationale="unit-test",
            volume=volume,
            timestamp=None,
        )

    def test_supplied_volume_05_is_used_not_overridden(self, tmp_path):
        pt = self._make_paper_trader(tmp_path)
        signal = self._make_xauusd_signal(volume=0.05, direction=TradeDirection.LONG)

        result = pt.process_signal(signal, spread=0.0, bid=3355.30, ask=3355.30)
        assert result.success, f"Trade rejected: {result.rejection_reason}"

        open_positions = pt.get_open_positions()
        assert len(open_positions) == 1
        # The bug would have produced 0.10 lots (recompute full risk).
        # The fix must keep 0.05 lots (orchestrator's SWARM decision).
        assert open_positions[0].volume == pytest.approx(0.05, abs=1e-9), (
            f"Paper trader must respect signal.volume=0.05; "
            f"got {open_positions[0].volume} (regression of "
            "047cd91d Bug B — orchestrator lots being overridden)"
        )

    def test_supplied_volume_10_is_used_not_overridden(self, tmp_path):
        """Sniper-sized 0.10 lots must also pass through unchanged."""
        pt = self._make_paper_trader(tmp_path)
        signal = self._make_xauusd_signal(volume=0.10, direction=TradeDirection.SHORT)

        result = pt.process_signal(signal, spread=0.0, bid=3355.30, ask=3355.30)
        assert result.success, f"Trade rejected: {result.rejection_reason}"

        open_positions = pt.get_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0].volume == pytest.approx(0.10, abs=1e-9)

    def test_no_supplied_volume_falls_back_to_recompute(self, tmp_path):
        """Backward compat: signals without ``volume`` still get sized."""
        from adapters.ctrader.models import CTraderTradeSignal

        pt = self._make_paper_trader(tmp_path)
        signal = CTraderTradeSignal(
            strategy_id="t",
            symbol="XAUUSD",
            direction=TradeDirection.LONG,
            entry_price=3355.30,
            stop_loss=3350.37,
            take_profit_1=3400.00,
            take_profit_2=None,
            take_profit_3=None,
            confidence=0.55,
            rationale="unit-test-no-volume",
            volume=None,
            timestamp=None,
        )
        result = pt.process_signal(signal, spread=0.0, bid=3355.30, ask=3355.30)
        assert result.success, f"Trade rejected: {result.rejection_reason}"

        open_positions = pt.get_open_positions()
        assert len(open_positions) == 1
        # OrderManager.calculate_position_size for XAUUSD with $50 risk
        # / 49.3 pips / $10/pip/lot = 0.10 lots.
        assert open_positions[0].volume == pytest.approx(0.10, abs=0.005)


# ── Test 4: End-to-end SHORT+LONG through OrderManager ─────────────────────


class TestEndToEndBlendUnitMix:
    """Walk the full orchestrator→OrderManager path for both directions.

    Combines the SLPositionSizer canonical sizing with the symbol-aware
    ``contract_size`` resolution so the AC scenario lands exactly on
    lots ≈ 0.10 and P&L ≈ -$49 / -$50.
    """

    @pytest.mark.parametrize(
        ("direction", "entry", "sl", "exit", "expected_pnl"),
        [
            # LONG: SL below entry → (exit - entry) × lots × contract_size
            ("LONG", 3355.30, 3350.37, 3350.37, -49.30),
            # SHORT: SL above entry → (entry - exit) × lots × contract_size
            ("SHORT", 3355.30, 3360.37, 3360.37, -50.70),
        ],
    )
    def test_sniper_lots_and_sl_pnl_match_canonical(
        self, direction, entry, sl, exit, expected_pnl
    ):
        sizer = SLPositionSizer(
            account_balance=10000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=1.0,
            daily_risk_cap_pct=0.03,
        )
        size_result = sizer.calculate(
            symbol="XAUUSD",
            entry_price=entry,
            sl_price=sl,
            profile="sniper",
        )
        assert not size_result.blocked
        lots = size_result.lots
        # Sniper lots ≈ 0.10 for both directions (SL distance 49.3 / 50.7
        # pips still rounds to 0.10 lots for the canonical formula).
        assert lots == pytest.approx(0.10, abs=0.005)

        # Walk through OrderManager (uses canonical contract_size=100).
        om = OrderManager()
        result = om.execute_paper_order(
            symbol="XAUUSD",
            direction=TradeDirection[direction],
            volume=lots,
            entry_price=entry,
            stop_loss=sl,
            take_profit=4000.00 if direction == "LONG" else 3000.00,
            spread=0.0,
            bid=entry,
            ask=entry,
        )
        assert result.success

        # Tick SL hit.
        om.update_position(
            result.position.position_id,
            current_price=exit,
            bid=exit,
            ask=exit,
        )
        closed = om.get_position(result.position.position_id)
        assert closed.status.name == "CLOSED"
        assert closed.closed_pnl == pytest.approx(expected_pnl, abs=1.0), (
            f"{direction} XAUUSD SL-close P&L must be ~${expected_pnl}; "
            f"got {closed.closed_pnl:.2f}"
        )
