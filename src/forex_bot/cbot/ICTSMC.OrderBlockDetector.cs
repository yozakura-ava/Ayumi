using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class OrderBlockDetector
    {
        private readonly int _freshnessWindow;
        private readonly double _minBodyRatio;
        private readonly double _overlapThreshold;
        private readonly int _lookback;
        private readonly TimeFrame _timeFrame;

        public OrderBlockDetector(
            int freshnessWindow = 5,
            double minBodyRatio = 0.5,
            double overlapThreshold = 0.7,
            int lookback = 50,
            TimeFrame timeFrame = default)
        {
            _freshnessWindow = freshnessWindow;
            _minBodyRatio = minBodyRatio;
            _overlapThreshold = overlapThreshold;
            _lookback = lookback;
            _timeFrame = timeFrame.Minutes == 0 ? TimeFrame.M15 : timeFrame;
        }

        public void Detect(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < 10) return;

            int startIdx = Math.Max(0, bars.Count - _lookback);
            int currentIdx = bars.Count - 1;

            for (int i = startIdx; i <= currentIdx - 2; i++)
            {
                var bar = bars[i];

                if (bar.BodyRatio < _minBodyRatio) continue;
                if (bar.Range == 0) continue;

                int age = currentIdx - i;
                if (age > _freshnessWindow) continue;

                TradeDirection direction = bar.IsBullish ? TradeDirection.Long : TradeDirection.Short;
                double top = Math.Max(bar.Open, bar.Close);
                double bottom = Math.Min(bar.Open, bar.Close);

                double avgBody = bars.Skip(Math.Max(0, bars.Count - 21)).Take(20)
                    .Where(b => b.Range > 0)
                    .Average(b => b.Body);

                double strength = 0.5;
                if (avgBody > 0)
                    strength = Math.Min(1.0, 0.5 + (bar.Body / avgBody - 1.0) * 0.25);

                double wickRatio = Math.Min(bar.UpperWick, bar.LowerWick) / bar.Range;
                if (wickRatio < 0.2) strength += 0.1;

                if (state.StructureBias == direction)
                    strength += 0.15;

                strength = Math.Min(1.0, strength);

                bool overlaps = state.ActiveOrderBlocks.Any(ob =>
                    ob.Direction == direction &&
                    Math.Max(0, Math.Min(top, ob.Top) - Math.Max(bottom, ob.Bottom)) /
                    Math.Min(ob.Top - ob.Bottom, top - bottom) > _overlapThreshold);

                if (overlaps) continue;

                bool mitigated = false;
                for (int j = i + 1; j <= currentIdx; j++)
                {
                    if (direction == TradeDirection.Long && bars[j].Low < bottom)
                    {
                        mitigated = true;
                        break;
                    }
                    if (direction == TradeDirection.Short && bars[j].High > top)
                    {
                        mitigated = true;
                        break;
                    }
                }

                if (!mitigated)
                {
                    state.ActiveOrderBlocks.Add(new OrderBlock
                    {
                        StartIndex = i,
                        EndIndex = i,
                        Top = top,
                        Bottom = bottom,
                        Direction = direction,
                        Strength = strength,
                        IsMitigated = false,
                        Age = age,
                        CreatedTime = bar.Time,
                        TimeFrame = _timeFrame,
                        BodySize = bar.Body
                    });
                }
            }

            CleanupMitigated(state);
        }

        private void CleanupMitigated(MarketState state)
        {
            var bars = state.Bars;
            state.ActiveOrderBlocks.RemoveAll(ob =>
            {
                for (int i = ob.StartIndex + 1; i < bars.Count; i++)
                {
                    if (ob.Direction == TradeDirection.Long && bars[i].Low < ob.Bottom)
                        return true;
                    if (ob.Direction == TradeDirection.Short && bars[i].High > ob.Top)
                        return true;
                }
                return ob.Age > _freshnessWindow * 3;
            });
        }

        public OrderBlock? GetMostRelevant(MarketState state, TradeDirection direction)
        {
            return state.ActiveOrderBlocks
                .Where(ob => ob.Direction == direction && !ob.IsMitigated)
                .OrderByDescending(ob => ob.Strength)
                .ThenBy(ob => ob.Age)
                .FirstOrDefault();
        }
    }
}
