"""Tests for the refactored MultiStrategyBacktestEngine.

These tests verify that the mixin-composition refactor preserves the
essential behaviors of the multi-strategy engine without the code
duplication that existed before.

Note (2026-09-16, card 6f06c041 rework r1): Amalgamation/AmalgamatedBacktestEngine
tests were removed when backtest.amalgamation was retired as a dead module
(zero callsites). See card 6f06c041-1c7c-47f5-9f72-6c34893e95fc.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/forex_bot is on sys.path
_src = Path(__file__).resolve().parents[2] / "src" / "forex_bot"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from backtest.multi_strategy_engine import (
    KellyConfig,
    MultiStrategyBacktestEngine,
    MultiStrategyConfig,
    StrategyBacktestResult,
)
from core.config import BacktestConfig
from core.types import (
    Bar,
    StrategySignal,
    TradeDirection,
)
from engine.base import EngineCore
from engine.mixins import ProgressiveSLMixin

# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_config(pair: str = "EURUSD") -> BacktestConfig:
    return BacktestConfig(
        pair=pair,
        starting_balance=10_000.0,
        risk_per_trade_pct=0.005,
        leverage=30,
        max_open_trades=3,
        min_bars_before_signal=5,
        min_confidence=0.40,
        max_total_drawdown_pct=0.20,
        max_daily_drawdown_pct=0.05,
        spread_pips=1.0,
        slippage_pips=0.5,
        commission_per_lot=7.0,
        swap_per_lot_per_day=0.0,
        sharpe_annualization_factor=252,
    )


def _make_bars(n: int = 50, base_price: float = 1.1000) -> list[Bar]:
    bars: list[Bar] = []
    t = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    for i in range(n):
        noise = 0.0002 * ((-1) ** i)
        bars.append(
            Bar(
                time=t.replace(hour=8 + (i % 8)),
                open=base_price + noise,
                high=base_price + noise + 0.0005,
                low=base_price + noise - 0.0005,
                close=base_price + noise + 0.0001,
                volume=1000.0,
            )
        )
    return bars


class _DummyStrategy:
    """Minimal strategy for testing — always returns a LONG signal."""

    def __init__(self, name: str = "Dummy", direction: TradeDirection = TradeDirection.LONG):
        self.name = name
        self._direction = direction

    def evaluate(self, state) -> StrategySignal | None:
        entry = 1.1000
        return StrategySignal(
            direction=self._direction,
            confidence=0.70,
            entry_price=entry,
            stop_loss=entry - 0.0020,
            take_profit_1=entry + 0.0020,
            take_profit_2=entry + 0.0040,
            take_profit_3=entry + 0.0060,
            rationale=f"{self.name}: test signal",
            is_volatile=False,
        )


# ────────────────────────────────────────────────────────────────────
# Tests: Mixin composition
# ────────────────────────────────────────────────────────────────────


class TestMixinComposition:
    def test_multi_strategy_inherits_engine_core(self):
        """MultiStrategyBacktestEngine must inherit from EngineCore."""
        assert issubclass(MultiStrategyBacktestEngine, EngineCore)

    def test_multi_strategy_inherits_progressive_sl(self):
        """MultiStrategyBacktestEngine must inherit ProgressiveSLMixin."""
        assert issubclass(MultiStrategyBacktestEngine, ProgressiveSLMixin)


# ────────────────────────────────────────────────────────────────────
# Tests: MultiStrategyBacktestEngine
# ────────────────────────────────────────────────────────────────────


class TestMultiStrategyBacktestEngine:
    def test_init_with_defaults(self):
        config = _make_config()
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy()],
        )
        assert engine.balance == config.starting_balance
        assert isinstance(engine.multi_config, MultiStrategyConfig)
        assert isinstance(engine._kelly_config, KellyConfig)
        assert engine._kelly_closed_trades == []

    def test_init_with_custom_configs(self):
        config = _make_config()
        multi = MultiStrategyConfig(min_combined_confidence=0.80)
        kelly = KellyConfig(enabled=False)
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy()],
            multi_config=multi,
            kelly_config=kelly,
        )
        assert engine.multi_config.min_combined_confidence == 0.80
        assert engine._kelly_config.enabled is False

    def test_run_all_strategies(self):
        config = _make_config()
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy("Alpha"), _DummyStrategy("Beta")],
        )
        bars = _make_bars(50)
        results = engine.run_all_strategies(bars)

        assert "Alpha" in results
        assert "Beta" in results
        assert isinstance(results["Alpha"], StrategyBacktestResult)
        assert results["Alpha"].strategy_name == "Alpha"

    def test_run_combined_strategies(self):
        config = _make_config()
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy("Alpha"), _DummyStrategy("Beta")],
        )
        bars = _make_bars(50)
        individual, combined = engine.run_combined_strategies(engine.strategies, bars)

        assert len(individual) == 2
        assert "Alpha" in individual
        # combined should be a BacktestMetrics
        assert combined.total_trades >= 0


# ────────────────────────────────────────────────────────────────────
# Tests: Kelly overlay
# ────────────────────────────────────────────────────────────────────


class TestKellyOverlay:
    def test_kelly_disabled(self):
        """With Kelly disabled, trades should not be skipped by Kelly."""
        config = _make_config()
        kelly = KellyConfig(enabled=False)
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy()],
            kelly_config=kelly,
        )
        assert engine._kelly_config.enabled is False

    def test_kelly_multiplier_no_trades(self):
        """Kelly multiplier should be 0.0 when no closed trades exist."""
        config = _make_config()
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy()],
        )
        mult = engine._compute_kelly_multiplier([])
        assert mult == 0.0

    def test_kelly_multiplier_all_wins(self):
        """Kelly should return a positive multiplier with a healthy win/loss mix."""
        from core.types import SimulatedTrade, TradeOutcome

        config = _make_config()
        engine = MultiStrategyBacktestEngine(
            config=config,
            strategies=[_DummyStrategy()],
        )

        # Create fake trades with a realistic win/loss mix
        def _make_trade(outcome: TradeOutcome, pnl: float) -> SimulatedTrade:
            return SimulatedTrade(
                entry_bar_index=0,
                exit_bar_index=1,
                direction=TradeDirection.LONG,
                entry_price=1.10,
                stop_loss=1.09,
                take_profit_1=1.11,
                take_profit_2=1.12,
                take_profit_3=1.13,
                exit_price=1.11,
                lot_size=0.5,
                risk_amount=50,
                pips=10 if outcome == TradeOutcome.WIN else -10,
                profit_loss=pnl,
                outcome=outcome,
                exit_reason=None,
                entry_time=datetime(2026, 1, 1),
                exit_time=datetime(2026, 1, 1),
                confidence_score=0.7,
                confluence_count=1,
                rationale="test",
            )

        trades = [_make_trade(TradeOutcome.WIN, 50.0) for _ in range(20)]
        trades += [_make_trade(TradeOutcome.LOSS, -30.0) for _ in range(10)]
        mult = engine._compute_kelly_multiplier(trades)
        assert mult > 0.0


# ────────────────────────────────────────────────────────────────────
# Tests: AmalgamationEngine + AmalgamatedBacktestEngine
# ────────────────────────────────────────────────────────────────────
#
# Removed 2026-09-16 (card 6f06c041 rework r1): backtest.amalgamation was
# retired as a dead module (zero callsites). The classes it exported
# (AmalgamatedBacktestEngine, AmalgamationEngine, AmalgamationConfig,
# ComponentExtractor) no longer exist. The tests that exercised them were
# tightly coupled to those symbols; the rest of the file
# (TestMultiStrategyBacktestEngine, TestKellyOverlay) is preserved
# because it tests the still-live backtest.multi_strategy_engine.
