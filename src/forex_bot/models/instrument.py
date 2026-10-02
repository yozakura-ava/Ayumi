"""Instrument model with symbol-type classification.

The ``SymbolType`` enum is the foundational asset-class tag that the
confidence engine uses to route symbols to the appropriate detector
stack.  Crypto-native detectors (OI, Funding, Liquidations) are
unchanged for ``crypto_perp`` symbols, while forex symbols route to
substitute detectors with different cadence and signal interpretation.

Substitution table (SRB-AYUMI-008 §4.1)
========================================

| Crypto detector   | Forex substitute                      | Cadence  |
|-------------------|---------------------------------------|----------|
| Open Interest     | COT net non-commercial positioning    | Weekly   |
| Funding Rate      | Central-bank rate differential        | Daily    |
| Liquidations      | Order-flow / DOM pressure proxies     | Intraday |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SymbolType(str, Enum):
    """Asset-class classification for a trading instrument.

    Members
    -------
    crypto_perp : Crypto perpetual futures (e.g. BTCUSDT perp)
    crypto_spot : Crypto spot (e.g. BTCUSDT)
    forex_major : Major FX pair (e.g. EURUSD, USDJPY, GBPUSD)
    forex_cross : Cross FX pair without USD (e.g. EURJPY, GBPJPY)
    forex_exotic : Exotic FX pair (e.g. USDTRY, USDMXN)
    metal : Precious/base metals (e.g. XAUUSD, XAGUSD)
    index : Broad market index (e.g. US500, NAS100)
    """

    crypto_perp = "crypto_perp"
    crypto_spot = "crypto_spot"
    forex_major = "forex_major"
    forex_cross = "forex_cross"
    forex_exotic = "forex_exotic"
    metal = "metal"
    index = "index"


# Detector-stack mapping per symbol type.
# Maps each symbol type to the list of detector names that should be
# active for that asset class.  Forex substitutes are placeholders —
# the actual detector modules are wired in separate cards (COT, FRED, etc.).
DETECTOR_STACK: dict[SymbolType, list[str]] = {
    SymbolType.crypto_perp: [
        "open_interest",
        "funding_rate",
        "liquidations",
    ],
    SymbolType.crypto_spot: [
        "open_interest",
    ],
    SymbolType.forex_major: [
        "cot_positioning",  # Weekly — substitute for open_interest
        "rate_differential",  # Daily — substitute for funding_rate
        "order_flow_proxy",  # Intraday — substitute for liquidations
    ],
    SymbolType.forex_cross: [
        "cot_positioning",
        "rate_differential",
        "order_flow_proxy",
    ],
    SymbolType.forex_exotic: [
        "cot_positioning",
        "rate_differential",
    ],
    SymbolType.metal: [
        "cot_positioning",
        "rate_differential",
    ],
    SymbolType.index: [
        "cot_positioning",
    ],
}


@dataclass(frozen=True)
class Instrument:
    """A trading instrument with symbol-type classification.

    Attributes
    ----------
    symbol : str
        Trading symbol (e.g. "EURUSD", "BTCUSDT").
    symbol_type : SymbolType
        Asset-class tag used for detector routing.
    pip_size : float
        Price change per pip (e.g. 0.0001 for EURUSD).
    lot_size : int
        Units per lot (e.g. 100000 for forex, 100 for XAUUSD).
    pip_value_per_lot : float
        USD value of 1 pip movement per 1 lot.
    """

    symbol: str
    symbol_type: SymbolType = SymbolType.forex_major
    pip_size: float = 0.0001
    lot_size: int = 100_000
    pip_value_per_lot: float = 10.0

    @property
    def is_forex(self) -> bool:
        """True if this instrument is any forex type (major/cross/exotic)."""
        return self.symbol_type in (
            SymbolType.forex_major,
            SymbolType.forex_cross,
            SymbolType.forex_exotic,
        )

    @property
    def is_crypto(self) -> bool:
        """True if this instrument is crypto (perp or spot)."""
        return self.symbol_type in (
            SymbolType.crypto_perp,
            SymbolType.crypto_spot,
        )

    @property
    def detector_stack(self) -> list[str]:
        """List of detector names active for this instrument's symbol type."""
        return DETECTOR_STACK.get(self.symbol_type, [])


# ---------------------------------------------------------------------------
# Default instrument registry
# ---------------------------------------------------------------------------
# Extends the legacy INSTRUMENTS dict from risk/sl_position_sizer.py with
# symbol_type.  Existing crypto symbols default to crypto_perp.
DEFAULT_INSTRUMENTS: dict[str, Instrument] = {
    # Forex majors
    "EURUSD": Instrument(
        "EURUSD",
        SymbolType.forex_major,
        pip_size=0.0001,
        lot_size=100_000,
        pip_value_per_lot=10.0,
    ),
    "GBPUSD": Instrument(
        "GBPUSD",
        SymbolType.forex_major,
        pip_size=0.0001,
        lot_size=100_000,
        pip_value_per_lot=10.0,
    ),
    "USDJPY": Instrument(
        "USDJPY",
        SymbolType.forex_major,
        pip_size=0.01,
        lot_size=100_000,
        pip_value_per_lot=6.5,
    ),
    # Metals
    "XAUUSD": Instrument("XAUUSD", SymbolType.metal, pip_size=0.1, lot_size=100, pip_value_per_lot=10.0),
}


def classify_symbol(symbol: str) -> SymbolType:
    """Heuristic classification of a symbol string to a SymbolType.

    Used as a fallback when no explicit ``symbol_type`` is registered.
    The heuristic checks common patterns:

    - Ends with ``"USDT"`` and not in a known forex pattern → crypto
    - Contains ``"XAU"`` or ``"XAG"`` → metal
    - 6-char alphabetic where USD/GBP/EUR/JPY are in positions → forex_major
    - Otherwise → forex_major (safe default)

    Parameters
    ----------
    symbol : str
        Uppercase trading symbol.

    Returns
    -------
    SymbolType
    """
    s = symbol.upper().strip()

    # Crypto detection
    if s.endswith("USDT") or s.endswith("PERP"):
        return SymbolType.crypto_perp
    if s in ("BTCUSD", "ETHUSD"):
        return SymbolType.crypto_spot

    # Metal detection
    if "XAU" in s or "XAG" in s:
        return SymbolType.metal

    # Index detection
    if s in ("US500", "NAS100", "US30", "US100", "GER40", "UK100"):
        return SymbolType.index

    # Forex major detection — pairs containing USD, EUR, GBP, JPY, etc.
    forex_major_currencies = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"}
    if len(s) == 6:
        first = s[:3]
        second = s[3:]
        if first in forex_major_currencies and second in forex_major_currencies:
            return SymbolType.forex_major

    # Forex exotic fallback
    return SymbolType.forex_exotic
