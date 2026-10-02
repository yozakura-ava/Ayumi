"""Currency-leg exposure tracker with correlation penalty.

Sits on top of :class:`risk.correlation_matrix.CorrelationMatrix` and
:class:`risk.correlation_sizer.CorrelationAwareSizer` to add a
*currency-leg* decomposition layer: instead of looking at pair-level
correlation only, it decomposes each position into its constituent
currencies (base / quote) and tracks net exposure per currency.

This enables two additional risk controls:

1. **Correlation penalty** — adjusts the risk of a new trade based on
   the average correlation with existing same-direction positions
   (``Adj = Base × (1 - Avg ρ)`` per SRB-AYUMI-012 §5.1 rec 3).

2. **Currency cap** — blocks a new trade if it would push any single
   currency's exposure above 2 % of equity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from risk.correlation_matrix import CorrelationMatrix

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Mapping of pair symbol to (base_currency, quote_currency).
CURRENCY_LEGS: dict[str, tuple[str, str]] = {
    "GBPUSD": ("GBP", "USD"),
    "EURUSD": ("EUR", "USD"),
    "USDJPY": ("USD", "JPY"),
    "EURCHF": ("EUR", "CHF"),
    "GBPJPY": ("GBP", "JPY"),
    "AUDUSD": ("AUD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "XAUUSD": ("XAU", "USD"),
}

#: Approximate USD value per unit for each currency (spot approximation).
#: JPY is ~0.0067 USD per yen; XAU (gold) is ~2000 USD per ounce.
#: These are rough constants for risk-percentage estimation only.
_USD_PER_UNIT: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "JPY": 0.0067,
    "CHF": 1.12,
    "AUD": 0.66,
    "CAD": 0.73,
    "XAU": 2000.0,
}


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class PositionExposure:
    """A position registered with the exposure tracker.

    Parameters
    ----------
    pair : str
        Instrument symbol (must exist in :data:`CURRENCY_LEGS`).
    direction : str
        ``"long"`` or ``"short"``.
    size_units : float
        Position size in BASE currency units (e.g. 100 000 = 1 standard lot).
    """

    pair: str
    direction: Literal["long", "short"]
    size_units: float


@dataclass
class CurrencyExposure:
    """Net exposure for a single currency.

    Attributes
    ----------
    currency : str
        ISO currency code (e.g. ``"USD"``).
    net_units : float
        Net units of the currency.  Positive = long, negative = short.
    """

    currency: str
    net_units: float


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #


class CurrencyExposureTracker:
    """Track currency-leg exposure across positions and enforce risk caps.

    Parameters
    ----------
    correlation_matrix : CorrelationMatrix
        Pre-populated matrix used for correlation lookups between pairs.
    """

    def __init__(self, correlation_matrix: CorrelationMatrix) -> None:
        self.cm = correlation_matrix
        self._positions: dict[str, PositionExposure] = {}

    # ------------------------------------------------------------------ #
    # Position registry
    # ------------------------------------------------------------------ #

    def register_position(self, pos: PositionExposure) -> None:
        """Register or overwrite a position for *pos.pair*."""
        if pos.pair not in CURRENCY_LEGS:
            raise KeyError(f"Unknown pair: {pos.pair}")
        self._positions[pos.pair] = pos
        logger.debug("Registered %s %s %.0f units", pos.direction, pos.pair, pos.size_units)

    def unregister_position(self, pair: str) -> bool:
        """Remove the position for *pair*.  Returns ``True`` if removed."""
        removed = self._positions.pop(pair, None)
        if removed:
            logger.debug("Unregistered position: %s", pair)
        return removed is not None

    # ------------------------------------------------------------------ #
    # Currency-leg decomposition
    # ------------------------------------------------------------------ #

    def net_exposure(self) -> dict[str, float]:
        """Return net currency exposure as ``{currency: net_units}``.

        For a long position of *N* units in ``GBPUSD``:
        - GBP: +N
        - USD: -N

        For a short position, the signs flip:
        - GBP: -N
        - USD: +N
        """
        result: dict[str, float] = {}
        for pair, pos in self._positions.items():
            base, quote = CURRENCY_LEGS[pair]
            size = pos.size_units
            if pos.direction == "long":
                result[base] = result.get(base, 0.0) + size
                result[quote] = result.get(quote, 0.0) - size
            else:  # short
                result[base] = result.get(base, 0.0) - size
                result[quote] = result.get(quote, 0.0) + size
        return result

    def currency_risk_pct(self, equity: float) -> dict[str, float]:
        """Return risk as a percentage of *equity* for each currency.

        Uses :data:`_USD_PER_UNIT` spot approximations to convert
        net currency units into USD, then divides by equity.
        """
        net = self.net_exposure()
        result: dict[str, float] = {}
        for currency, units in net.items():
            usd_value = abs(units) * _USD_PER_UNIT.get(currency, 1.0)
            result[currency] = usd_value / equity if equity > 0 else 0.0
        return result

    # ------------------------------------------------------------------ #
    # Correlation penalty
    # ------------------------------------------------------------------ #

    def aggregate_correlated_risk_pct(
        self,
        new_pair: str,
        new_risk_pct: float,
    ) -> float:
        """Adjust *new_risk_pct* based on average correlation with existing positions.

        Formula (SRB-AYUMI-012 §5.1 rec 3)::

            Adj = Base × (1 - Avg ρ)

        Only existing positions with ``|ρ| > 0.3`` are considered.
        If no existing positions meet the threshold, returns *new_risk_pct*
        unchanged.
        """
        existing = [pos for pos in self._positions.values() if pos.pair != new_pair]
        if not existing:
            return new_risk_pct

        correlations: list[float] = []
        for pos in existing:
            rho = self.cm.get_correlation(pos.pair, new_pair)
            if abs(rho) > 0.3:
                correlations.append(rho)

        if not correlations:
            return new_risk_pct

        avg_rho = sum(correlations) / len(correlations)
        adjusted = new_risk_pct * (1.0 - avg_rho)
        logger.debug(
            "Correlation penalty for %s: avg_ρ=%.3f, base=%.5f → adjusted=%.5f",
            new_pair,
            avg_rho,
            new_risk_pct,
            adjusted,
        )
        return adjusted

    def correlation_penalty_for_new_pair(
        self,
        new_pair: str,
        new_direction: str,
        base_risk_pct: float,
    ) -> dict:
        """Return a detailed penalty report for a prospective trade.

        Returns
        -------
        dict with keys:
            - ``adjusted_risk_pct`` — risk after correlation penalty
            - ``blocked`` — always ``False`` (penalty only, no block)
            - ``reason`` — human-readable explanation
            - ``avg_correlation`` — average ρ with correlated existing positions
        """
        existing = [pos for pos in self._positions.values() if pos.pair != new_pair]
        correlations: list[float] = []
        for pos in existing:
            rho = self.cm.get_correlation(pos.pair, new_pair)
            if abs(rho) > 0.3:
                correlations.append(rho)

        if not correlations:
            return {
                "adjusted_risk_pct": base_risk_pct,
                "blocked": False,
                "reason": "No correlated positions above threshold (|ρ| > 0.3).",
                "avg_correlation": 0.0,
            }

        avg_rho = sum(correlations) / len(correlations)
        adjusted = base_risk_pct * (1.0 - avg_rho)
        return {
            "adjusted_risk_pct": adjusted,
            "blocked": False,
            "reason": (f"Adjusted by (1 - {avg_rho:.3f}) due to {len(correlations)} correlated position(s)."),
            "avg_correlation": avg_rho,
        }

    # ------------------------------------------------------------------ #
    # Currency cap
    # ------------------------------------------------------------------ #

    def currency_cap_check(
        self,
        new_pair: str,
        new_risk_pct: float,
        equity: float,
        currency_cap_pct: float = 0.02,
    ) -> bool:
        """Return ``True`` if the new position keeps per-currency exposure within cap.

        Checks whether adding *new_risk_pct* of risk in the base and quote
        currencies of *new_pair* would push either currency's total exposure
        above *currency_cap_pct* of equity.
        """
        if new_pair not in CURRENCY_LEGS:
            raise KeyError(f"Unknown pair: {new_pair}")

        base, quote = CURRENCY_LEGS[new_pair]
        current_risk = self.currency_risk_pct(equity)

        # The new position adds risk to both base and quote currencies
        base_ok = current_risk.get(base, 0.0) + new_risk_pct <= currency_cap_pct
        quote_ok = current_risk.get(quote, 0.0) + new_risk_pct <= currency_cap_pct
        return base_ok and quote_ok

    # ------------------------------------------------------------------ #
    # Combined decision
    # ------------------------------------------------------------------ #

    def would_block(
        self,
        new_pair: str,
        new_direction: str,
        new_risk_pct: float,
        equity: float,
    ) -> tuple[bool, str]:
        """Decide whether a new trade should be blocked.

        Combines the currency cap check with correlation information.
        Returns ``(blocked, reason)``.
        """
        if new_pair not in CURRENCY_LEGS:
            raise KeyError(f"Unknown pair: {new_pair}")

        # Currency cap check
        cap_ok = self.currency_cap_check(new_pair, new_risk_pct, equity)
        if not cap_ok:
            base, quote = CURRENCY_LEGS[new_pair]
            current_risk = self.currency_risk_pct(equity)
            return True, (
                f"Currency cap exceeded: adding {new_risk_pct:.3%} risk to "
                f"{base}/{quote} would push exposure above 2% of equity "
                f"(current {base}: {current_risk.get(base, 0.0):.3%}, "
                f"{quote}: {current_risk.get(quote, 0.0):.3%})."
            )

        return False, "OK"

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def positions(self) -> list[PositionExposure]:
        """Snapshot of registered positions."""
        return list(self._positions.values())


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def default_tracker(equity: float = 10_000) -> CurrencyExposureTracker:
    """Return a :class:`CurrencyExposureTracker` with an empty matrix.

    Parameters
    ----------
    equity : float
        Current account equity (used only for informational purposes;
        actual equity is passed to methods like :meth:`currency_risk_pct`).
    """
    cm = CorrelationMatrix()
    return CurrencyExposureTracker(cm)
