"""Tests for the FRED + carry signal provider.

Covers:
* Static fallback path (no network / no API key).
* Composition of FRED + ECB SDMX + MT5 swap feeds.
* Regime classification for high / low / flipped differentials.
* USDJPY regime-shift scenario: the acceptance-criterion pivot.
* ``score_with_carry`` integration with the real ConfidenceEngine.
* Edge cases: unknown symbols, missing swap data, neutral differentials.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src" / "forex_bot"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from data.carry_signals import (  # noqa: I001
    CarryRegime,
    CarrySignalProvider,
    ECBSDMXProvider,
    InMemorySwapProvider,
    MT5SwapFileProvider,
    base_currency,
    quote_currency,
    score_with_carry,
)
from data.fred_fetcher import FredFetcher, StaticFredRates

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StaticRates:
    """Minimal CompositeRateProvider stand-in for tests."""

    def __init__(self, rates: dict[str, float]) -> None:
        self._rates = {k.upper(): float(v) for k, v in rates.items()}

    def latest_rate(self, currency: str) -> float | None:
        return self._rates.get(currency.upper())


def _provider(
    rates: dict[str, float] | None = None,
    swaps: dict[str, tuple[float, float]] | None = None,
    min_diff: float = 0.5,
) -> CarrySignalProvider:
    rate_provider = _StaticRates(rates or {})
    swap_provider = InMemorySwapProvider(swaps or {})
    return CarrySignalProvider(
        rate_provider=rate_provider,
        swap_provider=swap_provider,
        min_rate_diff=min_diff,
    )


# ---------------------------------------------------------------------------
# FredFetcher / StaticFredRates tests
# ---------------------------------------------------------------------------


class TestStaticFredRates(unittest.TestCase):
    def test_filters_by_date_range(self):
        src = StaticFredRates(
            [
                {"date": "2018-01-01", "rate": 2.27},
                {"date": "2020-01-01", "rate": 0.09},
                {"date": "2023-01-01", "rate": 5.33},
            ]
        )
        rows = src.get_series("FEDFUNDS", start_date="2019-01-01", end_date="2022-12-31")
        self.assertEqual([r["date"] for r in rows], ["2020-01-01"])

    def test_default_table_includes_known_years(self):
        src = StaticFredRates()
        rows = src.get_series("FEDFUNDS", start_date="2010-01-01", end_date="2025-12-31")
        # Should span every year in the fallback table.
        self.assertGreaterEqual(len(rows), 5)
        self.assertEqual(rows[-1]["date"][:4], "2025")


class TestFredFetcherOffline(unittest.TestCase):
    def test_offline_fallback_yields_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FredFetcher(
                api_key="",
                cache_path=str(Path(tmp) / "fred_cache.json"),
                force_offline=True,
            )
            rows = fetcher.get_us_policy_rate_history(start_date="2020-01-01")
            self.assertGreater(len(rows), 0)
            self.assertTrue(all("date" in r and "rate" in r for r in rows))
            # Sorted oldest → newest
            self.assertEqual(rows, sorted(rows, key=lambda r: r["date"]))

    def test_latest_rate_returns_float(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FredFetcher(
                api_key="",
                cache_path=str(Path(tmp) / "fred_cache.json"),
                force_offline=True,
            )
            latest = fetcher.latest_us_policy_rate()
            self.assertIsNotNone(latest)
            self.assertIsInstance(latest, float)

    def test_cache_persists_between_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fred_cache.json"
            fetcher = FredFetcher(
                api_key="",
                cache_path=str(cache_path),
                force_offline=False,
                source=StaticFredRates(
                    [
                        {"date": "2024-01-01", "rate": 5.33},
                        {"date": "2025-01-01", "rate": 4.58},
                    ]
                ),
            )
            fetcher.get_us_policy_rate_history(start_date="2020-01-01")
            self.assertTrue(cache_path.exists())
            payload = json.loads(cache_path.read_text())
            self.assertTrue(any(k.startswith("FEDFUNDS:") for k in payload))
            # Reload from disk
            fetcher2 = FredFetcher(
                api_key="",
                cache_path=str(cache_path),
                force_offline=False,
                source=StaticFredRates(
                    [
                        {
                            "date": "1900-01-01",
                            "rate": 99.0,
                        },  # would dominate if real call made
                    ]
                ),
            )
            rows = fetcher2.get_us_policy_rate_history(start_date="2020-01-01")
            # We should still see 5.33 from cache, not 99.0 from source.
            self.assertEqual(rows[-1]["rate"], 4.58)

    def test_uses_explicit_static_source_when_provided(self):
        fetcher = FredFetcher(
            api_key="",
            cache_path=None,
            force_offline=False,
            source=StaticFredRates(
                [
                    {"date": "2026-01-01", "rate": 4.25},
                    {"date": "2026-07-01", "rate": 4.75},
                ]
            ),
        )
        latest = fetcher.latest_us_policy_rate()
        self.assertEqual(latest, 4.75)


# ---------------------------------------------------------------------------
# ECB SDMX provider tests
# ---------------------------------------------------------------------------


class TestECBSDMXProvider(unittest.TestCase):
    def test_static_fallback_yields_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ECBSDMXProvider(cache_path=str(Path(tmp) / "ecb.json"))
            latest = provider.latest_eur_rate()
            self.assertIsNotNone(latest)
            self.assertGreater(latest, 0.0)

    def test_non_eur_currency_returns_none(self):
        provider = ECBSDMXProvider()
        self.assertIsNone(provider.latest_rate("USD"))

    def test_injected_fetcher_used_and_parsed(self):
        captured = {}

        def fake_fetcher(url: str) -> str:
            captured["url"] = url
            return json.dumps(
                {
                    "dataSets": [
                        {
                            "observations": {
                                "0": [3.40],
                                "1": [3.65],
                            }
                        }
                    ]
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            provider = ECBSDMXProvider(
                cache_path=str(Path(tmp) / "ecb.json"),
                fetcher=fake_fetcher,
            )
            # First call → calls fetcher, stores in cache.
            self.assertIsNotNone(provider.latest_eur_rate())
            self.assertIn("data-api.ecb.europa.eu", captured["url"])

    def test_invalid_payload_falls_back_to_static(self):
        def bad(url: str) -> str:
            return "{not-json"

        with tempfile.TemporaryDirectory() as tmp:
            provider = ECBSDMXProvider(
                cache_path=str(Path(tmp) / "ecb.json"),
                fetcher=bad,
            )
            latest = provider.latest_eur_rate()
            self.assertIsNotNone(latest)  # static fallback still works


# ---------------------------------------------------------------------------
# MT5 swap provider tests
# ---------------------------------------------------------------------------


class TestMT5SwapFileProvider(unittest.TestCase):
    def test_in_memory_records(self):
        prov = MT5SwapFileProvider(
            records=[
                {
                    "symbol": "USDJPY",
                    "long_swap_points": 12.5,
                    "short_swap_points": -15.0,
                },
            ]
        )
        self.assertEqual(prov.get_swap_points("USDJPY"), (12.5, -15.0))
        self.assertEqual(prov.get_swap_points("usdjpy"), (12.5, -15.0))

    def test_missing_symbol_returns_none(self):
        prov = MT5SwapFileProvider(records=[])
        self.assertIsNone(prov.get_swap_points("XYZ"))

    def test_disk_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "swaps.csv"
            csv_path.write_text("symbol,long_swap_points,short_swap_points,updated_at\nEURUSD,8.5,-10.0,2026-07-01\n")
            prov = MT5SwapFileProvider(csv_path=str(csv_path))
            self.assertEqual(prov.get_swap_points("EURUSD"), (8.5, -10.0))

    def test_missing_disk_file_yields_no_swaps(self):
        prov = MT5SwapFileProvider(csv_path="/nonexistent/path.csv")
        self.assertIsNone(prov.get_swap_points("USDJPY"))


# ---------------------------------------------------------------------------
# CarrySignalProvider unit tests
# ---------------------------------------------------------------------------


class TestCarryClassification(unittest.TestCase):
    def test_positive_long_usdjpy_high_us_low_jpy(self):
        # USD 5.50%, JPY 0.25% → long USDJPY earns the differential.
        provider = _provider(
            rates={"USD": 5.50, "JPY": 0.25},
            swaps={"USDJPY": (12.5, -15.0)},
        )
        signal = provider.evaluate_carry("USDJPY", "long")
        self.assertEqual(signal.regime, CarryRegime.POSITIVE)
        self.assertAlmostEqual(signal.rate_diff, 5.25, places=4)
        self.assertIsNotNone(signal.to_confluence())

    def test_negative_long_usdjpy_after_regime_shift(self):
        # Regime shift: USD falls below JPY. Long USDJPY now pays the diff.
        provider = _provider(
            rates={"USD": 0.50, "JPY": 5.00},
            swaps={"USDJPY": (-2.0, 10.0)},
        )
        signal = provider.evaluate_carry("USDJPY", "long")
        self.assertEqual(signal.regime, CarryRegime.NEGATIVE)
        # NEGATIVE carry must NOT add a positive confluence — the engine
        # would otherwise boost in both directions symmetrically. Signal
        # itself still carries the regime for diagnostics.
        self.assertIsNone(signal.to_confluence())
        self.assertEqual(signal.regime, CarryRegime.NEGATIVE)

    def test_neutral_when_diff_below_threshold(self):
        # Sub-threshold diff → carry doesn't matter. Diff strictly below
        # min_diff is the NEUTRAL band; diff == min_diff is the boundary
        # case (counts as carry-active, not NEUTRAL).
        provider = _provider(
            rates={"USD": 4.30, "JPY": 4.00},  # diff = 0.30 < 0.5
            min_diff=0.5,
        )
        signal = provider.evaluate_carry("USDJPY", "long")
        self.assertEqual(signal.regime, CarryRegime.NEUTRAL)
        self.assertIsNone(signal.to_confluence())

    def test_short_direction_inverts_diff(self):
        # Short USDJPY: base = USD, quote = JPY → we pay USD, earn JPY.
        # USD 0.5, JPY 5.0 → short earns JPY side, but since diff is negative,
        # we are paying more on the short → POSITIVE (carry favours short).
        provider = _provider(
            rates={"USD": 0.50, "JPY": 5.00},
        )
        signal = provider.evaluate_carry("USDJPY", "short")
        self.assertEqual(signal.regime, CarryRegime.POSITIVE)

    def test_swap_dominates_extreme_negative_roll(self):
        # Theoretical rate-positive pair but broker charges huge overnight.
        provider = _provider(
            rates={"EUR": 5.0, "USD": 1.0},  # long EURUSD would be carry-positive
            swaps={"EURUSD": (-200.0, 50.0)},  # but long pays 200pts/day
        )
        signal = provider.evaluate_carry("EURUSD", "long")
        # Swap override kicks in → NEGATIVE.
        self.assertEqual(signal.regime, CarryRegime.NEGATIVE)

    def test_unknown_symbol_returns_neutral(self):
        provider = _provider(rates={"USD": 5.0, "JPY": 0.5})
        signal = provider.evaluate_carry("ZZZZZ", "long")
        self.assertEqual(signal.regime, CarryRegime.NEUTRAL)
        self.assertIsNone(signal.rate_diff)
        self.assertIsNone(signal.to_confluence())

    def test_missing_rate_data_returns_neutral(self):
        provider = _provider(rates={"USD": 5.0})  # no JPY
        signal = provider.evaluate_carry("USDJPY", "long")
        self.assertEqual(signal.regime, CarryRegime.NEUTRAL)
        self.assertIsNone(signal.rate_diff)

    def test_invalid_direction_raises(self):
        provider = _provider(rates={"USD": 5.0, "JPY": 0.25})
        with self.assertRaises(ValueError):
            provider.evaluate_carry("USDJPY", "sideways")


class TestCarryCurrencyHelpers(unittest.TestCase):
    def test_base_and_quote_currency_lookup(self):
        self.assertEqual(base_currency("USDJPY"), "USD")
        self.assertEqual(quote_currency("USDJPY"), "JPY")
        self.assertEqual(base_currency("eurusd"), "EUR")
        self.assertIsNone(base_currency("XYZABC"))


# ---------------------------------------------------------------------------
# ConfidenceEngine integration — REGIME-SHIFT acceptance criterion
# ---------------------------------------------------------------------------


class TestScoreWithCarryRegimeShift(unittest.TestCase):
    """
    Acceptance criterion:
        regime shift triggers confidence adjustment on USDJPY.
    """

    def _engine(self):
        # Avoid importing confidence from anywhere that pulls network side
        # effects — keep this isolated.
        from confidence.engine import ConfidenceEngine

        return ConfidenceEngine()

    def test_regime_shift_increases_confidence_for_aligned_direction(self):
        engine = self._engine()

        # Pre-shift: USD high, JPY low → long USDJPY is carry-positive.
        provider_positive = _provider(
            rates={"USD": 5.50, "JPY": 0.25},
            swaps={"USDJPY": (12.0, -14.0)},
        )
        result_long = score_with_carry(
            engine,
            provider_positive,
            raw_confidence=0.55,
            symbol="USDJPY",
            direction="long",
        )
        result_short = score_with_carry(
            engine,
            provider_positive,
            raw_confidence=0.55,
            symbol="USDJPY",
            direction="short",
        )

        # Both should *pass* gates; the carry-positive direction should have
        # a higher final score because the carry confluence boosts it.
        self.assertFalse(result_long.blocked)
        self.assertFalse(result_short.blocked)
        self.assertGreater(result_long.final_score, result_short.final_score)

    def test_regime_shift_flips_confidence_advantage(self):
        engine = self._engine()

        # Post-shift: USD low, JPY high → short USDJPY is carry-positive.
        provider_negative = _provider(
            rates={"USD": 0.50, "JPY": 5.00},
            swaps={"USDJPY": (-2.0, 8.0)},
        )
        result_long = score_with_carry(
            engine,
            provider_negative,
            raw_confidence=0.55,
            symbol="USDJPY",
            direction="long",
        )
        result_short = score_with_carry(
            engine,
            provider_negative,
            raw_confidence=0.55,
            symbol="USDJPY",
            direction="short",
        )

        # Short should now have the higher confidence.
        self.assertFalse(result_long.blocked)
        self.assertFalse(result_short.blocked)
        self.assertGreater(result_short.final_score, result_long.final_score)

    def test_neutral_carry_does_not_perturb_engine(self):
        engine = self._engine()

        # diff = 0.30 < default min_diff (0.5) → NEUTRAL regime.
        provider_neutral = _provider(rates={"USD": 4.30, "JPY": 4.00})
        confluences = [{"strategy": "x", "direction": "long", "timeframe": "H1"}]

        without = engine.score(
            0.55,
            symbol="USDJPY",
            direction="long",
            confluences=list(confluences),
        )
        with_carry = score_with_carry(
            engine,
            provider_neutral,
            raw_confidence=0.55,
            symbol="USDJPY",
            direction="long",
            confluences=list(confluences),
        )

        # Neutral carry injects nothing → scores should be identical.
        self.assertAlmostEqual(without.final_score, with_carry.final_score, places=6)
        # Sanity: CarrySignal itself reports NEUTRAL.
        signal = provider_neutral.evaluate_carry("USDJPY", "long")
        self.assertEqual(signal.regime, CarryRegime.NEUTRAL)

    def test_carry_disabled_skips_injection(self):
        engine = self._engine()
        provider = _provider(
            rates={"USD": 5.50, "JPY": 0.25},
        )
        without = engine.score(0.55, symbol="USDJPY", direction="long")
        with_disabled = score_with_carry(
            engine,
            provider,
            raw_confidence=0.55,
            symbol="USDJPY",
            direction="long",
            include_carry=False,
        )
        self.assertAlmostEqual(without.final_score, with_disabled.final_score, places=6)


# ---------------------------------------------------------------------------
# Realistic input tests — make sure shapes work end-to-end
# ---------------------------------------------------------------------------


class TestRealisticInputs(unittest.TestCase):
    """Sample inputs with actual outputs, as required by autobuild docs."""

    def test_sample_input_usdjpy_high_carry(self):
        provider = _provider(
            rates={"USD": 5.50, "JPY": 0.25},
            swaps={"USDJPY": (12.0, -15.0)},
        )
        signal = provider.evaluate_carry("USDJPY", "long")
        confluence = signal.to_confluence()

        actual = {
            "regime": signal.regime.value,
            "rate_diff": round(signal.rate_diff, 4),
            "base_rate": signal.base_rate,
            "quote_rate": signal.quote_rate,
            "swap": signal.swap_points,
            "confluence_strategy": confluence["strategy"],
            "confluence_direction": confluence["direction"],
            "confluence_timeframe": confluence["timeframe"],
        }
        expected = {
            "regime": "positive",
            "rate_diff": 5.25,
            "base_rate": 5.5,
            "quote_rate": 0.25,
            "swap": (12.0, -15.0),
            "confluence_strategy": "carry_regime",
            "confluence_direction": "long",
            "confluence_timeframe": "D1",
        }
        self.assertEqual(actual, expected)

    def test_sample_input_eurusd_low_carry(self):
        # Sample 2: regime-shift scenario.
        provider = _provider(
            rates={"EUR": 2.00, "USD": 1.50},
            swaps={"EURUSD": (3.0, -5.0)},
        )
        signal = provider.evaluate_carry("EURUSD", "long")
        confluence = signal.to_confluence()

        actual = {
            "regime": signal.regime.value,
            "rate_diff": round(signal.rate_diff, 4),
            "confluence_strategy": confluence["strategy"] if confluence else None,
        }
        expected = {
            "regime": "positive",
            "rate_diff": 0.5,
            "confluence_strategy": "carry_regime",
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
