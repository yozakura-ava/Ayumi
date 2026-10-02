class PipCalculator:
    """Single source of truth for pip value and spread calculations."""

    @staticmethod
    def pip_value(price: float) -> float:
        if price >= 50.0:
            return 0.01
        elif price >= 1.0:
            return 0.0001
        else:
            return 0.00000001

    @staticmethod
    def pips_to_price(price: float, pips: float) -> float:
        return pips * PipCalculator.pip_value(price)

    @staticmethod
    def price_to_pips(price: float, price_diff: float) -> float:
        pv = PipCalculator.pip_value(price)
        return price_diff / pv if pv > 0 else 0.0
