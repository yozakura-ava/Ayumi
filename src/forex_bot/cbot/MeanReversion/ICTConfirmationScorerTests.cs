using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public static class ICTConfirmationScorerTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== ICT Confirmation Scorer Tests ===\n");

            Test_ConfigDefaults();
            Test_ConfigPresets();
            Test_InvalidSignalReturnsInvalid();
            Test_SignalWithNoConfirmationsFiltered();
            Test_SignalWithOneConfirmationFilteredWithDefaultThreshold();
            Test_SignalWithTwoConfirmationsPasses();
            Test_SignalWithAllThreeConfirmationsPasses();
            Test_OrderBlockConfirmationNearOB();
            Test_OrderBlockConfirmationFarFromOB();
            Test_OrderBlockConfirmationMitigatedOB();
            Test_OrderBlockConfidenceScore();
            Test_FVGConfirmationPriceInGap();
            Test_FVGConfirmationPriceNearGap();
            Test_FVGConfirmationStaleFVG();
            Test_FVGConfirmationMitigatedFVG();
            Test_FVGConfidenceScore();
            Test_KillzoneConfirmationLondon();
            Test_KillzoneConfirmationNY();
            Test_KillzoneConfirmationOutsideSession();
            Test_KillzoneConfidenceLondonCenter();
            Test_KillzoneConfidenceNYCenter();
            Test_ThresholdOnePassesWithSingleConfirmation();
            Test_ThresholdThreeRequiresAll();
            Test_MetricsTrackingSignalsFiltered();
            Test_MetricsTrackingSignalsPassed();
            Test_MetricsTrackingPerConfirmation();
            Test_MetricsPassRate();
            Test_ResetMetrics();
            Test_IntegrationFiltersMRSignal();
            Test_IntegrationPassesMRSignalWithFullConfluence();
            Test_EvaluatePreservesOriginalSignal();
            Test_LooseConfigWiderKillzone();
            Test_StrictConfigNarrowerOB();

            Console.WriteLine($"\n=== ICT Confirmation Scorer Results: {Passed} passed, {Failed} failed ===");
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

        private static void AssertApprox(double actual, double expected, double tolerance, string testName)
        {
            Assert(Math.Abs(actual - expected) < tolerance,
                $"{testName} (expected {expected:F4}, got {actual:F4})");
        }

        private static MRSignal MakeValidSignal(
            TradeDirection direction = TradeDirection.Long,
            double entryPrice = 1.1000,
            DateTime? time = null)
        {
            return new MRSignal
            {
                Direction = direction,
                EntryPrice = entryPrice,
                SignalTime = time ?? new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc),
                BBUpper = 1.1050,
                BBMiddle = 1.1000,
                BBLower = 1.0950,
                RSI = 25.0,
                D1EMA200 = 1.0950,
                ConfidenceScore = 0.8,
                IsValid = true
            };
        }

        private static MRSignal MakeInvalidSignal()
        {
            return new MRSignal
            {
                Direction = TradeDirection.Neutral,
                IsValid = false
            };
        }

        private static MarketState MakeStateWithOB(
            double obTop = 1.1010,
            double obBottom = 1.0990,
            TradeDirection direction = TradeDirection.Long,
            double strength = 0.8,
            int age = 2)
        {
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc),
                             Open = 1.1000, High = 1.1010, Low = 1.0990, Close = 1.1000,
                             Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.London
            };

            state.ActiveOrderBlocks.Add(new OrderBlock
            {
                Top = obTop,
                Bottom = obBottom,
                Direction = direction,
                Strength = strength,
                IsMitigated = false,
                Age = age,
                StartIndex = 0,
                EndIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H4,
                BodySize = obTop - obBottom
            });

            return state;
        }

        private static MarketState MakeStateWithFVG(
            double fvgTop = 1.1010,
            double fvgBottom = 1.0990,
            TradeDirection direction = TradeDirection.Long,
            double size = 0.0020,
            int age = 3,
            bool isFilled = false,
            bool isMitigated = false)
        {
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc),
                             Open = 1.1000, High = 1.1010, Low = 1.0990, Close = 1.1000,
                             Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.London
            };

            state.ActiveFVGs.Add(new FairValueGap
            {
                Top = fvgTop,
                Bottom = fvgBottom,
                Direction = direction,
                Size = size,
                Age = age,
                IsFilled = isFilled,
                IsMitigated = isMitigated,
                StartIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H1
            });

            return state;
        }

        private static void Test_ConfigDefaults()
        {
            Console.WriteLine("\nTest: Config defaults");
            var config = ICTConfirmationConfig.Default;

            Assert(config.RequiredConfirmations == 2, "Required confirmations = 2");
            AssertApprox(config.OBProximityATRMultiplier, 1.5, 0.001, "OB proximity multiplier = 1.5");
            AssertApprox(config.FVGMaxAge, 20, 0.001, "FVG max age = 20");
            AssertApprox(config.KillzoneStartHourUTC, 8.0, 0.001, "Killzone start = 8 UTC");
            AssertApprox(config.KillzoneEndHourUTC, 16.0, 0.001, "Killzone end = 16 UTC");
        }

        private static void Test_ConfigPresets()
        {
            Console.WriteLine("\nTest: Config presets have distinct values");
            var def = ICTConfirmationConfig.Default;
            var strict = ICTConfirmationConfig.Strict;
            var loose = ICTConfirmationConfig.Loose;

            Assert(strict.RequiredConfirmations == 3, "Strict requires 3 confirmations");
            Assert(loose.RequiredConfirmations == 1, "Loose requires 1 confirmation");
            Assert(strict.OBProximityATRMultiplier < def.OBProximityATRMultiplier,
                "Strict OB multiplier < Default");
            Assert(loose.OBProximityATRMultiplier > def.OBProximityATRMultiplier,
                "Loose OB multiplier > Default");
            Assert(strict.FVGMaxAge < def.FVGMaxAge, "Strict FVG max age < Default");
            Assert(loose.FVGMaxAge > def.FVGMaxAge, "Loose FVG max age > Default");
        }

        private static void Test_InvalidSignalReturnsInvalid()
        {
            Console.WriteLine("\nTest: Invalid MR signal returns invalid scorer result");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeInvalidSignal();
            var state = new MarketState();

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(!result.IsValid, "Result is invalid");
            Assert(result.ConfirmationsPassed == 0, "No confirmations passed");
        }

        private static void Test_SignalWithNoConfirmationsFiltered()
        {
            Console.WriteLine("\nTest: Signal with 0 confirmations is filtered");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(time: new DateTime(2026, 4, 2, 5, 0, 0, DateTimeKind.Utc));
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = signal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.Outside
            };

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(!result.IsValid, "Signal filtered");
            Assert(result.ConfirmationsPassed == 0, "0 confirmations passed");
            Assert(result.ConfirmationsChecked == 3, "3 confirmations checked");
            Assert(!result.KillzoneResult.Passed, "Killzone not passed");
        }

        private static void Test_SignalWithOneConfirmationFilteredWithDefaultThreshold()
        {
            Console.WriteLine("\nTest: Signal with 1 confirmation filtered (threshold=2)");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = signal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.London
            };

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(!result.IsValid, "Signal filtered with only 1 confirmation");
            Assert(result.KillzoneResult.Passed, "Killzone passed");
            Assert(!result.OrderBlockResult.Passed, "OB not passed");
            Assert(!result.FVGResult.Passed, "FVG not passed");
        }

        private static void Test_SignalWithTwoConfirmationsPasses()
        {
            Console.WriteLine("\nTest: Signal with 2 confirmations passes (threshold=2)");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(result.IsValid, "Signal passes with 2 confirmations");
            Assert(result.ConfirmationsPassed >= 2,
                $"At least 2 confirmations passed (got {result.ConfirmationsPassed})");
            Assert(result.KillzoneResult.Passed, "Killzone passed");
        }

        private static void Test_SignalWithAllThreeConfirmationsPasses()
        {
            Console.WriteLine("\nTest: Signal with all 3 confirmations passes");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);
            state.ActiveFVGs.Add(new FairValueGap
            {
                Top = 1.1010,
                Bottom = 1.0990,
                Direction = TradeDirection.Long,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H1
            });

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(result.IsValid, "Signal passes with all 3 confirmations");
            Assert(result.ConfirmationsPassed == 3, "All 3 confirmations passed");
            Assert(result.OrderBlockResult.Passed, "OB passed");
            Assert(result.FVGResult.Passed, "FVG passed");
            Assert(result.KillzoneResult.Passed, "Killzone passed");
            Assert(result.OverallConfidence > 0, "Overall confidence > 0");
        }

        private static void Test_OrderBlockConfirmationNearOB()
        {
            Console.WriteLine("\nTest: OB confirmation passes when price near OB");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);

            var result = scorer.CheckOrderBlock(signal, state, 0.005);

            Assert(result.Passed, "OB confirmation passes");
            Assert(result.Confidence > 0, "OB confidence > 0");
        }

        private static void Test_OrderBlockConfirmationFarFromOB()
        {
            Console.WriteLine("\nTest: OB confirmation fails when price far from OB");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1200);
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);

            var result = scorer.CheckOrderBlock(signal, state, 0.005);

            Assert(!result.Passed, "OB confirmation fails (too far)");
            Assert(result.Confidence == 0, "OB confidence = 0");
        }

        private static void Test_OrderBlockConfirmationMitigatedOB()
        {
            Console.WriteLine("\nTest: OB confirmation fails for mitigated OB");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);
            var obList = new List<OrderBlock>(state.ActiveOrderBlocks);
            obList[0] = new OrderBlock
            {
                Top = 1.1010,
                Bottom = 1.0990,
                Direction = TradeDirection.Long,
                Strength = 0.8,
                IsMitigated = true,
                Age = 2,
                StartIndex = 0,
                EndIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H4,
                BodySize = 0.002
            };
            state.ActiveOrderBlocks = obList;

            var result = scorer.CheckOrderBlock(signal, state, 0.005);

            Assert(!result.Passed, "OB confirmation fails (mitigated)");
        }

        private static void Test_OrderBlockConfidenceScore()
        {
            Console.WriteLine("\nTest: OB confidence score in valid range");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);

            var result = scorer.CheckOrderBlock(signal, state, 0.005);

            Assert(result.Confidence >= 0 && result.Confidence <= 1.0,
                $"Confidence in [0,1] range (got {result.Confidence:F4})");
        }

        private static void Test_FVGConfirmationPriceInGap()
        {
            Console.WriteLine("\nTest: FVG confirmation passes when price inside gap");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithFVG(1.1010, 1.0990, TradeDirection.Long, 0.002, 3);

            var result = scorer.CheckFVG(signal, state);

            Assert(result.Passed, "FVG confirmation passes (price in gap)");
            Assert(result.Confidence > 0, "FVG confidence > 0");
        }

        private static void Test_FVGConfirmationPriceNearGap()
        {
            Console.WriteLine("\nTest: FVG confirmation passes when price near gap");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1020);
            var state = MakeStateWithFVG(1.1010, 1.0990, TradeDirection.Long, 0.002, 3);

            var result = scorer.CheckFVG(signal, state);

            Assert(result.Passed, "FVG confirmation passes (price near gap)");
        }

        private static void Test_FVGConfirmationStaleFVG()
        {
            Console.WriteLine("\nTest: FVG confirmation fails for stale FVG");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithFVG(1.1010, 1.0990, TradeDirection.Long, 0.002, 25);

            var result = scorer.CheckFVG(signal, state);

            Assert(!result.Passed, "FVG confirmation fails (stale)");
        }

        private static void Test_FVGConfirmationMitigatedFVG()
        {
            Console.WriteLine("\nTest: FVG confirmation fails for mitigated FVG");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithFVG(
                1.1010, 1.0990, TradeDirection.Long, 0.002, 3,
                isFilled: false, isMitigated: true);

            var result = scorer.CheckFVG(signal, state);

            Assert(!result.Passed, "FVG confirmation fails (mitigated)");
        }

        private static void Test_FVGConfidenceScore()
        {
            Console.WriteLine("\nTest: FVG confidence score in valid range");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(entryPrice: 1.1000);
            var state = MakeStateWithFVG(1.1010, 1.0990, TradeDirection.Long, 0.002, 3);

            var result = scorer.CheckFVG(signal, state);

            Assert(result.Confidence >= 0 && result.Confidence <= 1.0,
                $"Confidence in [0,1] range (got {result.Confidence:F4})");
        }

        private static void Test_KillzoneConfirmationLondon()
        {
            Console.WriteLine("\nTest: Killzone confirmation passes during London");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 9, 30, 0, DateTimeKind.Utc));

            var result = scorer.CheckKillzone(signal);

            Assert(result.Passed, "Killzone passes during London");
            Assert(result.Confidence > 0, "Killzone confidence > 0");
        }

        private static void Test_KillzoneConfirmationNY()
        {
            Console.WriteLine("\nTest: Killzone confirmation passes during NY");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 14, 0, 0, DateTimeKind.Utc));

            var result = scorer.CheckKillzone(signal);

            Assert(result.Passed, "Killzone passes during NY");
            Assert(result.Confidence > 0, "Killzone confidence > 0");
        }

        private static void Test_KillzoneConfirmationOutsideSession()
        {
            Console.WriteLine("\nTest: Killzone confirmation fails outside sessions");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 5, 0, 0, DateTimeKind.Utc));

            var result = scorer.CheckKillzone(signal);

            Assert(!result.Passed, "Killzone fails outside sessions");
            Assert(result.Confidence == 0, "Killzone confidence = 0");
        }

        private static void Test_KillzoneConfidenceLondonCenter()
        {
            Console.WriteLine("\nTest: Killzone confidence peaks at London center");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);

            var center = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var edge = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc));

            var centerResult = scorer.CheckKillzone(center);
            var edgeResult = scorer.CheckKillzone(edge);

            Assert(centerResult.Confidence > edgeResult.Confidence,
                $"Center confidence ({centerResult.Confidence:F4}) > edge ({edgeResult.Confidence:F4})");
        }

        private static void Test_KillzoneConfidenceNYCenter()
        {
            Console.WriteLine("\nTest: Killzone confidence peaks at NY center");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);

            var center = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 14, 30, 0, DateTimeKind.Utc));
            var edge = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 13, 0, 0, DateTimeKind.Utc));

            var centerResult = scorer.CheckKillzone(center);
            var edgeResult = scorer.CheckKillzone(edge);

            Assert(centerResult.Confidence > edgeResult.Confidence,
                $"Center confidence ({centerResult.Confidence:F4}) > edge ({edgeResult.Confidence:F4})");
        }

        private static void Test_ThresholdOnePassesWithSingleConfirmation()
        {
            Console.WriteLine("\nTest: Threshold=1 passes with any single confirmation");
            var config = ICTConfirmationConfig.Default;
            config.RequiredConfirmations = 1;
            var scorer = new ICTConfirmationScorer(config);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = signal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.London
            };

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(result.IsValid, "Signal passes with threshold=1 (killzone only)");
            Assert(result.KillzoneResult.Passed, "Killzone confirmed");
        }

        private static void Test_ThresholdThreeRequiresAll()
        {
            Console.WriteLine("\nTest: Threshold=3 requires all confirmations");
            var config = ICTConfirmationConfig.Strict;
            var scorer = new ICTConfirmationScorer(config);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(!result.IsValid, "Signal filtered (no FVG, threshold=3)");
            Assert(result.OrderBlockResult.Passed, "OB confirmed");
            Assert(result.KillzoneResult.Passed, "Killzone confirmed");
        }

        private static void Test_MetricsTrackingSignalsFiltered()
        {
            Console.WriteLine("\nTest: Metrics track filtered signals");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 5, 0, 0, DateTimeKind.Utc));
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = signal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.Outside
            };

            scorer.Evaluate(signal, state, 0.005);

            Assert(scorer.Metrics.TotalSignalsEvaluated == 1, "1 signal evaluated");
            Assert(scorer.Metrics.SignalsFiltered == 1, "1 signal filtered");
            Assert(scorer.Metrics.SignalsPassed == 0, "0 signals passed");
        }

        private static void Test_MetricsTrackingSignalsPassed()
        {
            Console.WriteLine("\nTest: Metrics track passed signals");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);
            state.ActiveFVGs.Add(new FairValueGap
            {
                Top = 1.1010,
                Bottom = 1.0990,
                Direction = TradeDirection.Long,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H1
            });

            scorer.Evaluate(signal, state, 0.005);

            Assert(scorer.Metrics.TotalSignalsEvaluated == 1, "1 signal evaluated");
            Assert(scorer.Metrics.SignalsPassed == 1, "1 signal passed");
            Assert(scorer.Metrics.SignalsFiltered == 0, "0 signals filtered");
        }

        private static void Test_MetricsTrackingPerConfirmation()
        {
            Console.WriteLine("\nTest: Metrics track individual confirmation pass/fail");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 5, 0, 0, DateTimeKind.Utc));
            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = signal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.Outside
            };

            scorer.Evaluate(signal, state, 0.005);

            Assert(scorer.Metrics.KillzoneFailed == 1, "Killzone failed = 1");
            Assert(scorer.Metrics.KillzonePassed == 0, "Killzone passed = 0");
            Assert(scorer.Metrics.OrderBlockFailed == 1, "OB failed = 1");
            Assert(scorer.Metrics.FVGFailed == 1, "FVG failed = 1");
        }

        private static void Test_MetricsPassRate()
        {
            Console.WriteLine("\nTest: Metrics pass rate calculation");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);

            var passSignal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            var passState = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);
            passState.ActiveFVGs.Add(new FairValueGap
            {
                Top = 1.1010,
                Bottom = 1.0990,
                Direction = TradeDirection.Long,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H1
            });

            var failSignal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 5, 0, 0, DateTimeKind.Utc));
            var failState = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = failSignal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.Outside
            };

            scorer.Evaluate(passSignal, passState, 0.005);
            scorer.Evaluate(failSignal, failState, 0.005);

            AssertApprox(scorer.Metrics.PassRate, 50.0, 0.01, "Pass rate = 50%");
        }

        private static void Test_ResetMetrics()
        {
            Console.WriteLine("\nTest: ResetMetrics clears all counters");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var signal = MakeValidSignal();
            var state = MakeStateWithOB();

            scorer.Evaluate(signal, state, 0.005);
            Assert(scorer.Metrics.TotalSignalsEvaluated > 0, "Has metrics before reset");

            scorer.ResetMetrics();

            Assert(scorer.Metrics.TotalSignalsEvaluated == 0, "Total = 0 after reset");
            Assert(scorer.Metrics.SignalsPassed == 0, "Passed = 0 after reset");
            Assert(scorer.Metrics.SignalsFiltered == 0, "Filtered = 0 after reset");
            Assert(scorer.Metrics.OrderBlockPassed == 0, "OB passed = 0 after reset");
            Assert(scorer.Metrics.FVGPassed == 0, "FVG passed = 0 after reset");
            Assert(scorer.Metrics.KillzonePassed == 0, "KZ passed = 0 after reset");
        }

        private static void Test_IntegrationFiltersMRSignal()
        {
            Console.WriteLine("\nTest: Integration - scorer correctly filters MR signal");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);

            var signal = MakeValidSignal(
                TradeDirection.Long,
                1.1000,
                new DateTime(2026, 4, 2, 20, 0, 0, DateTimeKind.Utc));

            var state = new MarketState
            {
                Bars = new List<Bar>
                {
                    new Bar { Time = signal.SignalTime, Open = 1.1, High = 1.1,
                             Low = 1.1, Close = 1.1, Period = TimeFrame.H1 }
                },
                CurrentSession = SessionType.Outside
            };

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(!result.IsValid, "Signal filtered");
            Assert(!result.OrderBlockResult.Passed, "No OB confluence");
            Assert(!result.FVGResult.Passed, "No FVG confluence");
            Assert(!result.KillzoneResult.Passed, "Outside killzone");
            Assert(scorer.Metrics.SignalsFiltered == 1, "1 signal filtered in metrics");
        }

        private static void Test_IntegrationPassesMRSignalWithFullConfluence()
        {
            Console.WriteLine("\nTest: Integration - scorer passes MR signal with full confluence");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);

            var signal = MakeValidSignal(
                TradeDirection.Long,
                1.1000,
                new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));

            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);
            state.ActiveFVGs.Add(new FairValueGap
            {
                Top = 1.1010,
                Bottom = 1.0990,
                Direction = TradeDirection.Long,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H1
            });

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(result.IsValid, "Signal passes");
            Assert(result.ConfirmationsPassed == 3, "All 3 confirmations");
            Assert(result.OverallConfidence > 0, "Overall confidence > 0");
            Assert(result.Signal.EntryPrice == 1.1000, "Original signal preserved");
            Assert(result.Signal.Direction == TradeDirection.Long, "Original direction preserved");
            Assert(scorer.Metrics.SignalsPassed == 1, "1 signal passed in metrics");
        }

        private static void Test_EvaluatePreservesOriginalSignal()
        {
            Console.WriteLine("\nTest: Evaluate preserves all original MR signal fields");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);

            var signal = MakeValidSignal(
                TradeDirection.Short,
                1.1050,
                new DateTime(2026, 4, 2, 10, 0, 0, DateTimeKind.Utc));
            signal.BBUpper = 1.1080;
            signal.BBMiddle = 1.1050;
            signal.BBLower = 1.1020;
            signal.RSI = 75.0;
            signal.D1EMA200 = 1.1100;
            signal.ConfidenceScore = 0.65;

            var state = MakeStateWithOB(1.1060, 1.1040, TradeDirection.Short, 0.8, 2);
            state.ActiveFVGs.Add(new FairValueGap
            {
                Top = 1.1060,
                Bottom = 1.1040,
                Direction = TradeDirection.Short,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 0,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H1
            });

            var result = scorer.Evaluate(signal, state, 0.005);

            Assert(result.Signal.EntryPrice == 1.1050, "Entry price preserved");
            Assert(result.Signal.Direction == TradeDirection.Short, "Direction preserved");
            AssertApprox(result.Signal.RSI, 75.0, 0.001, "RSI preserved");
            AssertApprox(result.Signal.ConfidenceScore, 0.65, 0.001, "MR confidence preserved");
            AssertApprox(result.Signal.D1EMA200, 1.1100, 0.0001, "D1 EMA preserved");
        }

        private static void Test_LooseConfigWiderKillzone()
        {
            Console.WriteLine("\nTest: Loose config accepts wider killzone times");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Loose);
            var signal = MakeValidSignal(
                time: new DateTime(2026, 4, 2, 19, 0, 0, DateTimeKind.Utc));

            var result = scorer.CheckKillzone(signal);

            Assert(result.Passed, "Loose config accepts 19:00 UTC");
        }

        private static void Test_StrictConfigNarrowerOB()
        {
            Console.WriteLine("\nTest: Strict config requires closer OB proximity");
            var scorer = new ICTConfirmationScorer(ICTConfirmationConfig.Strict);
            var signal = MakeValidSignal(entryPrice: 1.1075);
            var state = MakeStateWithOB(1.1010, 1.0990, TradeDirection.Long, 0.8, 2);

            var defaultScorer = new ICTConfirmationScorer(ICTConfirmationConfig.Default);
            var defaultResult = defaultScorer.CheckOrderBlock(signal, state, 0.005);
            var strictResult = scorer.CheckOrderBlock(signal, state, 0.005);

            if (defaultResult.Passed)
            {
                Assert(!strictResult.Passed,
                    "Strict OB rejects signal that default accepts (farther away)");
            }
            else
            {
                Assert(!strictResult.Passed, "Strict OB also rejects");
            }
        }
    }
}
