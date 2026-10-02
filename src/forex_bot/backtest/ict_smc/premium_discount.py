from __future__ import annotations

from ..engine import TradeDirection
from .models import ICTMarketState, PremiumDiscountZone


class PremiumDiscountClassifier:
    def __init__(self, lookback_period: int = 20, equilibrium_buffer: float = 0.0002):
        self._lookback_period = lookback_period
        self._equilibrium_buffer = equilibrium_buffer

    def classify(self, state: ICTMarketState):
        bars = state.bars
        if len(bars) < self._lookback_period:
            return

        recent_bars = bars[-self._lookback_period :]
        range_high = max(b.high for b in recent_bars)
        range_low = min(b.low for b in recent_bars)
        range_size = range_high - range_low

        if range_size == 0:
            state.pd_zone = None
            return

        equilibrium = range_low + range_size * 0.5
        premium_boundary = equilibrium + range_size * 0.25
        discount_boundary = equilibrium - range_size * 0.25

        current_price = bars[-1].close

        is_premium = current_price > premium_boundary
        is_discount = current_price < discount_boundary
        is_equilibrium = not is_premium and not is_discount

        if is_premium:
            zone = TradeDirection.SHORT
        elif is_discount:
            zone = TradeDirection.LONG
        else:
            zone = TradeDirection.NEUTRAL

        distance_from_eq = (current_price - equilibrium) / equilibrium
        normalized_buffer = self._equilibrium_buffer / equilibrium
        near_equilibrium = abs(distance_from_eq) < normalized_buffer * 10

        zone_strength = 0.5
        if is_premium:
            denom = range_high - premium_boundary
            zone_strength = min(1.0, 0.5 + (current_price - premium_boundary) / denom * 0.5) if denom > 0 else 0.5
        elif is_discount:
            denom = discount_boundary - range_low
            zone_strength = min(1.0, 0.5 + (discount_boundary - current_price) / denom * 0.5) if denom > 0 else 0.5

        if near_equilibrium:
            zone_strength = max(zone_strength, 0.7)

        state.pd_zone = PremiumDiscountZone(
            equilibrium=equilibrium,
            premium_boundary=premium_boundary,
            discount_boundary=discount_boundary,
            current_price=current_price,
            current_zone=zone,
            distance_from_equilibrium=distance_from_eq,
            zone_strength=zone_strength,
            is_in_premium=is_premium,
            is_in_discount=is_discount,
            is_in_equilibrium=is_equilibrium,
        )

    def is_discount_entry(self, state: ICTMarketState, trade_direction: TradeDirection) -> bool:
        if state.pd_zone is None:
            return False
        return trade_direction == TradeDirection.LONG and state.pd_zone.is_in_discount

    def is_premium_entry(self, state: ICTMarketState, trade_direction: TradeDirection) -> bool:
        if state.pd_zone is None:
            return False
        return trade_direction == TradeDirection.SHORT and state.pd_zone.is_in_premium
