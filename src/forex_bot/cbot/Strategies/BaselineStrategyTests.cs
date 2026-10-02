using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class BaselineStrategyTests
    {
        public static void RunAllTests()
        {
            Console.WriteLine("=== Baseline Strategy Tests ===\n");
            TestMACrossStrategy();
            TestBBStrategy();
            TestRSIStrategy();
            TestSRBreakoutStrategy();
            TestROCMStrategy();
            TestMultiStrategyEngine();
            Console.WriteLine("\n=== All Tests Complete ===");
        }

        private static void TestMACrossStrategy()
        {
            Console.WriteLine("--- MA Cross Strategy Test ---");
            var strategy = new MACrossStrategy(9, 21, 2.5);
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars);
            var signal = strategy.Evaluate(state);
            Console.WriteLine($"Signal: {(signal.HasValue ? signal.Value.Direction.ToString() : "null")}");
            Console.WriteLine($"Confidence: {(signal.HasValue ? signal.Value.Confidence.ToString("F2") : "N/A")}");
            Console.WriteLine($"Rationale: {(signal.HasValue ? signal.Value.Rationale : "N/A")}");
            Console.WriteLine();
        }

        private static void TestBBStrategy()
        {
            Console.WriteLine("--- BB Strategy Test ---");
            var strategy = new BBStrategy(20, 2.0);
            var bars = MockDataGenerator.GenerateRangingMarket(100);
            var state = MockDataGenerator.BuildState(bars);
            var signal = strategy.Evaluate(state);
            Console.WriteLine($"Signal: {(signal.HasValue ? signal.Value.Direction.ToString() : "null")}");
            Console.WriteLine($"Confidence: {(signal.HasValue ? signal.Value.Confidence.ToString("F2") : "N/A")}");
            Console.WriteLine();
        }

        private static void TestRSIStrategy()
        {
            Console.WriteLine("--- RSI Strategy Test ---");
            var strategy = new RSIStrategy(14, 30, 70, 50);
            var bars = MockDataGenerator.GenerateBearishTrend(100);
            var state = MockDataGenerator.BuildState(bars);
            var signal = strategy.Evaluate(state);
            Console.WriteLine($"Signal: {(signal.HasValue ? signal.Value.Direction.ToString() : "null")}");
            Console.WriteLine($"Confidence: {(signal.HasValue ? signal.Value.Confidence.ToString("F2") : "N/A")}");
            Console.WriteLine();
        }

        private static void TestSRBreakoutStrategy()
        {
            Console.WriteLine("--- S/R Breakout Strategy Test ---");
            var strategy = new SRBreakoutStrategy(50, 2, 0.0005);
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars);
            var signal = strategy.Evaluate(state);
            Console.WriteLine($"Signal: {(signal.HasValue ? signal.Value.Direction.ToString() : "null")}");
            Console.WriteLine($"Confidence: {(signal.HasValue ? signal.Value.Confidence.ToString("F2") : "N/A")}");
            Console.WriteLine();
        }

        private static void TestROCMStrategy()
        {
            Console.WriteLine("--- ROC Momentum Strategy Test ---");
            var strategy = new ROCMStrategy(12, 0.5);
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars);
            var signal = strategy.Evaluate(state);
            Console.WriteLine($"Signal: {(signal.HasValue ? signal.Value.Direction.ToString() : "null")}");
            Console.WriteLine($"Confidence: {(signal.HasValue ? signal.Value.Confidence.ToString("F2") : "N/A")}");
            Console.WriteLine();
        }

        private static void TestMultiStrategyEngine()
        {
            Console.WriteLine("--- Multi-Strategy Backtest Engine Test ---");
            var strategies = new List<ISignalStrategy>
            {
                new MACrossStrategy(),
                new BBStrategy(),
                new ROCMStrategy()
            };

            var config = BacktestConfig.Default;
            var multiConfig = new MultiStrategyConfig();
            var engine = new MultiStrategyBacktestEngine(config, strategies, multiConfig);

            var bullishBars = MockDataGenerator.GenerateBullishTrend(500);
            var results = engine.RunAllStrategies(bullishBars);

            foreach (var result in results)
            {
                Console.WriteLine($"Strategy: {result.Key}");
                Console.WriteLine($"  Trades: {result.Value.Metrics.TotalTrades}");
                Console.WriteLine($"  P&L: ${result.Value.Metrics.TotalPnL:F2}");
                Console.WriteLine($"  Win Rate: {result.Value.Metrics.WinRate:F1}%");
                Console.WriteLine();
            }
        }
    }
}