using System;
using System.Collections.Generic;
using System.Linq;

namespace ICTSMC
{
    public class PremiumDiscountClassifier
    {
        private readonly int _lookbackPeriod;
        private readonly double _equilibriumBuffer;

        public PremiumDiscountClassifier(
            int lookbackPeriod = 20,
            double equilibriumBuffer = 0.0002)
        {
            _lookbackPeriod = lookbackPeriod;
            _equilibriumBuffer = equilibriumBuffer;
        }

        public void Classify(MarketState state)
        {
            var bars = state.Bars;
            if (bars.Count < _lookbackPeriod) return;

            var recentBars = bars.Skip(Math.Max(0, bars.Count - _lookbackPeriod)).Take(_lookbackPeriod).ToList();
            double rangeHigh = recentBars.Max(b => b.High);
            double rangeLow = recentBars.Min(b => b.Low);
            double range = rangeHigh - rangeLow;

            if (range == 0)
            {
                state.PDZone = null;
                return;
            }

            double equilibrium = rangeLow + range * 0.5;

            double premiumBoundary = equilibrium + range * 0.25;
            double discountBoundary = equilibrium - range * 0.25;

            double currentPrice = bars[bars.Count - 1].Close;

            TradeDirection zone;
            bool isPremium = currentPrice > premiumBoundary;
            bool isDiscount = currentPrice < discountBoundary;
            bool isEquilibrium = !isPremium && !isDiscount;

            if (isPremium)
                zone = TradeDirection.Short;
            else if (isDiscount)
                zone = TradeDirection.Long;
            else
                zone = TradeDirection.Neutral;

            double distanceFromEq = (currentPrice - equilibrium) / equilibrium;
            double normalizedBuffer = _equilibriumBuffer / equilibrium;
            bool nearEquilibrium = Math.Abs(distanceFromEq) < normalizedBuffer * 10;

            double zoneStrength = 0.5;
            if (isPremium)
                zoneStrength = Math.Min(1.0, 0.5 + (currentPrice - premiumBoundary) / (rangeHigh - premiumBoundary) * 0.5);
            else if (isDiscount)
                zoneStrength = Math.Min(1.0, 0.5 + (discountBoundary - currentPrice) / (discountBoundary - rangeLow) * 0.5);

            if (nearEquilibrium)
                zoneStrength = Math.Max(zoneStrength, 0.7);

            state.PDZone = new PremiumDiscountZone
            {
                Equilibrium = equilibrium,
                PremiumBoundary = premiumBoundary,
                DiscountBoundary = discountBoundary,
                CurrentPrice = currentPrice,
                CurrentZone = zone,
                DistanceFromEquilibrium = distanceFromEq,
                ZoneStrength = zoneStrength,
                IsInPremium = isPremium,
                IsInDiscount = isDiscount,
                IsInEquilibrium = isEquilibrium
            };
        }

        public bool IsDiscountEntry(MarketState state, TradeDirection tradeDirection)
        {
            if (!state.PDZone.HasValue) return false;
            var pd = state.PDZone.Value;
            return tradeDirection == TradeDirection.Long && pd.IsInDiscount;
        }

        public bool IsPremiumEntry(MarketState state, TradeDirection tradeDirection)
        {
            if (!state.PDZone.HasValue) return false;
            var pd = state.PDZone.Value;
            return tradeDirection == TradeDirection.Short && pd.IsInPremium;
        }
    }
}
