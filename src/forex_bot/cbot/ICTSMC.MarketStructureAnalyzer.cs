using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class MarketStructureAnalyzer
    {
        private readonly int _swingLookback;
        private readonly double _bosThreshold;

        public MarketStructureAnalyzer(
            int swingLookback = 5,
            double bosThreshold = 0.5)
        {
            _swingLookback = swingLookback;
            _bosThreshold = bosThreshold;
        }

        public void Analyze(MarketState state)
        {
            DetectSwingPoints(state);
            DetectStructureBreaks(state);
            DetermineBias(state);
            CalculateATR(state);
        }

        private void DetectSwingPoints(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < _swingLookback * 2 + 1) return;

            state.SwingHighs.Clear();
            state.SwingLows.Clear();

            for (int i = _swingLookback; i < bars.Count - _swingLookback; i++)
            {
                bool isSwingHigh = true;
                bool isSwingLow = true;

                for (int j = 1; j <= _swingLookback; j++)
                {
                    if (bars[i].High <= bars[i - j].High || bars[i].High <= bars[i + j].High)
                        isSwingHigh = false;
                    if (bars[i].Low >= bars[i - j].Low || bars[i].Low >= bars[i + j].Low)
                        isSwingLow = false;
                }

                if (isSwingHigh)
                {
                    state.SwingHighs.Add(new SwingPoint
                    {
                        Index = i,
                        Price = bars[i].High,
                        IsHigh = true,
                        Time = bars[i].Time
                    });
                }

                if (isSwingLow)
                {
                    state.SwingLows.Add(new SwingPoint
                    {
                        Index = i,
                        Price = bars[i].Low,
                        IsHigh = false,
                        Time = bars[i].Time
                    });
                }
            }
        }

        private void DetectStructureBreaks(MarketState state)
        {
            state.StructureBreaks.Clear();

            if (state.SwingHighs.Count < 2 || state.SwingLows.Count < 2) return;

            for (int i = 1; i < state.SwingHighs.Count; i++)
            {
                if (state.SwingHighs[i].Price > state.SwingHighs[i - 1].Price)
                {
                    double threshold = state.SwingHighs[i - 1].Price * (1 - _bosThreshold / 100);
                    if (state.SwingHighs[i].Price > threshold)
                    {
                        bool isCHoCH = false;
                        if (state.StructureBias == TradeDirection.Short)
                            isCHoCH = true;

                        state.StructureBreaks.Add(new StructureBreak
                        {
                            Time = state.SwingHighs[i].Time,
                            Direction = TradeDirection.Long,
                            BreakLevel = state.SwingHighs[i - 1].Price,
                            IsCHoCH = isCHoCH,
                            BreakStrength = Math.Min(1.0,
                                (state.SwingHighs[i].Price - state.SwingHighs[i - 1].Price) /
                                state.SwingHighs[i - 1].Price * 100 / _bosThreshold)
                        });
                    }
                }
            }

            for (int i = 1; i < state.SwingLows.Count; i++)
            {
                if (state.SwingLows[i].Price < state.SwingLows[i - 1].Price)
                {
                    double threshold = state.SwingLows[i - 1].Price * (1 + _bosThreshold / 100);
                    if (state.SwingLows[i].Price < threshold)
                    {
                        bool isCHoCH = false;
                        if (state.StructureBias == TradeDirection.Long)
                            isCHoCH = true;

                        state.StructureBreaks.Add(new StructureBreak
                        {
                            Time = state.SwingLows[i].Time,
                            Direction = TradeDirection.Short,
                            BreakLevel = state.SwingLows[i - 1].Price,
                            IsCHoCH = isCHoCH,
                            BreakStrength = Math.Min(1.0,
                                (state.SwingLows[i - 1].Price - state.SwingLows[i].Price) /
                                state.SwingLows[i - 1].Price * 100 / _bosThreshold)
                        });
                    }
                }
            }
        }

        private void DetermineBias(MarketState state)
        {
            if (state.StructureBreaks.Count == 0)
            {
                state.StructureBias = TradeDirection.Neutral;
                return;
            }

            var recentBreaks = state.StructureBreaks
                .OrderByDescending(sb => sb.Time)
                .Take(5)
                .ToList();

            int bullishCount = recentBreaks.Count(sb => sb.Direction == TradeDirection.Long);
            int bearishCount = recentBreaks.Count(sb => sb.Direction == TradeDirection.Short);

            double bullishStrength = recentBreaks.Where(sb => sb.Direction == TradeDirection.Long).Sum(sb => sb.BreakStrength);
            double bearishStrength = recentBreaks.Where(sb => sb.Direction == TradeDirection.Short).Sum(sb => sb.BreakStrength);

            var lastBreak = recentBreaks.First();
            double lastBreakWeight = 2.0;

            bullishStrength += lastBreak.Direction == TradeDirection.Long ? lastBreakWeight : 0;
            bearishStrength += lastBreak.Direction == TradeDirection.Short ? lastBreakWeight : 0;

            if (bullishStrength > bearishStrength * 1.3)
                state.StructureBias = TradeDirection.Long;
            else if (bearishStrength > bullishStrength * 1.3)
                state.StructureBias = TradeDirection.Short;
            else
                state.StructureBias = TradeDirection.Neutral;
        }

        private void CalculateATR(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < 15)
            {
                state.ATR = 0;
                return;
            }

            int period = Math.Min(14, bars.Count - 1);
            double atrSum = 0;
            for (int i = bars.Count - period; i < bars.Count; i++)
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
                    atrSum += tr;
                }
            }
            state.ATR = atrSum / period;
        }

        public bool IsBullishStructure(MarketState state) =>
            state.StructureBias == TradeDirection.Long;

        public bool IsBearishStructure(MarketState state) =>
            state.StructureBias == TradeDirection.Short;

        public double GetStructureStrength(MarketState state)
        {
            if (state.StructureBreaks.Count == 0) return 0;
            var recent = state.StructureBreaks
                .OrderByDescending(sb => sb.Time)
                .Take(3)
                .ToList();
            return recent.Count(sb => sb.Direction == state.StructureBias) / (double)recent.Count;
        }

        public bool IsStrongTrend(MarketState state)
        {
            if (state.StructureBias == TradeDirection.Neutral) return false;
            if (state.StructureBreaks.Count < 1) return false;

            var recentBreaks = state.StructureBreaks
                .OrderByDescending(sb => sb.Time)
                .Take(5)
                .ToList();

            var lastBreak = recentBreaks.First();
            if (lastBreak.Direction != state.StructureBias) return false;

            double lastBreakAge = (state.LatestBar.Time - lastBreak.Time).TotalMinutes;
            if (lastBreakAge > 960) return false;

            int alignedCount = recentBreaks.Count(sb => sb.Direction == state.StructureBias);
            double alignedRatio = alignedCount / (double)recentBreaks.Count;
            if (alignedRatio < 0.4) return false;

            return true;
        }
    }
}
