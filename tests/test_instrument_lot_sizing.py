"""Tests for instrument-type-aware lot sizing (crypto_phase_a card 961aa7aa).

These tests verify that crypto USDⓈ-M perpetual contracts are sized
from the base-asset contract face (e.g. 0.001 BTC for BTCUSDT_PERP)
rather than a flat constant, while forex behavior remains bit-identical
to the legacy ``risk.sl_position_sizer`` math.

Contract faces are verified against the Binance USDⓈ-M perpetual
specification documented in
``/root/.openclaw/workspace/docs/plans/sprint-2026-10-04-crypto-phase-a.md``:

    BTCUSDT_PERP  0.001 BTC per contract
    ETHUSDT_PERP  0.01  ETH per contract
    SOLUSDT_PERP  1     SOL per contract

The fixture prices below reflect a realistic reference snapshot
(2026-10-04 spot, not live market data) so any deviation in
notional parity surfaces in CI without external network calls.
"""

from __future__ import annotations

import pytest
from confidence.symbol_type_gating import SymbolTypeGate
from models.instrument import (
    DEFAULT_INSTRUMENTS,
    Instrument,
    SymbolType,
    classify_symbol,
)
from risk.sl_position_sizer import (
    INSTRUMENTS,
    SLPositionSizer,
    _compute_position_risk_usd,
)

# ---------------------------------------------------------------------------
# Reference price snapshot — synthetic, NOT live market data.
# ---------------------------------------------------------------------------
BTC_REFERENCE_PRICE = 100_000.0   # USD per BTC
ETH_REFERENCE_PRICE = 4_000.0     # USD per ETH
SOL_REFERENCE_PRICE = 200.0       # USD per SOL


# ---------------------------------------------------------------------------
# Instrument registry: contract face = lot_size, classification source.
# ---------------------------------------------------------------------------
class TestCryptoInstrumentRegistry:
    """Verify ``DEFAULT_INSTRUMENTS`` carries crypto perps with the
    correct Binance USDⓈ-M contract face in ``lot_size``.
    """

    def test_btcusdt_perp_registered_with_contract_face(self):
        inst = DEFAULT_INSTRUMENTS["BTCUSDT_PERP"]
        assert isinstance(inst, Instrument)
        assert inst.symbol_type == SymbolType.crypto_perp
        assert inst.lot_size == pytest.approx(0.001)
        assert inst.is_crypto is True
        assert inst.is_forex is False

    def test_ethusdt_perp_registered_with_contract_face(self):
        inst = DEFAULT_INSTRUMENTS["ETHUSDT_PERP"]
        assert inst.symbol_type == SymbolType.crypto_perp
        assert inst.lot_size == pytest.approx(0.01)
        assert inst.is_crypto is True

    def test_solusdt_perp_registered_with_contract_face(self):
        inst = DEFAULT_INSTRUMENTS["SOLUSDT_PERP"]
        assert inst.symbol_type == SymbolType.crypto_perp
        assert inst.lot_size == pytest.approx(1.0)
        assert inst.is_crypto is True

    def test_forex_instruments_bit_identical(self):
        """Forex ``lot_size`` must remain 100_000 (legacy contract)."""
        # DEFAULT_INSTRUMENTS carries the major pairs that
        # ``models/instrument.py`` registers explicitly.  AUDUSD/USDCHF/
        # USDCAD remain in the legacy ``risk.sl_position_sizer.INSTRUMENTS``
        # dict only and are out of scope for this card.
        for symbol in ("EURUSD", "GBPUSD", "USDJPY"):
            inst = DEFAULT_INSTRUMENTS[symbol]
            assert inst.lot_size == 100_000.0, symbol
            assert inst.is_forex is True, symbol
            assert inst.is_crypto is False, symbol
        # XAUUSD: legacy 100 oz per lot.
        xau = DEFAULT_INSTRUMENTS["XAUUSD"]
        assert xau.lot_size == 100.0
        assert xau.symbol_type == SymbolType.metal

    def test_sizer_instruments_forex_lot_size_unchanged(self):
        """Legacy forex entries in ``sl_position_sizer.INSTRUMENTS``
        still carry ``lot_size=100_000`` so the existing sizing tests
        remain bit-identical.
        """
        for symbol in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD"):
            assert INSTRUMENTS[symbol].lot_size == 100_000.0, symbol
        assert INSTRUMENTS["XAUUSD"].lot_size == 100.0

    def test_classify_symbol_routes_usdt_suffix_to_crypto_perp(self):
        """``classify_symbol`` (heuristic fallback) routes any ``*USDT``
        suffix to ``crypto_perp`` so unknown USDⓈ-M symbols still get
        the crypto detector stack without explicit registration.
        """
        assert classify_symbol("DOGEUSDT") == SymbolType.crypto_perp
        assert classify_symbol("BTCUSDT") == SymbolType.crypto_perp


