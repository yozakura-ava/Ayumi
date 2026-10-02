using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public enum MRExitReason
    {
        None,
        TakeProfit1,
        TakeProfit2,
        StopLoss,
        TimeStop,
        StructureBreak,
        TrailingStopHit
    }

    public struct MRExitConfig
    {
        public double ATRPeriod;
        public double ATRMultiplier;
        public double BBPeriod;
        public double BBStdDevMultiplier;
        public int TimeStopCandles;
        public int EMA200Period;
        public double PartialClosePct;
        public bool TrailingToBreakeven;

        public static MRExitConfig Default => new MRExitConfig
        {
            ATRPeriod = 14,
            ATRMultiplier = 2.0,
            BBPeriod = 20,
            BBStdDevMultiplier = 2.0,
            TimeStopCandles = 12,
            EMA200Period = 200,
            PartialClosePct = 0.5,
            TrailingToBreakeven = true
        };

        public static MRExitConfig Aggressive => new MRExitConfig
        {
            ATRPeriod = 14,
            ATRMultiplier = 1.5,
            BBPeriod = 20,
            BBStdDevMultiplier = 2.0,
            TimeStopCandles = 8,
            EMA200Period = 200,
            PartialClosePct = 0.5,
            TrailingToBreakeven = true
        };

        public static MRExitConfig Conservative => new MRExitConfig
        {
            ATRPeriod = 14,
            ATRMultiplier = 2.5,
            BBPeriod = 20,
            BBStdDevMultiplier = 1.5,
            TimeStopCandles = 16,
            EMA200Period = 200,
            PartialClosePct = 0.5,
            TrailingToBreakeven = true
        };
    }

    public struct BollingerBands
    {
        public double Upper;
        public double Middle;
        public double Lower;
    }

    public struct MRTradeState
    {
        public TradeDirection Direction;
        public double EntryPrice;
        public double CurrentStopLoss;
        public double LotSize;
        public bool TP1Hit;
        public bool PartialClosed;
        public double PartialClosePrice;
        public double PartialClosePnL;
        public int BarsSinceEntry;
        public double OriginalATR;
        public bool StructureBreakTriggered;
    }

    public struct MRExitSignal
    {
        public bool ShouldClose;
        public bool ShouldPartialClose;
        public double ExitPrice;
        public double PartialClosePrice;
        public double PartialClosePct;
        public double UpdatedStopLoss;
        public MRExitReason Reason;
    }

    public class MRExitManager
    {
        private readonly MRExitConfig _config;

        public MRExitManager(MRExitConfig config)
        {
            _config = config;
        }

        public double CalculateStopLoss(TradeDirection direction, double entryPrice, double atr)
        {
            double distance = atr * _config.ATRMultiplier;
            if (direction == TradeDirection.Long)
                return entryPrice - distance;
            return entryPrice + distance;
        }

        public MRExitSignal CheckExit(ref MRTradeState trade, Bar currentBar, BollingerBands bands, double d1Ema200, double currentATR)
        {
            var signal = new MRExitSignal
            {
                ShouldClose = false,
                ShouldPartialClose = false,
                ExitPrice = 0,
                PartialClosePrice = 0,
                PartialClosePct = _config.PartialClosePct,
                UpdatedStopLoss = trade.CurrentStopLoss,
                Reason = MRExitReason.None
            };

            bool slHit = CheckStopLoss(trade, currentBar, ref signal);
            if (slHit) return signal;

            if (!trade.TP1Hit)
            {
                bool tp1Hit = CheckTP1(trade, currentBar, bands, ref signal);
                if (tp1Hit)
                {
                    trade.TP1Hit = true;
                    trade.PartialClosed = true;
                    trade.PartialClosePrice = signal.PartialClosePrice;
                    double partialLots = trade.LotSize * _config.PartialClosePct;
                    double pipValue = GetPipValue(trade.EntryPrice);
                    double partialPips;
                    if (trade.Direction == TradeDirection.Long)
                        partialPips = (signal.PartialClosePrice - trade.EntryPrice) / pipValue;
                    else
                        partialPips = (trade.EntryPrice - signal.PartialClosePrice) / pipValue;
                    trade.PartialClosePnL = partialPips * partialLots * pipValue * 100000.0;
                    trade.LotSize *= (1.0 - _config.PartialClosePct);

                    if (_config.TrailingToBreakeven)
                    {
                        trade.CurrentStopLoss = trade.EntryPrice;
                        signal.UpdatedStopLoss = trade.EntryPrice;
                    }

                    return signal;
                }
            }

            if (trade.TP1Hit)
            {
                bool tp2Hit = CheckTP2(trade, currentBar, bands, ref signal);
                if (tp2Hit) return signal;
            }

            bool timeStopHit = CheckTimeStop(trade, currentBar, ref signal);
            if (timeStopHit) return signal;

            bool structureBreak = CheckStructureBreak(trade, currentBar, d1Ema200, ref signal);
            if (structureBreak) return signal;

            return signal;
        }

        private bool CheckStopLoss(MRTradeState trade, Bar bar, ref MRExitSignal signal)
        {
            if (trade.Direction == TradeDirection.Long)
            {
                if (bar.Low <= trade.CurrentStopLoss)
                {
                    signal.ShouldClose = true;
                    signal.ExitPrice = trade.CurrentStopLoss;
                    signal.Reason = MRExitReason.StopLoss;
                    return true;
                }
            }
            else
            {
                if (bar.High >= trade.CurrentStopLoss)
                {
                    signal.ShouldClose = true;
                    signal.ExitPrice = trade.CurrentStopLoss;
                    signal.Reason = MRExitReason.StopLoss;
                    return true;
                }
            }
            return false;
        }

        private bool CheckTP1(MRTradeState trade, Bar bar, BollingerBands bands, ref MRExitSignal signal)
        {
            if (trade.Direction == TradeDirection.Long)
            {
                if (bar.High >= bands.Middle)
                {
                    signal.ShouldPartialClose = true;
                    signal.PartialClosePrice = bands.Middle;
                    signal.Reason = MRExitReason.TakeProfit1;
                    return true;
                }
            }
            else
            {
                if (bar.Low <= bands.Middle)
                {
                    signal.ShouldPartialClose = true;
                    signal.PartialClosePrice = bands.Middle;
                    signal.Reason = MRExitReason.TakeProfit1;
                    return true;
                }
            }
            return false;
        }

        private bool CheckTP2(MRTradeState trade, Bar bar, BollingerBands bands, ref MRExitSignal signal)
        {
            if (!trade.TP1Hit) return false;

            if (trade.Direction == TradeDirection.Long)
            {
                if (bar.High >= bands.Lower)
                {
                    signal.ShouldClose = true;
                    signal.ExitPrice = bands.Lower;
                    signal.Reason = MRExitReason.TakeProfit2;
                    return true;
                }
            }
            else
            {
                if (bar.Low <= bands.Upper)
                {
                    signal.ShouldClose = true;
                    signal.ExitPrice = bands.Upper;
                    signal.Reason = MRExitReason.TakeProfit2;
                    return true;
                }
            }
            return false;
        }

        private bool CheckTimeStop(MRTradeState trade, Bar bar, ref MRExitSignal signal)
        {
            if (trade.TP1Hit) return false;
            if (trade.BarsSinceEntry < _config.TimeStopCandles) return false;

            signal.ShouldClose = true;
            signal.ExitPrice = bar.Close;
            signal.Reason = MRExitReason.TimeStop;
            return true;
        }

        private bool CheckStructureBreak(MRTradeState trade, Bar bar, double d1Ema200, ref MRExitSignal signal)
        {
            if (trade.StructureBreakTriggered) return false;
            if (d1Ema200 <= 0) return false;

            if (trade.Direction == TradeDirection.Long && bar.Close < d1Ema200)
            {
                trade.StructureBreakTriggered = true;
                signal.ShouldClose = true;
                signal.ExitPrice = bar.Close;
                signal.Reason = MRExitReason.StructureBreak;
                return true;
            }

            if (trade.Direction == TradeDirection.Short && bar.Close > d1Ema200)
            {
                trade.StructureBreakTriggered = true;
                signal.ShouldClose = true;
                signal.ExitPrice = bar.Close;
                signal.Reason = MRExitReason.StructureBreak;
                return true;
            }

            return false;
        }

        public static BollingerBands CalculateBollingerBands(List<Bar> bars, int period = 20, double stdDevMultiplier = 2.0)
        {
            if (bars == null || bars.Count < period)
                return new BollingerBands { Upper = 0, Middle = 0, Lower = 0 };

            var recent = bars.GetRange(bars.Count - period, period);
            double sum = recent.Sum(b => b.Close);
            double middle = sum / period;

            double variance = 0;
            for (int i = 0; i < recent.Count; i++)
            {
                double diff = recent[i].Close - middle;
                variance += diff * diff;
            }
            variance /= period;
            double stdDev = Math.Sqrt(variance);

            return new BollingerBands
            {
                Upper = middle + stdDevMultiplier * stdDev,
                Middle = middle,
                Lower = middle - stdDevMultiplier * stdDev
            };
        }

        public static double CalculateATR(List<Bar> bars, int period = 14)
        {
            if (bars == null || bars.Count < period + 1)
                return 0;

            double atrSum = 0;
            for (int i = bars.Count - period; i < bars.Count; i++)
            {
                if (i > 0)
                {
                    double tr = Math.Max(
                        bars[i].High - bars[i].Low,
                        Math.Max(
                            Math.Abs(bars[i].High - bars[i - 1].Close),
                            Math.Abs(bars[i].Low - bars[i - 1].Close)
                        )
                    );
                    atrSum += tr;
                }
            }
            return atrSum / period;
        }

        public static double CalculateEMA(List<Bar> bars, int period = 200)
        {
            if (bars == null || bars.Count < period)
                return 0;

            double multiplier = 2.0 / (period + 1);
            double ema = bars[0].Close;
            for (int i = 1; i < bars.Count; i++)
            {
                ema = (bars[i].Close - ema) * multiplier + ema;
            }
            return ema;
        }

        private static double GetPipValue(double price)
        {
            if (price > 50) return 0.01;
            return 0.0001;
        }
    }
}
