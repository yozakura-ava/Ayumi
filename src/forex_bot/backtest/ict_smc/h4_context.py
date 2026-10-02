from __future__ import annotations

from dataclasses import dataclass, field

from ..engine import Bar, TradeDirection
from .fvg import FVGDetector
from .models import ICTMarketState, OrderBlock
from .order_block import OrderBlockDetector


@dataclass
class H4ZoneMapping:
    top: float
    bottom: float
    direction: TradeDirection
    zone_type: str  # "order_block" or "fvg"
    strength: float = 0.0
    is_filled: bool = False


@dataclass
class H4ContextResult:
    order_block_zones: list[H4ZoneMapping] = field(default_factory=list)
    fvg_zones: list[H4ZoneMapping] = field(default_factory=list)
    bullish_score: float = 0.0
    bearish_score: float = 0.0
    confluence_count_bullish: int = 0
    confluence_count_bearish: int = 0


class H4ContextModule:
    def __init__(
        self,
        ob_freshness_window: int = 10,
        ob_min_body_ratio: float = 0.5,
        fvg_max_age: int = 40,
        fvg_mini_threshold: float = 0.0003,
        proximity_atr_multiplier: float = 2.0,
    ):
        self._ob_detector = OrderBlockDetector(
            freshness_window=ob_freshness_window,
            min_body_ratio=ob_min_body_ratio,
        )
        self._fvg_detector = FVGDetector(
            max_age=fvg_max_age,
            mini_threshold=fvg_mini_threshold,
        )
        self._proximity_atr_multiplier = proximity_atr_multiplier

    def analyze(
        self,
        h4_bars: list[Bar],
        h1_current_price: float,
        h1_atr: float,
    ) -> H4ContextResult:
        if not h4_bars or len(h4_bars) < 10:
            return H4ContextResult()

        h4_state = ICTMarketState(h4_bars)
        self._ob_detector.detect(h4_state)
        self._fvg_detector.detect(h4_state)

        ob_zones = self._map_order_blocks(h4_state.active_order_blocks)
        fvg_zones = self._map_fvg_zones(h4_state.active_fvgs)

        bullish_score, bearish_score = self._calculate_scores(ob_zones, fvg_zones, h1_current_price, h1_atr)

        bullish_confluences = self._count_zone_confluences(
            ob_zones, fvg_zones, TradeDirection.LONG, h1_current_price, h1_atr
        )
        bearish_confluences = self._count_zone_confluences(
            ob_zones, fvg_zones, TradeDirection.SHORT, h1_current_price, h1_atr
        )

        return H4ContextResult(
            order_block_zones=ob_zones,
            fvg_zones=fvg_zones,
            bullish_score=bullish_score,
            bearish_score=bearish_score,
            confluence_count_bullish=bullish_confluences,
            confluence_count_bearish=bearish_confluences,
        )

    def _map_order_blocks(self, order_blocks: list[OrderBlock]) -> list[H4ZoneMapping]:
        zones = []
        for ob in order_blocks:
            if ob.is_mitigated:
                continue
            zones.append(
                H4ZoneMapping(
                    top=ob.top,
                    bottom=ob.bottom,
                    direction=ob.direction,
                    zone_type="order_block",
                    strength=ob.strength,
                )
            )
        return zones

    def _map_fvg_zones(self, fvgs: list) -> list[H4ZoneMapping]:
        zones = []
        for fvg in fvgs:
            if fvg.is_mitigated:
                continue
            zones.append(
                H4ZoneMapping(
                    top=fvg.top,
                    bottom=fvg.bottom,
                    direction=fvg.direction,
                    zone_type="fvg",
                    strength=0.5 if not fvg.is_filled else 0.25,
                    is_filled=fvg.is_filled,
                )
            )
        return zones

    def _calculate_scores(
        self,
        ob_zones: list[H4ZoneMapping],
        fvg_zones: list[H4ZoneMapping],
        current_price: float,
        atr: float,
    ) -> tuple[float, float]:
        if atr == 0:
            return (0.0, 0.0)

        bullish_score = 0.0
        bearish_score = 0.0

        for zone in ob_zones:
            if self._price_in_zone(current_price, zone):
                score = self._zone_proximity_score(current_price, zone, atr)
                if zone.direction == TradeDirection.LONG:
                    bullish_score += score * zone.strength
                else:
                    bearish_score += score * zone.strength

        for zone in fvg_zones:
            if zone.is_filled:
                continue
            if self._price_near_zone(current_price, zone, atr * 3):
                score = self._zone_proximity_score(current_price, zone, atr)
                if zone.direction == TradeDirection.LONG:
                    bullish_score += score * 0.7
                else:
                    bearish_score += score * 0.7

        return (
            min(1.0, bullish_score),
            min(1.0, bearish_score),
        )

    def _count_zone_confluences(
        self,
        ob_zones: list[H4ZoneMapping],
        fvg_zones: list[H4ZoneMapping],
        direction: TradeDirection,
        current_price: float,
        atr: float,
    ) -> int:
        count = 0
        for zone in ob_zones:
            if zone.direction == direction and self._price_in_zone(current_price, zone):
                count += 1
        for zone in fvg_zones:
            if zone.direction == direction and not zone.is_filled:
                if self._price_near_zone(current_price, zone, atr * 3):
                    count += 1
        return count

    def _price_in_zone(self, price: float, zone: H4ZoneMapping) -> bool:
        return zone.bottom <= price <= zone.top

    def _price_near_zone(self, price: float, zone: H4ZoneMapping, threshold: float) -> bool:
        zone_mid = (zone.top + zone.bottom) / 2
        return abs(price - zone_mid) <= threshold

    def _zone_proximity_score(self, price: float, zone: H4ZoneMapping, atr: float) -> float:
        if atr == 0:
            return 0.0
        zone_mid = (zone.top + zone.bottom) / 2
        distance = abs(price - zone_mid) / atr
        if distance <= 0.5:
            return 1.0
        if distance <= 1.0:
            return 0.7
        if distance <= 2.0:
            return 0.4
        return 0.1
