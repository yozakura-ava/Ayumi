using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public struct ICTConfirmationConfig
    {
        public int RequiredConfirmations;
        public double OBProximityATRMultiplier;
        public double FVGMaxAge;
        public double KillzoneStartHourUTC;
        public double KillzoneEndHourUTC;

        public static ICTConfirmationConfig Default => new ICTConfirmationConfig
        {
            RequiredConfirmations = 2,
            OBProximityATRMultiplier = 1.5,
            FVGMaxAge = 20,
            KillzoneStartHourUTC = 8.0,
            KillzoneEndHourUTC = 16.0
        };

        public static ICTConfirmationConfig Strict => new ICTConfirmationConfig
        {
            RequiredConfirmations = 3,
            OBProximityATRMultiplier = 1.0,
            FVGMaxAge = 10,
            KillzoneStartHourUTC = 8.0,
            KillzoneEndHourUTC = 16.0
        };

        public static ICTConfirmationConfig Loose => new ICTConfirmationConfig
        {
            RequiredConfirmations = 1,
            OBProximityATRMultiplier = 2.0,
            FVGMaxAge = 30,
            KillzoneStartHourUTC = 7.0,
            KillzoneEndHourUTC = 20.0
        };
    }

    public struct ConfirmationResult
    {
        public bool Passed;
        public double Confidence;
    }

    public struct ICTScorerResult
    {
        public bool IsValid;
        public MRSignal Signal;
        public int ConfirmationsPassed;
        public int ConfirmationsChecked;
        public ConfirmationResult OrderBlockResult;
        public ConfirmationResult FVGResult;
        public ConfirmationResult KillzoneResult;
        public ConfirmationResult H4OrderBlockResult;
        public ConfirmationResult H4FVGResult;
        public double OverallConfidence;
        public bool UsedH4Context;
    }

    public struct ICTScorerMetrics
    {
        public int TotalSignalsEvaluated;
        public int SignalsPassed;
        public int SignalsFiltered;
        public int OrderBlockPassed;
        public int OrderBlockFailed;
        public int FVGPassed;
        public int FVGFailed;
        public int KillzonePassed;
        public int KillzoneFailed;
        public int H4OrderBlockPassed;
        public int H4OrderBlockFailed;
        public int H4FVGPassed;
        public int H4FVGFailed;

        public double PassRate => TotalSignalsEvaluated > 0
            ? (double)SignalsPassed / TotalSignalsEvaluated * 100.0
            : 0;
    }

    public class ICTConfirmationScorer
    {
        private readonly ICTConfirmationConfig _config;
        private readonly OrderBlockDetector _obDetector;
        private readonly FVGDetector _fvgDetector;
        private H4ContextModule _h4Module;
        private bool _useH4Context;
        private ICTScorerMetrics _metrics;

        public ICTScorerMetrics Metrics => _metrics;
        public H4ContextModule H4Module => _h4Module;

        public ICTConfirmationScorer(ICTConfirmationConfig config)
            : this(config, new OrderBlockDetector(), new FVGDetector())
        {
        }

        public ICTConfirmationScorer(
            ICTConfirmationConfig config,
            OrderBlockDetector obDetector,
            FVGDetector fvgDetector)
        {
            _config = config;
            _obDetector = obDetector;
            _fvgDetector = fvgDetector;
            _metrics = new ICTScorerMetrics();
            _h4Module = null;
            _useH4Context = false;
        }

        public void SetH4Context(H4ContextModule h4Module)
        {
            _h4Module = h4Module;
            _useH4Context = h4Module != null;
        }

        public void ResetMetrics()
        {
            _metrics = new ICTScorerMetrics();
        }

        public ICTScorerResult Evaluate(
            MRSignal signal,
            MarketState state,
            double atr)
        {
            return Evaluate(signal, state, atr, null);
        }

        public ICTScorerResult Evaluate(
            MRSignal signal,
            MarketState state,
            double atr,
            H4ContextModule h4Module)
        {
            if (!signal.IsValid)
            {
                return InvalidResult(signal);
            }

            _metrics.TotalSignalsEvaluated++;

            var obResult = CheckOrderBlock(signal, state, atr);
            var fvgResult = CheckFVG(signal, state);
            var kzResult = CheckKillzone(signal);

            var emptyH4 = new ConfirmationResult { Passed = false, Confidence = 0.0 };
            ConfirmationResult h4ObResult = emptyH4;
            ConfirmationResult h4FvgResult = emptyH4;
            bool usedH4 = false;

            var effectiveH4 = h4Module ?? _h4Module;
            if (effectiveH4 != null && effectiveH4.H4ATR > 0)
            {
                h4ObResult = effectiveH4.CheckH4OrderBlock(signal, effectiveH4.H4ATR);
                h4FvgResult = effectiveH4.CheckH4FVG(signal);
                usedH4 = true;
            }

            TrackConfirmationMetrics(obResult, fvgResult, kzResult, h4ObResult, h4FvgResult);

            var results = new List<ConfirmationResult> { obResult, fvgResult, kzResult };
            if (usedH4)
            {
                results.Add(h4ObResult);
                results.Add(h4FvgResult);
            }

            int passed = results.Count(r => r.Passed);

            if (passed < _config.RequiredConfirmations)
            {
                _metrics.SignalsFiltered++;
                return FilteredResult(signal, obResult, fvgResult, kzResult,
                    h4ObResult, h4FvgResult, passed, usedH4);
            }

            _metrics.SignalsPassed++;
            double overallConfidence = results
                .Where(r => r.Passed)
                .Average(r => r.Confidence);

            return new ICTScorerResult
            {
                IsValid = true,
                Signal = signal,
                ConfirmationsPassed = passed,
                ConfirmationsChecked = results.Count,
                OrderBlockResult = obResult,
                FVGResult = fvgResult,
                KillzoneResult = kzResult,
                H4OrderBlockResult = h4ObResult,
                H4FVGResult = h4FvgResult,
                OverallConfidence = overallConfidence,
                UsedH4Context = usedH4
            };
        }

        public ConfirmationResult CheckOrderBlock(MRSignal signal, MarketState state, double atr)
        {
            _obDetector.Detect(state);

            var ob = _obDetector.GetMostRelevant(state, signal.Direction);
            if (!ob.HasValue || ob.Value.IsMitigated)
            {
                return new ConfirmationResult { Passed = false, Confidence = 0.0 };
            }

            double proximityThreshold = atr * _config.OBProximityATRMultiplier;
            if (proximityThreshold <= 0) proximityThreshold = 0.001;

            double price = signal.EntryPrice;
            double obMid = (ob.Value.Top + ob.Value.Bottom) / 2.0;
            double distance = Math.Abs(price - obMid);

            if (distance > proximityThreshold)
            {
                return new ConfirmationResult { Passed = false, Confidence = 0.0 };
            }

            double proximityScore = 1.0 - (distance / proximityThreshold);
            double strengthScore = ob.Value.Strength;
            double ageFactor = Math.Max(0, 1.0 - ob.Value.Age / 15.0);
            double confidence = (proximityScore * 0.4 + strengthScore * 0.35 + ageFactor * 0.25);

            return new ConfirmationResult
            {
                Passed = true,
                Confidence = Math.Min(1.0, Math.Max(0.0, confidence))
            };
        }

        public ConfirmationResult CheckFVG(MRSignal signal, MarketState state)
        {
            _fvgDetector.Detect(state);

            var fvg = _fvgDetector.GetNearestUnfilled(
                state, signal.Direction, signal.EntryPrice);

            if (!fvg.HasValue || fvg.Value.IsMitigated)
            {
                return new ConfirmationResult { Passed = false, Confidence = 0.0 };
            }

            if (fvg.Value.Age > _config.FVGMaxAge)
            {
                return new ConfirmationResult { Passed = false, Confidence = 0.0 };
            }

            bool priceInGap = signal.EntryPrice >= fvg.Value.Bottom
                              && signal.EntryPrice <= fvg.Value.Top;

            if (!priceInGap)
            {
                double gapMid = (fvg.Value.Top + fvg.Value.Bottom) / 2.0;
                double distance = Math.Abs(signal.EntryPrice - gapMid);
                double gapSize = fvg.Value.Top - fvg.Value.Bottom;

                if (gapSize <= 0 || distance > gapSize * 2)
                {
                    return new ConfirmationResult { Passed = false, Confidence = 0.0 };
                }
            }

            double sizeFactor = Math.Min(1.0, fvg.Value.Size / 0.002);
            double ageFactor = Math.Max(0, 1.0 - fvg.Value.Age / _config.FVGMaxAge);
            double fillPenalty = fvg.Value.IsFilled ? 0.3 : 1.0;
            double confidence = (sizeFactor * 0.3 + ageFactor * 0.4 + 0.3) * fillPenalty;

            return new ConfirmationResult
            {
                Passed = true,
                Confidence = Math.Min(1.0, Math.Max(0.0, confidence))
            };
        }

        public ConfirmationResult CheckKillzone(MRSignal signal)
        {
            double hour = signal.SignalTime.Hour;
            double minute = signal.SignalTime.Minute;
            double timeDecimal = hour + minute / 60.0;

            bool inLondon = timeDecimal >= 8.0 && timeDecimal < 12.0;
            bool inNY = timeDecimal >= 13.0 && timeDecimal < 16.0;
            bool inConfigWindow = timeDecimal >= _config.KillzoneStartHourUTC
                                 && timeDecimal < _config.KillzoneEndHourUTC;

            if (!inLondon && !inNY && !inConfigWindow)
            {
                return new ConfirmationResult { Passed = false, Confidence = 0.0 };
            }

            double confidence;
            if (inLondon)
            {
                double londonCenter = 10.0;
                double distFromCenter = Math.Abs(timeDecimal - londonCenter);
                confidence = 0.7 + 0.3 * (1.0 - distFromCenter / 2.0);
            }
            else if (inNY)
            {
                double nyCenter = 14.5;
                double distFromCenter = Math.Abs(timeDecimal - nyCenter);
                confidence = 0.7 + 0.3 * (1.0 - distFromCenter / 1.5);
            }
            else
            {
                double windowCenter = (_config.KillzoneStartHourUTC + _config.KillzoneEndHourUTC) / 2.0;
                double windowHalf = (_config.KillzoneEndHourUTC - _config.KillzoneStartHourUTC) / 2.0;
                double distFromCenter = Math.Abs(timeDecimal - windowCenter);
                confidence = 0.5 + 0.3 * (1.0 - distFromCenter / windowHalf);
            }

            return new ConfirmationResult
            {
                Passed = true,
                Confidence = Math.Min(1.0, Math.Max(0.0, confidence))
            };
        }

        private void TrackConfirmationMetrics(
            ConfirmationResult ob,
            ConfirmationResult fvg,
            ConfirmationResult kz,
            ConfirmationResult h4Ob,
            ConfirmationResult h4Fvg)
        {
            if (ob.Passed) _metrics.OrderBlockPassed++;
            else _metrics.OrderBlockFailed++;

            if (fvg.Passed) _metrics.FVGPassed++;
            else _metrics.FVGFailed++;

            if (kz.Passed) _metrics.KillzonePassed++;
            else _metrics.KillzoneFailed++;

            if (h4Ob.Passed) _metrics.H4OrderBlockPassed++;
            else _metrics.H4OrderBlockFailed++;

            if (h4Fvg.Passed) _metrics.H4FVGPassed++;
            else _metrics.H4FVGFailed++;
        }

        private static ICTScorerResult InvalidResult(MRSignal signal)
        {
            var empty = new ConfirmationResult { Passed = false, Confidence = 0.0 };
            return new ICTScorerResult
            {
                IsValid = false,
                Signal = signal,
                ConfirmationsPassed = 0,
                ConfirmationsChecked = 0,
                OrderBlockResult = empty,
                FVGResult = empty,
                KillzoneResult = empty,
                H4OrderBlockResult = empty,
                H4FVGResult = empty,
                OverallConfidence = 0.0,
                UsedH4Context = false
            };
        }

        private static ICTScorerResult FilteredResult(
            MRSignal signal,
            ConfirmationResult ob,
            ConfirmationResult fvg,
            ConfirmationResult kz,
            ConfirmationResult h4Ob,
            ConfirmationResult h4Fvg,
            int passed,
            bool usedH4)
        {
            return new ICTScorerResult
            {
                IsValid = false,
                Signal = signal,
                ConfirmationsPassed = passed,
                ConfirmationsChecked = usedH4 ? 5 : 3,
                OrderBlockResult = ob,
                FVGResult = fvg,
                KillzoneResult = kz,
                H4OrderBlockResult = h4Ob,
                H4FVGResult = h4Fvg,
                OverallConfidence = 0.0,
                UsedH4Context = usedH4
            };
        }
    }
}
