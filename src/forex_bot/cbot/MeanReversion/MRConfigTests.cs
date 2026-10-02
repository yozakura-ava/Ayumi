using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public static class MRConfigTests
    {
        public static int Passed { get; private set; }
        public static int Failed { get; private set; }

        public static void RunAll()
        {
            Passed = 0;
            Failed = 0;

            Console.WriteLine("=== Mean Reversion Config System Tests ===\n");

            Test_ConservativePresetValues();
            Test_ModeratePresetValues();
            Test_AggressivePresetValues();
            Test_DefaultIsConservative();
            Test_AllPresetsReturnsThree();
            Test_ToSignalConfig_Conservative();
            Test_ToSignalConfig_Aggressive();
            Test_ToExitConfig_Conservative();
            Test_ToExitConfig_Aggressive();
            Test_ToBacktestConfig_Conservative();
            Test_ToBacktestConfig_Aggressive();
            Test_ToBacktestConfig_RiskPerTrade();
            Test_PresetNames();
            Test_PresetEnumValues();
            Test_SignalConfigRoundTrip();
            Test_ExitConfigRoundTrip();
            Test_BacktestConfigDailyDrawdown();
            Test_PrintSummary_DoesNotThrow();

            Console.WriteLine($"\n=== MR Config Results: {Passed} passed, {Failed} failed ===");
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

        private static void AssertEqual(double expected, double actual, string testName, double tolerance = 0.0001)
        {
            Assert(Math.Abs(expected - actual) < tolerance, $"{testName} (expected {expected}, got {actual})");
        }

        private static void Test_ConservativePresetValues()
        {
            Console.WriteLine("\nTest: Conservative preset values");
            var c = MRConfig.Conservative;

            AssertEqual(20, c.BBPeriod, "BB period");
            AssertEqual(2.0, c.BBStdDevMultiplier, "BB std dev");
            AssertEqual(14, c.RSIPeriod, "RSI period");
            AssertEqual(35.0, c.RSIOversold, "RSI oversold");
            AssertEqual(65.0, c.RSIOverbought, "RSI overbought");
            AssertEqual(200, c.EMAPeriod, "EMA period");
            AssertEqual(14, c.ATRPeriod, "ATR period");
            AssertEqual(2.0, c.SLATRMultiplier, "SL ATR multiplier");
            AssertEqual(12, c.TimeStopCandles, "Time stop candles");
            AssertEqual(0.005, c.RiskPerTradePct, "Risk per trade");
            AssertEqual(0.0, c.TouchBuffer, "Touch buffer");
            Assert(c.TrailingToBreakeven, "Trail to breakeven");
        }

        private static void Test_ModeratePresetValues()
        {
            Console.WriteLine("\nTest: Moderate preset values");
            var m = MRConfig.Moderate;

            AssertEqual(20, m.BBPeriod, "BB period");
            AssertEqual(2.0, m.BBStdDevMultiplier, "BB std dev");
            AssertEqual(14, m.RSIPeriod, "RSI period");
            AssertEqual(40.0, m.RSIOversold, "RSI oversold");
            AssertEqual(60.0, m.RSIOverbought, "RSI overbought");
            AssertEqual(200, m.EMAPeriod, "EMA period");
            AssertEqual(14, m.ATRPeriod, "ATR period");
            AssertEqual(1.5, m.SLATRMultiplier, "SL ATR multiplier");
            AssertEqual(8, m.TimeStopCandles, "Time stop candles");
            AssertEqual(0.0075, m.RiskPerTradePct, "Risk per trade");
            AssertEqual(0.0, m.TouchBuffer, "Touch buffer");
        }

        private static void Test_AggressivePresetValues()
        {
            Console.WriteLine("\nTest: Aggressive preset values");
            var a = MRConfig.Aggressive;

            AssertEqual(20, a.BBPeriod, "BB period");
            AssertEqual(1.8, a.BBStdDevMultiplier, "BB std dev");
            AssertEqual(14, a.RSIPeriod, "RSI period");
            AssertEqual(45.0, a.RSIOversold, "RSI oversold");
            AssertEqual(55.0, a.RSIOverbought, "RSI overbought");
            AssertEqual(100, a.EMAPeriod, "EMA period");
            AssertEqual(10, a.ATRPeriod, "ATR period");
            AssertEqual(1.2, a.SLATRMultiplier, "SL ATR multiplier");
            AssertEqual(6, a.TimeStopCandles, "Time stop candles");
            AssertEqual(0.01, a.RiskPerTradePct, "Risk per trade");
            AssertEqual(0.0002, a.TouchBuffer, "Touch buffer");
        }

        private static void Test_DefaultIsConservative()
        {
            Console.WriteLine("\nTest: Default preset equals Conservative");

            var def = MRConfig.Default;
            var cons = MRConfig.Conservative;

            AssertEqual(cons.BBPeriod, def.BBPeriod, "BB period matches");
            AssertEqual(cons.RSIOversold, def.RSIOversold, "RSI oversold matches");
            AssertEqual(cons.SLATRMultiplier, def.SLATRMultiplier, "SL ATR mult matches");
            AssertEqual(cons.RiskPerTradePct, def.RiskPerTradePct, "Risk per trade matches");
        }

        private static void Test_AllPresetsReturnsThree()
        {
            Console.WriteLine("\nTest: AllPresets returns 3 configs");

            var all = MRConfig.AllPresets;
            Assert(all.Length == 3, $"3 presets (got {all.Length})");
        }

        private static void Test_ToSignalConfig_Conservative()
        {
            Console.WriteLine("\nTest: Conservative ToSignalConfig maps correctly");

            var config = MRConfig.Conservative;
            var signal = config.ToSignalConfig();

            AssertEqual(config.BBPeriod, signal.BBPeriod, "BB period");
            AssertEqual(config.BBStdDevMultiplier, signal.BBStdDevMultiplier, "BB std dev");
            AssertEqual(config.RSIPeriod, signal.RSIPeriod, "RSI period");
            AssertEqual(config.RSIOversold, signal.RSIOversold, "RSI oversold");
            AssertEqual(config.RSIOverbought, signal.RSIOverbought, "RSI overbought");
            AssertEqual(config.EMAPeriod, signal.EMA200Period, "EMA period");
            AssertEqual(config.TouchBuffer, signal.TouchBuffer, "Touch buffer");
        }

        private static void Test_ToSignalConfig_Aggressive()
        {
            Console.WriteLine("\nTest: Aggressive ToSignalConfig maps correctly");

            var config = MRConfig.Aggressive;
            var signal = config.ToSignalConfig();

            AssertEqual(config.BBStdDevMultiplier, signal.BBStdDevMultiplier, "BB std dev");
            AssertEqual(config.RSIOversold, signal.RSIOversold, "RSI oversold");
            AssertEqual(config.EMAPeriod, signal.EMA200Period, "EMA period");
            AssertEqual(config.TouchBuffer, signal.TouchBuffer, "Touch buffer");
        }

        private static void Test_ToExitConfig_Conservative()
        {
            Console.WriteLine("\nTest: Conservative ToExitConfig maps correctly");

            var config = MRConfig.Conservative;
            var exit = config.ToExitConfig();

            AssertEqual(config.ATRPeriod, exit.ATRPeriod, "ATR period");
            AssertEqual(config.SLATRMultiplier, exit.ATRMultiplier, "SL ATR multiplier");
            AssertEqual(config.BBPeriod, exit.BBPeriod, "BB period");
            AssertEqual(config.BBStdDevMultiplier, exit.BBStdDevMultiplier, "BB std dev");
            AssertEqual(config.TimeStopCandles, exit.TimeStopCandles, "Time stop");
            AssertEqual(config.EMAPeriod, exit.EMA200Period, "EMA period");
            AssertEqual(config.PartialClosePct, exit.PartialClosePct, "Partial close");
            Assert(config.TrailingToBreakeven == exit.TrailingToBreakeven, "Trail to BE");
        }

        private static void Test_ToExitConfig_Aggressive()
        {
            Console.WriteLine("\nTest: Aggressive ToExitConfig maps correctly");

            var config = MRConfig.Aggressive;
            var exit = config.ToExitConfig();

            AssertEqual(config.ATRPeriod, exit.ATRPeriod, "ATR period");
            AssertEqual(config.SLATRMultiplier, exit.ATRMultiplier, "SL ATR multiplier");
            AssertEqual(config.TimeStopCandles, exit.TimeStopCandles, "Time stop");
        }

        private static void Test_ToBacktestConfig_Conservative()
        {
            Console.WriteLine("\nTest: Conservative ToBacktestConfig maps correctly");

            var config = MRConfig.Conservative;
            var bt = config.ToBacktestConfig();

            AssertEqual(10000, bt.StartingBalance, "Starting balance");
            AssertEqual(config.RiskPerTradePct, bt.RiskPerTradePct, "Risk per trade");
            Assert(bt.MaxDailyDrawdownPct > 0, "Max daily DD > 0");
            Assert(bt.MaxTotalDrawdownPct > 0, "Max total DD > 0");
            Assert(bt.PartialCloseEnabled, "Partial close enabled");
            AssertEqual(config.PartialClosePct, bt.PartialClosePct, "Partial close pct");
        }

        private static void Test_ToBacktestConfig_Aggressive()
        {
            Console.WriteLine("\nTest: Aggressive ToBacktestConfig maps correctly");

            var config = MRConfig.Aggressive;
            var bt = config.ToBacktestConfig();

            AssertEqual(config.RiskPerTradePct, bt.RiskPerTradePct, "Risk per trade");
            Assert(bt.MinConfidence == 0.0, "Min confidence = 0 (no filter)");
            Assert(bt.MinConfluences == 0, "Min confluences = 0 (no filter)");
        }

        private static void Test_ToBacktestConfig_RiskPerTrade()
        {
            Console.WriteLine("\nTest: Risk per trade scales with preset");

            var cons = MRConfig.Conservative.ToBacktestConfig();
            var mod = MRConfig.Moderate.ToBacktestConfig();
            var agg = MRConfig.Aggressive.ToBacktestConfig();

            Assert(cons.RiskPerTradePct < mod.RiskPerTradePct,
                $"Conservative < Moderate ({cons.RiskPerTradePct:F4} < {mod.RiskPerTradePct:F4})");
            Assert(mod.RiskPerTradePct < agg.RiskPerTradePct,
                $"Moderate < Aggressive ({mod.RiskPerTradePct:F4} < {agg.RiskPerTradePct:F4})");
        }

        private static void Test_PresetNames()
        {
            Console.WriteLine("\nTest: Preset names are set");

            Assert(MRConfig.Conservative.PresetName == "Conservative", "Conservative name");
            Assert(MRConfig.Moderate.PresetName == "Moderate", "Moderate name");
            Assert(MRConfig.Aggressive.PresetName == "Aggressive", "Aggressive name");
        }

        private static void Test_PresetEnumValues()
        {
            Console.WriteLine("\nTest: Preset enum values match");

            Assert(MRConfig.Conservative.Preset == MRPreset.Conservative, "Conservative enum");
            Assert(MRConfig.Moderate.Preset == MRPreset.Moderate, "Moderate enum");
            Assert(MRConfig.Aggressive.Preset == MRPreset.Aggressive, "Aggressive enum");
        }

        private static void Test_SignalConfigRoundTrip()
        {
            Console.WriteLine("\nTest: Signal config round-trip produces valid module");

            var configs = MRConfig.AllPresets;
            foreach (var config in configs)
            {
                var signalConfig = config.ToSignalConfig();
                var module = new MRSignalModule(signalConfig);

                Assert(module != null, $"{config.PresetName} signal module created");
            }
        }

        private static void Test_ExitConfigRoundTrip()
        {
            Console.WriteLine("\nTest: Exit config round-trip produces valid manager");

            var configs = MRConfig.AllPresets;
            foreach (var config in configs)
            {
                var exitConfig = config.ToExitConfig();
                var manager = new MRExitManager(exitConfig);

                Assert(manager != null, $"{config.PresetName} exit manager created");
            }
        }

        private static void Test_BacktestConfigDailyDrawdown()
        {
            Console.WriteLine("\nTest: Daily drawdown is 3x risk per trade");

            var configs = MRConfig.AllPresets;
            foreach (var config in configs)
            {
                var bt = config.ToBacktestConfig();
                double expected = config.RiskPerTradePct * 3;

                AssertEqual(expected, bt.MaxDailyDrawdownPct,
                    $"{config.PresetName} daily DD = 3x risk ({expected:F4})");
            }
        }

        private static void Test_PrintSummary_DoesNotThrow()
        {
            Console.WriteLine("\nTest: PrintSummary does not throw");

            bool noThrow = true;
            try
            {
                foreach (var config in MRConfig.AllPresets)
                {
                    config.PrintSummary();
                }
            }
            catch
            {
                noThrow = false;
            }

            Assert(noThrow, "PrintSummary completes without exception");
        }
    }
}
