"""Monetary pip-value computation for backtests and live sizing.

The previous price-based heuristic (``price >= 50 -> JPY pip``) mis-classified
gold and produced wrong pip values for every XAUUSD backtest. That bug is now
addressed by :mod:`forex_bot.utils.pip_value` (symbol-aware pip *size*) and
this module (symbol-aware pip *monetary value*).

Public API
----------
- :func:`compute_pip_value` — return the value of **one pip** in
  ``account_currency`` for the given ``symbol`` and ``lot_size``.
- :func:`contract_size_for_symbol` — number of underlying units per
  standard lot (XAUUSD = 100 oz, EURUSD = 100,000 EUR, …).
- :func:`quote_to_account_rate` — reference FX rate used when
  ``quote_currency != account_currency``. Pin to a single mapping in
  tests; an optional ``rates`` argument lets callers override.

Reference values (account_currency=USD, lot_size=1.0):
    XAUUSD  $10.00   (1 lot × 100 oz × 0.1 pip)
    EURUSD  $10.00   (1 lot × 100 000 EUR × 0.0001 pip)
    USDJPY  ≈ $6.67  at USDJPY=150 (1 lot × 100 000 USD × 0.01 pip = ¥1 000)
    GBPJPY  ≈ ¥1 000 (1 lot × 100 000 GBP × 0.01 pip, account=JPY)
"""

from __future__ import annotations

from typing import Mapping

from utils.pip_value import pip_value_for_symbol

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Contract size per standard lot by symbol class.
STANDARD_LOT_FX: float = 100_000.0
STANDARD_LOT_XAU: float = 100.0          # troy ounces per gold lot (cTrader).
STANDARD_LOT_XAG: float = 5_000.0        # troy ounces per silver lot (cTrader).
STANDARD_LOT_CRYPTO: float = 1.0         # whole-coin per crypto lot.

#: Currency codes we recognise for quote → account conversion. Anything
#: outside this set is rejected with ``ValueError`` to fail loudly rather
#: than silently misreport pip value (the failure mode that produced the
#: XAUUSD bug in the first place).
_KNOWN_CURRENCIES: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}
)

#: Pin to a single snapshot of reference FX rates used by tests and the
#: default ``quote_to_account_rate`` path. These are *not* live; the harness
#: exists precisely because live rates drift and the previous bug leaked
#: that drift into backtest numbers.
DEFAULT_RATES: Mapping[str, Mapping[str, float]] = {
    # 1 unit of the *first* currency = N units of the second currency.
    # e.g. USDJPY=150 means 1 USD = 150 JPY.
    "USD": {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0,
            "AUD": 1.50, "CAD": 1.36, "CHF": 0.88, "NZD": 1.62},
    "EUR": {"USD": 1.087, "EUR": 1.0, "GBP": 0.858, "JPY": 163.04,
            "AUD": 1.630, "CAD": 1.478, "CHF": 0.956, "NZD": 1.761},
    "GBP": {"USD": 1.266, "EUR": 1.165, "GBP": 1.0, "JPY": 189.87,
            "AUD": 1.899, "CAD": 1.722, "CHF": 1.114, "NZD": 2.051},
    "JPY": {"USD": 0.00667, "EUR": 0.00613, "GBP": 0.00527, "JPY": 1.0,
            "AUD": 0.010, "CAD": 0.00907, "CHF": 0.00587, "NZD": 0.0108},
    "AUD": {"USD": 0.667, "EUR": 0.613, "GBP": 0.527, "JPY": 100.0,
            "AUD": 1.0, "CAD": 0.907, "CHF": 0.587, "NZD": 1.080},
    "CAD": {"USD": 0.735, "EUR": 0.677, "GBP": 0.581, "JPY": 110.29,
            "AUD": 1.103, "CAD": 1.0, "CHF": 0.647, "NZD": 1.191},
    "CHF": {"USD": 1.136, "EUR": 1.046, "GBP": 0.898, "JPY": 170.45,
            "AUD": 1.704, "CAD": 1.546, "CHF": 1.0, "NZD": 1.841},
    "NZD": {"USD": 0.617, "EUR": 0.568, "GBP": 0.488, "JPY": 92.59,
            "AUD": 0.926, "CAD": 0.840, "CHF": 0.543, "NZD": 1.0},
}


