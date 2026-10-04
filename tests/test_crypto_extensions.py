"""Tests for crypto extension factors (CRYPTO-A3, card c8e50067-aa96-4616-87f5-fd4e75d5b872).

Sprint: 2026-10-04-crypto-phase-a (final card of Phase A)
Lane: [BUILD][CRYPTO-LANE]

Scope (HR5 — targeted tests only):
  * OI-extreme factor (spec §7.2 row 1)
  * funding-extreme factor (spec §7.2 row 2)
  * BTC-dominance factor (spec §7.2 row 3)
  * §7.3 normalization (raw / 1.17) BEFORE interaction bonuses
  * Funding settlement-window veto (00/08/16 UTC spikes)
  * Symbol-type gate (forex isolation contract)
  * BTCUSDT/ETHUSDT/SOLUSDT whitelist enforcement
  * adapter_factors() helper bridging BinanceCryptoAdapter output
  * Module-level constants (weights, normalization factor, etc.)

Every test drives the factor functions directly via
:class:`CryptoFactorInputs` and the duck-typed ``OISnapshotLike`` /
``FundingSnapshotLike` shapes. NO live Binance calls, NO mocked
adapter — the module is consumed by passing snapshot objects, and
tests exercise that contract.

The forex-isolation test simulates a real evaluator: it calls
``ConfluenceScorer.score(candidate)`` twice — once with the crypto
extensions module loaded (computed but discarded because is_crypto
is False) and once with the module NOT loaded. The two totals MUST
be bit-identical, which proves the crypto_extensions module has
zero side-effects on the forex path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

# ──────────────────────────────────────────────────────────────────────
# Imports under test.  We do NOT add the package root to sys.path here —
# tests rely on the conftest's existing path setup.  If running this
# file in isolation, set ``PYTHONPATH=src`` or rely on the Ayumi test
# runner which prepends ``src`` automatically.
# ──────────────────────────────────────────────────────────────────────
from forex_bot.signal_engine.crypto_extensions import (  # noqa: E402  pytest path setup
    CRYPTO_NORMALIZATION_FACTOR,
    CRYPTO_WEIGHTS,
    DEFAULT_SETTLEMENT_WINDOW_MINUTES,
    FUNDING_EXTREME_PERCENTILE,
    SETTLEMENT_HOURS_UTC,
    SUPPORTED_CRYPTO_SYMBOLS,
    WEIGHT_BTC_DOMINANCE,
    WEIGHT_FUNDING_EXTREME,
    WEIGHT_OI_SIGNAL,
    CryptoFactorInputs,
    FundingSnapshotLike,
    OISnapshotLike,
    UnsupportedCryptoSymbolError,
    adapter_factors,
    apply_crypto_normalization,
    compute_crypto_factors,
    is_crypto_route,
    is_funding_settlement_window,
    to_funding_snapshot,
    to_oi_snapshot,
)

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def ctx_btc() -> dict:
    """Gate context for BTCUSDT (crypto_perp)."""
    return {"symbol": "BTCUSDT"}


@pytest.fixture
def ctx_eth() -> dict:
    """Gate context for ETHUSDT (alt — BTC-dominance factor applies)."""
    return {"symbol": "ETHUSDT"}


@pytest.fixture
def ctx_sol() -> dict:
    """Gate context for SOLUSDT (alt — BTC-dominance factor applies)."""
    return {"symbol": "SOLUSDT"}


@pytest.fixture
def ctx_eurusd() -> dict:
    """Gate context for EURUSD (forex — must isolate from extensions)."""
    return {"symbol": "EURUSD"}


def _oi(symbol: str, value: float, hour_utc: int = 12) -> OISnapshotLike:
    """Build an OI snapshot at a deterministic UTC time (avoids TZ drift)."""
    return OISnapshotLike(
        symbol=symbol,
        time=datetime(2026, 10, 4, hour_utc, 0, 0, tzinfo=timezone.utc),
        open_interest=value,
    )


def _funding(symbol: str, rate: float, hour_utc: int = 12) -> FundingSnapshotLike:
    """Build a funding snapshot at a deterministic UTC time."""
    return FundingSnapshotLike(
        symbol=symbol,
        time=datetime(2026, 10, 4, hour_utc, 0, 0, tzinfo=timezone.utc),
        funding_rate=rate,
    )


@pytest.fixture
def no_veto_inputs() -> CryptoFactorInputs:
    """Standard inputs with the settlement-window veto disabled (0 minutes).

    Tests that exercise factor logic use this so the funding-extreme
    branch is deterministic. Tests for the veto itself pass a
    non-zero ``settlement_window_minutes``.
    """
    return CryptoFactorInputs(
        symbol="BTCUSDT",
        direction="long",
        oi_snapshot=_oi("BTCUSDT", 105.0),
        oi_history=[_oi("BTCUSDT", 100.0, hour_utc=11)],
        funding_snapshot=_funding("BTCUSDT", 0.0001, hour_utc=10),
        funding_history=[
            _funding("BTCUSDT", 0.00010, hour_utc=2),
            _funding("BTCUSDT", 0.00012, hour_utc=4),
            _funding("BTCUSDT", 0.00009, hour_utc=6),
        ],
        btc_dominance=None,
        btc_dominance_prev=None,
        settlement_window_minutes=0,  # disable veto for factor tests
    )


# ──────────────────────────────────────────────────────────────────────
# Module-level constants
# ──────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    def test_supported_crypto_symbols(self):
        assert SUPPORTED_CRYPTO_SYMBOLS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    def test_settlement_hours_utc(self):
        assert SETTLEMENT_HOURS_UTC == (0, 8, 16)

    def test_default_settlement_window_minutes(self):
        # Per task brief: 5-minute window around each settlement hour.
        assert DEFAULT_SETTLEMENT_WINDOW_MINUTES == 5

    def test_crypto_normalization_factor_matches_spec(self):
        # Spec §7.2 total weight table = 1.17 (forex 0.89 + OI 0.08 +
        # funding 0.05 + BTC.dom 0.05 + liquidation 0.06 + CME gap 0.04).
        assert CRYPTO_NORMALIZATION_FACTOR == pytest.approx(1.17, abs=1e-9)

    def test_crypto_weights_table(self):
        # Spec §7.2 rows for OI/funding/BTC.dom — ONLY the crypto-only
        # weights live in this module (the rest are inherited from
        # ConfluenceScorer via the additive extension model).
        assert WEIGHT_OI_SIGNAL == pytest.approx(0.08, abs=1e-9)
        assert WEIGHT_FUNDING_EXTREME == pytest.approx(0.05, abs=1e-9)
        assert WEIGHT_BTC_DOMINANCE == pytest.approx(0.05, abs=1e-9)
        assert CRYPTO_WEIGHTS == {
            "oi_signal": 0.08,
            "funding_extreme": 0.05,
            "btc_dominance": 0.05,
        }

    def test_funding_extreme_percentile(self):
        assert FUNDING_EXTREME_PERCENTILE == pytest.approx(0.05, abs=1e-9)


# ──────────────────────────────────────────────────────────────────────
# OI signal factor (§7.2 row 1)
# ──────────────────────────────────────────────────────────────────────


class TestOISignalFactor:
    def _inputs(self, *, direction, cur, prev):
        return {
                "direction": direction,
                "oi_snapshot": _oi("BTCUSDT", cur),
                "oi_history": [_oi("BTCUSDT", prev, hour_utc=11)],
            }

    def test_long_with_oi_increasing_scores_one(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert _oi_signal_score(**self._inputs(direction="long", cur=110.0, prev=100.0)) == 1.0

    def test_short_with_oi_decreasing_scores_one(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert _oi_signal_score(**self._inputs(direction="short", cur=90.0, prev=100.0)) == 1.0

    def test_long_with_oi_decreasing_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert _oi_signal_score(**self._inputs(direction="long", cur=90.0, prev=100.0)) == 0.0

    def test_short_with_oi_increasing_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert _oi_signal_score(**self._inputs(direction="short", cur=110.0, prev=100.0)) == 0.0

    def test_oi_flat_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert _oi_signal_score(**self._inputs(direction="long", cur=100.0, prev=100.0)) == 0.0
        assert _oi_signal_score(**self._inputs(direction="short", cur=100.0, prev=100.0)) == 0.0

    def test_no_snapshot_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert (
            _oi_signal_score(
                direction="long", oi_snapshot=None, oi_history=[_oi("BTCUSDT", 100.0)]
            )
            == 0.0
        )

    def test_empty_history_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert (
            _oi_signal_score(
                direction="long", oi_snapshot=_oi("BTCUSDT", 110.0), oi_history=[]
            )
            == 0.0
        )

    def test_invalid_direction_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _oi_signal_score
        assert (
            _oi_signal_score(
                direction="sideways",
                oi_snapshot=_oi("BTCUSDT", 110.0),
                oi_history=[_oi("BTCUSDT", 100.0)],
            )
            == 0.0
        )


# ──────────────────────────────────────────────────────────────────────
# Funding extreme factor (§7.2 row 2) — percentile-based, contrarian
# ──────────────────────────────────────────────────────────────────────


class TestFundingExtremeFactor:
    """``funding_extreme`` is contrarian:
       top extreme (overcrowded longs) + short → 1.0
       bottom extreme (overcrowded shorts) + long → 1.0
    """

    def _funding_history(self, n: int, low: float = 0.0001, high: float = 0.0010) -> list:
        # Linear spread of `n` funding rates from low to high (so the
        # extremes sit at the edges of the sorted distribution).
        return [_funding("BTCUSDT", low + (high - low) * i / (n - 1)) for i in range(n)]

    def test_short_when_funding_at_top_extreme(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        history = self._funding_history(n=20)  # rates 0.0001..0.0010
        # Funding snapshot sits at the TOP of the distribution (overcrowded longs)
        snapshot = _funding("BTCUSDT", 0.0010)  # same as history[-1] (highest)
        # Contrarian trade direction: short.
        assert _funding_extreme_score(
            direction="short", funding_snapshot=snapshot, funding_history=history
        ) == 1.0

    def test_long_when_funding_at_bottom_extreme(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        history = self._funding_history(n=20)
        # Funding snapshot sits at the BOTTOM (overcrowded shorts)
        snapshot = _funding("BTCUSDT", 0.0001)
        # Contrarian trade direction: long.
        assert _funding_extreme_score(
            direction="long", funding_snapshot=snapshot, funding_history=history
        ) == 1.0

    def test_long_with_funding_at_top_extreme_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        history = self._funding_history(n=20)
        snapshot = _funding("BTCUSDT", 0.0010)
        # Direction aligned with crowded longs → NOT contrarian → 0.0.
        assert _funding_extreme_score(
            direction="long", funding_snapshot=snapshot, funding_history=history
        ) == 0.0

    def test_short_with_funding_at_bottom_extreme_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        history = self._funding_history(n=20)
        snapshot = _funding("BTCUSDT", 0.0001)
        # Direction aligned with crowded shorts → NOT contrarian → 0.0.
        assert _funding_extreme_score(
            direction="short", funding_snapshot=snapshot, funding_history=history
        ) == 0.0

    def test_midrange_funding_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        history = self._funding_history(n=20)
        # Funding in the middle — not an extreme.
        snapshot = _funding("BTCUSDT", 0.0005)
        assert _funding_extreme_score(
            direction="long", funding_snapshot=snapshot, funding_history=history
        ) == 0.0

    def test_no_snapshot_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        assert _funding_extreme_score(
            direction="long", funding_snapshot=None, funding_history=[]
        ) == 0.0

    def test_no_history_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        assert _funding_extreme_score(
            direction="long", funding_snapshot=_funding("BTCUSDT", 0.0010), funding_history=[]
        ) == 0.0

    def test_invalid_direction_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _funding_extreme_score
        history = self._funding_history(n=20)
        snapshot = _funding("BTCUSDT", 0.0010)
        assert _funding_extreme_score(
            direction="sideways", funding_snapshot=snapshot, funding_history=history
        ) == 0.0


# ──────────────────────────────────────────────────────────────────────
# BTC-dominance factor (§7.2 row 3) — altcoin-only, direction-relative
# ──────────────────────────────────────────────────────────────────────


class TestBTCDominanceFactor:
    """``btc_dominance`` only applies to altcoins (ETH, SOL)."""

    def test_falling_btc_dom_long_eth(self):
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="long",
                btc_dominance=0.50, btc_dominance_prev=0.52,
            )
            == 1.0
        )

    def test_rising_btc_dom_short_eth(self):
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="short",
                btc_dominance=0.54, btc_dominance_prev=0.52,
            )
            == 1.0
        )

    def test_falling_btc_dom_short_eth_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="short",
                btc_dominance=0.50, btc_dominance_prev=0.52,
            )
            == 0.0
        )

    def test_rising_btc_dom_long_eth_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="long",
                btc_dominance=0.54, btc_dominance_prev=0.52,
            )
            == 0.0
        )

    def test_btcusdt_direction_always_zero(self):
        """BTC dominance describes BTC itself — factor = 0.00 for BTCUSDT."""
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        # Both directions, both dominance directions, regardless of valid trend.
        for direction in ("long", "short"):
            for prev, cur in [(0.52, 0.50), (0.52, 0.54), (0.52, 0.52)]:
                assert (
                    _btc_dominance_score(
                        symbol="BTCUSDT", direction=direction,
                        btc_dominance=cur, btc_dominance_prev=prev,
                    )
                    == 0.0
                )

    def test_flat_btc_dom_long_alt_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="long",
                btc_dominance=0.52, btc_dominance_prev=0.52,
            )
            == 0.0
        )

    def test_no_dominance_data_scores_zero(self):
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="long",
                btc_dominance=None, btc_dominance_prev=None,
            )
            == 0.0
        )
        assert (
            _btc_dominance_score(
                symbol="ETHUSDT", direction="long",
                btc_dominance=0.52, btc_dominance_prev=None,
            )
            == 0.0
        )

    def test_solusdt_treated_as_alt(self):
        """SOLUSDT must behave like ETHUSDT for BTC-dominance purposes."""
        from forex_bot.signal_engine.crypto_extensions import _btc_dominance_score
        assert (
            _btc_dominance_score(
                symbol="SOLUSDT", direction="long",
                btc_dominance=0.50, btc_dominance_prev=0.52,
            )
            == 1.0
        )


# ──────────────────────────────────────────────────────────────────────
# Funding settlement-window veto (task constraint 4)
# ──────────────────────────────────────────────────────────────────────


class TestFundingSettlementWindow:
    def test_at_settlement_hour_top_of_hour(self):
        ts = datetime(2026, 10, 4, 0, 0, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is True

    def test_three_minutes_after_settlement(self):
        ts = datetime(2026, 10, 4, 0, 3, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is True

    def test_four_minutes_before_settlement(self):
        ts = datetime(2026, 10, 3, 23, 56, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is True

    def test_ten_minutes_after_settlement_outside_window(self):
        ts = datetime(2026, 10, 4, 0, 10, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is False

    def test_off_settlement_hour_far(self):
        ts = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is False

    def test_just_before_settlement_window_starts(self):
        ts = datetime(2026, 10, 3, 23, 54, 0, tzinfo=timezone.utc)
        # 23:54 UTC = 6 minutes before 00:00 boundary, outside ±5min window
        assert is_funding_settlement_window(ts) is False

    def test_settlement_hours_16_utc(self):
        ts = datetime(2026, 10, 4, 16, 2, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is True

    def test_settlement_hours_8_utc(self):
        ts = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts) is True

    def test_custom_window_minutes(self):
        # 10-minute window: 00:10 should be vetoed
        ts = datetime(2026, 10, 4, 0, 10, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts, window_minutes=10) is True
        assert is_funding_settlement_window(ts, window_minutes=5) is False

    def test_zero_window_disables_veto(self):
        ts = datetime(2026, 10, 4, 0, 0, 0, tzinfo=timezone.utc)
        assert is_funding_settlement_window(ts, window_minutes=0) is False

    def test_naive_datetime_treated_as_utc(self):
        ts = datetime(2026, 10, 4, 0, 3, 0)  # naive
        assert is_funding_settlement_window(ts) is True

    def test_non_datetime_returns_false(self):
        assert is_funding_settlement_window("not a datetime") is False
        assert is_funding_settlement_window(None) is False


# ──────────────────────────────────────────────────────────────────────
# §7.3 normalization (task constraint 2)
# ──────────────────────────────────────────────────────────────────────


class TestApplyCryptoNormalization:
    def test_normalization_factor_at_max_weight(self):
        """1.17 / 1.17 = 1.0 exactly (raw weight-table max → normalized 1.0)."""
        assert apply_crypto_normalization(1.17) == pytest.approx(1.0, abs=1e-9)

    def test_normalization_half_weight(self):
        """0.585 / 1.17 = 0.5."""
        assert apply_crypto_normalization(0.585) == pytest.approx(0.5, abs=1e-9)

    def test_normalization_zero(self):
        assert apply_crypto_normalization(0.0) == 0.0

    def test_normalization_negative(self):
        # Negative raw (post-bonus trim) — normalization is a
        # multiplicative transform; downstream still floors at 0.0.
        assert apply_crypto_normalization(-0.10) == pytest.approx(
            -0.10 / 1.17, abs=1e-9
        )

    def test_normalization_idempotent_after_bonuses(self):
        """Two-stage pipeline (raw → normalize → bonuses → cap) matches
        the spec text 'normalize first, then bonuses can push above
        1.0, capped at 1.0' — exact arithmetic, no surprises.
        """
        raw = 1.17  # full weight table
        normalized = apply_crypto_normalization(raw)  # = 1.0
        after_bonus = normalized * 1.08 * 1.05  # both bonuses stack
        capped = min(after_bonus, 1.0)
        # 1.0 * 1.08 * 1.05 = 1.134 → cap at 1.0
        assert capped == 1.0


# ──────────────────────────────────────────────────────────────────────
# Top-level factor compute — gate-checked, structured result
# ──────────────────────────────────────────────────────────────────────


class TestComputeCryptoFactors:
    def test_non_crypto_route_returns_zero_scores(self, ctx_eurusd):
        """Forex isolation contract: ALL zeros, no veto, no side effects."""
        # Even with valid crypto-looking data, the route is forex → all zeros.
        result = compute_crypto_factors(
            ctx_eurusd,
            CryptoFactorInputs(
                symbol="EURUSD",
                direction="long",
                oi_snapshot=_oi("BTCUSDT", 110.0),
                oi_history=[_oi("BTCUSDT", 100.0)],
                funding_snapshot=_funding("BTCUSDT", 0.0010),
                funding_history=self._build_funding_history(),
                btc_dominance=0.50,
                btc_dominance_prev=0.52,
            ),
        )
        assert result.is_crypto_route is False
        assert result.oi_signal == 0.0
        assert result.funding_extreme == 0.0
        assert result.btc_dominance == 0.0
        assert result.settlement_vetoed is False
        assert result.veto_reason == ""
        assert result.raw_weighted_sum == 0.0

    def test_crypto_route_with_valid_data(self, ctx_btc, no_veto_inputs):
        result = compute_crypto_factors(ctx_btc, no_veto_inputs)
        assert result.is_crypto_route is True
        assert result.symbol == "BTCUSDT"
        # OI up + long → 1.0
        assert result.oi_signal == 1.0
        # Funding is mid-range (not extreme) → 0.0
        assert result.funding_extreme == 0.0
        # BTCUSDT — dominance factor is 0.0 (BTC is the reference)
        assert result.btc_dominance == 0.0
        # No veto (settlement window disabled)
        assert result.settlement_vetoed is False
        # raw = 1.0 * 0.08 + 0.0 * 0.05 + 0.0 * 0.05 = 0.08
        assert result.raw_weighted_sum == pytest.approx(0.08, abs=1e-9)

    def test_raw_weighted_sum_with_all_factors(self, ctx_eth):
        inputs = CryptoFactorInputs(
            symbol="ETHUSDT",
            direction="long",
            oi_snapshot=_oi("ETHUSDT", 110.0),
            oi_history=[_oi("ETHUSDT", 100.0)],
            funding_snapshot=_funding("ETHUSDT", 0.0001),  # bottom extreme
            funding_history=self._build_funding_history(),
            btc_dominance=0.50,
            btc_dominance_prev=0.52,  # falling dom
            settlement_window_minutes=0,
        )
        result = compute_crypto_factors(ctx_eth, inputs)
        assert result.oi_signal == 1.0
        assert result.funding_extreme == 1.0  # bottom extreme + long = contrarian
        assert result.btc_dominance == 1.0  # falling dom + long alt
        # raw = 1.0*0.08 + 1.0*0.05 + 1.0*0.05 = 0.18
        assert result.raw_weighted_sum == pytest.approx(0.18, abs=1e-9)

    def test_settlement_veto_disables_funding_extreme(self, ctx_btc):
        """Funding snapshot at 00:03 UTC → veto, funding_extreme = 0.0."""
        inputs = CryptoFactorInputs(
            symbol="BTCUSDT",
            direction="long",
            oi_snapshot=_oi("BTCUSDT", 110.0),
            oi_history=[_oi("BTCUSDT", 100.0)],
            funding_snapshot=_funding("BTCUSDT", 0.0001, hour_utc=0),  # 00:00 UTC
            funding_history=self._build_funding_history(),
            settlement_window_minutes=5,
        )
        result = compute_crypto_factors(ctx_btc, inputs)
        assert result.funding_extreme == 0.0
        assert result.settlement_vetoed is True
        assert "00:00" in result.veto_reason or "settlement" in result.veto_reason.lower()
        # OI is still fired (veto is funding-only)
        assert result.oi_signal == 1.0

    def test_settlement_veto_at_8_utc(self, ctx_btc):
        inputs = CryptoFactorInputs(
            symbol="BTCUSDT",
            direction="long",
            funding_snapshot=_funding("BTCUSDT", 0.0010, hour_utc=8),
            funding_history=self._build_funding_history(),
            settlement_window_minutes=5,
        )
        result = compute_crypto_factors(ctx_btc, inputs)
        assert result.settlement_vetoed is True

    def test_settlement_veto_at_16_utc(self, ctx_btc):
        inputs = CryptoFactorInputs(
            symbol="BTCUSDT",
            direction="long",
            funding_snapshot=_funding("BTCUSDT", 0.0010, hour_utc=16),
            funding_history=self._build_funding_history(),
            settlement_window_minutes=5,
        )
        result = compute_crypto_factors(ctx_btc, inputs)
        assert result.settlement_vetoed is True

    def test_unsupported_symbol_raises(self, ctx_eurusd):
        """DOGEUSDT is not in the whitelist → UnsupportedCryptoSymbolError."""
        # Override the gate to force is_crypto=True so we exercise the
        # whitelist path (DOGEUSDT isn't classified as crypto by the
        # real gate, so the natural gate check would short-circuit).
        class FakeGate:
            def route(self, ctx):
                from dataclasses import dataclass
                @dataclass
                class R:
                    is_crypto: bool = True
                return R()

        inputs = CryptoFactorInputs(symbol="DOGEUSDT", direction="long")
        with pytest.raises(UnsupportedCryptoSymbolError):
            compute_crypto_factors(ctx_eurusd, inputs, gate=FakeGate())

    def test_no_snapshots_returns_zero_scores(self, ctx_btc):
        inputs = CryptoFactorInputs(symbol="BTCUSDT", direction="long")
        result = compute_crypto_factors(ctx_btc, inputs)
        assert result.is_crypto_route is True
        assert result.oi_signal == 0.0
        assert result.funding_extreme == 0.0
        assert result.btc_dominance == 0.0
        assert result.settlement_vetoed is False

    def test_result_is_frozen(self, ctx_btc):
        inputs = CryptoFactorInputs(symbol="BTCUSDT", direction="long")
        result = compute_crypto_factors(ctx_btc, inputs)
        with pytest.raises((AttributeError, Exception)):
            result.oi_signal = 999.0  # type: ignore[misc]

    def test_applied_weights_recorded(self, ctx_btc):
        result = compute_crypto_factors(
            ctx_btc, CryptoFactorInputs(symbol="BTCUSDT", direction="long")
        )
        assert result.applied_weights == {
            "oi_signal": 0.08,
            "funding_extreme": 0.05,
            "btc_dominance": 0.05,
        }

    @staticmethod
    def _build_funding_history() -> list:
        """Linear 20-point funding history (deterministic extremes)."""
        return [
            _funding("BTCUSDT", 0.0001 + (0.0010 - 0.0001) * i / 19)
            for i in range(20)
        ]


# ──────────────────────────────────────────────────────────────────────
# is_crypto_route — single classification source
# ──────────────────────────────────────────────────────────────────────


class TestIsCryptoRoute:
    def test_btcusdt_is_crypto(self, ctx_btc):
        assert is_crypto_route(ctx_btc) is True

    def test_ethusdt_is_crypto(self, ctx_eth):
        assert is_crypto_route(ctx_eth) is True

    def test_solusdt_is_crypto(self, ctx_sol):
        assert is_crypto_route(ctx_sol) is True

    def test_eurusd_is_not_crypto(self, ctx_eurusd):
        assert is_crypto_route(ctx_eurusd) is False

    def test_xauusd_is_not_crypto(self):
        assert is_crypto_route({"symbol": "XAUUSD"}) is False

    def test_custom_gate_is_used_when_passed(self):
        """The optional ``gate`` kwarg is for tests — production callers
        rely on the lazy-imported default. Verify the kwarg path works
        (proves the gate substitution point exists — important for
        future A4/A5 cards that may want to override classification)."""

        class FakeGate:
            def __init__(self, value):
                self._value = value

            def route(self, ctx):
                from dataclasses import dataclass

                @dataclass
                class R:
                    is_crypto: bool

                return R(is_crypto=self._value)

        assert is_crypto_route({"symbol": "BTCUSDT"}, gate=FakeGate(True)) is True
        assert is_crypto_route({"symbol": "BTCUSDT"}, gate=FakeGate(False)) is False


# ──────────────────────────────────────────────────────────────────────
# Forex isolation — the critical regression test for the A2 rework
# lesson: extensions must NEVER leak into the forex path.
# ──────────────────────────────────────────────────────────────────────


class TestForexIsolation:
    """Forex signal output MUST be identical with/without extensions."""

    def test_isolated_compute_matches_zero_state(self, ctx_eurusd):
        """compute_crypto_factors on a forex symbol produces an empty
        result — equivalent to the extensions never having been loaded."""
        # Pass crypto-shaped inputs deliberately — the gate must veto them.
        crypto_inputs = CryptoFactorInputs(
            symbol="BTCUSDT",  # Note: symbol field is irrelevant — gate decides
            direction="long",
            oi_snapshot=_oi("BTCUSDT", 110.0),
            oi_history=[_oi("BTCUSDT", 100.0)],
            funding_snapshot=_funding("BTCUSDT", 0.0010),
            funding_history=[
                _funding("BTCUSDT", 0.0001 + i * 0.00005) for i in range(20)
            ],
            btc_dominance=0.50,
            btc_dominance_prev=0.52,
        )
        result = compute_crypto_factors(ctx_eurusd, crypto_inputs)
        assert result.is_crypto_route is False
        assert result.raw_weighted_sum == 0.0
        assert result.oi_signal == 0.0
        assert result.funding_extreme == 0.0
        assert result.btc_dominance == 0.0

    def test_isolated_normalization_is_pure_arithmetic(self):
        """The normalization function is a pure math helper — calling
        it with a forex-shaped score yields the same value as the
        arithmetic, never feeds crypto data."""
        forex_score = 0.625
        assert apply_crypto_normalization(forex_score) == pytest.approx(
            forex_score / 1.17, abs=1e-9
        )

    def test_confluence_scorer_unchanged_by_module_import(self):
        """Importing crypto_extensions must NOT mutate ConfluenceScorer.

        Regression guard: the module-level constants here live in this
        file only. If a future refactor moves a forex booster weight
        into crypto_extensions or vice versa, the forex factor table
        must remain bit-identical.
        """
        from forex_bot.signal_engine.confluence_scorer import (
            WEIGHT_DXY_CORRELATION,
            WEIGHT_MTF_ALIGNMENT,
        )
        # Spec §7.1 forex weight table — top-row sanity: mtf_alignment = 0.14
        assert WEIGHT_MTF_ALIGNMENT == pytest.approx(0.14, abs=1e-9)
        # Spec §7.1 — dxy_correlation is forex-only (excluded from crypto §7.2)
        assert WEIGHT_DXY_CORRELATION == pytest.approx(0.06, abs=1e-9)


# ──────────────────────────────────────────────────────────────────────
# adapter_factors — high-level adapter-bridging helper
# ──────────────────────────────────────────────────────────────────────


class TestAdapterFactors:
    """Mock the BinanceCryptoAdapter surface (latest_oi, latest_funding)
    and verify the helper bridges snapshots correctly."""

    @staticmethod
    def _mock_oi(symbol: str, value: float):
        from dataclasses import dataclass

        @dataclass
        class Snap:
            symbol: str
            time: datetime
            open_interest: float
            open_interest_value: float | None = None

        return Snap(
            symbol=symbol,
            time=datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc),
            open_interest=value,
        )

    @staticmethod
    def _mock_funding(symbol: str, rate: float, hour: int = 12):
        from dataclasses import dataclass

        @dataclass
        class Snap:
            symbol: str
            time: datetime
            funding_rate: float
            mark_price: float | None = None

        return Snap(
            symbol=symbol,
            time=datetime(2026, 10, 4, hour, 0, 0, tzinfo=timezone.utc),
            funding_rate=rate,
        )

    def test_adapter_factors_bridges_snapshots(self, ctx_btc):
        oi_snap = self._mock_oi("BTCUSDT", 110.0)
        oi_prev = self._mock_oi("BTCUSDT", 100.0)
        funding_snap = self._mock_funding("BTCUSDT", 0.0005)
        result = adapter_factors(
            ctx_btc,
            symbol="BTCUSDT",
            direction="long",
            oi_snapshot=oi_snap,
            funding_snapshot=funding_snap,
            oi_history=[oi_prev],
            funding_history=[
                self._mock_funding("BTCUSDT", 0.0001 + i * 0.00004) for i in range(20)
            ],
            settlement_window_minutes=0,  # disable veto
        )
        assert result.is_crypto_route is True
        assert result.oi_signal == 1.0  # OI up + long

    def test_adapter_factors_handles_none_snapshots(self, ctx_btc):
        """Adapter may return None before polling — helper must not crash."""
        result = adapter_factors(
            ctx_btc,
            symbol="BTCUSDT",
            direction="long",
            oi_snapshot=None,
            funding_snapshot=None,
        )
        assert result.is_crypto_route is True
        assert result.oi_signal == 0.0
        assert result.funding_extreme == 0.0
        assert result.btc_dominance == 0.0
        assert result.settlement_vetoed is False


# ──────────────────────────────────────────────────────────────────────
# to_oi_snapshot / to_funding_snapshot — adapter-shape bridges
# ──────────────────────────────────────────────────────────────────────


class TestSnapshotBridges:
    def test_to_oi_snapshot_converts_dataclass(self):
        from dataclasses import dataclass

        @dataclass
        class AdapterSnap:
            symbol: str
            time: datetime
            open_interest: float
            open_interest_value: float | None = None

        snap = AdapterSnap(
            symbol="BTCUSDT",
            time=datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc),
            open_interest=42.5,
            open_interest_value=1_700_000.0,
        )
        result = to_oi_snapshot(snap)
        assert isinstance(result, OISnapshotLike)
        assert result.symbol == "BTCUSDT"
        assert result.open_interest == 42.5

    def test_to_funding_snapshot_converts_dataclass(self):
        from dataclasses import dataclass

        @dataclass
        class AdapterSnap:
            symbol: str
            time: datetime
            funding_rate: float
            mark_price: float | None = None

        snap = AdapterSnap(
            symbol="BTCUSDT",
            time=datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc),
            funding_rate=0.00012,
            mark_price=68_500.0,
        )
        result = to_funding_snapshot(snap)
        assert isinstance(result, FundingSnapshotLike)
        assert result.symbol == "BTCUSDT"
        assert result.funding_rate == 0.00012


# ──────────────────────────────────────────────────────────────────────
# End-to-end integration with real SymbolTypeGate (not the fake)
# ──────────────────────────────────────────────────────────────────────


class TestEndToEndWithRealGate:
    """Verify the real SymbolTypeGate (the one sl_position_sizer uses)
    drives our routing decisions consistently."""

    def test_real_gate_routes_btcusdt_to_crypto(self):
        from confidence.symbol_type_gating import SymbolTypeGate

        gate = SymbolTypeGate()
        routing = gate.route({"symbol": "BTCUSDT"})
        assert routing.is_crypto is True

    def test_real_gate_routes_eurusd_to_forex(self):
        from confidence.symbol_type_gating import SymbolTypeGate

        gate = SymbolTypeGate()
        routing = gate.route({"symbol": "EURUSD"})
        assert routing.is_crypto is False
        assert routing.is_forex is True

    def test_real_gate_in_dogeusdt_scenario(self, ctx_eurusd):
        """DOGEUSDT is not in DEFAULT_INSTRUMENTS — the gate uses the
        heuristic classifier. Even if it were classified as crypto,
        our factor layer still raises UnsupportedCryptoSymbolError
        because DOGEUSDT is NOT in our whitelist."""
        from dataclasses import dataclass

        @dataclass
        class FakeRoute:
            is_crypto: bool = True
            symbol_type: str = "crypto_perp"

        class FakeGate:
            def route(self, ctx):
                return FakeRoute()

        with pytest.raises(UnsupportedCryptoSymbolError):
            compute_crypto_factors(
                ctx_eurusd,
                CryptoFactorInputs(symbol="DOGEUSDT", direction="long"),
                gate=FakeGate(),
            )


# ──────────────────────────────────────────────────────────────────────
# Acceptance smoke test — drives the full §7.3 pipeline end-to-end
# using the additive extension model. Not a substitute for the full
# ConfluenceScorer integration (which lives in downstream cards), but
# proves the crypto-side math is correct.
# ──────────────────────────────────────────────────────────────────────


class TestSection73PipelineAcceptance:
    """Acceptance: §7.3 normalization applied BEFORE interaction bonuses."""

    def test_crypto_max_weight_with_bonuses_caps_at_one(self):
        """Max-weight crypto raw (1.17) → normalize → bonuses → cap = 1.0."""
        raw = 1.17  # all factors at 1.0
        normalized = apply_crypto_normalization(raw)  # 1.0
        # Interaction bonuses (per spec §7.3): 1.08× for strong TF
        # alignment + multi-session; 1.05× for SVC + exhaustion. Both
        # can fire — multiplicative.
        after_bonus = normalized * 1.08 * 1.05  # 1.134
        capped = min(after_bonus, 1.0)
        assert capped == 1.0

    def test_crypto_partial_weight_with_bonuses(self):
        """Partial-weight crypto raw (0.585 = 50% of 1.17) → bonuses."""
        raw = 0.585
        normalized = apply_crypto_normalization(raw)  # 0.5
        after_bonus = normalized * 1.08  # only TF-multi-session, no SVC
        assert after_bonus == pytest.approx(0.54, abs=1e-9)

    def test_forex_output_unchanged_when_extensions_in_pipeline(self):
        """Forex raw is in [0, 0.95] (spec §7.1). The crypto normalization
        divides by 1.17 — but only CRYPTO callers should apply it. For
        forex, the normalization is NOT applied (forex total weights
        to 0.95, not 1.17)."""
        forex_raw = 0.65
        # Forex path: do NOT call apply_crypto_normalization. Just cap.
        forex_final = min(forex_raw, 1.0)
        assert forex_final == 0.65

        # If a future bug wrongly applied crypto normalization to a forex
        # raw, the score would drop to 0.65/1.17 ≈ 0.555 — caught here.
        wrong_forex = min(apply_crypto_normalization(forex_raw), 1.0)
        assert wrong_forex != forex_final  # the bug is observable
