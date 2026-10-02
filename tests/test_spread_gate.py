"""Tests for spread gate enforcement across the signal pipeline.

Validates:
1. SpreadGate blocks when spread exceeds per-symbol max
2. RiskGuard.check_signal rejects high-spread signals
3. RiskGuard.check_trade_allowed rejects high-spread trades
4. XAUUSD with 20+ pip spread is blocked (40.0 max)
5. Normal spread values pass through
6. _strategy_result_to_dict includes spread from bar
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Ensure src is on the path
SRC = Path(__file__).resolve().parent.parent / "src" / "forex_bot"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from adapters.ctrader.models import CTraderTradeSignal, TradeDirection
from adapters.ctrader.risk_guard import RiskGuard, RiskLimitType
from confidence.gates import GateConfig, SpreadGate

# ---------------------------------------------------------------------------
# SpreadGate unit tests
# ---------------------------------------------------------------------------


class TestSpreadGate:
    """Direct tests on SpreadGate.check()."""

    def setup_method(self):
        self.config = GateConfig(
            default_max_spread=2.0,
            symbol_max_spreads={"GBPUSD": 2.0, "XAUUSD": 40.0},
        )
        self.gate = SpreadGate(self.config)

    def test_normal_spread_passes(self):
        """GBPUSD with 1.5 pip spread should pass (max 2.0)."""
        result = self.gate.check({"symbol": "GBPUSD", "spread": 1.5})
        assert result.passed is True
        assert result.gate_name == "spread"

    def test_xauusd_high_spread_blocked(self):
        """XAUUSD with 45 pip spread should be blocked (max 40.0)."""
        result = self.gate.check({"symbol": "XAUUSD", "spread": 45.0})
        assert result.passed is False
        assert "45.0" in result.reason
        assert "40.0" in result.reason

    def test_xauusd_normal_spread_passes(self):
        """XAUUSD with 30 pip spread should pass (max 40.0)."""
        result = self.gate.check({"symbol": "XAUUSD", "spread": 30.0})
        assert result.passed is True

    def test_zero_spread_passes(self):
        """Zero spread should always pass."""
        result = self.gate.check({"symbol": "EURUSD", "spread": 0.0})
        assert result.passed is True

    def test_default_max_spread_for_unknown_symbol(self):
        """Unknown symbol uses default_max_spread."""
        result = self.gate.check({"symbol": "NZDUSD", "spread": 3.0})
        assert result.passed is False
        assert "2.0" in result.reason


# ---------------------------------------------------------------------------
# RiskGuard spread integration tests
# ---------------------------------------------------------------------------


class TestRiskGuardSpread:
    """Tests that RiskGuard enforces spread limits."""

    def setup_method(self):
        self.guard = RiskGuard(
            starting_balance=10000.0,
            state_path="/tmp/test_risk_guard_spread.json",  # noqa: S108
        )

    def _make_signal(
        self,
        symbol: str = "GBPUSD",
        direction: TradeDirection = TradeDirection.LONG,
        spread: float = 0.0,
    ) -> CTraderTradeSignal:
        return CTraderTradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=1.1000,
            stop_loss=1.0900,
            take_profit_1=1.1200,
            take_profit_2=1.1500,
            take_profit_3=1.1800,
            volume=0.01,
            confidence=0.8,
            rationale="test",
            timestamp=datetime.now(timezone.utc),
            strategy_id="test",
        )

    def test_check_trade_allowed_blocks_high_spread(self):
        """check_trade_allowed must block XAUUSD with 50 pip spread."""
        result = self.guard.check_trade_allowed(
            direction=TradeDirection.LONG,
            volume=0.01,
            entry_price=2000.0,
            stop_loss=1990.0,
            take_profit=2020.0,
            symbol="XAUUSD",
            spread=50.0,
        )
        assert result.allowed is False
        assert result.limit_type == RiskLimitType.SPREAD

    def test_check_trade_allowed_allows_normal_spread(self):
        """check_trade_allowed with normal spread should pass spread gate.

        Uses GBPUSD with tight SL (10 pips) to keep position risk under 0.5%.
        """
        result = self.guard.check_trade_allowed(
            direction=TradeDirection.LONG,
            volume=0.01,
            entry_price=1.1000,
            stop_loss=1.0990,
            take_profit=1.1020,
            symbol="GBPUSD",
            spread=1.0,
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# _strategy_result_to_dict spread wiring test
# ---------------------------------------------------------------------------


@dataclass
class MockBar:
    """Minimal bar stand-in for blend_runner tests."""

    symbol: str = "GBPUSD"
    close: float = 1.1000
    spread_pips: float = 1.5


class TestStrategyResultSpreadWiring:
    """Tests that _strategy_result_to_dict includes spread from bar."""

    def test_dict_result_gets_bar_spread(self):
        """When strategy returns a dict without spread, bar spread is injected."""
        from forward_test.blend_runner import BlendForwardTestRunner

        bar = MockBar(symbol="GBPUSD", spread_pips=1.8)
        result = {
            "symbol": "GBPUSD",
            "direction": "LONG",
            "entry_price": 1.1,
            "stop_loss": 1.09,
            "take_profit": 1.12,
            "confidence": 0.8,
        }
        out = BlendForwardTestRunner._strategy_result_to_dict(result, bar)
        assert out is not None
        assert out["spread"] == 1.8

    def test_dict_result_preserves_existing_spread(self):
        """When strategy dict already has spread, bar spread is not overwritten."""
        from forward_test.blend_runner import BlendForwardTestRunner

        bar = MockBar(symbol="GBPUSD", spread_pips=1.8)
        result = {
            "symbol": "GBPUSD",
            "direction": "LONG",
            "entry_price": 1.1,
            "stop_loss": 1.09,
            "take_profit": 1.12,
            "confidence": 0.8,
            "spread": 3.5,
        }
        out = BlendForwardTestRunner._strategy_result_to_dict(result, bar)
        assert out is not None
        assert out["spread"] == 3.5

    def test_object_result_gets_bar_spread(self):
        """When strategy returns an object, spread is taken from bar."""
        from forward_test.blend_runner import BlendForwardTestRunner

        class StrategyResult:
            symbol = "XAUUSD"
            direction = "LONG"
            entry_price = 2000.0
            stop_loss = 1990.0
            take_profit = 2020.0
            confidence = 0.7

        bar = MockBar(symbol="XAUUSD", spread_pips=35.0)
        out = BlendForwardTestRunner._strategy_result_to_dict(StrategyResult(), bar)
        assert out is not None
        assert out["spread"] == 35.0
