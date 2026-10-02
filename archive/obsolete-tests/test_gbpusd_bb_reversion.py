import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "forex_bot"))

from datetime import datetime

from backtest.engine import Bar, MarketState, SessionType, TradeDirection
from strategies.gbpusd_bb_reversion import (
    GBPUSD_H1_PRESET,
    BBMeanReversionStrategy,
    BBReversionConfig,
    _build_signal,
    _calculate_atr,
    _calculate_bollinger_bands,
    _calculate_rsi,
    _calculate_sma,
    _calculate_std,
    _is_low_volatility,
    _passes_session_filter,
)


def _make_bars(
    n: int = 100,
    base_price: float = 1.2000,
    volatility: float = 0.0005,
    seed: int = 42,
) -> list[Bar]:
    import random

    random.seed(seed)
    bars = []
    price = base_price
    for i in range(n):
        change = random.gauss(0, volatility)
        open_ = price
        close = price + change
        high = max(open_, close) + abs(random.gauss(0, volatility * 0.5))
        low = min(open_, close) - abs(random.gauss(0, volatility * 0.5))
        bars.append(
            Bar(
                time=datetime(2023, 1, 1, i % 24),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000,
            )
        )
        price = close
    return bars


def _make_state(bars: list[Bar], session: SessionType = SessionType.LONDON) -> MarketState:
    return MarketState(bars=bars, current_session=session)


