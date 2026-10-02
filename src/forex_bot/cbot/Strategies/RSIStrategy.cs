using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public class RSIStrategy : ISignalStrategy
    {
        public string Name => "RSI Divergence";

        private readonly int _period;
        private readonly double _oversold;
        private readonly double _overbought;
        private readonly int _divergenceLookback;

        public RSIStrategy(int period = 14, double oversold = 30, double overbought = 70, int divergenceLookback = 50)
        {
            _period = period;
            _oversold = oversold;
            _overbought = overbought;
            _divergenceLookback = divergenceLookback;
        }

        public StrategySignal? Evaluate(MarketState state)
        {
            if (state.Bars.Count < _period + _divergenceLookback)
                return null;

            double rsi = CalculateRSI(state.Bars);
            if (rsi == 0)
                return null;

            double atr = state.ATR > 0 ? state.ATR : CalculateATR(state.Bars);

            bool bullishDiv = DetectBullishDivergence(state);
            bool bearishDiv = DetectBearishDivergence(state);

            bool oversoldCondition = rsi <= _oversold;
            bool overboughtCondition = rsi >= _overbought;

            bool bullishSignal = (oversoldCondition || bullishDiv) && (bullishDiv || state.LatestBar.IsBullish);
            bool bearishSignal = (overboughtCondition || bearishDiv) && (bearishDiv || state.LatestBar.IsBearish);

            if (!bullishSignal && !bearishSignal)
                return null;

            TradeDirection direction = bullishSignal ? TradeDirection.Long : TradeDirection.Short;
            double entry = state.LatestBar.Close;
            double sl = direction == TradeDirection.Long
                ? entry - atr * 2.5
                : entry + atr * 2.5;
            double risk = Math.Abs(entry - sl);
            double tp1 = direction == TradeDirection.Long ? entry + risk * 1.0 : entry - risk * 1.0;
            double tp2 = direction == TradeDirection.Long ? entry + risk * 2.0 : entry - risk * 2.0;
            double tp3 = direction == TradeDirection.Long ? entry + risk * 3.0 : entry - risk * 3.0;

            double confidence = direction == TradeDirection.Long
                ? 0.50 + ((_oversold - rsi) / _oversold) * 0.45
                : 0.50 + ((rsi - _overbought) / (100 - _overbought)) * 0.45;

            if (direction == TradeDirection.Long && bullishDiv)
                confidence += 0.15;
            if (direction == TradeDirection.Short && bearishDiv)
                confidence += 0.15;

            string rationale = direction == TradeDirection.Long
                ? $"RSI oversold ({rsi:F1}) with {(bullishDiv ? "bullish divergence" : "bullish candle")}"
                : $"RSI overbought ({rsi:F1}) with {(bearishDiv ? "bearish divergence" : "bearish candle")}";

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

        private double CalculateRSI(List<Bar> bars)
        {
            if (bars.Count < _period + 1) return 0;

            var gains = new List<double>();
            var losses = new List<double>();

            for (int i = bars.Count - _period; i < bars.Count; i++)
            {
                double change = bars[i].Close - bars[i - 1].Close;
                gains.Add(change > 0 ? change : 0);
                losses.Add(change < 0 ? Math.Abs(change) : 0);
            }

            double avgGain = 0, avgLoss = 0;
            foreach (var g in gains) avgGain += g;
            foreach (var l in losses) avgLoss += l;
            avgGain /= _period;
            avgLoss /= _period;

            if (avgLoss == 0) return 100;
            double rs = avgGain / avgLoss;
            return 100 - (100 / (1 + rs));
        }

        private bool DetectBullishDivergence(MarketState state)
        {
            int lookback = Math.Min(_divergenceLookback, state.Bars.Count - _period);
            if (lookback < 20) return false;

            double lowestPrice = double.MaxValue;
            double lowestRSI = double.MaxValue;
            int lowestPriceIdx = 0;
            int lowestRSIIdx = 0;

            for (int i = state.Bars.Count - lookback; i < state.Bars.Count - _period; i += 5)
            {
                double price = state.Bars[i].Low;
                double rsi = CalculateRSI(state.Bars.GetRange(0, i + _period));
                if (price < lowestPrice) { lowestPrice = price; lowestPriceIdx = i; }
                if (rsi < lowestRSI) { lowestRSI = rsi; lowestRSIIdx = i; }
            }

            double recentPrice = state.LatestBar.Low;
            double recentRSI = CalculateRSI(state.Bars);

            return recentPrice < lowestPrice && recentRSI > lowestRSI;
        }

        private bool DetectBearishDivergence(MarketState state)
        {
            int lookback = Math.Min(_divergenceLookback, state.Bars.Count - _period);
            if (lookback < 20) return false;

            double highestPrice = double.MinValue;
            double highestRSI = double.MinValue;

            for (int i = state.Bars.Count - lookback; i < state.Bars.Count - _period; i += 5)
            {
                double price = state.Bars[i].High;
                double rsi = CalculateRSI(state.Bars.GetRange(0, i + _period));
                if (price > highestPrice) highestPrice = price;
                if (rsi > highestRSI) highestRSI = rsi;
            }

            double recentPrice = state.LatestBar.High;
            double recentRSI = CalculateRSI(state.Bars);

            return recentPrice > highestPrice && recentRSI < highestRSI;
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