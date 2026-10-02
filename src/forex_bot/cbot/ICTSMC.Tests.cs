using System;
using System.Linq;

namespace ICTSMC
{
    public static class ConfluenceEngineTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== ICT/SMC Signal Confluence Engine Tests ===\n");

            Test_BullishTrendProducesLongSignal();
            Test_BearishTrendProducesShortSignal();
            Test_RangingMarketProducesNoSignal();
            Test_ICTLongSetupProducesSignal();
            Test_ICTShortSetupProducesSignal();
            Test_OutsideSessionReducesConfidence();
            Test_SignalHasValidEntryExitLevels();
            Test_ConfluenceCountIsTracked();
            Test_RiskRewardIsPositive();
            Test_SignalConfidenceIsBounded();

            Console.WriteLine($"\n=== Results: {Passed} passed, {Failed} failed ===");
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

        private static void Test_BullishTrendProducesLongSignal()
        {
            Console.WriteLine("\nTest: Bullish trend produces Long signal");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.Direction == TradeDirection.Long, "Direction is Long");
                Assert(signal.Value.ConfidenceScore >= 0.55, $"Confidence >= 0.55 (got {signal.Value.ConfidenceScore:F2})");
            }
        }

        private static void Test_BearishTrendProducesShortSignal()
        {
            Console.WriteLine("\nTest: Bearish trend produces Short signal");
            var bars = MockDataGenerator.GenerateBearishTrend(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.Direction == TradeDirection.Short, "Direction is Short");
                Assert(signal.Value.ConfidenceScore >= 0.55, $"Confidence >= 0.55 (got {signal.Value.ConfidenceScore:F2})");
            }
        }

        private static void Test_RangingMarketProducesNoSignal()
        {
            Console.WriteLine("\nTest: Ranging market produces no signal (below threshold)");
            var bars = MockDataGenerator.GenerateRangingMarket(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.Outside);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            if (signal.HasValue)
            {
                Assert(signal.Value.ConfidenceScore < 0.70,
                    $"Low confidence in range: {signal.Value.ConfidenceScore:F2}");
            }
            else
            {
                Assert(true, "No signal generated (correct for low-conflict range)");
            }
        }

        private static void Test_ICTLongSetupProducesSignal()
        {
            Console.WriteLine("\nTest: ICT long setup produces signal");
            var bars = MockDataGenerator.GenerateICTSetup(isLong: true);
            var state = MockDataGenerator.BuildState(bars, SessionType.NYAM);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.Direction == TradeDirection.Long, "Direction is Long");
                Assert(signal.Value.ConfluenceCount >= 2, $"Confluence >= 2 (got {signal.Value.ConfluenceCount})");
            }
        }

        private static void Test_ICTShortSetupProducesSignal()
        {
            Console.WriteLine("\nTest: ICT short setup produces signal");
            var bars = MockDataGenerator.GenerateICTSetup(isLong: false);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.Direction == TradeDirection.Short, "Direction is Short");
            }
        }

        private static void Test_OutsideSessionReducesConfidence()
        {
            Console.WriteLine("\nTest: Outside session reduces confidence");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var stateInside = MockDataGenerator.BuildState(bars, SessionType.London);
            var stateOutside = MockDataGenerator.BuildState(bars, SessionType.Outside);
            var engine = new SignalConfluenceEngine();

            var signalInside = engine.Evaluate(stateInside);
            var signalOutside = engine.Evaluate(stateOutside);

            if (signalInside.HasValue && signalOutside.HasValue)
            {
                Assert(signalInside.Value.ConfidenceScore >= signalOutside.Value.ConfidenceScore,
                    $"Inside ({signalInside.Value.ConfidenceScore:F2}) >= Outside ({signalOutside.Value.ConfidenceScore:F2})");
            }
            else if (signalInside.HasValue && !signalOutside.HasValue)
            {
                Assert(true, "Signal inside session, no signal outside (session gate works)");
            }
            else
            {
                Assert(false, "Expected at least a signal inside session");
            }
        }

        private static void Test_SignalHasValidEntryExitLevels()
        {
            Console.WriteLine("\nTest: Signal has valid entry/exit levels");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                var s = signal.Value;
                Assert(s.EntryPrice > 0, "Entry > 0");
                Assert(s.StopLoss > 0, "SL > 0");
                Assert(s.TakeProfit1 > 0, "TP1 > 0");
                Assert(s.TakeProfit2 > 0, "TP2 > 0");
                Assert(s.TakeProfit3 > 0, "TP3 > 0");

                if (s.Direction == TradeDirection.Long)
                {
                    Assert(s.TakeProfit1 > s.EntryPrice, "TP1 > Entry (Long)");
                    Assert(s.EntryPrice > s.StopLoss, "Entry > SL (Long)");
                }
                else
                {
                    Assert(s.TakeProfit1 < s.EntryPrice, "TP1 < Entry (Short)");
                    Assert(s.EntryPrice < s.StopLoss, "Entry < SL (Short)");
                }
            }
        }

        private static void Test_ConfluenceCountIsTracked()
        {
            Console.WriteLine("\nTest: Confluence count is tracked");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.ConfluenceCount >= 1,
                    $"Confluence >= 1 (got {signal.Value.ConfluenceCount})");
                Assert(signal.Value.ConfluenceCount <= 6,
                    $"Confluence <= 6 (got {signal.Value.ConfluenceCount})");
            }
        }

        private static void Test_RiskRewardIsPositive()
        {
            Console.WriteLine("\nTest: Risk/Reward ratio is positive");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.RiskRewardRatio > 0,
                    $"R:R > 0 (got {signal.Value.RiskRewardRatio:F2})");
                Assert(signal.Value.RiskRewardRatio >= 0.5,
                    $"R:R >= 0.5 (got {signal.Value.RiskRewardRatio:F2})");
            }
        }

        private static void Test_SignalConfidenceIsBounded()
        {
            Console.WriteLine("\nTest: Signal confidence is bounded [0, 1]");
            var bars = MockDataGenerator.GenerateBullishTrend(100);
            var state = MockDataGenerator.BuildState(bars, SessionType.London);
            var engine = new SignalConfluenceEngine();
            var signal = engine.Evaluate(state);

            Assert(signal.HasValue, "Signal generated");
            if (signal.HasValue)
            {
                Assert(signal.Value.ConfidenceScore >= 0 && signal.Value.ConfidenceScore <= 1,
                    $"Confidence in [0,1] (got {signal.Value.ConfidenceScore:F2})");
            }
        }
    }
}
