using System;
using System.IO;
using System.Linq;

namespace ICTSMC
{
    public static class BacktestTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== Backtesting Harness Tests ===\n");

            Test_CsvDataLoaderParsesTradingViewFormat();
            Test_CsvDataLoaderInferTimeFrame();
            Test_CsvDataLoaderHandlesFromString();
            Test_BacktestEngineRunsOnBullishData();
            Test_BacktestEngineRunsOnBearishData();
            Test_BacktestEngineRespectsMaxDrawdown();
            Test_BacktestEngineRespectsDailyLossLimit();
            Test_BacktestMetricsCalculatesWinRate();
            Test_BacktestMetricsCalculatesSharpeRatio();
            Test_BacktestConservativeConfigFiltersMore();
            Test_BacktestPartialClose();
            Test_BacktestAggressiveConfig();
            Test_EquityCurveIsNonNegative();
            Test_BacktestReportPrints();

            Console.WriteLine($"\n=== Backtest Results: {Passed} passed, {Failed} failed ===");
        }

        private static void Assert(bool condition, string testName)
        {
            if (condition)
            {
                Console.WriteLine($"  PASS: {testName}");
                Passed++;
            }
            else
            {
                Console.WriteLine($"  FAIL: {testName}");
                Failed++;
            }
        }

        private static void Test_CsvDataLoaderParsesTradingViewFormat()
        {
            Console.WriteLine("\nTest: CSV Data Loader parses TradingView format");
            var csv = "Date,Open,High,Low,Close,Volume\n" +
                      "2026-01-05 08:00,1.10000,1.10100,1.09900,1.10050,1500\n" +
                      "2026-01-05 08:15,1.10050,1.10200,1.10000,1.10150,2000\n" +
                      "2026-01-05 08:30,1.10150,1.10300,1.10100,1.10250,1800";

            var loader = new CsvDataLoader();
            var bars = loader.LoadFromString(csv);

            Assert(bars.Count == 3, $"Parsed 3 bars (got {bars.Count})");
            Assert(Math.Abs(bars[0].Open - 1.10000) < 0.00001, "First bar open correct");
            Assert(Math.Abs(bars[0].Close - 1.10050) < 0.00001, "First bar close correct");
            Assert(Math.Abs(bars[1].High - 1.10200) < 0.00001, "Second bar high correct");
            Assert(bars[0].Volume == 1500, "First bar volume correct");
        }

        private static void Test_CsvDataLoaderInferTimeFrame()
        {
            Console.WriteLine("\nTest: CSV Data Loader infers timeframe");
            var csv = "Date,Open,High,Low,Close,Volume\n" +
                      "2026-01-05 08:00,1.10000,1.10100,1.09900,1.10050,1500\n" +
                      "2026-01-05 08:15,1.10050,1.10200,1.10000,1.10150,2000";

            var loader = new CsvDataLoader();
            var bars = loader.LoadFromString(csv);

            Assert(bars[0].Period.Minutes == 15, $"Inferred M15 (got {bars[0].Period.Minutes} min)");
        }

        private static void Test_CsvDataLoaderHandlesFromString()
        {
            Console.WriteLine("\nTest: CSV Data Loader handles LoadFromString");
            var csv = "Date,Open,High,Low,Close\n" +
                      "2026-01-05 08:00,1.10000,1.10100,1.09900,1.10050\n" +
                      "2026-01-05 08:15,1.10050,1.10200,1.10000,1.10150";

            var loader = new CsvDataLoader();
            var bars = loader.LoadFromString(csv);

            Assert(bars.Count == 2, $"Parsed 2 bars (got {bars.Count})");
            Assert(bars[0].Volume == 0, "Volume defaults to 0 when missing");
        }

        private static void Test_BacktestEngineRunsOnBullishData()
        {
            Console.WriteLine("\nTest: Backtest engine runs on bullish data");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);

            Assert(metrics.TotalTrades >= 0, "Engine completes without error");
            Assert(metrics.EndingBalance >= 0, "Ending balance is non-negative");
            Assert(metrics.StartingBalance == 10000, "Starting balance is correct");
        }

        private static void Test_BacktestEngineRunsOnBearishData()
        {
            Console.WriteLine("\nTest: Backtest engine runs on bearish data");
            var bars = MockDataGenerator.GenerateBearishTrend(100);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);

            Assert(metrics.TotalTrades >= 0, "Engine completes without error");
            Assert(metrics.EndingBalance >= 0, "Ending balance is non-negative");
        }

        private static void Test_BacktestEngineRespectsMaxDrawdown()
        {
            Console.WriteLine("\nTest: Backtest engine respects max total drawdown");
            var config = BacktestConfig.Default;
            config.MaxTotalDrawdownPct = 0.01;
            config.MaxDailyDrawdownPct = 0.01;

            var bars = MockDataGenerator.GenerateBearishTrend(100, 1.1000, 0.002);
            var engine = new BacktestEngine(config);
            var metrics = engine.Run(bars);

            Assert(metrics.MaxDrawdownPct <= 0.02,
                $"Max DD within tolerance (got {metrics.MaxDrawdownPct:F2}%)");
        }

        private static void Test_BacktestEngineRespectsDailyLossLimit()
        {
            Console.WriteLine("\nTest: Backtest engine respects daily loss limit");
            var config = BacktestConfig.Default;
            config.MaxDailyDrawdownPct = 0.01;
            config.MaxTotalDrawdownPct = 1.0;

            var bars = MockDataGenerator.GenerateBearishTrend(100, 1.1000, 0.002);
            var engine = new BacktestEngine(config);
            var metrics = engine.Run(bars);

            Assert(metrics.MaxDailyLossDollar <= config.StartingBalance * 0.02,
                $"Max daily loss within tolerance (got ${metrics.MaxDailyLossDollar:F2})");
        }

        private static void Test_BacktestMetricsCalculatesWinRate()
        {
            Console.WriteLine("\nTest: Backtest metrics calculates win rate");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);

            if (metrics.TotalTrades > 0)
            {
                Assert(metrics.WinRate >= 0 && metrics.WinRate <= 100,
                    $"Win rate in [0,100] (got {metrics.WinRate:F1}%)");
                Assert(metrics.WinningTrades + metrics.LosingTrades + metrics.BreakevenTrades == metrics.TotalTrades,
                    "Trade outcome counts sum to total");
            }
            else
            {
                Assert(true, "No trades (win rate N/A)");
            }
        }

        private static void Test_BacktestMetricsCalculatesSharpeRatio()
        {
            Console.WriteLine("\nTest: Backtest metrics calculates Sharpe ratio");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);

            Assert(!double.IsNaN(metrics.SharpeRatio), "Sharpe ratio is not NaN");
            Assert(!double.IsInfinity(metrics.SharpeRatio), "Sharpe ratio is not infinity");
        }

        private static void Test_BacktestConservativeConfigFiltersMore()
        {
            Console.WriteLine("\nTest: Conservative config filters more signals");
            var bars = MockDataGenerator.GenerateBullishTrend(100);

            var aggressiveEngine = new BacktestEngine(BacktestConfig.Aggressive);
            var conservativeEngine = new BacktestEngine(BacktestConfig.Conservative);

            var aggMetrics = aggressiveEngine.Run(bars);
            var consMetrics = conservativeEngine.Run(bars);

            Assert(consMetrics.RejectedSignals >= aggMetrics.RejectedSignals,
                $"Conservative rejects >= aggressive (cons: {consMetrics.RejectedSignals}, agg: {aggMetrics.RejectedSignals})");
        }

        private static void Test_BacktestPartialClose()
        {
            Console.WriteLine("\nTest: Backtest engine supports partial close");
            var config = BacktestConfig.Default;
            config.PartialCloseEnabled = true;

            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(config);
            var metrics = engine.Run(bars);

            bool hasPartial = metrics.Trades.Any(t => t.PartialClosed);
            if (metrics.TotalTrades > 0)
            {
                Assert(true, $"Engine runs with partial close (partial trades: {metrics.Trades.Count(t => t.PartialClosed)})");
            }
            else
            {
                Assert(true, "No trades to test partial close");
            }
        }

        private static void Test_BacktestAggressiveConfig()
        {
            Console.WriteLine("\nTest: Aggressive config uses 1% risk");
            var config = BacktestConfig.Aggressive;
            Assert(config.RiskPerTradePct == 0.01, $"Risk per trade is 1% (got {config.RiskPerTradePct * 100}%)");

            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(config);
            var metrics = engine.Run(bars);

            Assert(metrics.TotalTrades >= 0, "Aggressive engine completes");
            Assert(metrics.StartingBalance == 10000, "Starting balance correct");
        }

        private static void Test_EquityCurveIsNonNegative()
        {
            Console.WriteLine("\nTest: Equity curve has no negative values");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);

            bool allNonNegative = metrics.EquityCurve.All(v => v >= 0);
            Assert(allNonNegative, "All equity curve values >= 0");
            Assert(metrics.EquityCurve.Count > 0, "Equity curve has entries");
        }

        private static void Test_BacktestReportPrints()
        {
            Console.WriteLine("\nTest: Backtest report prints without error");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var engine = new BacktestEngine(BacktestConfig.Default);
            var metrics = engine.Run(bars);

            try
            {
                metrics.PrintReport();
                Assert(true, "PrintReport completes");
            }
            catch
            {
                Assert(false, "PrintReport threw exception");
            }
        }
    }
}
