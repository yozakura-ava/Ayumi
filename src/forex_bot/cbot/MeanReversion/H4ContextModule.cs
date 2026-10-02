using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public struct H4ContextConfig
    {
        public int OBFreshnessWindow;
        public int OBLookback;
        public int FVGMaxAge;
        public double FVGMiniThreshold;
        public double FVGMassiveThreshold;

        public static H4ContextConfig Default => new H4ContextConfig
        {
            OBFreshnessWindow = 10,
            OBLookback = 30,
            FVGMaxAge = 15,
            FVGMiniThreshold = 0.0003,
            FVGMassiveThreshold = 0.002
        };
    }

    public struct H4ZoneResult
    {
        public bool HasH4OB;
        public bool HasH4FVG;
        public ConfirmationResult OBResult;
        public ConfirmationResult FVGResult;
        public int H4ConfirmationsPassed;
    }

    public class H4ContextModule
    {
        private readonly H4ContextConfig _config;
        private readonly OrderBlockDetector _obDetector;
        private readonly FVGDetector _fvgDetector;
        private readonly MarketStructureAnalyzer _structureAnalyzer;
        private readonly MarketState _h4State;
        private List<Bar> _allH4Bars;
        private int _h4SliceEnd;

        public List<OrderBlock> ActiveH4OrderBlocks => _h4State.ActiveOrderBlocks;
        public List<FairValueGap> ActiveH4FVGs => _h4State.ActiveFVGs;
        public TradeDirection H4StructureBias => _h4State.StructureBias;
        public double H4ATR => _h4State.ATR;

        public H4ContextModule()
            : this(H4ContextConfig.Default) { }

        public H4ContextModule(H4ContextConfig config)
        {
            _config = config;
            _obDetector = new OrderBlockDetector(
                freshnessWindow: config.OBFreshnessWindow,
                lookback: config.OBLookback,
                timeFrame: TimeFrame.H4);
            _fvgDetector = new FVGDetector(
                maxAge: config.FVGMaxAge,
                miniThreshold: config.FVGMiniThreshold,
                massiveThreshold: config.FVGMassiveThreshold,
                timeFrame: TimeFrame.H4);
            _structureAnalyzer = new MarketStructureAnalyzer();
            _h4State = new MarketState();
            _allH4Bars = new List<Bar>();
            _h4SliceEnd = 0;
        }

        public void Update(List<Bar> allH4Bars, DateTime h1BarTime)
        {
            _allH4Bars = allH4Bars;
            ExpandH4Slice(h1BarTime);
        }

        private void ExpandH4Slice(DateTime h1BarTime)
        {
            while (_h4SliceEnd < _allH4Bars.Count &&
                   _allH4Bars[_h4SliceEnd].Time <= h1BarTime)
            {
                _h4SliceEnd++;
            }

            RebuildH4State();
        }

        private void RebuildH4State()
        {
            _h4State.Bars = _allH4Bars.GetRange(0, _h4SliceEnd);
            _h4State.ActiveOrderBlocks.Clear();
            _h4State.ActiveFVGs.Clear();
            _h4State.StructureBreaks.Clear();
            _h4State.SwingHighs.Clear();
            _h4State.SwingLows.Clear();

            if (_h4State.Bars.Count >= 10)
            {
                _structureAnalyzer.Analyze(_h4State);
                _obDetector.Detect(_h4State);
                _fvgDetector.Detect(_h4State);
            }
        }

        public OrderBlock? GetMostRelevantOB(TradeDirection direction)
        {
            return _obDetector.GetMostRelevant(_h4State, direction);
        }

        public FairValueGap? GetNearestUnfilledFVG(TradeDirection direction, double currentPrice)
        {
            return _fvgDetector.GetNearestUnfilled(_h4State, direction, currentPrice);
        }

        public ConfirmationResult CheckH4OrderBlock(
            MRSignal signal, double h4ATR, double proximityATRMultiplier = 2.0)
        {
            var ob = GetMostRelevantOB(signal.Direction);
            if (!ob.HasValue || ob.Value.IsMitigated)
            {
                return new ConfirmationResult { Passed = false, Confidence = 0.0 };
            }

            double proximityThreshold = h4ATR * proximityATRMultiplier;
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
            double ageFactor = Math.Max(0, 1.0 - ob.Value.Age / (_config.OBFreshnessWindow * 2.0));
            double tfBonus = 0.1;
            double confidence = (proximityScore * 0.35 + strengthScore * 0.35 + ageFactor * 0.20 + tfBonus);

            return new ConfirmationResult
            {
                Passed = true,
                Confidence = Math.Min(1.0, Math.Max(0.0, confidence))
            };
        }

        public ConfirmationResult CheckH4FVG(MRSignal signal)
        {
            var fvg = GetNearestUnfilledFVG(signal.Direction, signal.EntryPrice);

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

                if (gapSize <= 0 || distance > gapSize * 3)
                {
                    return new ConfirmationResult { Passed = false, Confidence = 0.0 };
                }
            }

            double sizeFactor = Math.Min(1.0, fvg.Value.Size / 0.002);
            double ageFactor = Math.Max(0, 1.0 - fvg.Value.Age / _config.FVGMaxAge);
            double fillPenalty = fvg.Value.IsFilled ? 0.3 : 1.0;
            double tfBonus = 0.1;
            double confidence = (sizeFactor * 0.3 + ageFactor * 0.3 + 0.3 + tfBonus) * fillPenalty;

            return new ConfirmationResult
            {
                Passed = true,
                Confidence = Math.Min(1.0, Math.Max(0.0, confidence))
            };
        }

        public H4ZoneResult EvaluateH4Zones(MRSignal signal, double proximityATRMultiplier = 2.0)
        {
            double h4ATR = _h4State.ATR;
            if (h4ATR <= 0) h4ATR = 0.01;

            var obResult = CheckH4OrderBlock(signal, h4ATR, proximityATRMultiplier);
            var fvgResult = CheckH4FVG(signal);

            int passed = 0;
            if (obResult.Passed) passed++;
            if (fvgResult.Passed) passed++;

            return new H4ZoneResult
            {
                HasH4OB = obResult.Passed,
                HasH4FVG = fvgResult.Passed,
                OBResult = obResult,
                FVGResult = fvgResult,
                H4ConfirmationsPassed = passed
            };
        }

        public static List<Bar> BuildH4Slice(List<Bar> allH4Bars, DateTime h1BarTime)
        {
            var slice = new List<Bar>();
            foreach (var bar in allH4Bars)
            {
                if (bar.Time > h1BarTime) break;
                slice.Add(bar);
            }
            return slice;
        }

        public static int GetH4BarIndex(List<Bar> h4Bars, DateTime h1Time)
        {
            for (int i = h4Bars.Count - 1; i >= 0; i--)
            {
                if (h4Bars[i].Time <= h1Time)
                    return i;
            }
            return -1;
        }
    }
}