# ---------------------------------------------------------------------------
# Sizer registry mirrors DEFAULT_INSTRUMENTS with InstrumentSpec.
# ---------------------------------------------------------------------------
class TestSizerInstrumentRegistry:
    """Verify the legacy ``INSTRUMENTS`` dict in ``sl_position_sizer``
    carries the same crypto specs so ``SLPositionSizer.calculate``
    resolves them.
    """

    def test_sizer_knows_all_three_crypto_perps(self):
        for symbol in ("BTCUSDT_PERP", "ETHUSDT_PERP", "SOLUSDT_PERP"):
            spec = INSTRUMENTS[symbol]
            assert spec.symbol == symbol
            assert spec.lot_size > 0

    def test_sizer_btcusdt_perp_lot_size_matches_contract_face(self):
        assert INSTRUMENTS["BTCUSDT_PERP"].lot_size == pytest.approx(0.001)
        assert INSTRUMENTS["ETHUSDT_PERP"].lot_size == pytest.approx(0.01)
        assert INSTRUMENTS["SOLUSDT_PERP"].lot_size == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# SLPositionSizer.calculate(): notional parity unit tests.
# ---------------------------------------------------------------------------
class TestCryptoPositionSizing:
    """Drive ``SLPositionSizer.calculate`` end-to-end and verify that
    lots × contract face × entry_price equals the intended notional,
    and risk equals the intended USD risk on a 1 % SL.
    """

    def setup_method(self):
        # Match the production setup: $10k balance, 0.5 % sniper risk
        # = $50 intended risk per trade, daily cap 3 %.  Crypto sizing
        # can produce a few hundred contracts at $50 risk on a 1 % SL
        # (e.g. ETHUSDT_PERP → 125 contracts), so raise the legacy
        # forex cap of 1.0 lot.  Daily cap and open-risk cap also
        # raised so unrelated blockers don't fire on the test fixture.
        self.sizer = SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=10_000.0,        # crypto lots are contracts; raise cap
            daily_risk_cap_pct=0.99,      # don't trip daily cap on this fixture
            max_positions_per_symbol=10_000,
            max_total_open_risk=1_000_000.0,
        )

    # --- BTCUSDT_PERP -------------------------------------------------

    def test_btcusdt_perp_sizing_1pct_sl_long(self):
        """Long BTCUSDT_PERP at $100k with 1 % SL → 50 contracts.

        Intended risk: $50 (0.5 % of $10k balance).
        Notional      : 50 contracts × 0.001 BTC × $100k = $5,000.
        Risk on SL hit: $5,000 × 1 % = $50 ✓
        """
        result = self.sizer.calculate(
            "BTCUSDT_PERP",
            entry_price=BTC_REFERENCE_PRICE,
            sl_price=BTC_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        assert not result.blocked, result.block_reason
        # lots = risk / (sl_pips * pip_value_per_lot)
        # sl_distance_price = $1,000, pip_size = 1.0 → 1000 pips
        # pip_value_per_lot = 0.001 BTC * $100,000 = $100 / pip / lot
        # lots = $50 / (1000 * $100) = 0.0005 → rounded up to 0.01
        # The sizer enforces a 0.01 lot floor on sub-floor calcs.
        assert result.sl_distance_pips == pytest.approx(1000.0, rel=1e-6)
        assert result.risk_amount == pytest.approx(50.0, abs=0.5)
        # Notional parity: lots * lot_size * entry_price.
        notional = result.lots * 0.001 * BTC_REFERENCE_PRICE
        # Notional must be sized so that a 1 % move equals $50 risk.
        assert notional == pytest.approx(5000.0, abs=50.0)
        # Lot count must be substantially > 1 contract (legacy flat-100
        # constant would have produced fractional nonsense).
        assert result.lots > 1.0, (
            f"expected > 1 lot (50 contracts ≈ 0.05 lots of 0.001 BTC = "
            f"lot count via contract face); got {result.lots}"
        )

    def test_btcusdt_perp_sizing_short(self):
        """Short BTCUSDT_PERP works symmetrically — SL above entry."""
        result = self.sizer.calculate(
            "BTCUSDT_PERP",
            entry_price=BTC_REFERENCE_PRICE,
            sl_price=BTC_REFERENCE_PRICE * 1.01,
            profile="sniper",
        )
        assert not result.blocked
        assert result.sl_distance_pips == pytest.approx(1000.0, rel=1e-6)
        assert result.risk_amount == pytest.approx(50.0, abs=0.5)

    # --- ETHUSDT_PERP -------------------------------------------------

    def test_ethusdt_perp_sizing_1pct_sl(self):
        """Long ETHUSDT_PERP at $4k with 1 % SL.

        Intended risk: $50.
        Notional      : lots × 0.01 ETH × $4,000.
        On 1 % SL hit: notional × 1 % = $50 → notional = $5,000
                       → lots = $5,000 / (0.01 × $4,000) = 125 contracts.
        """
        result = self.sizer.calculate(
            "ETHUSDT_PERP",
            entry_price=ETH_REFERENCE_PRICE,
            sl_price=ETH_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        assert not result.blocked, result.block_reason
        # sl_distance_price = $40, pip_size = $0.10 → 400 pips
        assert result.sl_distance_pips == pytest.approx(400.0, rel=1e-6)
        assert result.risk_amount == pytest.approx(50.0, abs=0.5)
        notional = result.lots * 0.01 * ETH_REFERENCE_PRICE
        assert notional == pytest.approx(5000.0, abs=50.0)
        # 125 contracts = 1.25 lots at contract face 0.01 ETH.
        assert result.lots >= 1.0, (
            f"expected ~1.25 lots (125 contracts); got {result.lots}"
        )

    # --- SOLUSDT_PERP -------------------------------------------------

    def test_solusdt_perp_sizing_1pct_sl(self):
        """Long SOLUSDT_PERP at $200 with 1 % SL.

        Intended risk: $50.
        Notional      : lots × 1 SOL × $200.
        On 1 % SL hit: notional × 1 % = $50 → notional = $5,000
                       → lots = $5,000 / (1 × $200) = 25 contracts.
        """
        result = self.sizer.calculate(
            "SOLUSDT_PERP",
            entry_price=SOL_REFERENCE_PRICE,
            sl_price=SOL_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        assert not result.blocked, result.block_reason
        # sl_distance_price = $2, pip_size = $0.01 → 200 pips
        assert result.sl_distance_pips == pytest.approx(200.0, rel=1e-6)
        assert result.risk_amount == pytest.approx(50.0, abs=0.5)
        notional = result.lots * 1.0 * SOL_REFERENCE_PRICE
        assert notional == pytest.approx(5000.0, abs=50.0)
        # 25 contracts = 25 lots at contract face 1 SOL.
        assert result.lots >= 10.0, (
            f"expected ~25 lots; got {result.lots}"
        )

    # --- Cross-cutting: dynamic pip_value_per_lot ---------------------

    def test_crypto_pip_value_per_lot_is_dynamic(self):
        """``PositionSizeResult.pip_value`` reflects the contract face
        sizing: ``lot_size × pip_size`` (USD per pip per contract).
        For BTC at 0.001 BTC lot and $1 pip: $0.001/pip/lot.
        """
        btc_result = self.sizer.calculate(
            "BTCUSDT_PERP",
            entry_price=BTC_REFERENCE_PRICE,
            sl_price=BTC_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        assert btc_result.pip_value == pytest.approx(
            INSTRUMENTS["BTCUSDT_PERP"].lot_size * INSTRUMENTS["BTCUSDT_PERP"].pip_size,
            rel=1e-6,
        )

        eth_result = self.sizer.calculate(
            "ETHUSDT_PERP",
            entry_price=ETH_REFERENCE_PRICE,
            sl_price=ETH_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        assert eth_result.pip_value == pytest.approx(
            INSTRUMENTS["ETHUSDT_PERP"].lot_size * INSTRUMENTS["ETHUSDT_PERP"].pip_size,
            rel=1e-6,
        )

        sol_result = self.sizer.calculate(
            "SOLUSDT_PERP",
            entry_price=SOL_REFERENCE_PRICE,
            sl_price=SOL_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        assert sol_result.pip_value == pytest.approx(
            INSTRUMENTS["SOLUSDT_PERP"].lot_size * INSTRUMENTS["SOLUSDT_PERP"].pip_size,
            rel=1e-6,
        )

    def test_swarm_profile_halves_crypto_lots(self):
        """Swarm profile halves risk → halves lots (forex behaviour
        parity for crypto routes).
        """
        sniper = self.sizer.calculate(
            "BTCUSDT_PERP",
            entry_price=BTC_REFERENCE_PRICE,
            sl_price=BTC_REFERENCE_PRICE * 0.99,
            profile="sniper",
        )
        swarm = self.sizer.calculate(
            "BTCUSDT_PERP",
            entry_price=BTC_REFERENCE_PRICE,
            sl_price=BTC_REFERENCE_PRICE * 0.99,
            profile="swarm",
        )
        assert not sniper.blocked and not swarm.blocked
        assert swarm.lots == pytest.approx(sniper.lots * 0.5, abs=0.01)

    def test_crypto_unknown_symbol_still_blocked(self):
        """An unregistered crypto symbol must be blocked (not crash)."""
        result = self.sizer.calculate(
            "UNKNOWNUSDT_PERP",
            entry_price=100.0,
            sl_price=99.0,
            profile="sniper",
        )
        assert result.blocked
        assert "unknown" in result.block_reason.lower()


# ---------------------------------------------------------------------------
# SymbolTypeGate is the single classification source.
# ---------------------------------------------------------------------------
class TestSymbolTypeGateIntegration:
    """``SymbolTypeGate`` (the gate the confidence engine consumes)
    must agree with ``SLPositionSizer`` on which symbols are crypto.
    """

    def test_gate_flags_registered_crypto_perps(self):
        gate = SymbolTypeGate()
        for symbol in ("BTCUSDT_PERP", "ETHUSDT_PERP", "SOLUSDT_PERP"):
            ctx = {"symbol": symbol}
            routing = gate.route(ctx)
            assert routing.is_crypto is True, symbol
            assert routing.is_forex is False, symbol
            assert routing.symbol_type == SymbolType.crypto_perp

    def test_gate_flags_forex_as_not_crypto(self):
        gate = SymbolTypeGate()
        routing = gate.route({"symbol": "EURUSD"})
        assert routing.is_crypto is False
        assert routing.is_forex is True
        assert routing.symbol_type == SymbolType.forex_major


# ---------------------------------------------------------------------------
# Notional parity: lots × contract face × entry_price = intended notional.
# ---------------------------------------------------------------------------
class TestNotionalParity:
    """Cross-check the sizing formula directly via the lot count
    reported by the sizer.
    """

    def test_btcusdt_perp_notional_parity(self):
        """For BTCUSDT_PERP, notional = lots × 0.001 BTC × entry_price,
        and SL-hit USD risk = notional × (sl_distance_price / entry_price).
        """
        sizer = SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=10_000.0,
            daily_risk_cap_pct=0.99,
            max_positions_per_symbol=10_000,
            max_total_open_risk=1_000_000.0,
        )
        entry = BTC_REFERENCE_PRICE
        sl = entry * 0.99
        result = sizer.calculate("BTCUSDT_PERP", entry, sl, profile="sniper")
        assert not result.blocked, result.block_reason

        contract_face = INSTRUMENTS["BTCUSDT_PERP"].lot_size
        notional_btc = result.lots * contract_face
        notional_usd = notional_btc * entry
        sl_distance_pct = abs(entry - sl) / entry  # 0.01
        sl_hit_usd = notional_usd * sl_distance_pct

        # Intended risk $50 (0.5 % of $10k balance, sniper).
        # Allow generous tolerance — the sizer rounds lots to 0.01.
        assert sl_hit_usd == pytest.approx(50.0, abs=5.0), (
            f"notional_parity violated: lots={result.lots}, "
            f"contract_face={contract_face}, entry={entry}, "
            f"sl_distance_pct={sl_distance_pct}, sl_hit_usd={sl_hit_usd}"
        )

    def test_ethusdt_perp_notional_parity(self):
        sizer = SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=10_000.0,
            daily_risk_cap_pct=0.99,
            max_positions_per_symbol=10_000,
            max_total_open_risk=1_000_000.0,
        )
        entry = ETH_REFERENCE_PRICE
        sl = entry * 0.99
        result = sizer.calculate("ETHUSDT_PERP", entry, sl, profile="sniper")
        assert not result.blocked, result.block_reason

        contract_face = INSTRUMENTS["ETHUSDT_PERP"].lot_size
        notional_eth = result.lots * contract_face
        notional_usd = notional_eth * entry
        sl_distance_pct = abs(entry - sl) / entry
        sl_hit_usd = notional_usd * sl_distance_pct

        assert sl_hit_usd == pytest.approx(50.0, abs=5.0), (
            f"notional_parity violated: lots={result.lots}, "
            f"sl_hit_usd={sl_hit_usd}"
        )

    def test_solusdt_perp_notional_parity(self):
        sizer = SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=10_000.0,
            daily_risk_cap_pct=0.99,
            max_positions_per_symbol=10_000,
            max_total_open_risk=1_000_000.0,
        )
        entry = SOL_REFERENCE_PRICE
        sl = entry * 0.99
        result = sizer.calculate("SOLUSDT_PERP", entry, sl, profile="sniper")
        assert not result.blocked, result.block_reason

        contract_face = INSTRUMENTS["SOLUSDT_PERP"].lot_size
        notional_sol = result.lots * contract_face
        notional_usd = notional_sol * entry
        sl_distance_pct = abs(entry - sl) / entry
        sl_hit_usd = notional_usd * sl_distance_pct

        assert sl_hit_usd == pytest.approx(50.0, abs=5.0), (
            f"notional_parity violated: lots={result.lots}, "
            f"sl_hit_usd={sl_hit_usd}"
        )


# ---------------------------------------------------------------------------
# _compute_position_risk_usd: crypto helper parity for seeded positions.
# ---------------------------------------------------------------------------
class TestSeededPositionRisk:
    """``_compute_position_risk_usd`` (used by ``reconcile_with_broker``
    and the engine's ``_seed_existing_positions``) must compute the
    same crypto risk that ``SLPositionSizer.calculate`` would.
    """

    def test_crypto_seeded_risk_matches_live_sizing(self):
        # Live sizing: BTCUSDT_PERP, 1 contract, $1k drop.
        live = _compute_position_risk_usd(
            symbol="BTCUSDT_PERP",
            entry_price=BTC_REFERENCE_PRICE,
            sl_price=BTC_REFERENCE_PRICE - 1_000.0,
            lots=1.0,
        )
        # sl_distance_price = $1000, pip_size = $1 → 1000 pips
        # pip_value_per_lot = lot_size × pip_size = 0.001 × $1 = $0.001/pip/lot
        # risk = 1000 pips × 1 lot × $0.001/pip = $1.00
        assert live == pytest.approx(1.0, abs=0.001), (
            f"expected $1 risk for 1 BTC contract on $1k move; got {live}"
        )
        # Forex baseline sanity check (30-pip SL on EURUSD with 1 lot):
        eur_live = _compute_position_risk_usd(
            symbol="EURUSD",
            entry_price=1.0850,
            sl_price=1.0820,
            lots=1.0,
        )
        assert eur_live == pytest.approx(300.0, abs=0.01)


# ---------------------------------------------------------------------------
# Forex behavior bit-identical (regression).
# ---------------------------------------------------------------------------
class TestForexBehaviorBitIdentical:
    """Forex sizer math must remain bit-identical: legacy tests in
    ``tests/unit/risk/test_sl_position_sizer.py`` are unchanged and
    must stay green.  This test re-runs a representative subset so the
    card's release does not depend on the legacy file's exact assertions.
    """

    def setup_method(self):
        self.sizer = SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,
            max_lot_size=1.0,
            daily_risk_cap_pct=0.03,
            max_positions_per_symbol=10,
            max_total_open_risk=10_000.0,
        )

    def test_eurusd_30_pip_sl_yields_expected_lots(self):
        """Mirror of legacy ``test_basic_eurusd`` — unchanged."""
        result = self.sizer.calculate("EURUSD", 1.0850, 1.0820, profile="sniper")
        assert not result.blocked
        assert result.sl_distance_pips == pytest.approx(30.0)
        assert result.risk_amount == pytest.approx(50.0, abs=1.0)

    def test_xauusd_300_pip_sl_unchanged(self):
        """Mirror of legacy ``test_basic_xauusd`` — unchanged."""
        result = self.sizer.calculate("XAUUSD", 3350.0, 3320.0, profile="sniper")
        assert not result.blocked
        assert result.sl_distance_pips == pytest.approx(300.0)

    def test_forex_swarm_halves_lots(self):
        sniper = self.sizer.calculate("EURUSD", 1.0850, 1.0820, profile="sniper")
        swarm = self.sizer.calculate("EURUSD", 1.0850, 1.0820, profile="swarm")
        assert not sniper.blocked and not swarm.blocked
        assert swarm.lots == pytest.approx(sniper.lots * 0.5, abs=0.01)

