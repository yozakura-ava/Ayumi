using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class FVGDetector
    {
        private readonly int _maxAge;
        private readonly double _miniThreshold;
        private readonly double _massiveThreshold;
        private readonly TimeFrame _timeFrame;

        public FVGDetector(
            int maxAge = 20,
            double miniThreshold = 0.0003,
            double massiveThreshold = 0.002,
            TimeFrame timeFrame = default)
        {
            _maxAge = maxAge;
            _miniThreshold = miniThreshold;
            _massiveThreshold = massiveThreshold;
            _timeFrame = timeFrame.Minutes == 0 ? TimeFrame.M15 : timeFrame;
            System.Console.WriteLine($"[DEBUG] FVGDetector created: timeFrame.Minutes={timeFrame.Minutes}, _timeFrame.Minutes={_timeFrame.Minutes}");
        }

        public void Detect(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < 5) return;

            int startIdx = Math.Max(2, bars.Count - _maxAge - 5);
            int currentIdx = bars.Count - 1;

            for (int i = startIdx; i <= currentIdx - 2; i++)
            {
                var candle1 = bars[i];
                var candle3 = bars[i + 2];

                if (candle3.High < candle1.Low) continue;
                if (candle3.Low > candle1.High) continue;

                double gap;
                TradeDirection direction;

                if (candle1.High < candle3.Low)
                {
                    gap = candle3.Low - candle1.High;
                    direction = TradeDirection.Long;
                }
                else if (candle3.High > candle1.Low)
                {
                    gap = candle3.High - candle1.Low;
                    direction = TradeDirection.Short;
                }
                else
                {
                    continue;
                }

                double normalizedGap = gap / bars[i + 1].Close;
                if (normalizedGap < _miniThreshold) continue;

                int age = currentIdx - (i + 1);

                bool filled = false;
                bool mitigated = false;
                for (int j = i + 3; j <= currentIdx; j++)
                {
                    if (direction == TradeDirection.Long)
                    {
                        if (bars[j].Low <= candle1.High)
                        {
                            mitigated = true;
                            break;
                        }
                        double fillPercent = (candle1.High - candle3.Low + gap) > 0
                            ? Math.Max(0, (candle3.Low - bars[j].Low) / gap)
                            : 0;
                        if (fillPercent >= 0.5) filled = true;
                    }
                    else
                    {
                        if (bars[j].High >= candle3.High)
                        {
                            mitigated = true;
                            break;
                        }
                        double fillPercent = (candle3.High - candle1.Low + gap) > 0
                            ? Math.Max(0, (bars[j].High - candle3.High) / gap)
                            : 0;
                        if (fillPercent >= 0.5) filled = true;
                    }
                }

                if (mitigated) continue;

                double top = direction == TradeDirection.Long ? candle3.Low : candle3.High;
                double bottom = direction == TradeDirection.Long ? candle1.High : candle1.Low;
                if (bottom > top) (top, bottom) = (bottom, top);

                state.ActiveFVGs.Add(new FairValueGap
                {
                    StartIndex = i,
                    Top = top,
                    Bottom = bottom,
                    Direction = direction,
                    Size = gap,
                    Age = age,
                    IsFilled = filled,
                    IsMitigated = false,
                    CreatedTime = candle1.Time,
                    TimeFrame = _timeFrame
                });
                System.Console.WriteLine($"[DEBUG] FVG added: _timeFrame.Minutes={_timeFrame.Minutes}, lastFVG.Minutes={state.ActiveFVGs[state.ActiveFVGs.Count-1].TimeFrame.Minutes}");
            }

            CleanupStale(state);
        }

        private void CleanupStale(MarketState state)
        {
            state.ActiveFVGs.RemoveAll(fvg =>
                fvg.IsMitigated || fvg.Age > _maxAge);
        }

        public FairValueGap? GetNearestUnfilled(MarketState state, TradeDirection direction, double currentPrice)
        {
            return state.ActiveFVGs
                .Where(fvg => fvg.Direction == direction && !fvg.IsMitigated)
                .OrderBy(fvg => Math.Abs(currentPrice - (fvg.Top + fvg.Bottom) / 2))
                .FirstOrDefault();
        }
    }
}
