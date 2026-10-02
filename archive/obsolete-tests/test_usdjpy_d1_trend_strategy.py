import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "forex_bot"))

from backtest.engine import Bar, MarketState, TradeDirection
from strategies.usdjpy_d1_trend import USDJPYD1TrendStrategy


def make_usdjpy_bars(n=300, seed=42, trend="up"):
    import numpy as np
    import pandas as pd

    np.random.seed(seed)
    dates = pd.date_range("2022-01-01", periods=n, freq="1D")
    dates = [d for d in dates if d.weekday() != 4]

    price = 130.0
    prices = []
    for i in range(len(dates)):  # noqa: B007
        drift = 0.05 if trend == "up" else -0.05
        price += drift + np.random.normal(0, 0.3)
        prices.append(price)

    bars = []
    for i, dt in enumerate(dates):
        spread = 0.03
        bars.append(
            Bar(
                time=dt.to_pydatetime(),
                open=prices[i] - spread * np.random.uniform(0, 1),
                high=prices[i] + spread * np.random.uniform(1, 3),
                low=prices[i] - spread * np.random.uniform(1, 3),
                close=prices[i],
                volume=10000,
            )
        )
    return bars


def make_friday_bar(base_time=None):
    from datetime import datetime, timedelta

    if base_time is None:
        base_time = datetime(2023, 6, 9)
    dt = base_time
    while dt.weekday() != 4:
        dt += timedelta(days=1)
    return Bar(
        time=dt,
        open=130.0,
        high=130.5,
        low=129.5,
        close=130.2,
        volume=10000,
    )


