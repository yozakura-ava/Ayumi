using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public class BBStrategy : ISignalStrategy
    {
        public string Name => "Bollinger Band Mean Reversion";

        private readonly int _period;
        private readonly double _stdDevMultiplier;

        public BBStrategy(int period = 20, double stdDevMultiplier = 2.0)
        {
            _period = period;
            _stdDevMultiplier = stdDevMultiplier;
        }

        public StrategySignal? Evaluate(MarketState state)
        {
            if (state.Bars.Count < _period + 1)
                return null;

            var (middle, upper, lower) = CalculateBollingerBands(state.Bars);
            if (middle == 0 || upper == 0 || lower == 0)
                return null;

            double price = state.LatestBar.Close;
            double atr = state.ATR > 0 ? state.ATR : CalculateATR(state.Bars);

            bool nearLowerBand = price <= lower;
            bool nearUpperBand = price >= upper;
            bool nearMiddle = Math.Abs(price - middle) < (upper - lower) * 0.2;

            bool bullishReversal = nearLowerBand && state.LatestBar.IsBullish;
            bool bearishReversal = nearUpperBand && state.LatestBar.IsBearish;

            if (!bullishReversal && !bearishReversal)
                return null;

            TradeDirection direction = bullishReversal ? TradeDirection.Long : TradeDirection.Short;
            double entry = price;
            double sl = direction == TradeDirection.Long
                ? lower - atr * 0.5
                : upper + atr * 0.5;
            double risk = Math.Abs(entry - sl);
            double tp1 = direction == TradeDirection.Long ? entry + risk * 1.0 : entry - risk * 1.0;
            double tp2 = direction == TradeDirection.Long ? entry + risk * 2.0 : entry - risk * 2.0;
            double tp3 = direction == TradeDirection.Long ? entry + risk * 3.0 : entry - risk * 3.0;

            double bandWidth = upper - lower;
            double normalizedPosition = (price - lower) / (bandWidth > 0 ? bandWidth : 1);
            double confidence = direction == TradeDirection.Long
                ? 0.50 + (1 - normalizedPosition) * 0.45
                : 0.50 + normalizedPosition * 0.45;

            string rationale = direction == TradeDirection.Long
                ? $"Price near lower BB: {price:F5} <= {lower:F5}"
                : $"Price near upper BB: {price:F5} >= {upper:F5}";

            return new StrategySignal
            {
                Direction = direction,
                Confidence = Math.Min(0.95, confidence),
                EntryPrice = entry,
                StopLoss = sl,
                TakeProfit1 = tp1,
                TakeProfit2 = tp2,
                TakeProfit3 = tp3,
                Rationale = rationale
            };
        }

        private (double middle, double upper, double lower) CalculateBollingerBands(List<Bar> bars)
        {
            if (bars.Count < _period) return (0, 0, 0);

            double sum = 0;
            for (int i = bars.Count - _period; i < bars.Count; i++)
                sum += bars[i].Close;

            double middle = sum / _period;

            double sumSquares = 0;
            for (int i = bars.Count - _period; i < bars.Count; i++)
                sumSquares += Math.Pow(bars[i].Close - middle, 2);

            double stdDev = Math.Sqrt(sumSquares / _period);
            double upper = middle + stdDev * _stdDevMultiplier;
            double lower = middle - stdDev * _stdDevMultiplier;

            return (middle, upper, lower);
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