using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class LiquiditySweepDetector
    {
        private readonly double _sweepWickRatio;
        private readonly int _poolLookback;
        private readonly double _sweepATRMultiplier;
        private readonly int _sweepValidityBars;

        public LiquiditySweepDetector(
            double sweepWickRatio = 0.6,
            int poolLookback = 50,
            double sweepATRMultiplier = 1.5,
            int sweepValidityBars = 5)
        {
            _sweepWickRatio = sweepWickRatio;
            _poolLookback = poolLookback;
            _sweepATRMultiplier = sweepATRMultiplier;
            _sweepValidityBars = sweepValidityBars;
        }

        public void UpdateLiquidityPools(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < 5) return;

            state.DayHigh = bars.Skip(Math.Max(0, bars.Count - 32)).Max(b => b.High);
            state.DayLow = bars.Skip(Math.Max(0, bars.Count - 32)).Min(b => b.Low);

            state.LiquidityPools.Clear();

            int startIdx = Math.Max(0, bars.Count - _poolLookback);
            for (int i = startIdx; i < bars.Count; i++)
            {
                if (i > 0 && i < bars.Count - 1)
                {
                    if (bars[i].High >= bars[i - 1].High && bars[i].High >= bars[i + 1].High)
                    {
                        int touches = 1;
                        for (int j = i + 1; j < bars.Count; j++)
                        {
                            if (bars[j].High >= bars[i].High - (bars[i].High * 0.0001))
                                touches++;
                            else
                                break;
                        }
                        state.LiquidityPools.Add(new LiquidityPool
                        {
                            Level = bars[i].High,
                            Touches = touches,
                            IsHigh = true,
                            IsDayHigh = Math.Abs(bars[i].High - state.DayHigh) < bars[i].High * 0.0001,
                            BarIndex = i
                        });
                    }

                    if (bars[i].Low <= bars[i - 1].Low && bars[i].Low <= bars[i + 1].Low)
                    {
                        int touches = 1;
                        for (int j = i + 1; j < bars.Count; j++)
                        {
                            if (bars[j].Low <= bars[i].Low + (bars[i].Low * 0.0001))
                                touches++;
                            else
                                break;
                        }
                        state.LiquidityPools.Add(new LiquidityPool
                        {
                            Level = bars[i].Low,
                            Touches = touches,
                            IsHigh = false,
                            IsDayLow = Math.Abs(bars[i].Low - state.DayLow) < bars[i].Low * 0.0001,
                            BarIndex = i
                        });
                    }
                }
            }
        }

        public void DetectSweeps(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < 5 || state.ATR == 0) return;

            var current = bars[bars.Count - 1];
            var previous = bars.Count > 1 ? bars[bars.Count - 2] : current;

            foreach (var pool in state.LiquidityPools)
            {
                if (Math.Abs(pool.BarIndex - (bars.Count - 1)) > _sweepValidityBars) continue;

                if (pool.IsHigh)
                {
                    if (current.High > pool.Level && previous.High <= pool.Level)
                    {
                        double wickLength = current.High - Math.Max(current.Open, current.Close);
                        double totalRange = current.Range;
                        if (totalRange > 0 && wickLength / totalRange > _sweepWickRatio)
                        {
                            double sweepDistance = current.High - pool.Level;
                            if (sweepDistance > state.ATR * 0.5 && sweepDistance < state.ATR * _sweepATRMultiplier)
                            {
                                var sweep = new LiquiditySweep
                                {
                                    Time = current.Time,
                                    SweepLevel = pool.Level,
                                    SweepHigh = current.High,
                                    SweepLow = current.Low,
                                    RejectionBody = current.Body,
                                    SweptHigh = true,
                                    Session = state.CurrentSession,
                                    Strength = CalculateSweepStrength(sweepDistance, state.ATR, pool.Touches),
                                    ImpliedDirection = TradeDirection.Short
                                };
                                state.RecentSweeps.Add(sweep);
                            }
                        }
                    }
                }
                else
                {
                    if (current.Low < pool.Level && previous.Low >= pool.Level)
                    {
                        double wickLength = Math.Min(current.Open, current.Close) - current.Low;
                        double totalRange = current.Range;
                        if (totalRange > 0 && wickLength / totalRange > _sweepWickRatio)
                        {
                            double sweepDistance = pool.Level - current.Low;
                            if (sweepDistance > state.ATR * 0.5 && sweepDistance < state.ATR * _sweepATRMultiplier)
                            {
                                var sweep = new LiquiditySweep
                                {
                                    Time = current.Time,
                                    SweepLevel = pool.Level,
                                    SweepHigh = current.High,
                                    SweepLow = current.Low,
                                    RejectionBody = current.Body,
                                    SweptHigh = false,
                                    Session = state.CurrentSession,
                                    Strength = CalculateSweepStrength(sweepDistance, state.ATR, pool.Touches),
                                    ImpliedDirection = TradeDirection.Long
                                };
                                state.RecentSweeps.Add(sweep);
                            }
                        }
                    }
                }
            }

            CleanupOld(state);
        }

        private double CalculateSweepStrength(double sweepDistance, double atr, double touches)
        {
            double distanceScore = Math.Min(1.0, sweepDistance / atr);
            double touchScore = Math.Min(1.0, touches / 3.0);
            return (distanceScore * 0.6 + touchScore * 0.4);
        }

        private void CleanupOld(MarketState state)
        {
            if (state.Bars.Count == 0) return;
            var cutoff = state.Bars[state.Bars.Count - 1].Time.AddHours(-4);
            state.RecentSweeps.RemoveAll(s => s.Time < cutoff);
        }
    }
}
