using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace ICTSMC
{
    public class SignalConfluenceEngine
    {
        private readonly MarketStructureAnalyzer _structureAnalyzer;
        private readonly OrderBlockDetector _obDetector;
        private readonly FVGDetector _fvgDetector;
        private readonly LiquiditySweepDetector _sweepDetector;
        private readonly PremiumDiscountClassifier _pdClassifier;

        private readonly double _minConfidence;
        private readonly double _structureWeight;
        private readonly double _obWeight;
        private readonly double _fvgWeight;
        private readonly double _sweepWeight;
        private readonly double _pdWeight;
        private readonly double _sessionWeight;
        private readonly double _defaultSLMultiplier;
        private readonly double _tp1RR;
        private readonly double _tp2RR;
        private readonly double _tp3RR;

        public SignalConfluenceEngine(
            double minConfidence = 0.55,
            double structureWeight = 0.30,
            double obWeight = 0.25,
            double fvgWeight = 0.15,
            double sweepWeight = 0.15,
            double pdWeight = 0.10,
            double sessionWeight = 0.05,
            double defaultSLMultiplier = 3.0,
            double tp1RR = 1.0,
            double tp2RR = 2.0,
            double tp3RR = 3.0)
        {
            _minConfidence = minConfidence;
            _structureWeight = structureWeight;
            _obWeight = obWeight;
            _fvgWeight = fvgWeight;
            _sweepWeight = sweepWeight;
            _pdWeight = pdWeight;
            _sessionWeight = sessionWeight;
            _defaultSLMultiplier = defaultSLMultiplier;
            _tp1RR = tp1RR;
            _tp2RR = tp2RR;
            _tp3RR = tp3RR;

            _structureAnalyzer = new MarketStructureAnalyzer();
            _obDetector = new OrderBlockDetector();
            _fvgDetector = new FVGDetector();
            _sweepDetector = new LiquiditySweepDetector();
            _pdClassifier = new PremiumDiscountClassifier();
        }

        public ConfluenceSignal? Evaluate(MarketState state)
        {
            _structureAnalyzer.Analyze(state);
            _obDetector.Detect(state);
            _fvgDetector.Detect(state);
            _sweepDetector.UpdateLiquidityPools(state);
            _sweepDetector.DetectSweeps(state);
            _pdClassifier.Classify(state);

            if (state.ATR == 0) return null;

            var bullishScore = CalculateDirectionalScore(state, TradeDirection.Long);
            var bearishScore = CalculateDirectionalScore(state, TradeDirection.Short);

            TradeDirection direction;
            double confidence;

            if (bullishScore > bearishScore && bullishScore >= _minConfidence)
            {
                direction = TradeDirection.Long;
                confidence = bullishScore;
            }
            else if (bearishScore > bullishScore && bearishScore >= _minConfidence)
            {
                direction = TradeDirection.Short;
                confidence = bearishScore;
            }
            else
            {
                return null;
            }

            double spread = state.LatestBar.Range * 0.1;
            double entry = direction == TradeDirection.Long
                ? state.LatestBar.Close + spread
                : state.LatestBar.Close - spread;

            (double sl, double tp1, double tp2, double tp3) = CalculateLevels(
                state, direction, entry);

            double risk = Math.Abs(entry - sl);
            if (risk == 0) return null;

            double rr = Math.Abs(tp2 - entry) / risk;

            var rationale = BuildRationale(state, direction, confidence);

            var signal = new ConfluenceSignal
            {
                Direction = direction,
                Strength = ClassifyStrength(confidence),
                ConfidenceScore = confidence,
                EntryPrice = entry,
                StopLoss = sl,
                TakeProfit1 = tp1,
                TakeProfit2 = tp2,
                TakeProfit3 = tp3,
                SignalTime = state.LatestBar.Time,
                EntryTimeFrame = TimeFrame.M15,
                Rationale = rationale,
                HasOrderBlock = bullishScore > 0 ? HasOBConfluence(state, direction) : false || bearishScore > 0 ? HasOBConfluence(state, direction) : false,
                HasFVG = HasFVGConfluence(state, direction),
                HasLiquiditySweep = HasSweepConfluence(state, direction),
                HasPremiumDiscountConfluence = HasPDConfluence(state, direction),
                HasStructureAlignment = state.StructureBias == direction,
                ConfluenceCount = CountConfluences(state, direction),
                RiskRewardRatio = rr
            };

            return signal;
        }

        private double CalculateDirectionalScore(MarketState state, TradeDirection direction)
        {
            double structureScore = ScoreStructure(state, direction);
            double obScore = ScoreOrderBlocks(state, direction);
            double fvgScore = ScoreFVG(state, direction);
            double sweepScore = ScoreSweeps(state, direction);
            double pdScore = ScorePremiumDiscount(state, direction);
            double sessionScore = ScoreSession(state);

            double total = structureScore * _structureWeight
                         + obScore * _obWeight
                         + fvgScore * _fvgWeight
                         + sweepScore * _sweepWeight
                         + pdScore * _pdWeight
                         + sessionScore * _sessionWeight;

            return Math.Min(1.0, total);
        }

        private double ScoreStructure(MarketState state, TradeDirection direction)
        {
            if (state.StructureBias != direction) return 0.1;

            double strength = _structureAnalyzer.GetStructureStrength(state);
            double bonus = 0;

            var lastBreak = state.StructureBreaks
                .Where(sb => sb.Direction == direction)
                .OrderByDescending(sb => sb.Time)
                .FirstOrDefault();

            if (lastBreak.IsCHoCH)
                bonus += 0.15;

            bonus += lastBreak.BreakStrength * 0.15;

            return Math.Min(1.0, strength + bonus);
        }

        private double ScoreOrderBlocks(MarketState state, TradeDirection direction)
        {
            var relevantOB = _obDetector.GetMostRelevant(state, direction);
            if (!relevantOB.HasValue) return 0;

            var ob = relevantOB.Value;
            double score = ob.Strength * 0.7;

            if (ob.Age <= 2) score += 0.2;
            else if (ob.Age <= 4) score += 0.1;

            double currentPrice = state.LatestBar.Close;
            double obMid = (ob.Top + ob.Bottom) / 2;
            double distance = Math.Abs(currentPrice - obMid) / state.ATR;

            if (distance <= 1.0) score += 0.2;
            else if (distance <= 2.0) score += 0.1;

            return Math.Min(1.0, score);
        }

        private double ScoreFVG(MarketState state, TradeDirection direction)
        {
            var fvg = _fvgDetector.GetNearestUnfilled(state, direction, state.LatestBar.Close);
            if (!fvg.HasValue) return 0;

            var gap = fvg.Value;
            double score = 0.4;

            if (gap.Age <= 3) score += 0.2;
            else if (gap.Age <= 8) score += 0.1;

            double normalizedSize = gap.Size / state.ATR;
            if (normalizedSize > 1.5) score += 0.2;
            else if (normalizedSize > 0.5) score += 0.1;

            if (gap.IsFilled) score *= 0.5;

            return Math.Min(1.0, score);
        }

        private double ScoreSweeps(MarketState state, TradeDirection direction)
        {
            var recentSweeps = state.RecentSweeps
                .Where(s => s.ImpliedDirection == direction)
                .ToList();

            if (recentSweeps.Count == 0) return 0;

            var bestSweep = recentSweeps.OrderByDescending(s => s.Strength).First();
            double score = bestSweep.Strength * 0.5;

            if (bestSweep.Session == SessionType.London || bestSweep.Session == SessionType.NYAM)
                score += 0.2;

            var sweepAge = (state.LatestBar.Time - bestSweep.Time).TotalMinutes;
            if (sweepAge <= 30) score += 0.3;
            else if (sweepAge <= 90) score += 0.15;

            return Math.Min(1.0, score);
        }

        private double ScorePremiumDiscount(MarketState state, TradeDirection direction)
        {
            if (!state.PDZone.HasValue) return 0.3;

            var pd = state.PDZone.Value;

            if (direction == TradeDirection.Long && pd.IsInDiscount)
                return 0.7 + pd.ZoneStrength * 0.3;

            if (direction == TradeDirection.Short && pd.IsInPremium)
                return 0.7 + pd.ZoneStrength * 0.3;

            if (pd.IsInEquilibrium)
                return 0.4;

            return 0.1;
        }

        private double ScoreSession(MarketState state)
        {
            return state.CurrentSession switch
            {
                SessionType.London => 0.8,
                SessionType.NYAM => 0.9,
                SessionType.NYPM => 0.6,
                _ => 0.1
            };
        }

        private (double sl, double tp1, double tp2, double tp3) CalculateLevels(
            MarketState state, TradeDirection direction, double entry)
        {
            double sl;
            double atr = state.ATR;

            sl = direction == TradeDirection.Long
                ? entry - atr * _defaultSLMultiplier
                : entry + atr * _defaultSLMultiplier;

            double risk = Math.Abs(entry - sl);
            double tp1 = direction == TradeDirection.Long
                ? entry + risk * _tp1RR
                : entry - risk * _tp1RR;
            double tp2 = direction == TradeDirection.Long
                ? entry + risk * _tp2RR
                : entry - risk * _tp2RR;
            double tp3 = direction == TradeDirection.Long
                ? entry + risk * _tp3RR
                : entry - risk * _tp3RR;

            return (sl, tp1, tp2, tp3);
        }

        private string BuildRationale(MarketState state, TradeDirection direction, double confidence)
        {
            var sb = new StringBuilder();
            sb.AppendLine($"{direction} signal (confidence: {confidence:F2})");

            if (state.StructureBias == direction)
                sb.AppendLine("- Structure aligned");

            var recentBreak = state.StructureBreaks
                .Where(sb => sb.Direction == direction)
                .OrderByDescending(sb => sb.Time)
                .FirstOrDefault();
            if (recentBreak.IsCHoCH)
                sb.AppendLine("- CHoCH detected (high conviction)");

            if (HasOBConfluence(state, direction))
                sb.AppendLine("- Order block confluence");

            if (HasFVGConfluence(state, direction))
                sb.AppendLine("- Fair Value Gap present");

            if (HasSweepConfluence(state, direction))
                sb.AppendLine("- Liquidity swept");

            if (HasPDConfluence(state, direction))
            {
                var pd = state.PDZone!.Value;
                sb.AppendLine(direction == TradeDirection.Long
                    ? "- Price in discount zone"
                    : "- Price in premium zone");
            }

            sb.Append($"- Confluence count: {CountConfluences(state, direction)}");
            return sb.ToString();
        }

        private bool HasOBConfluence(MarketState state, TradeDirection direction)
        {
            return _obDetector.GetMostRelevant(state, direction).HasValue;
        }

        private bool HasFVGConfluence(MarketState state, TradeDirection direction)
        {
            return _fvgDetector.GetNearestUnfilled(state, direction, state.LatestBar.Close).HasValue;
        }

        private bool HasSweepConfluence(MarketState state, TradeDirection direction)
        {
            return state.RecentSweeps.Any(s =>
                s.ImpliedDirection == direction &&
                (state.LatestBar.Time - s.Time).TotalMinutes <= 120);
        }

        private bool HasPDConfluence(MarketState state, TradeDirection direction)
        {
            if (!state.PDZone.HasValue) return false;
            return direction == TradeDirection.Long
                ? state.PDZone.Value.IsInDiscount
                : state.PDZone.Value.IsInPremium;
        }

        private int CountConfluences(MarketState state, TradeDirection direction)
        {
            int count = 0;
            if (state.StructureBias == direction) count++;
            if (HasOBConfluence(state, direction)) count++;
            if (HasFVGConfluence(state, direction)) count++;
            if (HasSweepConfluence(state, direction)) count++;
            if (HasPDConfluence(state, direction)) count++;
            if (state.CurrentSession != SessionType.Outside) count++;
            return count;
        }

        private static SignalStrength ClassifyStrength(double confidence)
        {
            if (confidence >= 0.85) return SignalStrength.VeryStrong;
            if (confidence >= 0.70) return SignalStrength.Strong;
            if (confidence >= 0.55) return SignalStrength.Moderate;
            return SignalStrength.Weak;
        }
    }
}
