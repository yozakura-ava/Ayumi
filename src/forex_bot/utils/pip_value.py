"""Symbol-aware pip-size lookup for cTrader instruments.

The previous price-based heuristic (price >= 50 → JPY pip) mis-classified
XAUUSD because gold's ~1900-2200 price range triggered the JPY branch.
That corrupted all XAUUSD backtest results. This module replaces that
heuristic with explicit symbol-name matching.

Pip sizes (cTrader conventions):

* **Forex non-JPY** (EURUSD, GBPUSD, AUDUSD, …): ``0.0001``
* **Forex JPY** (USDJPY, EURJPY, GBPJPY, …): ``0.01``
* **XAUUSD / GOLD**: ``0.1`` (standard cTrader gold pip)
* **XAGUSD / SILVER**: ``0.001``
* **Crypto majors** (BTCUSD, ETHUSD, …): ``1.0`` (whole-dollar pip)

Symbol matching is case-insensitive and tolerant of common aliases
(GOLD↔XAUUSD, SILVER↔XAGUSD).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Pip size for non-JPY forex pairs (5-digit brokers).
DEFAULT_PIP: float = 0.0001

#: Pip size for JPY-quoted forex pairs (3-digit brokers).
JPY_PIP: float = 0.01

#: Pip size for spot gold on cTrader.
XAU_PIP: float = 0.1

#: Pip size for spot silver on cTrader.
XAG_PIP: float = 0.001

#: Pip size for crypto majors (whole-dollar moves).
CRYPTO_PIP: float = 1.0

#: Symbols whose pip size is NOT the forex default.
_NON_DEFAULT_SYMBOLS: frozenset[str] = frozenset(
    {
        # Precious metals — explicit aliases so "GOLD" and "XAUUSD" both work.
        "XAUUSD",
        "GOLD",
        "XAU",
        "XAGUSD",
        "SILVER",
        "XAG",
        # Major crypto pairs (whole-dollar pip).
        "BTCUSD",
        "ETHUSD",
        "BTCUSDT",
        "ETHUSDT",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pip_value_for_symbol(symbol: str) -> float:
    """Return the pip size for ``symbol``.

    Parameters
    ----------
    symbol : str
        Instrument name (case-insensitive). Accepts common aliases
        like ``"GOLD"`` or ``"XAUUSD"``.

    Returns
    -------
    float
        Pip size in price units (e.g. ``0.0001`` for EURUSD, ``0.1`` for XAUUSD).

    Raises
    ------
    ValueError
        If ``symbol`` is empty or not a string.

    Examples
    --------
    >>> pip_value_for_symbol("EURUSD")
    0.0001
    >>> pip_value_for_symbol("USDJPY")
    0.01
    >>> pip_value_for_symbol("XAUUSD")
    0.1
    >>> pip_value_for_symbol("xauusd")  # case-insensitive
    0.1
    """
    if not isinstance(symbol, str):
        raise ValueError(f"symbol must be a string, got {type(symbol).__name__}")
    sym = symbol.strip().upper()
    if not sym:
        raise ValueError("symbol must be a non-empty string")

    # Precious metals (check before JPY to avoid XAUUSD ever hitting JPY branch).
    if sym in {"XAUUSD", "GOLD", "XAU"}:
        return XAU_PIP
    if sym in {"XAGUSD", "SILVER", "XAG"}:
        return XAG_PIP

    # Crypto majors — whole-dollar pip. Match BTC/ETH/USDT prefix heuristics
    # plus the explicit allow-list for safety.
    if sym in _NON_DEFAULT_SYMBOLS or (sym.startswith(("BTC", "ETH")) and sym.endswith(("USD", "USDT"))):
        return CRYPTO_PIP

    # JPY pairs.
    if "JPY" in sym:
        return JPY_PIP

    # Default: standard forex non-JPY pip.
    return DEFAULT_PIP


__all__ = [
    "DEFAULT_PIP",
    "JPY_PIP",
    "XAU_PIP",
    "XAG_PIP",
    "CRYPTO_PIP",
    "pip_value_for_symbol",
]
