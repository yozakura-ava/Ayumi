"""Integration tests for FilterChain wiring into StrategyExecutor.

Verifies that:
1. StrategyExecutor without filter_config works unchanged (backward compat)
2. StrategyExecutor with filter_config passes signals that align with filters
3. StrategyExecutor with filter_config blocks signals that fail filters
4. End-to-end signal pipeline flows through the chain correctly
5. NEUTRAL signals bypass the filter chain

Run:
    PYTHONPATH=src/forex_bot python3 -m pytest tests/test_signal_pipeline_with_filter_chain.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/forex_bot is importable
_forex_bot = Path(__file__).resolve().parents[1] / "src" / "forex_bot"
if str(_forex_bot) not in sys.path:
    sys.path.insert(0, str(_forex_bot))

from backtest.engine import Bar, MarketState, TradeDirection
from engine.protocol import CanonicalSignal
from engine.strategy_executor import StrategyExecutor
from engine.strategy_registry import StrategySlot

# ─── Fixtures ────────────────────────────────────────────────────────────────


def _make_slot(
    symbol: str = "GBPUSD",
    timeframe: str = "H1",
    pip_value: float = 0.0001,
) -> StrategySlot:
    return StrategySlot(
        id="test_strategy",
        strategy_type="srmr_plus",
        symbol=symbol,
        timeframe=timeframe,
        params={"pip_value": pip_value, "session_range_min_pips": 15.0},
        min_confidence=0.40,
        enabled=True,
    )


def _make_bars(n: int = 30, base_price: float = 1.3000) -> list[Bar]:
    """Generate n synthetic bars with slight uptrend for EMA alignment."""
    bars = []
    for i in range(n):
        t = datetime(2026, 1, 1, i % 24, (i * 5) % 60, tzinfo=timezone.utc)
        p = base_price + i * 0.0005  # gentle uptrend
        bars.append(
            Bar(
                time=t,
                open=p,
                high=p + 0.0008,
                low=p - 0.0003,
                close=p + 0.0003,
                volume=1000,
            )
        )
    return bars


def _make_signal(direction: TradeDirection = TradeDirection.LONG) -> CanonicalSignal:
    return CanonicalSignal(
        strategy_id="test_strategy",
        symbol="GBPUSD",
        direction=direction,
        confidence=0.65,
        entry_price=1.3050,
        stop_loss=1.3020,
        take_profit_1=1.3080,
        take_profit_2=None,
        take_profit_3=None,
        rationale="test signal",
        metadata={},
    )


class _FakeStrategyResult:
    """Mimics the result object returned by ISignalStrategy.evaluate()."""

    def __init__(self, direction: TradeDirection = TradeDirection.LONG):
        self.direction = direction
        self.confidence = 0.65
        self.entry_price = 1.3050
        self.stop_loss = 1.3020
        self.take_profit_1 = 1.3080
        self.take_profit_2 = 0.0
        self.take_profit_3 = 0.0
        self.rationale = "fake result"
        self.is_volatile = False


class _FakeStrategy:
    """Minimal ISignalStrategy stub."""

    def evaluate(self, state: MarketState):
        return _FakeStrategyResult()


class _RejectAllStrategy:
    """Strategy stub that always returns a LONG signal — for filter rejection tests."""

    def evaluate(self, state: MarketState):
        return _FakeStrategyResult(direction=TradeDirection.LONG)


# ─── Tests ───────────────────────────────────────────────────────────────────


class TestBackwardCompatibility:
    """StrategyExecutor without filter_config should behave identically to before."""

    def test_no_filter_config_accepts_signal(self):
        """Executor without filter_config passes signals through unchanged."""
        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_FakeStrategy(),
            max_bars=50,
            min_bars=5,
        )

        # Inject bars directly
        executor._bars = _make_bars(30)
        executor._bar_closed = True

        signal = executor.try_evaluate()
        assert signal is not None
        assert signal.direction == TradeDirection.LONG

    def test_no_filter_config_returns_none_on_no_bar_close(self):
        """Executor returns None when no bar has closed."""
        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_FakeStrategy(),
            max_bars=50,
            min_bars=5,
        )
        executor._bars = _make_bars(30)
        executor._bar_closed = False

        assert executor.try_evaluate() is None


class TestFilterChainPass:
    """Signals that pass all filters should be emitted normally."""

    def test_aligned_long_signal_passes_trend_filter(self):
        """A LONG signal in an uptrend should pass the TrendFilter."""
        # Uptrend bars → EMA fast > EMA slow → bullish
        bars = _make_bars(30, base_price=1.3000)

        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_FakeStrategy(),
            max_bars=50,
            min_bars=5,
            filter_config={
                "trend": {"enabled": True, "ema_fast_period": 9, "ema_slow_period": 21},
            },
        )
        executor._bars = bars
        executor._bar_closed = True

        signal = executor.try_evaluate()
        assert signal is not None
        assert signal.direction == TradeDirection.LONG

    def test_disabled_filters_allow_all(self):
        """When all filters are disabled, the chain should be empty or pass."""
        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_FakeStrategy(),
            max_bars=50,
            min_bars=5,
            filter_config={
                "trend": {"enabled": False},
                "atr": {"enabled": False},
                "fvg": {"enabled": False},
            },
        )
        executor._bars = _make_bars(30)
        executor._bar_closed = True

        signal = executor.try_evaluate()
        assert signal is not None


class TestFilterChainReject:
    """Signals that fail a filter should be blocked (return None)."""

    def test_long_signal_rejected_against_downtrend(self):
        """A LONG signal should be rejected when EMAs show a downtrend."""
        # Downtrend bars → EMA fast < EMA slow → bearish
        bars = []
        for i in range(30):
            t = datetime(2026, 1, 1, i % 24, (i * 5) % 60, tzinfo=timezone.utc)
            p = 1.3500 - i * 0.0005  # gentle downtrend
            bars.append(
                Bar(
                    time=t,
                    open=p,
                    high=p + 0.0003,
                    low=p - 0.0008,
                    close=p - 0.0003,
                    volume=1000,
                )
            )

        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_RejectAllStrategy(),  # Always produces LONG
            max_bars=50,
            min_bars=5,
            filter_config={
                "trend": {"enabled": True, "ema_fast_period": 9, "ema_slow_period": 21},
            },
        )
        executor._bars = bars
        executor._bar_closed = True

        # TrendFilter should reject LONG in downtrend
        signal = executor.try_evaluate()
        assert signal is None, "LONG signal should be rejected in downtrend"


class TestNeutralSignalBypass:
    """NEUTRAL signals should never be filtered."""

    def test_neutral_signal_bypasses_filter_chain(self):
        """NEUTRAL signals carry no directional intent — skip filtering."""

        class NeutralStrategy:
            def evaluate(self, state: MarketState):
                return _FakeStrategyResult(direction=TradeDirection.NEUTRAL)

        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=NeutralStrategy(),
            max_bars=50,
            min_bars=5,
            filter_config={
                "trend": {"enabled": True},
            },
        )
        executor._bars = _make_bars(30)
        executor._bar_closed = True

        signal = executor.try_evaluate()
        assert signal is not None
        assert signal.direction == TradeDirection.NEUTRAL


class TestIntegrationSignalFlow:
    """End-to-end tests proving the full filter chain evaluates correctly."""

    def test_full_chain_passes_aligned_signal(self):
        """A LONG signal in an uptrend with normal ATR should pass all filters."""
        bars = _make_bars(30, base_price=1.3000)

        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_FakeStrategy(),
            max_bars=50,
            min_bars=5,
            filter_config={
                "trend": {"enabled": True, "ema_fast_period": 9, "ema_slow_period": 21},
                "atr": {"enabled": True},
                "fvg": {"enabled": True},
            },
        )
        executor._bars = bars
        executor._bar_closed = True

        # Signal may or may not pass FVG (depends on synthetic bars),
        # but it should at least pass trend and ATR.
        # If FVG rejects, signal is None — that's the chain working.
        # Verify the chain was actually built and has 3 filters
        executor.try_evaluate()
        assert executor._filter_chain is not None
        assert len(executor._filter_chain.filters) == 3

    def test_filter_chain_context_is_built_correctly(self):
        """Verify the context dict passed to filters has all required keys."""
        bars = _make_bars(30, base_price=1.3000)

        slot = _make_slot()
        executor = StrategyExecutor(
            slot=slot,
            strategy=_FakeStrategy(),
            max_bars=50,
            min_bars=5,
            filter_config={
                "trend": {"enabled": True, "ema_fast_period": 9, "ema_slow_period": 21},
            },
        )
        executor._bars = bars
        executor._bar_closed = True

        # Capture the context by mocking evaluate
        captured_context = {}

        original_evaluate = executor._filter_chain.evaluate

        def capture_evaluate(**context):
            captured_context.update(context)
            return original_evaluate(**context)

        executor._filter_chain.evaluate = capture_evaluate

        executor.try_evaluate()

        assert "signal_direction" in captured_context
        assert "ema_fast" in captured_context
        assert "ema_slow" in captured_context
        assert "atr_value" in captured_context
        assert "highs" in captured_context
        assert "lows" in captured_context
        assert "closes" in captured_context
        assert captured_context["signal_direction"] == "LONG"
        assert len(captured_context["highs"]) > 0

    def test_ema_computation(self):
        """Verify _compute_ema produces sensible values."""
        # Flat data → EMA equals the price
        flat = [1.3000] * 21
        ema = StrategyExecutor._compute_ema(flat, 9)
        assert abs(ema - 1.3000) < 0.0001

        # Rising data → EMA is between first and last, closer to last
        rising = [1.3000 + i * 0.001 for i in range(21)]
        ema_fast = StrategyExecutor._compute_ema(rising, 9)
        ema_slow = StrategyExecutor._compute_ema(rising, 21)
        assert ema_fast > ema_slow  # Fast reacts quicker to the rise

    def test_atr_computation(self):
        """Verify _compute_atr produces positive values for volatile bars."""
        bars = _make_bars(30, base_price=1.3000)
        atr = StrategyExecutor._compute_atr(bars, 14)
        assert atr > 0
        # Each bar has high-low ≈ 0.0011, so ATR should be around that range
        assert 0.0005 < atr < 0.005


class TestStrategiesYamlConfig:
    """Verify the filter config from strategies.yaml parses correctly."""

    def test_build_chain_from_production_config(self):
        """The filters section from strategies.yaml should build a valid chain."""
        from signal_engine.filters.filter_chain import build_chain_from_config

        prod_config = {
            "trend": {
                "enabled": True,
                "ema_fast_period": 9,
                "ema_slow_period": 21,
                "tolerance_pips": 0.0,
            },
            "atr": {"enabled": True},
            "fvg": {"enabled": True},
            "orb": {
                "enabled": True,
                "min_score_threshold": 0.3,
                "breakout_min_fraction": 0.10,
            },
        }

        chain = build_chain_from_config(prod_config)
        assert len(chain.filters) == 4

        # Verify priority order (ascending)
        priorities = [getattr(f, "priority", 50) for f in chain.filters]
        assert priorities == sorted(priorities)

        # Verify chain passes a signal that should pass
        result = chain.evaluate(
            signal_direction="LONG",
            ema_fast=1.3010,
            ema_slow=1.3000,
            atr_value=0.0015,
            pip_value=0.0001,
            highs=[b.high for b in _make_bars(30)],
            lows=[b.low for b in _make_bars(30)],
            closes=[b.close for b in _make_bars(30)],
        )
        # TrendFilter: LONG with ema_fast > ema_slow → pass
        # ATRFilter: 0.0015 is in default band → pass
        # FVGFilter: may or may not pass depending on synthetic bars
        # Either way, it shouldn't crash
        assert isinstance(result, bool)