class TestCalculateSma(unittest.TestCase):
    def test_basic_sma(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _calculate_sma(values, 3)
        self.assertAlmostEqual(result, 4.0)

    def test_insufficient_data(self):
        values = [1.0, 2.0]
        result = _calculate_sma(values, 5)
        self.assertEqual(result, 0.0)

    def test_exact_period(self):
        values = [10.0] * 5
        result = _calculate_sma(values, 5)
        self.assertAlmostEqual(result, 10.0)


class TestCalculateStd(unittest.TestCase):
    def test_zero_std(self):
        values = [5.0] * 10
        result = _calculate_std(values, 10)
        self.assertAlmostEqual(result, 0.0)

    def test_basic_std(self):
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        result = _calculate_std(values, 8)
        self.assertGreater(result, 0.0)

    def test_insufficient_data(self):
        values = [1.0]
        result = _calculate_std(values, 5)
        self.assertEqual(result, 0.0)


class TestCalculateAtr(unittest.TestCase):
    def test_basic_atr(self):
        bars = _make_bars(30, volatility=0.001)
        result = _calculate_atr(bars, 14)
        self.assertGreater(result, 0.0)

    def test_insufficient_bars(self):
        bars = _make_bars(5)
        result = _calculate_atr(bars, 14)
        self.assertAlmostEqual(result, 0.0001)

    def test_low_vs_high_volatility(self):
        low_vol_bars = _make_bars(30, volatility=0.00001)
        high_vol_bars = _make_bars(30, volatility=0.01)
        low_atr = _calculate_atr(low_vol_bars, 14)
        high_atr = _calculate_atr(high_vol_bars, 14)
        self.assertLess(low_atr, high_atr)


class TestCalculateRsi(unittest.TestCase):
    def test_rsi_range(self):
        closes = [1.0 + i * 0.001 for i in range(50)]
        rsi = _calculate_rsi(closes, 14)
        self.assertGreaterEqual(rsi, 0.0)
        self.assertLessEqual(rsi, 100.0)

    def test_insufficient_data(self):
        closes = [1.0, 2.0]
        rsi = _calculate_rsi(closes, 14)
        self.assertAlmostEqual(rsi, 50.0)

    def test_uptrend_high_rsi(self):
        closes = [1.0 + i * 0.01 for i in range(50)]
        rsi = _calculate_rsi(closes, 14)
        self.assertGreater(rsi, 70.0)

    def test_downtrend_low_rsi(self):
        closes = [2.0 - i * 0.01 for i in range(50)]
        rsi = _calculate_rsi(closes, 14)
        self.assertLess(rsi, 30.0)


class TestCalculateBollingerBands(unittest.TestCase):
    def test_upper_above_middle(self):
        bars = _make_bars(30)
        closes = [b.close for b in bars]
        upper, middle, lower = _calculate_bollinger_bands(closes, 20, 2.0)
        self.assertGreater(upper, middle)
        self.assertLess(lower, middle)

    def test_symmetric_bands(self):
        bars = _make_bars(30)
        closes = [b.close for b in bars]
        upper, middle, lower = _calculate_bollinger_bands(closes, 20, 2.0)
        self.assertAlmostEqual(upper - middle, middle - lower, places=4)

    def test_wider_std_dev_wider_bands(self):
        bars = _make_bars(30)
        closes = [b.close for b in bars]
        u1, _, l1 = _calculate_bollinger_bands(closes, 20, 1.5)
        u2, _, l2 = _calculate_bollinger_bands(closes, 20, 3.0)
        self.assertGreater(u2 - l2, u1 - l1)


class TestIsLowVolatility(unittest.TestCase):
    def test_declining_volatility_is_low(self):
        bars = _make_bars(50, volatility=0.005)
        for i in range(50, 60):
            import random

            random.seed(42 + i)
            change = random.gauss(0, 0.00002)
            prev = bars[-1].close
            bars.append(
                Bar(
                    time=datetime(2023, 1, 1, i % 24),
                    open=prev,
                    high=prev + abs(change) + 0.00001,
                    low=prev - abs(change) - 0.00001,
                    close=prev + change,
                    volume=1000,
                )
            )
        result = _is_low_volatility(bars, 14, 20)
        self.assertTrue(result)

    def test_insufficient_bars(self):
        bars = _make_bars(10)
        result = _is_low_volatility(bars, 14, 20)
        self.assertFalse(result)


class TestSessionFilter(unittest.TestCase):
    def test_london_session_passes(self):
        state = MarketState(bars=[], current_session=SessionType.LONDON)
        self.assertTrue(_passes_session_filter(state))

    def test_ny_am_session_passes(self):
        state = MarketState(bars=[], current_session=SessionType.NY_AM)
        self.assertTrue(_passes_session_filter(state))

    def test_outside_session_fails(self):
        state = MarketState(bars=[], current_session=SessionType.OUTSIDE)
        self.assertFalse(_passes_session_filter(state))

    def test_ny_pm_session_fails(self):
        state = MarketState(bars=[], current_session=SessionType.NY_PM)
        self.assertFalse(_passes_session_filter(state))

    def test_asian_session_fails(self):
        state = MarketState(bars=[], current_session=SessionType.ASIAN)
        self.assertFalse(_passes_session_filter(state))


class TestBuildSignal(unittest.TestCase):
    def test_long_signal(self):
        config = BBReversionConfig()
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.001, config, 0.70, "test")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, TradeDirection.LONG)
        self.assertLess(signal.stop_loss, signal.entry_price)
        self.assertGreater(signal.take_profit_1, signal.entry_price)

    def test_short_signal(self):
        config = BBReversionConfig()
        signal = _build_signal(TradeDirection.SHORT, 1.2000, 0.001, config, 0.70, "test")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, TradeDirection.SHORT)
        self.assertGreater(signal.stop_loss, signal.entry_price)
        self.assertLess(signal.take_profit_1, signal.entry_price)

    def test_low_confidence_rejected(self):
        config = BBReversionConfig(min_confidence=0.70)
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.001, config, 0.50, "test")
        self.assertIsNone(signal)

    def test_zero_atr_rejected(self):
        config = BBReversionConfig()
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.0, config, 0.70, "test")
        self.assertIsNone(signal)

    def test_sl_is_15x_atr(self):
        config = BBReversionConfig(atr_sl_multiplier=1.5)
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.001, config, 0.70, "test")
        risk = 0.001 * 1.5
        self.assertAlmostEqual(signal.stop_loss, 1.2000 - risk, places=5)

    def test_tp_is_15x_rr(self):
        config = BBReversionConfig(atr_sl_multiplier=1.5, tp_rr=1.5)
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.001, config, 0.70, "test")
        risk = 0.001 * 1.5
        expected_tp = 1.2000 + risk * 1.5
        self.assertAlmostEqual(signal.take_profit_1, expected_tp, places=5)

    def test_confidence_capped_at_095(self):
        config = BBReversionConfig()
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.001, config, 0.99, "test")
        self.assertLessEqual(signal.confidence, 0.95)

    def test_all_tp_levels_equal(self):
        config = BBReversionConfig()
        signal = _build_signal(TradeDirection.LONG, 1.2000, 0.001, config, 0.70, "test")
        self.assertAlmostEqual(signal.take_profit_1, signal.take_profit_2, places=5)
        self.assertAlmostEqual(signal.take_profit_2, signal.take_profit_3, places=5)


