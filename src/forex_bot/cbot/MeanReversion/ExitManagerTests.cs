using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public static class MRExitManagerTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== Mean Reversion Exit Manager Tests ===\n");

            Test_StopLossHitLong();
            Test_StopLossHitShort();
            Test_TP1PartialCloseLong();
            Test_TP1PartialCloseShort();
            Test_TP2FullCloseLong();
            Test_TP2FullCloseShort();
            Test_TP2OnlyAfterTP1();
            Test_TimeStopClosesPosition();
            Test_TimeStopDisabledAfterTP1();
            Test_StructureBreakLong();
            Test_StructureBreakShort();
            Test_StructureBreakNoTriggerWhenAligned();
            Test_TrailingStopToBreakeven();
            Test_CalculateStopLossLong();
            Test_CalculateStopLossShort();
            Test_CalculateBollingerBands();
            Test_CalculateATR();
            Test_CalculateEMA();
            Test_StopLossPriorityOverTP1();
            Test_PartialCloseUpdatesLotSize();
            Test_ConfigDefaults();
            Test_ConfigPresets();

            Console.WriteLine($"\n=== MR Exit Manager Results: {Passed} passed, {Failed} failed ===");
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

        private static List<Bar> MakeBars(int count, double basePrice, double closeStep = 0.0001)
        {
            var bars = new List<Bar>();
            var time = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            for (int i = 0; i < count; i++)
            {
                bars.Add(new Bar
                {
                    Time = time.AddHours(i),
                    Open = basePrice,
                    High = basePrice + closeStep * 2,
                    Low = basePrice - closeStep * 2,
                    Close = basePrice + closeStep * i,
                    Volume = 1000,
                    Period = TimeFrame.H1
                });
            }
            return bars;
        }

        private static MRTradeState MakeLongTrade(double entry, double sl, int barsSinceEntry = 0)
        {
            return new MRTradeState
            {
                Direction = TradeDirection.Long,
                EntryPrice = entry,
                CurrentStopLoss = sl,
                LotSize = 0.1,
                TP1Hit = false,
                PartialClosed = false,
                PartialClosePrice = 0,
                PartialClosePnL = 0,
                BarsSinceEntry = barsSinceEntry,
                OriginalATR = 0.005,
                StructureBreakTriggered = false
            };
        }

        private static MRTradeState MakeShortTrade(double entry, double sl, int barsSinceEntry = 0)
        {
            return new MRTradeState
            {
                Direction = TradeDirection.Short,
                EntryPrice = entry,
                CurrentStopLoss = sl,
                LotSize = 0.1,
                TP1Hit = false,
                PartialClosed = false,
                PartialClosePrice = 0,
                PartialClosePnL = 0,
                BarsSinceEntry = barsSinceEntry,
                OriginalATR = 0.005,
                StructureBreakTriggered = false
            };
        }

        private static void Test_StopLossHitLong()
        {
            Console.WriteLine("\nTest: Stop loss hit for long trade");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0980);
            var bar = new Bar { Close = 1.0975, High = 1.0990, Low = 1.0970 };

            var signal = manager.CheckExit(ref trade, bar, new BollingerBands { Middle = 1.1000 }, 1.0950, 0.005);

            Assert(signal.ShouldClose, "Should close on SL hit");
            Assert(!signal.ShouldPartialClose, "No partial close on SL");
            Assert(Math.Abs(signal.ExitPrice - 1.0980) < 0.00001, "Exit at SL price");
            Assert(signal.Reason == MRExitReason.StopLoss, "Reason is StopLoss");
        }

        private static void Test_StopLossHitShort()
        {
            Console.WriteLine("\nTest: Stop loss hit for short trade");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeShortTrade(1.1000, 1.1020);
            var bar = new Bar { Close = 1.1025, High = 1.1030, Low = 1.1010 };

            var signal = manager.CheckExit(ref trade, bar, new BollingerBands { Middle = 1.1000 }, 1.1050, 0.005);

            Assert(signal.ShouldClose, "Should close on SL hit");
            Assert(signal.Reason == MRExitReason.StopLoss, "Reason is StopLoss");
            Assert(Math.Abs(signal.ExitPrice - 1.1020) < 0.00001, "Exit at SL price");
        }

        private static void Test_TP1PartialCloseLong()
        {
            Console.WriteLine("\nTest: TP1 triggers partial close for long");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0980);
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1055, High = 1.1060, Low = 1.1040 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(signal.ShouldPartialClose, "Should partial close at TP1");
            Assert(!signal.ShouldClose, "Should not fully close at TP1");
            Assert(Math.Abs(signal.PartialClosePrice - 1.1050) < 0.00001, "Partial close at middle BB");
            Assert(Math.Abs(signal.PartialClosePct - 0.5) < 0.00001, "Partial close is 50%");
            Assert(signal.Reason == MRExitReason.TakeProfit1, "Reason is TP1");
        }

        private static void Test_TP1PartialCloseShort()
        {
            Console.WriteLine("\nTest: TP1 triggers partial close for short");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeShortTrade(1.1000, 1.1020);
            var bb = new BollingerBands { Middle = 1.0950, Upper = 1.1000, Lower = 1.0900 };
            var bar = new Bar { Close = 1.0945, High = 1.0960, Low = 1.0940 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.1050, 0.005);

            Assert(signal.ShouldPartialClose, "Should partial close at TP1");
            Assert(!signal.ShouldClose, "Should not fully close at TP1");
            Assert(Math.Abs(signal.PartialClosePrice - 1.0950) < 0.00001, "Partial close at middle BB");
        }

        private static void Test_TP2FullCloseLong()
        {
            Console.WriteLine("\nTest: TP2 triggers full close for long after TP1");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0990);
            trade.TP1Hit = true;
            trade.PartialClosed = true;
            trade.LotSize = 0.05;
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1005, High = 1.1010, Low = 1.0995 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(signal.ShouldClose, "Should fully close at TP2");
            Assert(Math.Abs(signal.ExitPrice - 1.1000) < 0.00001, "Exit at lower BB");
            Assert(signal.Reason == MRExitReason.TakeProfit2, "Reason is TP2");
        }

        private static void Test_TP2FullCloseShort()
        {
            Console.WriteLine("\nTest: TP2 triggers full close for short after TP1");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeShortTrade(1.1000, 1.1010);
            trade.TP1Hit = true;
            trade.PartialClosed = true;
            trade.LotSize = 0.05;
            var bb = new BollingerBands { Middle = 1.0950, Upper = 1.1000, Lower = 1.0900 };
            var bar = new Bar { Close = 1.0995, High = 1.1005, Low = 1.0990 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.1050, 0.005);

            Assert(signal.ShouldClose, "Should fully close at TP2");
            Assert(Math.Abs(signal.ExitPrice - 1.1000) < 0.00001, "Exit at upper BB");
            Assert(signal.Reason == MRExitReason.TakeProfit2, "Reason is TP2");
        }

        private static void Test_TP2OnlyAfterTP1()
        {
            Console.WriteLine("\nTest: TP2 does not trigger without TP1 hit");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0980);
            trade.TP1Hit = false;
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1005, High = 1.1010, Low = 1.0995 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(!signal.ShouldClose, "Should not close at TP2 without TP1");
            Assert(signal.Reason != MRExitReason.TakeProfit2, "Reason is not TP2");
        }

        private static void Test_TimeStopClosesPosition()
        {
            Console.WriteLine("\nTest: Time stop closes position after 12 candles");
            var config = MRExitConfig.Default;
            var manager = new MRExitManager(config);
            var trade = MakeLongTrade(1.1000, 1.0980, barsSinceEntry: 13);
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1000, High = 1.1010, Low = 1.0990 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(signal.ShouldClose, "Should close on time stop");
            Assert(Math.Abs(signal.ExitPrice - 1.1000) < 0.00001, "Exit at current close");
            Assert(signal.Reason == MRExitReason.TimeStop, "Reason is TimeStop");
        }

        private static void Test_TimeStopDisabledAfterTP1()
        {
            Console.WriteLine("\nTest: Time stop does not trigger after TP1 hit");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.1000, barsSinceEntry: 20);
            trade.TP1Hit = true;
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1020, High = 1.1030, Low = 1.1010 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(signal.Reason != MRExitReason.TimeStop, "Time stop not triggered after TP1");
        }

        private static void Test_StructureBreakLong()
        {
            Console.WriteLine("\nTest: Structure break closes long when price crosses below D1 EMA 200");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0920);
            var bb = new BollingerBands { Middle = 1.1000, Upper = 1.1050, Lower = 1.0950 };
            var bar = new Bar { Close = 1.0940, High = 1.0960, Low = 1.0930 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(signal.ShouldClose, "Should close on structure break");
            Assert(Math.Abs(signal.ExitPrice - 1.0940) < 0.00001, "Exit at close price");
            Assert(signal.Reason == MRExitReason.StructureBreak, "Reason is StructureBreak");
        }

        private static void Test_StructureBreakShort()
        {
            Console.WriteLine("\nTest: Structure break closes short when price crosses above D1 EMA 200");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeShortTrade(1.1000, 1.1080);
            var bb = new BollingerBands { Middle = 1.1000, Upper = 1.1050, Lower = 1.0950 };
            var bar = new Bar { Close = 1.1060, High = 1.1070, Low = 1.1050 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.1050, 0.005);

            Assert(signal.ShouldClose, "Should close on structure break");
            Assert(signal.Reason == MRExitReason.StructureBreak, "Reason is StructureBreak");
        }

        private static void Test_StructureBreakNoTriggerWhenAligned()
        {
            Console.WriteLine("\nTest: No structure break when price aligned with EMA 200");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0980);
            var bb = new BollingerBands { Middle = 1.1060, Upper = 1.1100, Lower = 1.1020 };
            var bar = new Bar { Close = 1.1020, High = 1.1030, Low = 1.1010 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(!signal.ShouldClose, "Should not close - price above EMA for long");
            Assert(signal.Reason != MRExitReason.StructureBreak, "Reason is not StructureBreak");
        }

        private static void Test_TrailingStopToBreakeven()
        {
            Console.WriteLine("\nTest: Trailing stop moves SL to breakeven after TP1");
            var config = MRExitConfig.Default;
            config.TrailingToBreakeven = true;
            var manager = new MRExitManager(config);
            var trade = MakeLongTrade(1.1000, 1.0980);
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1055, High = 1.1060, Low = 1.1040 };

            manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(Math.Abs(trade.CurrentStopLoss - 1.1000) < 0.00001, "SL moved to entry (breakeven)");
            Assert(trade.PartialClosed, "Trade marked as partially closed");
            Assert(trade.TP1Hit, "TP1 marked as hit");
        }

        private static void Test_CalculateStopLossLong()
        {
            Console.WriteLine("\nTest: Calculate stop loss for long");
            var manager = new MRExitManager(MRExitConfig.Default);
            double sl = manager.CalculateStopLoss(TradeDirection.Long, 1.1000, 0.005);

            Assert(Math.Abs(sl - 1.0900) < 0.00001, $"SL = entry - 2*ATR = 1.0900 (got {sl:F5})");
        }

        private static void Test_CalculateStopLossShort()
        {
            Console.WriteLine("\nTest: Calculate stop loss for short");
            var manager = new MRExitManager(MRExitConfig.Default);
            double sl = manager.CalculateStopLoss(TradeDirection.Short, 1.1000, 0.005);

            Assert(Math.Abs(sl - 1.1100) < 0.00001, $"SL = entry + 2*ATR = 1.1100 (got {sl:F5})");
        }

        private static void Test_CalculateBollingerBands()
        {
            Console.WriteLine("\nTest: Calculate Bollinger Bands");
            var bars = MakeBars(30, 1.1000, 0.0001);
            var bb = MRExitManager.CalculateBollingerBands(bars, 20, 2.0);

            Assert(bb.Middle > 0, "Middle band (SMA) > 0");
            Assert(bb.Upper > bb.Middle, "Upper > Middle");
            Assert(bb.Lower < bb.Middle, "Lower < Middle");
            Assert(Math.Abs(bb.Upper - bb.Middle - (bb.Middle - bb.Lower)) < 0.0000001,
                "Upper - Middle == Middle - Lower (symmetric)");
        }

        private static void Test_CalculateATR()
        {
            Console.WriteLine("\nTest: Calculate ATR");
            var bars = MakeBars(30, 1.1000, 0.001);
            double atr = MRExitManager.CalculateATR(bars, 14);

            Assert(atr > 0, "ATR > 0");
            Assert(!double.IsNaN(atr), "ATR is not NaN");
        }

        private static void Test_CalculateEMA()
        {
            Console.WriteLine("\nTest: Calculate EMA 200");
            var bars = MakeBars(250, 1.1000, 0.0001);
            double ema = MRExitManager.CalculateEMA(bars, 200);

            Assert(ema > 0, "EMA > 0");
            Assert(!double.IsNaN(ema), "EMA is not NaN");
            Assert(ema > 1.0 && ema < 1.2, "EMA in reasonable range for test data");
        }

        private static void Test_StopLossPriorityOverTP1()
        {
            Console.WriteLine("\nTest: Stop loss has priority over TP1");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0980);
            var bb = new BollingerBands { Middle = 1.0985, Upper = 1.1000, Lower = 1.0970 };
            var bar = new Bar { Close = 1.0975, High = 1.0990, Low = 1.0960 };

            var signal = manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(signal.ShouldClose, "Should close");
            Assert(signal.Reason == MRExitReason.StopLoss, "SL takes priority over TP1");
            Assert(!signal.ShouldPartialClose, "No partial close when SL hits");
        }

        private static void Test_PartialCloseUpdatesLotSize()
        {
            Console.WriteLine("\nTest: Partial close reduces lot size by 50%");
            var manager = new MRExitManager(MRExitConfig.Default);
            var trade = MakeLongTrade(1.1000, 1.0980);
            double originalLots = trade.LotSize;
            var bb = new BollingerBands { Middle = 1.1050, Upper = 1.1100, Lower = 1.1000 };
            var bar = new Bar { Close = 1.1055, High = 1.1060, Low = 1.1040 };

            manager.CheckExit(ref trade, bar, bb, 1.0950, 0.005);

            Assert(Math.Abs(trade.LotSize - originalLots * 0.5) < 0.000001,
                $"Lot size halved (was {originalLots}, now {trade.LotSize})");
        }

        private static void Test_ConfigDefaults()
        {
            Console.WriteLine("\nTest: Default config values");
            var config = MRExitConfig.Default;

            Assert(config.ATRMultiplier == 2.0, "ATR multiplier = 2.0");
            Assert(config.BBPeriod == 20, "BB period = 20");
            Assert(config.BBStdDevMultiplier == 2.0, "BB std dev = 2.0");
            Assert(config.TimeStopCandles == 12, "Time stop = 12 candles");
            Assert(config.PartialClosePct == 0.5, "Partial close = 50%");
            Assert(config.TrailingToBreakeven, "Trailing to breakeven enabled");
        }

        private static void Test_ConfigPresets()
        {
            Console.WriteLine("\nTest: Config presets have distinct values");
            var def = MRExitConfig.Default;
            var agg = MRExitConfig.Aggressive;
            var cons = MRExitConfig.Conservative;

            Assert(agg.ATRMultiplier < def.ATRMultiplier,
                $"Aggressive ATR < Default ({agg.ATRMultiplier} < {def.ATRMultiplier})");
            Assert(cons.ATRMultiplier > def.ATRMultiplier,
                $"Conservative ATR > Default ({cons.ATRMultiplier} > {def.ATRMultiplier})");
            Assert(agg.TimeStopCandles < def.TimeStopCandles,
                $"Aggressive time stop < Default ({agg.TimeStopCandles} < {def.TimeStopCandles})");
            Assert(cons.TimeStopCandles > def.TimeStopCandles,
                $"Conservative time stop > Default ({cons.TimeStopCandles} > {def.TimeStopCandles})");
        }
    }
}