class TestUSDJPYD1TrendStrategy(unittest.TestCase):
    def test_strategy_name(self):
        strategy = USDJPYD1TrendStrategy()
        self.assertEqual(strategy.name, "USDJPY D1 Trend-Following")

    def test_returns_none_with_insufficient_bars(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(50)
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        self.assertIsNone(result)

    def test_returns_none_when_adx_below_threshold(self):
        strategy = USDJPYD1TrendStrategy(adx_threshold=50.0)
        bars = make_usdjpy_bars(300, trend="up")
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        self.assertIsNone(result)

    def test_skips_friday_bars(self):
        strategy = USDJPYD1TrendStrategy()
        friday_bar = make_friday_bar()
        bars = make_usdjpy_bars(250, trend="up")
        bars.append(friday_bar)
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        self.assertIsNone(result)

    def test_friday_detection(self):
        from datetime import datetime

        strategy = USDJPYD1TrendStrategy()
        friday = datetime(2023, 6, 9, 12, 0)
        monday = datetime(2023, 6, 5, 12, 0)
        self.assertTrue(strategy._is_friday(friday))
        self.assertFalse(strategy._is_friday(monday))

    def test_bullish_signal_structure(self):
        strategy = USDJPYD1TrendStrategy(
            fast_ema_period=20,
            slow_ema_period=50,
            adx_threshold=15.0,
        )
        bars = make_usdjpy_bars(200, seed=7, trend="up")
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        if result is not None:
            self.assertEqual(result.direction, TradeDirection.LONG)
            self.assertGreater(result.entry_price, 0)
            self.assertGreater(result.stop_loss, 0)
            self.assertGreater(result.take_profit_1, result.entry_price)
            self.assertGreater(result.take_profit_2, result.take_profit_1)
            self.assertGreater(result.take_profit_3, result.take_profit_2)
            self.assertLess(result.stop_loss, result.entry_price)
            self.assertIn("EMA", result.rationale)
            self.assertIn("ADX", result.rationale)
            self.assertIn("RSI", result.rationale)

    def test_bearish_signal_structure(self):
        strategy = USDJPYD1TrendStrategy(
            fast_ema_period=20,
            slow_ema_period=50,
            adx_threshold=15.0,
        )
        bars = make_usdjpy_bars(200, seed=7, trend="down")
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        if result is not None:
            self.assertEqual(result.direction, TradeDirection.SHORT)
            self.assertGreater(result.entry_price, 0)
            self.assertGreater(result.stop_loss, 0)
            self.assertLess(result.take_profit_1, result.entry_price)
            self.assertGreater(result.stop_loss, result.entry_price)

    def test_tp_levels_use_rr_ratio(self):
        strategy = USDJPYD1TrendStrategy(
            fast_ema_period=20,
            slow_ema_period=50,
            adx_threshold=15.0,
            tp_rr_ratio=2.0,
        )
        bars = make_usdjpy_bars(200, seed=7, trend="up")
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        if result is not None:
            risk = abs(result.entry_price - result.stop_loss)
            self.assertAlmostEqual(result.take_profit_1, result.entry_price + risk * 2.0, places=3)
            self.assertAlmostEqual(result.take_profit_2, result.entry_price + risk * 3.0, places=3)
            self.assertAlmostEqual(result.take_profit_3, result.entry_price + risk * 4.0, places=3)

    def test_confidence_bounds(self):
        strategy = USDJPYD1TrendStrategy(
            fast_ema_period=20,
            slow_ema_period=50,
            adx_threshold=15.0,
        )
        bars = make_usdjpy_bars(200, seed=7, trend="up")
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        if result is not None:
            self.assertGreaterEqual(result.confidence, 0.0)
            self.assertLessEqual(result.confidence, 0.90)

    def test_custom_parameters(self):
        strategy = USDJPYD1TrendStrategy(
            fast_ema_period=30,
            slow_ema_period=100,
            adx_period=20,
            adx_threshold=30.0,
            rsi_period=10,
            atr_period=20,
            sl_atr_multiplier=1.5,
            tp_rr_ratio=3.0,
        )
        self.assertEqual(strategy.fast_ema_period, 30)
        self.assertEqual(strategy.slow_ema_period, 100)
        self.assertEqual(strategy.adx_period, 20)
        self.assertEqual(strategy.adx_threshold, 30.0)
        self.assertEqual(strategy.rsi_period, 10)
        self.assertEqual(strategy.atr_period, 20)
        self.assertEqual(strategy.sl_atr_multiplier, 1.5)
        self.assertEqual(strategy.tp_rr_ratio, 3.0)

    def test_atr_calculation(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(50)
        atr = strategy._calculate_atr(bars)
        self.assertGreater(atr, 0)

    def test_rsi_calculation(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(50)
        rsi = strategy._calculate_rsi(bars)
        self.assertIsNotNone(rsi)
        self.assertGreaterEqual(rsi, 0)
        self.assertLessEqual(rsi, 100)

    def test_ema_calculation(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(300)
        ema50 = strategy._calculate_ema(bars, 50)
        ema200 = strategy._calculate_ema(bars, 200)
        self.assertGreater(ema50, 0)
        self.assertGreater(ema200, 0)

    def test_ema_returns_zero_for_insufficient_bars(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(10)
        ema = strategy._calculate_ema(bars, 200)
        self.assertEqual(ema, 0.0)

    def test_adx_returns_none_for_insufficient_bars(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(5)
        adx = strategy._calculate_adx(bars)
        self.assertIsNone(adx)

    def test_rsi_returns_none_for_insufficient_bars(self):
        strategy = USDJPYD1TrendStrategy()
        bars = make_usdjpy_bars(5)
        rsi = strategy._calculate_rsi(bars)
        self.assertIsNone(rsi)

    def test_no_signal_when_ema_flat(self):
        import numpy as np

        strategy = USDJPYD1TrendStrategy()
        bars = []
        price = 130.0
        for i in range(300):
            dt = __import__("pandas").date_range("2022-01-01", periods=300, freq="1D")[i]
            if dt.weekday() == 4:
                continue
            bars.append(
                Bar(
                    time=dt.to_pydatetime(),
                    open=price + np.random.normal(0, 0.01),
                    high=price + 0.1,
                    low=price - 0.1,
                    close=price,
                    volume=10000,
                )
            )
        state = MarketState(bars=bars)
        result = strategy.evaluate(state)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
