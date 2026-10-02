using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public class ROCMStrategy : ISignalStrategy
    {
        public string Name => "Momentum ROC";

        private readonly int _period;
        private readonly double _rocThreshold;

        public ROCMStrategy(int period = 12, double rocThreshold = 0.5)
        {
            _period = period;
            _rocThreshold = rocThreshold;
        }

        public StrategySignal? Evaluate(MarketState state)
        {
            if (state.Bars.Count < _period + 1)
                return null;

            double roc = CalculateROC(state.Bars);
            if (Math.Abs(roc) < _rocThreshold)
                return null;

            double atr = state.ATR > 0 ? state.ATR : CalculateATR(state.Bars);

            bool bullishMomentum = roc > _rocThreshold;
            bool bearishMomentum = roc < -_rocThreshold;

            if (!bullishMomentum && !bearishMomentum)
                return null;

            TradeDirection direction = bullishMomentum ? TradeDirection.Long : TradeDirection.Short;
            double entry = state.LatestBar.Close;
            double sl = direction == TradeDirection.Long
                ? entry - atr * 2.5
                : entry + atr * 2.5;
            double risk = Math.Abs(entry - sl);
            double tp1 = direction == TradeDirection.Long ? entry + risk * 1.0 : entry - risk * 1.0;
            double tp2 = direction == TradeDirection.Long ? entry + risk * 2.0 : entry - risk * 2.0;
            double tp3 = direction == TradeDirection.Long ? entry + risk * 3.0 : entry - risk * 3.0;

            double normalizedROC = Math.Min(1.0, Math.Abs(roc) / 3.0);
            double confidence = 0.50 + normalizedROC * 0.45;

            string rationale = bullishMomentum
                ? $"Positive momentum ROC: {roc:F2}% (> {_rocThreshold}%)"
                : $"Negative momentum ROC: {roc:F2}% (< - {_rocThreshold}%)";

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

        private double CalculateROC(List<Bar> bars)
        {
            int idx = bars.Count - _period - 1;
            if (idx < 0) return 0;

            double current = bars[bars.Count - 1].Close;
            double past = bars[idx].Close;

            if (past == 0) return 0;

            return ((current - past) / past) * 100;
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