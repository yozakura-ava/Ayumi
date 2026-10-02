"""Unit-test harness for :mod:`forex_bot.backtest.pip_value`.

These tests pin the monetary pip-value computation that backtest reports
rely on. The previous price-based heuristic (``price >= 50 → JPY pip``)
mis-classified gold and corrupted every XAUUSD backtest result. This
harness exists to lock in the symbol-aware implementation so that drift
cannot silently regress.

All tests are pure-Python and deterministic — no live FX rate lookups,
no broker calls. Default reference rates are pinned in
:data:`forex_bot.backtest.pip_value.DEFAULT_RATES`; tests may override
via the ``rates`` argument to ``compute_pip_value`` when exercising
custom-rate behaviour.

Test categories
----------------
* Reference-value assertions against broker-statement numbers
* Symbol coverage (XAUUSD / EURUSD / USDJPY / GBPJPY / crypto / aliases)
* Lot-size scaling (0.01 / 0.1 / 1.0)
* Currency validation (account_currency vs base/quote edge cases)
* Error paths (invalid symbol, invalid currency, negative lot_size,
  non-numeric lot_size, missing rate pair)
* Custom-rate override determinism
"""

from __future__ import annotations

import pytest
from backtest.pip_value import (
    DEFAULT_RATES,
    PIP_VALUE_VALIDATED,
    STANDARD_LOT_CRYPTO,
    STANDARD_LOT_FX,
    STANDARD_LOT_XAU,
    compute_pip_value,
    contract_size_for_symbol,
    quote_to_account_rate,
)

# ---------------------------------------------------------------------------
# Fixtures (local — conftest.py at this path does not exist; fixtures
# declared here avoid touching conftest without spec authority to create
# a new conftest.py module).
# ---------------------------------------------------------------------------


@pytest.fixture
def usd_rates():
    """Default USD-centric rate table snippet for tests that override."""
    return {
        "USD": {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 150.0},
        "EUR": {"USD": 1.087, "EUR": 1.0, "GBP": 0.858, "JPY": 163.04},
        "GBP": {"USD": 1.266, "EUR": 1.165, "GBP": 1.0, "JPY": 189.87},
        "JPY": {"USD": 0.00667, "EUR": 0.00613, "GBP": 0.00527, "JPY": 1.0},
    }


# ---------------------------------------------------------------------------
# Reference-value assertions — the numbers backtests depend on
# ---------------------------------------------------------------------------


