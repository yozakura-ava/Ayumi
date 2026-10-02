"""VolumeCalculator — per-symbol volume conversion between lots and cTrader raw volume.

cTrader represents volume in integer units where:
    raw_volume = lots × lot_size

For forex (EURUSD): lot_size = 100,000 → 1 lot = 100,000 units
For crypto (BTCUSD): lot_size = 100 → 1 lot = 100 units

Lifecycle: Depends on OpenApiSpotFeed._symbols being populated. Callers must
ensure symbol discovery has run before invoking lots_to_volume / volume_to_lots
for a given symbol_id. Unknown symbols raise ValueError on volume methods and
warn+fallback on price decoding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Tuple

if TYPE_CHECKING:
    from .market_data_feed import SymbolInfo

logger = logging.getLogger("ayumi.ctrader.volume_calculator")


class VolumeCalculator:
    """Converts between lots, cTrader protocol volume, and notional.

    Knows the per-symbol lot size from SymbolInfo, avoiding hardcoded 100k.
    Holds a reference to OpenApiSpotFeed._symbols dict — sees updates live.
    """

    def __init__(self, symbols: Dict[int, "SymbolInfo"]):
        self._symbols = symbols  # reference to OpenApiSpotFeed._symbols

    def lots_to_volume(self, symbol_id: int, lots: float) -> int:
        """Strategy lots → cTrader protocol integer volume.

        Raises ValueError if symbol_id unknown.
        """
        sym = self._symbols.get(symbol_id)
        if sym is None:
            raise ValueError(f"Unknown symbol_id: {symbol_id}")
        return int(round(lots * sym.lot_size))

    def volume_to_lots(self, symbol_id: int, volume: int) -> float:
        """cTrader protocol volume → strategy lots.

        Raises ValueError if symbol_id unknown.
        """
        sym = self._symbols.get(symbol_id)
        if sym is None:
            raise ValueError(f"Unknown symbol_id: {symbol_id}")
        return volume / sym.lot_size

    def validate_volume(self, symbol_id: int, volume: int) -> Tuple[bool, str]:
        """Validate volume against symbol min/max/step constraints.

        Returns (True, "OK") if valid, (False, reason) if invalid.
        """
        sym = self._symbols.get(symbol_id)
        if sym is None:
            return False, f"Unknown symbol_id: {symbol_id}"
        if sym.min_volume and volume < sym.min_volume:
            return False, f"Volume {volume} < min {sym.min_volume}"
        if sym.max_volume and volume > sym.max_volume:
            return False, f"Volume {volume} > max {sym.max_volume}"
        if sym.step_volume and sym.step_volume > 0:
            remainder = volume % sym.step_volume
            if remainder != 0:
                return False, f"Volume {volume} not aligned to step {sym.step_volume}"
        return True, "OK"

    def price_from_raw(self, symbol_id: int, raw_price: int) -> float:
        """Decode a cTrader raw price using per-symbol digits.

        Falls back to /100_000 with warning for unknown symbols.
        """
        sym = self._symbols.get(symbol_id)
        if sym is None:
            logger.warning(
                "price_from_raw: unknown symbol_id=%s, using default 5 digits",
                symbol_id,
            )
            return raw_price / 100_000
        return raw_price / (10**sym.digits)
