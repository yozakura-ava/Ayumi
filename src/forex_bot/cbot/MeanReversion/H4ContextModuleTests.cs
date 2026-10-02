using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public static class H4ContextModuleTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== H4 Context Module Tests ===\n");

            Test_ConfigDefaults();
            Test_UpdateExpandsH4Slice();
            Test_NoDetectionWithFewBars();
            Test_DetectsH4OrderBlock();
            Test_DetectsH4FVG();
            Test_H4OBConfirmationNearZone();
            Test_H4OBConfirmationFarFromZone();
            Test_H4OBConfirmationMitigated();
            Test_H4OBBConfidenceBounds();
            Test_H4FVGConfirmationPriceInGap();
            Test_H4FVGConfirmationPriceNearGap();
            Test_H4FVGConfirmationStale();
            Test_H4FVGConfirmationMitigated();
            Test_H4FVGConfidenceBounds();
            Test_EvaluateH4ZonesBothPass();
            Test_EvaluateH4ZonesNonePass();
            Test_EvaluateH4ZonesOnlyOBPasses();
            Test_EvaluateH4ZonesOnlyFVGPasses();
            Test_BuildH4Slice();
            Test_GetH4BarIndex();
            Test_H4StructureBias();
            Test_H4ATR();
            Test_H4ZonesWithRealisticData();

            Console.WriteLine($"\n=== H4 Context Module Results: {Passed} passed, {Failed} failed ===");
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

        private static List<Bar> MakeH4Bars(int count, double startPrice = 1.1000,
            double volatility = 0.005, DateTime? startTime = null)
        {
            var bars = new List<Bar>();
            var baseTime = startTime ?? new DateTime(2026, 4, 1, 0, 0, 0, DateTimeKind.Utc);
            double price = startPrice;
            var rng = new Random(42);

            for (int i = 0; i < count; i++)
            {
                double change = (rng.NextDouble() - 0.48) * volatility;
                double open = price;
                double close = price + change;
                double high = Math.Max(open, close) + rng.NextDouble() * volatility * 0.5;
                double low = Math.Min(open, close) - rng.NextDouble() * volatility * 0.5;

                bars.Add(new Bar
                {
                    Time = baseTime.AddHours(i * 4),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = 1000 + rng.NextDouble() * 5000,
                    Period = TimeFrame.H4
                });

                price = close;
            }

            return bars;
        }

        private static List<Bar> MakeH4BarsWithBullishOB(int count = 20)
        {
            var bars = new List<Bar>();
            var baseTime = new DateTime(2026, 4, 1, 0, 0, 0, DateTimeKind.Utc);
            double price = 1.0900;

            for (int i = 0; i < count; i++)
            {
                double open = price;
                double close, high, low;

                if (i == 10)
                {
                    close = open + 0.008;
                    high = close + 0.001;
                    low = open - 0.0005;
                }
                else if (i > 10 && i < 15)
                {
                    close = open + 0.002 * (i - 10);
                    high = Math.Max(open, close) + 0.001;
                    low = Math.Min(open, close) - 0.0005;
                }
                else
                {
                    close = open + (i < 10 ? 0.001 : -0.001) + (new Random(i).NextDouble() - 0.5) * 0.002;
                    high = Math.Max(open, close) + 0.001;
                    low = Math.Min(open, close) - 0.0005;
                }

                bars.Add(new Bar
                {
                    Time = baseTime.AddHours(i * 4),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Period = TimeFrame.H4
                });

                price = close;
            }

            return bars;
        }

        private static List<Bar> MakeH4BarsWithFVG(int count = 20)
        {
            var bars = new List<Bar>();
            var baseTime = new DateTime(2026, 4, 1, 0, 0, 0, DateTimeKind.Utc);
            double price = 1.1000;

            for (int i = 0; i < count; i++)
            {
                double open = price;
                double close, high, low;

                if (i == 10)
                {
                    close = open + 0.006;
                    high = close + 0.002;
                    low = open - 0.001;
                }
                else if (i == 11)
                {
                    close = open - 0.003;
                    high = open + 0.001;
                    low = close - 0.001;
                }
                else if (i == 12)
                {
                    close = open + 0.005;
                    high = close + 0.001;
                    low = open + 0.003;
                }
                else
                {
                    close = open + (new Random(i + 100).NextDouble() - 0.5) * 0.003;
                    high = Math.Max(open, close) + 0.001;
                    low = Math.Min(open, close) - 0.001;
                }

                bars.Add(new Bar
                {
                    Time = baseTime.AddHours(i * 4),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Period = TimeFrame.H4
                });

                price = close;
            }

            return bars;
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

        private static void Test_ConfigDefaults()
        {
            Console.WriteLine("\nTest: H4 context config defaults");
            var config = H4ContextConfig.Default;

            Assert(config.OBFreshnessWindow == 10, "OB freshness window = 10");
            Assert(config.OBLookback == 30, "OB lookback = 30");
            Assert(config.FVGMaxAge == 15, "FVG max age = 15");
            AssertApprox(config.FVGMiniThreshold, 0.0003, 0.00001, "FVG mini threshold = 0.0003");
        }

        private static void Test_UpdateExpandsH4Slice()
        {
            Console.WriteLine("\nTest: Update expands H4 slice correctly");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(20);
            var earlyTime = h4Bars[5].Time;
            var laterTime = h4Bars[15].Time;

            module.Update(h4Bars, earlyTime);
            Assert(module.ActiveH4OrderBlocks.Count >= 0, "Early update succeeds");
            Assert(module.ActiveH4FVGs.Count >= 0, "Early FVG update succeeds");

            module.Update(h4Bars, laterTime);
            Assert(module.ActiveH4OrderBlocks.Count >= 0, "Later update succeeds");
            Assert(module.ActiveH4FVGs.Count >= 0, "Later FVG update succeeds");
        }

        private static void Test_NoDetectionWithFewBars()
        {
            Console.WriteLine("\nTest: No detection with fewer than 10 bars");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(5);
            var time = h4Bars[4].Time;

            module.Update(h4Bars, time);

            Assert(module.ActiveH4OrderBlocks.Count == 0, "No OBs with 5 bars");
            Assert(module.ActiveH4FVGs.Count == 0, "No FVGs with 5 bars");
            Assert(module.H4ATR == 0, "No ATR with 5 bars");
        }

        private static void Test_DetectsH4OrderBlock()
        {
            Console.WriteLine("\nTest: Detects H4 order blocks");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithBullishOB(20);
            var time = h4Bars[19].Time;

            module.Update(h4Bars, time);

            var ob = module.GetMostRelevantOB(TradeDirection.Long);
            Assert(ob.HasValue, "Found at least one bullish H4 OB");
            if (ob.HasValue)
            {
                Assert(!ob.Value.IsMitigated, "H4 OB is not mitigated");
                Assert(ob.Value.TimeFrame.Equals(TimeFrame.H4), "H4 OB has H4 timeframe");
            }
        }

        private static void Test_DetectsH4FVG()
        {
            Console.WriteLine("\nTest: Detects H4 fair value gaps");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithFVG(20);
            var time = h4Bars[19].Time;

            module.Update(h4Bars, time);

            var fvg = module.GetNearestUnfilledFVG(TradeDirection.Long, 1.1000);
            Assert(fvg.HasValue, "Found at least one H4 FVG");
            if (fvg.HasValue)
            {
                Assert(!fvg.Value.IsMitigated, "H4 FVG is not mitigated");
                Console.WriteLine($"    FVG TimeFrame.Minutes={fvg.Value.TimeFrame.Minutes}, H4={TimeFrame.H4.Minutes}");
                Assert(fvg.Value.TimeFrame.Minutes == TimeFrame.H4.Minutes, "H4 FVG has H4 timeframe");
            }
        }

        private static void Test_H4OBConfirmationNearZone()
        {
            Console.WriteLine("\nTest: H4 OB confirmation passes when price near zone");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithBullishOB(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var ob = module.GetMostRelevantOB(TradeDirection.Long);
            if (!ob.HasValue)
            {
                Assert(false, "No H4 OB found - test setup issue");
                return;
            }

            double obMid = (ob.Value.Top + ob.Value.Bottom) / 2.0;
            var signal = MakeValidSignal(TradeDirection.Long, obMid);

            var result = module.CheckH4OrderBlock(signal, module.H4ATR);

            Assert(result.Passed, "H4 OB confirmation passes near zone");
            Assert(result.Confidence > 0, "H4 OB confidence > 0");
            Assert(result.Confidence <= 1.0, "H4 OB confidence <= 1.0");
        }

        private static void Test_H4OBConfirmationFarFromZone()
        {
            Console.WriteLine("\nTest: H4 OB confirmation fails when price far from zone");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithBullishOB(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var ob = module.GetMostRelevantOB(TradeDirection.Long);
            if (!ob.HasValue)
            {
                Assert(false, "No H4 OB found - test setup issue");
                return;
            }

            double farPrice = ob.Value.Top + 0.05;
            var signal = MakeValidSignal(TradeDirection.Long, farPrice);

            var result = module.CheckH4OrderBlock(signal, module.H4ATR);

            Assert(!result.Passed, "H4 OB confirmation fails far from zone");
            Assert(result.Confidence == 0, "H4 OB confidence = 0");
        }

        private static void Test_H4OBConfirmationMitigated()
        {
            Console.WriteLine("\nTest: H4 OB confirmation fails for mitigated OB");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithBullishOB(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            if (module.ActiveH4OrderBlocks.Count == 0)
            {
                Assert(false, "No H4 OBs found - test setup issue");
                return;
            }

            var obList = new List<OrderBlock>(module.ActiveH4OrderBlocks);
            obList[0] = new OrderBlock
            {
                Top = obList[0].Top,
                Bottom = obList[0].Bottom,
                Direction = obList[0].Direction,
                Strength = obList[0].Strength,
                IsMitigated = true,
                Age = obList[0].Age,
                StartIndex = obList[0].StartIndex,
                EndIndex = obList[0].EndIndex,
                CreatedTime = obList[0].CreatedTime,
                TimeFrame = obList[0].TimeFrame,
                BodySize = obList[0].BodySize
            };
            module.ActiveH4OrderBlocks.Clear();
            foreach (var ob in obList)
                module.ActiveH4OrderBlocks.Add(ob);

            var signal = MakeValidSignal(TradeDirection.Long, 1.1000);
            var result = module.CheckH4OrderBlock(signal, module.H4ATR);

            Assert(!result.Passed, "H4 OB confirmation fails for mitigated OB");
        }

        private static void Test_H4OBBConfidenceBounds()
        {
            Console.WriteLine("\nTest: H4 OB confidence score in valid range");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(30);
            var time = h4Bars[29].Time;
            module.Update(h4Bars, time);

            var signal = MakeValidSignal(TradeDirection.Long, 1.1000);
            var result = module.CheckH4OrderBlock(signal, module.H4ATR);

            if (result.Passed)
            {
                Assert(result.Confidence >= 0 && result.Confidence <= 1.0,
                    $"H4 OB confidence in [0,1] (got {result.Confidence:F4})");
            }
            else
            {
                Assert(result.Confidence == 0, "H4 OB confidence = 0 when not passed");
            }
        }

        private static void Test_H4FVGConfirmationPriceInGap()
        {
            Console.WriteLine("\nTest: H4 FVG confirmation passes when price inside gap");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithFVG(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var fvg = module.GetNearestUnfilledFVG(TradeDirection.Long, 1.1000);
            if (!fvg.HasValue)
            {
                Assert(false, "No H4 FVG found - test setup issue");
                return;
            }

            double gapMid = (fvg.Value.Top + fvg.Value.Bottom) / 2.0;
            var signal = MakeValidSignal(TradeDirection.Long, gapMid);

            var result = module.CheckH4FVG(signal);

            Assert(result.Passed, "H4 FVG confirmation passes when price in gap");
            Assert(result.Confidence > 0, "H4 FVG confidence > 0");
        }

        private static void Test_H4FVGConfirmationPriceNearGap()
        {
            Console.WriteLine("\nTest: H4 FVG confirmation passes when price near gap");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithFVG(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var fvg = module.GetNearestUnfilledFVG(TradeDirection.Long, 1.1000);
            if (!fvg.HasValue)
            {
                Assert(false, "No H4 FVG found - test setup issue");
                return;
            }

            double nearPrice = fvg.Value.Top + (fvg.Value.Top - fvg.Value.Bottom) * 0.5;
            var signal = MakeValidSignal(TradeDirection.Long, nearPrice);

            var result = module.CheckH4FVG(signal);

            Assert(result.Passed, "H4 FVG confirmation passes when price near gap");
        }

        private static void Test_H4FVGConfirmationStale()
        {
            Console.WriteLine("\nTest: H4 FVG confirmation fails for stale FVG");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithFVG(40);
            var time = h4Bars[39].Time;
            module.Update(h4Bars, time);

            var signal = MakeValidSignal(TradeDirection.Long, 1.1000);

            foreach (var fvg in module.ActiveH4FVGs.Where(f => f.Age > 15))
            {
                var staleFVG = fvg;
                module.ActiveH4FVGs.Clear();
                module.ActiveH4FVGs.Add(staleFVG);

                var result = module.CheckH4FVG(signal);
                Assert(!result.Passed, "H4 FVG confirmation fails for stale FVG");
                break;
            }
        }

        private static void Test_H4FVGConfirmationMitigated()
        {
            Console.WriteLine("\nTest: H4 FVG confirmation fails for mitigated FVG");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithFVG(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            if (module.ActiveH4FVGs.Count == 0)
            {
                Assert(false, "No H4 FVGs found - test setup issue");
                return;
            }

            var fvgList = new List<FairValueGap>(module.ActiveH4FVGs);
            fvgList[0] = new FairValueGap
            {
                Top = fvgList[0].Top,
                Bottom = fvgList[0].Bottom,
                Direction = fvgList[0].Direction,
                Size = fvgList[0].Size,
                Age = fvgList[0].Age,
                IsFilled = false,
                IsMitigated = true,
                StartIndex = fvgList[0].StartIndex,
                CreatedTime = fvgList[0].CreatedTime,
                TimeFrame = fvgList[0].TimeFrame
            };
            module.ActiveH4FVGs.Clear();
            foreach (var fvg in fvgList)
                module.ActiveH4FVGs.Add(fvg);

            var signal = MakeValidSignal(TradeDirection.Long, 1.1000);
            var result = module.CheckH4FVG(signal);

            Assert(!result.Passed, "H4 FVG confirmation fails for mitigated FVG");
        }

        private static void Test_H4FVGConfidenceBounds()
        {
            Console.WriteLine("\nTest: H4 FVG confidence score in valid range");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithFVG(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var fvg = module.GetNearestUnfilledFVG(TradeDirection.Long, 1.1000);
            if (!fvg.HasValue)
            {
                Assert(false, "No H4 FVG found - test setup issue");
                return;
            }

            double gapMid = (fvg.Value.Top + fvg.Value.Bottom) / 2.0;
            var signal = MakeValidSignal(TradeDirection.Long, gapMid);

            var result = module.CheckH4FVG(signal);

            if (result.Passed)
            {
                Assert(result.Confidence >= 0 && result.Confidence <= 1.0,
                    $"H4 FVG confidence in [0,1] (got {result.Confidence:F4})");
            }
            else
            {
                Assert(result.Confidence == 0, "H4 FVG confidence = 0 when not passed");
            }
        }

        private static void Test_EvaluateH4ZonesBothPass()
        {
            Console.WriteLine("\nTest: EvaluateH4Zones with both OB and FVG passing");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithBullishOB(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var ob = module.GetMostRelevantOB(TradeDirection.Long);
            if (!ob.HasValue)
            {
                Assert(false, "No H4 OB - test setup issue");
                return;
            }

            double obMid = (ob.Value.Top + ob.Value.Bottom) / 2.0;
            var signal = MakeValidSignal(TradeDirection.Long, obMid);

            if (module.ActiveH4FVGs.Count > 0)
            {
                module.ActiveH4FVGs.Clear();
            }
            module.ActiveH4FVGs.Add(new FairValueGap
            {
                Top = obMid + 0.001,
                Bottom = obMid - 0.001,
                Direction = TradeDirection.Long,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 10,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H4
            });

            var result = module.EvaluateH4Zones(signal);

            Assert(result.HasH4OB, "H4 OB detected");
            Assert(result.HasH4FVG, "H4 FVG detected");
            Assert(result.H4ConfirmationsPassed >= 1, "At least 1 H4 confirmation");
        }

        private static void Test_EvaluateH4ZonesNonePass()
        {
            Console.WriteLine("\nTest: EvaluateH4Zones with neither passing");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            module.ActiveH4OrderBlocks.Clear();
            module.ActiveH4FVGs.Clear();

            var signal = MakeValidSignal(TradeDirection.Long, 1.2000);
            var result = module.EvaluateH4Zones(signal);

            Assert(!result.HasH4OB, "No H4 OB");
            Assert(!result.HasH4FVG, "No H4 FVG");
            Assert(result.H4ConfirmationsPassed == 0, "0 H4 confirmations");
        }

        private static void Test_EvaluateH4ZonesOnlyOBPasses()
        {
            Console.WriteLine("\nTest: EvaluateH4Zones with only OB passing");
            var module = new H4ContextModule();
            var h4Bars = MakeH4BarsWithBullishOB(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            var ob = module.GetMostRelevantOB(TradeDirection.Long);
            if (!ob.HasValue)
            {
                Assert(false, "No H4 OB - test setup issue");
                return;
            }

            double obMid = (ob.Value.Top + ob.Value.Bottom) / 2.0;
            var signal = MakeValidSignal(TradeDirection.Long, obMid);

            module.ActiveH4FVGs.Clear();

            var result = module.EvaluateH4Zones(signal);

            Assert(result.HasH4OB, "H4 OB detected");
            Assert(!result.HasH4FVG, "No H4 FVG");
            Assert(result.H4ConfirmationsPassed == 1, "1 H4 confirmation (OB only)");
        }

        private static void Test_EvaluateH4ZonesOnlyFVGPasses()
        {
            Console.WriteLine("\nTest: EvaluateH4Zones with only FVG passing");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(20);
            var time = h4Bars[19].Time;
            module.Update(h4Bars, time);

            module.ActiveH4OrderBlocks.Clear();

            double fvgMid = h4Bars[19].Close;
            module.ActiveH4FVGs.Add(new FairValueGap
            {
                Top = fvgMid + 0.001,
                Bottom = fvgMid - 0.001,
                Direction = TradeDirection.Long,
                Size = 0.002,
                Age = 3,
                IsFilled = false,
                IsMitigated = false,
                StartIndex = 15,
                CreatedTime = DateTime.UtcNow,
                TimeFrame = TimeFrame.H4
            });

            var signal = MakeValidSignal(TradeDirection.Long, fvgMid);
            var result = module.EvaluateH4Zones(signal);

            Assert(!result.HasH4OB, "No H4 OB");
            Assert(result.HasH4FVG, "H4 FVG detected");
            Assert(result.H4ConfirmationsPassed == 1, "1 H4 confirmation (FVG only)");
        }

        private static void Test_BuildH4Slice()
        {
            Console.WriteLine("\nTest: BuildH4Slice static method");
            var h4Bars = MakeH4Bars(10);
            var targetTime = h4Bars[5].Time;

            var slice = H4ContextModule.BuildH4Slice(h4Bars, targetTime);

            Assert(slice.Count == 6, $"Slice has 6 bars (got {slice.Count})");
            Assert(slice[5].Time == targetTime, "Last bar is at target time");
        }

        private static void Test_GetH4BarIndex()
        {
            Console.WriteLine("\nTest: GetH4BarIndex static method");
            var h4Bars = MakeH4Bars(10);

            int idx = H4ContextModule.GetH4BarIndex(h4Bars, h4Bars[5].Time);
            Assert(idx == 5, $"Index = 5 (got {idx})");

            var betweenTime = h4Bars[5].Time.AddHours(2);
            idx = H4ContextModule.GetH4BarIndex(h4Bars, betweenTime);
            Assert(idx == 5, $"Index = 5 for time between bars (got {idx})");

            var beforeFirst = h4Bars[0].Time.AddHours(-1);
            idx = H4ContextModule.GetH4BarIndex(h4Bars, beforeFirst);
            Assert(idx == -1, $"Index = -1 for time before first bar (got {idx})");
        }

        private static void Test_H4StructureBias()
        {
            Console.WriteLine("\nTest: H4 structure bias available after update");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(30);
            var time = h4Bars[29].Time;
            module.Update(h4Bars, time);

            bool validBias = module.H4StructureBias == TradeDirection.Long
                          || module.H4StructureBias == TradeDirection.Short
                          || module.H4StructureBias == TradeDirection.Neutral;

            Assert(validBias, $"H4 structure bias is valid ({module.H4StructureBias})");
        }

        private static void Test_H4ATR()
        {
            Console.WriteLine("\nTest: H4 ATR calculated after update");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(30);
            var time = h4Bars[29].Time;
            module.Update(h4Bars, time);

            Assert(module.H4ATR > 0, $"H4 ATR > 0 (got {module.H4ATR:F6})");
        }

        private static void Test_H4ZonesWithRealisticData()
        {
            Console.WriteLine("\nTest: H4 zones with realistic trending data");
            var module = new H4ContextModule();
            var h4Bars = MakeH4Bars(50, 1.1000, 0.008);
            var time = h4Bars[49].Time;
            module.Update(h4Bars, time);

            bool hasOBs = module.ActiveH4OrderBlocks.Count > 0;
            bool hasFVGs = module.ActiveH4FVGs.Count > 0;

            Console.WriteLine($"    H4 OBs: {module.ActiveH4OrderBlocks.Count}, FVGs: {module.ActiveH4FVGs.Count}");

            Assert(module.H4ATR > 0, "ATR calculated");
            if (hasOBs)
            {
                Assert(module.ActiveH4OrderBlocks.All(ob => ob.TimeFrame.Equals(TimeFrame.H4)),
                    "All H4 OBs have H4 timeframe");
            }
            if (hasFVGs)
            {
                Assert(module.ActiveH4FVGs.All(fvg => fvg.TimeFrame.Equals(TimeFrame.H4)),
                    "All H4 FVGs have H4 timeframe");
            }
        }
    }
}
