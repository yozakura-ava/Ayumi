using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class SRBreakoutStrategy : ISignalStrategy
    {
        public string Name => "S/R Breakout";

        private readonly int _lookback;
        private readonly int _confirmationBars;
        private readonly double _breakoutThreshold;

        public SRBreakoutStrategy(int lookback = 50, int confirmationBars = 2, double breakoutThreshold = 0.0005)
        {
            _lookback = lookback;
            _confirmationBars = confirmationBars;
            _breakoutThreshold = breakoutThreshold;
        }

        public StrategySignal? Evaluate(MarketState state)
        {
            if (state.Bars.Count < _lookback + _confirmationBars)
                return null;

            var (support, resistance) = FindSupportResistance(state.Bars);
            if (support == 0 || resistance == 0)
                return null;

            double price = state.LatestBar.Close;
            double atr = state.ATR > 0 ? state.ATR : CalculateATR(state.Bars);
            double range = resistance - support;

            bool bullishBreakout = price > resistance + range * _breakoutThreshold;
            bool bearishBreakout = price < support - range * _breakoutThreshold;

            bool confirmedBullish = bullishBreakout && ConfirmBreakout(state.Bars, support, resistance, TradeDirection.Long);
            bool confirmedBearish = bearishBreakout && ConfirmBreakout(state.Bars, support, resistance, TradeDirection.Short);

            if (!confirmedBullish && !confirmedBearish)
                return null;

            TradeDirection direction = confirmedBullish ? TradeDirection.Long : TradeDirection.Short;
            double entry = price;
            double sl = direction == TradeDirection.Long
                ? support - atr * 0.5
                : resistance + atr * 0.5;
            double risk = Math.Abs(entry - sl);
            double tp1 = direction == TradeDirection.Long ? entry + risk * 1.0 : entry - risk * 1.0;
            double tp2 = direction == TradeDirection.Long ? entry + risk * 2.0 : entry - risk * 2.0;
            double tp3 = direction == TradeDirection.Long ? entry + risk * 3.0 : entry - risk * 3.0;

            double strength = CalculateBreakoutStrength(state.Bars, support, resistance, direction);
            double confidence = 0.50 + strength * 0.45;

            string rationale = direction == TradeDirection.Long
                ? $"Bullish breakout above resistance: {price:F5} > {resistance:F5}"
                : $"Bearish breakdown below support: {price:F5} < {support:F5}";

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

        private (double support, double resistance) FindSupportResistance(List<Bar> bars)
        {
            int startIdx = Math.Max(0, bars.Count - _lookback);
            var range = bars.GetRange(startIdx, bars.Count - startIdx);

            var lows = range.Select(b => b.Low).OrderBy(x => x).Take(5).Average();
            var highs = range.Select(b => b.High).OrderByDescending(x => x).Take(5).Average();

            double support = lows;
            double resistance = highs;

            return (support, resistance);
        }

        private bool ConfirmBreakout(List<Bar> bars, double support, double resistance, TradeDirection direction)
        {
            int startIdx = bars.Count - _confirmationBars;
            int confirmCount = 0;

            for (int i = startIdx; i < bars.Count; i++)
            {
                if (direction == TradeDirection.Long)
                {
                    if (bars[i].Close > resistance) confirmCount++;
                }
                else
                {
                    if (bars[i].Close < support) confirmCount++;
                }
            }

            return confirmCount >= _confirmationBars - 1;
        }

        private double CalculateBreakoutStrength(List<Bar> bars, double support, double resistance, TradeDirection direction)
        {
            double range = resistance - support;
            if (range == 0) return 0;

            double recentRange = bars[bars.Count - 1].Range;
            double avgRange = bars.Skip(Math.Max(0, bars.Count - 20)).Average(b => b.Range);

            double volumeProxy = recentRange / (avgRange > 0 ? avgRange : 1);

            double distanceFromLevel = direction == TradeDirection.Long
                ? (bars[bars.Count - 1].Close - resistance) / range
                : (support - bars[bars.Count - 1].Close) / range;

            return Math.Min(1.0, (volumeProxy + distanceFromLevel) / 2);
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