class TestReferenceValues:
    """Pin the monetary pip values the backtest reports quote.

    Source: broker statement (cTrader XAUUSD/EURUSD/USDJPY/GBPJPY lot=1.0,
    account=USD unless noted).
    """

    def test_xauusd_lot_one_usd(self):
        # XAUUSD: 100 oz per lot, pip size 0.1 → 1 pip = $10.
        assert compute_pip_value("XAUUSD", "USD", 1.0) == pytest.approx(10.0)

    def test_eurusd_lot_one_usd(self):
        # EURUSD: 100,000 EUR per lot, pip size 0.0001, quote=USD → $10.
        assert compute_pip_value("EURUSD", "USD", 1.0) == pytest.approx(10.0)

    def test_usdjpy_lot_one_usd_at_150(self):
        # USDJPY: 100,000 USD per lot, pip size 0.01 = 1,000 JPY per pip.
        # At USDJPY=150: 1000 JPY * USD→USD rate=1.0... wait, USDJPY quote=JPY
        # account=USD, so rate = JPY→USD = 0.00667.
        # 1000 JPY * 0.00667 = 6.67 USD per pip (rounded to 10 decimals).
        result = compute_pip_value("USDJPY", "USD", 1.0)
        assert result == pytest.approx(6.67, rel=1e-4)

    def test_gbpjpy_lot_one_jpy(self):
        # GBPJPY with account=JPY: 100,000 GBP per lot, pip size 0.01 = 1,000 JPY.
        assert compute_pip_value("GBPJPY", "JPY", 1.0) == pytest.approx(1000.0)

    def test_btcusd_lot_one_usd(self):
        # BTCUSD: 1 BTC per lot, pip size 1.0 → $1 per pip (crypto convention).
        assert compute_pip_value("BTCUSD", "USD", 1.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Symbol coverage — alias handling, case-insensitivity, crypto
# ---------------------------------------------------------------------------


class TestSymbolCoverage:
    """All known cTrader symbols and aliases must produce stable values."""

    @pytest.mark.parametrize(
        "symbol, expected",
        [
            ("XAUUSD", 10.0),
            ("xauusd", 10.0),  # case-insensitive
            ("GOLD", 10.0),  # alias
            ("XAU", 10.0),  # alias
            ("XAGUSD", 5.0),  # 5,000 oz × 0.001 = 5 USD per pip (silver lot)
            ("SILVER", 5.0),
        ],
    )
    def test_metal_symbols_usd_account(self, symbol, expected):
        result = compute_pip_value(symbol, "USD", 1.0)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_eur_account_for_eurusd(self):
        # EURUSD quote=USD, account=EUR → USD→EUR rate = 0.92.
        # 10 USD * 0.92 = 9.2 EUR per pip.
        assert compute_pip_value("EURUSD", "EUR", 1.0) == pytest.approx(9.2)

    def test_eur_account_for_xauusd_via_conversion(self):
        # XAUUSD quote=USD, account=EUR → rate 1 USD = 0.92 EUR.
        # 10 USD * 0.92 = 9.2 EUR per pip.
        assert compute_pip_value("XAUUSD", "EUR", 1.0) == pytest.approx(9.2)

    def test_jpy_account_for_xauusd(self):
        # XAUUSD quote=USD, account=JPY → rate 1 USD = 150 JPY.
        # 10 USD * 150 = 1500 JPY per pip.
        assert compute_pip_value("XAUUSD", "JPY", 1.0) == pytest.approx(1500.0)

    @pytest.mark.parametrize("symbol", ["BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT"])
    def test_crypto_lot_one_usd(self, symbol):
        # All crypto majors: 1 whole-coin per lot × $1 pip = $1 per pip.
        assert compute_pip_value(symbol, "USD", 1.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Lot-size scaling
# ---------------------------------------------------------------------------


class TestLotSizeScaling:
    """Pip value scales linearly with lot size."""

    @pytest.mark.parametrize("lot_size", [0.01, 0.1, 0.5, 1.0, 2.5])
    def test_xauusd_scales_linearly(self, lot_size):
        expected = 10.0 * lot_size
        result = compute_pip_value("XAUUSD", "USD", lot_size)
        assert result == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize("lot_size", [0.01, 0.1, 1.0])
    def test_eurusd_scales_linearly(self, lot_size):
        expected = 10.0 * lot_size
        result = compute_pip_value("EURUSD", "USD", lot_size)
        assert result == pytest.approx(expected, rel=1e-9)

    def test_zero_lot_is_zero(self):
        assert compute_pip_value("XAUUSD", "USD", 0.0) == 0.0

    def test_fractional_lot_under_one_cent(self):
        # 0.001 lots XAUUSD = $0.01 per pip — used by micro strategies.
        assert compute_pip_value("XAUUSD", "USD", 0.001) == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# Currency validation
# ---------------------------------------------------------------------------


class TestCurrencyValidation:
    """Invalid currencies must fail loudly, not silently misreport."""

    def test_unknown_account_currency_raises(self):
        with pytest.raises(ValueError, match="unknown currency code"):
            compute_pip_value("XAUUSD", "ZZZ", 1.0)

    def test_empty_account_currency_raises(self):
        with pytest.raises(ValueError, match="unknown currency code"):
            compute_pip_value("XAUUSD", "", 1.0)

    def test_currency_normalisation_lowercase(self):
        # "usd" should be accepted (normalised to "USD").
        result = compute_pip_value("XAUUSD", "usd", 1.0)
        assert result == pytest.approx(10.0)

    def test_currency_normalisation_whitespace(self):
        result = compute_pip_value("XAUUSD", "  USD  ", 1.0)
        assert result == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Lot-size validation
# ---------------------------------------------------------------------------


class TestLotSizeValidation:
    """Negative or non-numeric lot sizes must raise."""

    def test_negative_lot_raises_value_error(self):
        with pytest.raises(ValueError, match="non-negative"):
            compute_pip_value("XAUUSD", "USD", -0.1)

    def test_string_lot_raises_type_error(self):
        with pytest.raises(TypeError, match="lot_size must be numeric"):
            compute_pip_value("XAUUSD", "USD", "1.0")  # type: ignore[arg-type]

    def test_none_lot_raises_type_error(self):
        with pytest.raises(TypeError, match="lot_size must be numeric"):
            compute_pip_value("XAUUSD", "USD", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Custom-rate override
# ---------------------------------------------------------------------------


class TestCustomRateOverride:
    """The ``rates`` parameter must override the default table deterministically."""

    def test_override_shifts_result(self):
        custom = {
            "USD": {"USD": 1.0, "EUR": 2.0, "GBP": 2.0, "JPY": 100.0},
            "EUR": {"USD": 0.5, "EUR": 1.0, "GBP": 1.0, "JPY": 50.0},
            "GBP": {"USD": 0.5, "EUR": 1.0, "GBP": 1.0, "JPY": 50.0},
            "JPY": {"USD": 0.01, "EUR": 0.02, "GBP": 0.02, "JPY": 1.0},
        }
        # XAUUSD with USD→EUR=2.0: 10 USD * 2.0 = 20 EUR per pip.
        result = compute_pip_value("XAUUSD", "EUR", 1.0, rates=custom)
        assert result == pytest.approx(20.0)

    def test_override_does_not_mutate_default(self):
        custom = {
            "USD": {"USD": 1.0, "EUR": 99.0, "GBP": 1.0, "JPY": 1.0},
            "EUR": {"USD": 1.0, "EUR": 1.0, "GBP": 1.0, "JPY": 1.0},
            "GBP": {"USD": 1.0, "EUR": 1.0, "GBP": 1.0, "JPY": 1.0},
            "JPY": {"USD": 1.0, "EUR": 1.0, "GBP": 1.0, "JPY": 1.0},
        }
        # Mutating rate to 99 should not change a fresh default call.
        compute_pip_value("XAUUSD", "EUR", 1.0, rates=custom)
        fresh = compute_pip_value("XAUUSD", "EUR", 1.0)
        assert fresh == pytest.approx(9.2)  # original DEFAULT_RATES value

    def test_missing_rate_pair_raises(self, usd_rates):
        # Override with no CAD row → ValueError on USD→CAD.
        with pytest.raises(ValueError, match="USD"):
            # CAD is not in usd_rates fixture — request USDJPY conversion
            # would work, but quote→account uses the override table.
            # Use a quote currency not in the override.
            compute_pip_value("EURUSD", "CAD", 1.0, rates=usd_rates)


# ---------------------------------------------------------------------------
# Contract-size helpers
# ---------------------------------------------------------------------------


class TestContractSize:
    """Contract sizes mirror cTrader conventions."""

    @pytest.mark.parametrize(
        "symbol, expected",
        [
            ("XAUUSD", STANDARD_LOT_XAU),  # 100 oz
            ("GOLD", STANDARD_LOT_XAU),
            ("XAU", STANDARD_LOT_XAU),
            ("XAGUSD", 5000.0),
            ("BTCUSD", STANDARD_LOT_CRYPTO),
            ("ETHUSDT", STANDARD_LOT_CRYPTO),
            ("EURUSD", STANDARD_LOT_FX),
            ("USDJPY", STANDARD_LOT_FX),
            ("GBPJPY", STANDARD_LOT_FX),
        ],
    )
    def test_contract_size_for_known_symbols(self, symbol, expected):
        assert contract_size_for_symbol(symbol) == expected

    def test_empty_symbol_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            contract_size_for_symbol("")

    def test_none_symbol_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            contract_size_for_symbol(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Quote-to-account rate lookup
# ---------------------------------------------------------------------------


class TestQuoteToAccountRate:
    """Direct coverage of :func:`quote_to_account_rate`."""

    def test_same_currency_returns_one(self):
        assert quote_to_account_rate("USD", "USD") == 1.0

    def test_default_table_deterministic(self):
        # Pinned values from DEFAULT_RATES — any drift is a regression.
        assert quote_to_account_rate("USD", "EUR") == pytest.approx(0.92)
        assert quote_to_account_rate("EUR", "USD") == pytest.approx(1.087)

    def test_unknown_quote_raises(self):
        # _normalise_currency rejects unknown codes first; the downstream
        # "no rate row" error is only reached when the quote is in
        # _KNOWN_CURRENCIES but the supplied rate table is missing the row.
        with pytest.raises(ValueError, match="unknown currency code"):
            quote_to_account_rate("ZZZ", "USD")

    def test_unknown_account_raises(self, usd_rates):
        with pytest.raises(ValueError, match="no USD→CAD rate"):
            quote_to_account_rate("USD", "CAD", rates=usd_rates)


# ---------------------------------------------------------------------------
# Default-rate table integrity
# ---------------------------------------------------------------------------


class TestDefaultRatesTable:
    """Lock in the shape of :data:`DEFAULT_RATES`."""

    def test_default_rates_is_mapping(self):
        assert isinstance(DEFAULT_RATES, dict)

    def test_default_rates_has_required_currencies(self):
        # If a currency is added/removed, the dispatch table needs updating too.
        for ccy in ("USD", "EUR", "GBP", "JPY"):
            assert ccy in DEFAULT_RATES

    def test_default_rates_diagonal_is_one(self):
        # Every currency row must rate itself at 1.0 (sanity).
        for ccy, row in DEFAULT_RATES.items():
            assert row[ccy] == pytest.approx(1.0), f"{ccy}→{ccy} not 1.0"


# ---------------------------------------------------------------------------
# Harness validation marker (AC4: report flag surface)
# ---------------------------------------------------------------------------


class TestValidationMarker:
    """``PIP_VALUE_VALIDATED`` must be exported so backtest reports can
    assert the harness is wired through."""

    def test_marker_is_true(self):
        assert PIP_VALUE_VALIDATED is True

    def test_marker_is_bool_type(self):
        # Stable JSON-serialisable type — backtest reports call json.dump.
        assert isinstance(PIP_VALUE_VALIDATED, bool)