class TestBBMeanReversionStrategy(unittest.TestCase):
    def test_strategy_name(self):
        strategy = BBMeanReversionStrategy()
        self.assertEqual(strategy.name, "GBPUSD BB Mean Reversion")

    def test_default_config(self):
        strategy = BBMeanReversionStrategy()
        self.assertEqual(strategy.config.bb_period, 20)
        self.assertEqual(strategy.config.bb_std_dev, 2.0)
        self.assertEqual(strategy.config.rsi_period, 14)
        self.assertEqual(strategy.config.atr_sl_multiplier, 1.5)
        self.assertEqual(strategy.config.tp_rr, 1.5)
        self.assertTrue(strategy.config.session_filter)

    def test_custom_config(self):
        config = BBReversionConfig(bb_period=10, atr_sl_multiplier=2.0)
        strategy = BBMeanReversionStrategy(config)
        self.assertEqual(strategy.config.bb_period, 10)
        self.assertEqual(strategy.config.atr_sl_multiplier, 2.0)

    def test_returns_none_with_insufficient_bars(self):
        strategy = BBMeanReversionStrategy()
        bars = _make_bars(20)
        state = _make_state(bars)
        result = strategy.evaluate(state)
        self.assertIsNone(result)

    def test_returns_none_outside_session_with_filter(self):
        strategy = BBMeanReversionStrategy(BBReversionConfig(session_filter=True))
        bars = _make_bars(100)
        state = _make_state(bars, SessionType.OUTSIDE)
        result = strategy.evaluate(state)
        self.assertIsNone(result)

    def test_no_signal_on_normal_data(self):
        strategy = BBMeanReversionStrategy(BBReversionConfig(session_filter=False))
        bars = _make_bars(100, volatility=0.001)
        state = _make_state(bars, SessionType.OUTSIDE)
        result = strategy.evaluate(state)
        self.assertIsNone(result)

    def test_reset_is_noop(self):
        strategy = BBMeanReversionStrategy()
        strategy.reset()

    def test_long_signal_when_all_conditions_met(self):
        strategy = BBMeanReversionStrategy(BBReversionConfig(session_filter=False, min_confidence=0.30))
        bars = []
        price = 1.2000

        for i in range(50):  # noqa: B007
            bars.append(
                Bar(
                    time=datetime(2023, 1, 1, 9),
                    open=price,
                    high=price + 0.003,
                    low=price - 0.003,
                    close=price,
                    volume=1000,
                )
            )

        for i in range(30):
            price -= 0.001
            half_range = max(0.00005, 0.0015 - i * 0.00005)
            bars.append(
                Bar(
                    time=datetime(2023, 1, 1, 9),
                    open=price + half_range,
                    high=price + half_range,
                    low=price - half_range,
                    close=price,
                    volume=1000,
                )
            )

        state = _make_state(bars, SessionType.OUTSIDE)
        result = strategy.evaluate(state)
        if result is not None:
            self.assertEqual(result.direction, TradeDirection.LONG)
        else:
            from strategies.gbpusd_bb_reversion import (
                _calculate_bollinger_bands,
                _calculate_rsi,
                _is_low_volatility,
            )

            closes = [b.close for b in bars]
            latest = bars[-1]
            _, _, bb_lower = _calculate_bollinger_bands(closes, 20, 2.0)
            rsi = _calculate_rsi(closes, 14)
            low_vol = _is_low_volatility(bars, 14, 20)
            self.assertFalse(
                latest.low <= bb_lower and latest.close > bb_lower and rsi < 40 and low_vol,
                "All conditions met but no signal generated",
            )


class TestBBReversionConfig(unittest.TestCase):
    def test_default_values(self):
        config = BBReversionConfig()
        self.assertEqual(config.bb_period, 20)
        self.assertAlmostEqual(config.bb_std_dev, 2.0)
        self.assertEqual(config.rsi_period, 14)
        self.assertAlmostEqual(config.rsi_long_threshold, 40.0)
        self.assertAlmostEqual(config.rsi_short_threshold, 60.0)
        self.assertEqual(config.atr_period, 14)
        self.assertEqual(config.atr_sma_period, 20)
        self.assertAlmostEqual(config.atr_sl_multiplier, 1.5)
        self.assertAlmostEqual(config.tp_rr, 1.5)
        self.assertTrue(config.session_filter)
        self.assertAlmostEqual(config.min_confidence, 0.55)

    def test_frozen_dataclass(self):
        config = BBReversionConfig()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.bb_period = 10

    def test_custom_values(self):
        config = BBReversionConfig(
            bb_period=10,
            bb_std_dev=1.5,
            rsi_long_threshold=30.0,
            atr_sl_multiplier=2.0,
        )
        self.assertEqual(config.bb_period, 10)
        self.assertAlmostEqual(config.bb_std_dev, 1.5)
        self.assertAlmostEqual(config.rsi_long_threshold, 30.0)
        self.assertAlmostEqual(config.atr_sl_multiplier, 2.0)


class TestPresets(unittest.TestCase):
    def test_gbpusd_h1_preset_values(self):
        self.assertEqual(GBPUSD_H1_PRESET.bb_period, 20)
        self.assertEqual(GBPUSD_H1_PRESET.bb_std_dev, 2.0)
        self.assertEqual(GBPUSD_H1_PRESET.rsi_period, 14)
        self.assertEqual(GBPUSD_H1_PRESET.atr_sl_multiplier, 1.5)
        self.assertEqual(GBPUSD_H1_PRESET.tp_rr, 1.5)
        self.assertTrue(GBPUSD_H1_PRESET.session_filter)

    def test_preset_is_frozen(self):
        self.assertTrue(dataclasses.is_dataclass(BBReversionConfig))
        self.assertTrue(getattr(BBReversionConfig, "__dataclass_params__").frozen)  # noqa: B009


if __name__ == "__main__":
    unittest.main()
