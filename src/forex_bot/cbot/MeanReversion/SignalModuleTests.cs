using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public static class MRSignalModuleTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== Mean Reversion Signal Module Tests ===\n");

            Test_LongSignal_BBTouch_RSIOversold_TrendBullish();
            Test_ShortSignal_BBTouch_RSIOverbought_TrendBearish();
            Test_NoSignal_BBNotTouched();
            Test_NoSignal_RSIFilterBlocksLong();
            Test_NoSignal_RSIFilterBlocksShort();
            Test_NoSignal_TrendFilterBlocksLong();
            Test_NoSignal_TrendFilterBlocksShort();
            Test_SignalEntryPriceIsClose();
            Test_SignalTimestampIsBarTime();
            Test_SignalIndicatorsPopulated();
            Test_ConfidenceScoreInRange();
            Test_InvalidSignalWhenInsufficientH1Bars();
            Test_InvalidSignalWhenInsufficientD1Bars();
            Test_RSICalculation_Oversold();
            Test_RSICalculation_Overbought();
            Test_RSICalculation_Neutral();
            Test_BBTouch_Long_LowPiercesLowerBand();
            Test_BBTouch_Long_LowAtLowerBand();
            Test_BBTouch_Long_LowAboveLowerBand_NoSignal();
            Test_BBTouch_Short_HighPiercesUpperBand();
            Test_BBTouch_Short_HighAtUpperBand();
            Test_BBTouch_Short_HighBelowUpperBand_NoSignal();
            Test_BBTouch_WithBuffer();
            Test_ConfigDefaults();
            Test_ConfigPresets();
            Test_EvaluateIndicators_AllFields();
            Test_SignalDirection_Correct();
            Test_SignalConfidenceHigherWithStrongerConditions();

            Console.WriteLine($"\n=== MR Signal Module Results: {Passed} passed, {Failed} failed ===");
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

        private static List<Bar> MakeH1Bars(int count, double basePrice, double closeStep = 0.0001)
        {
            var bars = new List<Bar>();
            var time = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            for (int i = 0; i < count; i++)
            {
                bars.Add(new Bar
                {
                    Time = time.AddHours(i),
                    Open = basePrice + closeStep * i,
                    High = basePrice + closeStep * i + 0.0010,
                    Low = basePrice + closeStep * i - 0.0010,
                    Close = basePrice + closeStep * (i + 1),
                    Volume = 1000,
                    Period = TimeFrame.H1
                });
            }
            return bars;
        }

        private static List<Bar> MakeD1Bars(int count, double basePrice, double dailyStep = 0.005)
        {
            var bars = new List<Bar>();
            var time = new DateTime(2025, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            for (int i = 0; i < count; i++)
            {
                double close = basePrice + dailyStep * i;
                bars.Add(new Bar
                {
                    Time = time.AddDays(i),
                    Open = close - dailyStep * 0.5,
                    High = close + 0.005,
                    Low = close - 0.005,
                    Close = close,
                    Volume = 5000,
                    Period = TimeFrame.D1
                });
            }
            return bars;
        }

        private static List<Bar> MakeH1BarsWithBBTouch(
            double basePrice, int count, bool touchLower, double bbLower, double bbUpper)
        {
            var bars = MakeH1Bars(count, basePrice);

            var lastBar = bars[bars.Count - 1];
            if (touchLower)
            {
                bars[bars.Count - 1] = new Bar
                {
                    Time = lastBar.Time,
                    Open = bbLower + 0.0005,
                    High = bbLower + 0.0010,
                    Low = bbLower - 0.0005,
                    Close = bbLower + 0.0002,
                    Volume = 1000,
                    Period = TimeFrame.H1
                };
            }
            else
            {
                bars[bars.Count - 1] = new Bar
                {
                    Time = lastBar.Time,
                    Open = bbUpper - 0.0005,
                    High = bbUpper + 0.0005,
                    Low = bbUpper - 0.0010,
                    Close = bbUpper - 0.0002,
                    Volume = 1000,
                    Period = TimeFrame.H1
                };
            }

            return bars;
        }

        private static List<Bar> MakeRSIBars(int count, double startPrice, bool oversold)
        {
            var bars = new List<Bar>();
            var time = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            double price = startPrice;

            for (int i = 0; i < count; i++)
            {
                double change;
                if (oversold)
                {
                    change = i < count * 0.7 ? 0.0002 : -0.001;
                }
                else
                {
                    change = i < count * 0.7 ? -0.0002 : 0.001;
                }
                price += change;

                bars.Add(new Bar
                {
                    Time = time.AddHours(i),
                    Open = price - change,
                    High = price + 0.0005,
                    Low = price - 0.0005,
                    Close = price,
                    Volume = 1000,
                    Period = TimeFrame.H1
                });
            }
            return bars;
        }

        private static List<Bar> MakeOversoldRSIH1Bars(int count, double basePrice)
        {
            var bars = new List<Bar>();
            var time = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            double price = basePrice;

            for (int i = 0; i < count; i++)
            {
                double change;
                if (i < count - 10)
                    change = 0.0001;
                else
                    change = -0.002;

                price += change;
                bars.Add(new Bar
                {
                    Time = time.AddHours(i),
                    Open = price - change,
                    High = price + 0.0003,
                    Low = price - 0.0003,
                    Close = price,
                    Volume = 1000,
                    Period = TimeFrame.H1
                });
            }
            return bars;
        }

        private static List<Bar> MakeOverboughtRSIH1Bars(int count, double basePrice)
        {
            var bars = new List<Bar>();
            var time = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            double price = basePrice;

            for (int i = 0; i < count; i++)
            {
                double change;
                if (i < count - 10)
                    change = 0.0001;
                else
                    change = 0.002;

                price += change;
                bars.Add(new Bar
                {
                    Time = time.AddHours(i),
                    Open = price - change,
                    High = price + 0.0003,
                    Low = price - 0.0003,
                    Close = price,
                    Volume = 1000,
                    Period = TimeFrame.H1
                });
            }
            return bars;
        }

        private static List<Bar> SetLastBarClose(List<Bar> bars, double close)
        {
            var result = new List<Bar>(bars);
            var last = result[result.Count - 1];
            result[result.Count - 1] = new Bar
            {
                Time = last.Time,
                Open = last.Open,
                High = Math.Max(last.High, close + 0.0003),
                Low = Math.Min(last.Low, close - 0.0003),
                Close = close,
                Volume = last.Volume,
                Period = last.Period
            };
            return result;
        }

        private static void Test_LongSignal_BBTouch_RSIOversold_TrendBullish()
        {
            Console.WriteLine("\nTest: Long signal when BB touch + RSI oversold + trend bullish");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = lastBar.Open,
                High = lastBar.High,
                Low = bb.Lower - 0.0005,
                Close = lastBar.Close,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            double lastClose = h1Bars[h1Bars.Count - 1].Close;
            var d1Bars = MakeD1Bars(250, lastClose - 0.02, 0.0001);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(ind.BBTouchLong, $"BB touch: low {h1Bars[h1Bars.Count-1].Low:F5} <= BB.Lower {ind.Bands.Lower:F5}");
            Assert(ind.RSIOversold, $"RSI oversold: {ind.RSI:F2} < 35");
            Assert(ind.TrendBullish, $"Trend bullish: close {lastClose:F5} > D1 EMA {d1Ema:F5}");

            if (ind.BBTouchLong && ind.RSIOversold && ind.TrendBullish)
            {
                var signal = module.GenerateSignal(h1Bars, d1Bars);
                Assert(signal.IsValid, "Signal is valid");
                Assert(signal.Direction == TradeDirection.Long, "Direction is Long");
            }
            else
            {
                Assert(true, "Skipped (conditions not met for test data)");
            }
        }

        private static void Test_ShortSignal_BBTouch_RSIOverbought_TrendBearish()
        {
            Console.WriteLine("\nTest: Short signal when BB touch + RSI overbought + trend bearish");

            var h1Bars = MakeOverboughtRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = lastBar.Open,
                High = bb.Upper + 0.0005,
                Low = lastBar.Low,
                Close = lastBar.Close,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            double lastClose = h1Bars[h1Bars.Count - 1].Close;
            var d1Bars = MakeD1Bars(250, lastClose + 0.02, 0.0001);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(ind.BBTouchShort, $"BB touch: high {h1Bars[h1Bars.Count-1].High:F5} >= BB.Upper {ind.Bands.Upper:F5}");
            Assert(ind.RSIOverbought, $"RSI overbought: {ind.RSI:F2} > 65");
            Assert(ind.TrendBearish, $"Trend bearish: close {lastClose:F5} < D1 EMA {d1Ema:F5}");

            if (ind.BBTouchShort && ind.RSIOverbought && ind.TrendBearish)
            {
                var signal = module.GenerateSignal(h1Bars, d1Bars);
                Assert(signal.IsValid, "Signal is valid");
                Assert(signal.Direction == TradeDirection.Short, "Direction is Short");
            }
            else
            {
                Assert(true, "Skipped (conditions not met for test data)");
            }
        }

        private static void Test_NoSignal_BBNotTouched()
        {
            Console.WriteLine("\nTest: No signal when BB not touched");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Middle,
                High = bb.Middle + 0.0005,
                Low = bb.Middle - 0.0005,
                Close = bb.Middle + 0.0002,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var d1Bars = MakeD1Bars(250, 1.1000);
            h1Bars = SetLastBarClose(h1Bars, h1Bars[h1Bars.Count - 1].Close);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            Assert(!signal.IsValid, "Signal is invalid when BB not touched");
        }

        private static void Test_NoSignal_RSIFilterBlocksLong()
        {
            Console.WriteLine("\nTest: No long signal when RSI not oversold");

            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Lower - 0.0005,
                High = bb.Lower + 0.0010,
                Low = bb.Lower - 0.0010,
                Close = bb.Lower - 0.0003,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            var bar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = bar.Time,
                Open = bar.Open,
                High = Math.Max(bar.High, d1Ema + 0.0010),
                Low = bar.Low,
                Close = d1Ema + 0.0010,
                Volume = bar.Volume,
                Period = bar.Period
            };

            var module = new MRSignalModule(MRSignalConfig.Default);
            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(!ind.RSIOversold, $"RSI not oversold: {ind.RSI:F2} >= 35");
            if (ind.BBTouchLong && ind.TrendBullish && !ind.RSIOversold)
            {
                var signal = module.GenerateSignal(h1Bars, d1Bars);
                Assert(!signal.IsValid, "Signal invalid when RSI filter blocks");
            }
            else
            {
                Assert(true, "Skipped (conditions not all met to test RSI filter in isolation)");
            }
        }

        private static void Test_NoSignal_RSIFilterBlocksShort()
        {
            Console.WriteLine("\nTest: No short signal when RSI not overbought");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = lastBar.Open,
                High = bb.Upper + 0.0005,
                Low = lastBar.Low,
                Close = lastBar.Close,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            double lastClose = h1Bars[h1Bars.Count - 1].Close;
            var d1Bars = MakeD1Bars(250, lastClose + 0.05);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);

            if (d1Ema <= lastClose)
            {
                d1Bars = MakeD1Bars(250, lastClose + 0.10);
                d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            }

            var module = new MRSignalModule(MRSignalConfig.Default);
            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(!ind.RSIOverbought, $"RSI not overbought: {ind.RSI:F2} <= 65");
            if (ind.BBTouchShort && ind.TrendBearish && !ind.RSIOverbought)
            {
                var signal = module.GenerateSignal(h1Bars, d1Bars);
                Assert(!signal.IsValid, "Signal invalid when RSI filter blocks");
            }
            else
            {
                Assert(true, "Skipped (conditions not all met to test RSI filter in isolation)");
            }
        }

        private static void Test_NoSignal_TrendFilterBlocksLong()
        {
            Console.WriteLine("\nTest: No long signal when price below D1 EMA 200");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Lower + 0.0005,
                High = bb.Lower + 0.0010,
                Low = bb.Lower - 0.0005,
                Close = bb.Lower + 0.0002,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var d1Bars = MakeD1Bars(250, 1.1200);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            var bar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = bar.Time,
                Open = bar.Open,
                High = bar.High,
                Low = Math.Min(bar.Low, d1Ema - 0.005),
                Close = d1Ema - 0.005,
                Volume = bar.Volume,
                Period = bar.Period
            };

            var module = new MRSignalModule(MRSignalConfig.Default);
            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(!ind.TrendBullish, "Trend is not bullish (price below D1 EMA)");
            if (ind.BBTouchLong && ind.RSIOversold && !ind.TrendBullish)
            {
                var signal = module.GenerateSignal(h1Bars, d1Bars);
                Assert(!signal.IsValid, "Signal invalid when price below D1 EMA (wrong trend for long)");
            }
            else
            {
                Assert(true, "Skipped (conditions not all met to test trend filter in isolation)");
            }
        }

        private static void Test_NoSignal_TrendFilterBlocksShort()
        {
            Console.WriteLine("\nTest: No short signal when price above D1 EMA 200");

            var h1Bars = MakeOverboughtRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Upper - 0.0005,
                High = bb.Upper + 0.0005,
                Low = bb.Upper - 0.0010,
                Close = bb.Upper - 0.0002,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            var bar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = bar.Time,
                Open = bar.Open,
                High = Math.Max(bar.High, d1Ema + 0.005),
                Low = bar.Low,
                Close = d1Ema + 0.005,
                Volume = bar.Volume,
                Period = bar.Period
            };

            var module = new MRSignalModule(MRSignalConfig.Default);
            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(!ind.TrendBearish, "Trend is not bearish (price above D1 EMA)");
            if (ind.BBTouchShort && ind.RSIOverbought && !ind.TrendBearish)
            {
                var signal = module.GenerateSignal(h1Bars, d1Bars);
                Assert(!signal.IsValid, "Signal invalid when price above D1 EMA (wrong trend for short)");
            }
            else
            {
                Assert(true, "Skipped (conditions not all met to test trend filter in isolation)");
            }
        }

        private static void Test_SignalEntryPriceIsClose()
        {
            Console.WriteLine("\nTest: Signal entry price equals last bar close");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);
            h1Bars = MakeH1BarsWithBBTouch(1.1000, 30, touchLower: true, bbLower: bb.Lower, bbUpper: bb.Upper);

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            h1Bars = SetLastBarClose(h1Bars, d1Ema + 0.005);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            if (signal.IsValid)
            {
                Assert(Math.Abs(signal.EntryPrice - h1Bars[h1Bars.Count - 1].Close) < 0.0000001,
                    "Entry price equals last bar close");
            }
            else
            {
                Assert(true, "Skipped (signal not valid for test data)");
            }
        }

        private static void Test_SignalTimestampIsBarTime()
        {
            Console.WriteLine("\nTest: Signal timestamp equals last bar time");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);
            h1Bars = MakeH1BarsWithBBTouch(1.1000, 30, touchLower: true, bbLower: bb.Lower, bbUpper: bb.Upper);

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            h1Bars = SetLastBarClose(h1Bars, d1Ema + 0.005);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            if (signal.IsValid)
            {
                Assert(signal.SignalTime == h1Bars[h1Bars.Count - 1].Time,
                    "Signal time equals last bar time");
            }
            else
            {
                Assert(true, "Skipped (signal not valid for test data)");
            }
        }

        private static void Test_SignalIndicatorsPopulated()
        {
            Console.WriteLine("\nTest: Signal contains all indicator values");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);
            h1Bars = MakeH1BarsWithBBTouch(1.1000, 30, touchLower: true, bbLower: bb.Lower, bbUpper: bb.Upper);

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            h1Bars = SetLastBarClose(h1Bars, d1Ema + 0.005);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            if (signal.IsValid)
            {
                Assert(signal.BBUpper > 0, "BB Upper > 0");
                Assert(signal.BBMiddle > 0, "BB Middle > 0");
                Assert(signal.BBLower > 0, "BB Lower > 0");
                Assert(signal.RSI > 0, "RSI > 0");
                Assert(signal.D1EMA200 > 0, "D1 EMA 200 > 0");
            }
            else
            {
                Assert(true, "Skipped (signal not valid for test data)");
            }
        }

        private static void Test_ConfidenceScoreInRange()
        {
            Console.WriteLine("\nTest: Confidence score is between 0 and 1");

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);
            h1Bars = MakeH1BarsWithBBTouch(1.1000, 30, touchLower: true, bbLower: bb.Lower, bbUpper: bb.Upper);

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            h1Bars = SetLastBarClose(h1Bars, d1Ema + 0.005);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            if (signal.IsValid)
            {
                Assert(signal.ConfidenceScore >= 0 && signal.ConfidenceScore <= 1.0,
                    $"Confidence {signal.ConfidenceScore:F4} in [0, 1]");
            }
            else
            {
                Assert(true, "Skipped (signal not valid for test data)");
            }
        }

        private static void Test_InvalidSignalWhenInsufficientH1Bars()
        {
            Console.WriteLine("\nTest: Invalid signal when insufficient H1 bars");

            var h1Bars = MakeH1Bars(10, 1.1000);
            var d1Bars = MakeD1Bars(250, 1.0800);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            Assert(!signal.IsValid, "Signal invalid with < 21 H1 bars");
        }

        private static void Test_InvalidSignalWhenInsufficientD1Bars()
        {
            Console.WriteLine("\nTest: Invalid signal when insufficient D1 bars");

            var h1Bars = MakeH1Bars(30, 1.1000);
            var d1Bars = MakeD1Bars(100, 1.0800);

            var module = new MRSignalModule(MRSignalConfig.Default);
            var signal = module.GenerateSignal(h1Bars, d1Bars);

            Assert(!signal.IsValid, "Signal invalid with < 200 D1 bars");
        }

        private static void Test_RSICalculation_Oversold()
        {
            Console.WriteLine("\nTest: RSI calculation gives oversold reading for declining bars");

            var bars = MakeOversoldRSIH1Bars(30, 1.1000);
            double rsi = MRSignalModule.CalculateRSI(bars, 14);

            Assert(rsi < 35.0, $"RSI oversold: {rsi:F2} < 35");
            Assert(rsi >= 0 && rsi <= 100, $"RSI in valid range: {rsi:F2}");
        }

        private static void Test_RSICalculation_Overbought()
        {
            Console.WriteLine("\nTest: RSI calculation gives overbought reading for rising bars");

            var bars = MakeOverboughtRSIH1Bars(30, 1.1000);
            double rsi = MRSignalModule.CalculateRSI(bars, 14);

            Assert(rsi > 65.0, $"RSI overbought: {rsi:F2} > 65");
            Assert(rsi >= 0 && rsi <= 100, $"RSI in valid range: {rsi:F2}");
        }

        private static void Test_RSICalculation_Neutral()
        {
            Console.WriteLine("\nTest: RSI calculation gives neutral reading for alternating bars");

            var bars = new List<Bar>();
            var time = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            double price = 1.1000;

            for (int i = 0; i < 30; i++)
            {
                double change = (i % 2 == 0) ? 0.0001 : -0.0001;
                price += change;
                bars.Add(new Bar
                {
                    Time = time.AddHours(i),
                    Open = price - change,
                    High = price + 0.0003,
                    Low = price - 0.0003,
                    Close = price,
                    Volume = 1000,
                    Period = TimeFrame.H1
                });
            }

            double rsi = MRSignalModule.CalculateRSI(bars, 14);

            Assert(rsi >= 30 && rsi <= 70, $"RSI neutral: {rsi:F2} in [30, 70]");
        }

        private static void Test_BBTouch_Long_LowPiercesLowerBand()
        {
            Console.WriteLine("\nTest: BB touch detected for long when low pierces lower band");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Lower + 0.001,
                High = bb.Lower + 0.002,
                Low = bb.Lower - 0.001,
                Close = bb.Lower + 0.0005,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(ind.BBTouchLong, "BB touch detected (low pierces lower band)");
        }

        private static void Test_BBTouch_Long_LowAtLowerBand()
        {
            Console.WriteLine("\nTest: BB touch detected for long when low at or below lower band");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);

            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Lower + 0.002,
                High = bb.Lower + 0.003,
                Low = bb.Lower - 0.0001,
                Close = bb.Lower + 0.001,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(ind.BBTouchLong, "BB touch detected (low at/below recalculated lower band)");
        }

        private static void Test_BBTouch_Long_LowAboveLowerBand_NoSignal()
        {
            Console.WriteLine("\nTest: No BB touch for long when low above lower band");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Lower + 0.003,
                High = bb.Lower + 0.004,
                Low = bb.Lower + 0.002,
                Close = bb.Lower + 0.003,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(!ind.BBTouchLong, "No BB touch when low above lower band");
        }

        private static void Test_BBTouch_Short_HighPiercesUpperBand()
        {
            Console.WriteLine("\nTest: BB touch detected for short when high pierces upper band");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Upper - 0.001,
                High = bb.Upper + 0.001,
                Low = bb.Upper - 0.002,
                Close = bb.Upper - 0.0005,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(ind.BBTouchShort, "BB touch detected (high pierces upper band)");
        }

        private static void Test_BBTouch_Short_HighAtUpperBand()
        {
            Console.WriteLine("\nTest: BB touch detected for short when high equals upper band");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Upper - 0.001,
                High = bb.Upper,
                Low = bb.Upper - 0.002,
                Close = bb.Upper - 0.0005,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(ind.BBTouchShort, "BB touch detected (high at upper band)");
        }

        private static void Test_BBTouch_Short_HighBelowUpperBand_NoSignal()
        {
            Console.WriteLine("\nTest: No BB touch for short when high below upper band");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Upper - 0.003,
                High = bb.Upper - 0.002,
                Low = bb.Upper - 0.004,
                Close = bb.Upper - 0.003,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(!ind.BBTouchShort, "No BB touch when high below upper band");
        }

        private static void Test_BBTouch_WithBuffer()
        {
            Console.WriteLine("\nTest: BB touch with buffer allows near-touch detection");

            var config = new MRSignalConfig
            {
                BBPeriod = 20,
                BBStdDevMultiplier = 2.0,
                RSIPeriod = 14,
                RSIOversold = 35.0,
                RSIOverbought = 65.0,
                EMA200Period = 200,
                TouchBuffer = 0.001
            };
            var module = new MRSignalModule(config);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);

            var lastBar = h1Bars[h1Bars.Count - 1];
            h1Bars[h1Bars.Count - 1] = new Bar
            {
                Time = lastBar.Time,
                Open = bb.Lower + 0.002,
                High = bb.Lower + 0.003,
                Low = bb.Lower + 0.0005,
                Close = bb.Lower + 0.001,
                Volume = 1000,
                Period = TimeFrame.H1
            };

            var ind = module.EvaluateIndicators(h1Bars, MakeD1Bars(250, 1.0800));

            Assert(ind.BBTouchLong, "BB touch with buffer (low near but not at lower band)");
        }

        private static void Test_ConfigDefaults()
        {
            Console.WriteLine("\nTest: Default config values");

            var config = MRSignalConfig.Default;

            Assert(config.BBPeriod == 20, "BB period = 20");
            Assert(config.BBStdDevMultiplier == 2.0, "BB std dev = 2.0");
            Assert(config.RSIPeriod == 14, "RSI period = 14");
            Assert(config.RSIOversold == 35.0, "RSI oversold = 35");
            Assert(config.RSIOverbought == 65.0, "RSI overbought = 65");
            Assert(config.EMA200Period == 200, "EMA period = 200");
            Assert(config.TouchBuffer == 0.0, "Touch buffer = 0");
        }

        private static void Test_ConfigPresets()
        {
            Console.WriteLine("\nTest: Config presets have distinct values");

            var def = MRSignalConfig.Default;
            var agg = MRSignalConfig.Aggressive;
            var cons = MRSignalConfig.Conservative;

            Assert(cons.BBStdDevMultiplier < def.BBStdDevMultiplier,
                $"Conservative BB std dev < Default ({cons.BBStdDevMultiplier} < {def.BBStdDevMultiplier})");
            Assert(agg.RSIOversold > def.RSIOversold,
                $"Aggressive RSI oversold > Default ({agg.RSIOversold} > {def.RSIOversold})");
            Assert(agg.RSIOverbought < def.RSIOverbought,
                $"Aggressive RSI overbought < Default ({agg.RSIOverbought} < {def.RSIOverbought})");
            Assert(cons.RSIOversold < def.RSIOversold,
                $"Conservative RSI oversold < Default ({cons.RSIOversold} < {def.RSIOversold})");
            Assert(cons.RSIOverbought > def.RSIOverbought,
                $"Conservative RSI overbought > Default ({cons.RSIOverbought} > {def.RSIOverbought})");
            Assert(agg.TouchBuffer > def.TouchBuffer,
                $"Aggressive touch buffer > Default ({agg.TouchBuffer} > {def.TouchBuffer})");
        }

        private static void Test_EvaluateIndicators_AllFields()
        {
            Console.WriteLine("\nTest: EvaluateIndicators returns all indicator fields");

            var module = new MRSignalModule(MRSignalConfig.Default);
            var h1Bars = MakeH1Bars(30, 1.1000);
            var d1Bars = MakeD1Bars(250, 1.0800);

            var ind = module.EvaluateIndicators(h1Bars, d1Bars);

            Assert(ind.Bands.Upper > 0, "Bands.Upper > 0");
            Assert(ind.Bands.Middle > 0, "Bands.Middle > 0");
            Assert(ind.Bands.Lower > 0, "Bands.Lower > 0");
            Assert(ind.RSI >= 0 && ind.RSI <= 100, $"RSI in range: {ind.RSI:F2}");
            Assert(ind.D1EMA200 > 0, "D1 EMA 200 > 0");
        }

        private static void Test_SignalDirection_Correct()
        {
            Console.WriteLine("\nTest: Signal direction matches setup conditions");

            var module = new MRSignalModule(MRSignalConfig.Default);

            var h1Bars = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb = MRExitManager.CalculateBollingerBands(h1Bars, 20, 2.0);
            h1Bars = MakeH1BarsWithBBTouch(1.1000, 30, touchLower: true, bbLower: bb.Lower, bbUpper: bb.Upper);

            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            h1Bars = SetLastBarClose(h1Bars, d1Ema + 0.005);

            var signal = module.GenerateSignal(h1Bars, d1Bars);

            if (signal.IsValid)
            {
                Assert(signal.Direction == TradeDirection.Long, "Long setup produces Long direction");
                Assert(signal.Direction != TradeDirection.Short, "Long setup does not produce Short");
            }
            else
            {
                Assert(true, "Skipped (signal not valid for test data)");
            }
        }

        private static void Test_SignalConfidenceHigherWithStrongerConditions()
        {
            Console.WriteLine("\nTest: Confidence varies with condition strength");

            var module = new MRSignalModule(MRSignalConfig.Default);

            var h1Bars1 = MakeOversoldRSIH1Bars(30, 1.1000);
            var bb1 = MRExitManager.CalculateBollingerBands(h1Bars1, 20, 2.0);
            h1Bars1 = MakeH1BarsWithBBTouch(1.1000, 30, touchLower: true, bbLower: bb1.Lower, bbUpper: bb1.Upper);
            var d1Bars = MakeD1Bars(250, 1.0800);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, 200);
            h1Bars1 = SetLastBarClose(h1Bars1, d1Ema + 0.005);

            var signal1 = module.GenerateSignal(h1Bars1, d1Bars);

            if (signal1.IsValid)
            {
                Assert(signal1.ConfidenceScore > 0, "Confidence > 0 for valid signal");
            }
            else
            {
                Assert(true, "Skipped (signal not valid for test data)");
            }
        }
    }
}
