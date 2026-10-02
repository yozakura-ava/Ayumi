using System;

namespace ICTSMC
{
    public enum MRPreset
    {
        Conservative,
        Moderate,
        Aggressive
    }

    public struct MRConfig
    {
        public MRPreset Preset;
        public string PresetName;

        public int BBPeriod;
        public double BBStdDevMultiplier;
        public int RSIPeriod;
        public double RSIOversold;
        public double RSIOverbought;
        public int EMAPeriod;
        public int ATRPeriod;
        public double SLATRMultiplier;
        public int TimeStopCandles;
        public double RiskPerTradePct;
        public double TouchBuffer;
        public double PartialClosePct;
        public bool TrailingToBreakeven;

        public static MRConfig Conservative => new MRConfig
        {
            Preset = MRPreset.Conservative,
            PresetName = "Conservative",
            BBPeriod = 20,
            BBStdDevMultiplier = 2.0,
            RSIPeriod = 14,
            RSIOversold = 35.0,
            RSIOverbought = 65.0,
            EMAPeriod = 200,
            ATRPeriod = 14,
            SLATRMultiplier = 2.0,
            TimeStopCandles = 12,
            RiskPerTradePct = 0.005,
            TouchBuffer = 0.0,
            PartialClosePct = 0.5,
            TrailingToBreakeven = true
        };

        public static MRConfig Moderate => new MRConfig
        {
            Preset = MRPreset.Moderate,
            PresetName = "Moderate",
            BBPeriod = 20,
            BBStdDevMultiplier = 2.0,
            RSIPeriod = 14,
            RSIOversold = 40.0,
            RSIOverbought = 60.0,
            EMAPeriod = 200,
            ATRPeriod = 14,
            SLATRMultiplier = 1.5,
            TimeStopCandles = 8,
            RiskPerTradePct = 0.0075,
            TouchBuffer = 0.0,
            PartialClosePct = 0.5,
            TrailingToBreakeven = true
        };

        public static MRConfig Aggressive => new MRConfig
        {
            Preset = MRPreset.Aggressive,
            PresetName = "Aggressive",
            BBPeriod = 20,
            BBStdDevMultiplier = 1.8,
            RSIPeriod = 14,
            RSIOversold = 45.0,
            RSIOverbought = 55.0,
            EMAPeriod = 100,
            ATRPeriod = 10,
            SLATRMultiplier = 1.2,
            TimeStopCandles = 6,
            RiskPerTradePct = 0.01,
            TouchBuffer = 0.0002,
            PartialClosePct = 0.5,
            TrailingToBreakeven = true
        };

        public static MRConfig Default => Conservative;

        public MRSignalConfig ToSignalConfig()
        {
            return new MRSignalConfig
            {
                BBPeriod = BBPeriod,
                BBStdDevMultiplier = BBStdDevMultiplier,
                RSIPeriod = RSIPeriod,
                RSIOversold = RSIOversold,
                RSIOverbought = RSIOverbought,
                EMA200Period = EMAPeriod,
                TouchBuffer = TouchBuffer
            };
        }

        public MRExitConfig ToExitConfig()
        {
            return new MRExitConfig
            {
                ATRPeriod = ATRPeriod,
                ATRMultiplier = SLATRMultiplier,
                BBPeriod = BBPeriod,
                BBStdDevMultiplier = BBStdDevMultiplier,
                TimeStopCandles = TimeStopCandles,
                EMA200Period = EMAPeriod,
                PartialClosePct = PartialClosePct,
                TrailingToBreakeven = TrailingToBreakeven
            };
        }

        public BacktestConfig ToBacktestConfig()
        {
            return new BacktestConfig
            {
                StartingBalance = 10000,
                RiskPerTradePct = RiskPerTradePct,
                MaxDailyDrawdownPct = RiskPerTradePct * 3,
                MaxTotalDrawdownPct = 0.10,
                SpreadPips = 0.5,
                CommissionPerLot = 3.5,
                MinConfidence = 0.0,
                MinConfluences = 0,
                MinRiskReward = 0.0,
                MaxOpenTrades = 2,
                MinBarsBeforeSignal = BBPeriod + RSIPeriod,
                PartialCloseEnabled = true,
                PartialCloseAtRR = 1.0,
                PartialClosePct = PartialClosePct,
                TrailingStopEnabled = false,
                TrailingStopATRMultiplier = 1.0,
                RegimeFilterEnabled = false,
                NewsVolatilityFilterEnabled = false,
                Leverage = 100.0,
                MinBarsBetweenTrades = 0
            };
        }

        public static MRConfig[] AllPresets => new[]
        {
            Conservative,
            Moderate,
            Aggressive
        };

        public void PrintSummary()
        {
            Console.WriteLine($"  Preset:             {PresetName}");
            Console.WriteLine($"  BB({BBPeriod}, {BBStdDevMultiplier:F1})");
            Console.WriteLine($"  RSI({RSIPeriod}) oversold={RSIOversold:F0} overbought={RSIOverbought:F0}");
            Console.WriteLine($"  EMA({EMAPeriod})");
            Console.WriteLine($"  ATR({ATRPeriod}) SL mult={SLATRMultiplier:F1}");
            Console.WriteLine($"  Time stop:          {TimeStopCandles} candles");
            Console.WriteLine($"  Risk/trade:         {RiskPerTradePct * 100:F2}%");
            Console.WriteLine($"  Touch buffer:       {TouchBuffer}");
            Console.WriteLine($"  Partial close:      {PartialClosePct * 100:F0}%");
            Console.WriteLine($"  Trail to BE:        {TrailingToBreakeven}");
        }
    }
}
