using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public enum TradeDirection
    {
        Long,
        Short,
        Neutral
    }

    public enum StructureType
    {
        Bullish,
        Bearish,
        Ranging
    }

    public enum SignalStrength
    {
        Weak,
        Moderate,
        Strong,
        VeryStrong
    }

    public enum SessionType
    {
        London,
        NYAM,
        NYPM,
        Outside
    }

    public struct Bar
    {
        public DateTime Time;
        public double Open;
        public double High;
        public double Low;
        public double Close;
        public double Volume;
        public TimeFrame Period;

        public double Range => High - Low;
        public double Body => Math.Abs(Close - Open);
        public double UpperWick => High - Math.Max(Open, Close);
        public double LowerWick => Math.Min(Open, Close) - Low;
        public double BodyRatio => Range > 0 ? Body / Range : 0;
        public bool IsBullish => Close > Open;
        public bool IsBearish => Close < Open;
    }

    public struct TimeFrame
    {
        public int Minutes { get; }

        public TimeFrame(int minutes)
        {
            Minutes = minutes;
        }

        public static readonly TimeFrame M15 = new TimeFrame(15);
        public static readonly TimeFrame H1 = new TimeFrame(60);
        public static readonly TimeFrame H4 = new TimeFrame(240);
        public static readonly TimeFrame D1 = new TimeFrame(1440);

        public bool IsHigherThan(TimeFrame other) => Minutes > other.Minutes;
    }

    public struct SwingPoint
    {
        public int Index;
        public double Price;
        public bool IsHigh;
        public DateTime Time;
    }

    public struct StructureBreak
    {
        public DateTime Time;
        public TradeDirection Direction;
        public double BreakLevel;
        public bool IsCHoCH;
        public double BreakStrength;
    }

    public struct OrderBlock
    {
        public int StartIndex;
        public int EndIndex;
        public double Top;
        public double Bottom;
        public TradeDirection Direction;
        public double Strength;
        public bool IsMitigated;
        public int Age;
        public DateTime CreatedTime;
        public TimeFrame TimeFrame;
        public double BodySize;
    }

    public struct FairValueGap
    {
        public int StartIndex;
        public double Top;
        public double Bottom;
        public TradeDirection Direction;
        public double Size;
        public int Age;
        public bool IsFilled;
        public bool IsMitigated;
        public DateTime CreatedTime;
        public TimeFrame TimeFrame;
    }

    public struct LiquidityPool
    {
        public double Level;
        public double Touches;
        public bool IsHigh;
        public bool IsDayHigh;
        public bool IsDayLow;
        public DateTime LastSweepTime;
        public int BarIndex;
    }

    public struct LiquiditySweep
    {
        public DateTime Time;
        public double SweepLevel;
        public double SweepHigh;
        public double SweepLow;
        public double RejectionBody;
        public bool SweptHigh;
        public SessionType Session;
        public double Strength;
        public TradeDirection ImpliedDirection;
    }

    public struct PremiumDiscountZone
    {
        public double Equilibrium;
        public double PremiumBoundary;
        public double DiscountBoundary;
        public double CurrentPrice;
        public TradeDirection CurrentZone;
        public double DistanceFromEquilibrium;
        public double ZoneStrength;
        public bool IsInPremium;
        public bool IsInDiscount;
        public bool IsInEquilibrium;
    }

    public struct ConfluenceSignal
    {
        public TradeDirection Direction;
        public SignalStrength Strength;
        public double ConfidenceScore;
        public double EntryPrice;
        public double StopLoss;
        public double TakeProfit1;
        public double TakeProfit2;
        public double TakeProfit3;
        public DateTime SignalTime;
        public TimeFrame EntryTimeFrame;
        public string Rationale;
        public bool HasOrderBlock;
        public bool HasFVG;
        public bool HasLiquiditySweep;
        public bool HasPremiumDiscountConfluence;
        public bool HasStructureAlignment;
        public int ConfluenceCount;
        public double RiskRewardRatio;
    }

    public class MarketState
    {
        public List<Bar> Bars { get; set; } = new List<Bar>();
        public TradeDirection StructureBias { get; set; } = TradeDirection.Neutral;
        public List<StructureBreak> StructureBreaks { get; set; } = new List<StructureBreak>();
        public List<SwingPoint> SwingHighs { get; set; } = new List<SwingPoint>();
        public List<SwingPoint> SwingLows { get; set; } = new List<SwingPoint>();
        public List<OrderBlock> ActiveOrderBlocks { get; set; } = new List<OrderBlock>();
        public List<FairValueGap> ActiveFVGs { get; set; } = new List<FairValueGap>();
        public List<LiquiditySweep> RecentSweeps { get; set; } = new List<LiquiditySweep>();
        public List<LiquidityPool> LiquidityPools { get; set; } = new List<LiquidityPool>();
        public PremiumDiscountZone? PDZone { get; set; }
        public SessionType CurrentSession { get; set; } = SessionType.Outside;
        public double DayHigh { get; set; }
        public double DayLow { get; set; }
        public double ATR { get; set; }

        public Bar LatestBar => Bars.Count > 0 ? Bars[Bars.Count - 1] : default;
        public Bar PreviousBar => Bars.Count > 1 ? Bars[Bars.Count - 2] : default;
    }
}
