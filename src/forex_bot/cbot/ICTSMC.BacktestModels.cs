using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public enum TradeOutcome
    {
        Win,
        Loss,
        Breakeven,
        Open
    }

    public enum ExitReason
    {
        TakeProfit1,
        TakeProfit2,
        TakeProfit3,
        StopLoss,
        SignalFlip,
        EndOfData,
        MaxDailyLoss
    }

    public struct BacktestConfig
    {
        public double StartingBalance;
        public double RiskPerTradePct;
        public double MaxDailyDrawdownPct;
        public double MaxTotalDrawdownPct;
        public double SpreadPips;
        public double CommissionPerLot;
        public double MinConfidence;
        public int MinConfluences;
        public double MinRiskReward;
        public int MaxOpenTrades;
        public int MinBarsBeforeSignal;
        public bool PartialCloseEnabled;
        public double PartialCloseAtRR;
        public double PartialClosePct;
        public bool TrailingStopEnabled;
        public double TrailingStopATRMultiplier;
        public bool RegimeFilterEnabled;
        public bool NewsVolatilityFilterEnabled;
        public double Leverage;
        public int MinBarsBetweenTrades;

        public static BacktestConfig Default => new BacktestConfig
        {
            StartingBalance = 10000,
            RiskPerTradePct = 0.01,
            MaxDailyDrawdownPct = 0.03,
            MaxTotalDrawdownPct = 0.10,
            SpreadPips = 0.5,
            CommissionPerLot = 3.5,
            MinConfidence = 0.50,
            MinConfluences = 2,
            MinRiskReward = 0.8,
            MaxOpenTrades = 2,
            MinBarsBeforeSignal = 30,
            PartialCloseEnabled = true,
            PartialCloseAtRR = 1.0,
            PartialClosePct = 0.5,
            TrailingStopEnabled = false,
            TrailingStopATRMultiplier = 1.0,
            RegimeFilterEnabled = true,
            NewsVolatilityFilterEnabled = true,
            Leverage = 100.0,
            MinBarsBetweenTrades = 0
        };

        public static BacktestConfig Aggressive => new BacktestConfig
        {
            StartingBalance = 10000,
            RiskPerTradePct = 0.01,
            MaxDailyDrawdownPct = 0.05,
            MaxTotalDrawdownPct = 0.10,
            SpreadPips = 0.3,
            CommissionPerLot = 3.5,
            MinConfidence = 0.20,
            MinConfluences = 1,
            MinRiskReward = 0.8,
            MaxOpenTrades = 2,
            MinBarsBeforeSignal = 20,
            PartialCloseEnabled = true,
            PartialCloseAtRR = 1.0,
            PartialClosePct = 0.5,
            TrailingStopEnabled = false,
            TrailingStopATRMultiplier = 1.0,
            RegimeFilterEnabled = false,
            NewsVolatilityFilterEnabled = false,
            Leverage = 100.0,
            MinBarsBetweenTrades = 0
        };

        public static BacktestConfig Conservative => new BacktestConfig
        {
            StartingBalance = 10000,
            RiskPerTradePct = 0.005,
            MaxDailyDrawdownPct = 0.015,
            MaxTotalDrawdownPct = 0.04,
            SpreadPips = 0.5,
            CommissionPerLot = 3.5,
            MinConfidence = 0.65,
            MinConfluences = 3,
            MinRiskReward = 1.5,
            MaxOpenTrades = 1,
            MinBarsBeforeSignal = 40,
            PartialCloseEnabled = true,
            PartialCloseAtRR = 1.0,
            PartialClosePct = 0.5,
            TrailingStopEnabled = false,
            TrailingStopATRMultiplier = 1.0,
            RegimeFilterEnabled = true,
            NewsVolatilityFilterEnabled = true,
            Leverage = 100.0,
            MinBarsBetweenTrades = 0
        };

        public static BacktestConfig Tuned => new BacktestConfig
        {
            StartingBalance = 10000,
            RiskPerTradePct = 0.005,
            MaxDailyDrawdownPct = 0.02,
            MaxTotalDrawdownPct = 0.05,
            SpreadPips = 0.5,
            CommissionPerLot = 3.5,
            MinConfidence = 0.45,
            MinConfluences = 2,
            MinRiskReward = 1.2,
            MaxOpenTrades = 1,
            MinBarsBeforeSignal = 30,
            PartialCloseEnabled = true,
            PartialCloseAtRR = 1.0,
            PartialClosePct = 0.5,
            TrailingStopEnabled = false,
            TrailingStopATRMultiplier = 1.0,
            RegimeFilterEnabled = true,
            NewsVolatilityFilterEnabled = true,
            Leverage = 100.0,
            MinBarsBetweenTrades = 4
        };
    }

    public struct SimulatedTrade
    {
        public int EntryBarIndex;
        public int ExitBarIndex;
        public TradeDirection Direction;
        public double EntryPrice;
        public double StopLoss;
        public double TakeProfit1;
        public double TakeProfit2;
        public double TakeProfit3;
        public double ExitPrice;
        public double LotSize;
        public double RiskAmount;
        public double Pips;
        public double ProfitLoss;
        public TradeOutcome Outcome;
        public ExitReason ExitReason;
        public DateTime EntryTime;
        public DateTime ExitTime;
        public double ConfidenceScore;
        public int ConfluenceCount;
        public string Rationale;
        public bool PartialClosed;
        public double PartialClosePrice;
        public double PartialClosePnL;
    }

    public struct BacktestMetrics
    {
        public double StartingBalance;
        public double EndingBalance;
        public double TotalPnL;
        public double TotalPnLPct;
        public double WinRate;
        public int TotalTrades;
        public int WinningTrades;
        public int LosingTrades;
        public int BreakevenTrades;
        public double AvgWin;
        public double AvgLoss;
        public double LargestWin;
        public double LargestLoss;
        public double ProfitFactor;
        public double MaxDrawdownPct;
        public double MaxDrawdownDollar;
        public double MaxDailyLossDollar;
        public double SharpeRatio;
        public double AvgRiskReward;
        public double AvgHoldingBars;
        public double Expectancy;
        public List<double> EquityCurve;
        public List<SimulatedTrade> Trades;
        public double TotalSpreadCost;
        public double TotalCommissionCost;
        public int RejectedSignals;

        public void PrintReport()
        {
            Console.WriteLine("\n╔══════════════════════════════════════════════╗");
            Console.WriteLine("║        ICT/SMC BACKTEST RESULTS              ║");
            Console.WriteLine("╚══════════════════════════════════════════════╝");
            Console.WriteLine($"\n  Starting Balance:    ${StartingBalance:F2}");
            Console.WriteLine($"  Ending Balance:      ${EndingBalance:F2}");
            Console.WriteLine($"  Total P&L:           ${TotalPnL:F2} ({TotalPnLPct:F2}%)");
            Console.WriteLine($"\n  Total Trades:        {TotalTrades}");
            Console.WriteLine($"  Winning:             {WinningTrades}");
            Console.WriteLine($"  Losing:              {LosingTrades}");
            Console.WriteLine($"  Breakeven:           {BreakevenTrades}");
            Console.WriteLine($"  Rejected Signals:    {RejectedSignals}");
            Console.WriteLine($"\n  Win Rate:            {WinRate:F1}%");
            Console.WriteLine($"  Avg Win:             ${AvgWin:F2}");
            Console.WriteLine($"  Avg Loss:            ${AvgLoss:F2}");
            Console.WriteLine($"  Largest Win:         ${LargestWin:F2}");
            Console.WriteLine($"  Largest Loss:        ${LargestLoss:F2}");
            Console.WriteLine($"  Profit Factor:       {ProfitFactor:F2}");
            Console.WriteLine($"  Expectancy:          ${Expectancy:F2}");
            Console.WriteLine($"  Avg R:R:             {AvgRiskReward:F2}");
            Console.WriteLine($"\n  Max Drawdown:        {MaxDrawdownPct:F2}% (${MaxDrawdownDollar:F2})");
            Console.WriteLine($"  Max Daily Loss:      ${MaxDailyLossDollar:F2}");
            Console.WriteLine($"  Sharpe Ratio:        {SharpeRatio:F2}");
            Console.WriteLine($"  Avg Holding Bars:    {AvgHoldingBars:F0}");
            Console.WriteLine($"\n  Spread Cost:         ${TotalSpreadCost:F2}");
            Console.WriteLine($"  Commission Cost:     ${TotalCommissionCost:F2}");
            Console.WriteLine();
        }
    }
}
