from dataclasses import dataclass

from core.pip import PipCalculator


@dataclass(frozen=True)
class SpreadModel:
    spread_pips: float
    slippage_pips: float = 0.0

    def adjust_entry_long(self, price: float) -> float:
        return price + self._total_spread_price(price)

    def adjust_entry_short(self, price: float) -> float:
        return price - self._total_spread_price(price)

    def current_spread_pips(self) -> float:
        return self.spread_pips

    def _total_spread_price(self, price: float) -> float:
        return PipCalculator.pips_to_price(price, self.spread_pips + self.slippage_pips)


class RealisticSpreadModel:
    """Spread model that uses per-bar spread from bid/ask parquet data.

    Falls back to a fixed default spread when bar.spread_pips is 0
    (e.g. bid-only data or CSV-sourced bars).
    """

    def __init__(
        self,
        default_spread_pips: float = 1.5,
        slippage_pips: float = 0.5,
    ):
        self._default_spread_pips = default_spread_pips
        self._slippage_pips = slippage_pips
        self._current_bar_spread_pips: float = default_spread_pips

    def set_bar_spread(self, spread_pips: float) -> None:
        if spread_pips > 0:
            self._current_bar_spread_pips = spread_pips
        else:
            self._current_bar_spread_pips = self._default_spread_pips

    def adjust_entry_long(self, price: float) -> float:
        total = self._current_bar_spread_pips + self._slippage_pips
        return price + PipCalculator.pips_to_price(price, total)

    def adjust_entry_short(self, price: float) -> float:
        total = self._current_bar_spread_pips + self._slippage_pips
        return price - PipCalculator.pips_to_price(price, total)

    def current_spread_pips(self) -> float:
        return self._current_bar_spread_pips

    @property
    def spread_pips(self) -> float:
        return self._current_bar_spread_pips

    @property
    def slippage_pips(self) -> float:
        return self._slippage_pips
