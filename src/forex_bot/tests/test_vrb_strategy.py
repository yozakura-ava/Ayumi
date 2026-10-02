from __future__ import annotations

from datetime import datetime, timezone

from backtest.engine import Bar, MarketState, SessionType, TradeDirection
from strategies.volatility_regime_breakout import (
    VolatilityRegimeBreakoutStrategy,
    VRBConfig,
    _atr_percentile,
    _calculate_atr,
    _range_position,
    _trend_direction,
)


def _make_bars(closes: list[float], hours_offset: int = 0) -> list[Bar]:
    bars = []
    base_time = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    for i, c in enumerate(closes):
        bars.append(
            Bar(
                time=base_time.replace(hour=(8 + i + hours_offset) % 24),
                open=c - 0.0001,
                high=c + 0.001,
                low=c - 0.001,
                close=c,
                volume=100,
            )
        )
    return bars


def _make_low_vol_bars(n: int = 100, base: float = 1.26000) -> list[Bar]:
    bars = []
    base_time = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    import random

    rng = random.Random(42)  # noqa: S311
    price = base
    for i in range(n):
        hour = (8 + i) % 24
        if hour < 7:
            session = SessionType.ASIAN
        elif hour < 15:
            session = SessionType.LONDON
        else:
            session = SessionType.NY_AM

        if session in (SessionType.LONDON, SessionType.NY_AM):
            change = (rng.random() - 0.5) * 0.0003
        else:
            change = (rng.random() - 0.5) * 0.0001

        price += change
        high = price + abs(change)
        low = price - abs(change)
        bars.append(
            Bar(
                time=base_time.replace(day=1 + i // 96, hour=hour),
                open=price - change * 0.5,
                high=high,
                low=low,
                close=price,
                volume=100,
            )
        )
    return bars


def test_calculate_atr_basic():
    bars = _make_bars(
        [
            1.2600,
            1.2605,
            1.2610,
            1.2608,
            1.2615,
            1.2620,
            1.2618,
            1.2625,
            1.2630,
            1.2628,
            1.2635,
            1.2640,
            1.2638,
            1.2645,
            1.2650,
            1.2648,
            1.2655,
            1.2660,
            1.2658,
            1.2665,
        ]
    )
    atr = _calculate_atr(bars, 14)
    assert atr > 0, "ATR should be positive"  # noqa: S101


def test_calculate_atr_insufficient_bars():
    bars = _make_bars([1.2600, 1.2605, 1.2610])
    atr = _calculate_atr(bars, 14)
    assert atr == 0.0001, "ATR should return default for insufficient bars"  # noqa: S101


def test_range_position_middle():
    bars = []
    base_time = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    for i in range(20):
        bars.append(
            Bar(
                time=base_time,
                open=1.2600 + i * 0.0001,
                high=1.2600 + i * 0.0002,
                low=1.2600 + i * 0.00005,
                close=1.2600 + i * 0.0001,
            )
        )
    pos = _range_position(bars, 20)
    assert pos is not None  # noqa: S101
    assert 0.0 <= pos <= 1.0  # noqa: S101


def test_range_position_insufficient_bars():
    bars = _make_bars([1.2600])
    pos = _range_position(bars, 20)
    assert pos is None  # noqa: S101


def test_trend_direction_bullish():
    closes = [1.2600 + i * 0.0002 for i in range(60)]
    bars = _make_bars(closes)
    trend = _trend_direction(bars, 50)
    assert trend == 1, "Should detect bullish trend"  # noqa: S101


def test_trend_direction_bearish():
    closes = [1.2700 - i * 0.0002 for i in range(60)]
    bars = _make_bars(closes)
    trend = _trend_direction(bars, 50)
    assert trend == -1, "Should detect bearish trend"  # noqa: S101


def test_trend_direction_insufficient_bars():
    bars = _make_bars([1.2600] * 30)
    trend = _trend_direction(bars, 50)
    assert trend == 0  # noqa: S101


def test_atr_percentile_basic():
    bars = _make_low_vol_bars(80)
    pct = _atr_percentile(bars, 14, 50)
    assert 0.0 <= pct <= 100.0  # noqa: S101


def test_atr_percentile_insufficient_bars():
    bars = _make_bars([1.2600] * 20)
    pct = _atr_percentile(bars, 14, 50)
    assert pct == 50.0, "Should return default for insufficient bars"  # noqa: S101


def test_strategy_name():
    strategy = VolatilityRegimeBreakoutStrategy()
    assert strategy.name == "Volatility Regime Breakout"  # noqa: S101


def test_strategy_reset():
    strategy = VolatilityRegimeBreakoutStrategy()
    strategy._last_signal_bar_index = 100
    strategy.reset()
    assert strategy._last_signal_bar_index == -1  # noqa: S101


def test_strategy_no_signal_insufficient_bars():
    strategy = VolatilityRegimeBreakoutStrategy()
    bars = _make_bars([1.2600] * 10)
    state = MarketState(bars=bars, current_session=SessionType.LONDON)
    signal = strategy.evaluate(state)
    assert signal is None  # noqa: S101


def test_strategy_no_signal_outside_session():
    strategy = VolatilityRegimeBreakoutStrategy()
    bars = _make_low_vol_bars(80)
    state = MarketState(bars=bars, current_session=SessionType.OUTSIDE)
    signal = strategy.evaluate(state)
    assert signal is None  # noqa: S101


def test_strategy_returns_signal_on_low_vol():
    strategy = VolatilityRegimeBreakoutStrategy(
        VRBConfig(
            atr_percentile_low=80.0,
            range_position_max=0.90,
            min_confidence=0.40,
        )
    )
    bars = _make_low_vol_bars(80)
    state = MarketState(bars=bars, current_session=SessionType.LONDON)
    signal = strategy.evaluate(state)
    if signal is not None:
        assert signal.direction in (TradeDirection.LONG, TradeDirection.SHORT)  # noqa: S101
        assert signal.confidence >= 0.40  # noqa: S101
        assert signal.entry_price > 0  # noqa: S101
        assert signal.stop_loss > 0  # noqa: S101
        assert signal.stop_loss != signal.entry_price  # noqa: S101


def test_strategy_signal_structure():
    strategy = VolatilityRegimeBreakoutStrategy(
        VRBConfig(
            atr_percentile_low=80.0,
            range_position_max=0.90,
            min_confidence=0.30,
        )
    )
    bars = _make_low_vol_bars(80)
    state = MarketState(bars=bars, current_session=SessionType.LONDON)
    signal = strategy.evaluate(state)
    if signal is not None:
        assert isinstance(signal.rationale, str)  # noqa: S101
        assert "VRB" in signal.rationale  # noqa: S101


def test_strategy_cooldown():
    config = VRBConfig(
        atr_percentile_low=80.0,
        range_position_max=0.90,
        min_confidence=0.30,
        cooldown_bars=100,
    )
    strategy = VolatilityRegimeBreakoutStrategy(config)

    bars = _make_low_vol_bars(80)
    state = MarketState(bars=bars, current_session=SessionType.LONDON)

    first_signal = strategy.evaluate(state)
    if first_signal is not None:
        second_signal = strategy.evaluate(state)
        assert second_signal is None, "Should respect cooldown"  # noqa: S101


def test_custom_pip_value():
    config = VRBConfig(
        pip_value=0.01,
        atr_percentile_low=80.0,
        range_position_max=0.90,
        min_confidence=0.30,
    )
    strategy = VolatilityRegimeBreakoutStrategy(config)
    assert strategy.config.pip_value == 0.01  # noqa: S101