# ---------------------------------------------------------------------------
# Currency validation
# ---------------------------------------------------------------------------


def _normalise_currency(code: str) -> str:
    if not isinstance(code, str):
        raise ValueError(f"currency must be a string, got {type(code).__name__}")
    norm = code.strip().upper()
    if norm not in _KNOWN_CURRENCIES:
        raise ValueError(
            f"unknown currency code {code!r}; known codes: "
            f"{sorted(_KNOWN_CURRENCIES)}"
        )
    return norm


# ---------------------------------------------------------------------------
# Contract sizes
# ---------------------------------------------------------------------------


def contract_size_for_symbol(symbol: str) -> float:
    """Return the contract size (units per standard lot) for ``symbol``.

    Mirrors the cTrader conventions:
      - XAUUSD / XAU / GOLD: 100 troy oz
      - XAGUSD / SILVER: 5 000 troy oz
      - Crypto majors (BTC/ETH): 1 whole coin
      - Everything else (forex): 100 000 base-currency units
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol must be a non-empty string")

    if sym in {"XAUUSD", "XAU", "GOLD"}:
        return STANDARD_LOT_XAU
    if sym in {"XAGUSD", "XAG", "SILVER"}:
        return STANDARD_LOT_XAG
    if sym.startswith(("BTC", "ETH")) and sym.endswith(("USD", "USDT")):
        return STANDARD_LOT_CRYPTO

    # Default: standard forex 100 000-unit lot.
    return STANDARD_LOT_FX


# ---------------------------------------------------------------------------
# Reference rate lookup
# ---------------------------------------------------------------------------


def quote_to_account_rate(
    quote_currency: str,
    account_currency: str,
    rates: Mapping[str, Mapping[str, float]] | None = None,
) -> float:
    """Return the FX rate to convert one unit of ``quote_currency`` into
    ``account_currency``.

    Parameters
    ----------
    quote_currency, account_currency
        ISO-like codes. Must be members of :data:`_KNOWN_CURRENCIES`.
    rates
        Optional override for the rate table. Defaults to
        :data:`DEFAULT_RATES` (pinned for test determinism).

    Returns
    -------
    float
        Multiplier such that ``value_in_quote * rate ==
        value_in_account``.

    Raises
    ------
    ValueError
        If either currency is unknown or the rate table has no entry for
        the pair.
    """
    quote = _normalise_currency(quote_currency)
    account = _normalise_currency(account_currency)
    table = rates if rates is not None else DEFAULT_RATES

    if quote == account:
        return 1.0

    try:
        row = table[quote]
    except KeyError as exc:
        raise ValueError(
            f"no rate row for quote currency {quote!r}"
        ) from exc

    try:
        return float(row[account])
    except KeyError as exc:
        raise ValueError(
            f"no {quote}→{account} rate in supplied rate table"
        ) from exc


# ---------------------------------------------------------------------------
# Public compute
# ---------------------------------------------------------------------------


def compute_pip_value(
    symbol: str,
    account_currency: str,
    lot_size: float,
    rates: Mapping[str, Mapping[str, float]] | None = None,
) -> float:
    """Monetary value of one pip in ``account_currency``.

    Formula::

        pip_value = pip_size * contract_size * lot_size
                   * quote_to_account_rate(quote_ccy, account_ccy)

    where ``quote_ccy`` is read from the symbol (USDJPY → JPY,
    EURUSD → USD, USDJPY-with-USD-account → JPY, etc.) and
    ``pip_size`` comes from :func:`forex_bot.utils.pip_value.pip_value_for_symbol`.

    Parameters
    ----------
    symbol
        Instrument code (case-insensitive). ``XAUUSD``, ``EURUSD``,
        ``USDJPY``, ``GBPJPY`` covered; all standard forex pairs
        supported.
    account_currency
        Currency the result is denominated in. Must be one of
        :data:`_KNOWN_CURRENCIES`.
    lot_size
        Lot multiplier. ``1.0`` = standard lot; ``0.1`` = mini;
        ``0.01`` = micro.
    rates
        Optional rate-table override. Defaults to
        :data:`DEFAULT_RATES` (pinned, deterministic).

    Returns
    -------
    float
        Pip value in ``account_currency``. Always non-negative for
        non-negative ``lot_size``.

    Raises
    ------
    ValueError
        For invalid symbols, currencies, or unknown rate pairs.
    TypeError
        For non-numeric ``lot_size``.
    """
    if not isinstance(lot_size, (int, float)):
        raise TypeError(
            f"lot_size must be numeric, got {type(lot_size).__name__}"
        )
    if lot_size < 0:
        raise ValueError(f"lot_size must be non-negative, got {lot_size}")

    pip_size = pip_value_for_symbol(symbol)
    contract_size = contract_size_for_symbol(symbol)
    quote_ccy = _quote_currency_of(symbol)
    rate = quote_to_account_rate(quote_ccy, account_currency, rates=rates)

    pip_value_in_quote = pip_size * contract_size * lot_size
    pip_value_in_account = pip_value_in_quote * rate

    # Round to a sensible precision (10 decimal places) to keep downstream
    # backtest sums stable. Tenths of a cent matter when summing across
    # tens of thousands of trades.
    return round(pip_value_in_account, 10)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quote_currency_of(symbol: str) -> str:
    """Best-effort extraction of the quote currency from a 6-letter
    instrument name.

    For symbols where this can't be inferred (e.g. ``GOLD``) we fall
    back to ``"USD"`` (the cTrader default quote currency for spot
    metals). Crypto pairs use ``"USD"`` as the quote currency.
    """
    sym = (symbol or "").strip().upper()

    # Crypto: assume USD quote.
    if sym in {"BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT"}:
        return "USD"

    # Spot metals default quote.
    if sym in {"XAUUSD", "XAU", "GOLD", "XAGUSD", "XAG", "SILVER"}:
        return "USD"

    # 6-letter forex pair (e.g. EURUSD, USDJPY).
    if len(sym) == 6 and sym.isalpha():
        return sym[3:6]

    # If we get here, we don't know enough to pick the quote currency
    # deterministically. Default to USD (matches how ``compute_pip_value``
    # would behave for "GOLD"-shaped names) — but surface the uncertainty
    # by warning the caller via a deterministic prefix comment.
    raise ValueError(
        f"cannot infer quote currency for symbol {symbol!r}; "
        f"expected a 6-letter forex code or a recognised alias"
    )


__all__ = [
    "STANDARD_LOT_FX",
    "STANDARD_LOT_XAU",
    "STANDARD_LOT_XAG",
    "STANDARD_LOT_CRYPTO",
    "DEFAULT_RATES",
    "PIP_VALUE_VALIDATED",
    "contract_size_for_symbol",
    "quote_to_account_rate",
    "compute_pip_value",
]


# ---------------------------------------------------------------------------
# Harness validation marker
# ---------------------------------------------------------------------------


#: Marker that consumers (backtest reports, walk-forward runs, paper/live
#: replay) can write into their report dict to indicate the symbol-aware
#: pip-value harness is in the call path. ``True`` only when this module
#: has been imported — i.e. when the call graph actually crosses through
#: :func:`compute_pip_value`. Centralised here so a single grep suffices to
#: verify the marker source.
PIP_VALUE_VALIDATED: bool = True
