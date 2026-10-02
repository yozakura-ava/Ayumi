using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public struct MRSignalConfig
    {
        public int BBPeriod;
        public double BBStdDevMultiplier;
        public int RSIPeriod;
        public double RSIOversold;
        public double RSIOverbought;
        public int EMA200Period;
        public double TouchBuffer;

        public static MRSignalConfig Default => new MRSignalConfig
        {
            BBPeriod = 20,
            BBStdDevMultiplier = 2.0,
            RSIPeriod = 14,
            RSIOversold = 35.0,
            RSIOverbought = 65.0,
            EMA200Period = 200,
            TouchBuffer = 0.0
        };

        public static MRSignalConfig Conservative => new MRSignalConfig
        {
            BBPeriod = 20,
            BBStdDevMultiplier = 1.5,
            RSIPeriod = 14,
            RSIOversold = 30.0,
            RSIOverbought = 70.0,
            EMA200Period = 200,
            TouchBuffer = 0.0
        };

        public static MRSignalConfig Aggressive => new MRSignalConfig
        {
            BBPeriod = 20,
            BBStdDevMultiplier = 2.0,
            RSIPeriod = 14,
            RSIOversold = 40.0,
            RSIOverbought = 60.0,
            EMA200Period = 200,
            TouchBuffer = 0.0002
        };
    }

    public struct MRSignal
    {
        public TradeDirection Direction;
        public double EntryPrice;
        public DateTime SignalTime;
        public double BBUpper;
        public double BBMiddle;
        public double BBLower;
        public double RSI;
        public double D1EMA200;
        public double ConfidenceScore;
        public bool IsValid;
    }

    public struct MRSignalIndicators
    {
        public BollingerBands Bands;
        public double RSI;
        public double D1EMA200;
        public bool BBTouchLong;
        public bool BBTouchShort;
        public bool RSIOversold;
        public bool RSIOverbought;
        public bool TrendBullish;
        public bool TrendBearish;
    }

    public class MRSignalModule
    {
        private readonly MRSignalConfig _config;

        public MRSignalModule(MRSignalConfig config)
        {
            _config = config;
        }

        public MRSignalIndicators EvaluateIndicators(List<Bar> h1Bars, List<Bar> d1Bars)
        {
            var bands = MRExitManager.CalculateBollingerBands(h1Bars, _config.BBPeriod, _config.BBStdDevMultiplier);
            double rsi = CalculateRSI(h1Bars, _config.RSIPeriod);
            double d1Ema = MRExitManager.CalculateEMA(d1Bars, _config.EMA200Period);

            Bar current = h1Bars[h1Bars.Count - 1];

            bool bbTouchLong = CheckBBTouch(current, bands, TradeDirection.Long);
            bool bbTouchShort = CheckBBTouch(current, bands, TradeDirection.Short);
            bool rsiOversold = rsi < _config.RSIOversold;
            bool rsiOverbought = rsi > _config.RSIOverbought;
            bool trendBullish = current.Close > d1Ema && d1Ema > 0;
            bool trendBearish = current.Close < d1Ema && d1Ema > 0;

            return new MRSignalIndicators
            {
                Bands = bands,
                RSI = rsi,
                D1EMA200 = d1Ema,
                BBTouchLong = bbTouchLong,
                BBTouchShort = bbTouchShort,
                RSIOversold = rsiOversold,
                RSIOverbought = rsiOverbought,
                TrendBullish = trendBullish,
                TrendBearish = trendBearish
            };
        }

        public MRSignal GenerateSignal(List<Bar> h1Bars, List<Bar> d1Bars)
        {
            if (h1Bars == null || h1Bars.Count < _config.BBPeriod + 1)
                return InvalidSignal();

            if (d1Bars == null || d1Bars.Count < _config.EMA200Period)
                return InvalidSignal();

            var indicators = EvaluateIndicators(h1Bars, d1Bars);

            bool longSetup = indicators.BBTouchLong && indicators.RSIOversold && indicators.TrendBullish;
            bool shortSetup = indicators.BBTouchShort && indicators.RSIOverbought && indicators.TrendBearish;

            if (!longSetup && !shortSetup)
                return InvalidSignal();

            Bar current = h1Bars[h1Bars.Count - 1];
            double confidence = CalculateConfidence(indicators, longSetup);

            return new MRSignal
            {
                Direction = longSetup ? TradeDirection.Long : TradeDirection.Short,
                EntryPrice = current.Close,
                SignalTime = current.Time,
                BBUpper = indicators.Bands.Upper,
                BBMiddle = indicators.Bands.Middle,
                BBLower = indicators.Bands.Lower,
                RSI = indicators.RSI,
                D1EMA200 = indicators.D1EMA200,
                ConfidenceScore = confidence,
                IsValid = true
            };
        }

        public static double CalculateRSI(List<Bar> bars, int period = 14)
        {
            if (bars == null || bars.Count < period + 1)
                return 50.0;

            double avgGain = 0;
            double avgLoss = 0;

            double firstGainSum = 0;
            double firstLossSum = 0;
            for (int i = bars.Count - period; i < bars.Count; i++)
            {
                double change = bars[i].Close - bars[i - 1].Close;
                if (change > 0)
                    firstGainSum += change;
                else
                    firstLossSum += Math.Abs(change);
            }

            avgGain = firstGainSum / period;
            avgLoss = firstLossSum / period;

            if (avgLoss == 0) return 100.0;

            double rs = avgGain / avgLoss;
            return 100.0 - (100.0 / (1.0 + rs));
        }

        private bool CheckBBTouch(Bar bar, BollingerBands bands, TradeDirection direction)
        {
            double buffer = _config.TouchBuffer;

            if (direction == TradeDirection.Long)
                return bar.Low <= bands.Lower + buffer;
            else
                return bar.High >= bands.Upper - buffer;
        }

        private double CalculateConfidence(MRSignalIndicators ind, bool isLong)
        {
            double score = 0.0;
            double maxScore = 0.0;

            double bbPenetration;
            if (isLong)
            {
                bbPenetration = (ind.Bands.Lower - ind.Bands.Middle) > 0
                    ? Math.Min(1.0, (ind.Bands.Lower - ind.RSI / 100.0 * ind.Bands.Middle) / (ind.Bands.Lower - ind.Bands.Middle))
                    : 0.5;
            }
            else
            {
                bbPenetration = (ind.Bands.Upper - ind.Bands.Middle) > 0
                    ? Math.Min(1.0, (ind.RSI / 100.0 * ind.Bands.Upper - ind.Bands.Middle) / (ind.Bands.Upper - ind.Bands.Middle))
                    : 0.5;
            }

            score += bbPenetration * 0.3;
            maxScore += 0.3;

            double rsiScore;
            if (isLong)
                rsiScore = Math.Max(0, Math.Min(1, (_config.RSIOversold - ind.RSI) / _config.RSIOversold));
            else
                rsiScore = Math.Max(0, Math.Min(1, (ind.RSI - _config.RSIOverbought) / (100.0 - _config.RSIOverbought)));

            score += rsiScore * 0.3;
            maxScore += 0.3;

            double trendScore = 0.5;
            score += trendScore * 0.2;
            maxScore += 0.2;

            double momentumScore = isLong ? Math.Max(0, 1.0 - ind.RSI / 30.0) : Math.Max(0, (ind.RSI - 70.0) / 30.0);
            score += momentumScore * 0.2;
            maxScore += 0.2;

            return maxScore > 0 ? score / maxScore : 0;
        }

        private static MRSignal InvalidSignal()
        {
            return new MRSignal
            {
                Direction = TradeDirection.Neutral,
                EntryPrice = 0,
                SignalTime = DateTime.MinValue,
                BBUpper = 0,
                BBMiddle = 0,
                BBLower = 0,
                RSI = 0,
                D1EMA200 = 0,
                ConfidenceScore = 0,
                IsValid = false
            };
        }
    }
}
