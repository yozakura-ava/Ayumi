using System;
using System.Collections.Generic;

namespace ICTSMC
{
    public static class MockDataGenerator
    {
        private static readonly Random _rng = new Random(42);

        public static List<Bar> GenerateBullishTrend(int count = 100, double startPrice = 1.1000, double volatility = 0.001)
        {
            var bars = new List<Bar>();
            double price = startPrice;
            var baseTime = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);

            for (int i = 0; i < count; i++)
            {
                double trend = volatility * 0.3;
                double noise = (NextDouble() - 0.45) * volatility;
                double change = trend + noise;

                double open = price;
                double close = price + change;
                double high = Math.Max(open, close) + NextDouble() * volatility * 0.5;
                double low = Math.Min(open, close) - NextDouble() * volatility * 0.3;

                bars.Add(new Bar
                {
                    Time = baseTime.AddMinutes(i * 15),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = 1000 + NextDouble() * 5000,
                    Period = TimeFrame.M15
                });

                price = close;
            }

            return bars;
        }

        public static List<Bar> GenerateBearishTrend(int count = 100, double startPrice = 1.1100, double volatility = 0.001)
        {
            var bars = new List<Bar>();
            double price = startPrice;
            var baseTime = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);

            for (int i = 0; i < count; i++)
            {
                double trend = -volatility * 0.3;
                double noise = (NextDouble() - 0.55) * volatility;
                double change = trend + noise;

                double open = price;
                double close = price + change;
                double high = Math.Max(open, close) + NextDouble() * volatility * 0.3;
                double low = Math.Min(open, close) - NextDouble() * volatility * 0.5;

                bars.Add(new Bar
                {
                    Time = baseTime.AddMinutes(i * 15),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = 1000 + NextDouble() * 5000,
                    Period = TimeFrame.M15
                });

                price = close;
            }

            return bars;
        }

        public static List<Bar> GenerateICTSetup(bool isLong = true, int count = 80, double startPrice = 1.1000)
        {
            var bars = new List<Bar>();
            double price = startPrice;
            var baseTime = new DateTime(2026, 4, 2, 7, 0, 0, DateTimeKind.Utc);
            double vol = 0.0008;

            for (int i = 0; i < count; i++)
            {
                double change;
                double noise = (NextDouble() - 0.5) * vol;

                if (isLong)
                {
                    if (i < 20) change = noise - vol * 0.1;
                    else if (i < 25) change = -vol * 0.5 + noise;
                    else if (i < 30) change = vol * 0.3 + noise;
                    else if (i < 45) change = noise;
                    else if (i < 50) change = vol * 0.4 + noise;
                    else if (i < 55) change = -vol * 0.3 + noise;
                    else if (i < 60) change = -vol * 0.6 + noise;
                    else if (i < 65) change = vol * 0.2 + noise;
                    else change = vol * 0.3 + noise;
                }
                else
                {
                    if (i < 20) change = noise + vol * 0.1;
                    else if (i < 25) change = vol * 0.5 + noise;
                    else if (i < 30) change = -vol * 0.3 + noise;
                    else if (i < 45) change = noise;
                    else if (i < 50) change = -vol * 0.4 + noise;
                    else if (i < 55) change = vol * 0.3 + noise;
                    else if (i < 60) change = vol * 0.6 + noise;
                    else if (i < 65) change = -vol * 0.2 + noise;
                    else change = -vol * 0.3 + noise;
                }

                double open = price;
                double close = price + change;
                double high = Math.Max(open, close) + NextDouble() * vol * 0.4;
                double low = Math.Min(open, close) - NextDouble() * vol * 0.4;

                bars.Add(new Bar
                {
                    Time = baseTime.AddMinutes(i * 15),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = 1000 + NextDouble() * 5000,
                    Period = TimeFrame.M15
                });

                price = close;
            }

            return bars;
        }

        public static List<Bar> GenerateRangingMarket(int count = 100, double startPrice = 1.1000, double volatility = 0.001)
        {
            var bars = new List<Bar>();
            double price = startPrice;
            var baseTime = new DateTime(2026, 4, 2, 8, 0, 0, DateTimeKind.Utc);
            double upperBound = startPrice + volatility * 15;
            double lowerBound = startPrice - volatility * 15;

            for (int i = 0; i < count; i++)
            {
                double change = (NextDouble() - 0.5) * volatility * 2;
                double meanRevert = (startPrice - price) * 0.05;
                change += meanRevert;

                double open = price;
                double close = price + change;
                close = Math.Max(lowerBound, Math.Min(upperBound, close));
                double high = Math.Max(open, close) + NextDouble() * volatility * 0.3;
                double low = Math.Min(open, close) - NextDouble() * volatility * 0.3;

                bars.Add(new Bar
                {
                    Time = baseTime.AddMinutes(i * 15),
                    Open = open,
                    High = high,
                    Low = low,
                    Close = close,
                    Volume = 1000 + NextDouble() * 5000,
                    Period = TimeFrame.M15
                });

                price = close;
            }

            return bars;
        }

        public static MarketState BuildState(List<Bar> bars, SessionType session = SessionType.London)
        {
            return new MarketState
            {
                Bars = bars,
                CurrentSession = session
            };
        }

        private static double NextDouble()
        {
            lock (_rng)
            {
                return _rng.NextDouble();
            }
        }
    }
}
