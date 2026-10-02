using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public class MACrossStrategy : ISignalStrategy
    {
        public string Name => "MA Crossover";

        private readonly int _fastPeriod;
        private readonly int _slowPeriod;
        private readonly double _atrMultiplier;

        public MACrossStrategy(int fastPeriod = 9, int slowPeriod = 21, double atrMultiplier = 2.5)
        {
            _fastPeriod = fastPeriod;
            _slowPeriod = slowPeriod;
            _atrMultiplier = atrMultiplier;
        }

        public StrategySignal? Evaluate(MarketState state)
        {
            if (state.Bars.Count < _slowPeriod + 1)
                return null;

            var fastMA = CalculateSMA(state.Bars, _fastPeriod);
            var slowMA = CalculateSMA(state.Bars, _slowPeriod);
            var prevFastMA = CalculateSMA(state.Bars.GetRange(0, state.Bars.Count - 1), _fastPeriod);
            var prevSlowMA = CalculateSMA(state.Bars.GetRange(0, state.Bars.Count - 1), _slowPeriod);

            if (fastMA == 0 || slowMA == 0 || prevFastMA == 0 || prevSlowMA == 0)
                return null;

            bool bullishCross = prevFastMA <= prevSlowMA && fastMA > slowMA;
            bool bearishCross = prevFastMA >= prevSlowMA && fastMA < slowMA;

            if (!bullishCross && !bearishCross)
                return null;

            TradeDirection direction = bullishCross ? TradeDirection.Long : TradeDirection.Short;
            double atr = state.ATR > 0 ? state.ATR : CalculateATR(state.Bars);
            double entry = state.LatestBar.Close;
            double sl = direction == TradeDirection.Long
                ? entry - atr * _atrMultiplier
                : entry + atr * _atrMultiplier;
            double risk = Math.Abs(entry - sl);
            double tp1 = direction == TradeDirection.Long ? entry + risk * 1.0 : entry - risk * 1.0;
            double tp2 = direction == TradeDirection.Long ? entry + risk * 2.0 : entry - risk * 2.0;
            double tp3 = direction == TradeDirection.Long ? entry + risk * 3.0 : entry - risk * 3.0;

            double trendStrength = CalculateTrendStrength(fastMA, slowMA);
            double confidence = Math.Min(0.95, 0.50 + trendStrength * 0.45);

            string rationale = direction == TradeDirection.Long
                ? $"Bullish MA cross: fast={fastMA:F5} > slow={slowMA:F5}"
                : $"Bearish MA cross: fast={fastMA:F5} < slow={slowMA:F5}";

            return new StrategySignal
            {
                Direction = direction,
                Confidence = confidence,
                EntryPrice = entry,
                StopLoss = sl,
                TakeProfit1 = tp1,
                TakeProfit2 = tp2,
                TakeProfit3 = tp3,
                Rationale = rationale
            };
        }

        private double CalculateSMA(List<Bar> bars, int period)
        {
            if (bars.Count < period) return 0;
            double sum = 0;
            for (int i = bars.Count - period; i < bars.Count; i++)
                sum += bars[i].Close;
            return sum / period;
        }

        private double CalculateTrendStrength(double fastMA, double slowMA)
        {
            if (slowMA == 0) return 0;
            return Math.Min(1.0, Math.Abs(fastMA - slowMA) / slowMA * 10);
        }

        private double CalculateATR(List<Bar> bars)
        {
            if (bars.Count < 14) return 0.0001;
            double sum = 0;
            for (int i = bars.Count - 14; i < bars.Count; i++)
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
                    sum += tr;
                }
            }
            return sum / 14;
        }
    }
}