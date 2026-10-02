using System;

namespace ICTSMC
{
    public interface ISignalStrategy
    {
        string Name { get; }
        StrategySignal? Evaluate(MarketState state);
    }

    public struct StrategySignal
    {
        public TradeDirection Direction;
        public double Confidence;
        public double EntryPrice;
        public double StopLoss;
        public double TakeProfit1;
        public double TakeProfit2;
        public double TakeProfit3;
        public string Rationale;
    }

    public class StrategyBacktestResult
    {
        public string StrategyName;
        public BacktestMetrics Metrics;
        public StrategySignal? LastSignal;
    }

    public class MultiStrategyConfig
    {
        public double[] Weights = { 1.0, 1.0, 1.0, 1.0, 1.0 };
        public double MinCombinedConfidence = 0.50;
        public bool UseConfluenceScoring = true;
    }
}